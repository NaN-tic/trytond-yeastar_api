import json
from datetime import datetime, timedelta

import pytz
from dateutil.relativedelta import relativedelta
from sql import Null

from trytond.exceptions import UserError
from trytond.model import ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction, TransactionError


class WebhookCall(ModelSQL, ModelView):
    'Yeastar Processed Call'
    __name__ = 'yeastar.webhook.call'
    _rec_name = 'call_id'

    pbx = fields.Many2One('yeastar.pbx', 'PBX', required=True,
        ondelete='RESTRICT', readonly=True)
    call_id = fields.Char('Call ID', required=True, readonly=True)
    employee = fields.Many2One('company.employee', 'Employee', required=True,
        ondelete='RESTRICT', readonly=True)
    activity = fields.Many2One('activity.activity', 'Activity',
        ondelete='SET NULL', readonly=True)
    duration = fields.TimeDelta('Accounted Duration', readonly=True)
    legs = fields.One2Many('yeastar.webhook.leg', 'call', 'Call Legs',
        readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('call_employee_unique',
                Unique(table, table.pbx, table.call_id, table.employee),
                'yeastar_api.msg_webhook_call_unique'),
            ]


class WebhookLeg(ModelSQL, ModelView):
    'Yeastar External Call Leg'
    __name__ = 'yeastar.webhook.leg'
    _rec_name = 'note_id'

    call = fields.Many2One('yeastar.webhook.call', 'Call', required=True,
        ondelete='RESTRICT', readonly=True)
    pbx = fields.Many2One('yeastar.pbx', 'PBX', required=True,
        ondelete='RESTRICT', readonly=True)
    call_id = fields.Char('Call ID', required=True, readonly=True)
    uid = fields.Char('CDR ID', required=True, readonly=True)
    note_id = fields.Char('Call Note ID', required=True, readonly=True)
    extension = fields.Char('Extension', required=True, readonly=True)
    external_number = fields.Char('External Number', required=True,
        readonly=True)
    direction = fields.Selection([
            ('Inbound', 'Inbound'), ('Outbound', 'Outbound'),
            ], 'Direction', required=True, readonly=True)
    start = fields.DateTime('Start (UTC)', required=True, readonly=True)
    talk_duration = fields.TimeDelta('CDR Talk Duration', readonly=True)
    hold_duration = fields.TimeDelta('Excluded Hold Duration', readonly=True)
    duration = fields.TimeDelta('External Conversation Duration', readonly=True)
    recording = fields.Char('Recording File', readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('leg_unique', Unique(table, table.pbx, table.call_id, table.note_id),
                'yeastar_api.msg_webhook_leg_unique'),
            ]


