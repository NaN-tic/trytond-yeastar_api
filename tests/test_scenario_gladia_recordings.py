import base64
import hmac
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from proteus import Model, Wizard
from werkzeug.test import Client

from trytond.modules.company.tests.tools import create_company, get_company
from trytond.modules.yeastar_api import gladia_api, recording_ai
from trytond.pool import Pool
from trytond.protocols.wrappers import Response
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction
from trytond.wsgi import app


class TestGladiaRecordings(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def tick(self):
        with Transaction().start(self.config.database_name, 0):
            table = Pool().get('yeastar.transcription.job').__table__()
            Transaction().connection.cursor().execute(*table.update(
                    [table.next_attempt], [None]))
        self.process.click('run_once')

    def result(self, job_id):
        localized = {lang: {'title': f'{lang} title {job_id}',
                'summary': f'- {lang} summary {job_id}\n- {lang} next step'}
            for lang in ('ca', 'es', 'en')}
        response = {'id': job_id, 'status': 'done', 'result': {
            'transcription': {
                'full_transcript': 'Original mixed conversation',
                'languages': ['es', 'ca'],
                'utterances': [
                    {'start': 0, 'end': 60, 'language': 'ca',
                        'speaker': 0, 'channel': 0, 'text': 'Catalan passage'},
                    {'start': 60, 'end': 80, 'language': 'es',
                        'speaker': 1, 'channel': 0, 'text': 'Spanish passage'}],
                },
            'translation': {'success': True, 'results': [
                {'languages': [lang], 'full_transcript': f'{lang} translated',
                    'utterances': [], 'error': None}
                for lang in ('ca', 'es', 'en')]},
            'audio_to_llm': {'success': True, 'results': [{
                'results': {
                    'prompt': gladia_api.ACTIVITY_PROMPT,
                    'response': json.dumps(localized)}}]},
            }}
        if job_id == 'gladia-2':
            response['result']['transcription'].update({
                'full_transcript': 'Original English conversation',
                'languages': ['en'], 'utterances': [
                    {'start': 0, 'end': 100, 'language': 'en',
                        'speaker': 0, 'channel': 0, 'text': 'English passage'}]})
        elif job_id == 'gladia-3':
            response['result']['transcription'].update({
                'languages': ['ca', 'es'], 'utterances': [
                    {'start': 0, 'end': 120, 'language': 'es',
                        'speaker': 0, 'channel': 0, 'text': 'Spanish passage'},
                    {'start': 120, 'end': 125, 'language': 'ca',
                        'speaker': 1, 'channel': 0, 'text': 'Catalan passage'}]})
        return response

    def request(self, method, url, **kwargs):
        self.assertEqual(kwargs['timeout'], (10, 120))
        self.assertEqual(kwargs['headers']['x-gladia-key'], 'test-key')
        if url.endswith('/upload'):
            self.assertEqual(method, 'POST')
            self.assertEqual(kwargs['files']['audio'][1].read(), b'original audio')
            return Mock(status_code=201,
                json=lambda: {'audio_url': 'https://api.gladia.io/file/test'})
        if method == 'POST':
            self.assertTrue(url.endswith('/v2/pre-recorded'))
            parameters = kwargs['json']
            self.assertEqual(parameters['model'], 'solaria-1')
            self.assertEqual(parameters['language_config'],
                {'languages': ['ca', 'es', 'en'], 'code_switching': True})
            self.assertNotIn('diarization_config', parameters)
            self.assertEqual(parameters['translation_config']['target_languages'],
                ['ca', 'es', 'en'])
            self.assertEqual(parameters['audio_to_llm_config']['prompts'],
                [gladia_api.ACTIVITY_PROMPT + recording_ai.SUMMARY_SCHEMA])
            self.submissions += 1
            job_id = f'gladia-{self.submissions}'
            self.results[job_id] = self.result(job_id)
            if self.lose_response:
                self.lose_response = False
                raise requests.Timeout('The job was accepted but the reply was lost')
            return Mock(status_code=201, json=lambda: {'id': job_id})
        job_id = url.rsplit('/', 1)[1]
        if self.poll_timeout:
            self.poll_timeout = False
            raise requests.Timeout('Temporary polling failure')
        return Mock(status_code=200, json=lambda: self.results[job_id])

    def test(self):
        self.config = activate_modules('yeastar_api')
        create_company(config=self.config)
        company = get_company()
        PBX = Model.get('yeastar.pbx')
        Party = Model.get('party.party')
        Employee = Model.get('company.employee')
        Activity = Model.get('activity.activity')
        Type = Model.get('activity.type')
        Configuration = Model.get('activity.configuration')
        Cron = Model.get('ir.cron')
        Job = Model.get('yeastar.transcription.job')
        Attachment = Model.get('ir.attachment')
        Transcription = Model.get('call.transcription')
        activity_type = Type(name='Phone Call')
        activity_type.save()
        configuration = Configuration(1)
        configuration.default_yeastar_activity_type = activity_type
        configuration.save()
        pbx = PBX(name='Test PBX', company=company,
            base_url='https://pbx.example.com', api_path='openapi/v1.0',
            username='test', password='test', time_format='%Y/%m/%d %H:%M:%S',
            webhook_base_url='https://tryton.example.com',
            webhook_secret='test-secret', webhook_enabled=True,
            webhook_timezone='UTC', webhook_mode='activities')
        pbx.save()
        party = Party(name='Employee')
        party.save()
        employee = Employee(party=party, company=company,
            yeastar_pbx=pbx, yeastar_extension=213)
        employee.save()
        start = (datetime.now() - timedelta(days=1)).replace(microsecond=0)
        client = Client(app, Response)
        for note, offset, direction in [('first', 0, 'Inbound'),
                ('return', 100, 'Inbound'), ('internal', 200, 'Internal')]:
            message = {'call_id': 'call-1' if direction == 'Inbound' else 'internal',
                'uid': 'cdr-call-1', 'call_note_id': note,
                'time_start': (start + timedelta(seconds=offset)).strftime(
                    '%Y-%m-%d %H:%M:%S'),
                'call_from': '555123456', 'call_to': '213',
                'type': direction, 'status': 'ANSWERED', 'talk_duration': 80,
                'call_duration': 85, 'recording': f'{note}.wav'}
            body = json.dumps({'type': 30012, 'msg': json.dumps(message)}).encode()
            signature = base64.b64encode(hmac.digest(
                b'test-secret', body, 'sha256')).decode()
            response = client.post(urlsplit(pbx.webhook_url).path, data=body,
                headers={'X-Signature': signature})
            self.assertEqual(response.status_code, 204)
        with Transaction().start(self.config.database_name, 0):
            table = Pool().get('yeastar.webhook.event').__table__()
            Transaction().connection.cursor().execute(*table.update(
                    [table.create_date], [start + timedelta(minutes=5)]))
        events, = Cron.find([('method', '=', 'yeastar.webhook.event|process_pending')])
        events.click('run_once')
        activity, = Activity.find()
        self.assertEqual(activity.duration, timedelta(seconds=160))
        # Upgrade compatibility: the previous webhook implementation created
        # the same placeholder without tracking its generated origin.
        with Transaction().start(self.config.database_name, 0):
            ActivityModel = Pool().get('activity.activity')
            ActivityModel.write([ActivityModel(activity.id)], {
                    'call_generated_subject': None})
        self.process, = Cron.find([
                ('method', '=', 'yeastar.transcription.job|process_pending')])
        self.submissions = 0
        self.results = {}
        self.lose_response = False
        self.poll_timeout = False
        PBXModel = Pool(self.config.database_name).get('yeastar.pbx')
        with patch.object(gladia_api, 'API_KEY', 'test-key'), \
                patch.object(gladia_api, 'URL', 'https://api.gladia.io/v2'), \
                patch.object(gladia_api.requests, 'request', side_effect=self.request), \
                patch.object(PBXModel, 'get_token', return_value='token') as token, \
                patch.object(PBXModel, 'download_record', side_effect=
                    lambda file, token: {'file_name': file,
                        'download_resource_url': '/recording.wav'}) as download, \
                patch.object(PBXModel, '_requests', return_value=b'original audio'):
            for _ in range(9):
                self.tick()
            jobs = Job.find()
            self.assertEqual(len(jobs), 2)
            self.assertEqual({j.state for j in jobs}, {'done'})
            self.assertEqual(self.submissions, 2)
            self.assertEqual({c.kwargs['file'] for c in download.call_args_list},
                {'first.wav', 'return.wav'})
            self.assertTrue(all(c.kwargs == {'commit': False}
                for c in token.call_args_list))
            self.assertEqual(len(Attachment.find()), 2)
            activity.reload()
            self.assertEqual(activity.duration, timedelta(seconds=160))
            self.assertEqual(activity.call_primary_language, 'en')
            self.assertEqual(json.loads(activity.call_language_percentages),
                {'ca': 33.33, 'es': 11.11, 'en': 55.56})
            self.assertIn('en title gladia-1', activity.subject)
            self.assertIn('en title gladia-2', activity.subject)
            self.assertEqual(activity.description,
                '- en summary gladia-1\n- en next step\n\n'
                '- en summary gladia-2\n- en next step')
            for job in jobs:
                primary = 'ca' if job.recording == 'first.wav' else 'en'
                self.assertEqual(job.primary_language, primary)
                self.assertEqual(len(job.transcriptions), 3)
                original, = [t for t in job.transcriptions if t.kind == 'original']
                if primary == 'ca':
                    self.assertEqual(original.transcription, 'Original mixed conversation')
                    self.assertEqual(json.loads(original.utterances)[1]['speaker'], 1)
                    self.assertEqual(json.loads(job.language_percentages),
                        {'ca': 75, 'es': 25, 'en': 0})
                else:
                    self.assertEqual(original.transcription, 'Original English conversation')
                self.assertEqual({t.language_code for t in job.transcriptions
                        if t.kind == 'translation'}, {'ca', 'es', 'en'} - {primary})
            # Repeated cron executions and queue clicks never resubmit audio.
            activity.click('get_call_transcription')
            self.tick()
            self.assertEqual(self.submissions, 2)
            self.assertEqual(len(Transcription.find()), 6)
            # Recover a saved result rejected by the previous parser without
            # uploading, submitting or polling the recording again.
            first = jobs[0]
            saved_run_result = first.runs[0].raw_result
            saved_run_id = first.runs[0].remote_id
            with Transaction().start(self.config.database_name, 0):
                JobModel = Pool().get('yeastar.transcription.job')
                JobModel.write([JobModel(first.id)], {
                        'raw_result': saved_run_result,
                        'gladia_id': saved_run_id,
                        'state': 'error', 'error_stage': 'received',
                        'error_message':
                            'Gladia titles and summaries are missing.'})
            first.reload()
            saved_result = first.raw_result
            saved_id = first.gladia_id
            with patch.object(gladia_api.requests, 'request') as request:
                first.click('retry')
                self.tick()
                request.assert_not_called()
            first.reload()
            self.assertEqual(first.state, 'done')
            self.assertFalse(first.error_message)
            self.assertEqual(first.raw_result, saved_result)
            self.assertEqual(first.gladia_id, saved_id)
            self.assertEqual(len(Transcription.find()), 6)
            self.assertEqual(self.submissions, 2)
            saved_response = json.loads(saved_result)
            output = saved_response['result']['audio_to_llm']['results'][0][
                'results']
            localized = json.loads(output['response'])
            localized['ca']['summary'] += '\nLiteral punctuation: }, }, "es": {'
            valid_text = json.dumps(localized, ensure_ascii=False, indent=2)
            malformed_text = valid_text.replace(
                '\n  },\n  "es": {', '\n  },\n  },\n  "es": {', 1)
            self.assertNotEqual(valid_text, malformed_text)
            # Recover the reported redundant closure for both prompt versions.
            for prompt in (gladia_api.ACTIVITY_PROMPT,
                    gladia_api.LEGACY_ACTIVITY_PROMPT):
                output.update(prompt=prompt, response=malformed_text)
                raw_result = json.dumps(saved_response)
                with Transaction().start(self.config.database_name, 0):
                    JobModel.write([JobModel(first.id)], {
                            'raw_result': raw_result, 'state': 'error',
                            'error_stage': 'received'})
                first.reload()
                with patch.object(gladia_api.requests, 'request') as request:
                    first.click('retry')
                    self.tick()
                    request.assert_not_called()
                first.reload()
                self.assertEqual(first.state, 'done')
                self.assertEqual(first.raw_result, raw_result)
                self.assertEqual(json.loads(first.localized_texts), localized)
                self.assertEqual(first.gladia_id, saved_id)
                self.assertEqual(len(Transcription.find()), 6)
                self.assertEqual({t.language_code: t.summary
                    for t in first.transcriptions},
                    {lang: content['summary']
                        for lang, content in localized.items()})
            # Incomplete, duplicate-language and invalid-schema results must
            # preserve all previously published texts, without network calls.
            for invalid_text in (malformed_text[:-1],
                    malformed_text.replace('"es": {', '"ca": {'),
                    malformed_text.replace('"title":', '"missing":', 1),
                    malformed_text + ' extra data'):
                output['response'] = invalid_text
                with Transaction().start(self.config.database_name, 0):
                    JobModel.write([JobModel(first.id)], {
                            'raw_result': json.dumps(saved_response),
                            'state': 'error', 'error_stage': 'received'})
                first.reload()
                with patch.object(gladia_api.requests, 'request') as request:
                    first.click('retry')
                    self.tick()
                    request.assert_not_called()
                first.reload()
                self.assertEqual(first.state, 'error')
                self.assertEqual(first.error_stage, 'received')
                self.assertEqual(json.loads(first.localized_texts), localized)
                self.assertEqual(len(Transcription.find()), 6)
            self.assertEqual(self.submissions, 2)
            # Preserve user edits while explicitly reprocessing one recording.
            activity.subject = 'Edited subject'
            activity.description = 'Edited description'
            activity.save()
            first = jobs[0]
            wizard = Wizard('yeastar.recording.experiment', [first])
            wizard.execute('generate')
            self.lose_response = True
            self.tick()  # Upload and submit the saved audio; HTTP reply lost.
            first.reload()
            run = first.runs[0]
            self.assertEqual(run.state, 'uncertain')
            self.assertEqual(self.submissions, 3)
            self.tick()
            self.assertEqual(self.submissions, 3)
            run.remote_id = 'gladia-3'
            run.save()
            run.click('resume')
            self.poll_timeout = True
            self.tick()
            run.reload()
            self.assertEqual(run.state, 'error')
            run.click('retry')
            for _ in range(4):
                self.tick()
            first.reload()
            run.reload()
            self.assertEqual(run.state, 'done')
            self.assertEqual(self.submissions, 3)
            self.assertEqual(len(Attachment.find()), 2)
            self.assertEqual(len(Transcription.find()), 6)
            self.assertEqual(len(first.runs), 2)
            # Alternative results never replace the selected version on arrival.
            self.assertNotEqual(first.selected_summary.run, run)
            self.assertEqual(first.primary_language, 'ca')
            summary, = run.summaries
            self.assertFalse(summary.published)
            summary.click('publish')
            first.reload()
            self.assertEqual(first.selected_summary, summary)
            activity.reload()
            self.assertEqual(activity.subject, 'Edited subject')
            self.assertEqual(activity.description, 'Edited description')
            self.assertEqual(activity.duration, timedelta(seconds=160))
            self.assertEqual(first.primary_language, 'es')
            self.assertEqual(activity.call_primary_language, 'es')
            # Missing translations do not publish partially updated results.
            wizard = Wizard('yeastar.recording.experiment', [first])
            wizard.execute('generate')
            self.tick()
            first.reload()
            run = first.runs[0]
            self.assertEqual((run.state, run.remote_id),
                ('submitted', 'gladia-4'))
            self.results['gladia-4']['result']['translation']['success'] = False
            self.tick()
            run.reload()
            self.assertEqual(run.state, 'received')
            self.assertFalse(json.loads(run.raw_result)['result'][
                    'translation']['success'])
            self.tick()
            run.reload()
            self.assertEqual(run.state, 'error')
            self.assertIn('translations', run.error)
            self.assertEqual(len(Transcription.find()), 6)
            run.click('retry')
            self.tick()
            self.assertEqual(self.submissions, 4)
            # Compare OpenRouter transcriptions and summaries on saved audio.
            Catalog = Model.get('ai.model.openrouter')
            with Transaction().start(self.config.database_name, 0):
                models = Pool().get('ai.model.openrouter').create([
                    {'name': 'Audio test', 'openrouter_id': 'test/audio',
                        'input_modalities': 'text,audio',
                        'output_modalities': 'text'},
                    {'name': 'Summary test', 'openrouter_id': 'test/text',
                        'input_modalities': 'text', 'output_modalities': 'text'}])
                model_ids = [m.id for m in models]
            audio_model, text_model = map(Catalog, model_ids)
            calls = []

            def completion(payload, origin):
                calls.append(payload)
                if isinstance(payload['messages'][1]['content'], list):
                    part = payload['messages'][1]['content'][0]
                    self.assertEqual(base64.b64decode(
                        part['input_audio']['data']), b'original audio')
                    content = {'transcript': 'New original transcript',
                        'languages': ['ca', 'es'], 'primary_language': 'ca',
                        'translations': {lang: f'{lang} new translation'
                            for lang in ('ca', 'es', 'en')}}
                else:
                    self.assertEqual(payload['messages'][1]['content'],
                        'New original transcript')
                    content = {lang: {'title': f'{lang} new title',
                        'summary': f'- {lang} summary {len(calls)}'}
                        for lang in ('ca', 'es', 'en')}
                return {'choices': [{'message': {'content': json.dumps(content)}}]}

            with patch.object(recording_ai, 'complete', side_effect=completion):
                wizard = Wizard('yeastar.recording.experiment', [first])
                wizard.form.transcription_provider = 'openrouter'
                wizard.form.transcription_model = audio_model
                wizard.form.summary_provider = 'openrouter'
                wizard.form.summary_model = text_model
                wizard.execute('generate')
                for _ in range(5):
                    self.tick()
                first.reload()
                openrouter_run = first.runs[0]
                self.assertEqual(openrouter_run.state, 'done')
                self.assertEqual(openrouter_run.transcript, 'New original transcript')
                self.assertEqual(openrouter_run.model_name, 'test/audio')
                self.assertEqual(first.selected_summary, summary)
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.submissions, 4)
                self.assertEqual(download.call_count, 2)
                original_summary, = openrouter_run.summaries
                original_summary.click('publish')
                first.reload()
                self.assertEqual(first.selected_summary, original_summary)
                self.assertEqual(json.loads(first.language_percentages), {})
                original, = [t for t in first.transcriptions if t.kind == 'original']
                self.assertEqual(original.transcription, 'New original transcript')
                # A summary-only experiment must never resend the recording.
                wizard = Wizard('yeastar.recording.experiment', [openrouter_run])
                wizard.form.summary_model = text_model
                wizard.form.summary_prompt = 'Return a concise bullet summary.'
                wizard.execute('generate')
                for _ in range(3):
                    self.tick()
                self.assertEqual(len(calls), 3)
                openrouter_run.reload()
                self.assertEqual(len(openrouter_run.summaries), 2)
                first.reload()
                self.assertEqual(first.selected_summary, original_summary)
                replacement = openrouter_run.summaries[0]
                self.assertEqual(replacement.prompt,
                    'Return a concise bullet summary.')
                replacement.click('publish')
                first.reload()
                self.assertEqual(first.selected_summary, replacement)
                original_summary.reload()
                self.assertFalse(original_summary.published)
                self.assertIn('summary 2', original_summary.result)
                self.assertIn('summary 3', replacement.result)
            # Copying an activity must never copy jobs or their generated text.
            copied, = activity.duplicate()
            self.assertFalse(copied.transcription_jobs)
            self.assertFalse(copied.transcriptions)
            self.assertIsNone(copied.call_id)
            # PBX defaults drive automatic runs too, and the first result is
            # published without an explicit validation click.
            pbx.transcription_provider = 'openrouter'
            pbx.transcription_model = audio_model
            pbx.summary_provider = 'openrouter'
            pbx.summary_model = text_model
            pbx.transcription_prompt = 'Preserve speaker turns and languages.'
            pbx.save()
            attachment_id = first.attachment.id
            with Transaction().start(self.config.database_name, 0):
                auto_job, = Pool().get('yeastar.transcription.job').create([{
                    'pbx': pbx.id, 'activity': copied.id,
                    'recording': 'automatic.wav', 'attachment': attachment_id,
                    'state': 'ready'}])
                auto_job_id = auto_job.id
            with patch.object(recording_ai, 'complete', side_effect=completion):
                for _ in range(7):
                    self.tick()
            auto_job = Job(auto_job_id)
            self.assertEqual(auto_job.selected_summary.state, 'done')
            self.assertTrue(auto_job.selected_summary.published)
            auto_run, = auto_job.runs
            self.assertEqual(auto_run.transcription_prompt,
                'Preserve speaker turns and languages.')
            pbx.transcription_prompt = 'A different prompt for future calls.'
            pbx.save()
            auto_run.reload()
            self.assertEqual(auto_run.transcription_prompt,
                'Preserve speaker turns and languages.')
            # The recording shortcut regenerates only the selected transcript's
            # summary; malformed responses retain the previous publication.
            previous_summary = auto_job.selected_summary
            wizard = Wizard('yeastar.recording.summary.experiment', [auto_job])
            wizard.execute('generate')
            with patch.object(recording_ai, 'complete', return_value={
                    'choices': [{'message': {'content': '{"ca": {}}'}}]}):
                for _ in range(3):
                    self.tick()
            auto_job.reload()
            self.assertEqual(auto_job.selected_summary, previous_summary)
            auto_run.reload()
            failed_summary = auto_run.summaries[0]
            self.assertEqual(failed_summary.state, 'error')
            with patch.object(recording_ai, 'complete') as request:
                failed_summary.click('retry')
                self.tick()
                request.assert_not_called()
            # A malformed Gladia summary must not hide a valid transcription.
            wizard = Wizard('yeastar.recording.experiment', [auto_job])
            wizard.form.transcription_provider = 'gladia'
            wizard.form.summary_provider = 'gladia'
            wizard.execute('generate')
            self.tick()
            self.results['gladia-5']['result']['audio_to_llm']['results'][0][
                'results']['response'] = '{"ca": {}}'
            for _ in range(4):
                self.tick()
            auto_job.reload()
            run = auto_job.runs[0]
            self.assertEqual(run.state, 'done')
            self.assertEqual(run.transcript, 'Original mixed conversation')
            self.assertEqual(run.summaries[0].state, 'error')
            self.assertEqual(auto_job.selected_summary, previous_summary)
