#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
# https://help.yeastar.com/en/p-series-cloud-edition/index.html
import requests
import json
from datetime import datetime, timedelta

from trytond.pool import Pool, PoolMeta
from trytond.model import ModelView, ModelSQL, fields, Unique
from trytond.pyson import Eval, Bool
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)
from trytond.transaction import Transaction
from trytond.i18n import gettext
from trytond.exceptions import UserError, UserWarning

HEADERS = {
    'Content-Type': 'application/json',
    'User-Agent': 'OpenAPI',
    }

STATUS_CALL = [
    ('answered', 'Answered'),
    ('noanswer', 'No Answer'),
    ('busy', 'Busy'),
    ('failed', 'Failed'),
    ('voicemail', 'Voicemail'),
    ]


class YeastarEndpoint(ModelSQL, ModelView):
    'Yeastar Endpoint'
    __name__ = 'yeastar.endpoint'

    yeastar = fields.Many2One('yeastar.pbx', 'Yeastar PBX')
    action = fields.Selection('get_actions', 'Action', sort=False)
    endpoint = fields.Char('Endpoint')

    @classmethod
    def get_actions(cls):
        return [
            (None, ''),
            ('get_token', 'Get Token'),
            ('refresh_token', 'Refresh Token'),
            ('revoke_token', 'Revoke Token'),
            ('query_pbx_information', 'Query PBX Information'),
            ('add_phonebook', 'Add a Phonebook'),
            ('update_phonebook', 'Edit a Phonebook'),
            ('query_phonebook', 'Query information of a Phonebook'),
            ('delete_phonebook', 'Delete a Phonebook'),
            ('add_contact', 'Add a Contact'),
            ('update_contact', 'Edit a Contact'),
            ('delete_contact', 'Delete a Contact'),
            ('dial', 'Make a call'),
            ('query_call', 'Query calls'),
            ('get_records_list', 'Query Recording List'),
            ('download_record_call', 'Download Recording Call File'),
            ('cdr_search', 'Get CDR'),
            ]

    @classmethod
    def __setup__(cls):
        super().__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('action_uniq', Unique(t, t.yeastar, t.action),
                'There is already one action like this deffined in the same '
                'Yeastar PBX.'),
            ]


