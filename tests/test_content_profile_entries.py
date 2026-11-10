"""DF1109B-12: raw funding/report mutations through real CLI and the inherited installer; synthetic, offline only."""
import copy
import json
import os
import socket
import tempfile
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import content_profile as cp
from campaign_tool.content import ContentError, FUNDING_KINDS, REPORT_KINDS, load_agencies
from tests.test_content_profile_documents import agencies, run, A, B

DAY = "2026-01-02"
NOTICE = "Content only; nothing was built, sent or deployed.\n"


def funding(**values):
    return dict(kind="grant", name="Fictional program", as_stated_in="Synthetic award",
                source_url="https://example.invalid/award", checked_on=DAY) | values


def report(**values):
    return dict(title="Synthetic report", kind="annual_report", period_start="2025-01-01",
                period_end="2025-12-31", source_url="https://example.invalid/report", checked_on=DAY) | values


class EntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content/agencies.json"
        self.write(agencies())
        for target in ((socket, "socket"), (socket, "create_connection"), (urllib.request, "urlopen")):
            patch = mock.patch.object(*target, side_effect=AssertionError("network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, value):
        self.target.write_text(json.dumps(value, ensure_ascii=False) + "\n")

    def stored(self):
        return json.loads(self.target.read_text())

    def tree(self):
        if not self.target.parent.exists():
            return {}
        return {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.target.parent.iterdir() if p.is_file()}

    def command(self, field, action, entry=None, index="1", agency=A):
        args = ["content", "agency-profile", "funding" if field == "funding" else "report", action,
                "--directory", self.root, "--agency", agency]
        if action == "add":
            for key, value in entry.items():
                args += ["--" + key.replace("_", "-"), value]
        else:
            args += ["--index", index]
        return run(*args)

    def add(self, field, entry, agency=A):
        return cp.add_profile_entry(self.root, agency, field, entry)

    def refused(self, field, entry, needle):
        before = self.tree()
        with self.assertRaises(ContentError) as error:
            self.add(field, entry)
        self.assertIn(needle, str(error.exception))
        self.assertEqual(self.tree(), before)

    def test_funding_add_required_and_optional_fields_preserve_literal_amount(self):
        for n, kind in enumerate(FUNDING_KINDS, 1):
            item = funding(kind=kind, source_url=f"https://example.invalid/{n}")
            if n <= 3:
                item.update(amount_usd=("0", "50000.00", "100000000")[n - 1], period="total", fiscal_year="2999")
            with mock.patch.object(cp, "_install", wraps=cp._install) as install:
                code, out, err = self.command("funding", "add", item)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, f"Funding added: {A}, index {n}.\n" + NOTICE +
                             "Reminder: check that the name is not a person.\n")
            self.assertEqual(install.call_count, 1)
            self.assertEqual(self.stored()[0]["profile"]["funding"][-1], item)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_report_add_preserves_raw_order_but_loader_sorts(self):
        old = report(title="Older", checked_on="2026-01-02")
        new = report(title="Newer", source_url="https://example.invalid/newer", period_start="2026-01-01", period_end="2027-12-31", checked_on="2026-01-01")
        self.add("reports", old)
        self.add("reports", new)
        self.assertEqual(self.stored()[0]["profile"]["reports"], [old, new])
        loaded = load_agencies(self.stored(), "content/agencies.json")
        self.assertEqual([r["title"] for r in loaded[0]["reports"]], ["Newer", "Older"])
        rows = [r for r in cp.list_profile_checks(self.root) if r["field"] == "reports"]
        self.assertEqual([r["index"] for r in rows], [2, 1])
        self.assertEqual(self.command("reports", "remove", index=str(rows[0]["index"]))[0], 0)
        self.assertEqual(self.stored()[0]["profile"]["reports"], [old])
        for kind in REPORT_KINDS:
            self.assertEqual(self.command("reports", "add", report(kind=kind, source_url=f"https://example.invalid/{kind}"))[0], 0)

    def test_remove_first_middle_last_and_final_omits_key(self):
        for field, make, limit in (("funding", funding, 10), ("reports", report, 20)):
            self.write(agencies())
            items = [make(source_url=f"https://example.invalid/{i}") for i in range(limit)]
            items[0].update(archived_url="https://archive.ph/synthetic", archived_on="2026-01-01")
            for item in items:
                self.add(field, item)
            original = self.stored()
            self.refused(field, make(), "must be a list")
            for index in (len(items), 2, 1):
                code, out, err = self.command(field, "remove", index=str(index))
                del items[index - 1]
                label = "Funding" if field == "funding" else "Report"
                self.assertEqual((code, out, err), (0, f"{label} removed: {A}, index {index}; {len(items)} remaining.\n" + NOTICE, ""))
                self.assertEqual(self.stored()[0]["profile"][field], items)
                load_agencies(self.stored(), "content/agencies.json")
            while items:
                self.command(field, "remove")
                items.pop(0)
            original[0]["profile"].pop(field)
            self.assertEqual(self.stored(), original)

    def test_duplicate_url_refusal_is_local_and_not_a_loader_migration(self):
        for field, make in (("funding", funding), ("reports", report)):
            self.write(agencies())
            item = make()
            self.add(field, item)
            self.refused(field, item | {"kind": "other"}, f"profile.{field}: source_url already recorded")
            self.add(field, item, B)
            other = "reports" if field == "funding" else "funding"
            self.add(other, (report() if other == "reports" else funding()) | {"source_url": item["source_url"]})
            doc = self.stored()
            doc[0]["profile"][field].append(copy.deepcopy(item))
            self.write(doc)
            load_agencies(doc, "content/agencies.json")
            self.add(field, item | {"source_url": item["source_url"] + "#different"})
            self.assertEqual(self.command(field, "remove")[0], 0)

    def test_counts_amount_pair_and_text_boundaries_refuse_without_writes(self):
        for amount in ("-1", "1e2", "1,000", 42, "1.234", "100000000.01", " 1"):
            self.refused("funding", funding(amount_usd=amount, period="annual"), "amount_usd")
        for changes, needle in [({"amount_usd": "1"}, "go together"), ({"period": "annual"}, "go together"),
                                ({"name": "X" * 121}, ".name"), ({"name": " padded"}, ".name"),
                                ({"as_stated_in": "X" * 121}, "as_stated_in"), ({"kind": "bad"}, ".kind"),
                                ({"fiscal_year": "０２０２"}, "fiscal_year"), ({"fiscal_year": "0123"}, "fiscal_year")]:
            self.refused("funding", funding(**changes), needle)
        self.refused("reports", report(title="X" * 161), ".title")
        self.add("funding", funding(name="Ü" * 120, as_stated_in="X" * 120))
        self.add("reports", report(title="Ü" * 160))

    def test_dates_and_urls_delegate_without_extra_period_rules(self):
        for changes, needle in [({"period_end": "2025-01-01"}, "period_end"),
                                ({"period_start": "2026-01-01"}, "period_end"),
                                ({"checked_on": "2026-02-30"}, "checked_on"),
                                ({"checked_on": "20260102"}, "checked_on"),
                                ({"checked_on": (date.today() + timedelta(days=1)).isoformat()}, "checked_on"),
                                ({"published_on": "2026-01-03"}, "published_on"),
                                ({"source_url": "http://example.invalid/report"}, "source_url")]:
            self.refused("reports", report(**changes), needle)
        self.add("reports", report(period_end="2999-01-01", published_on=DAY))

    def test_index_is_stored_one_based_and_strict(self):
        self.add("funding", funding())
        for index in ("0", "-1", "+1", "01", "1.0", " 1", "١", "x", "9" * 1000, "2"):
            before = self.tree()
            code, out, err = self.command("funding", "remove", index=index)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("--index: must name a stored position from 1 to 1", err)
            self.assertEqual(self.tree(), before)
        self.command("funding", "remove")
        self.assertIn("profile.funding: no entries recorded", self.command("funding", "remove", index="0")[2])

    def test_missing_empty_broken_and_unselected_refuse(self):
        for raw in (b"", b"\xff", b"{", b"null", b"{}", b"[null]"):
            self.target.write_bytes(raw)
            self.refused("funding", funding(), "")
        for alter in (lambda d: d[0].update(profile=None), lambda d: d[0].update(profile=[]),
                      lambda d: d[0]["profile"].update(funding=[]), lambda d: d.append(copy.deepcopy(d[0])),
                      lambda d: d[1].update(profile={"reports": []})):
            doc = agencies()
            alter(doc)
            self.write(doc)
            self.refused("funding", funding(), "")
        self.write(agencies())
        for agency in ("missing", "ca-gamma-pd"):
            before = self.tree()
            self.assertEqual(self.command("funding", "add", funding(), agency=agency)[0], 1)
            self.assertEqual(self.tree(), before)
        self.target.unlink()
        self.refused("funding", funding(), "")
        self.target.parent.rmdir()
        self.refused("funding", funding(), "")
        self.assertFalse(self.target.parent.exists())

    def test_every_mutation_validates_full_prospective_document(self):
        self.add("funding", funding())
        doc = self.stored()
        doc[1]["profile"]["reports"] = []
        self.write(doc)
        self.refused("funding", funding(source_url="https://example.invalid/new"), "reports")
        before = self.tree()
        self.assertEqual(self.command("funding", "remove")[0], 1)
        self.assertEqual(self.tree(), before)
        self.write(agencies())
        before = self.stored()
        self.add("funding", funding())
        after = self.stored()
        after[0]["profile"].pop("funding")
        self.assertEqual(after, before)
        for field in ("status", "reports.any", "transparency"):
            with self.assertRaises(cp.ContentProfileError):
                self.add(field, funding())

    def test_privacy_refuses_category_without_text_and_reminder_is_not_clearance(self):
        for field, key, make in (("funding", "name", funding), ("funding", "as_stated_in", funding),
                                 ("reports", "title", report)):
            for sentinel in ("synthetic@example.invalid", "805-555-0199", "CA plate 7ABC123"):
                before = self.tree()
                code, out, err = self.command(field, "add", make(**{key: sentinel}))
                self.assertEqual((code, out), (1, ""), err)
                self.assertNotIn(sentinel, err)
                self.assertEqual(self.tree(), before)

    def test_symlinks_special_files_and_concurrent_swaps_do_not_escape(self):
        outside = self.root.parent / "foreign"
        outside.write_bytes(self.target.read_bytes())
        self.target.unlink()
        self.target.symlink_to(outside)
        self.refused("funding", funding(), "")
        self.target.unlink()
        os.link(outside, self.target)
        with mock.patch("campaign_tool.content_sharing._read_all", side_effect=AssertionError("foreign read")):
            self.refused("funding", funding(), "")
        self.target.unlink()
        os.mkfifo(self.target)
        with self.assertRaises(ContentError):
            self.add("funding", funding())
        self.target.unlink()
        self.write(agencies())
        def newer():
            self.target.write_bytes(b"newer edit")
        with mock.patch.object(cp, "_before_install", side_effect=newer):
            with self.assertRaises(ContentError):
                self.add("funding", funding())
        self.assertEqual(self.target.read_bytes(), b"newer edit")
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["agencies.json"])
        self.target.unlink()
        self.target.parent.rmdir()
        external = self.root.parent / "outside-content"
        external.mkdir()
        (external / "agencies.json").write_bytes(outside.read_bytes())
        self.target.parent.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ContentError):
            self.add("funding", funding())
        self.assertEqual((external / "agencies.json").read_bytes(), outside.read_bytes())

    def test_partial_write_fsync_replace_errors_keep_previous_target(self):
        unrelated = self.target.parent / ".unrelated.tmp"
        unrelated.write_bytes(b"keep")
        for name in ("write", "fsync", "replace"):
            before = self.tree()
            with mock.patch.object(cp.os, name, side_effect=OSError("synthetic error")):
                self.assertEqual(self.command("funding", "add", funding())[0], 1)
            self.assertEqual(self.tree(), before)
        real_write = os.write
        def partial(fd, data):
            return real_write(fd, data[:7])
        with mock.patch.object(cp.os, "write", side_effect=partial):
            self.add("funding", funding())
        self.assertEqual(self.stored()[0]["profile"]["funding"], [funding()])

    def test_serialized_utf8_cap_checked_before_temp(self):
        doc = agencies()
        item = funding(name="Ü" * 120)
        expected = copy.deepcopy(doc)
        expected[0]["profile"]["funding"] = [item]
        size = len((json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode())
        with mock.patch.object(cp, "MAX_JSON_BYTES", size - 1), mock.patch.object(cp, "_install") as install:
            self.refused("funding", item, "exceed")
            install.assert_not_called()
        with mock.patch.object(cp, "MAX_JSON_BYTES", size):
            self.add("funding", item)
        self.assertEqual(self.target.stat().st_size, size)

    def test_parser_missing_unknown_and_invalid_choices_write_nothing(self):
        for args in (("--kind", "bad"), ("--replace",), ("--index", "1")):
            before = self.tree()
            code, out, err = run("content", "agency-profile", "funding", "add", "--directory", self.root,
                                 "--agency", A, *args)
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
