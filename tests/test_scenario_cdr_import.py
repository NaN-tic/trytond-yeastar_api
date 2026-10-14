import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from proteus import Model

from trytond.modules.company.tests.tools import create_company, get_company
from trytond.modules.yeastar_api import api
from trytond.pool import Pool
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestCDRImport(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('yeastar_api')
        create_company(config=config)
        company = get_company()
        PBX = Model.get('yeastar.pbx')
        CDR = Model.get('yeastar.cdr')
        Cron = Model.get('ir.cron')
        pbx = PBX(name='Test PBX', company=company,
            base_url='https://pbx.example.com', api_path='openapi/v1.0',
            username='test', password='test', time_format='%Y/%m/%d %H:%M:%S')
        pbx.save()
        cron = Cron(method='yeastar.cdr|get_calls_details',
            interval_number=1, interval_type='days', hour=3, minute=0)
        cron.save()
        records = [dict(uid=str(i), time=f'2026/10/14 10:00:0{i}',
            call_from='100', call_to='200', call_type='Inbound',
            last_status=status, routing_duration=0, handling_duration=12,
            call_duration=12, dids=['123'], outbound_caller_ids=[])
            for i, status in enumerate(['ANSWERED', 'ABANDONED'])]
        pages = []

        def get(url, params, timeout):
            self.assertEqual(url,
                'https://pbx.example.com/openapi/v2.0/cdr/search')
            self.assertEqual(timeout, (10, 120))
            self.assertEqual(params['order_by'], 'asc')
            page = params['page']
            pages.append(page)
            # A short page is not the end, and total_number is unreliable.
            data = records[page - 1:page] if page <= 2 else None
            return Mock(json=lambda: {
                'errcode': 0, 'total_number': 999, 'data': data})

        PBXModel = Pool(config.database_name).get('yeastar.pbx')
        with patch.object(PBXModel, 'get_token', return_value='token'), \
                patch.object(api.requests, 'get', side_effect=get):
            cron.click('run_once')
            self.assertEqual(len(CDR.find()), 2)
            first, = CDR.find([('uid', '=', '0')])
            self.assertEqual(first.ring_duration, timedelta(0))
            self.assertEqual(first.talk_duration, timedelta(seconds=12))
            abandoned, = CDR.find([('disposition', '=', 'abandoned')])
            self.assertEqual(abandoned.uid, '1')
            records[0]['handling_duration'] = 15
            cron.click('run_once')
            self.assertEqual(len(CDR.find()), 2)
            first.reload()
            self.assertEqual(first.talk_duration, timedelta(seconds=15))
            self.assertIn(3, pages)
        pbx.reload()
        self.assertEqual(pbx.api_path, 'openapi/v1.0')
