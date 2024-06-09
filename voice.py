#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
# https://help.yeastar.com/en/p-series-cloud-edition/index.html
import html
try:
    import google.cloud.translate_v2 as translate
except ImportError:
    translate = None
try:
    import google.cloud.texttospeech as tts
except ImportError:
    tts = None

from trytond.pool import Pool
from trytond.model import ModelView, ModelSQL, fields, Unique
from trytond.pyson import Eval, Bool
from trytond.i18n import gettext
from trytond.exceptions import UserError
from trytond.wizard import Button, StateTransition, StateView, Wizard


class VoicePromptText(ModelSQL, ModelView):
    'Voice Prompt Text'
    __name__ = 'voice.prompt.text'

    prompt = fields.Many2One('voice.prompt', 'Prompt', required=True,
        ondelete='CASCADE',
        states={
            'required': False,
            })
    main_language = fields.Boolean('Main Language')
    language_code = fields.Selection('get_languages_code', 'Language',
        sort=True)
    voice = fields.Selection('get_voice', 'Voice')
    text = fields.Text('Text')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('action_uniq', Unique(t, t.prompt, t.main_language),
                'Could be only one main language.'),
            ('action_uniq', Unique(t, t.prompt, t.language_code),
                'There is already one language like that for this voice.'),
            ]

    @classmethod
    def get_languages_code(cls):
        if not tts:
            return []
        client = tts.TextToSpeechClient()
        response = client.list_voices()
        languages = set()
        for voice in response.voices:
            for language_code in voice.language_codes:
                languages.add(language_code)
        return list((x, x) for x in sorted(languages))

    @fields.depends('language_code')
    def get_voice(self):
        result =  [(None, '')]
        if self.language_code:
            if not tts:
                return result
            client = tts.TextToSpeechClient()
            response = client.list_voices(language_code=self.language_code)
            for voice in response.voices:
                name = voice.name
                gender = tts.SsmlVoiceGender(voice.ssml_gender).name
                value = "%s | %s" %(name, gender)
                result.append((name, value))
        return result

    @classmethod
    def copy(cls, prompts_text, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()

        default.setdefault('main_language', None)
        default.setdefault('voice', None)
        default.setdefault('text', None)
        return super().copy(prompts_text, default=default)


class VoicePrompt(ModelSQL, ModelView):
    'Voice Prompt'
    __name__ = 'voice.prompt'

    name = fields.Char('Name', required=True)
    prompts_text = fields.One2Many('voice.prompt.text', 'prompt',
        'Prompts Text')
    main_language = fields.Function(fields.Boolean('Main Language'),
        'on_change_with_main_language')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'translate_prompt_text': {
                    'invisible': ~Bool(Eval('main_language')),
                    'depends': ['main_language'],
                    },
                'create_voice_files': {
                    'invisible': ~Bool(Eval('main_language')),
                    'depends': ['main_language'],
                    },
                })

    @fields.depends('prompts_text')
    def on_change_with_main_language(self, name=None):
        language = None
        for text in self.prompts_text:
            if text.main_language:
                language = text.main_language
                break
        return language

    def translate_google(self, text, source, target):
        # Text can also be a sequence of strings, in which case this method
        # will return a sequence of results for each text.
        if not translate:
            raise UserError(gettext('yeastar_api.msg_missing_library'))
        translate_client = translate.Client()
        model = 'nmt'
        try:
            result = translate_client.translate(text, source_language=source,
                target_language=target, model=model)
            res = result['translatedText']
            res = html.unescape(res)
        except:
            raise UserError(
                gettext('yeastar_api.msg_prompt_not_translated',
                    name=self.name,
                    source=source,
                    target=target))
        return res

    @classmethod
    @ModelView.button_action('yeastar_api.wizard_voice_prompt_translate_text')
    def translate_prompt_text(cls, prompts):
        pass

    @classmethod
    @ModelView.button
    def create_voice_files(cls, prompts):
        '''
        Text to speech and generate XXX file.
        Synthesizes speech from the input string of text or ssml.
        Make sure to be working in a virtual environment.

        Note: ssml must be well-formed according to:
        https://www.w3.org/TR/speech-synthesis/
        '''
        pool = Pool()
        Attachment = pool.get('ir.attachment')

        if not tts:
            return
        attachments = []
        for prompt in prompts:
            for prompt_text in prompt.prompts_text:
                voice_name = prompt_text.voice
                if not voice_name:
                    raise UserError(
                        gettext('yeastar_api.msg_prompt_text_missing_voice',
                            prompt=prompt.name,
                            lang=prompt_text.language_code))
                language_code = "-".join(voice_name.split("-")[:2])
                text_input = tts.SynthesisInput(text=prompt_text.text)
                voice_params = tts.VoiceSelectionParams(
                    language_code=language_code, name=voice_name)
                # Format requirement for Yeastar prompts:
                #PCM, 8K, 16bit, 128kbps
                #A-law (g.711), 8k, 8bit, 64kbps
                #u-law (g.711), 8k, 8bit, 64kbps
                # https://cloud.google.com/text-to-speech/docs/reference/rest/v1/AudioConfig
                audio_config = tts.AudioConfig(
                    audio_encoding=tts.AudioEncoding.MULAW)

                client = tts.TextToSpeechClient()
                response = client.synthesize_speech(
                    input=text_input,
                    voice=voice_params,
                    audio_config=audio_config)
                attach = Attachment(
                    name="%s.wav" % voice_name,
                    type='data',
                    data=response.audio_content,
                    resource=prompt)
                attachments.append(attach)
        if attachments:
            Attachment.save(attachments)


class VoicePromptTranslateTextStart(ModelView):
    'Voice Prompt Translate Text Start'
    __name__ = 'voice.prompt.translate_text.start'

    prompt = fields.Many2One('voice.prompt', "Prompt",
        states={
            'invisible': True,
            })
    language_codes = fields.MultiSelection('get_languages_codes', 'Language',
        sort=True, required=True)

    @classmethod
    def get_languages_codes(cls):
        pool = Pool()
        PromptText = pool.get('voice.prompt.text')

        return PromptText.get_languages_code()


class VoicePromptTranslateText(Wizard):
    'Voice Prompt Translate Text'
    __name__ = 'voice.prompt.translate_text'

    start = StateView('voice.prompt.translate_text.start',
        'yeastar_api.voice_prompt_translate_text_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Translate', 'translate_text', 'tryton-ok', True),
            ])
    translate_text = StateTransition()

    def default_start(self, fields):
        return {
            'prompt': self.record.id,
            }

    def transition_translate_text(self):
        '''
        Transalte from the main language to the other languages.
        '''
        pool = Pool()
        PromptText = pool.get('voice.prompt.text')

        prompt = self.start.prompt
        languages = self.start.language_codes
        if not prompt or not languages:
            return 'end'
        sources = [x for x in prompt.prompts_text if x.main_language]
        not_translate = [x.language_code for x in prompt.prompts_text]
        targets = [x for x in languages if x not in not_translate]

        prompts_to_save = []
        if sources and targets:
            source = sources[0]
            for target in targets:
                new_prompt, = PromptText.copy([source])
                new_prompt.language_code = target
                new_prompt.text = prompt.translate_google(
                    source.text, source.language_code, target)
                prompts_to_save.append(new_prompt)
        if prompts_to_save:
            PromptText.save(prompts_to_save)
        return 'end'
