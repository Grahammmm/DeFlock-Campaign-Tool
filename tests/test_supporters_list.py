"""DF1109B-06: ``supporters list --directory D [--json]``, the private, read-only supporter inventory.

Synthetic organizations and meetings only; no network. Run: python3 -B -m unittest tests.test_supporters_list -q
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import supporters as supporters_module  # noqa: E402
from campaign_tool.supporters import (SupporterError, add_supporter, derive_id, load_supporters,  # noqa: E402
                                      row_hash)
from tests.test_findings_guide_doc import invoke  # noqa: E402
from tests.test_supporters import snapshot  # noqa: E402

REFUSAL = "Stopped: supporters list: cannot read valid supporter and staged meeting files\n"
KEYS = ["id", "status", "kind", "name", "joined_on", "consent_present", "confirmed_on", "cosign",
        "cosign_confirmed_on", "staged_meetings"]
EVIDENCE, OTHER = "e1" * 32, "f2" * 32
SECRET_KIT = "PRIVATE-KIT-SENTINEL"  # pragma: allowlist secret


def public_row(name, kind="community", joined="2026-09-01", url="https://alpha.example.invalid/about"):
    return {"id": derive_id(name), "name": name, "kind": kind, "url": url, "joined_on": joined, "organization": True}


def record(row, cosign=None, cosign_on="2026-09-05"):
    found = {"organization": True, "confirmed_on": row["joined_on"], "how": "letter", "public_sha256": row_hash(row),
             "correspondence": EVIDENCE}
    if cosign is not None:
        found.update({"cosign": cosign, "cosign_confirmed_on": cosign_on, "cosign_how": "form",
                      "cosign_correspondence": OTHER})
    return found


class ListBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-supporters-list-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.public_file = self.root / "content" / "supporters.json"
        self.private_file = self.root / "kit" / "supporters-consent.json"

    def write_public(self, rows, raw=None):
        self.public_file.write_text(raw if raw is not None else json.dumps({"schema_version": 1, "supporters": rows}),
                                    encoding="utf-8")

    def write_private(self, records, raw=None, mode=0o600):
        (self.root / "kit").mkdir(mode=0o700, exist_ok=True)
        self.private_file.write_text(raw if raw is not None else json.dumps({"schema_version": 1, "supporters": records}),
                                     encoding="utf-8")
        os.chmod(self.private_file, mode)

    def meeting(self, filename, cosigners=None, raw=None, **fields):
        folder = self.root / "content" / "meetings"
        folder.mkdir(exist_ok=True)
        document = {"body": "Cedar City Council", "starts_at": "2026-09-15T18:00:00-07:00", **fields}
        if cosigners is not None:
            document["cosigners"] = cosigners
        (folder / filename).write_text(raw if raw is not None else json.dumps(document), encoding="utf-8")

    def run_list(self, *extra, directory=None):
        return invoke("campaign_tool", ["supporters", "list", "--directory", str(directory or self.root), *extra])

    def ok(self, *extra):
        code, out, err = self.run_list(*extra)
        self.assertEqual((code, err), (0, ""), out)
        return json.loads(out) if "--json" in extra else out

    def refused(self, why):
        for extra in ((), ("--json",)):
            code, out, err = self.run_list(*extra)
            self.assertEqual((code, out, err), (2, "", REFUSAL), why)

    def populated(self):
        """Four listed rows written in reverse order (one without consent) and one staged meeting."""
        self.rows = {name: public_row(name) for name in ("Zeta Labor Council", "Gamma Neighbors", "Beta Faith Group",
                                                          "Alpha Union")}
        self.rows["Beta Faith Group"]["kind"] = "faith"
        self.write_public(list(self.rows.values()))
        rec = {derive_id(n): record(r, cosign=c) for (n, r), c in zip(
            list(self.rows.items())[1:], (["m-zulu", "council-jan"], "any", None))}
        self.write_private(rec)
        self.meeting("council-jan.json", ["gamma-neighbors", "beta-faith-group"], id="council-jan",
                     comment_kit_md=f"Alpha Union {SECRET_KIT}")


class SupportersListTests(ListBase):
    def test_list_public_fields_and_all_grant_shapes(self):
        self.populated()
        stored = self.private_file.read_bytes()
        self.assertEqual(self.ok(), (
            'alpha-union | community | "Alpha Union" | joined 2026-09-01 | consent present | confirmed 2026-09-01 | '
            "co-sign none | co-sign confirmed - | staged none\n"
            'beta-faith-group | faith | "Beta Faith Group" | joined 2026-09-01 | consent present | confirmed 2026-09-01 | '
            "co-sign any | co-sign confirmed 2026-09-05 | staged council-jan\n"
            'gamma-neighbors | community | "Gamma Neighbors" | joined 2026-09-01 | consent present | '
            "confirmed 2026-09-01 | co-sign council-jan,m-zulu | co-sign confirmed 2026-09-05 | staged council-jan\n"
            'zeta-labor-council | community | "Zeta Labor Council" | joined 2026-09-01 | consent missing | confirmed - | '
            "co-sign none | co-sign confirmed - | staged none\n"))
        rows = self.ok("--json")
        self.assertEqual([list(row) for row in rows], [KEYS] * 4)
        self.assertEqual(rows[2], {"id": "gamma-neighbors", "status": "listed", "kind": "community",
                                   "name": "Gamma Neighbors", "joined_on": "2026-09-01", "consent_present": True,
                                   "confirmed_on": "2026-09-01", "cosign": ["council-jan", "m-zulu"],
                                   "cosign_confirmed_on": "2026-09-05", "staged_meetings": ["council-jan"]})
        self.assertEqual(rows[3]["consent_present"], False)
        self.assertEqual([rows[0]["cosign"], rows[1]["cosign"], rows[3]["cosign_confirmed_on"]], [None, "any", None])
        self.assertEqual(self.private_file.read_bytes(), stored)  # the stored scope order is untouched
        self.assertEqual(json.loads(stored)["supporters"]["gamma-neighbors"]["cosign"], ["m-zulu", "council-jan"])

    def test_missing_and_empty_stores(self):
        self.assertEqual(self.ok(), "none recorded\n")
        self.assertEqual(self.ok("--json"), [])
        self.write_public([])
        self.assertEqual(self.ok(), "none recorded\n")
        self.write_private({})
        self.assertEqual(self.ok("--json"), [])
        self.public_file.unlink()
        self.assertEqual(self.ok(), "none recorded\n")
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), ["content", "kit", "supporters-consent.json"])
        bare = self.tmp / "bare"
        bare.mkdir()
        self.assertEqual(self.run_list(directory=bare), (0, "none recorded\n", ""))
        self.assertEqual(list(bare.iterdir()), [])  # no kit/, ledger or private initialization
        absent = self.tmp / "absent"
        self.assertEqual(self.run_list(directory=absent), (2, "", REFUSAL))
        self.assertFalse(absent.exists())

    def test_orphan_private_record_one_row(self):
        orphan = public_row("Delta Tenants")
        self.write_private({"delta-tenants": record(orphan, cosign=["council-jan"])})
        self.meeting("council-jan.json", ["delta-tenants"])
        line = ("delta-tenants | private record without public entry | confirmed 2026-09-01 | co-sign council-jan | "
                "co-sign confirmed 2026-09-05 | staged council-jan\n")
        for public in (None, []):
            if public is not None:
                self.write_public(public)
            self.assertEqual(self.ok(), line)
            self.assertEqual(self.ok("--json"), [{
                "id": "delta-tenants", "status": "private record without public entry", "kind": None, "name": None,
                "joined_on": None, "consent_present": True, "confirmed_on": "2026-09-01", "cosign": ["council-jan"],
                "cosign_confirmed_on": "2026-09-05", "staged_meetings": ["council-jan"]}])
        alpha = public_row("Alpha Union")
        self.write_public([alpha])
        self.write_private({"delta-tenants": record(orphan), "alpha-union": record(alpha)})
        rows = self.ok("--json")
        self.assertEqual([(r["id"], r["status"]) for r in rows], [("alpha-union", "listed"), ("delta-tenants", (
            "private record without public entry"))])
        with self.assertRaisesRegex(SupporterError, "orphan"):  # the writer still refuses that state
            add_supporter(self.root, name="Omega Group", kind="other", organization=True, confirmed_on="2026-09-01",
                          how="email")

    def test_pair_binding_mismatch_refused_not_present(self):
        alpha = public_row("Alpha Union")
        self.write_private({"alpha-union": record(alpha)})
        for change in ({"kind": "labor"}, {"url": None}, {"joined_on": "2026-09-02"}):
            self.write_public([{**alpha, **change}])
            self.refused(change)
        self.write_public([alpha])
        self.write_private({"alpha-union": {**record(alpha), "public_sha256": OTHER}})
        self.refused("digest")
        self.write_private({"alpha-union": {**record(alpha), "confirmed_on": "2026-09-03"}})
        self.refused("confirmed date")
        self.write_private({})
        self.assertIn("consent missing", self.ok())
        with self.assertRaisesRegex(SupporterError, "no consent record"):
            load_supporters(self.root)

    def test_staged_references_are_structured_not_name_search(self):
        self.populated()
        self.meeting("budget-hearing.json", ["gamma-neighbors", "nobody-org"], status="cancelled",
                     starts_at="2025-03-01T18:00:00-08:00")
        self.meeting("prose.json", None, id="prose-only", comment_kit_md="Alpha Union and Zeta Labor Council")
        (self.root / "content" / "meetings" / "notes.txt").write_text("{not json", encoding="utf-8")
        rows = {row["id"]: row for row in self.ok("--json")}
        self.assertEqual({key: row["staged_meetings"] for key, row in rows.items()}, {
            "alpha-union": [], "beta-faith-group": ["council-jan"],
            "gamma-neighbors": ["budget-hearing", "council-jan"], "zeta-labor-council": []})
        self.assertNotIn("m-zulu", rows["gamma-neighbors"]["staged_meetings"])  # granted, never staged
        self.meeting("copy.json", ["alpha-union"], id="council-jan")  # a second file resolving to the same id
        self.refused("duplicate meeting id")

    def test_broken_input_one_line_no_partial_output(self):
        alpha = public_row("Alpha Union")
        good_public = json.dumps({"schema_version": 1, "supporters": [alpha]})
        broken = {
            "public json": ("public", "{not json"), "public version": ("public", '{"schema_version": 2, "supporters": []}'),
            "public duplicate key": ("public", '{"schema_version": 1, "schema_version": 1, "supporters": []}'),
            "public duplicate name": ("public", json.dumps({"schema_version": 1, "supporters": [alpha, alpha]})),
            "public future date": ("public", json.dumps({"schema_version": 1, "supporters": [
                public_row("Alpha Union", joined="2999-01-01")]})),
            "private nan": ("private", '{"schema_version": 1, "supporters": {"x": NaN}}'),
            "private type": ("private", '{"schema_version": 1, "supporters": []}'),
            "private partial cosign": ("private", json.dumps({"schema_version": 1, "supporters": {
                "alpha-union": {**record(alpha), "cosign": "any"}}})),
            "private impossible date": ("private", json.dumps({"schema_version": 1, "supporters": {
                "alpha-union": {**record(alpha), "confirmed_on": "2026-02-30"}}})),
            "meeting json": ("meeting", "[1, 2"), "meeting schema": ("meeting", '{"body": "Council"}'),
            "meeting cosigners": ("meeting", json.dumps({"body": "B", "starts_at": "2026-09-15T18:00:00-07:00",
                                                         "cosigners": ["alpha-union", "alpha-union"]})),
            "meeting deep": ("meeting", "[" * 100000 + "]" * 100000),
        }
        for why, (where, raw) in broken.items():
            with self.subTest(why):
                self.write_public(None, good_public)
                self.write_private({"alpha-union": record(alpha)})
                meetings = self.root / "content" / "meetings"
                for path in meetings.glob("*.json") if meetings.exists() else ():
                    path.unlink()
                self.meeting("a-good.json", ["alpha-union"])  # a valid file read before the broken one
                if where == "public":
                    self.write_public(None, raw)
                elif where == "private":
                    self.write_private(None, raw)
                else:
                    self.meeting("z-broken.json", raw=raw.replace("Alpha Union", SECRET_KIT))
                self.refused(why)

    def test_caps_and_links_refused_before_following(self):
        alpha = public_row("Alpha Union")
        rows = [public_row(f"Org {chr(97 + i // 26)}{chr(97 + i % 26)}", url=None) for i in range(201)]
        self.write_public(rows[:200])
        self.assertEqual(len(self.ok("--json")), 200)  # 200 is the limit, missing consent is a state
        self.write_public(rows)
        self.refused("201 rows")
        self.write_public([alpha])
        self.write_private({"alpha-union": record(alpha, cosign=[f"m-{i}" for i in range(100)])})
        self.assertEqual(len(self.ok("--json")[0]["cosign"]), 100)
        self.write_private({"alpha-union": record(alpha, cosign=[f"m-{i}" for i in range(101)])})
        self.refused("101 grant ids")
        self.write_private({"alpha-union": record(alpha)})
        text = json.dumps({"schema_version": 1, "supporters": [alpha]})
        self.write_public(None, text + " " * (supporters_module.MAX_BYTES - len(text)))
        self.assertIn("alpha-union", self.ok())
        self.write_public(None, text + " " * (supporters_module.MAX_BYTES + 1 - len(text)))
        self.refused("public over 1 MiB")
        self.write_public([alpha])
        meeting = json.dumps({"body": "B", "starts_at": "2026-09-15T18:00:00-07:00", "cosigners": ["alpha-union"]})
        self.meeting("big.json", raw=meeting + " " * (supporters_module.MAX_MEETING_BYTES - len(meeting)))
        self.assertIn("staged big", self.ok())
        self.meeting("big.json", raw=meeting + " " * (supporters_module.MAX_MEETING_BYTES + 1 - len(meeting)))
        self.refused("meeting over 4 MiB")
        self.meeting("big.json", raw=meeting)
        os.chmod(self.private_file, 0o644)
        self.refused("private store readable by others")
        os.chmod(self.private_file, 0o600)
        outside = self.tmp / "outside.json"
        outside.write_text(meeting, encoding="utf-8")
        sentinel = outside.read_bytes()
        meetings = self.root / "content" / "meetings"
        cases = []

        def link_meeting():
            (meetings / "linked.json").symlink_to(outside)
            return lambda: (meetings / "linked.json").unlink()

        def hardlink_meeting():
            os.link(outside, meetings / "hard.json")
            return lambda: (meetings / "hard.json").unlink()

        def fifo_meeting():
            os.mkfifo(meetings / "pipe.json")
            return lambda: (meetings / "pipe.json").unlink()

        def swap(path, target_is_dir=False):
            def apply():
                moved = path.with_name(path.name + ".real")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=target_is_dir)
                return lambda: (path.unlink(), moved.rename(path))
            return apply

        cases = [link_meeting, hardlink_meeting, fifo_meeting, swap(self.public_file), swap(self.private_file),
                 swap(self.root / "kit", True), swap(meetings, True), swap(self.root / "content", True)]
        for case in cases:
            with self.subTest(getattr(case, "__name__", "swap")):
                undo = case()
                self.refused(case)
                undo()
        self.assertIn("alpha-union", self.ok())
        link = self.tmp / "via-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.run_list(directory=link), (2, "", REFUSAL))
        ancestor = self.tmp / "ancestor-link"
        ancestor.symlink_to(self.tmp, target_is_directory=True)
        self.assertEqual(self.run_list(directory=ancestor / "campaign"), (2, "", REFUSAL))
        with mock.patch.object(supporters_module.os, "open", side_effect=PermissionError("denied")):
            self.refused("injected read error")
        self.assertEqual(outside.read_bytes(), sentinel)

    def test_sensitive_fields_never_leave_projection(self):
        self.populated()
        orphan = public_row("Delta Tenants")
        stores = json.loads(self.private_file.read_text())["supporters"]
        stores["delta-tenants"] = record(orphan, cosign="any")
        self.write_private(stores)
        outputs = [self.run_list(), self.run_list("--json")]
        for row in json.loads(outputs[1][1]):
            self.assertEqual(list(row), KEYS)
        self.write_private(None, '{"schema_version": 1, "supporters": {"' + SECRET_KIT.lower() + '": 1}}')
        outputs += [self.run_list(), self.run_list("--json")]
        self.assertEqual(outputs[2:], [(2, "", REFUSAL)] * 2)
        text = "".join(out + err for _, out, err in outputs)
        for secret in (EVIDENCE, OTHER, EVIDENCE[:12], row_hash(orphan), "letter", '"how"', "correspondence",
                       "public_sha256", SECRET_KIT, SECRET_KIT.lower(), "https://", "Cedar City Council", str(self.tmp)):
            self.assertNotIn(secret, text)

    def test_list_is_read_only_every_outcome(self):
        def boom(*args, **kwargs):
            raise AssertionError("a writer, ledger or network entry point was reached")

        patches = [mock.patch.object(supporters_module, name, boom) for name in (
            "_install_pair", "_install_private", "_temporary", "_ensure_private_dir", "_correspondence")]
        patches += [mock.patch("socket.socket.connect", boom), mock.patch("campaign_tool.site.build_site", boom)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        states = {
            "empty": lambda: None,
            "populated": self.populated,
            "orphan": lambda: self.write_private({"delta-tenants": record(public_row("Delta Tenants"))}),
            "refusal": lambda: self.write_public(None, "{broken"),
        }
        for name, prepare in states.items():
            with self.subTest(name):
                prepare()
                before = snapshot(self.root)
                for extra in ((), ("--json",)):
                    code, _, _ = self.run_list(*extra)
                    self.assertEqual(code, 2 if name == "refusal" else 0)
                self.assertEqual(snapshot(self.root), before)

    def test_cli_json_dest_and_branch_do_not_mutate(self):
        self.populated()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        before = snapshot(self.root)
        for argv in (["--json", "--directory", str(self.root)], ["--directory", str(self.root), "--json"]):
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "supporters", "list", *argv],
                                  cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual([row["id"] for row in json.loads(done.stdout)],
                             ["alpha-union", "beta-faith-group", "gamma-neighbors", "zeta-labor-council"])
            self.assertTrue(done.stdout.endswith("]\n"))
            self.assertNotIn("Recorded only", done.stdout)
        for argv in ([], ["--directory", str(self.root), "--id", "alpha-union"],
                     ["--directory", str(self.root), "--checklist"], ["--directory", str(self.root), "--replace"]):
            code, out, err = invoke("campaign_tool", ["supporters", "list", *argv])
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("usage:", err)
        with mock.patch.object(supporters_module, "add_supporter", side_effect=AssertionError), \
                mock.patch.object(supporters_module, "remove_supporter", side_effect=AssertionError), \
                mock.patch.object(supporters_module, "set_cosign_consent", side_effect=AssertionError):
            self.assertIn("alpha-union", self.ok())
        self.assertEqual(snapshot(self.root), before)

    def test_legacy_defaults_byte_identical(self):
        common = ["--directory", str(self.root)]
        self.assertEqual(invoke("campaign_tool", ["supporters", "add", *common, "--name", "Alpha Union", "--kind",
                                                  "community", "--organization", "--confirmed-on", "2026-09-01",
                                                  "--how", "email"]),
                         (0, '{"id": "alpha-union", "action": "added"}\nRecorded only. Nothing was published.\n', ""))
        self.assertIn("consent present", self.ok())
        self.assertEqual(invoke("campaign_tool", ["supporters", "consent", *common, "--id", "alpha-union", "--cosign",
                                                  "any", "--confirmed-on", "2026-09-02", "--how", "meeting"]),
                         (0, '{"id": "alpha-union", "action": "cosign granted"}\n'
                             "Recorded only. Nothing was published.\n", ""))
        self.assertIn("co-sign any | co-sign confirmed 2026-09-02", self.ok())
        self.assertEqual(invoke("campaign_tool", ["supporters", "remove", *common, "--id", "alpha-union"]),
                         (0, '{"id": "alpha-union", "action": "removed"}\nRecorded only. Nothing was published.\n', ""))
        self.assertEqual(self.ok(), "none recorded\n")

    def test_name_column_quoted_and_unicode_stable(self):
        names = ['Zürich | "Quoted" \\ Coop', "Ångström Tenants", "Mid Union"]
        rows = [public_row(name, kind=kind, url=None) for name, kind in zip(names, ("business", "other", "labor"))]
        self.write_public(rows)
        self.write_private({row["id"]: record(row) for row in rows})
        listed = self.ok("--json")
        self.assertEqual([row["id"] for row in listed], sorted(row["id"] for row in rows))
        self.assertEqual({row["name"] for row in listed}, set(names))
        lines = self.ok().splitlines()
        self.assertEqual(len(lines), 3)
        quoted = next(line for line in lines if line.startswith("z-rich-quoted-coop | "))
        self.assertEqual(quoted.split(" | ", 2)[2].split(" | joined")[0], json.dumps(names[0], ensure_ascii=False))
        self.assertEqual(quoted.count(" | "), 9)  # the name's own pipe is inside the quoted column


if __name__ == "__main__":
    unittest.main()
