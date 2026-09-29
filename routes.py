import base64
import hashlib
import hmac
import json

from trytond.protocols.wrappers import (
    Response, abort, with_pool, with_transaction)
from trytond.wsgi import app


@app.route('/<database_name>/yeastar/webhook/<int:pbx_id>', methods={'POST'})
@with_pool
@with_transaction(readonly=False)
def webhook(request, pool, pbx_id):
    PBX = pool.get('yeastar.pbx')
    Event = pool.get('yeastar.webhook.event')

    records = PBX.search([
            ('id', '=', pbx_id),
            ('webhook_enabled', '=', True),
            ], limit=1)
    if not records:
        abort(404)
    pbx, = records
    if not pbx.webhook_secret:
        abort(403)
    body = request.get_data()
    signature = base64.b64encode(hmac.digest(
            pbx.webhook_secret.encode('utf-8'), body, 'sha256'))
    supplied = request.headers.get('X-Signature', '').encode('utf-8')
    if not hmac.compare_digest(signature, supplied):
        abort(403)
    try:
        payload = body.decode('utf-8')
        data = json.loads(payload)
    except (UnicodeError, ValueError):
        abort(400)
    if not isinstance(data, dict):
        abort(400)
    if (pbx.serial_number and data.get('sn')
            and data['sn'] != pbx.serial_number):
        abort(403)
    if data.get('event') == 'test':
        event_type = 'test'
    elif type(data.get('type')) is int:
        event_type = str(data['type'])
    else:
        abort(400)
    # Yeastar may encode the nested message as a JSON string.
    message = data.get('msg', {})
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except ValueError:
            message = {}
    call_id = message.get('call_id') if isinstance(message, dict) else None
    if not isinstance(call_id, str):
        call_id = None
    digest = hashlib.sha256(body).hexdigest()
    # Serialize deliveries for a PBX, including concurrent retries.
    PBX.lock([pbx])
    if not Event.search([
                ('pbx', '=', pbx.id),
                ('digest', '=', digest),
                ], limit=1):
        Event.create([{
                    'pbx': pbx.id,
                    'event_type': event_type,
                    'call_id': call_id,
                    'digest': digest,
                    'payload': payload,
                    }])
    return Response(status=204)
