"""Synthetic layout and live route coverage; no external records or assets."""
import threading
from html.parser import HTMLParser

from campaign_tool.records import admin_panel as ap, console_layout as layout
from campaign_tool.records.outcomes.reading import BANNER
from tests.records.test_admin_panel import Base, Http, RUN, AS_OF, tree_state


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.current, self.ids, self.tags = [], [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if 'id' in a:
            self.ids.append(a['id'])
        if tag == 'a':
            self.links.append(a['href'])
            if 'aria-current' in a:
                self.current.append(a['href'])


class LayoutTests(Base):
    def test_nav_order_and_active(self):
        self.assertEqual(layout.LINKS[-1], ("Settings", "/settings"))
        expected = [url for _, url in layout.LINKS + layout.MORE]
        for active, current in ((None, []), ('Calendar', ['/calendar']), ('unknown', []),
                                ('<script>', [])):
            with self.subTest(active=active):
                html = layout.nav(active)
                parsed = Markup(html)
                self.assertEqual(parsed.links, expected)
                self.assertEqual(parsed.current, current)
                self.assertNotIn('<script', html)
                self.assertIn('More:', html)
        positions = [layout.nav().index('>' + label + '</a>') for label, _ in layout.LINKS]
        self.assertEqual(positions, sorted(positions))

    def test_shell_escapes_title_and_is_asset_free(self):
        html = layout.shell('<b>x</b>', '<p class="overdue">OVERDUE</p>')
        self.assertIn('<title>&lt;b&gt;x&lt;/b&gt;</title>', html)
        self.assertIn(BANNER, html)
        self.assertIn('noindex,nofollow', html)
        self.assertIn('.overdue{color:#b00020;font-weight:700}', html)
        self.assertIn('<style media="(max-width:699px)">', html)
        self.assertEqual(Markup(html).ids, ['content'])
        for forbidden in ('<script', 'http://', 'https://'):
            self.assertNotIn(forbidden, html)

    def test_every_page_and_nav_destination_is_read_only(self):
        # Missing/stale optional inputs must still render the shared shell.
        for populated in (False, True):
            if populated:
                self.full()
            server = ap.build_server(self.project, RUN, actor='operator-1', as_of=AS_OF,
                                     system=False, run_report_path=self.project / 'report.json')
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                client = Http(server.server_address[1])
                # The existing packets reader initializes its synchronization lock.
                self.assertEqual(client.send('GET', '/packets')[0], 200)
                # SQLite readers may create WAL/SHM sidecars; compare logical rows
                # separately from non-database evidence bytes.
                def snapshot():
                    import sqlite3
                    from contextlib import closing
                    files = {p: digest for p, digest in tree_state(self.project).items()
                             if not p.endswith(('ledger.sqlite-shm', 'ledger.sqlite-wal'))}
                    rows = None
                    if self.ledger.exists():
                        with closing(sqlite3.connect(self.ledger)) as con:
                            rows = list(con.iterdump())
                    return files, rows
                before = snapshot()
                paths = dict.fromkeys(url.split('#')[0] for _, url in layout.LINKS + layout.MORE)
                paths['/agency?agency=alpha-pd'] = None
                for path in paths:
                    with self.subTest(path=path, populated=populated):
                        status, _, html = client.send('GET', path)
                        self.assertEqual(status, 200)
                        parsed = Markup(html)
                        self.assertEqual(parsed.links[:len(layout.LINKS + layout.MORE)], [u for _, u in layout.LINKS + layout.MORE])
                        self.assertEqual(parsed.tags.count('main'), 1)
                        self.assertEqual(parsed.ids.count('content'), 1)
                        self.assertIn(layout.STYLE, html)
                        self.assertIn(BANNER, html)
                        active = {'/': '/', '/board': '/board', '/calendar': '/calendar',
                                  '/documents': '/documents', '/settings': '/settings', '/agency?agency=alpha-pd': '/board'}
                        self.assertEqual(parsed.current, [active[path]] if path in active else [])
                        if path == '/':
                            self.assertTrue({'decide', 'more'} <= set(parsed.ids))
                        if path == '/calendar':
                            self.assertIn('/calendar.ics', parsed.links)
                self.assertEqual(snapshot(), before)
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()
