"""DF1109B-13: ``content agency-profile transparency add`` through the real CLI, the build's loader and the inherited
installer; synthetic agencies in temporary storage, nothing is fetched.

Run from the repository root: python3 -B -m unittest tests.test_content_profile_transparency -q
"""
import contextlib
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content_profile as cp  # noqa: E402
from campaign_tool.content import ContentError, TRANSPARENCY_FIELDS, load_agencies  # noqa: E402

A, B, UNVERIFIED, NOSLUG, OFF = "ca-alpha-pd", "ca-beta-so", "ca-unverified-pd", "ca-noslug-pd", "ca-off-pd"
PORTAL = "https://transparency.flocksafety.com/"
CLOSING = "Content only; nothing was built, sent or deployed.\nThe portal is the vendor's report, not verified transfers.\n"
NO_NETWORK = AssertionError("no network in these tests")


def day(offset):
    """A calendar day ``offset`` days after 2025-01-01 (always before today)."""
    return (date(2025, 1, 1) + timedelta(days=offset)).isoformat()


def agency(agency_id, **values):
    return {"agency_id": agency_id, "name": f"{agency_id} (fictional)", "kind": "police", "verified": True,
            "verification": {"source_url": "https://example.invalid/contact", "checked_on": "2026-01-02",
                             "fields": ["records_url"]},
            "records_url": "https://example.invalid/records"} | values


def fixture():
    return [agency(A, slug="alpha", flock_transparency_slug="alpha-flock", profile={"status": "Records requested"}),
            agency(B, slug="beta-page", flock_transparency_slug="beta-flock", profile={"summary": "Ünïcode ✓"}),
            agency(UNVERIFIED, verified=False, flock_transparency_slug="unverified-flock"),
            agency(NOSLUG),
            {"agency_id": OFF, "name": "Deselected", "selected": False}]


