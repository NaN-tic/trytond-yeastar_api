"""OpenRouter speech-to-text discovery, options and response normalization."""
import copy
import json
import math

import requests

from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.pool import PoolMeta
from trytond.modules.ai_model.ai import OPENROUTER_MODELS_URL


class OpenRouterModel(metaclass=PoolMeta):
    __name__ = 'ai.model.openrouter'

    @classmethod
    def _fetch_openrouter_models(cls):
        models = super()._fetch_openrouter_models()
        extra = cls._fetch_transcription_data(OPENROUTER_MODELS_URL,
            params={'output_modalities': 'transcription'})
        # Fail atomically on an incomplete catalog: sync_models deactivates
        # models not present in this combined snapshot.
        by_id = {model['id']: model for model in models if model.get('id')}
        for model in extra:
            if not isinstance(model, dict) or not model.get('id'):
                raise UserError(gettext('yeastar_api.msg_stt_invalid_catalog'))
            if model['id'] in by_id:
                original = by_id[model['id']]
                architecture = original.setdefault('architecture', {})
                for key in ('input_modalities', 'output_modalities'):
                    architecture[key] = list(dict.fromkeys(
                        architecture.get(key, [])
                        + (model.get('architecture') or {}).get(key, [])))
            else:
                by_id[model['id']] = model
        return list(by_id.values())

    @classmethod
    def _fetch_transcription_data(cls, url, params=None):
        try:
            response = requests.get(url, params=params,
                headers=cls._openrouter_headers(), timeout=30)
            response.raise_for_status()
            data = response.json()['data']
            if params and not isinstance(data, list):
                raise ValueError('Expected a model list.')
            return data
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            raise UserError(gettext('ai_model.msg_openrouter_model_sync_error',
                error=str(error))) from error

    def transcription_endpoints(self):
        data = self._fetch_transcription_data(
            f'{OPENROUTER_MODELS_URL}/{self.openrouter_id}/endpoints')
        if not isinstance(data, dict) or not isinstance(data.get('endpoints'), list):
            raise UserError(gettext('yeastar_api.msg_stt_invalid_catalog'))
        return data['endpoints']


def is_transcription_model(model):
    return bool(model and 'transcription' in (
        model.output_modalities or '').replace(',', ' ').split())


def parse_options(text):
    try:
        options = json.loads(text or '{}')
    except (ValueError, TypeError) as error:
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options')) from error
    if not isinstance(options, dict):
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    # The audio, model and transport are controlled by the recording workflow.
    if set(options) - {'response_format', 'timestamp_granularities', 'provider',
            'temperature', 'language', 'keyterms', 'diarize'}:
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    if 'provider' in options and not isinstance(options['provider'], dict):
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    provider_options = options.get('provider', {}).get('options', {})
    if (not isinstance(provider_options, dict)
            or any(not isinstance(value, dict) for value in provider_options.values())):
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    return options


def merge_options(defaults, overrides):
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_options(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def request_options(model, diarize, options):
    endpoints = [e for e in model.transcription_endpoints()
        if e.get('status', 0) == 0]
    supports_diarization = bool(endpoints) and all(
        e.get('supports_diarization') for e in endpoints)
    # These three models document structured responses. Other models default
    # to plain JSON unless their endpoint explicitly advertises diarization.
    verbose = supports_diarization or model.openrouter_id in {
        'elevenlabs/scribe-v2', 'microsoft/mai-transcribe-2',
        'mistralai/voxtral-small-transcribe-3-0'}
    result = {'response_format': 'verbose_json' if verbose else 'json'}
    if verbose:
        result['timestamp_granularities'] = ['segment']
    if diarize and supports_diarization:
        result['timestamp_granularities'] = ['segment', 'word']
        tags = {e.get('tag') for e in endpoints}
        if tags == {'elevenlabs'}:
            result['provider'] = {'options': {'elevenlabs': {'diarize': True}}}
        elif tags == {'azure'}:
            result['provider'] = {'options': {
                'azure': {'diarization': {'enabled': True}}}}
        else:
            result['diarize'] = True
    result = merge_options(result, parse_options(options))
    if result.get('response_format') not in {'json', 'verbose_json'}:
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    provider_options = result.get('provider', {}).get('options', {})
    diarization_requested = result.get('diarize') or any(
        value.get('diarize') or (
            isinstance(value.get('diarization'), dict)
            and value['diarization'].get('enabled'))
        for value in provider_options.values())
    if diarization_requested and result['response_format'] != 'verbose_json':
        raise UserError(gettext('yeastar_api.msg_stt_invalid_options'))
    return result


def normalize_response(response):
    if not isinstance(response, dict):
        raise ValueError(gettext('yeastar_api.msg_ai_missing_transcript'))
    text = response.get('text')
    if not isinstance(text, str):
        raise ValueError(gettext('yeastar_api.msg_ai_missing_transcript'))
    segments = response.get('segments') or []
    words = response.get('words') or []
    if (not isinstance(segments, list) or not isinstance(words, list)
            or any(not isinstance(item, dict) for item in segments + words)):
        raise ValueError(gettext('yeastar_api.msg_ai_invalid_timestamps'))
    for item in segments + words:
        start, end = item.get('start'), item.get('end')
        if (not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= end):
            raise ValueError(gettext('yeastar_api.msg_ai_invalid_timestamps'))
    def speaker_key(item):
        return (item.get('speaker_label')
            if item.get('speaker_label') is not None else item.get('speaker'))
    utterances = segments
    # Some providers put speaker labels only on words, not on segments.
    if any(speaker_key(w) is not None for w in words):
        utterances = []
        for word in words:
            speaker = speaker_key(word)
            channel = word.get('channel')
            if (utterances and utterances[-1]['speaker'] == speaker
                    and utterances[-1]['channel'] == channel):
                utterances[-1]['text'] += ' ' + word.get('word', '').strip()
                utterances[-1]['end'] = word['end']
            else:
                utterances.append({'speaker': speaker, 'channel': channel,
                    'start': word['start'], 'end': word['end'],
                    'text': word.get('word', '').strip()})
    turns = []
    for segment in utterances:
        speaker = speaker_key(segment)
        if speaker is not None:
            turns.append(f'[Speaker {speaker}] {segment.get("text", "")}')
        else:
            turns.append(segment.get('text', ''))
    language = response.get('language')
    if not isinstance(language, str):
        language = None
    # Do not attribute the entire duration to the global language label:
    # STT models may still contain code-switching within the recording.
    speaker_transcript = ('\n'.join(turns)
        if any(speaker_key(s) is not None for s in utterances) else '')
    return {'transcript': text, 'speaker_transcript': speaker_transcript,
        'languages': [language] if language else [],
        'primary_language': language, 'translations': {}, 'durations': {},
        'utterances': utterances, 'words': words}
