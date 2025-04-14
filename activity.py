#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from datetime import timedelta
try:
    from pydub import AudioSegment
except Exception:
    AudioSegment = None
from io import BytesIO

from trytond.model import ModelView, ModelSQL, fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction
from trytond.pyson import Eval, Bool
from trytond.modules.widgets import tools
from trytond.i18n import gettext
from trytond.exceptions import UserError
from .openai_api import (API_KEY as openai_api_key,
    transcribe as openai_transcribe)
from .gladia_api import (API_KEY as gladia_api_key,
    upload_file as gladia_upload_file,
    create_transcribe as gladia_create_transcribe,
    get_transcribe as gladia_get_transcribe)


def convert_wav_to_mp3(wav_file):
    '''
    Convert the wave_file to MP3 format.
    The wave_fil could be byte type object or a BytesIO object.
    The MP3 returned will be the same type as the wav_file.
    '''
    if not AudioSegment:
        raise Exception("pydub is not installed")
    if isinstance(wav_file, bytes):
        input = BytesIO(wav_file)
    elif isinstance(wav_file, BytesIO):
        input = wav_file
    else:
        raise Exception("Invalid wav_file type")
    buffer = BytesIO()
    audio = AudioSegment.from_wav(input)
    audio.export(buffer, format="mp3")
    if isinstance(wav_file, bytes):
        mp3_file = buffer.getvalue()
    else:
        mp3_file = buffer
    return mp3_file


class CallTranscription(ModelSQL, ModelView):
    'Call Transcription'
    __name__ = 'call.transcription'

    name = fields.Char('Name', readonly=True)
    activity = fields.Many2One('activity.activity', 'Activity')
    transcription = fields.Text('Transcription')


class CallTranscriptionLLMProcess(ModelSQL, ModelView):
    'Call Transcription LLM Process'
    __name__ = 'call.transcription.llm.process'

    activity = fields.Many2One('activity.activity', 'Activity')
    prompt = fields.Char('Prompt')
    response = fields.Text('Response', readonly=True)


STATE = {
    'invisible': ~Bool(Eval('call_id')),
    }


