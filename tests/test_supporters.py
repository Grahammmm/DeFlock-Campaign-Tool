"""DF2207-13: consent-backed supporting organizations (synthetic organizations only; no network).

content/supporters.json is public, kit/supporters-consent.json is private (0600); a public row is listed only while a
consent record binds its exact bytes.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import supporters as supporters_module
from campaign_tool.content import STARTER_SLUGS, ContentError, load_content, load_pages
from campaign_tool.content_check import content_problems
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.supporters import (KINDS, SupporterError, add_supporter, consent_for_supporter, load_supporters,
                                      read_consent, remove_supporter, row_hash, supporter_problems)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 1)
BASE = "https://cedar-records.example.invalid"
DIGEST_A = "ab" * 32
DIGEST_B = "ab" * 31 + "cd"
ONE = "Cedar Valley Civil Liberties Union"


def snapshot(root):
    """Every file under ``root`` except the build output, with bytes, mode and mtime (symlinks not followed)."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == "public" or relative.startswith("public/"):
            continue
        info = os.lstat(path)
        found[relative] = (None if path.is_dir() or path.is_symlink() else path.read_bytes(), info.st_mode, info.st_mtime_ns)
    return found


def tree(public):
    return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(Path(public).rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-supporters-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public_file = self.root / "content" / "supporters.json"
        self.private_file = self.root / "kit" / "supporters-consent.json"
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def add(self, name=ONE, kind="civil_liberties", **fields):
        values = dict(name=name, kind=kind, organization=True, confirmed_on="2026-09-01", how="email", today=TODAY)
        values.update(fields)
        return add_supporter(self.root, **values)

    def ledger(self, *digests):
        private = self.root / "private"
        private.mkdir(exist_ok=True)
        db = sqlite3.connect(private / "ledger.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS objects (sha256 TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO objects VALUES (?)", [(d,) for d in digests])
        db.commit()
        db.close()

    def stores(self):
        return json.loads(self.public_file.read_text("utf-8")), json.loads(self.private_file.read_text("utf-8"))

    def write_stores(self, public=None, private=None, mode=0o600):
        """Hand-written stores: ``public``/``private`` are documents or raw text."""
        for path, value in ((self.public_file, public), (self.private_file, private)):
            if value is None:
                continue
            path.parent.mkdir(exist_ok=True)
            path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        if private is not None:
            os.chmod(self.private_file, mode)

    def row(self, name=ONE, **fields):
        base = {"id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name, "kind": "community", "url": None,
                "joined_on": "2026-09-01", "organization": True}
        base.update(fields)
        return base

    def record(self, row, **fields):
        base = {"organization": True, "confirmed_on": row["joined_on"], "how": "email", "public_sha256": row_hash(row),
                "correspondence": None}
        base.update(fields)
        return base

    def good_pair(self, *names):
        rows = [self.row(name) for name in names]
        self.write_stores({"schema_version": 1, "supporters": rows},
                          {"schema_version": 1, "supporters": {r["id"]: self.record(r) for r in rows}})
        return rows

    def build(self, today=TODAY):
        build_site(self.root, today=today)
        return tree(self.root / "public")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "campaign_tool", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=120)

    def refused(self, pattern=None, **fields):
        """add_supporter refuses and leaves every byte, mode and mtime of the campaign as it was."""
        before = snapshot(self.root)
        with self.assertRaises(SupporterError) as caught:
            self.add(**fields)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        self.assertEqual(snapshot(self.root) == before, True, "a refused add changed the campaign")


