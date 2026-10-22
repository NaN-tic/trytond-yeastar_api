import json
import math
from datetime import datetime, timedelta

import requests

from trytond import backend
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import Index, ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction, TransactionError

from . import gladia_api


def decode_activity_texts(text):
    """Recover redundant object closures only between complete language values."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        # Decode strings and nested objects normally: punctuation inside a
        # title or summary must never be modified by a textual replacement.
        decoder = json.JSONDecoder()
        remaining = text.strip()
        if not remaining.startswith('{'):
            raise error
        remaining = remaining[1:].lstrip()
        localized = {}
        for index in range(len(gladia_api.LANGUAGES)):
            language, end = decoder.raw_decode(remaining)
            if (not isinstance(language, str)
                    or language not in gladia_api.LANGUAGES
                    or language in localized):
                raise error
            remaining = remaining[end:].lstrip()
            if not remaining.startswith(':'):
                raise error
            content, end = decoder.raw_decode(remaining[1:].lstrip())
            if not isinstance(content, dict):
                raise error
            localized[language] = content
            remaining = remaining[1:].lstrip()[end:].lstrip()
            if index < len(gladia_api.LANGUAGES) - 1:
                if not remaining.startswith(','):
                    raise error
                remaining = remaining[1:].lstrip()
                if remaining.startswith('}'):
                    remaining = remaining[1:].lstrip()
                    if not remaining.startswith(','):
                        raise error
                    remaining = remaining[1:].lstrip()
        if remaining != '}':
            raise error
        return localized


class TranscriptionJob(ModelSQL, ModelView):
    'Yeastar Recording Transcription'
    __name__ = 'yeastar.transcription.job'
    _rec_name = 'recording'

    pbx = fields.Many2One('yeastar.pbx', 'PBX', required=True,
        ondelete='RESTRICT', readonly=True)
    activity = fields.Many2One('activity.activity', 'Activity',
        ondelete='SET NULL', readonly=True)
    recording = fields.Char('Recording File', required=True, readonly=True)
    start = fields.DateTime('Recording Start', readonly=True)
    attachment = fields.Many2One('ir.attachment', 'Audio', readonly=True,
        ondelete='SET NULL')
    audio_url = fields.Char('Gladia Upload URL', readonly=True)
    gladia_id = fields.Char('Gladia Job ID', states={
            'readonly': Eval('state') != 'uncertain',
            })
    state = fields.Selection([
            ('pending', 'Waiting for Recording'), ('ready', 'Ready to Upload'),
            ('uploaded', 'Ready to Submit'), ('submitting', 'Submitting'),
            ('submitted', 'Transcribing'), ('received', 'Result Received'),
            ('done', 'Completed'), ('empty', 'No Speech'),
            ('error', 'Error'), ('failed', 'Gladia Job Failed'),
            ('uncertain', 'Submission Status Unknown'),
            ('processing', 'Processing Versions'),
            ], 'State', required=True, readonly=True)
    attempts = fields.Integer('Retry Count', readonly=True)
    next_attempt = fields.DateTime('Next Attempt', readonly=True)
    error_stage = fields.Char('Failed Stage', readonly=True)
    error_message = fields.Text('Processing Error', readonly=True)
    raw_result = fields.Text('Gladia Result (JSON)', readonly=True)
    previous_results = fields.Text('Previous Jobs (JSON)', readonly=True)
    primary_language = fields.Char('Primary Language', readonly=True)
    language_durations = fields.Text('Speech Seconds by Language (JSON)',
        readonly=True)
    language_percentages = fields.Text('Language Percentages (JSON)',
        readonly=True)
    localized_texts = fields.Text('Subjects and Summaries (JSON)',
        readonly=True)
    transcriptions = fields.One2Many('call.transcription', 'job',
        'Transcriptions', readonly=True)
    runs = fields.One2Many('yeastar.recording.run', 'job',
        'Transcription Versions', readonly=True)
    selected_summary = fields.Many2One('yeastar.recording.summary',
        'Published Summary', readonly=True, ondelete='RESTRICT')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('recording_unique',
                Unique(table, table.pbx, table.activity, table.recording),
                'yeastar_api.msg_recording_job_unique'),
            ]
        cls._sql_indexes.add(Index(table, (table.state, Index.Equality()),
                (table.next_attempt, Index.Range())))
        cls._order = [('start', 'ASC'), ('id', 'ASC')]
        cls._buttons.update({
                'retry': {'invisible': Eval('state') != 'error'},
                'reprocess': {'invisible': ~Eval('state').in_(
                        ['done', 'empty', 'failed', 'error', 'processing'])},
                'regenerate_summary': {
                    'invisible': ~Bool(Eval('localized_texts'))},
                'resume': {'invisible': Eval('state') != 'uncertain'},
                })

    @staticmethod
    def default_state():
        return 'pending'

    @staticmethod
    def default_attempts():
        return 0

    @classmethod
    def enqueue(cls, activities, discover=False):
        pool = Pool()
        Leg = pool.get('yeastar.webhook.leg')
        PBX = pool.get('yeastar.pbx')
        Activity = pool.get('activity.activity')
        Configuration = pool.get('activity.configuration')
        for activity in activities:
            candidates = {}
            legs = Leg.search([('call.activity', '=', activity.id)])
            # Recognize unchanged placeholders created before this feature was
            # installed, while leaving authored subjects alone.
            if (not activity.call_generated_subject and activity.subject in {
                    f'{leg.direction} call: {leg.external_number}'
                    for leg in legs}):
                Activity.write([activity], {
                        'call_generated_subject': activity.subject})
            for leg in legs:
                if leg.recording:
                    candidates.setdefault(
                        (leg.pbx.id, leg.recording), leg.start)
            if not legs and discover and activity.call_id:
                employee = activity.employee
                pbx = employee.yeastar_pbx
                if pbx and employee.yeastar_extension is not None:
                    configuration = Configuration(1)
                    with Transaction().set_context(yeastar_wo_employee=True):
                        records = pbx.get_records_list(
                            page=configuration.yeastar_record_list_page or '1',
                            page_size=(
                                configuration.yeastar_record_list_page_size
                                or '100'), sort_by='id', order_by='desc')
                    for record in records.get('data') or []:
                        if (record.get('call_type', '').lower()
                                not in {'inbound', 'outbound'}):
                            continue
                        if str(employee.yeastar_extension) not in {
                                str(record.get('call_from_number')),
                                str(record.get('call_to_number'))}:
                            continue
                        filename = record.get('file', '')
                        if activity.call_id in filename:
                            candidates[(pbx.id, filename)] = activity.dtstart
            for (pbx_id, filename), start in candidates.items():
                PBX.lock([PBX(pbx_id)])
                if not cls.search([
                            ('pbx', '=', pbx_id),
                            ('activity', '=', activity.id),
                            ('recording', '=', filename),
                            ], limit=1):
                    cls.create([{
                                'pbx': pbx_id, 'activity': activity.id,
                                'recording': filename, 'start': start,
                                }])

    @classmethod
    def _locked(cls, job_id, operation, *args):
        return cls._run_locked(cls, job_id, operation, *args)

    @classmethod
    def _run_locked(cls, Model, job_id, operation, *args):
        """Retry database work without repeating remote submissions."""
        extras = {}
        failures = 0
        while True:
            try:
                with Transaction().new_transaction(**extras):
                    record = Model(job_id)
                    Model.lock([record])
                    job = record
                    if Model.__name__ == 'yeastar.recording.run':
                        job = record.job
                    elif Model.__name__ == 'yeastar.recording.summary':
                        job = record.run.job
                    with Transaction().set_context(
                            company=job.pbx.company.id, _check_access=False):
                        return getattr(record, operation)(*args)
            except TransactionError as exception:
                exception.fix(extras)
            except backend.DatabaseOperationalError:
                failures += 1
                if failures >= 5:
                    raise

    @classmethod
    def process_pending(cls):
        pool = Pool()
        pool.get('yeastar.recording.run').process_pending()
        pool.get('yeastar.recording.summary').process_pending()
        Call = pool.get('yeastar.webhook.call')
        company = Transaction().context.get('company')
        domain = [('activity', '!=', None),
            ('state', 'in', ['pending', 'ready', 'uploaded', 'submitting',
                    'submitted', 'received']),
            ['OR', ('next_attempt', '=', None),
                ('next_attempt', '<=', datetime.now())]]
        if company:
            domain.append(('pbx.company', '=', company))
        # Work only on committed jobs. New jobs are enqueued after processing.
        for job in cls.search(domain, order=[('next_attempt', 'ASC'),
                    ('id', 'ASC')], limit=100):
            try:
                submission = cls._locked(job.id, '_advance')
            except (UserError, ValueError, TypeError, KeyError,
                    requests.RequestException,
                    gladia_api.GladiaError) as error:
                cls._locked(job.id, '_failed', error)
                continue
            if submission:
                # _advance has committed the submitting marker. A crash or
                # ambiguous response can never cause an automatic second POST.
                try:
                    response = gladia_api.create_transcribe(
                        submission['audio_url'], metadata={
                            'tryton_job': job.id,
                            'database': Transaction().database.name,
                            })
                    job_id = response.get('id')
                    if not isinstance(job_id, str) or not job_id:
                        raise ValueError('Gladia did not return a job ID.')
                except (ValueError, requests.RequestException,
                        gladia_api.GladiaError) as error:
                    cls._locked(job.id, '_failed', error)
                    continue
                cls._locked(job.id, '_accept', job_id)
        domain = [('activity', '!=', None),
            ('pbx.webhook_mode', '=', 'activities'),
            ('pbx.automatic_transcription', '=', True)]
        if company:
            domain.append(('pbx.company', '=', company))
        # Enqueue in its own transaction too. A parent PBX lock would both
        # replay the cron on lock escalation and block child token updates.
        extras = {}
        while True:
            try:
                with Transaction().new_transaction(**extras):
                    activities = list({c.activity
                        for c in Call.search(domain)})
                    cls.enqueue(activities)
                break
            except TransactionError as exception:
                exception.fix(extras)

    def _advance(self):
        cls = self.__class__
        if not self.activity or (self.next_attempt
                and self.next_attempt > datetime.now()):
            return
        if self.state == 'submitting':
            if self.write_date < datetime.now() - timedelta(minutes=10):
                cls.write([self], {'state': 'uncertain', 'error_message':
                        'Submission interrupted. Recover the Gladia job ID.'})
            return
        if self.state == 'pending':
            Attachment = Pool().get('ir.attachment')
            with Transaction().set_context(yeastar_wo_employee=True):
                token = self.pbx.get_token(commit=False)
            if not token:
                raise ValueError('The PBX did not return an access token.')
            info = self.pbx.download_record(file=self.recording, token=token)
            if not info or not info.get('download_resource_url'):
                raise UserError(gettext('yeastar_api.msg_recording_not_ready'))
            audio = self.pbx._requests('GET',
                args=info['download_resource_url'], token=token)
            if not isinstance(audio, bytes) or not audio:
                raise UserError(gettext('yeastar_api.msg_recording_not_ready'))
            filename = info.get('file_name') or self.recording
            attachments = Attachment.search([
                    ('resource', '=', str(self.activity)),
                    ('name', '=', filename),
                    ], limit=1)
            if attachments:
                attachment, = attachments
            else:
                attachment, = Attachment.create([{
                            'name': filename, 'type': 'data', 'data': audio,
                            'resource': str(self.activity),
                            }])
            cls.write([self], {'attachment': attachment.id, 'state': 'ready',
                    'attempts': 0, 'next_attempt': None,
                    'error_message': None})
        elif self.state == 'ready':
            if not self.attachment or not self.attachment.data:
                raise ValueError('The saved recording is missing.')
            self.pbx.check_settings()
            Run = Pool().get('yeastar.recording.run')
            if not self.runs:
                Run.create([{'job': self.id, **self.pbx.settings()}])
            cls.write([self], {'state': 'processing', 'attempts': 0,
                    'next_attempt': None, 'error_message': None})
        elif self.state == 'uploaded':
            if not gladia_api.API_KEY:
                raise ValueError('Configure the Gladia API key.')
            cls.write([self], {'state': 'submitting', 'error_message': None})
            return {'audio_url': self.audio_url}
        elif self.state == 'submitted':
            response = gladia_api.get_transcribe(self.gladia_id)
            status = response.get('status')
            if status == 'done':
                cls.write([self], {'raw_result': json.dumps(response),
                        'state': 'received', 'attempts': 0,
                        'next_attempt': None, 'error_message': None})
            elif status == 'error':
                cls.write([self], {'raw_result': json.dumps(response),
                        'state': 'failed', 'error_message':
                            f'Gladia job failed: {response.get("error_code")}',
                        })
            elif status in {'queued', 'processing'}:
                cls.write([self], {'next_attempt':
                        datetime.now() + timedelta(minutes=1), 'attempts': 0})
            else:
                raise ValueError('Unknown Gladia job status.')
        elif self.state == 'received':
            self._apply_result()

    def _accept(self, job_id):
        if self.state not in {'submitting', 'uncertain'}:
            return
        self.__class__.write([self], {
                'gladia_id': job_id, 'state': 'submitted',
                'attempts': 0, 'next_attempt': None, 'error_message': None})

    def _failed(self, error):
        stage = self.state
        attempts = (self.attempts or 0) + 1
        message = str(error)
        if isinstance(error, requests.RequestException):
            # PBX transport exception URLs may contain an access token.
            message = f'{type(error).__name__}: remote request failed.'
        if stage == 'submitting':
            if (isinstance(error, gladia_api.GladiaError)
                    and error.status in {400, 401, 403, 404, 422, 429}):
                stage = 'uploaded'
            else:
                self.__class__.write([self], {'state': 'uncertain',
                        'error_message': message})
                return
        retryable = isinstance(error, (requests.RequestException, UserError))
        if isinstance(error, gladia_api.GladiaError):
            retryable = error.status == 429 or error.status >= 500
        self.__class__.write([self], {
                'state': stage if retryable and attempts < 5 else 'error',
                'error_stage': stage, 'error_message': message,
                'attempts': attempts,
                'next_attempt': datetime.now() + timedelta(
                    seconds=min(3600, 60 * 2 ** min(attempts, 6))),
                })

    def _apply_result(self):
        pool = Pool()
        Transcription = pool.get('call.transcription')
        response = json.loads(self.raw_result)
        result = response['result']
        original = result['transcription']
        if not original.get('full_transcript', '').strip():
            Transcription.delete(Transcription.search([('job', '=', self.id)]))
            self.__class__.write([self], {'state': 'empty',
                    'primary_language': None, 'localized_texts': None,
                    'language_durations': None, 'language_percentages': None,
                    'error_message': None})
            self._publish_activity()
            return
        durations = dict.fromkeys(gladia_api.LANGUAGES, 0.0)
        for utterance in original['utterances']:
            language = utterance.get('language')
            if language not in durations:
                raise ValueError('Unexpected language in Gladia result.')
            start, end = utterance['start'], utterance['end']
            if (not isinstance(start, (int, float))
                    or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start <= end):
                raise ValueError('Invalid utterance timestamps.')
            durations[language] += end - start
        if not sum(durations.values()):
            raise ValueError('Missing timed speech for language detection.')
        # Ties follow Gladia's ranked languages, then the configured order.
        ranking = list(dict.fromkeys(
            original.get('languages', []) + list(gladia_api.LANGUAGES)))
        primary = max(durations, key=lambda lang:
            (durations[lang], -ranking.index(lang)))
        translations = result.get('translation') or {}
        if not translations.get('success'):
            raise ValueError('Gladia translations failed or are missing.')
        translated = {}
        for translation in translations.get('results') or []:
            for language in translation.get('languages', []):
                if not translation.get('error'):
                    translated[language] = translation
        llm = result.get('audio_to_llm') or {}
        localized = None
        if llm.get('success'):
            for entry in llm.get('results') or []:
                output = entry.get('results') or {}
                if (entry.get('success') is not False
                        and not entry.get('error')
                        and output.get('prompt') in (
                            gladia_api.ACTIVITY_PROMPT,
                            gladia_api.LEGACY_ACTIVITY_PROMPT)):
                    text = output['response'].strip()
                    if text.startswith('```') and text.endswith('```'):
                        text = '\n'.join(text.splitlines()[1:-1])
                    localized = decode_activity_texts(text)
        if not isinstance(localized, dict):
            raise ValueError('Gladia titles and summaries are missing.')
        values = []
        for language in gladia_api.LANGUAGES:
            content = localized.get(language)
            if not isinstance(content, dict) or any(
                    not isinstance(content.get(key), str)
                    or not content[key].strip()
                    for key in ('title', 'summary')):
                raise ValueError(f'Missing activity texts in {language}.')
            source = (original if language == primary
                else translated.get(language))
            if not source or not source.get('full_transcript', '').strip():
                raise ValueError(f'Missing translation in {language}.')
            values.append({
                    'job': self.id, 'activity': self.activity.id,
                    'name': language, 'language_code': language,
                    'kind': ('original' if language == primary
                        else 'translation'),
                    'transcription': source['full_transcript'],
                    'utterances': json.dumps(source.get('utterances', [])),
                    'subject': content['title'].strip(),
                    'summary': content['summary'].strip(),
                    })
        # Validate every add-on before replacing any published result.
        Transcription.delete(Transcription.search([('job', '=', self.id)]))
        Transcription.create(values)
        percentages = {lang: round(seconds * 100 / sum(durations.values()), 2)
            for lang, seconds in durations.items()}
        self.__class__.write([self], {'primary_language': primary,
                'language_durations': json.dumps(durations),
                'language_percentages': json.dumps(percentages),
                'localized_texts': json.dumps(localized), 'state': 'done',
                'error_message': None, 'next_attempt': None, 'attempts': 0})
        self._publish_activity()

    def _publish_activity(self):
        Activity = Pool().get('activity.activity')
        Activity.lock([self.activity])
        activity = self.activity
        jobs = self.__class__.search([
                ('activity', '=', activity.id),
                ('localized_texts', '!=', None)],
            order=[('start', 'ASC'), ('id', 'ASC')])
        durations = dict.fromkeys(gladia_api.LANGUAGES, 0.0)
        complete_durations = True
        for job in jobs:
            job_durations = json.loads(job.language_durations or '{}')
            if not sum(job_durations.values()):
                complete_durations = False
            for lang, seconds in job_durations.items():
                durations[lang] = durations.get(lang, 0) + seconds
        ranking = list(dict.fromkeys([j.primary_language for j in jobs]
            + list(gladia_api.LANGUAGES) + list(durations)))
        primary = (max(durations,
                key=lambda lang: (durations[lang], -ranking.index(lang)))
            if jobs and complete_durations and sum(durations.values()) else (
                jobs[0].primary_language if jobs else None))
        texts = [json.loads(job.localized_texts).get(primary)
            or json.loads(job.localized_texts)['en'] for job in jobs]
        subject = '; '.join(dict.fromkeys(
                t['title'].strip() for t in texts))[:240]
        description = '\n\n'.join(t['summary'].strip() for t in texts)
        values = {
            'call_primary_language': primary,
            'call_language_percentages': json.dumps({
                lang: round(seconds * 100 / sum(durations.values()), 2)
                for lang, seconds in durations.items()}
                if complete_durations and sum(durations.values()) else {}),
            'summarization_result': description,
            }
        # Generated placeholders/results may be updated; manual edits survive.
        if (not activity.subject
                or activity.subject == activity.call_generated_subject):
            values.update(subject=subject, call_generated_subject=subject)
        if (not activity.description
                or activity.description
                == activity.call_generated_description):
            values.update(description=description,
                call_generated_description=description)
        Activity.write([activity], values)

    @classmethod
    @ModelView.button
    def retry(cls, jobs):
        for job in jobs:
            cls.lock([job])
            if job.state == 'error':
                cls.write([job], {'state': job.error_stage, 'attempts': 0,
                        'next_attempt': None, 'error_message': None})

    @classmethod
    @ModelView.button
    def resume(cls, jobs):
        for job in jobs:
            cls.lock([job])
            if job.state == 'uncertain' and job.gladia_id:
                job._accept(job.gladia_id)

    @classmethod
    @ModelView.button_action('yeastar_api.act_recording_experiment')
    def reprocess(cls, jobs):
        pass

    @classmethod
    @ModelView.button_action('yeastar_api.act_recording_summary_experiment')
    def regenerate_summary(cls, jobs):
        pass

    def archive_legacy_result(self):
        """Preserve the current pre-versioning result before an experiment."""
        pool = Pool()
        self.__class__.lock([self])
        if self.runs or not self.localized_texts:
            return
        originals = [t for t in self.transcriptions if t.kind == 'original']
        if not originals:
            return
        original, = originals
        normalized = {'transcript': original.transcription,
            'languages': list(json.loads(self.language_durations or '{}')),
            'primary_language': self.primary_language,
            'durations': json.loads(self.language_durations or '{}'),
            'translations': {t.language_code: t.transcription
                for t in self.transcriptions},
            'utterances': json.loads(original.utterances or '[]')}
        Run = pool.get('yeastar.recording.run')
        Summary = pool.get('yeastar.recording.summary')
        prompt = gladia_api.ACTIVITY_PROMPT
        if self.raw_result:
            response = json.loads(self.raw_result)
            entries = (response.get('result', {}).get('audio_to_llm') or {}).get(
                'results', [])
            for entry in entries:
                prompt = (entry.get('results') or {}).get('prompt') or prompt
        run, = Run.create([{'job': self.id, 'state': 'done',
            'transcription_provider': 'gladia', 'summary_provider': 'gladia',
            'transcription_prompt': None, 'summary_prompt': prompt,
            'remote_id': self.gladia_id, 'raw_result': self.raw_result,
            'result': json.dumps(normalized),
            'transcript': original.transcription,
            'languages': ', '.join(normalized['languages'])}])
        summary, = Summary.create([{'run': run.id, 'provider': 'gladia',
            'prompt': prompt, 'state': 'done',
            'result': self.localized_texts}])
        self.__class__.write([self], {'selected_summary': summary.id})


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.append((
                'yeastar.transcription.job|process_pending',
                'Process Yeastar Recordings'))
        cls.methods_company_needed.add(
            'yeastar.transcription.job|process_pending')