class YeastarPBX(ModelSQL, ModelView):
    'Yeastar PBX'
    __name__ = 'yeastar.pbx'

    name = fields.Char('Server Name', required=True)
    company = fields.Many2One('company.company', 'Company', required=True)
    serial_number = fields.Char('Serial Number', readonly=True)
    base_url = fields.Char('Yeastar base addr', required=True,
            help="IPv4 address or DNS name of the Yeastar server.")
    api_path = fields.Char('API path', required=True,
            help="The format of API path is openapi/{version}; "
            "the {version} indicates the API version.")
    endpoints = fields.One2Many('yeastar.endpoint', 'yeastar', 'Endpoints',
            help="Endpoint indicates the specific address of API request.")
    username = fields.Char('Client ID', required=True,
            help="Username that Tryton will use to login to Yeastar API.")
    password = fields.Char('Client secret', required=True, strip=True,
            help="Password that Tryton will use to login to Yeastar API.")
    time_format = fields.Char('Time Format', required=True,
            help="The time format depends on the date and time display format "
            "of PBX (set in System > Date and Time > Display Format on PBX)")
    contacts = fields.One2Many('yeastar.contact', 'yeastar_pbx',
        'Yeastar Contacts', readonly=True)
    token = fields.Char('Token', readonly=True)
    token_expire = fields.DateTime('Token Expire', readonly=True)
    refresh_token = fields.Char('Refresh Token', readonly=True)
    refresh_token_expire = fields.DateTime('Refresh Token Expire',
        readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('action_uniq', Unique(t, t.company),
                'There is already one Yeastar PBX for this company.'),
            ]
        cls._buttons.update({
                'get_pbx_information': {},
                'delete_token': {
                    'invisible': ~Bool(Eval('token')),
                    'depends': ['token'],
                    },
                'load_contacts': {},
                'sync_contacts': {},
                })

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    def get_endpoint(self, action):
        endpoint = None
        if action:
            endpoints = [x.endpoint for x in self.endpoints
                if x.action == action]
            endpoint = endpoints[0] if endpoints else None
        if not endpoint:
            raise UserError(
                gettext('yeastar_api.msg_not_yeastar_endpoint',
                    pbx=self.rec_name,
                    action=action))
        return endpoint

    def construct_url(self, endpoint=None, token=None, args=None):
        '''
        args could be:
           - Alone string (e.g.: 'id=1234')
           - A list of strings (e.g.: ['id=1234', 'name=AAAA'])
           - A dictionary with key and values strings:
               (e.g.: {'id': '1234', 'name': 'AAAA'})
        '''
        url = None
        if endpoint:
            url = "%s/%s/%s" % (self.base_url, self.api_path, endpoint)
            if args:
                if isinstance(args, list):
                    first = True
                    for arg in args:
                        if first:
                            url = "%s?%s" % (url, arg)
                            first = False
                        else:
                            url = "%s&%s" % (url, arg)
                elif isinstance(args, dict):
                    first = True
                    for param, value in args.items():
                        if first:
                            url = "%s?%s=%s" % (url, param, value)
                            first = False
                        else:
                            url = "%s&%s=%s" % (url, param, value)
                else:
                    url = "%s?%s" % (url, args)
            if token:
                url = ("%s&access_token=%s" % (url, token)
                    if args else "%s?access_token=%s" % (url, token))
        elif args:
            url = "%s%s" % (self.base_url, args)
            if token:
                url = "%s?access_token=%s" % (url, token)
        return url

    def _requests(self, type_, endpoint=None, data=None, headers=None,
            args=None, token=None, verify=False):
        url = self.construct_url(endpoint, token=token, args=args)
        if not url:
            return None
        if type_ == 'POST':
            response = requests.post(url, data=data,
                headers=headers, verify=verify)
        elif type_ == 'GET':
            response = requests.get(url, verify=verify)
            #response = requests.get(url, stream=True, verify=verify)
        else:
            return None

        if response.status_code == 200:
            if 'json' in response.headers.get('Content-Type', ''):
                return response.json()
            else:
                return response.content
        else:
            self.revoke_token(token)
            raise UserError(
                gettext('yeastar_api.msg_get_post_fail',
                    url=self.base_url,
                    pbx=self.rec_name,
                    status=response.status_code))

    def get_token(self):
        '''
        Access token expires after 30 minutes.
        You could obtain new one wiht the refresh token.
        You could revoke if it's needed.
        '''
        pool = Pool()
        User = pool.get('res.user')

        user = User(Transaction().user)
        employee = user.employee if user else None
        if not employee:
            raise UserError(
                gettext('yeastar_api.msg_not_employee',
                    user=user.name))
        if employee.yeastar_pbx != self:
            raise UserError(
                gettext('yeastar_api.msg_different_employee_pbx',
                    employtee=employee.rec_name,
                    pbx=self.rec_name))
        token = self.token or None
        token_expire = self.token_expire or None
        refresh_token = self.refresh_token or None
        refresh_token_expire = self.refresh_token_expire or None

        now = datetime.now()
        if token and token_expire and token_expire > now:
            return token
        elif (refresh_token and refresh_token_expire
                and refresh_token_expire > now):
            data = {
                "refresh_token": refresh_token
                }
            endpoint = self.get_endpoint('refresh_token')
        else:
            data = {
                "username": self.username,
                "password": self.password,
                }
            endpoint = self.get_endpoint('get_token')
        response_json = self._requests('POST', endpoint, data=json.dumps(data),
            headers=HEADERS)

        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_get_token_fail',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))

        self.token = response_json.get('access_token', None)
        token_expire = int(response_json.get('access_token_expire_time', '0'))
        self.token_expire = now + timedelta(seconds=token_expire)
        self.refresh_token = response_json.get('refresh_token', None)
        refresh_token_expire = int(response_json.get(
                'refresh_token_expire_time', '0'))
        self.refresh_token_expire = now + timedelta(
            seconds=refresh_token_expire)
        self.save()
        #If token is getted correctly, commit it to use in the future calls,
        # although the current call fail.
        Transaction().commit()

        return self.token

    def revoke_token(self, token=None):
        pool = Pool()
        Warning = pool.get('res.user.warning')

        if not token:
            return False
        endpoint = self.get_endpoint('revoke_token')
        response_json = self._requests('GET', endpoint, token=token)

        if not response_json or response_json.get('errcode', 1) != 0:
            warning_key = Warning.format('revoke_token_warning', [self.id])
            if Warning.check(warning_key):
                raise UserWarning(warning_key,
                    gettext('yeastar_api.msg_revoke_token_fail',
                        pbx=self.rec_name,
                        errmsg=(response_json.get('errmsg', '')
                            if response_json else '')))
        return True

    def query_pbx_information(self):
        token = self.get_token()
        response_json = None
        if token:
            endpoint = self.get_endpoint('query_pbx_information')
            response_json = self._requests('GET', endpoint,
                headers=HEADERS, token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_query_pbx_information_not_possible',
                    pbx=self.rec_name))
        return response_json.get('data', {}).get('sn', None)

    def make_call(self, caller_number, callee_number):
        '''
        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/make-a-call.html
        Supported types of call
        CALLER            CALLEE
        Extension         Extension number
                          IVR number
                          Ring group number
                          Queue number
                          Paging Group number
                          Conference number
                          External number
        IVR               External number
        Ring Group        External number
        Queue             External number
        Exteranl number   External number
        '''
        if not caller_number or not callee_number:
            return None
        token = self.get_token()
        response_json = None
        if token:
            data = {
                'caller': caller_number,
                'callee': callee_number,
                }
            endpoint = self.get_endpoint('dial')
            response_json = self._requests('POST', endpoint,
                data=json.dumps(data), headers=HEADERS, token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_make_call_not_possible',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        return response_json.get('call_id', None)

    def query_call_by_extension(self, extension):
        '''
        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/query-calls.html
        Return a dictionary with the structure:
        {
            call_id
            callee
            yeastar_contact
            trunk_name
            }
        ''' 
        pool = Pool()
        Contact = pool.get('yeastar.contact') 

        if not extension:
            return None
        if isinstance(extension, int):
            extension = str(extension)
        token = self.get_token()
        response_json = None
        if token:
            endpoint = self.get_endpoint('query_call')
            args = 'number=' + extension
            response_json = self._requests('GET', endpoint, args=args, token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_query_call_by_extension_not_possible',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        data = response_json.get('data', [])
        if not data:
            return {}
        call_id = data[0].get('call_id', None)
        members = data[0].get('members', {})
        call = {}
        callee = ''
        contact = None
        for member in members:
            if member.get('extension', None):
                continue
            if member.get('inbound', None):
                call = member['inbound']
                callee = call.get('from', '')
            elif member.get('outbound', None):
                call = member['outbound']
                callee = call.get('to', '')
            contacts = Contact.search([
                    ('yeastar_pbx', '=', self),
                    ('number', '=', callee),
                    ], limit=1)
            contact, = contacts if contacts else [None]
        return {
            'call_id': call_id,
            'callee': callee,
            'yeastar_contact': contact,
            'trunk_name': call.get('trunk_name' ''),
            'channel_id': call.get('channel_id' ''),
            }

    def get_records_list(self, page=None, page_size=None, sort_by=None,
            order_by=None):
        '''
        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/query-recording-list.html
        return a dictioanry with the total records and a list of diccitionary
        with the records information. The subdict is structured as:
            {
                id: The unique ID of the call recording.
                time: The time the call was made or received.
                uid: The unique ID of the CDR for which the recording is
                    proceeded.
                call_from: caller
                call_to: callee
                duration: call duration
                size: The size of the call recording file. (Unit: Byte)
                call_type: Communication type (Inbound, Outbound, Internal)
                file: The name of the call recording file.
                call_from_number
                call_from_name
                call_to_number
                call_to_name
                }
        ''' 
        token = self.get_token()
        response_json = None
        if token:
            endpoint = self.get_endpoint('get_records_list')
            args = {}
            if page:
                args['page'] = page
            if page_size:
                args['page_size'] = page_size
            if sort_by:
                args['sort_by'] = sort_by
            if order_by:
                args['order_by'] = order_by
            response_json = self._requests('GET', endpoint, args=args,
                token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_download_record_call_not_possible',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        return {
            'total_number': response_json.get('total_number', None),
            'data': response_json.get('data', None),
            }

    def download_record(self, id=None, file=None):
        '''
        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/download-a-recording-file.html
        It's only necessary one of the bothe vraibles, or 'id' or 'file'.
        Return a dictionary with:
        {
            file_name
            download_resource_url
            }
        ''' 
        if not id and not file:
            return None
        token = self.get_token()
        response_json = None
        if token:
            endpoint = self.get_endpoint('download_record_call')
            if id:
                args = 'id=' + str(id) if isinstance(id, int) else id
            else:
                args = 'file=' + file
            response_json = self._requests('GET', endpoint, args=args,
                token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_download_record_call_not_possible',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        return {
            'file_name': response_json.get('file', None),
            'download_resource_url': response_json.get('download_resource_url',
                None),
            }

    def get_cdr_details(self, start_time, end_time, call_from=None,
            call_to=None, status=None):
        '''
        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/search-specific-cdr.html
        start_time and end_time must be datetime fornat or None.
        Return a dictionary with:
        {
            total_number: The total number of the searched CDR.
            data: A list of dicts with the detailed information of the CDR
            }
        ''' 
        args = {}
        if start_time and isinstance(start_time, datetime):
            args['start_time'] = start_time.strftime(self.time_format)
        if end_time and isinstance(end_time, datetime):
            args['end_time'] = end_time.strftime(self.time_format)
        if call_from:
            args['call_from'] = call_from
        if call_to:
            args['call_to'] = call_to
        status_valid_values = [x[0].upper() for x in STATUS_CALL]
        status = status.upper() if status else status
        if status and status in status_valid_values:
            args['status'] = status
        token = self.get_token()
        response_json = None
        if token:
            endpoint = self.get_endpoint('cdr_search')
            response_json = self._requests('GET', endpoint, args=args,
                token=token)
        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_download_record_call_not_possible',
                    pbx=self.rec_name,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        return {
            'total_number': response_json.get('total_number', None),
            'data': response_json.get('data', None),
            }

    @classmethod
    @ModelView.button
    def get_pbx_information(cls, pbxs):
        for pbx in pbxs:
            pbx.serial_number = pbx.query_pbx_information()
        cls.save(pbxs)

    @classmethod
    @ModelView.button
    def delete_token(cls, pbxs):
        for pbx in pbxs:
            pbx.revoke_token(pbx.token)
            pbx.token = None
            pbx.token_expire = None
            pbx.refresh_token = None
            pbx.refresh_token_expire = None
        cls.save(pbxs)

    @classmethod
    @ModelView.button
    def sync_contacts(cls, pbxs):
        pool = Pool()
        Contact = pool.get('yeastar.contact')

        for pbx in pbxs:
            Contact.sync_contact(pbx.contacts)

    @classmethod
    @ModelView.button
    def load_contacts(cls, pbxs):
        '''
        Load or update the Contacts mechanism model to the Yeastart PBX model.
        To synchronize after.
        '''
        pool = Pool()
        Party = pool.get('party.party')
        Mechanism = pool.get('party.contact_mechanism')
        Contact = pool.get('yeastar.contact')

        # TODO: Control the possible multi PBX for mutli company
        # Ass the moment is possible to have one PBX for company is not
        # necessary to control for each PBX wich contacts set.
        domain = [
            ('party.active', '=', True),
            ('type', 'in', ('phone', 'mobile'))
            ]
        if hasattr(Party, 'current_company'):
            domain.append(('party.current_company', '=', True))
        all_mechanims = Mechanism.search(domain)
        mechanisms = dict(((x, Contact.valid_number(x.value))
                for x in all_mechanims if Contact.valid_number(x.value)))
        yeastar_contacts = dict(((x.contact_mechanism, x)
                for x in Contact.search([('sync', '=', True)])))
        contact_to_update = []
        contact_to_save = []
        for mechanism, value in mechanisms.items():
            first_name = mechanism.party.name
            if mechanism.name:
                first_name = mechanism.name
            elif mechanism.address and mechanism.address.party_name:
                first_name = mechanism.address.party_name
            first_name = first_name[:60]
            add_contact = False
            if mechanism in yeastar_contacts:
                contact = yeastar_contacts[mechanism]
                if value != contact.number:
                    contact.number = value
                    add_contact = True
                if first_name != contact.first_name:
                    contact.first_name = first_name
                    add_contact = True
                if add_contact:
                    contact_to_update.append(contact)
            else:
                # Create Contact and Contact Number
                contact = Contact()
                contact.sync = True
                contact.contact_mechanism = mechanism
                contact.first_name = first_name
                contact.num_type = 'business_number'
                contact.number = value
                contact_to_save.append(contact)
        if contact_to_save:
            for pbx in pbxs:
                for contact in contact_to_save:
                    contact.yeastar_pbx = pbx
        if contact_to_update:
            contact_to_save.extend(contact_to_update)
        if contact_to_save:
            Contact.save(contact_to_save)


class YeastarPhonebook(ModelSQL, ModelView):
    'Yeastar Phonebook'
    __name__ = 'yeastar.phonebook'

    name = fields.Char('Name', required=True)
    yeastar_pbx = fields.Many2One('yeastar.pbx', 'Yeastar', required=True)
    yeastar_phonebook_id = fields.Integer('Yeastar Phonebook ID',
        readonly=True)
    member_select = fields.Selection([
            ('sel_all', "Select all company contacts"),
            ('sel_specific', "Select specific company contacts"),
            ], "Member Selection Method")
    contacts = fields.Many2Many('yeastar.phonebook-yeastar.contact',
        'phonebook', 'contact', 'Contacts',
        domain=[
            ('yeastar_contact_id', '!=', None),
            ('yeastar_pbx', '=', Eval('yeastar_pbx', -1)),
            ],
        states={
            'invisible': Eval('member_select') != 'sel_specific'
            })

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'sync_phonebook': {},
                'delete_phonebook': {
                    'invisible': ~Eval('yeastar_phonebook_id', False),
                    'depends': ['yeastar_phonebook_id'],
                    },
                })

    @staticmethod
    def default_member_select():
        return 'sel_all'

    def get_phonebook_contacts(self, yeastar_phonebook_id, token=None):
        '''
        Query the detailed information of a phonebook.
        '''
        if (not yeastar_phonebook_id
                or not isinstance(yeastar_phonebook_id, int)):
            return None
        pbx = self.yeastar_pbx
        endpoint = pbx.get_endpoint('query_phonebook')
        response_json = None
        if not token:
            token = pbx.get_token()
        if token:
            args = "id=%s" % str(yeastar_phonebook_id)
            response_json = pbx._requests('GET', endpoint, args=args,
                token=token)

        if not response_json or response_json.get('errcode', 1) != 0:
            raise UserError(
                gettext('yeastar_api.msg_query_phonebook_fail',
                    pbx=pbx.rec_name,
                    phonebook=self.nme,
                    errmsg=(response_json.get('errmsg', '')
                        if response_json else '')))
        return response_json.get('data', {}).get('contacts_list', [])

    @classmethod
    @ModelView.button
    def sync_phonebook(cls, phonebooks):
        '''
        Add or edit the Phonebooks to Yeastar P-Serie Cloud Edition PBX
        '''
        to_save = []
        for phonebook in phonebooks:
            data = {
                "name": phonebook.name,
                "member_select": phonebook.member_select,
                }
            pbx = phonebook.yeastar_pbx
            token = pbx.get_token()
            response_json = None
            if token:
                phonebook_contacts = [x.yeastar_contact_id
                    for x in phonebook.contacts]
                if phonebook.yeastar_phonebook_id:
                    add_contacts = None
                    del_contacts = None
                    if phonebook.member_select == 'sel_specific':
                        actual_contacts = phonebook.get_phonebook_contacts(
                            phonebook.yeastar_phonebook_id, token=token)
                        actual_contacts = [x['id'] for x in actual_contacts]
                        add_contacts = list(
                            set(phonebook_contacts) - set(actual_contacts))
                        if actual_contacts:
                            del_contacts = list(
                                set(actual_contacts) - set(phonebook_contacts))
                    data['id'] = phonebook.yeastar_phonebook_id
                    if add_contacts:
                        data['add_contacts_ids_list'] = add_contacts
                    if del_contacts:
                        data['del_contacts_ids_list'] = del_contacts
                    endpoint = pbx.get_endpoint('update_phonebook')
                else:
                    if (phonebook.member_select == 'sel_specific'
                            and phonebook_contacts):
                        data['add_contacts_ids_list'] = phonebook_contacts
                    endpoint = pbx.get_endpoint('add_phonebook')
                response_json = pbx._requests('POST', endpoint,
                    data=json.dumps(data), headers=HEADERS, token=token)

            if not response_json or response_json.get('errcode', 1) != 0:
                raise UserError(
                    gettext('yeastar_api.msg_sync_phonebook_fail',
                        pbx=phonebook.yeastar_pbx.rec_name,
                        phonebook=phonebook.name,
                        errmsg=(response_json.get('errmsg', '')
                            if response_json else '')))
            if not phonebook.yeastar_phonebook_id:
                phonebook.yeastar_phonebook_id = response_json.get('id', None)
                to_save.append(phonebook)
        if to_save:
            cls.save(to_save)

    @classmethod
    @ModelView.button
    def delete_phonebook(cls, phonebooks):
        for phonebook in phonebooks:
            if not phonebook.yeastar_phonebook_id:
                continue
            pbx = phonebook.yeastar_pbx
            endpoint = pbx.get_endpoint('delete_phonebook')
            args = 'id=%s' % phonebook.yeastar_phonebook_id
            token = pbx.get_token()
            response_json = None
            if token:
                response_json = (pbx._requests('GET', endpoint, args=args,
                        token=token) if token else None)
            if not response_json or response_json.get('errcode', 1) != 0:
                raise UserError(
                    gettext('yeastar_api.msg_delete_phonebook_fail',
                        pbx=phonebook.yeastar_pbx.rec_name,
                        phonebook=phonebook.name,
                        errmsg=(response_json.get('errmsg', '')
                            if response_json else '')))
            phonebook.yeastar_phonebook_id = None
        if phonebooks:
            cls.save(phonebooks)


