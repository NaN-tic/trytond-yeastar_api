import base64
import hashlib
import hmac
import json
import unittest
from urllib.parse import quote, urlsplit

from proteus import Model
from werkzeug.test import Client

from trytond.modules.company.tests.tools import create_company, get_company
from trytond.protocols.wrappers import Response
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.wsgi import app


class TestWebhookCapture(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('yeastar_api')
        create_company(config=config)
        PBX = Model.get('yeastar.pbx')
        Event = Model.get('yeastar.webhook.event')
        Activity = Model.get('activity.activity')
        pbx = PBX(
            name='Test PBX', company=get_company(),
            base_url='https://pbx.example.com', api_path='openapi/v1.0',
            username='test', password='test', time_format='%Y/%m/%d %H:%M:%S',
            webhook_base_url='https://tryton.example.com/',
            webhook_secret='test-webhook-signing-secret',
            webhook_enabled=True)
        pbx.save()
        self.assertEqual(pbx.webhook_url,
            f'https://tryton.example.com/{quote(config.database_name, safe="")}'
            f'/yeastar/webhook/{pbx.id}')
        self.assertEqual(pbx.webhook_mode, 'capture')
        path = urlsplit(pbx.webhook_url).path
        client = Client(app, Response)
        payloads = [
            {'event': 'test', 'message': 'Connectivity test'},
            {'type': 30011, 'sn': 'test-pbx',
                'msg': json.dumps({'call_id': '1790587807.3343',
                    'members': [{'extension': {'number': '213',
                        'member_status': 'ANSWER'}}]})},
            {'type': 30013, 'msg': {'call_id': '1790587807.3343'}},
            ]
        for payload in payloads:
            body = json.dumps(payload).encode()
            signature = base64.b64encode(hmac.digest(
                pbx.webhook_secret.encode(), body, 'sha256')).decode()
            for attempt in range(2):
                response = client.post(path, data=body,
                    content_type='application/json',
                    headers={'X-Signature': signature})
                self.assertEqual(response.status_code, 204)
            event, = Event.find([
                    ('digest', '=', hashlib.sha256(body).hexdigest()),
                    ])
            self.assertEqual(event.payload, body.decode())
            self.assertEqual(event.state, 'captured')
            if payload.get('type'):
                self.assertEqual(event.call_id, '1790587807.3343')
        self.assertEqual(len(Event.find()), 3)
        self.assertEqual(Activity.find(), [])
        for headers, rejected_body, status in [
                ({}, body, 403),
                ({'X-Signature': 'invalid'}, body, 403),
                ({'X-Signature': signature}, body + b' ', 403),
                ]:
            response = client.post(path, data=rejected_body,
                content_type='application/json', headers=headers)
            self.assertEqual(response.status_code, status)
        for body in [b'not json', b'[]', b'{}']:
            signature = base64.b64encode(hmac.digest(
                pbx.webhook_secret.encode(), body, 'sha256')).decode()
            response = client.post(path, data=body,
                headers={'X-Signature': signature})
            self.assertEqual(response.status_code, 400)
        pbx.webhook_enabled = False
        pbx.save()
        response = client.post(path, data=body,
            headers={'X-Signature': signature})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(Event.find()), 3)
