"""Console page admission and GET-only dispatch against synthetic fixtures."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

from campaign_tool.records import admin_panel as panel, console_layout as layout, console_pages as pages
from campaign_tool.records import console_edge_smoke, console_findings, console_ocr, console_oversight
from campaign_tool.records import console_packets, console_phone, console_redactions
from tests.records.test_admin_panel import AS_OF, RUN, Base, Http
from tests.records.test_console_layout import Markup


def stub(name, paths, nav=None) -> Any:
    module = ModuleType(f'{pages.__package__}.console_page_{name}')
    vars(module).update(PATHS=paths, NAV=nav, serve=mock.Mock(return_value=False))
    return module


class ConsolePagesTests(Base):
    def load_registry(self, *modules):
        # Execute the real import-time initializer using a disposable package
        # directory and inject stubs as the modules returned by discovery.
        fixture = tempfile.TemporaryDirectory(dir=self.tmp.name)
        self.addCleanup(fixture.cleanup)
        directory = Path(fixture.name)
        source = directory / 'console_pages.py'
        source.write_bytes(Path(pages.__file__).read_bytes())
        for module in modules:
            (directory / (module.__name__.rsplit('.', 1)[1] + '.py')).touch()
        nested = directory / 'console_page_nested'
        nested.mkdir()
        (nested / '__init__.py').touch()
        (directory / 'console_page_Upper.py').touch()
        (directory / 'console_page_.py').touch()
        (directory / 'console_page_link.py').symlink_to(source.name)
        spec = importlib.util.spec_from_file_location(pages.__name__, source)
        assert spec is not None and spec.loader is not None
        registry = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {m.__name__: m for m in modules}):
            spec.loader.exec_module(registry)
        return registry

    def test_registered_get_dispatch_and_nav(self):
        view = stub('view', ('/fixture-page', '/fixture-alias'), ('Fixture <view> & more', 10))
        tail = stub('tail', ('/fixture-tail',), ('Last', 20))
        ahead = stub('zed', ('/fixture-ahead',), ('First', 10))
        tie = stub('aaa', ('/fixture-z',), ('First', 10))

        def render(handler):
            handler._send(200, layout.shell('Fixture', '<p>Registered page</p>', active=view.NAV[0]))
            return True

        view.serve.side_effect = render
        registry = self.load_registry(tail, tie, view, ahead)
        with mock.patch.object(panel, 'console_pages', registry), \
                mock.patch.object(pages, '_NAV', registry.nav_links()):
            server = panel.build_server(self.project, RUN, actor='operator-1', as_of=AS_OF, system=False)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            with server:
                thread.start()
                try:
                    client = Http(server.server_address[1])
                    for path in ('/fixture-page?filter=sample', '/fixture-alias'):
                        status, _, html = client.send('GET', path)
                        self.assertEqual(status, 200)
                        self.assertIn('Registered page', html)
                        self.assertIn('Fixture &lt;view&gt; &amp; more', html)
                        markup = Markup(html)
                        self.assertEqual(markup.current, ['/fixture-page'])
                        self.assertEqual(markup.links, ['#content', *(url for _, url in layout.LINKS + layout.MORE),
                                                        '/fixture-ahead', '/fixture-z', '/fixture-page', '/fixture-tail'])
                    for path in ('/fixture-pages', '/fixture-page/', '/%66ixture-page'):
                        self.assertEqual(client.send('GET', path)[0], 404)
                    self.assertEqual(client.send('GET', '/fixture-tail')[0], 404)  # callback declined
                    self.assertEqual(client.post('/fixture-page', {})[0], 404)
                    self.assertEqual(client.send('GET', '/fixture-page', host='example.invalid')[0], 403)
                finally:
                    server.shutdown()
                    thread.join(5)
        self.assertEqual([call.args[0].path for call in view.serve.call_args_list],
                         ['/fixture-page?filter=sample', '/fixture-alias'])
        tail.serve.assert_called_once()
        ahead.serve.assert_not_called()
        tie.serve.assert_not_called()

    def test_registered_get_dispatch_and_reserved_refusals(self):
        good = stub('ok', ('/fixture',))
        missing = stub('missing', ('/fixture',))
        del missing.serve
        post = stub('nonget', ('/fixture',))
        post.POST_PATHS = ('/submit',)
        for modules, error in (
            ((good, stub('duplicate', ('/fixture',))), 'duplicate path /fixture'),
            ((stub('repeat', ('/fixture', '/fixture')),), 'duplicate path /fixture'),
            ((stub('legacy', ('/board',)),), 'reserved path /board'),
            ((stub('action', ('/redactions/signoff',)),), 'reserved path /redactions/signoff'),
            ((missing,), 'serve must be callable'),
            ((post,), 'only GET'),
            ((stub('empty', ()),), 'nonempty tuple'),
            ((stub('badnav', ('/fixture',), ('', 1)),), 'printable label'),
            ((stub('badorder', ('/fixture',), ('Fixture', True)),), 'integer order'),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.load_registry(*modules)
        for path in ('POST /fixture', '/fixture?q=x', '/fixture#x', '/%66ixture', '/fixture/*',
                     '//elsewhere/fixture', '/fixture/..', 'https://elsewhere/fixture', None):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'exact GET paths'):
                self.load_registry(stub('malformed', (path,)))

    def test_reserved_paths_cover_current_console(self):
        inputs = [*(href for _, href in layout.LINKS + layout.MORE), *panel.RETURNS, *panel.ROUTES,
                  panel.RELINK_PATH, *console_edge_smoke.GET_PATHS, *console_edge_smoke.POST_PATHS,
                  *console_findings.ROUTES, *console_ocr.ROUTES, *console_oversight.ROUTES,
                  *console_packets.ROUTES, console_redactions.ROUTE, *console_phone.PATHS,
                  '/calendar.ics', '/redactions', '/review', '/site-prs']
        # A primary-nav link served by a registered page (Timeline) is a route, not a reserved built-in path.
        self.assertEqual({urlsplit(path).path for path in inputs} - set(pages._ROUTES), pages.EXISTING_PATHS)
