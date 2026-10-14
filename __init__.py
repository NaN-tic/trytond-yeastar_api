# This file is part asterisk module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import configuration
from . import api
from . import activity
from . import contact_mechanism
from . import company
from . import voice
from . import webhook
from . import webhook_processing
from . import routes

__all__ = ['register', 'routes']


def register():
    Pool.register(
        configuration.ActivityConfiguration,
        configuration.ActivityConfigurationYeastar,
        api.YeastarPBX,
        api.YeastarEndpoint,
        api.YeastarPhonebook,
        api.YeastarContact,
        api.YeastarPhonebookContact,
        api.CreateFromProgressCallStart,
        api.YeastarCDR,
        api.Cron,
        activity.CallTranscription,
        activity.CallTranscriptionLLMProcess,
        activity.Activity,
        contact_mechanism.ContactMechanism,
        company.Employee,
        voice.VoicePromptText,
        voice.VoicePrompt,
        voice.VoicePromptTranslateTextStart,
        webhook.PBX,
        webhook.WebhookEvent,
        webhook_processing.WebhookCall,
        webhook_processing.WebhookLeg,
        webhook_processing.WebhookEvent,
        webhook_processing.Cron,
        module='yeastar_api', type_='model')
    Pool.register(
        api.CreateFromProgressCall,
        api.YeastarGetCallsDetails,
        voice.VoicePromptTranslateText,
        module='yeastar_api', type_='wizard')
