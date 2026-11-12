"""Loopback packet decisions only: no site-change writer, switch changes or transport."""
import hmac
import urllib.parse

from . import public_packets as pp
from .console_oversight import _journal_actor
from .console_snapshot import LiveSource
from .outcomes.reading import BANNER, esc
from . import console_layout

ROUTES = {"/packets/approve": "signature", "/packets/reject": "reason"}


def render(project, token, source=None):
    """token None (a read-only export) renders the packets and their exact payloads without any form."""
    source = source or LiveSource(project)
    rows = source.packets_pending()
    switch = source.packets_status()["switch"]
    out = [console_layout.start("Update packets"), '<h1>Update packets</h1>',
           '<p>Packets switch: %s. Approve records G\'s signed decision only. '
           'Nothing is sent, published or written to site-changes. Writing remains a separate CLI step.</p>' % esc(switch)]
    if not rows:
        out.append('<p>No packets pending.</p>')
    for row in rows:
        out.append('<section><h2>%s</h2><dl>%s</dl>' % (esc(row['title']), ''.join(
            '<dt>%s</dt><dd>%s</dd>' % (esc(k), esc(row[k])) for k in (
                'id', 'kind', 'agency_key', 'request_id', 'source', 'revision', 'status',
                'proposed_at', 'age_hours', 'expires_at'))))
        if row['status'] == 'proposed':
            out.append('<h3>Exact signature payload</h3><pre>%s</pre>' % esc(source.packet_payload(row['id']).decode('utf-8')))
        for path, field in ROUTES.items() if token is not None else ():
            if field == 'signature' and row['status'] != 'proposed':
                continue
            out.append('<form method="post" action="%s">%s<label>%s<textarea name="%s" required></textarea></label>'
                       '<button type="submit">%s</button></form>' % (
                           path, ''.join('<input type="hidden" name="%s" value="%s">' % (k, esc(v))
                                         for k, v in (('csrf_token', token), ('id', row['id']), ('revision', row['revision']))),
                           'G’s SSH signature' if field == 'signature' else 'Rejection reason (private audit)', field,
                           'Approve' if field == 'signature' else 'Reject'))
        out.append('</section>')
    return '\n'.join(out + ['</main></body></html>'])


def serve(handler):
    path = urllib.parse.urlsplit(handler.path)
    if path.path != '/packets' or path.query:
        return False
    try:
        page = render(handler.options['project'], handler.csrf_token(), handler.options['source'])
    except (pp.PacketError, OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
    else:
        handler._send(200, page)
    return True


def post(handler):
    if handler.path not in ROUTES:
        return False
    from . import console_origin
    if not console_origin.request_ok(handler, post=True):
        handler._fail(403)
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    field = ROUTES[handler.path]
    if set(fields) != {'csrf_token', 'id', 'revision', field}:
        handler._fail(400)
        return True
    if not hmac.compare_digest(fields['csrf_token'].encode('utf-8'), handler.server.csrf_token.encode('ascii')):
        handler._fail(403)
        return True
    if not pp.PACKET_ID.fullmatch(fields['id']) or not fields['revision'].isascii() or not fields['revision'].isdigit() or len(fields['revision']) > 10:
        handler._fail(400)
        return True
    try:
        project, revision = handler.server.options['project'], int(fields['revision'])
        if field == 'signature':
            pp.approve(project, fields['id'], fields[field].replace('\r\n', '\n'), revision=revision)
        else:
            pp.decide(project, fields['id'], 'rejected', _journal_actor(handler.server.actor),
                      revision=revision, reason=fields[field])
    except pp.PacketError as error:
        handler._fail(403 if error.code == 'approval_required' else 400 if error.code == 'reason_invalid' else
                      409 if error.code in ('packet_unknown', 'packet_stale', 'packet_not_open', 'packet_expired') else 503)
    except OSError:
        handler._fail(503)
    else:
        handler._send(303, '', location='/packets')
    return True