class AddTests(Base):
    def test_add_binds_public_row_and_private_consent(self):
        self.ledger(DIGEST_A, "cd" * 32)
        results = []
        for number, kind in enumerate(KINDS):
            url = f"https://org{number}.example.invalid/about" if number % 2 == 0 else None
            results.append(self.add(f"Synthetic {kind.replace('_', ' ')} Alliance", kind, url=url,
                                    confirmed_on=f"2026-09-0{number + 1}", how=("email", "letter", "meeting", "form")[number % 4],
                                    correspondence="cd" * 6 if number == 0 else None))
        self.assertEqual({r["action"] for r in results}, {"added"})
        self.assertEqual(set().union(*(set(r) for r in results)), {"id", "action"})
        public, private = self.stores()
        self.assertEqual(public["schema_version"], 1)
        self.assertEqual(len(public["supporters"]), 7)
        for row in public["supporters"]:
            self.assertEqual(list(row), ["id", "name", "kind", "url", "joined_on", "organization"])
            self.assertIs(row["organization"], True)
            record = private["supporters"][row["id"]]
            self.assertEqual(list(record), ["organization", "confirmed_on", "how", "public_sha256", "correspondence"])
            self.assertEqual(record["public_sha256"], row_hash(row))
            self.assertEqual(record["confirmed_on"], row["joined_on"])
        by_kind = {r["kind"]: r for r in public["supporters"]}
        self.assertEqual(set(by_kind), set(KINDS))
        self.assertEqual(by_kind["civil_liberties"]["url"], "https://org0.example.invalid/about")
        self.assertIsNone(by_kind["community"]["url"])
        self.assertEqual(by_kind["civil_liberties"]["joined_on"], "2026-09-01")
        self.assertEqual(private["supporters"][by_kind["civil_liberties"]["id"]]["correspondence"], "cd" * 32)
        self.assertEqual(private["supporters"][by_kind["community"]["id"]]["correspondence"], None)
        self.assertEqual(oct(self.private_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.private_file.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(self.public_file.stat().st_mode & 0o777), "0o644")
        self.assertEqual([r["id"] for r in load_supporters(self.root, today=TODAY)], sorted(r["id"] for r in public["supporters"]))
        self.assertEqual(supporter_problems(self.root, today=TODAY), [])
        pair = consent_for_supporter(self.root, by_kind["civil_liberties"]["id"], today=TODAY)
        self.assertEqual(set(pair), {"public", "consent"})
        self.assertEqual(read_consent(self.root, today=TODAY), private["supporters"])
        files = self.build()
        for name, data in files.items():  # nothing private reaches the public site
            for secret in (DIGEST_A, "cd" * 32, "public_sha256", "confirmed_on", "supporters-consent"):
                self.assertNotIn(secret.encode(), data, name)
        self.assertNotIn("kit/", "".join(files))

    def test_cli_prints_only_id_and_action(self):
        self.ledger(DIGEST_A)
        result = self.cli("supporters", "add", "--directory", str(self.root), "--name", ONE, "--kind", "civil_liberties",
                          "--url", "https://cvclu.example.invalid/", "--organization", "--confirmed-on", "2026-09-01",
                          "--how", "meeting", "--correspondence", "ab" * 8)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(first, {"id": "cedar-valley-civil-liberties-union", "action": "added"})
        self.assertNotIn(DIGEST_A, result.stdout + result.stderr)
        self.assertNotIn("meeting", result.stdout)
        check = self.cli("content-check", "--directory", str(self.root))
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        removed = self.cli("supporters", "remove", "--directory", str(self.root), "--id", first["id"])
        self.assertEqual(json.loads(removed.stdout.splitlines()[0]), {"id": first["id"], "action": "removed"})
        self.assertEqual(self.stores()[0]["supporters"], [])
        self.assertEqual(self.stores()[1]["supporters"], {})

    def test_cli_without_the_attestation_flag_writes_nothing(self):
        result = self.cli("supporters", "add", "--directory", str(self.root), "--name", ONE, "--kind", "other",
                          "--confirmed-on", "2026-09-01", "--how", "form")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--organization", result.stderr)
        self.assertFalse(self.public_file.exists())
        self.assertFalse((self.root / "kit").exists())

    def test_attestation_and_privacy_refusals_write_nothing(self):
        self.ledger(DIGEST_A, DIGEST_B)
        self.refused("organization", organization=False)
        self.refused("organization", organization=None)
        self.refused("organization", organization="true")
        self.refused("organization", organization=1)
        for name in ("", " Padded Org", "Padded Org ", "x" * 121, "Line\nBreak Org", "Tab\tOrg", "Bold <b>Org</b>",
                     "jane@example.invalid", "Call 555-123-4567 Org", "Call (555) 123 4567",
                     "+1 555 123 4567 Org", "Contact: Jane Doe", "Phone: Org", "Name: Jane", "Person: Jane Doe",
                     "mailto: team", "tel: 5551234", "\uff4a\uff41\uff4e\uff45\uff20example Org", "4H Club", "!!!",
                     "x" * 65):
            with self.subTest(name=name):
                self.refused(name=name)
        self.refused("kind", kind="person")
        self.refused("kind", kind="Civil_Liberties")
        self.refused("how", how="phone")
        for confirmed in ("2026-02-30", "2026-9-1", "2026-09-01T00:00", "1999-12-31", "2026-10-02", "yesterday", "", None, "\uff12\uff10\uff12\uff16-09-01"):
            with self.subTest(confirmed=confirmed):
                self.refused(confirmed_on=confirmed)
        # Assemble synthetic userinfo at runtime; still exercise credential-bearing URL rejection.
        userinfo_url = "https://" + "user:pw" + "@x.example.invalid/"
        for url in ("http://x.example.invalid/", userinfo_url, "https://x.example.invalid/?a=1",
                    "https://x.example.invalid/#f", "mailto:team@example.invalid", "tel:5551234567", "https://localhost/",
                    "https://x.example.invalid:8443/", "https://x.example.invalid/tel:5551234567",
                    "https://x.example.invalid/contact:jane", "https://x.example.invalid/%6Aane%40example.invalid",
                    "https://x.example.invalid/5551234567", "https://x.example.invalid/" + "a" * 300, "",
                    "https://x.example.invalid/a b", "https://x.example.invalid/<b>"):
            with self.subTest(url=url):
                self.refused(url=url)
        self.refused("correspondence", correspondence="abcd")
        self.refused("correspondence", correspondence="XYZ" * 5)
        self.refused("correspondence", correspondence="ab" * 6)  # ambiguous: two ingested hashes start ab...
        self.refused("correspondence", correspondence="ef" * 6)  # not ingested
        self.assertFalse(self.public_file.exists())

    def test_duplicates_and_capacity_refused(self):
        self.add("Cedar Org")
        before = snapshot(self.root)
        for name in ("Cedar Org", "cedar org", "CEDAR   ORG", "Cedar-Org", "\uff23\uff25\uff24\uff21\uff32 ORG", "cedar ORG!"):
            with self.subTest(name=name):
                with self.assertRaises(SupporterError):
                    self.add(name)
        self.assertEqual(snapshot(self.root) == before, True)
        rows = [self.row(f"Org {chr(97 + n // 26)}{chr(97 + n % 26)}") for n in range(200)]
        self.write_stores({"schema_version": 1, "supporters": rows},
                          {"schema_version": 1, "supporters": {r["id"]: self.record(r) for r in rows}})
        self.assertEqual(len(load_supporters(self.root, today=TODAY)), 200)
        self.refused("at most 200", name="One More Org")

    def test_store_documents_with_unknown_or_unsafe_content_are_refused(self):
        row = self.row()
        good_private = {"schema_version": 1, "supporters": {row["id"]: self.record(row)}}
        public_variants = {
            "bool version": {"schema_version": True, "supporters": [row]},
            "float version": {"schema_version": 1.0, "supporters": [row]},
            "unknown top key": {"schema_version": 1, "supporters": [row], "contact": "x"},
            "not a list": {"schema_version": 1, "supporters": {}},
            "email field": {"schema_version": 1, "supporters": [dict(row, email="a@example.invalid")]},
            "person field": {"schema_version": 1, "supporters": [dict(row, person="Jane Doe")]},
            "phone field": {"schema_version": 1, "supporters": [dict(row, phone="5551234567")]},
            "missing url": {"schema_version": 1, "supporters": [{k: v for k, v in row.items() if k != "url"}]},
            "organization 1": {"schema_version": 1, "supporters": [dict(row, organization=1)]},
            "organization false": {"schema_version": 1, "supporters": [dict(row, organization=False)]},
            "organization string": {"schema_version": 1, "supporters": [dict(row, organization="true")]},
            "mailto url": {"schema_version": 1, "supporters": [dict(row, url="mailto:a@example.invalid")]},
            "wrong id": {"schema_version": 1, "supporters": [dict(row, id="other")]},
            "email name": {"schema_version": 1, "supporters": [dict(row, name="a@example.invalid", id="a-example-invalid")]},
            "duplicate id": {"schema_version": 1, "supporters": [row, row]},
            "duplicate name": {"schema_version": 1, "supporters": [row, dict(row, name=row["name"].upper(), id="x")]},
            "too many": {"schema_version": 1, "supporters": [self.row(f"Org {n}") for n in range(201)]},
            "not an object": [row],
        }
        for label, document in public_variants.items():
            with self.subTest(label):
                self.write_stores(document, good_private)
                self.assertTrue(supporter_problems(self.root, today=TODAY), label)
                with self.assertRaises(ContentError):
                    load_supporters(self.root, today=TODAY)
                with self.assertRaises(SupporterError):
                    self.add("Fresh Org")
        raw = '{"schema_version": 1, "supporters": [], "supporters": []}'
        for text in (raw, '{"schema_version": 1, "supporters": [NaN]}', "{", "", "\xff"):
            with self.subTest(raw=text):
                self.write_stores(text, good_private)
                with self.assertRaises(SupporterError):
                    load_supporters(self.root, today=TODAY)
        self.write_stores('{"schema_version": 1, "supporters": [], "x": "' + "a" * (1024 * 1024) + '"}', good_private)
        with self.assertRaisesRegex(SupporterError, "too large"):
            load_supporters(self.root, today=TODAY)
        public = {"schema_version": 1, "supporters": [row]}
        private_variants = {
            "bool version": {"schema_version": True, "supporters": good_private["supporters"]},
            "extra record key": {"schema_version": 1, "supporters": {row["id"]: dict(self.record(row), note="x")}},
            "person key": {"schema_version": 1, "supporters": {row["id"]: dict(self.record(row), person="Jane")}},
            "bad how": {"schema_version": 1, "supporters": {row["id"]: self.record(row, how="phone")}},
            "bad hash": {"schema_version": 1, "supporters": {row["id"]: self.record(row, public_sha256="AB" * 32)}},
            "bad correspondence": {"schema_version": 1, "supporters": {row["id"]: self.record(row, correspondence="abcd")}},
            "list": {"schema_version": 1, "supporters": []},
        }
        for label, document in private_variants.items():
            with self.subTest("private " + label):
                self.write_stores(public, document)
                with self.assertRaises(SupporterError):
                    load_supporters(self.root, today=TODAY)
                with self.assertRaises(SupporterError):
                    read_consent(self.root, today=TODAY)
        self.write_stores(public, '{"schema_version": 1, "supporters": {"a": {}, "a": {}}}')
        with self.assertRaises(SupporterError):
            read_consent(self.root, today=TODAY)


class ConsentTests(Base):
    def test_consent_is_not_inferred_or_reused_after_edit(self):
        self.add("Alpha Neighbors", "neighborhood", url="https://alpha.example.invalid/")
        self.add("Beta Congregation", "faith")
        self.add("Gamma Workers", "labor")
        good = self.build()
        before = tree(self.root / "public")
        public = json.loads(self.public_file.read_text("utf-8"))
        private = json.loads(self.private_file.read_text("utf-8"))

        def check(label, public_doc, private_doc, ids, mode=0o600):
            with self.subTest(label):
                self.write_stores(public_doc, private_doc, mode)
                with self.assertRaises(ContentError):
                    build_site(self.root, today=TODAY)
                self.assertEqual(tree(self.root / "public") == before, True, "public/ changed after a refused build")
                lines = supporter_problems(self.root, today=TODAY)
                for supporter_id in ids:
                    self.assertTrue(any(supporter_id in line for line in lines), (supporter_id, lines))
                self.assertEqual(lines, sorted(lines))
                reported = content_problems(self.root, limit=None)
                for line in lines:
                    self.assertIn(line, reported)

        def edited(index, **fields):
            rows = json.loads(json.dumps(public["supporters"]))
            rows[index].update(fields)
            return {"schema_version": 1, "supporters": rows}

        ids = [r["id"] for r in public["supporters"]]
        self.assertEqual(ids, sorted(ids))
        alpha, beta, gamma = ids
        dropped = json.loads(json.dumps(private))
        del dropped["supporters"][beta]
        check("missing record", public, dropped, [beta])
        check("other row's record is not reused", public, {"schema_version": 1, "supporters": {
            alpha: private["supporters"][alpha], beta: private["supporters"][alpha], gamma: private["supporters"][gamma]}}, [beta])
        check("url edited", edited(0, url="https://changed.example.invalid/"), private, [alpha])
        check("url removed", edited(0, url=None), private, [alpha])
        check("kind edited", edited(1, kind="community"), private, [beta])
        check("date edited", edited(2, joined_on="2026-08-01"), private, [gamma])
        check("empty private", public, {"schema_version": 1, "supporters": {}}, ids)
        # a renamed organization changes its id, so the old record covers neither id
        renamed = edited(1, name="Beta Congregation Two", id="beta-congregation-two")
        with self.subTest("renamed"):
            self.write_stores(renamed, private)
            with self.assertRaises(ContentError):
                build_site(self.root, today=TODAY)
        for label, change in (("attestation false", {"organization": False}), ("attestation missing", None),
                              ("confirmed date moved", {"confirmed_on": "2026-08-01"}),
                              ("future date", {"confirmed_on": "2999-01-01"}), ("bad how", {"how": "phone"})):
            doc = json.loads(json.dumps(private))
            if change is None:
                del doc["supporters"][beta]["organization"]
            else:
                doc["supporters"][beta].update(change)
            check(label, public, doc, [beta])
        future = [dict(public["supporters"][0], joined_on="2999-01-01")]
        future_row = future[0]
        check("future joined date with matching hash", {"schema_version": 1, "supporters": future},
              {"schema_version": 1, "supporters": {alpha: self.record(future_row)}}, [alpha])
        check("malformed private store", public, "{not json", ids)
        check("permissive private store", public, private, ids, mode=0o644)
        check("group readable private store", public, private, ids, mode=0o640)
        # restoring the pair builds again, byte-identical to the first build
        self.write_stores(public, private)
        self.assertEqual(self.build() == good, True)

    def test_matching_id_alone_is_not_permission_and_read_only_checks_do_not_write(self):
        row = self.row("Delta Business Group", kind="business")
        self.write_stores({"schema_version": 1, "supporters": [row]})  # public row, no private store at all
        self.assertFalse(self.private_file.exists())
        before = snapshot(self.root)
        with self.assertRaisesRegex(ContentError, "no consent record"):
            load_content(self.root, TODAY)
        self.assertEqual(supporter_problems(self.root, today=TODAY),
                         [f"content/supporters.json: {row['id']}: no consent record"])
        lines = content_problems(self.root, limit=None)
        self.assertIn(f"content/supporters.json: {row['id']}: no consent record", lines)
        result = self.cli("content-check", "--directory", str(self.root), "--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        records = [r for r in json.loads(result.stdout)["problems"] if "supporters" in (r["file"] or "")]
        self.assertEqual(records, [{"file": "content/supporters.json", "message": f"{row['id']}: no consent record"}])
        self.assertFalse(self.private_file.exists())
        self.assertEqual(snapshot(self.root) == before, True, "a read-only check wrote or touched a store")

    def test_problems_name_ids_without_values(self):
        private_url = "https://leak-check.example.invalid/private-path"
        row = self.row("Epsilon Faith Circle", kind="faith", url=private_url)
        good = {"schema_version": 1, "supporters": {row["id"]: self.record(row)}}
        self.write_stores({"schema_version": 1, "supporters": [dict(row, url=private_url + "-edited")]}, good)
        text = "\n".join(supporter_problems(self.root, today=TODAY))
        self.assertIn(row["id"], text)
        self.assertNotIn("leak-check", text)
        self.assertNotIn(row["name"], text)
        self.write_stores({"schema_version": 1, "supporters": [dict(row, email="jane@example.invalid", url="bad")]}, good)
        text = "\n".join(supporter_problems(self.root, today=TODAY))
        self.assertNotIn("jane", text)
        self.assertNotIn("bad", text.replace("bad ", ""))


class WriteTests(Base):
    def flaky(self, failing):
        real, calls = os.replace, []

        def replace(source, target, *args, **kwargs):
            calls.append(Path(target).name)
            if len(calls) in failing:
                raise OSError("injected")
            return real(source, target, *args, **kwargs)
        replace.calls = calls
        return replace

    def temporaries(self):
        return [p for p in self.root.rglob(".supporters-*")]

    def test_pair_write_rollback_and_unsafe_paths(self):
        self.add("First Org")
        before = snapshot(self.root)
        modes = (self.public_file.stat().st_mode, self.private_file.stat().st_mode)
        flaky = self.flaky({2})
        with mock.patch("campaign_tool.supporters.os.replace", flaky):
            with self.assertRaisesRegex(SupporterError, "previous files were restored"):
                self.add("Second Org")
        self.assertEqual(flaky.calls, ["supporters-consent.json", "supporters.json", "supporters-consent.json"])
        after = snapshot(self.root)
        for name in ("content/supporters.json", "kit/supporters-consent.json"):
            self.assertEqual(after[name][:2], before[name][:2], name)  # bytes and mode
        self.assertEqual((self.public_file.stat().st_mode, self.private_file.stat().st_mode), modes)
        self.assertEqual(self.temporaries(), [])
        self.assertEqual(sorted(after), sorted(before))
        # the rollback itself failing is an explicit error, never a success
        flaky = self.flaky({2, 3})
        with mock.patch("campaign_tool.supporters.os.replace", flaky):
            with self.assertRaisesRegex(SupporterError, "could not be undone for kit/supporters-consent.json"):
                self.add("Third Org")
        self.assertEqual(self.temporaries(), [])
        self.assertEqual(self.stores()[1]["supporters"].keys() - {"first-org"}, {"third-org"})  # saved pair needed
        self.public_file.write_bytes(before["content/supporters.json"][0])
        self.private_file.write_bytes(before["kit/supporters-consent.json"][0])
        os.chmod(self.private_file, 0o600)
        # a first failure leaves the old pair
        flaky = self.flaky({1})
        with mock.patch("campaign_tool.supporters.os.replace", flaky):
            with self.assertRaises(SupporterError):
                self.add("Fourth Org")
        self.assertEqual(snapshot(self.root)["content/supporters.json"][0], before["content/supporters.json"][0])
        self.assertEqual(snapshot(self.root)["kit/supporters-consent.json"][0], before["kit/supporters-consent.json"][0])
        # remove: public first, then private; a failed second step restores the public file
        flaky = self.flaky({2})
        with mock.patch("campaign_tool.supporters.os.replace", flaky):
            with self.assertRaises(SupporterError):
                remove_supporter(self.root, "first-org", today=TODAY)
        self.assertEqual(flaky.calls[0], "supporters.json")
        self.assertEqual(self.public_file.read_bytes(), before["content/supporters.json"][0])
        self.assertEqual(self.private_file.read_bytes(), before["kit/supporters-consent.json"][0])
        self.assertEqual(self.temporaries(), [])

    def test_failed_first_add_leaves_nothing_behind(self):
        flaky = self.flaky({2})
        with mock.patch("campaign_tool.supporters.os.replace", flaky):
            with self.assertRaises(SupporterError):
                self.add()
        self.assertFalse(self.public_file.exists())
        self.assertFalse((self.root / "kit").exists())  # the folder this add created is removed again
        self.assertEqual(self.temporaries(), [])
        self.add()  # and the same add then succeeds
        self.assertEqual(len(load_supporters(self.root, today=TODAY)), 1)

    def test_existing_kit_folder_is_secured_and_kept(self):
        kit = self.root / "kit"
        kit.mkdir(mode=0o755)
        os.chmod(kit, 0o755)
        (kit / "agencies.json").write_text("[]", encoding="utf-8")
        self.add()
        self.assertEqual(oct(kit.stat().st_mode & 0o777), "0o700")
        self.assertEqual((kit / "agencies.json").read_text(), "[]")

    def test_unsafe_paths_are_refused_and_nothing_outside_changes(self):
        sentinel = self.outside / "target.json"
        sentinel.write_text("outside", encoding="utf-8")
        outside_before = sentinel.read_bytes()
        cases = {}

        def public_link():
            os.symlink(sentinel, self.public_file)

        def public_dangling():
            os.symlink(self.outside / "missing.json", self.public_file)

        def public_dir():
            self.public_file.mkdir()

        def public_hardlink():
            self.public_file.write_text('{"schema_version": 1, "supporters": []}', encoding="utf-8")
            os.link(self.public_file, self.outside / "hard.json")

        def kit_link():
            os.symlink(self.outside, self.root / "kit")

        def kit_dangling():
            os.symlink(self.tmp / "nowhere", self.root / "kit")

        def kit_file():
            (self.root / "kit").write_text("not a folder", encoding="utf-8")

        def private_link():
            (self.root / "kit").mkdir()
            os.symlink(sentinel, self.private_file)

        def private_dangling():
            (self.root / "kit").mkdir()
            os.symlink(self.outside / "missing.json", self.private_file)

        def private_dir():
            self.private_file.mkdir(parents=True)

        def private_hardlink():
            (self.root / "kit").mkdir()
            self.private_file.write_text('{"schema_version": 1, "supporters": {}}', encoding="utf-8")
            os.chmod(self.private_file, 0o600)
            os.link(self.private_file, self.outside / "hard-private.json")

        def private_open_mode():
            (self.root / "kit").mkdir()
            self.private_file.write_text('{"schema_version": 1, "supporters": {}}', encoding="utf-8")
            os.chmod(self.private_file, 0o644)

        cases.update({"public link": public_link, "public dangling": public_dangling, "public folder": public_dir,
                      "public hardlink": public_hardlink, "kit link": kit_link, "kit dangling": kit_dangling,
                      "kit file": kit_file, "private link": private_link, "private dangling": private_dangling,
                      "private folder": private_dir, "private hardlink": private_hardlink,
                      "private permissive": private_open_mode})
        for label, setup in cases.items():
            with self.subTest(label):
                for leftover in (self.root / "kit", self.public_file):
                    if leftover.is_symlink() or leftover.is_file():
                        leftover.unlink()
                    elif leftover.is_dir():
                        shutil.rmtree(leftover)
                for stray in self.outside.iterdir():
                    if stray != sentinel:
                        stray.unlink()
                setup()
                before = snapshot(self.root)
                outside_state = {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.outside.iterdir()}
                with self.assertRaises(SupporterError):
                    self.add()
                with self.assertRaises(SupporterError):
                    remove_supporter(self.root, "cedar-valley-civil-liberties-union", today=TODAY)
                self.assertEqual(snapshot(self.root) == before, True)
                self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mode) for p in self.outside.iterdir()}, outside_state)
                self.assertEqual(sentinel.read_bytes(), outside_before)
                self.assertEqual(self.temporaries(), [])
        # build and content-check refuse the same stores instead of following them
        for leftover in (self.root / "kit", self.public_file):
            if leftover.is_symlink() or leftover.is_file():
                leftover.unlink()
            elif leftover.is_dir():
                shutil.rmtree(leftover)
        os.symlink(sentinel, self.public_file)
        self.assertTrue(supporter_problems(self.root, today=TODAY))
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(sentinel.read_bytes(), outside_before)

    def test_content_without_a_folder_is_refused(self):
        shutil.rmtree(self.root / "content")
        with self.assertRaisesRegex(SupporterError, "content/ does not exist"):
            self.add()
        self.assertFalse((self.root / "kit").exists())

    def test_orphan_consent_blocks_further_changes(self):
        self.add("Orphan Test Org")
        public = json.loads(self.public_file.read_text("utf-8"))
        public["supporters"] = []
        self.public_file.write_text(json.dumps(public), encoding="utf-8")  # an interrupted add/remove: record only
        before = snapshot(self.root)
        with self.assertRaisesRegex(SupporterError, "orphan consent record.*orphan-test-org.*restore the saved pair"):
            self.add("Another Org")
        with self.assertRaisesRegex(SupporterError, "orphan consent record"):
            remove_supporter(self.root, "orphan-test-org", today=TODAY)
        self.assertEqual(snapshot(self.root) == before, True)
        self.assertEqual(supporter_problems(self.root, today=TODAY), [])  # an orphan record never lists anyone