class YeastarContact(ModelSQL, ModelView):
    'Yeastar Contact'
    __name__ = 'yeastar.contact'
    _rec_name = 'first_name'

    sync = fields.Boolean('Synch with Yeatar PBXs')
    yeastar_pbx = fields.Many2One('yeastar.pbx', 'Yeastar',
        states={
            'readonly': ~Bool(Eval('sync')),
            'required': Bool(Eval('sync')),
            })
    yeastar_contact_id = fields.Integer('Yeastar Contact ID', readonly=True)
    contact_mechanism = fields.Many2One('party.contact_mechanism', 'Contact',
        required=True, domain=[
            ('type', 'in', ('phone', 'mobile')),
            ])
    first_name = fields.Char('First Name', size=60, required=True)
    company = fields.Function(fields.Char('Yeastar Company'), 'get_company')
    num_type = fields.Selection('get_num_types', 'Number Type')
    number = fields.Char('Number', required=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('action_uniq', Unique(t, t.contact_mechanism),
                'There is already one Yeastar Contact with this contact mechanism.'),
            ]
        cls._buttons.update({
                'sync_contact': {},
                'delete_contact': {
                    'invisible': ~Eval('yeastar_contact_id', False),
                    'depends': ['yeastar_contact_id'],
                    },
                })

    @classmethod
    def get_num_types(cls):
        return [
            ('business_number', 'Bussines Number'),
            ('business_number2', 'Bussines Number 2'),
            ('mobile_number', 'Mobile Number'),
            ('mobile_number2', 'Mobile Number 2'),
            ('home_number', 'Home Number'),
            ('home_number2', 'Home Number 2'),
            ('other_number', 'Other Number')
            ]

    def get_company(self, name=None):
        return (self.contact_mechanism.party.name[:120]
            if self.contact_mechanism and self.contact_mechanism.party
            and self.contact_mechanism.party.name else '')

    @staticmethod
    def default_num_type():
        return 'business_number'

    @staticmethod
    def default_sync():
        return True

    @fields.depends('contact_mechanism', 'first_name', 'number')
    def on_change_contact_mechanism(self):
        mechanism = self.contact_mechanism
        if mechanism:
            first_name = mechanism.party.name
            if mechanism.name:
                first_name = mechanism.name
            elif mechanism.address and mechanism.address.party_name:
                first_name = mechanism.address.party_name
            if not self.first_name:
                self.first_name = first_name[:60]
            if not self.number:
                self.number = self.__class__.valid_number(mechanism.value)

    @classmethod
    def valid_number(cls, phone_number):
        phone_number = (phone_number.replace('+', '00').replace(
                ' ', '').replace('-', '').replace('_', '')
            if phone_number else None)
        return (phone_number if phone_number and phone_number.isdigit()
            else None)

    @classmethod
    @ModelView.button
    def sync_contact(cls, contacts):
        '''
        Add or edit the Contacts to Yeastar P-Serie Cloud Edition PBX
        '''
        for contact in contacts:
            if not contact.sync:
                continue
            data = {
                'first_name': contact.first_name, 
                'company': contact.company,
                'number_list': [{
                        'num_type': contact.num_type,
                        'number': contact.number,
                        }],
                }
            pbx = contact.yeastar_pbx
            token = pbx.get_token()
            response_json = None
            if token:
                if contact.yeastar_contact_id:
                    data['id'] = contact.yeastar_contact_id
                    endpoint = pbx.get_endpoint('update_contact')
                else:
                    endpoint = pbx.get_endpoint('add_contact')
                response_json = pbx._requests('POST', endpoint,
                    data=json.dumps(data), headers=HEADERS, token=token)
            if not response_json or response_json.get('errcode', 1) != 0:
                raise UserError(
                    gettext('yeastar_api.msg_sync_contact_fail',
                        pbx=contact.yeastar_pbx.rec_name,
                        contact=contact.contact_mechanism.party.name,
                        errmsg=(response_json.get('errmsg', '')
                            if response_json else '')))
            if not contact.yeastar_contact_id:
                contact.yeastar_contact_id = response_json.get('id', None)
                # As the Yeastar API only allow to upload the contacts one by
                # one, we need to commit the yeatar_contact_id each time the
                # contat is upload becasue if ther is a fail, not lost the
                # contact ID upload and not duplicate the next time we upload
                # the contacts.
                contact.save()
                Transaction().commit()

    @classmethod
    @ModelView.button
    def delete_contact(cls, contacts):
        for contact in contacts:
            if not contact.yeastar_contact_id:
                continue
            pbx = contact.yeastar_pbx
            endpoint = pbx.get_endpoint('delete_contact')
            token = pbx.get_token()
            response_json = None
            if token:
                args = 'id=%s' % contact.yeastar_contact_id
                response_json = pbx._requests('GET', endpoint, args=args,
                    token=token)
            if not response_json or response_json.get('errcode', 1) != 0:
                raise UserError(
                    gettext('yeastar_api.msg_delete_contact_fail',
                        pbx=contact.yeastar_pbx.rec_name,
                        contact=contact.contact_mechanism.party.name,
                        errmsg=(response_json.get('errmsg', '')
                            if response_json else '')))
            contact.yeastar_contact_id = None
        if contacts:
            cls.save(contacts)


