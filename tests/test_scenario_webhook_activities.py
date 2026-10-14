import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from unittest.mock import patch

import pytz
from proteus import Model, Wizard
from werkzeug.test import Client

from trytond.modules.company.tests.tools import create_company, get_company
from trytond.pool import Pool
from trytond.protocols.wrappers import Response
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction
from trytond.wsgi import app


class TestWebhookActivities(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def send(self, type_, message, received_at):
        body = json.dumps({'type': type_, 'msg': json.dumps(message)}).encode()
        signature = base64.b64encode(hmac.digest(
            self.pbx.webhook_secret.encode(), body, 'sha256')).decode()
        response = self.client.post(self.path, data=body,
            content_type='application/json', headers={'X-Signature': signature})
        self.assertEqual(response.status_code, 204)
        # Reproduce the reception timeline without sleeping or changing the
        # actual PBX timestamps stored inside the signed payload.
        with Transaction().start(self.config.database_name, 0):
            Event = Pool().get('yeastar.webhook.event')
            table = Event.__table__()
            Transaction().connection.cursor().execute(*table.update(
                    [table.create_date], [received_at],
                    where=table.digest == hashlib.sha256(body).hexdigest()))

    def cdr(self, call_id, note, extension='213', direction='Inbound',
            talk=60, total=65, start=None, **values):
        start = start or self.start
        local = pytz.utc.localize(start).astimezone(
            pytz.timezone('Europe/Madrid'))
        message = {
            'call_id': call_id, 'uid': f'cdr-{call_id}', 'call_note_id': note,
            'time_start': local.strftime('%Y-%m-%d %H:%M:%S'),
            'call_from': '+34911222333' if direction == 'Inbound' else extension,
            'call_to': extension if direction == 'Inbound' else '+34911222333',
            'type': direction, 'status': 'ANSWERED',
            'talk_duration': talk, 'call_duration': total,
            'recording': f'{call_id}-{note}.wav',
            }
        message.update(values)
        return message

    def test(self):
        self.config = activate_modules('yeastar_api')
        create_company(config=self.config)
        company = get_company()
        company.timezone = 'Europe/Madrid'
        company.save()
        PBX = Model.get('yeastar.pbx')
        Party = Model.get('party.party')
        Employee = Model.get('company.employee')
        Activity = Model.get('activity.activity')
        Type = Model.get('activity.type')
        Configuration = Model.get('activity.configuration')
        Event = Model.get('yeastar.webhook.event')
        Call = Model.get('yeastar.webhook.call')
        Leg = Model.get('yeastar.webhook.leg')
        Cron = Model.get('ir.cron')
        Attachment = Model.get('ir.attachment')
        User = Model.get('res.user')
        activity_type = Type(name='Phone Call')
        activity_type.save()
        configuration = Configuration(1)
        configuration.default_yeastar_activity_type = activity_type
        configuration.save()
        self.pbx = PBX(name='Test PBX', company=company,
            base_url='https://pbx.example.com', api_path='openapi/v1.0',
            username='test', password='test', time_format='%Y/%m/%d %H:%M:%S',
            webhook_base_url='https://tryton.example.com',
            webhook_secret='test-secret', webhook_enabled=True,
            webhook_timezone='Europe/Madrid')
        self.pbx.save()
        employees = []
        for extension in [213, 205]:
            party = Party(name=f'Employee {extension}')
            party.save()
            employee = Employee(party=party, company=company,
                yeastar_pbx=self.pbx, yeastar_extension=extension)
            employee.save()
            employees.append(employee)
        # The end date itself is still a working day.
        employees[1].end_date = datetime.now().date()
        employees[1].save()
        departed_party = Party(name='Former Employee')
        departed_party.save()
        departed = Employee(party=departed_party, company=company,
            yeastar_pbx=self.pbx, yeastar_extension=206,
            end_date=datetime.now().date() - timedelta(days=1))
        departed.save()
        customer = Party(name='External Customer')
        contact = customer.contact_mechanisms.new()
        contact.type = 'phone'
        contact.value = '+34911222333'
        customer.save()
        process, = Cron.find([
                ('method', '=', 'yeastar.webhook.event|process_pending')])
        clean, = Cron.find([('method', '=', 'yeastar.webhook.event|clean')])
        self.client = Client(app, Response)
        self.path = urlsplit(self.pbx.webhook_url).path
        self.start = (datetime.now() - timedelta(days=5)).replace(microsecond=0)
        first = self.cdr('external', 'first', talk=110, total=120)
        second = self.cdr('external', 'second', extension='205',
            talk=60, total=60, start=self.start + timedelta(seconds=120),
            recording='different-recording-call-id.wav')
        for seconds, status in [(10, 'ANSWER'), (80, 'HOLD'), (120, 'BYE')]:
            self.send(30011, {'call_id': 'external', 'members': [
                {'extension': {'number': '213', 'channel_id': 'channel-213',
                    'member_status': status}},
                {'inbound': {'member_status': 'ANSWERED'}},
                ]}, self.start + timedelta(seconds=seconds))
        self.send(30011, {'call_id': 'external', 'members': [
            {'inbound': {'member_status': 'BYE'}},
            ]}, self.start + timedelta(seconds=180))
        self.send(30012, first, self.start + timedelta(seconds=181))
        self.send(30012, second, self.start + timedelta(seconds=182))
        self.send(30013, {'call_id': 'consultation', 'members': [
            {'extension': {'number': '205', 'channel_id': 'channel-205',
                'member_status': 'ANSWER'}},
            ]}, self.start + timedelta(seconds=120))
        self.send(30012, self.cdr('consultation', 'internal',
            direction='Internal', call_to='205'),
            self.start + timedelta(seconds=121))
        self.send(30012, self.cdr('missed', 'missed', status='NO ANSWER', talk=0),
            self.start + timedelta(seconds=200))
        self.send(30012, self.cdr('queue', 'queue', extension='6402'),
            self.start + timedelta(seconds=201))
        # Capture mode never creates activities.
        process.click('run_once')
        self.assertEqual(Activity.find(), [])
        self.pbx.webhook_mode = 'activities'
        self.pbx.save()
        # Reuse an activity made manually while the call was in progress.
        user = User(self.config.user)
        user.employees.append(employees[0])
        user.employee = employees[0]
        user.save()
        self.config._context = User.get_preferences(True, {})
        with patch.object(Pool(self.config.database_name).get('yeastar.pbx'),
                'query_call_by_extension', return_value={
                    'call_id': 'external', 'callee': '+34911222333'}):
            wizard = Wizard('yeastar.create.from_progress_call')
            wizard.execute('create_activity')
        manual, = Activity.find()
        manual.subject = 'Keep this subject'
        manual.save()
        process.click('run_once')
        self.assertEqual(Event.find([('state', '=', 'error')]), [])
        self.assertEqual(len(Activity.find()), 2)
        manual.reload()
        self.assertEqual(manual.duration, timedelta(seconds=70))
        self.assertEqual(manual.subject, 'Keep this subject')
        transferred, = Activity.find([('employee', '=', employees[1].id)])
        self.assertEqual(transferred.duration, timedelta(seconds=60))
        self.assertEqual(transferred.dtstart,
            self.start + timedelta(seconds=120))
        self.assertEqual(transferred.party, customer)
        self.assertEqual(transferred.state, 'done')
        self.assertEqual(len(Call.find()), 2)
        self.assertEqual(len(Leg.find()), 2)
        self.assertEqual(len(Event.find([('state', '=', 'discarded')])), 3)
        self.assertEqual(Attachment.find(), [])
        # Retries with different JSON envelopes must not add another leg.
        duplicate = dict(first, extra='retry with additional metadata')
        self.send(30012, duplicate, self.start + timedelta(seconds=183))
        process.click('run_once')
        manual.reload()
        self.assertEqual(manual.duration, timedelta(seconds=70))
        self.assertEqual(len(Leg.find()), 2)
        # A return to the first employee adds only the new external segment.
        manual.duration += timedelta(seconds=5)
        manual.save()
        self.send(30012, self.cdr('external', 'return', talk=20, total=20,
            start=self.start + timedelta(seconds=180)),
            self.start + timedelta(seconds=201))
        process.click('run_once')
        manual.reload()
        self.assertEqual(manual.duration, timedelta(seconds=95))
        self.assertEqual(len(Activity.find()), 2)
        self.assertEqual(len(Leg.find()), 3)
        # Queueing is idempotent and does not download or process audio inline.
        manual.click('get_call_transcription')
        manual.click('get_call_transcription')
        self.assertEqual({j.recording for j in manual.transcription_jobs},
            {'external-first.wav', 'external-return.wav'})
        manual.reload()
        self.assertEqual(manual.duration, timedelta(seconds=95))
        self.assertEqual(len(manual.transcriptions), 0)
        self.assertEqual(len(Attachment.find()), 0)
        # Bad CDRs roll back atomically without preventing other calls.
        self.send(30012, self.cdr('bad', 'valid-before-bad'),
            self.start + timedelta(seconds=209))
        self.send(30012, self.cdr('bad', 'bad', talk=-1),
            self.start + timedelta(seconds=210))
        self.send(30012, self.cdr('outgoing', 'outgoing', direction='Outbound'),
            self.start + timedelta(seconds=211))
        self.send(30012, self.cdr('departed', 'departed', extension='206'),
            self.start + timedelta(seconds=212))
        process.click('run_once')
        errors = Event.find([('call_id', '=', 'bad')])
        self.assertEqual(len(errors), 2)
        error = errors[0]
        self.assertEqual(error.state, 'error')
        self.assertIn('duration', error.processing_message)
        self.assertEqual(len(Activity.find()), 3)
        self.assertEqual(len(Call.find()), 3)
        # Pending calls and errors protect their entire raw history.
        self.send(30011, {'call_id': 'live', 'members': [
            {'inbound': {'member_status': 'ANSWERED'}},
            ]}, self.start)
        process.click('run_once')
        with Transaction().start(self.config.database_name, 0):
            table = Pool().get('yeastar.webhook.event').__table__()
            Transaction().connection.cursor().execute(*table.update(
                    [table.create_date], [datetime.now() - timedelta(days=120)]))
        self.pbx.webhook_retention_months = 0
        self.pbx.save()
        before = len(Event.find())
        clean.click('run_once')
        self.assertEqual(len(Event.find()), before)
        self.pbx.webhook_retention_months = 3
        self.pbx.save()
        clean.click('run_once')
        self.assertEqual({e.call_id for e in Event.find()}, {'bad', 'live'})
        self.assertEqual(len(Activity.find()), 3)
        self.assertEqual(len(Leg.find()), 4)
        self.assertEqual(len(Attachment.find()), 0)
        # Permanent leg identity survives raw event cleanup.
        self.send(30012, first, self.start + timedelta(seconds=181))
        process.click('run_once')
        manual.reload()
        self.assertEqual(manual.duration, timedelta(seconds=95))
        self.assertEqual(len(Activity.find()), 3)
        self.assertEqual(len(Leg.find()), 4)
        error.click('retry')
        error.reload()
        self.assertEqual(error.state, 'captured')
        self.assertIsNone(error.processing_message)
