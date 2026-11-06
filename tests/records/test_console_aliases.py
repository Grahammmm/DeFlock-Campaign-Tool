"""Optional, all-or-nothing Documents aliases, using invented agency identifiers."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import admin_panel as ap, console_documents as cd
from tests.records.test_console_documents import build, put, RUN, NOW

HEADER = "agency_key\talias\n"
VALID = HEADER + "alpha-pd\talphaca\nalpha-pd\tAlpha Police Department\n"


class Aliases(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.path = self.project / "agency-aliases.tsv"
        build(self.project)
        out = self.project / "run-outputs" / RUN / "outcomes"
        for name, field, ident, value in (
                ("received-items.json", "items", "agency_id", "alphaca"),
                ("gap-proposals.json", "agencies", "agency_id", "Alpha Police Department"),
                ("response-items.json", "requests", "agency_key", "Alpha Police Department")):
            doc = json.loads((out / name).read_text())
            for row in doc[field]:
                if row[ident] == "alpha-pd":
                    row[ident] = value
            put(out / name, doc)
        doc = json.loads((out / "release-links.json").read_text())
        doc["links"][0]["agency_ids"] = ["alphaca", "alpha-pd"]
        put(out / "release-links.json", doc)

    def model(self):
        return cd.load(self.project, RUN, pages=False)

    def test_absent_preserves_view_and_page_bytes(self):
        model = self.model()
        with patch.object(cd, "_aliases", return_value={"state": "absent"}):
            baseline = self.model()
        baseline.pop("aliases")  # model shape predating optional aliases
        self.assertEqual(cd.view(model, "alpha-pd", NOW), cd.view(baseline, "alpha-pd", NOW))
        self.assertEqual(cd.render(model, NOW).encode(), cd.render(baseline, NOW).encode())
        self.assertNotIn("aliases", cd.sources(model))

    def test_join_all_sources_and_deduplicate(self):
        self.path.write_text(VALID)
        model = self.model()
        self.assertEqual(cd.agencies(model), ["alpha-pd", "beta-so"])
        view = cd.view(model, "alpha-pd", NOW)
        self.assertEqual([i["item_id"] for i in view["items"]], ["i-a"])
        self.assertEqual(view["items"][0]["answers_asks"], ["a2"])
        self.assertEqual(len(view["release_links"]), 1)
        self.assertEqual(view["release_links"][0]["status"], "expired")
        self.assertEqual(len(view["dispositions"]), 1)
        self.assertEqual([p["proposal_id"] for p in view["findings"]], ["p1", "p2"])
        self.assertEqual(len(cd.view(model, None, NOW)["release_links"]), 1)
        self.assertEqual(cd.view(model, "alphaca", NOW)["items"], [])
        self.assertEqual(cd.sources(model)["aliases"], {"state": "ok"})

    def test_invalid_files_never_apply_partial_map(self):
        cases = ["bad\n", VALID + "beta-so\talphaca\n", VALID + "beta-so\talpha-pd\n",
                 VALID + "bad key\tnew\n", VALID + "alpha-pd\ta\tb\n",
                 VALID + "alpha-pd\t" + "x" * 129 + "\n", VALID + "alpha-pd\t\n",
                 VALID + "alpha-pd\tx\x00\n", VALID + "alpha-pd\tx\vbeta-so\ty\n",
                 HEADER + "".join("alpha-pd\tx%d\n" % i for i in range(501)),
                 "x" * (512 * 1024 + 1), b"agency_key\talias\n\xff"]
        for data in cases:
            with self.subTest(data=repr(data[:60])):
                self.path.write_bytes(data.encode() if isinstance(data, str) else data)
                self.assert_invalid()
        self.path.unlink()
        self.path.symlink_to(self.project / "missing.tsv")
        self.assert_invalid()
        (self.project / "missing.tsv").write_text(VALID)
        self.assert_invalid()
        self.path.unlink()
        self.path.mkdir()
        self.assert_invalid()
        self.path.rmdir()
        os.mkfifo(self.path)
        self.assert_invalid()

    def assert_invalid(self):
        model = self.model()
        self.assertEqual(model["aliases"], {"state": "unavailable", "code": "aliases_invalid"})
        self.assertEqual(cd.view(model, "alpha-pd", NOW)["items"], [])
        self.assertIn("alphaca", cd.agencies(model))
        self.assertIn("aliases: unavailable (aliases_invalid)", cd.render(model, NOW))

    def test_helper_identity_and_valid_limits(self):
        for aliases in (None, {}, {"state": "absent"}, {"state": "unavailable", "map": {"alphaca": "wrong"}},
                        {"state": "ok", "map": None}):
            self.assertEqual(cd._key({"aliases": aliases}, "alphaca"), "alphaca")
        self.assertEqual(cd._key({}, "alphaca"), "alphaca")
        self.path.write_text(HEADER + "".join("alpha-pd\tx%d\n" % i for i in range(499)) + "alpha-pd\t" + "x" * 128 + "\n")
        model = self.model()
        self.assertEqual(cd._key(model, "x0"), "alpha-pd")
        self.assertEqual(cd._key(model, "unknown"), "unknown")
        self.path.write_text(VALID + "alpha-pd\talphaca\n")
        self.assertEqual(cd._key(self.model(), "alphaca"), "alpha-pd")

    def test_panel_route_shows_joined_item(self):
        self.path.write_text(VALID)
        server = ap.build_server(self.project, RUN, actor="op", system=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with closing(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)) as conn:
            conn.request("GET", "/documents?agency=alpha-pd")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("i-a", response.read().decode())
