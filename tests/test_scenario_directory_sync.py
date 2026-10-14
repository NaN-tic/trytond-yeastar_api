import json
import unittest
from unittest.mock import patch

from proteus import Model

from trytond.modules.company.tests.tools import create_company
from trytond.pool import Pool
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestDirectorySync(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('yeastar_api')
        create_company(config=config)
        create_company(config=config)
        Company = Model.get('company.company')
        companies = Company.find()
        User = Model.get('res.user')
        user = User(config.user)
        user.companies.clear()
        user.companies.extend(companies)
        user.company_filter = 'all'
        user.save()
        config._context = User.get_preferences(True, {})
        PBX = Model.get('yeastar.pbx')
        Party = Model.get('party.party')
        Contact = Model.get('yeastar.contact')
        Book = Model.get('yeastar.phonebook')
        Cron = Model.get('ir.cron')
        pbxs = []
        books = []
        for name, pbx_company in zip(['First', 'Second'], companies):
            pbx = PBX(name=name, company=pbx_company,
                base_url='https://pbx.example.com', api_path='openapi/v1.0',
                username='test', password='test',
                time_format='%Y/%m/%d %H:%M:%S')
            pbx.save()
            pbxs.append(pbx)
            book = Book(name=name, yeastar_pbx=pbx)
            book.save()
            books.append(book)
        party = Party(name='Customer')
        mechanism = party.contact_mechanisms.new(type='phone', value='123456')
        party.save()
        mechanism, = party.contact_mechanisms
        cron, = Cron.find([('method', '=', 'yeastar.pbx|sync_directory')])
        cron.companies.clear()
        cron.companies.extend([Company(c.id) for c in companies])
        cron.save()
        self.assertEqual((cron.hour, cron.minute), (4, 0))
        contacts = {}
        remote_books = {}
        next_id = [1]

        def request(pbx, method, endpoint=None, data=None, args=None,
                **kwargs):
            self.assertTrue(Transaction().context['yeastar_wo_employee'])
            self.assertEqual(Transaction().context['company'], pbx.company.id)
            payload = json.loads(data) if data else {}
            key = (pbx.id, payload.get('id'))
            if endpoint == 'add_contact':
                id_ = next_id[0]
                next_id[0] += 1
                contacts[(pbx.id, id_)] = payload
                return {'errcode': 0, 'id': id_}
            if endpoint == 'update_contact':
                self.assertIn(key, contacts)
                contacts[key] = payload
            elif endpoint == 'delete_contact':
                key = (pbx.id, int(args.split('=')[1]))
                del contacts[key]
                for (pbx_id, _), book in remote_books.items():
                    if pbx_id == pbx.id:
                        book['members'].discard(key[1])
            elif endpoint == 'add_phonebook':
                id_ = next_id[0]
                next_id[0] += 1
                remote_books[(pbx.id, id_)] = dict(payload,
                    members=set(payload.get('add_contacts_ids_list', [])))
                return {'errcode': 0, 'id': id_}
            elif endpoint == 'query_phonebook':
                key = (pbx.id, int(args.split('=')[1]))
                return {'errcode': 0, 'data': {'contacts_list': [
                    {'id': id_} for id_ in remote_books[key]['members']]}}
            elif endpoint == 'update_phonebook':
                book = remote_books[key]
                book['member_select'] = payload['member_select']
                book['members'].update(payload.get('add_contacts_ids_list', []))
                book['members'].difference_update(
                    payload.get('del_contacts_ids_list', []))
            else:
                self.fail(endpoint)
            return {'errcode': 0}

        PBXModel = Pool(config.database_name).get('yeastar.pbx')
        with patch.object(PBXModel, 'get_token', return_value='token'), \
                patch.object(PBXModel, 'get_endpoint',
                    new=lambda pbx, action: action), \
                patch.object(PBXModel, '_requests', new=request):
            cron.click('run_once')
            self.assertEqual(len(contacts), 2)
            self.assertEqual(len(Contact.find()), 2)
            cron.click('run_once')
            self.assertEqual(len(contacts), 2)
            # Each PBX has its own identity for the same source number.
            first, = Contact.find([('yeastar_pbx', '=', pbxs[0].id)])
            book = books[0]
            book.reload()
            book.member_select = 'sel_specific'
            book.contacts.append(first)
            book.save()
            party.name = 'Renamed customer'
            party.save()
            mechanism, = party.contact_mechanisms
            mechanism.value = '654321'
            party.save()
            mechanism, = party.contact_mechanisms
            cron.click('run_once')
            self.assertEqual({c['first_name'] for c in contacts.values()},
                {'Renamed customer'})
            self.assertEqual({c['number_list'][0]['number']
                for c in contacts.values()}, {'654321'})
            key = (pbxs[0].id, book.yeastar_phonebook_id)
            self.assertEqual(remote_books[key]['members'],
                {first.yeastar_contact_id})
            # A new contact is published, but not selected in a specific book.
            other = Party(name='Other')
            other.contact_mechanisms.new(type='mobile', value='987654')
            other.save()
            cron.click('run_once')
            self.assertEqual(len(contacts), 4)
            self.assertEqual(remote_books[key]['members'],
                {first.yeastar_contact_id})
            book.contacts.pop()
            book.save()
            cron.click('run_once')
            self.assertFalse(remote_books[key]['members'])
            self.assertEqual(len(contacts), 4)
            # Deactivation removes the remote contact but preserves its local
            # link so reactivation can republish it.
            mechanism.active = False
            mechanism.save()
            cron.click('run_once')
            self.assertEqual(len(contacts), 2)
            mechanism.active = True
            mechanism.save()
            cron.click('run_once')
            self.assertEqual(len(contacts), 4)
            # A deleted source leaves a null link until the next sync removes
            # both managed remote contacts and their local tracking rows.
            mechanism.delete()
            cron.click('run_once')
            self.assertEqual(len(contacts), 2)
            self.assertEqual(len(Contact.find()), 2)
            # Contacts without a Tryton link are outside the managed set.
            contacts[(pbxs[0].id, 9999)] = {'first_name': 'PBX-only contact'}
            other.active = False
            other.save()
            cron.click('run_once')
            self.assertEqual(set(contacts), {(pbxs[0].id, 9999)})