class Activity(metaclass=PoolMeta):
    'Activity'
    __name__ = 'activity.activity'

    call_id = fields.Char('Call ID', readonly=True)
    language = fields.Many2One('ir.lang', 'Language Call', states=STATE)
    speakers = fields.Integer('Number of Speakers', states=STATE,
        help="Leave it blank to let the AI try to figure it out.")
    translation_languages = fields.MultiSelection('get_translation_languages',
        "Translation Languages", states=STATE)
    transcriptions = fields.One2Many('call.transcription', 'activity',
        'Call Transcriptions', states=STATE)
    summarization = fields.Selection([
            (None, ""),
            ('general', "General"),
            ('bullet_points', "Bullet Points"),
            ('concise', "Concise"),
            ], "Summarization", states=STATE)
    summarization_result = fields.Text('Summarization Result', readonly=True)
    llm_process = fields.One2Many('call.transcription.llm.process', 'activity',
        'LLM Processing', states=STATE)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'get_call_transcription': {},
                })

    @classmethod
    def get_translation_languages(cls):
        pool = Pool()
        Language = pool.get('ir.lang')

        langs = Language.search([])
        return [(x.code, x.name) for x in langs]

    @staticmethod
    def default_speakers():
        return 2

    @staticmethod
    def default_translation_languages():
        return None

    @staticmethod
    def default_summarization():
        return 'bullet_points'

    @classmethod
    @ModelView.button
    def get_call_transcription(cls, activities):
        '''
        Download the file recorde of the call and convert speech to text
        '''
        # TODO: Allow to download the record file at any time, not only
        # after the call.
        pool = Pool()
        Configuration = pool.get('activity.configuration')
        User = pool.get('res.user')
        Attachment = pool.get('ir.attachment')
        CallTranscription = pool.get('call.transcription')

        configuration = Configuration(1)
        user = User(Transaction().user)
        employee = user.employee if user else None
        if not employee:
            raise UserError(
                gettext('yeastar_api.msg_not_employee',
                    user=user.name))
        extension = employee.yeastar_extension
        pbx = employee.yeastar_pbx
        if not extension or not pbx:
            raise UserError(
                gettext('yeastar_api.msg_not_extension_pbx_employee',
                    employee=employee))
        attachments = []
        to_save = []
        for activity in activities:
            if not activity.call_id:
                continue
            page = configuration.yeastar_record_list_page or '1'
            page_size = configuration.yeastar_record_list_page_size or '5'
            records_list = pbx.get_records_list(page=page, page_size=page_size,
                sort_by='id', order_by='desc')
            if not records_list or not records_list.get('data', None):
                continue
            data = records_list['data']
            id = None
            duration = None
            for record in data:
                if (record.get('call_from_number') != str(extension)
                        and record.get('call_to_number') != str(extension)):
                    continue
                if activity.call_id in record.get('file', ''):
                    id = record.get('id')
                    duration = record.get('duration')
                    break

            record_info = None
            token = pbx.get_token()
            if not token:
                return
            if id:
                record_info = pbx.download_record(id, token=token)
            file_name = (record_info.get('file_name', None)
                if record_info else None)
            audio_url = (record_info.get('download_resource_url', None)
                if record_info else None)
            if (not file_name or not audio_url):
                continue
            wav_response = pbx._requests('GET', args=audio_url, token=token)

            # Convert the WAV file donwlaod from Yeastar to MP3 to take up
            # less space.
            mp3_response = None
            try:
                mp3_response = convert_wav_to_mp3(wav_response)
            except Exception:
                pass
            if mp3_response:
                response = mp3_response
                file_name = file_name.replace('.wav', '.mp3')
                mime = 'audio/mp3'
            else:
                response = wav_response
                mime = 'audio/wav'

            transcription = None
            lang = activity.language.code if activity.language else None
            if gladia_api_key:
                audio_to_llm = []
                for llm in activity.llm_process:
                    audio_to_llm.append(llm.prompt)
                file = gladia_upload_file(response, filename=file_name,
                    mime=mime)
                audio_url = file.get('audio_url', None)
                transcription = gladia_create_transcribe(audio_url, lang=lang,
                    number_speakers=activity.speakers,
                    translation_lang=activity.translation_languages,
                    summarization=activity.summarization,
                    audio_to_llm=audio_to_llm, metadata={})
                if transcription:
                    transcription = gladia_get_transcribe(
                        transcription.get('id', None))
            elif openai_api_key:
                if not lang:
                    lang = Transaction().context.get('language')
                transcription = openai_transcribe(response, language=lang)
            else:
                transcription = None
            if not transcription:
                return None

            activity.duration = (timedelta(seconds=duration)
                if duration else timedelta(seconds=0))
            for key, value in transcription.items():
                if key == 'transcript':
                    call_transcription = CallTranscription()
                    call_transcription.name = value.get('language', lang)
                    call_transcription.activity = activity
                    call_transcription.transcription = tools.text_to_js(
                        value.get('result', ''))
                    call_transcription.save()
                if key == 'sentences':
                    sentences = []
                    sentence_lang = (value[0].get('language', lang)
                        if value and len(value) > 0 else lang)
                    for res in value:
                        sentences.append(
                            "(%s) %s" % (res.get('speaker', ''),
                                res.get('sentence', '')))
                    call_transcription = CallTranscription()
                    call_transcription.name = sentence_lang
                    call_transcription.activity = activity
                    call_transcription.transcription = tools.text_to_js(
                        "\n".join(sentences))
                    call_transcription.save()
                if key == 'translations':
                    for res in value:
                        call_transcription = CallTranscription()
                        call_transcription.name = res.get('language', lang)
                        call_transcription.activity = activity
                        call_transcription.transcription = tools.text_to_js(
                            res.get('result', ''))
                        call_transcription.save()
                if key == 'summarization':
                    activity.summarization_result = tools.text_to_js(value)
                if key == 'llm':
                    for llm in activity.llm_process:
                        for res in value:
                            if res.get('prompt', '') == llm.prompt:
                                llm.response = tools.text_to_js(
                                    res.get('result', ''))
                        llm.save()
            to_save.append(activity)

            attach = Attachment(
                name=file_name,
                type='data',
                #data=audio_content,
                data=response,
                resource=activity)
            attachments.append(attach)

        if to_save:
            cls.save(to_save)
        if attachments:
            Attachment.save(attachments)

    @classmethod
    def copy(cls, activities, default=None):
        default = default.copy() if default is not None else {}
        default.setdefault('call_id', None)
        default.setdefault('language', None)
        return super().copy(activities, default=default)
