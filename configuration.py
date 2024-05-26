#This file is part asterisk module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from trytond.model import fields, ModelSQL
from trytond.pool import Pool, PoolMeta
from trytond.modules.company.model import CompanyValueMixin


class ActivityConfiguration(metaclass=PoolMeta):
    'Activity Configuration'
    __name__ = 'activity.configuration'

    default_yeastar_activity_type = fields.MultiValue(
        fields.Many2One('activity.type', 'Yeastar Type', required=True))

    @classmethod
    def multivalue_model(cls, field):
        pool = Pool()
        if field == 'default_yeastar_activity_type':
            return pool.get('activity.configuration.yeastar')
        return super().multivalue_model(field)


class ActivityConfigurationYeastar(ModelSQL, CompanyValueMixin):
    'Yeastar Activity Configuration'
    __name__ = 'activity.configuration.yeastar'

    default_yeastar_activity_type = fields.Many2One('activity.type', 'Yeastar Type',
        required=True)
