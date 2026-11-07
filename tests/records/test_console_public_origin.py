"""Synthetic exact-pair acceptance for the loopback records panel."""
import contextlib
import io
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from campaign_tool.records import admin_panel as ap
from tests.records.test_admin_panel import ServerCase, RUN, journal

PUBLIC = "https://admin.example.test"
HOST = "admin.example.test"


class PublicOriginTests(ServerCase):
    def public_server(self):
        self.server = self.start(public_origin=PUBLIC)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def request(self, method="GET", path="/board", hosts=None, origins=None, fields=None, extra_headers=None):
        hosts = [HOST] if hosts is None else hosts
        origins = [PUBLIC] if origins is None else origins
        body = urlencode(fields or {}).encode()
        headers = [("Host", h) for h in hosts] + [("Origin", o) for o in origins]
        headers += list((extra_headers or {}).items())
        headers += [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", str(len(body)))]
        raw = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body
        return self.http.send(method, path, raw=raw)

    def reject_fields(self):
        return {"csrf_token": self.tok, "id": self.draft_row["id"], "reason": "not needed"}

    def test_default_is_loopback_only(self):
        loop = f"127.0.0.1:{self.http.port}"
        for hosts, status in (([HOST], 403), ([f"localhost:{self.http.port}"], 403), ([loop], 200)):
            self.assertEqual(self.request(hosts=hosts)[0], status)
        before = journal(self.project)
        self.assertEqual(self.request("POST", "/approvals/reject", [loop], fields=self.reject_fields())[0], 403)
        self.assertEqual(journal(self.project), before)

    def test_public_get_host_matrix(self):
        self.public_server()
        loop = f"127.0.0.1:{self.http.port}"
        for hosts in (["ADMIN.example.test"], [HOST + ":443"], ["evil.example.test"],
                      [HOST + ".evil.test"], ["x" + HOST], [""], [], [HOST, loop], [loop, HOST], [HOST, HOST]):
            with self.subTest(hosts=hosts):
                self.assertEqual(self.request(hosts=hosts)[0], 403)
        for hosts in ([HOST], [loop]):
            self.assertEqual(self.request(hosts=hosts)[0], 200)

    def test_public_post_csrf_and_readback(self):
        self.public_server()
        fields = self.reject_fields()
        before = journal(self.project)
        self.assertEqual(self.request("POST", "/approvals/reject", fields={**fields, "csrf_token": "wrong"})[0], 403)
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.request("POST", "/approvals/reject", fields=fields)[0], 303)
        self.assertEqual(self.row()["status"], "rejected")
        # Valid transport never bypasses a terminal queue transition.
        self.assertEqual(self.request("POST", "/approvals/reject", fields=fields)[0], 409)

    def test_post_origin_matrix_never_mutates(self):
        self.public_server()
        loop = f"127.0.0.1:{self.http.port}"
        before = journal(self.project)
        cases = [([HOST], origins) for origins in (["http://" + HOST], ["null"], [], [PUBLIC, PUBLIC],
                 [PUBLIC + ":443"], [PUBLIC + "/"], ["https://ADMIN.example.test"], ["http://" + loop],
                 [""], ["https://u@" + HOST], [PUBLIC + ".evil.test"], ["https://x" + HOST])]
        cases += [([loop], [PUBLIC]), ([], [PUBLIC]), ([HOST, HOST], [PUBLIC])]
        for hosts, origins in cases:
            with self.subTest(hosts=hosts, origins=origins):
                self.assertEqual(self.request("POST", "/approvals/reject", hosts, origins, self.reject_fields())[0], 403)
                self.assertEqual(journal(self.project), before)
                self.assertEqual(self.row()["status"], "pending")

    def test_all_dispatch_paths_use_same_gate(self):
        self.public_server()
        loop = f"127.0.0.1:{self.http.port}"
        for path in ("/public-updates/off", "/manual-event", "/packets/build"):
            for hosts, origins, refused in (([HOST], [PUBLIC], False), ([loop], [PUBLIC], True),
                                            ([HOST], ["null"], True)):
                with self.subTest(path=path, hosts=hosts, origins=origins):
                    status = self.request("POST", path, hosts, origins, {"csrf_token": self.tok})[0]
                    self.assertEqual(status == 403, refused)
        records = Path(ap.__file__).parent
        for name in ("admin_panel.py", "console_oversight.py", "console_packets.py"):
            self.assertNotIn('http://127.0.0.1:%d', (records / name).read_text())
        self.assertIn('http://127.0.0.1:%d', (records / "console_origin.py").read_text())

    def test_refusals_do_not_echo_or_log(self):
        self.public_server()
        with mock.patch.object(ap.sys, "stderr") as err:
            response = self.request("POST", "/origin-marker", ["evil-marker.example.test"],
                                    ["https://origin-marker.example.test"])
        self.assertEqual(response[0], 403)
        for marker in ("evil-marker", "origin-marker"):
            self.assertNotIn(marker, response[2])
        err.write.assert_not_called()

    def test_invalid_startup_never_builds_listener(self):
        bad = ["http://" + HOST, PUBLIC + "/", PUBLIC + ":8443", "https://u@" + HOST,
               "https://*.example.test", "https://Admin.example.test", "https://127.0.0.1",
               "https://localhost", "https://admin", "https://admin..test", "https://-a.example.test",
               PUBLIC + ".", PUBLIC + "?x=1", PUBLIC + "#f", "https://" + "a" * 260,
               PUBLIC + " ", PUBLIC + "\n", "https://admin.123", "https://" + "a" * 64 + ".test", ""]
        for value in bad:
            with self.subTest(value=value), mock.patch.object(ap, "build_server") as build, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                result = ap.main(["--project", str(self.project), "--run-id", RUN, "--actor", "op",
                                  "--public-origin", value])
                self.assertEqual((result, err.getvalue()), (2, "public_origin_invalid\n"))
                build.assert_not_called()
            with mock.patch.object(ap.http.server, "HTTPServer") as listener:
                with self.assertRaises(ap.PanelError) as caught:
                    ap.build_server(self.project, RUN, actor="op", public_origin=value)
                self.assertEqual(caught.exception.code, "public_origin_invalid")
                listener.assert_not_called()

    def test_startup_names_public_origin_and_render_ignores_it(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 8765)
        server.serve_forever.side_effect = KeyboardInterrupt
        args = ["--project", str(self.project), "--run-id", RUN, "--no-system", "--public-origin", PUBLIC]
        with mock.patch.object(ap, "build_server", return_value=server), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ap.main(args + ["--actor", "op"]), 0)
        self.assertIn(PUBLIC, out.getvalue())
        self.assertIn("127.0.0.1", out.getvalue())
        server.server_close.assert_called_once()
        target = self.project / "snapshot.html"
        with mock.patch.object(ap, "build_server") as build:
            self.assertEqual(ap.main(args[:-1] + ["not-an-origin", "--render", str(target)]), 0)
            build.assert_not_called()
        self.assertIn("Static snapshot", target.read_text())
