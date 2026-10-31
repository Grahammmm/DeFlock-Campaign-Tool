"""NB-DF-06: the digest groups agencies by slug and lists ALPR-relevant items first, never cut by the 39-line cap."""
import contextlib
import csv
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_test_digest_rank_monitor", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
DATE = "2026-10-30"


def item(n, agency, title, url, seen_at=DATE):
    return dict(item_id="M-%012d" % n, agency=agency, watch_kind="agenda", url=url, title=title,
                page_sha256="0" * 64, seen_at=seen_at, kind="public", status="unreviewed")


class DigestRankTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()

    def digest(self, items):
        with open(self.store / "MONITOR.csv", "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, monitor.MONITOR_HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(items)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor.main(["digest", "--store", str(self.store), "--date", DATE])
        self.assertEqual(code, 0, out.getvalue())
        return (self.store / "digest" / (DATE + ".md")).read_text(encoding="utf-8").split("\n")[:-1]

    def test_uses_the_shared_meetings_pattern_object(self):
        from campaign_tool.meetings import KEYWORDS
        self.assertIs(monitor.keywords(), KEYWORDS)

    def test_agencies_by_slug_relevant_first_order_kept_terms_marked(self):
        z, a, u = "Zed County", "alpha city", "https://www.zed.example/a/"
        lines = self.digest([
            item(1, z, "Budget hearing", u + "1"),
            item(2, a, "FLOCK camera contract", "https://www.alpha.example/x/2"),
            item(3, z, "ALPR policy and License Plate readers, alpr", u + "3"),
            item(4, a, "Parks update", "https://www.alpha.example/x/4"),
            item(5, z, "Surveillance use policy", u + "5"),
            item(6, a, "Library hours", "https://www.alpha.example/x/6"),
            item(7, z, "Other", u + "7", seen_at="2026-10-29"),
        ])
        self.assertEqual(lines, [
            "Flock monitor 2026-10-30: 6 new items (public, unreviewed), 3 relevant",
            "- alpha city agenda: FLOCK camera contract https://www.alpha.example/x/2 [ALPR: flock]",
            "- alpha city agenda: Parks update https://www.alpha.example/x/4",
            "- alpha city agenda: Library hours https://www.alpha.example/x/6",
            "- Zed County agenda: ALPR policy and License Plate readers, alpr https://www.zed.example/a/3 "
            "[ALPR: alpr, license plate]",
            "- Zed County agenda: Surveillance use policy https://www.zed.example/a/5 [ALPR: surveillance]",
            "- Zed County agenda: Budget hearing https://www.zed.example/a/1",
        ])

    def test_url_path_matches_only_path_and_follows_shared_boundaries(self):
        pattern = monitor.keywords()
        base = "https://www.city.example"
        self.assertEqual(monitor.alpr_terms(dict(title="Contract", url=base + "/docs/Flock_Safety-contract"), pattern),
                         ["flock"])
        self.assertEqual(monitor.alpr_terms(dict(title="Policy", url=base + "/files/alpr-policy.pdf"), pattern), ["alpr"])
        self.assertEqual(monitor.alpr_terms(dict(title="Minutes", url=base + "/m/License%20Plate%2DReaders"), pattern),
                         ["license plate"])
        for url in ("https://flock.example/notes", base + "/notes?q=flock", base + "/notes#flock"):  # host/query/fragment
            self.assertEqual(monitor.alpr_terms(dict(title="Notes", url=url), pattern), [], url)
        for title in ("Flocking birds", "surveillanceX", "ALPRS", "ALPRx", "licenseplate", "Flock's", "unflocked"):
            self.assertEqual(bool(monitor.alpr_terms(dict(title=title, url=base + "/n"), pattern)),
                             bool(pattern.search(title)), title)
            self.assertEqual(bool(monitor.alpr_terms(dict(title="n", url=base + "/" + title), pattern)),
                             bool(pattern.search(title)), title)

    def test_cap_keeps_five_late_relevant_items(self):
        rows = [item(n, "Example City", "Item %d" % n, "https://www.city.example/i/%d" % n) for n in range(40)]
        rows += [item(n, "Example City", "Flock item %d" % n, "https://www.city.example/i/%d" % n) for n in range(40, 45)]
        lines = self.digest(rows)
        self.assertEqual(len(lines), 40)
        self.assertEqual(lines[0], "Flock monitor 2026-10-30: 45 new items (public, unreviewed), 5 relevant")
        self.assertEqual([line.endswith("[ALPR: flock]") for line in lines[1:39]], [True] * 5 + [False] * 33)
        self.assertEqual(lines[1], "- Example City agenda: Flock item 40 https://www.city.example/i/40 [ALPR: flock]")
        self.assertEqual(lines[6], "- Example City agenda: Item 0 https://www.city.example/i/0")
        self.assertEqual(lines[39], "- … and 7 more in MONITOR.csv (0 relevant)")

    def test_cap_relevant_in_later_agency_still_shown_and_grouped(self):
        rows = [item(n, "Aaa Town", "Item %d" % n, "https://www.aaa.example/i/%d" % n) for n in range(40)]
        rows += [item(n, "Bbb Town", "Surveillance %d" % n, "https://www.bbb.example/i/%d" % n) for n in range(40, 45)]
        lines = self.digest(rows)
        self.assertEqual([line.split()[1] for line in lines[1:39]], ["Aaa"] * 33 + ["Bbb"] * 5)
        self.assertEqual(lines[33], "- Aaa Town agenda: Item 32 https://www.aaa.example/i/32")
        self.assertEqual(lines[39], "- … and 7 more in MONITOR.csv (0 relevant)")

    def test_cap_all_relevant_counts_hidden_relevant(self):
        rows = [item(n, "Example City", "ALPR %d" % n, "https://www.city.example/i/%d" % n) for n in range(45)]
        lines = self.digest(rows)
        self.assertEqual((len(lines), lines[0]), (40, "Flock monitor 2026-10-30: 45 new items (public, unreviewed), 45 relevant"))
        self.assertEqual(lines[38], "- Example City agenda: ALPR 37 https://www.city.example/i/37 [ALPR: alpr]")
        self.assertEqual(lines[39], "- … and 7 more in MONITOR.csv (7 relevant)")

    def test_cap_boundary_39_shown_40_overflows(self):
        rows = [item(n, "Example City", "Item %d" % n, "https://www.city.example/i/%d" % n) for n in range(40)]
        self.assertEqual(len(self.digest(rows[:39])), 40)
        lines = self.digest(rows[:39] + [item(99, "Example City", "Flock", "https://www.city.example/f")])
        self.assertEqual((len(lines), lines[1].endswith("[ALPR: flock]")), (40, True))
        self.assertEqual(lines[39], "- … and 2 more in MONITOR.csv (0 relevant)")

    def test_no_items_for_date_unchanged(self):
        self.digest([item(1, "Example City", "Flock", "https://www.city.example/f", seen_at="2026-10-29")])
        self.assertEqual((self.store / "digest" / (DATE + ".md")).read_text(encoding="utf-8"),
                         "Flock monitor 2026-10-30: no new items\n")

    def test_markup_titles_stay_one_line(self):
        lines = self.digest([
            item(1, "Example City", "Line one\n# Flock <script>x</script>\r\n- [x](y)", "https://www.city.example/1"),
            item(2, "Example City", "**bold**\n\n<b>plain</b>\t|", "https://www.city.example/2"),
        ])
        self.assertEqual(lines[1:], [
            "- Example City agenda: Line one # Flock <script>x</script> - [x](y) https://www.city.example/1 [ALPR: flock]",
            "- Example City agenda: **bold** <b>plain</b> | https://www.city.example/2",
        ])

    def test_import_failure_fails_digest_without_writing(self):
        with open(self.store / "MONITOR.csv", "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerow(monitor.MONITOR_HEADER)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"campaign_tool.meetings": None}), contextlib.redirect_stdout(out):
            code = monitor.main(["digest", "--store", str(self.store), "--date", DATE])
        self.assertEqual((code, out.getvalue()), (1, "io error: ImportError\n"))
        self.assertFalse((self.store / "digest").exists())


if __name__ == "__main__":
    unittest.main()
