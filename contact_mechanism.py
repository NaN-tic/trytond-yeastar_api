# This file is part of party_asterisk module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.model import ModelView
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.transaction import Transaction
from trytond.i18n import gettext
from trytond.exceptions import UserError


class ContactMechanism(metaclass=PoolMeta):
    __name__ = "party.contact_mechanism"

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'dial': {
                    'invisible': ~Eval('type').in_(['phone', 'mobile']),
                    },
                })

    @classmethod
    @ModelView.button
    def dial(cls, mechanisms):
        '''
        Function called by the button 'Dial' next to the 'phone' field
        in the contact_mechanism view
        '''
        pool = Pool()
        YeastarContact = pool.get('yeastar.contact')
        User = pool.get('res.user')

        user = User(Transaction().user)
        if not user:
            raise UserError(gettext('yeastar_api.msg_not_user_defined'))
        employee = user.employee
        if not employee:
            raise UserError(gettext('yeastar_api.msg_not_employee_defined'))
        if not employee.yeastar_extension:
            raise UserError(
                gettext('yeastar_api.msg_not_extension_pbx_employee',
                    employee=employee.rec_name))
        if not employee.yeastar_pbx:
            raise UserError(
                gettext('yeastar_api.msg_not_pbx_employee',
                    employee=employee.rec_name))

        for mechanism in mechanisms:
            callee_number = mechanism.value_compact or mechanism.value
            callee_number = YeastarContact.valid_number(callee_number)
            caller_number = str(employee.yeastar_extension)
            if caller_number and callee_number:
                employee.yeastar_pbx.make_call(caller_number, callee_number)
