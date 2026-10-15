# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.model import ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval


class CallTranscription(ModelSQL, ModelView):
    'Call Transcription'
    __name__ = 'call.transcription'

    name = fields.Char('Name', readonly=True)
    activity = fields.Many2One('activity.activity', 'Activity')
    transcription = fields.Text('Transcription')
    job = fields.Many2One('yeastar.transcription.job', 'Recording Job',
        ondelete='SET NULL', readonly=True)
    language_code = fields.Char('Language', readonly=True)
    kind = fields.Selection([
            (None, ''), ('original', 'Original'),
            ('translation', 'Translation'),
            ], 'Kind', readonly=True)
    utterances = fields.Text('Timed Speaker Segments (JSON)', readonly=True)
    subject = fields.Char('Subject', readonly=True)
    summary = fields.Text('Summary', readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('job_language_kind_unique',
                Unique(table, table.job, table.language_code, table.kind),
                'yeastar_api.msg_transcription_unique'),
            ]


class CallTranscriptionLLMProcess(ModelSQL, ModelView):
    'Call Transcription LLM Process'
    __name__ = 'call.transcription.llm.process'

    activity = fields.Many2One('activity.activity', 'Activity')
    prompt = fields.Char('Prompt')
    response = fields.Text('Response', readonly=True)


STATE = {'invisible': ~Bool(Eval('call_id'))}


class Activity(metaclass=PoolMeta):
    __name__ = 'activity.activity'

    def identify_call_party(self, number, pbx):
        from .phone import matching_phone_parties

        if not self.party:
            parties = matching_phone_parties(number, self.company)
            if len(parties) == 1:
                self.__class__.write([self], {'party': parties[0].id})

    call_id = fields.Char('Call ID', readonly=True)
    # Keep historical settings/results available on upgraded databases.
    language = fields.Many2One('ir.lang', 'Language Call', states=STATE)
    speakers = fields.Integer('Number of Speakers', states=STATE)
    translation_languages = fields.MultiSelection('get_translation_languages',
        'Translation Languages', states=STATE)
    transcriptions = fields.One2Many('call.transcription', 'activity',
        'Call Transcriptions', states=STATE)
    summarization = fields.Selection([
            (None, ''), ('general', 'General'),
            ('bullet_points', 'Bullet Points'), ('concise', 'Concise'),
            ], 'Summarization', states=STATE)
    summarization_result = fields.Text('Summarization Result', readonly=True)
    llm_process = fields.One2Many('call.transcription.llm.process', 'activity',
        'LLM Processing', states=STATE)
    transcription_jobs = fields.One2Many(
        'yeastar.transcription.job', 'activity',
        'Recordings', readonly=True, states=STATE)
    call_primary_language = fields.Char('Primary Language', readonly=True)
    call_language_percentages = fields.Text(
        'Spoken Language Percentages (JSON)',
        readonly=True)
    call_generated_subject = fields.Char('Last Generated Subject',
        readonly=True)
    call_generated_description = fields.Text('Last Generated Description',
        readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({'get_call_transcription': {}})

    @classmethod
    def get_translation_languages(cls):
        Language = Pool().get('ir.lang')
        return [(language.code, language.name)
            for language in Language.search([])]

    @classmethod
    @ModelView.button
    def get_call_transcription(cls, activities):
        """Queue recordings; downloading and transcription run in the cron."""
        Job = Pool().get('yeastar.transcription.job')
        Job.enqueue(activities, discover=True)

    @classmethod
    def copy(cls, activities, default=None):
        default = default.copy() if default is not None else {}
        for name in ('code', 'call_id', 'language', 'call_primary_language',
                'call_language_percentages', 'call_generated_subject',
                'call_generated_description', 'transcription_jobs',
                'transcriptions', 'summarization_result'):
            default.setdefault(name, None)
        return super().copy(activities, default=default)