class WebhookEvent(metaclass=PoolMeta):
    __name__ = 'yeastar.webhook.event'

    @classmethod
    def process_pending(cls):
        """Process each call atomically without blocking other calls on errors."""
        PBX = Pool().get('yeastar.pbx')
        company = Transaction().context.get('company')
        domain = [('webhook_mode', '=', 'activities')]
        if company:
            domain.append(('company', '=', company))
        cutoff = datetime.now() - timedelta(minutes=1)
        for pbx in PBX.search(domain):
            call_ids = dict.fromkeys(e.call_id for e in cls.search([
                        ('pbx', '=', pbx.id), ('state', '=', 'captured'),
                        ('create_date', '<', cutoff),
                        ], order=[('id', 'ASC')]))
            for call_id in call_ids:
                extras = {}
                while True:
                    try:
                        with Transaction().new_transaction(**extras):
                            cls._process_call(pbx.id, call_id, cutoff)
                        break
                    except TransactionError as exception:
                        exception.fix(extras)
                    except (UserError, ValueError, TypeError, KeyError,
                            pytz.InvalidTimeError) as error:
                        # The failed call transaction has already rolled back.
                        with Transaction().new_transaction():
                            events = cls.search([
                                    ('pbx', '=', pbx.id),
                                    ('call_id', '=', call_id),
                                    ('state', '=', 'captured'),
                                    ])
                            cls.write(events, {
                                    'state': 'error',
                                    'processing_message': str(error),
                                    })
                        break

    @classmethod
    def _process_call(cls, pbx_id, call_id, cutoff):
        pool = Pool()
        PBX = pool.get('yeastar.pbx')
        pbx = PBX(pbx_id)
        PBX.lock([pbx])
        if pbx.webhook_mode != 'activities':
            return
        events = cls.search([
                ('pbx', '=', pbx.id), ('call_id', '=', call_id),
                ], order=[('create_date', 'ASC'), ('id', 'ASC')])
        if not events or events[len(events) - 1].create_date >= cutoff:
            return
        messages = []
        for event in events:
            message = json.loads(event.payload).get('msg', {})
            if isinstance(message, str):
                message = json.loads(message)
            if not isinstance(message, dict):
                raise ValueError('Expected an object in the webhook message.')
            messages.append((event, message))

        cdrs = [(e, m) for e, m in messages if e.event_type == '30012']
        states = [(e, m) for e, m in messages if e.event_type == '30011']
        closed = bool(cdrs) and all(
            m.get('type') == 'Internal' for _, m in cdrs)
        if states:
            members = states[-1][1].get('members', [])
            external = [v for member in members
                for k, v in member.items() if k in {'inbound', 'outbound'}]
            if external:
                closed = all(v.get('member_status') == 'BYE' for v in external)

        with Transaction().set_context(
                company=pbx.company.id, _check_access=False):
            for event, message in messages:
                if event.state != 'captured':
                    continue
                if event.event_type == '30012':
                    reason = cls._process_cdr(pbx, event, message, states)
                    cls.write([event], {
                            'state': 'discarded' if reason else 'processed',
                            'processing_message': reason,
                            })
                elif event.event_type in {'30011', '30013', '30014'}:
                    # Keep live-call evidence until the call has ended.
                    if closed:
                        cls.write([event], {'state': 'processed',
                                'processing_message': None})
                else:
                    cls.write([event], {'state': 'discarded',
                            'processing_message': 'Not a call event.'})

    @classmethod
    def _process_cdr(cls, pbx, event, message, states):
        pool = Pool()
        Employee = pool.get('company.employee')
        Date = pool.get('ir.date')
        Activity = pool.get('activity.activity')
        Configuration = pool.get('activity.configuration')
        Call = pool.get('yeastar.webhook.call')
        Leg = pool.get('yeastar.webhook.leg')

        direction = message.get('type')
        if direction == 'Internal':
            return 'Internal call: no activity, duration or recording.'
        if direction not in {'Inbound', 'Outbound'}:
            raise ValueError('Unsupported CDR direction.')
        if message.get('status') != 'ANSWERED':
            return 'Unanswered external call.'
        extension = message.get(
            'call_to' if direction == 'Inbound' else 'call_from')
        number = message.get(
            'call_from' if direction == 'Inbound' else 'call_to')
        if not isinstance(extension, str) or not isinstance(number, str):
            raise ValueError('Missing CDR caller or callee.')
        if not extension.isdecimal():
            return 'No configured employee extension.'
        with Transaction().set_context(active_test=False):
            employees = Employee.search([
                    ('company', '=', pbx.company.id),
                    ('yeastar_pbx', '=', pbx.id),
                    ('yeastar_extension', '=', int(extension)),
                    ['OR', ('end_date', '=', None),
                        ('end_date', '>=', Date.today())],
                    ])
        if not employees:
            return 'No current employee assigned to this extension.'
        if len(employees) != 1:
            raise ValueError(f'Multiple employees for extension {extension}.')
        employee, = employees
        if not event.call_id or message.get('call_id') != event.call_id:
            raise ValueError('Missing or inconsistent call ID.')
        uid, note_id = message.get('uid'), message.get('call_note_id')
        if not isinstance(uid, str) or not uid or not isinstance(
                note_id, str) or not note_id:
            raise ValueError('Missing CDR ID or call note ID.')
        # Check before interpreting timestamps or needing retained state events.
        # These identities outlive the raw webhooks and employee reassignments.
        if Leg.search([
                    ('pbx', '=', pbx.id), ('call_id', '=', event.call_id),
                    ('note_id', '=', note_id),
                    ], limit=1):
            return None
        if not pbx.webhook_timezone:
            raise ValueError('Configure the PBX timezone before processing.')
        local_start = None
        for format_ in ('%Y-%m-%d %H:%M:%S', pbx.time_format):
            try:
                local_start = datetime.strptime(message['time_start'], format_)
                break
            except ValueError:
                continue
        if local_start is None:
            raise ValueError('Invalid CDR start time.')
        start = pytz.timezone(pbx.webhook_timezone).localize(
            local_start, is_dst=None).astimezone(pytz.utc).replace(tzinfo=None)
        talk, total = message.get('talk_duration'), message.get('call_duration')
        if (type(talk) is not int or type(total) is not int
                or not 0 <= talk <= total):
            raise ValueError('Invalid CDR duration.')
        if not talk:
            return 'No external conversation time.'
        end = start + timedelta(seconds=total)
        answered = end - timedelta(seconds=talk)
        hold = cls._hold_duration(states, extension, answered, end)
        duration = timedelta(seconds=max(0, talk - hold))
        activity_type = Configuration(1).default_yeastar_activity_type
        if not activity_type:
            raise ValueError('Configure the default Yeastar activity type.')
        calls = Call.search([
                ('pbx', '=', pbx.id), ('call_id', '=', event.call_id),
                ('employee', '=', employee.id),
                ])
        if calls:
            call, = calls
            activity = call.activity
            if not activity:
                raise ValueError('The activity for this call has been deleted.')
            old_duration = call.duration or timedelta()
        else:
            activities = Activity.search([
                    ('company', '=', pbx.company.id),
                    ('employee', '=', employee.id),
                    ('call_id', '=', event.call_id),
                    ])
            if len(activities) > 1:
                raise ValueError('Multiple existing activities for this call.')
            if activities:
                activity, = activities
            else:
                activity, = Activity.create([{
                            'company': pbx.company.id,
                            'employee': employee.id,
                            'activity_type': activity_type.id,
                            'call_id': event.call_id,
                            'dtstart': answered,
                            'duration': timedelta(),
                            'state': 'done',
                            'subject': f'{direction} call: {number}',
                            'call_generated_subject':
                                f'{direction} call: {number}',
                            }])
            call, = Call.create([{
                        'pbx': pbx.id, 'call_id': event.call_id,
                        'employee': employee.id, 'activity': activity.id,
                        'duration': timedelta(),
                        }])
            old_duration = None
        activity.identify_call_party(number, pbx)
        Leg.create([{
                    'call': call.id, 'pbx': pbx.id, 'call_id': event.call_id,
                    'uid': uid, 'note_id': note_id, 'extension': extension,
                    'external_number': number, 'direction': direction,
                    'start': start, 'talk_duration': timedelta(seconds=talk),
                    'hold_duration': timedelta(seconds=hold),
                    'duration': duration, 'recording': message.get('recording'),
                    }])
        accounted = (old_duration or timedelta()) + duration
        # Reconcile a pre-existing manual activity once. Later deliveries add
        # only new legs, preserving subsequent manual duration adjustments.
        updated = (accounted if old_duration is None else
            (activity.duration or timedelta()) + duration)
        Activity.write([activity], {'duration': updated})
        Call.write([call], {'duration': accounted})

    @classmethod
    def _hold_duration(cls, states, extension, start, end):
        """Subtract the union of observed holds, including internal consultation.

        Yeastar state events have no occurrence timestamp. Reception times are
        used, clipped to the CDR conversation interval and rounded to seconds.
        """
        held = {}
        intervals = []
        for event, message in states:
            for member in message.get('members', []):
                member = member.get('extension', {})
                if member.get('number') != extension:
                    continue
                channel = member.get('channel_id')
                status = member.get('member_status')
                if status == 'HOLD':
                    held.setdefault(channel, event.create_date)
                elif channel in held and status in {'ANSWER', 'ANSWERED', 'BYE'}:
                    intervals.append((held.pop(channel), event.create_date))
        intervals.extend((value, end) for value in held.values())
        merged = []
        for first, last in sorted(intervals):
            first, last = max(first, start), min(last, end)
            if first >= last:
                continue
            if merged and first <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        return round(sum((last - first).total_seconds() for first, last in merged))

    @classmethod
    def clean(cls):
        PBX = Pool().get('yeastar.pbx')
        table = cls.__table__()
        other = cls.__table__()
        cursor = Transaction().connection.cursor()
        domain = [('webhook_retention_months', '>', 0)]
        company = Transaction().context.get('company')
        if company:
            domain.append(('company', '=', company))
        for pbx in PBX.search(domain):
            PBX.lock([pbx])
            cutoff = datetime.now() - relativedelta(
                months=pbx.webhook_retention_months)
            terminal = ['processed', 'discarded']
            protected = other.select(other.call_id,
                where=(other.pbx == pbx.id) & (other.call_id != Null)
                & (~other.state.in_(terminal) | (other.create_date >= cutoff)))
            while True:
                cursor.execute(*table.select(table.id,
                        where=(table.pbx == pbx.id)
                        & table.state.in_(terminal)
                        & (table.create_date < cutoff)
                        & ((table.call_id == Null)
                            | ~table.call_id.in_(protected)),
                        limit=1000))
                ids = [row[0] for row in cursor]
                if not ids:
                    break
                cls.delete(cls.browse(ids))


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
                ('yeastar.webhook.event|process_pending',
                    'Process Yeastar Webhooks'),
                ('yeastar.webhook.event|clean', 'Clean Yeastar Webhooks'),
                ])
