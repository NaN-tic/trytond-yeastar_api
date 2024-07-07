#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
# https://help.yeastar.com/en/p-series-cloud-edition/index.html
from io import BytesIO
from openai import OpenAI

from trytond.config import config


API_KEY = config.get('openai', 'api_key')
ORGANITZATION = config.get('openai', 'organization')


def transcribe(audio_file, language=None):
    if not audio_file:
        return None
    if not API_KEY or not ORGANITZATION:
        return None
    client = OpenAI(api_key=API_KEY, organization=ORGANITZATION)
    audio = BytesIO(audio_file)
    # It is important to set the filename otherwise the API will not
    # recognize the audio
    audio.name = 'file.wav'
    transcription = client.audio.transcriptions.create(model="whisper-1",
        file=audio, language=language)
    return {
        'transcript': {
            'language': language,
            'result': transcription.text,
        }}