class YeastarPhonebookContact(ModelSQL):
    "Yeastar Phonebook Contact"
    __name__ = 'yeastar.phonebook-yeastar.contact'

    phonebook = fields.Many2One('yeastar.phonebook', "Phonebook",
        ondelete='CASCADE', required=True)
    contact = fields.Many2One('yeastar.contact', "Contact", ondelete='CASCADE')


class CreateFromProgressCallStart(ModelView):
    'Create From Progress Call Start'
    __name__ = 'yeastar.create.from_progress_call.start'

    call_id = fields.Char('Call ID', readonly=True,
        states={
            'invisible': True,
            })
    extension = fields.Char('Extension', readonly=True)
    name = fields.Char('Name', readonly=True)
    party = fields.Many2One('party.party', 'Party')
    party_call = fields.Many2One('party.party', 'Party On The Call',
        readonly=True)
    employee = fields.Many2One('company.employee', 'Employee', readonly=True,
        states={
            'invisible': True,
            })


class CreateFromProgressCall(Wizard):
    'Create From Progress Call'
    __name__ = 'yeastar.create.from_progress_call'

    start = StateView('yeastar.create.from_progress_call.start',
        'yeastar_api.create_from_progress_call_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Create Activity', 'create_activity', 'tryton-ok',
                default=True),
            ])
    create_activity = StateAction('activity.act_activity_activity')

    def default_start(self, fields):
        pool = Pool()
        User = pool.get('res.user')

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
        call = pbx.query_call_by_extension(str(extension))

        if not call:
            raise UserError(
                gettext('yeastar_api.msg_not_call_in_progress',
                    employee=employee.rec_name,
                    extension=extension,
                    pbx=pbx.rec_name))
        callee = call.get('callee', '')
        contact = call.get('yeastar_contact', None)
        party = None
        if contact:
            party = contact.contact_mechanism.party
        party_id = party.id if party else None
        values = {
            'extension': str(extension),
            'name': contact.first_name if contact else callee,
            'party': party_id,
            'party_call': party_id,
            'call_id': call.get('call_id', ''),
            'employee': employee.id,
            }
        return values

    def _create_activity(self):
        pool = Pool()
        Activity = pool.get('activity.activity')
        Configuration = pool.get('activity.configuration')
        Date = pool.get('ir.date')

        config = Configuration(1)

        activity = Activity()
        activity.activity_type = config.default_yeastar_activity_type
        activity.date = Date.today()
        activity.employee = self.start.employee
        if self.start.party:
            activity.party = self.start.party
        if self.start.party_call:
            activity.contacts = [self.start.party_call]
        activity.call_id = self.start.call_id
        activity.save()
        return activity

    def do_create_activity(self, action):
        activity = self._create_activity()
        return action, {'res_id': activity.id}