class RemoveTests(Base):
    def test_remove_both_and_no_file_compatibility(self):
        old = self.build()  # the starter with no supporters anywhere
        self.assertNotIn("supporters.html", old)
        for name, data in old.items():
            self.assertNotIn(b"supporters", data.lower(), name)
        self.assertFalse(self.public_file.exists())
        before = snapshot(self.root)
        with self.assertRaisesRegex(SupporterError, "unknown supporter id"):
            remove_supporter(self.root, "nobody", today=TODAY)
        self.assertEqual(snapshot(self.root) == before, True)
        self.assertFalse((self.root / "kit").exists())  # no consent store auto-created
        self.add("Keep Me Org", "community")
        self.add("Drop Me Org", "business")
        (self.root / "private").mkdir(exist_ok=True)
        original = self.root / "private" / "original-record.pdf"
        original.write_bytes(b"synthetic original")
        with self.assertRaisesRegex(SupporterError, "unknown supporter id"):
            remove_supporter(self.root, "keep-me", today=TODAY)
        with self.assertRaisesRegex(SupporterError, "unknown supporter id"):
            remove_supporter(self.root, None, today=TODAY)
        self.assertEqual(remove_supporter(self.root, "drop-me-org", today=TODAY), {"id": "drop-me-org", "action": "removed"})
        public, private = self.stores()
        self.assertEqual([r["id"] for r in public["supporters"]], ["keep-me-org"])
        self.assertEqual(list(private["supporters"]), ["keep-me-org"])
        self.assertEqual(oct(self.private_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(original.read_bytes(), b"synthetic original")
        files = self.build()
        self.assertIn(b"Keep Me Org", files["supporters.html"])
        self.assertNotIn(b"Drop Me Org", b"".join(files.values()))
        # removing the last row: no page, no footer link, no sitemap line; both stores stay as empty lists
        remove_supporter(self.root, "keep-me-org", today=TODAY)
        self.assertEqual(self.stores(), ({"schema_version": 1, "supporters": []}, {"schema_version": 1, "supporters": {}}))
        files = self.build()
        self.assertNotIn("supporters.html", files)
        self.assertEqual(files == old, True, "an empty list must build byte-identically to no file")
        self.assertEqual(original.read_bytes(), b"synthetic original")

    def test_read_only_operations_keep_bytes_and_mtime(self):
        self.add()
        self.build()
        before = snapshot(self.root)
        load_supporters(self.root, today=TODAY)
        read_consent(self.root, today=TODAY)
        supporter_problems(self.root, today=TODAY)
        content_problems(self.root, limit=None)
        self.assertEqual(self.cli("content-check", "--directory", str(self.root)).returncode, 0)
        self.assertEqual(snapshot(self.root) == before, True)


class PageTests(Base):
    def set_base(self):
        path = self.root / "content" / "site.json"
        site = json.loads(path.read_text("utf-8"))
        site["base_url"] = BASE
        path.write_text(json.dumps(site), encoding="utf-8")

    def test_group_order_reserved_paths_and_labels(self):
        self.set_base()
        without = self.build()
        self.ledger(DIGEST_A)
        self.add("Zeta Hardware & Supply", "business", url="https://zeta.example.invalid/shop", confirmed_on="2026-09-09")
        self.add("alpha Merchants", "business", confirmed_on="2026-09-02", how="letter")
        self.add("Beta Prayer Circle", "faith", confirmed_on="2026-08-30", how="meeting")
        self.add("Cedar Neighbors", "neighborhood", confirmed_on="2026-09-03", how="form")
        self.add("Civil Rights Forum", "civil_liberties", confirmed_on="2026-09-04")
        self.add("Dock Workers Local", "labor", confirmed_on="2026-09-05")
        self.add("Misc Collective", "other", confirmed_on="2026-09-06")
        self.add("Block Association", "community", confirmed_on="2026-09-07")
        files = self.build()
        page = files["supporters.html"].decode()
        headings = re.findall(r"<h3>(.*?)</h3>", page)
        self.assertEqual(headings, ["Civil liberties", "Community", "Faith", "Labor", "Neighborhood", "Business", "Other"])
        business = re.search(r"<h3>Business</h3><ul>(.*?)</ul>", page).group(1)
        self.assertLess(business.index("alpha Merchants"), business.index("Zeta Hardware"))  # casefolded order
        self.assertIn("Zeta Hardware &amp; Supply", business)
        self.assertNotIn("Zeta Hardware & Supply", page)
        self.assertIn('<a href="https://zeta.example.invalid/shop" rel="noopener noreferrer">Zeta Hardware &amp; Supply</a>', business)
        self.assertIn('Joined <time datetime="2026-09-09">2026-09-09</time>', business)
        self.assertIn('Joined <time datetime="2026-09-02">2026-09-02</time>', business)
        self.assertIn("<li>alpha Merchants · Joined", business)  # no url: plain text, not a link
        self.assertIn("not a legal endorsement", page)
        self.assertIn("<title>Supporting organizations | ", page)
        # every page links it from the footer; the sitemap lists it (base_url set)
        link = '<a href="/supporters.html">Supporting organizations</a>'
        html_files = [n for n in files if n.endswith(".html")]
        self.assertGreater(len(html_files), 8)
        for name in html_files:
            self.assertIn(link, files[name].decode(), name)
        for name in without:
            if name.endswith(".html"):
                self.assertNotIn("supporters", without[name].decode(), name)
        self.assertIn(f"<loc>{BASE}/supporters.html</loc>", files["sitemap.xml"].decode())
        self.assertNotIn("supporters", without["sitemap.xml"].decode())
        # the other exports and the feed are unchanged
        for name in without:
            if name.startswith("data/") or name in ("feed.xml", "style.css", "search.js", "app.js"):
                self.assertEqual(files[name] == without[name], True, name)
        # a build without base_url has no sitemap but still a page and a footer link
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text("utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        plain = self.build()
        self.assertIn("supporters.html", plain)
        self.assertNotIn("sitemap.xml", plain)
        # no individual or contact data anywhere in the public site
        blob = b"\n".join(files.values()).decode("utf-8", "replace")
        self.assertNotIn("kit/supporters", blob)
        self.assertNotIn("mailto:jane", blob)
        self.assertNotIn("public_sha256", blob)
        self.assertNotIn(DIGEST_A, blob)

    def test_reserved_slugs_hold_without_a_file(self):
        self.assertIn("supporters", STARTER_SLUGS)
        self.assertIn("supporters.html", GENERATED_TOP_LEVEL)
        pages = self.root / "content" / "pages"
        pages.mkdir()
        (pages / "supporters.json").write_text(json.dumps({"title": "Backers", "body_md": "x"}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"^pages/supporters\.json: slug supporters is reserved$"):
            load_pages(self.root / "content", set())
        with self.assertRaisesRegex(ContentError, "supporters is reserved"):
            build_site(self.root, today=TODAY)
        (pages / "supporters.json").unlink()
        finding = json.loads(next((self.root / "content" / "findings").glob("*.json")).read_text("utf-8"))
        finding["slug"] = "supporters"
        (self.root / "content" / "findings" / "supporters.json").write_text(json.dumps(finding), encoding="utf-8")
        with self.assertRaises(ContentError):
            load_content(self.root, TODAY)
        self.assertEqual(supporters_module.PUBLIC_LABEL, "content/supporters.json")

    def test_redirect_cannot_take_the_generated_page(self):
        (self.root / "content" / "redirects.json").write_text(
            json.dumps({"schema_version": 1, "redirects": [{"from": "/supporters.html", "to": "/about.html"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"redirects\.json.*/supporters\.html"):
            build_site(self.root, today=TODAY)

    def test_every_supporters_label_can_be_overridden(self):
        keys = ["supporters.title", "supporters.description", "supporters.joined"] + [f"supporters.kind.{k}" for k in KINDS]
        for number, kind in enumerate(KINDS):
            self.add(f"Label Check {kind.replace('_', ' ')}", kind)
        markers = {key: f"X{key}X" for key in keys}
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": markers}), encoding="utf-8")
        files = self.build()
        page = files["supporters.html"].decode()
        for key in keys:
            self.assertIn(f"X{key}X", page, key)
        for english in ("Supporting organizations", "Joined", "legal endorsement", "Civil liberties"):
            self.assertNotIn(english, page)
        self.assertIn('<a href="/supporters.html">Xsupporters.titleX</a>', files["index.html"].decode())
        # the Spanish draft's wording is accepted and used
        shutil.copy(REPO_ROOT / "examples" / "labels" / "es.json", self.root / "content" / "labels.json")
        spanish = self.build()["supporters.html"].decode()
        self.assertIn("Organizaciones que apoyan", spanish)
        self.assertIn("Se sumó el <time datetime=", spanish)

    def test_site_model_carries_public_rows_only(self):
        self.ledger(DIGEST_A)
        self.add(correspondence="ab" * 6)
        model = load_content(self.root, TODAY)
        self.assertEqual(list(model["supporters"][0]), ["id", "name", "kind", "url", "joined_on", "organization"])
        self.assertNotIn(DIGEST_A, json.dumps(model["supporters"]))
        self.assertNotIn("supporters", load_content(self._bare(), TODAY))

    def _bare(self):
        other = self.tmp / "bare"
        shutil.copytree(EXAMPLE, other, ignore=shutil.ignore_patterns("public"))
        return other


if __name__ == "__main__":
    unittest.main()
