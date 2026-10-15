"""Conservative phone matching shared by webhook and contact resolution."""
import re

try:
    import phonenumbers
except ImportError:
    phonenumbers = None


def normalize_phone(value, country=None):
    if not value or not re.fullmatch(r'[+\d\s().-]+', value, flags=re.ASCII):
        return None
    value = re.sub(r'[\s().-]', '', value)
    if value.startswith('00'):
        value = '+' + value[2:]
    if phonenumbers:
        try:
            number = phonenumbers.parse(value, country)
        except phonenumbers.NumberParseException:
            return None
        if not phonenumbers.is_valid_number(number) or number.extension:
            return None
        return phonenumbers.format_number(number,
            phonenumbers.PhoneNumberFormat.E164)
    # Without the optional validator, only compare explicit international
    # numbers. Never guess a country or match by suffix.
    if re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
        return value
    return None


def phone_country(company):
    address = company.party.address_get()
    return address.country.code if address and address.country else None


def matching_phone_parties(number, company):
    from trytond.pool import Pool
    from trytond.transaction import Transaction

    pool = Pool()
    Mechanism = pool.get('party.contact_mechanism')
    Party = pool.get('party.party')
    country = phone_country(company)
    normalized = normalize_phone(number, country)
    if not normalized:
        return []
    domain = [('type', 'in', ['phone', 'mobile']),
        ('party.active', '=', True)]
    if hasattr(Party, 'current_company'):
        domain.append(('party.current_company', '=', True))
    with Transaction().set_context(company=company.id):
        return sorted({m.party for m in Mechanism.search(domain)
            if normalize_phone(m.value, country) == normalized},
            key=lambda party: party.id)
