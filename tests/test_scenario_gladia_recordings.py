import base64
import hmac
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from proteus import Model
from werkzeug.test import Client

from trytond.modules.company.tests.tools import create_company, get_company
from trytond.modules.yeastar_api import gladia_api
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
                'summary': f'{lang} summary {job_id}'}
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
                'success': True, 'results': {
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
                [gladia_api.ACTIVITY_PROMPT])
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
            for _ in range(6):
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
                'en summary gladia-1\n\nen summary gladia-2')
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
            # Preserve user edits while explicitly reprocessing one recording.
            activity.subject = 'Edited subject'
            activity.description = 'Edited description'
            activity.save()
            first = jobs[0]
            first.click('reprocess')
            self.lose_response = True
            self.tick()  # Upload the saved attachment, without downloading again.
            self.tick()  # Accepted remotely; HTTP reply lost.
            first.reload()
            self.assertEqual(first.state, 'uncertain')
            self.assertEqual(self.submissions, 3)
            self.tick()
            self.assertEqual(self.submissions, 3)
            first.gladia_id = 'gladia-3'
            first.save()
            first.click('resume')
            self.poll_timeout = True
            self.tick()
            first.reload()
            self.assertEqual(first.state, 'submitted')
            self.assertEqual(first.attempts, 1)
            self.assertIsNotNone(first.next_attempt)
            self.tick()
            self.tick()
            first.reload()
            self.assertEqual(first.state, 'done')
            self.assertEqual(self.submissions, 3)
            self.assertEqual(len(Attachment.find()), 2)
            self.assertEqual(len(Transcription.find()), 6)
            activity.reload()
            self.assertEqual(activity.subject, 'Edited subject')
            self.assertEqual(activity.description, 'Edited description')
            self.assertEqual(activity.duration, timedelta(seconds=160))
            self.assertEqual(first.primary_language, 'es')
            self.assertEqual(activity.call_primary_language, 'es')
            # Missing translations do not publish partially updated results.
            first.click('reprocess')
            self.tick()
            self.tick()
            first.reload()
            self.assertEqual((first.state, first.gladia_id),
                ('submitted', 'gladia-4'))
            self.results['gladia-4']['result']['translation']['success'] = False
            self.tick()
            first.reload()
            self.assertEqual(first.state, 'received')
            self.assertFalse(json.loads(first.raw_result)['result'][
                    'translation']['success'])
            self.tick()
            first.reload()
            self.assertEqual(first.state, 'error')
            self.assertEqual(first.error_stage, 'received')
            self.assertIn('translations', first.error_message)
            self.assertEqual(len(Transcription.find()), 6)
            first.click('retry')
            self.tick()
            self.assertEqual(self.submissions, 4)
            # Copying an activity must never copy jobs or their generated text.
            copied, = activity.duplicate()
            self.assertFalse(copied.transcription_jobs)
            self.assertFalse(copied.transcriptions)
            self.assertIsNone(copied.call_id)
