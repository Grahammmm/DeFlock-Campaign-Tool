"""Read-only outgoing switches for the private loopback console; no transports."""
import os

from . import console_layout, followup_clock, mail_send, public_packets, public_updates
from .outcomes.reading import esc


def _row(label, value):
    return '<dt>%s</dt><dd>%s</dd>' % (esc(label), esc(value))


def render(project, send_config=None, drafts_config=None):
    out = ['<h1>Settings</h1><p>Read-only. Changes are owner-only steps outside this page.</p>']
    config = None
    if send_config is None:
        out.append(_row('Send config', 'not given to the console'))
    else:
        out.append(_row('Send config', 'given to the console'))
        try:
            config = mail_send.load_config(send_config)
        except mail_send.MailSendError:
            out.append(_row('Send config invalid or unavailable', 'sending unavailable'))
        if config is not None:
            state = mail_send.switch_state(project, send_config)
            out.extend((_row('Enabled', 'yes' if config['enabled'] else 'no'),
                        _row('Can send now (subject to approval and recipient gates)',
                             'yes' if state['can_send'] else 'no'),
                        _row('Daily cap', state['cap'] if state['cap'] is not None else 'unknown'),
                        _row('Sent today (%s; includes sending)' %
                             (state.get('timezone') if state['count'] is not None and state.get('timezone')
                              else 'unknown zone'),
                             state['count'] if state['count'] is not None else 'unknown'),
                        _row('Mode', 'live' if config['live'] else 'test'),
                        _row('Test recipient', config['test_recipient'] or 'not configured'),
                        _row('SMTP password file', 'configured')))
    out.insert(1, '<p role="status">%s</p><h2>Sending</h2><dl>' % esc(
        'Nothing can be sent from this console: sending is not set up.' if config is None else
        ('Sending is set up and %s.' % ('can send now, after approval' if state['can_send'] else
                                        'is off, so nothing can be sent now'))))
    present = []
    for index, path in enumerate(mail_send.kill_files(project, config)):
        if os.path.lexists(path):
            present.append(('approvals/SEND-STOP', 'modes/RECORDS-SEND-STOP')[index]
                           if index < 2 else os.path.basename(path))
    out.append(_row('Stop files present', ', '.join(present) or 'none'))
    if send_config is not None and config is None:
        out.append(_row('Additional config stop file', 'unknown (config unavailable)'))
    out.append('</dl><p><code>records mail-send init ...</code> writes it switched off; '
               'only G edits <code>enabled</code>, <code>live</code>, <code>daily_cap</code> and '
               '<code>test_recipient</code>; removing a stop file is G’s step.</p>')
    out.append('<h2>Drafts mailbox</h2><dl>' + _row('Configured', 'yes' if drafts_config else 'no') +
               '</dl><p>Change the console’s <code>records panel --mail-drafts-config ...</code> option.</p>')
    settings = followup_clock.load_settings(project)
    out.append('<h2>Reminders</h2>')
    if settings['invalid']:
        out.append('<p>settings file invalid: all reminders off</p>')
    out.append('<table><tr><th>Agency</th><th>Autonomy</th><th>Silence after days</th>'
               '<th>Notice after days</th></tr>')
    for label, override in [('Default (all other agencies)', {})] + sorted(settings['agencies'].items()):
        effective = dict(settings['default'], **override)
        out.append('<tr>' + ''.join('<td>%s</td>' % esc(v) for v in
                                   (label, effective['autonomy'], effective['silence_after_days'],
                                    effective['notice_after_days'])) + '</tr>')
    out.append('</table><p>Edit <code>followup-settings.json</code> (owner-only); '
               '<code>draft</code> makes reminder drafts, there is no send value.</p>')
    for title, module, command, payload in (
            ('Public status lines', public_updates, 'public-updates', 'payload'),
            ('Packets', public_packets, 'public-packets', 'switch-payload')):
        try:
            state = module.switch_state(project, read_only=True)
        except (public_updates.PublicUpdatesError, public_packets.PacketError, OSError, ValueError, KeyError, TypeError):
            state = 'unavailable (not confirmed ON or OFF)'
        out.append('<h2>%s</h2><p>Switch: %s</p><p>G uses <code>records %s off --project PROJECT</code> '
                   'to turn off. To turn on, obtain <code>records %s %s --project PROJECT</code>, '
                   'sign it, then <code>records %s enable --project PROJECT --signature-file SIGNATURE</code>. '
                   'Existing stop files and approval gates still apply; this page changes nothing.</p>' %
                   (esc(title), esc(state), command, command, payload, command))
    return console_layout.shell('Settings', '\n'.join(out), active='Settings')


def serve(handler):
    if handler.path != '/settings':
        return False
    options = handler.server.options
    handler._send(200, render(options['project'], options.get('send_config'), handler.server.mail_drafts_config))
    return True
