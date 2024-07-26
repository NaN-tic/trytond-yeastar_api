#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
# https://help.yeastar.com/en/p-series-cloud-edition/index.html
import requests
import json
import time
from io import BytesIO, BufferedReader

from trytond.config import config
from trytond.i18n import gettext
from trytond.exceptions import UserError


URL = config.get('gladia', 'url')
API_KEY = config.get('gladia', 'api_key')


def _requests(type_, endpoint, data=None, files=None, args=None, headers=None,
    verify=True):
    if not endpoint:
        return None
    if not URL:
        return None
    url = "%s/%s" % (URL, endpoint)
    if type_ == 'POST':
        if data:
            response = requests.post(url, data=data, headers=headers,
                verify=verify)
        elif files:
            response = requests.post(url, files=files, headers=headers,
                verify=verify)
        else:
            return None
    elif type_ == 'GET' and args:
        url = "%s/%s" % (url, args)
        response = requests.get(url, headers=headers, verify=verify)
    else:
        return None
    if response.status_code in (200, 201):
        return response.json()
    else:
        response_vals = response.json()
        raise UserError(
            gettext('yeastar_api.msg_gladia_requests_fail',
                code=response_vals.get('statusCode', ''),
                message=response_vals.get('message', ''),
                validation_errors=response_vals.get('validation_errors', '')))


def upload_file(audio, filename='conversation.wav', mime='audio/wav'):
    '''
    Example response:
    {
        "audio_url": "https://api.gladia.io/file/636c70f6-92c1-4026-a8b6-0dfe3ecf826f",
        "audio_metadata": {
            "id": "636c70f6-92c1-4026-a8b6-0dfe3ecf826f",
            "filename": "conversation.wav",
            "extension": "wav",
            "size": 99515383,
            "audio_duration": 4146.468542,
            "number_of_channels": 2
            }
        }
    '''
    if not audio:
        return None

    headers = {'x-gladia-key': API_KEY}
    if isinstance(audio, bytes):
        audio_io = BytesIO(audio)
        buffered_file = BufferedReader(audio_io)
    elif isinstance(audio, BytesIO):
        buffered_file = BufferedReader(audio)
    elif isinstance(audio, BufferedReader):
        buffered_file = audio
    else:
        buffered_file = open(audio, 'rb')
    files = {"audio": (filename, buffered_file, mime)}

    return _requests('POST', 'upload', files=files, headers=headers)


def create_transcribe(audio_url, lang=None, number_speakers=None,
        translation_lang=None, summarization='bullet_points', audio_to_llm=[], 
        metadata={}):
    '''
    Since Gladia /v2/transcription endpoint only accept audio URLs, if You
    need to transcribe a file, use the 'uplod_audio_file before to get a
    file url.

    If lang is not deffined, the transcription will be done to catalan,
    even the audio is in another language.

    translation_lang: List of target languages in iso639-1 format you want
    '''
    if not audio_url:
        return None
    headers = {
        'x-gladia-key': API_KEY,
        'Content-Type': 'application/json',
        }
    data = {
        #"context_prompt": "<string>", # Context to feed the transcription model with for possible better performance
        #"custom_vocabulary": ["<string>"], # Specific vocabulary list to feed the transcription model with
        #"callback_url": "http://callback.example", # Callback URL we will do a POST request to with the result of the transcription
        #"subtitles": False,
        #"subtitles_config": {"formats": ["srt", "vtt"]},
        "diarization": True, # Enable speaker recognition (diarization) for this audio
        #"moderation": True,
        #"named_entity_recognition": True,
        #"chapterization": True,
        #"name_consistency": True,
        #"custom_spelling": True,
        #"custom_spelling_config": {"prompts": {
        #        "Gettleman": ["gettleman"],
        #        "SQL": ["Sequel"]
        #    }},
        #"structured_data_extraction": True,
        #"structured_data_extraction_config": {"classes": ["person", "organization"]},
        "sentiment_analysis": True,
        "custom_metadata": metadata,
        "sentences": True,
        #"display_mode": True, # Allows to change the output display_mode for this audio. The output will be reordered, creating new utterances when speakers overlapped
        "audio_url": audio_url,
        }
    if number_speakers:
        data["diarization_config"] = {
            "number_of_speakers": number_speakers,
        }
    else:
        data["diarization_config"] = {
            "min_speakers": 2,
            "max_speakers": 5,
        }
    if lang:
        data.update({
                "language": lang,
                })
    else:
        data.update({
                "detect_language": True, # Detect the language from the given audio
                "enable_code_switching": True, # Detect multiple languages in the given audio
                "code_switching_config": {
                    "languages": ["ca", "es", "en"]
                    }
                })
    if summarization:
        # The type of summarization to apply.
        # Available options: general, bullet_points, concise
        data.update({
                "summarization": True,
                "summarization_config": {"type": summarization},  
            })
    if translation_lang:
        data.update({
                "translation": True,
                "translation_config": {
                    "target_languages": translation_lang, # Target language in iso639-1 format you want the transcription translated to
                    "model": "base", # Model you want the translation model to use to translate. Available options: base, enhanced 
                    }})
    if audio_to_llm and isinstance(audio_to_llm, list):
        data.update({
                "audio_to_llm": True,
                "audio_to_llm_config": {
                    "prompts": audio_to_llm
                    }})

    return _requests('POST', 'transcription', data=json.dumps(data), headers=headers)


def get_transcribe(id):
    if not id:
        return None
    headers = {'x-gladia-key': API_KEY}
    transcription = {}
    # Polling the API to check for transcription completion
    while True:
        response = _requests('GET', 'transcription', args=id, headers=headers)
        status = response.get("status", None)
        if status == "done":
            result = response.get('result', None)
            if not result:
                return None

            transcription['transcript'] ={
                'language': ",".join(
                    result.get("transcription", {}).get('languages', [])),
                'result': result.get("transcription", {}).get('full_transcript', '')
                }

            if result.get("sentences", {}).get('success', False):
                transcription['sentences'] = []
                for res in result.get("sentences", {}).get('results', []):
                    transcription['sentences'].append({
                            'sentence': res.get('sentence', ''),
                            'speaker': res.get('speaker', ''),
                            'language': "sentences_%s" % res.get('language', ''),
                            })

            if result.get("translation", {}).get('success', False):
                transcription['translations'] = []
                for res in result.get("translation", {}).get('results', []):
                    transcription['translations'].append({
                            'language': ", ".join(res.get('languages', [])),
                            'result': res.get('full_transcript', ''),
                            })

            if result.get("summarization", {}).get('success', False):
                transcription['summarization'] = result.get(
                    "summarization", {}).get('results', '')

            if result.get("audio_to_llm", {}).get('success', False):
                transcription['llm'] = []
                for res in result.get("audio_to_llm", {}).get('results', []):
                    if not res.get("success", False):
                        continue
                    transcription['llm'].append({
                            'prompt': res.get('results', {}).get('prompt', ''),
                            'result': res.get('results', {}).get(
                                'response', ''),
                             })
            break
        elif status in ["queued", "processing"]:
            # Transcription is not yet complete. Waiting...
            # Wait for 5 seconds before checking again
            time.sleep(5)
        elif status == "error":
            raise UserError(
                gettext('yeastar_api.msg_gladia_error_get_transcribe',
                    code=response.get('error_code', '')))
        else:
            raise UserError(
                gettext('yeastar_api.msg_gladia_fail_get_transcribe'))
    return transcription
