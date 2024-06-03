#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from datetime import timedelta
from io import BytesIO
from openai import OpenAI

from trytond.model import ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction
from trytond.pyson import Eval, Bool
from trytond.config import config
from trytond.modules.widgets import tools
from trytond.i18n import gettext
from trytond.exceptions import UserError


class Activity(metaclass=PoolMeta):
    'Activity'
    __name__ = 'activity.activity'

    call_id = fields.Char('Call ID', readonly=True)
    language = fields.Many2One('ir.lang', 'Language Call')
    call_transcription = fields.Text('Call Transcription',
        states={
            'invisible': ~Bool(Eval('call_id')),
            })

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'get_call_transcription': {},
                })

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

        config = Configuration(1)
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
            page = config.yeastar_record_list_page or '1'
            page_size = config.yeastar_record_list_page_size or '5'
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
            if id:
                record_info = pbx.download_record(id)
            if (not record_info or not record_info.get('file_name', None)
                    or not record_info.get('download_resource_url', None)):
                continue

            # Download file
            token = pbx.get_token()
            if not token:
                return
            response = pbx._requests('GET',
                args=record_info['download_resource_url'], token=token)

            lang = activity.language.code if activity.language else None
            transcription = cls.speech_to_text(response, language=lang)

            activity.duration = (timedelta(seconds=duration)
                if duration else timedelta(seconds=0))
            activity.call_transcription = (tools.text_to_js(transcription)
                if transcription else None)
            to_save.append(activity)

            attach = Attachment(
                name=record_info['file_name'],
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
    def speech_to_text(cls, audio_file, language=None):
        if not audio_file:
            return None
        client = OpenAI(api_key=config.get('openai', 'api_key'),
            organization=config.get('openai', 'organization'))
        audio = BytesIO(audio_file)
        # It is important to set the filename otherwise the API will not
        # recognize the audio
        audio.name = 'file.wav'
        if not language:
            language = Transaction().context.get('language')
        transcription = client.audio.transcriptions.create(model="whisper-1",
            file=audio, language=language)
        return transcription.text