class YeastarCDR(ModelSQL, ModelView):
    'Yeastar CDR'
    __name__ = 'yeastar.cdr'

    yeastar_pbx = fields.Many2One('yeastar.pbx', 'Yeastar')
    uid = fields.Char('CDR ID')
    time = fields.DateTime('Call Time')
    call_from = fields.Char('Caller')
    call_to = fields.Char('Callee')
    ring_duration = fields.TimeDelta('Ring Duration')
    talk_duration = fields.TimeDelta('Talk Duration')
    duration = fields.TimeDelta('Total Duration')
    disposition = fields.Selection('get_dispostion', 'Call Status')
    call_type = fields.Selection('get_call_type', 'Call Type')
    did_number = fields.Char('DID Number')
    dod_number = fields.Char('DOD Number')
    record_file = fields.Char('Record File')
    reason = fields.Char('Reason')

    @classmethod
    def get_dispostion(cls):
        return STATUS_CALL

    @classmethod
    def get_call_type(cls):
        return [
            ('inbound', 'Inbound'),
            ('outbound', 'Outbound'),
            ('internal', 'Internal'),
            ]

    @classmethod
    def get_yeastar_cdr_details(cls, pbx):
        cdrs = cls.search([
                ('yeastar_pbx', '=', pbx),
                ], order=[('time', 'DESC')], limit=1)
        if cdrs:
            cdr, = cdrs
            start_time = cdr.time
        else:
            start_time = None
        end_time = datetime.now()
        cdr_details = pbx.get_cdr_details(start_time, end_time)
        if not cdr_details:
            return None
        details = cdr_details.get('data', None)
        if not details:
            return None
        to_create = []
        for detail in details:
            time = detail.get('time', None)
            ring_duration = detail.get('ring_duration', None)
            talk_duration = detail.get('talk_duration', None)
            duration = detail.get('duration', None)
            to_create.append({
                    'yeastar_pbx': pbx.id,
                    'uid': detail.get('uid', None),
                    'time': (datetime.strptime(time, pbx.time_format)
                        if time else None),
                    'call_from': detail.get('call_from', None),
                    'call_to': detail.get('call_to', None),
                    'ring_duration': (int(ring_duration)
                        if ring_duration else None),
                    'talk_duration': (int(talk_duration)
                        if talk_duration else None),
                    'duration': int(duration) if duration else None,
                    'disposition': detail.get('disposition', None).replace(
                        ' ', '').lower(),
                    'call_type': detail.get('call_type', None).lower(),
                    'did_number': detail.get('did_number', None),
                    'dod_number': detail.get('dod_number', None),
                    'record_file': detail.get('record_file', None),
                    'reason': detail.get('reason', None),
                    })
        if to_create:
            cls.create(to_create)

    @classmethod
    def get_calls_details(cls):
        pool = Pool()
        Pbx = pool.get('yeastar.pbx')

        company_id = Transaction().context.get('company')
        if not company_id:
            return
        pbxs = Pbx.search([
                ('company.id', '=', company_id),
                ])
        for pbx in pbxs:
            cls.get_yeastar_cdr_details(pbx)


class YeastarGetCallsDetails(Wizard):
    'Yeastar Get Calls Details'
    __name__ = 'yeastar.cdr.get_calls_details'
    start = StateTransition()

    def transition_start(self):
        pool = Pool()
        Cdr = pool.get('yeastar.cdr')

        Cdr.get_calls_details()
        return 'end'


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
            ('yeastar.cdr|get_calls_details',
                "Get Yeastar CDR Details"),
            ])