def snapshot(offset, **numbers):
    return {"checked_on": day(offset), "source_url": PORTAL + "alpha-flock"} | numbers


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class TransparencyTests(unittest.TestCase):
    def setUp(self):
        for target in ((socket, "socket"), (socket, "create_connection"), (urllib.request, "urlopen")):
            patch = mock.patch.object(*target, side_effect=NO_NETWORK)
            patch.start()
            self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-transparency-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content" / "agencies.json"
        self.write(fixture())

    def write(self, document):
        self.target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def stored(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def history(self, agency_id=A):
        return next(a for a in self.stored() if a["agency_id"] == agency_id)["profile"].get("transparency")

    def seed(self, count, agency_id=A):
        doc = self.stored()
        profile = next(a for a in doc if a["agency_id"] == agency_id).setdefault("profile", {})
        profile["transparency"] = [snapshot(offset) for offset in range(count * 10, 0, -10)]
        self.write(doc)
        return profile["transparency"]

    def tree(self):
        if not self.target.parent.exists():
            return {}
        return {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.target.parent.iterdir()}

    def add(self, checked_on, *flags, agency_id=A):
        return run("content", "agency-profile", "transparency", "add", "--directory", self.root, "--agency", agency_id,
                   "--checked-on", checked_on, *flags)

    def refused(self, needle, checked_on, *flags, agency_id=A, code=1):
        before = self.tree()
        result = self.add(checked_on, *flags, agency_id=agency_id)
        self.assertEqual((result[0], result[1]), (code, ""), result)
        self.assertIn(needle, result[2])
        if code == 1:
            self.assertIn("nothing was written", result[2])
        self.assertEqual(self.tree(), before)
        return result

    def test_omitted_numbers_stay_absent_and_explicit_zero_is_integer(self):
        flags = {key: "--" + key.replace("_", "-") for key in TRANSPARENCY_FIELDS}
        cases = [({}, {}), ({"cameras": "0"}, {"cameras": 0})]
        cases += [({key: "7"}, {key: 7}) for key in TRANSPARENCY_FIELDS]
        cases += [({key: "0"}, {key: 0}) for key in TRANSPARENCY_FIELDS]
        cases += [({key: "0" for key in TRANSPARENCY_FIELDS}, {key: 0 for key in TRANSPARENCY_FIELDS}),
                  ({"cameras": "007", "hotlist_alerts": "0", "retention_days": "30"},
                   {"cameras": 7, "hotlist_alerts": 0, "retention_days": 30})]
        for number, (typed, expected) in enumerate(cases):
            with self.subTest(typed=typed):
                self.target.write_text(json.dumps(self._clean(), indent=2) + "\n")
                args = [item for key, value in typed.items() for item in (flags[key], value)]
                code, out, err = self.add(day(30), *args)
                self.assertEqual((code, err), (0, ""))
                stored = self.history()[0]
                self.assertEqual(stored, {"checked_on": day(30), "source_url": PORTAL + "alpha-flock"} | expected)
                for key, value in expected.items():
                    self.assertIs(type(value), int)
                    self.assertIs(type(stored[key]), int)
                self.assertEqual(list(stored)[:2], ["checked_on", "source_url"])
                self.assertEqual(set(stored) & set(TRANSPARENCY_FIELDS), set(expected))  # absent keys are not null keys

    def _clean(self):
        doc = self.stored()
        for entry in doc:
            if isinstance(entry.get("profile"), dict):
                entry["profile"].pop("transparency", None)
        return doc

    def test_prepend_keeps_raw_history_and_unrelated_fields(self):
        for count in (0, 1, 23):
            with self.subTest(count=count):
                self.target.write_text(json.dumps(self._clean(), indent=2) + "\n")
                old = self.seed(count) if count else []
                if count:  # a legacy null number, an archive pair and a source-only snapshot survive exactly as stored
                    doc = self.stored()
                    rows = next(a for a in doc if a["agency_id"] == A)["profile"]["transparency"]
                    rows[-1].update(cameras=None)
                    rows[0] = {"checked_on": rows[0]["checked_on"], "source_url": rows[0]["source_url"] + "/",
                               "archived_url": "https://archive.ph/synthetic", "archived_on": "2026-01-03"}
                    old = copy.deepcopy(rows)
                    self.write(doc)
                before = self.stored()
                code, out, err = self.add(day(500), "--cameras", "3")
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, f"Transparency snapshot added: {A}, checked {day(500)}; {count + 1} snapshots stored.\n" + CLOSING)
                after = self.stored()
                self.assertEqual(after[0]["profile"]["transparency"][1:], old)
                self.assertEqual(after[0]["profile"]["transparency"][0], snapshot(500, cameras=3))
                after[0]["profile"].pop("transparency")
                before[0]["profile"].pop("transparency", None)
                self.assertEqual(after, before)  # every other agency, key, inactive row and the profile's other fields
                load_agencies(self.stored(), "content/agencies.json")

    def test_other_profile_fields_and_absent_profile(self):
        doc = self.stored()
        doc[3] = agency(NOSLUG, flock_transparency_slug="noslug-flock")  # no profile at all
        self.write(doc)
        self.assertEqual(self.add(day(5), agency_id=NOSLUG)[0], 0)
        self.assertEqual(self.history(NOSLUG), [{"checked_on": day(5), "source_url": PORTAL + "noslug-flock"}])
        doc = self.stored()
        doc[0]["profile"] = []
        self.write(doc)
        self.refused("profile", day(6), code=1)

    def test_verified_slug_is_only_source_url(self):
        self.assertEqual(self.add(day(1), agency_id=A)[0], 0)
        self.assertEqual(self.add(day(1), agency_id=B)[0], 0)
        self.assertEqual(self.history(A)[0]["source_url"], PORTAL + "alpha-flock")
        self.assertEqual(self.history(B)[0]["source_url"], PORTAL + "beta-flock")  # not the agency page slug
        for agency_id in (UNVERIFIED, NOSLUG):
            self.refused("needs a verified agency with a flock_transparency_slug", day(2), agency_id=agency_id)
        doc = self.stored()
        doc[0]["verified"] = "yes"
        self.write(doc)
        self.refused("", day(9))
        doc = self.stored()
        doc[0].update(verified=True, flock_transparency_slug="Bad Slug")
        self.write(doc)
        self.refused("flock_transparency_slug", day(9))
        before = self.tree()
        code, out, err = run("content", "agency-profile", "transparency", "add", "--directory", self.root, "--agency", A,
                             "--checked-on", day(9), "--source-url", "https://example.invalid/x")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(self.tree(), before)

    def test_duplicate_and_backdated_are_never_replaced_or_sorted(self):
        self.seed(3)  # days 30, 20, 10, newest first
        stored = self.history()
        self.assertEqual([r["checked_on"] for r in stored], [day(30), day(20), day(10)])
        for flags in ((), ("--drop-oldest",)):
            for duplicate in (30, 20, 10):  # newest, interior, oldest
                self.refused("profile.transparency: checked_on already recorded", day(duplicate), *flags)
            for older in (29, 15, 1):
                self.refused("must be after the newest stored snapshot", day(older), *flags)
        self.assertEqual(self.add(day(31))[0], 0)
        self.assertEqual([r["checked_on"] for r in self.history()], [day(n) for n in (31, 30, 20, 10)])
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for bad in (tomorrow, "2026-02-30", "2026-2-3", "20260203", "٢٠٢٦-٠١-٠٢", " 2026-01-02", "2026-01-02\n"):
            result = self.refused("--checked-on", bad)
            self.assertNotIn(bad.strip() or "x", result[2].replace("YYYY-MM-DD", ""))  # the typed value is never echoed
        self.assertEqual(self.add(date.today().isoformat())[0], 0)
        self.write(self._clean())
        self.assertEqual(self.add("2028-02-29" if date.today() >= date(2028, 2, 29) else "2024-02-29")[0], 0)

    def test_capacity_permission_discards_exactly_one_only_when_full(self):
        for count in (0, 1, 23):  # the flag deletes nothing below 24
            self.write(self._clean())
            old = self.seed(count) if count else []
            code, out, err = self.add(day(300), "--drop-oldest")
            self.assertEqual((code, err), (0, ""))
            self.assertNotIn("Dropped", out)
            self.assertEqual(len(self.history()), count + 1)
            self.assertEqual(self.history()[1:], old)
        self.write(self._clean())
        old = self.seed(24)
        code, out, err = self.add(day(300), "--hotlist-alerts", "0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("profile.transparency: already has 24 snapshots; use --drop-oldest to discard the oldest", err)
        self.assertEqual(self.history(), old)
        before = self.history()
        code, out, err = self.add(day(300), "--drop-oldest", "--hotlist-alerts", "0")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Transparency snapshot added: {A}, checked {day(300)}; 24 snapshots stored.\n"
                              f"Dropped oldest transparency snapshot: {before[-1]['checked_on']}.\n" + CLOSING)
        self.assertEqual(self.history(), [snapshot(300, hotlist_alerts=0)] + before[:23])
        # invalid new input never evicts: a duplicate or backdated date and a bad number refuse with 24 stored intact
        self.write(self._clean())
        old = self.seed(24)
        self.refused("already recorded", day(240), "--drop-oldest")
        self.refused("must be after the newest", day(5), "--drop-oldest")
        self.refused("--cameras", day(300), "--drop-oldest", "--cameras", "-1")
        self.assertEqual(self.history(), old)
        # already-invalid stores are refused, never repaired: 25, unsorted, duplicated or null rows
        for broken in (old + [snapshot(0)], old[::-1], old[:2] + [old[1]], old[:2] + [None]):
            doc = self.stored()
            doc[0]["profile"]["transparency"] = broken
            self.write(doc)
            self.refused("profile.transparency", day(300), "--drop-oldest")

    def test_numeric_admission_retention_boundary_and_no_echo(self):
        for flag in ("--cameras", "--searches-last-30-days", "--hotlist-alerts", "--shared-with-agencies"):
            self.write(self._clean())
            self.assertEqual(self.add(day(1), flag, "12345678901234567890")[0], 0)
        self.write(self._clean())
        self.assertEqual(self.add(day(1), "--retention-days", "3650")[0], 0)
        self.assertEqual(self.history()[0]["retention_days"], 3650)
        self.write(self._clean())
        self.refused("--retention-days: at most 3650", day(2), "--retention-days", "3651")
        for text in ("-1", "+1", "1.5", "1e3", "1,000", " 1", "1 ", "٣", "１２", "", "0x10", "1_0", "9" * 5000, "x" * 40):
            for flag in ("--cameras", "--retention-days"):
                before = self.tree()
                code, out, err = self.add(day(3), f"{flag}={text}")
                self.assertEqual((code, out), (1, ""), (flag, text))
                self.assertIn(flag + ":", err)
                if text.strip():
                    self.assertNotIn(text, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.tree(), before)
        # helper calls: only exact ints, only known keys
        for numbers in ({"cameras": True}, {"cameras": 1.0}, {"cameras": "1"}, {"cameras": None}, {"cameras": [1]},
                        {"cameras": -1}, {"unknown": 1}, {"cameras": 10 ** 5000}, [("cameras", 1)], None):
            with self.assertRaises(cp.ContentProfileError):
                cp.add_transparency_snapshot(self.root, A, day(4), numbers)
        self.assertIsNone(self.history())

    def test_original_and_prospective_whole_document_validated(self):
        doc = self.stored()
        doc[1]["profile"] = {"funding": "nonsense"}  # a malformed unrelated selected row blocks the install
        self.write(doc)
        self.refused("profile.funding", day(1))
        doc[1]["profile"] = {"summary": "ok"}
        doc[4] = {"agency_id": OFF, "name": "Deselected", "selected": False, "profile": "ignored"}  # inactive rows stay as they are
        self.write(doc)
        self.assertEqual(self.add(day(1))[0], 0)
        self.assertEqual(self.stored()[4], doc[4])
        for transparency in (None, []):
            doc = self._clean()
            doc[0]["profile"]["transparency"] = transparency
            self.write(doc)
            self.assertEqual(self.add(day(2))[0], 0)
        for broken in ({}, "x", 5, [{"checked_on": day(1)}], [[]]):
            doc = self._clean()
            doc[0]["profile"]["transparency"] = broken
            self.write(doc)
            self.refused("profile.transparency", day(3))
        for raw in (b"", b"\xff", b"{", b"null", b"{}", b"[null]"):
            self.target.write_bytes(raw)
            self.refused("", day(3))
        self.write(fixture()[:1] + fixture()[:1])
        self.refused("duplicate agency_id", day(3))  # a duplicated target
        for agency_id in ("missing", OFF):
            self.write(fixture())
            self.refused("--agency", day(3), agency_id=agency_id)
        self.target.unlink()
        self.refused("", day(3))
        self.target.parent.rmdir()
        self.refused("", day(3))
        self.assertFalse(self.target.parent.exists())  # nothing created
        self.assertEqual(run("content", "agency-profile", "transparency", "add", "--directory", self.root / "nope",
                             "--agency", A, "--checked-on", day(3))[0], 1)
        self.assertFalse((self.root / "nope").exists())

    def test_serialized_utf8_reader_cap_before_temp(self):
        item = {"checked_on": day(1), "source_url": PORTAL + "alpha-flock"}
        doc = self.stored()
        doc[1]["profile"] = {"summary": "Ü" * 300}  # multibyte sibling prose counts in bytes, not characters
        self.write(doc)
        expected = copy.deepcopy(doc)
        expected[0]["profile"]["transparency"] = [item]
        size = len((json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        before = self.tree()
        with mock.patch.object(cp, "MAX_JSON_BYTES", size - 1), mock.patch.object(cp, "_install") as install:
            self.assertEqual(self.refused("exceed", day(1))[0], 1)
            install.assert_not_called()
        self.assertEqual(self.tree(), before)
        with mock.patch.object(cp, "MAX_JSON_BYTES", size):
            self.assertEqual(self.add(day(1))[0], 0)
        self.assertEqual(self.target.stat().st_size, size)

    def test_boundary_and_concurrency_never_touch_foreign_or_newer_bytes(self):
        outside = self.root.parent / "foreign.json"
        outside.write_bytes(self.target.read_bytes())
        foreign = outside.read_bytes()
        self.target.unlink()
        self.target.symlink_to(outside)
        self.refused("agencies.json", day(1))
        self.target.unlink()
        os.link(outside, self.target)
        with mock.patch("campaign_tool.content_sharing._read_all", side_effect=AssertionError("foreign read")):
            self.assertEqual(self.add(day(1))[0], 1)
        self.target.unlink()
        os.mkfifo(self.target)
        self.assertEqual(self.add(day(1))[0], 1)
        self.target.unlink()
        self.target.mkdir()
        self.assertEqual(self.add(day(1))[0], 1)
        self.target.rmdir()
        self.write(fixture())
        newer = b"newer edit"

        def swap():
            self.target.write_bytes(newer)
        with mock.patch.object(cp, "_before_install", side_effect=swap):
            self.assertEqual(self.add(day(1))[0], 1)
        self.assertEqual(self.target.read_bytes(), newer)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["agencies.json"])
        self.target.unlink()
        self.target.parent.rmdir()
        external = self.root.parent / "outside-content"
        external.mkdir()
        (external / "agencies.json").write_bytes(foreign)
        self.target.parent.symlink_to(external, target_is_directory=True)
        self.assertEqual(self.add(day(1))[0], 1)
        self.assertEqual(outside.read_bytes(), foreign)
        self.assertEqual((external / "agencies.json").read_bytes(), foreign)
        self.assertEqual([p.name for p in external.iterdir()], ["agencies.json"])

    def test_atomic_failures_and_modes(self):
        unrelated = self.target.parent / ".unrelated.tmp"
        unrelated.write_bytes(b"keep")
        for name in ("write", "fsync", "replace"):
            before = self.tree()
            with mock.patch.object(cp.os, name, side_effect=OSError("synthetic error")):
                code, out, err = self.add(day(1))
            self.assertEqual((code, out), (1, ""))
            self.assertIn("nothing was written", err)
            self.assertEqual(self.tree(), before)  # old bytes and mode, the unrelated temp kept, no leftover temp
        real_write = os.write
        with mock.patch.object(cp.os, "write", side_effect=lambda fd, data: real_write(fd, data[:7])), \
                mock.patch.object(cp, "_install", wraps=cp._install) as install:
            code, out, err = self.add(day(1))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(install.call_count, 1)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.history()[0], snapshot(1))
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_cli_messages_are_content_only_vendor_report(self):
        for flags in ((), ("--cameras", "0", "--retention-days", "30")):
            self.write(self._clean())
            before = {p.name for p in self.root.rglob("*")}
            code, out, err = self.add(day(7), *flags)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, f"Transparency snapshot added: {A}, checked {day(7)}; 1 snapshots stored.\n" + CLOSING)
            self.assertNotIn("30", out.replace(day(7), ""))  # numbers are not echoed
            self.assertEqual({p.name for p in self.root.rglob("*")}, before)  # only the agencies target changed
        for bad in (("--cameras", "-1"), ("--cameras", "x")):
            code, out, err = self.add(day(8), *bad)
            self.assertEqual(out, "")
            self.assertNotEqual(code, 0)
        for missing in (("--directory", self.root, "--agency", A), ("--directory", self.root, "--checked-on", day(1)),
                        ("--agency", A, "--checked-on", day(1))):
            self.assertEqual(run("content", "agency-profile", "transparency", "add", *missing)[:2], (2, ""))
        self.assertEqual(run("content", "agency-profile", "transparency", "add", "--directory", self.root, "--agency", A,
                             "--checked-on", day(1), "--replace")[:2], (2, ""))

    def test_snapshot_builds_like_independently_authored_raw_json(self):
        self.write(self._clean())
        self.assertEqual(self.add(day(7), "--cameras", "12", "--searches-last-30-days", "0")[0], 0)
        cli_made = load_agencies(self.stored(), "content/agencies.json")
        doc = self._clean()
        doc[0]["profile"]["transparency"] = [{"checked_on": day(7), "source_url": PORTAL + "alpha-flock", "cameras": 12,
                                              "searches_last_30_days": 0}]
        self.assertEqual(cli_made, load_agencies(doc, "content/agencies.json"))
        self.assertEqual(self.history()[0].get("hotlist_alerts", "absent"), "absent")
        self.assertEqual(cli_made[0]["transparency"][0]["hotlist_alerts"], None)  # normalized null, never stored


if __name__ == "__main__":
    unittest.main()
