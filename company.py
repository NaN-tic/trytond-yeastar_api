#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from trytond.model import fields
from trytond.pool import PoolMeta
from trytond.pyson import Eval


class Employee(metaclass=PoolMeta):
    'Employee'
    __name__ = 'company.employee'

    yeastar_pbx = fields.Many2One('yeastar.pbx', 'Yeastar PBX',
        domain=[
            ('company', '=', Eval('company')),
            ])
    yeastar_extension = fields.Integer('Yeastar Extension')

    @classmethod
    def copy(cls, employees, default=None):
        default = default.copy() if default is not None else {}
        default.setdefault('yeastar_pbx', None)
        default.setdefault('yeastar_extension', None)
        return super().copy(employees, default=default)
