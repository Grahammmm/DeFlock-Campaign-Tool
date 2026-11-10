"""kit-repeat: a follow-on request starts the day after the earlier request's recorded coverage. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_repeat, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

LINE = kit.RANGE_PREFIX


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitRepeatTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-repeat-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.kit = self.root / "kit"
        drafts = sorted((self.kit / "requests").glob("*.md"))
        self.agency, self.other = drafts[0].stem, drafts[1].stem
        ready_drafts(self.root)
        self.sent = self.kit / "sent.json"
        self.store = self.kit / "repeat-drafts.json"

    # --- helpers -----------------------------------------------------------------------------------------------
    def draft(self, request="main", agency=None):
        return self.kit / "requests" / (kit_track._stem(agency or self.agency, request) + ".md")

    def set_range(self, span, request="main"):
        path = self.draft(request)
        text = re.sub(r"(?m)^The date range for this request is .*$", f"{LINE}{span}.", path.read_text(encoding="utf-8"))
        text = re.sub(r"(?m)^(Subject: .*?), (?:synthetic value|\d{4}-\d\d-\d\d through \S+)$", rf"\1, {span}", text)
        path.write_text(text, encoding="utf-8")

    def send(self, request="main", sent_on="2026-10-15", *extra, agency=None):
        return run("kit-sent", "--directory", self.root, "--agency", agency or self.agency, "--date", sent_on,
                   "--channel", "email", "--request", request, *extra)

    def sent_entry(self, request="main"):
        return kit_track.read_sent(self.root)[kit_track._key(self.agency, request)]

    def repeat(self, new, request="main", **kw):
        kw.setdefault("today", date(2026, 11, 1))
        return kit_repeat.repeat_request(self.root, self.agency, request, new, **kw)

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.is_symlink(), stat.S_IMODE(p.lstat().st_mode),
                                               None if p.is_dir() or p.is_symlink() else p.read_bytes())
                for p in sorted(self.tmp.rglob("*"))}

    def refused(self, call, *words):
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            call()
        for word in words:
            self.assertIn(word, str(caught.exception))
        self.assertEqual(self.snapshot(), before, "a refusal must write nothing")

    def sent_main(self, span="2026-01-01 through 2026-09-30", sent_on="2026-10-15"):
        self.set_range(span)
        code, _, err = self.send("main", sent_on)
        self.assertEqual(code, 0, err)

    # --- named acceptance tests --------------------------------------------------------------------------------
    def test_delayed_send_uses_coverage_not_send_date(self):
        self.sent_main()
        self.assertEqual(self.sent_entry()["covers_through"], "2026-09-30")
        (self.kit / "request-settings.json").write_text(json.dumps({"schema_version": 1, "date_from": "2026-05-05"}))
        (self.kit / "summary.json").write_text('{"scopes": "all"}')  # neither file is a coverage source
        out = self.repeat("next")
        self.assertEqual((out["date_from"], out["date_to"]), ("2026-10-01", "2026-11-01"))
        new = self.draft("next").read_text(encoding="utf-8")
        self.assertIn(f"{LINE}2026-10-01 through 2026-11-01.\n", new)
        self.assertNotIn("2026-10-16", new)
        self.assertNotIn("through today", new)
        self.assertIn(", 2026-10-01 through 2026-11-01\n", new)  # the generated Subject range matches
        source = self.draft().read_text(encoding="utf-8")

        def strip(text):
            return [line for line in text.split("\n") if "2026-" not in line]
        self.assertEqual(strip(new), strip(source))  # every scope section, requester and fee text kept
        self.assertEqual(stat.S_IMODE(self.draft("next").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertNotIn(f"{self.agency}#next", json.loads(self.sent.read_text())["requests"])  # not sent at creation

    def test_pre_2000_canonical_range_stores_nothing_and_file_stays_readable(self):
        self.sent_main("1990-01-01 through 1999-12-31")
        self.assertNotIn("covers_through", self.sent_entry())  # outside the reader's 2000-01-01 bound: not inferred
        self.assertEqual(run("kit-status", "--directory", self.root)[0], 0)
        self.assertEqual(self.send("main", "2026-10-15", "--replace")[0], 0)  # correction with --replace still works
        self.assertNotIn("covers_through", self.sent_entry())
        self.set_range("2026-01-01 through 2026-09-30")
        self.assertEqual(self.send("main", "2026-10-15", "--replace")[0], 0)
        self.assertEqual(self.sent_entry()["covers_through"], "2026-09-30")

    def test_today_resolves_at_send_and_explicit_override(self):
        self.sent_main("2026-01-01 through today", "2026-03-02")
        self.assertEqual(self.sent_entry()["covers_through"], "2026-03-02")
        before = self.sent.read_bytes()
        for bad in ("2026-02-30", "2026-3-1", "2026-03-03", "1999-12-31", "today", "2026-03-02T00:00"):
            code, _, err = self.send("main", "2026-03-02", "--replace", "--covers-through", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("--covers-through", err)
            self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(self.send("main", "2026-03-02", "--replace", "--covers-through", "2026-02-01")[0], 0)
        self.assertEqual(self.sent_entry()["covers_through"], "2026-02-01")  # explicit past TO wins over the draft
        self.set_range("2026-01-01 through 2026-02-15")  # --replace recomputes from the new draft bytes
        self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 0)
        entry = self.sent_entry()
        self.assertEqual(entry["covers_through"], "2026-02-15")
        self.assertEqual(entry["draft_sha256"], kit_track._draft(self.root, self.agency))
        text = re.sub(r"(?m)^The date range .*\n", "", self.draft().read_text(encoding="utf-8"))
        self.draft().write_text(text, encoding="utf-8")  # no range line now: the stale boundary is not kept
        self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 0)
        self.assertNotIn("covers_through", self.sent_entry())

    def test_unknown_coverage_requires_from(self):
        cases = {"edited": "The date range for this request is 2026-01-01 to 2026-02-01.",
                 "future": f"{LINE}2026-01-01 through 2026-12-31.",
                 "reversed": f"{LINE}2026-03-01 through 2026-02-01.", "impossible": f"{LINE}2026-02-30 through 2026-03-01.",
                 "spaced": f"{LINE}2026-01-01 through 2026-02-01. ",
                 "twice": f"{LINE}2026-01-01 through 2026-02-01.\n{LINE}2026-01-01 through 2026-02-01.",
                 "missing": ""}
        original = self.draft().read_text(encoding="utf-8")
        old_line = f"{LINE}synthetic value."
        self.assertIn(old_line, original)
        for name, line in cases.items():
            self.draft().write_text(original.replace(old_line, line), encoding="utf-8")
            self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 0, name)
            self.assertNotIn("covers_through", self.sent_entry(), name)
            self.refused(lambda: self.repeat("later"), "--from")
        self.assertFalse(self.store.exists())
        self.assertIsNone(kit.draft_range(f"{LINE}[date range].\n"))  # a placeholder stores nothing (kit-sent refuses it)
        self.draft().write_text(original.replace(old_line, f"{LINE}[date range]."), encoding="utf-8")
        self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 1)
        # two range lines are ambiguous with --from: refused, never rewritten (the last case above was "missing")
        self.draft().write_text(original.replace(old_line, cases["twice"]), encoding="utf-8")
        self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 0)
        self.refused(lambda: self.repeat("twice", date_from="2026-02-02"), "more than one")
        # an explicit --from works on a draft with no range line: one canonical line is appended before the context
        self.draft().write_text(original.replace(old_line, ""), encoding="utf-8")
        self.assertEqual(self.send("main", "2026-03-02", "--replace")[0], 0)
        out = self.repeat("later", date_from="2026-02-02")
        text = self.draft("later").read_text(encoding="utf-8")
        self.assertEqual(text.count(LINE), 1)
        self.assertIn(f"{LINE}2026-02-02 through 2026-11-01.\n", text)
        self.assertLess(text.index(LINE), text.index("## Statutory context"))
        self.assertEqual(out["date_from"], "2026-02-02")
        # imported (no draft on record) and absent/changed originals are separate refusals
        entry = self.sent_entry()
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency] = {k: v for k, v in entry.items() if k not in ("draft", "draft_sha256")} \
            | {"imported": True}
        self.sent.write_text(json.dumps(document))
        self.refused(lambda: self.repeat("imp", date_from="2026-02-02"), "imported", "scopes")
        self.refused(lambda: self.repeat("imp"), "--from")
        document["requests"][self.agency] = entry
        self.sent.write_text(json.dumps(document))
        self.draft().write_text(original, encoding="utf-8")
        self.refused(lambda: self.repeat("edited", date_from="2026-02-02"), "changed since")
        self.draft().unlink()
        self.refused(lambda: self.repeat("gone", date_from="2026-02-02"), "regular file")

    def test_follow_on_chain_persists_through_send(self):
        self.sent_main()
        self.assertEqual(self.send("main", "2026-10-16", "--replace", "--reference", "R1")[0], 0)
        self.repeat("first")
        self.assertEqual(json.loads(self.store.read_text())["requests"][f"{self.agency}#first"]["follows"], "main")
        _, out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        pending = [r for r in out.splitlines() if r.startswith(self.agency + ",") and ",first," in r]
        self.assertEqual(len(pending), 1)
        self.assertIn(",main,2026-10-01,2026-11-01,", pending[0])
        self.assertEqual(self.send("first", "2026-11-01")[0], 0)
        entry = self.sent_entry("first")
        self.assertEqual((entry["follows"], entry["covers_through"], entry["steps"]), ("main", "2026-11-01", []))
        self.assertFalse({"reference", "owner", "correspondence"} & set(entry))  # nothing inherited from the parent
        self.assertIn("reference", self.sent_entry())
        self.assertEqual(self.send("first", "2026-11-01", "--replace")[0], 0)  # recomputed from its own line
        self.assertEqual(self.sent_entry("first")["follows"], "main")
        _, out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        rows = [r for r in out.splitlines() if r.startswith(self.agency + ",") and ",first," in r]
        self.assertEqual(len(rows), 1)  # one row after recording, not a pending one and a sent one
        self.assertIn("2026-11-01", rows[0])
        second = self.repeat("second", "first", today=date(2026, 11, 3))
        self.assertEqual((second["date_from"], second["date_to"]), ("2026-11-02", "2026-11-03"))
        code, _, err = self.send("second", "2026-11-03")
        self.assertEqual(code, 0, err)
        status = {(r["agency_id"], r["request"]): r
                  for r in json.loads(run("kit-status", "--directory", self.root)[1])["requests"]}
        self.assertEqual(status[(self.agency, "second")]["follows"], "first")
        self.assertEqual(status[(self.agency, "second")]["covers_through"], "2026-11-03")
        self.assertNotIn("follows", status[(self.agency, "main")])

    def test_range_and_identity_refusals_write_nothing(self):
        self.sent_main()
        self.refused(lambda: self.repeat("x", "nothere"), "not recorded as sent")
        self.refused(lambda: kit_repeat.repeat_request(self.root, "ca-unknown", "main", "x", today=date(2026, 11, 1)),
                     "kit/agencies.json")
        for name in ("Bad", "", "a" * 41):
            self.refused(lambda name=name: self.repeat(name), "--request")
        self.refused(lambda: self.repeat("main"), "not main")
        self.refused(lambda: self.repeat("nope", through="2026-11-02"), "after today")
        self.refused(lambda: self.repeat("nope", through="2026-09-30"), "after its end")
        self.refused(lambda: self.repeat("nope", date_from="2026-11-02"), "after its end")
        for bad in ("2026-02-30", "2026-2-3", "1999-12-31", "0000-01-01", "10000-01-01"):
            self.refused(lambda bad=bad: self.repeat("nope", through=bad), "--through")
            self.refused(lambda bad=bad: self.repeat("nope", date_from=bad), "--from")
        # a used name, an existing draft file and a link to an outside file
        self.repeat("used")
        self.refused(lambda: self.repeat("used"), "already used")
        outside = self.tmp / "outside.md"
        outside.write_text("keep")
        os.symlink(outside, self.draft("linked"))
        self.refused(lambda: self.repeat("linked"), "already exists")
        self.assertEqual(outside.read_text(), "keep")
        self.draft("taken").write_text("mine")
        self.refused(lambda: self.repeat("taken"), "already exists")
        # a source that is a link, or was edited after it was sent
        original = self.draft().read_bytes()
        moved = self.tmp / "moved.md"
        moved.write_bytes(original)
        self.draft().unlink()
        os.symlink(moved, self.draft())
        self.refused(lambda: self.repeat("viasym"), "regular file")
        self.draft().unlink()
        self.draft().write_bytes(original + b"edit\n")
        self.refused(lambda: self.repeat("edited"), "changed since")
        self.draft().write_bytes(original)
        # a corrupt, duplicate-key, oversized, linked or wrongly shaped store
        for content in (b"{", b'{"schema_version":1,"requests":{},"requests":{}}', b"x" * ((1 << 20) + 1),
                        json.dumps({"schema_version": 1, "requests": {f"{self.agency}#used": {"follows": "main"}}}).encode(),
                        json.dumps({"schema_version": 2, "requests": {}}).encode()):
            self.store.write_bytes(content)
            self.refused(lambda: self.repeat("viastore"), "repeat-drafts.json")
        self.store.unlink()
        os.symlink(outside, self.store)
        self.refused(lambda: self.repeat("viastore"), "repeat-drafts.json")
        self.store.unlink()
        self.store.write_text(json.dumps({"schema_version": 1, "requests": {}}))
        # a failed metadata replace leaves no new draft and the old bytes intact
        before = self.snapshot()
        with mock.patch("campaign_tool.kit_repeat.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.repeat("failing")
        self.assertFalse(self.draft("failing").exists())
        self.assertEqual(self.snapshot(), before)
        # a linked kit/requests folder is refused
        real, away = self.kit / "requests", self.tmp / "away"
        real.rename(away)
        os.symlink(away, real)
        self.refused(lambda: self.repeat("viafolder"), "kit/requests")

    def test_views_and_check_keep_legacy_bytes(self):
        self.sent_main()
        legacy = json.loads(self.sent.read_text())
        legacy["requests"][self.agency].pop("covers_through")  # an older kit/sent.json
        self.sent.write_text(json.dumps(legacy, indent=2) + "\n")
        old_bytes = self.sent.read_bytes()
        _, status_out, _ = run("kit-status", "--directory", self.root)
        self.assertNotIn("covers_through", status_out)
        self.assertNotIn("follows", status_out)
        _, table, _ = run("kit-coverage", "--directory", self.root)
        _, csv_out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertEqual(csv_out.splitlines()[0], ",".join(kit_track.CSV_HEADER))
        for word in ("follows", "date_from", "covers_through"):
            self.assertNotIn(word, table)
        out_csv = self.tmp / "status.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", out_csv)[0], 0)
        self.assertNotIn("follows", out_csv.read_text())
        self.assertEqual(self.sent.read_bytes(), old_bytes)  # read-only views never rewrite
        # with chain data the extra columns appear, and views stay read-only
        self.assertEqual(self.send("main", "2026-10-15", "--replace")[0], 0)
        self.repeat("chain")
        self.assertEqual(self.send("chain", "2026-11-01")[0], 0)
        stamps = {p: p.stat().st_mtime_ns for p in (self.sent, self.store)}
        _, csv_out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertTrue(csv_out.splitlines()[0].endswith("follows,date_from,date_to,covers_through"))
        out_csv2 = self.tmp / "status2.csv"
        run("kit-status", "--directory", self.root, "--csv", out_csv2)
        self.assertTrue(out_csv2.read_text().splitlines()[0].endswith("follows,covers_through"))
        _, table, _ = run("kit-coverage", "--directory", self.root)
        self.assertIn("covers_through", table)
        self.assertEqual({p: p.stat().st_mtime_ns for p in stamps}, stamps)
        # a stale repeat draft cannot be recorded: the chain is not silently lost
        self.repeat("again")
        self.draft("again").write_text(self.draft("again").read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
        code, _, err = self.send("again", "2026-11-01")
        self.assertEqual(code, 1)
        self.assertIn("restore the bound draft", err)
        self.assertNotIn(f"{self.agency}#again", json.loads(self.sent.read_text())["requests"])
        # invalid stored coverage or chain is refused by the readers
        good = json.loads(self.sent.read_text())
        for mutate in (lambda r: r[self.agency].update(covers_through="2026-12-01"),
                       lambda r: r[self.agency].update(covers_through="2026-02-30"),
                       lambda r: r[f"{self.agency}#chain"].update(follows="chain"),
                       lambda r: r[f"{self.agency}#chain"].update(follows="gone"),
                       lambda r: r[self.agency].update(follows="chain")):
            document = json.loads(json.dumps(good))
            mutate(document["requests"])
            self.sent.write_text(json.dumps(document))
            for command in ("kit-status", "kit-coverage"):
                self.assertEqual(run(command, "--directory", self.root)[0], 1)
        self.sent.write_text(json.dumps(good))
        # kit-sent still gates on placeholders: a valid range line does not exempt the draft
        other = self.draft(agency=self.other)
        other.write_text(other.read_text(encoding="utf-8") + "\n[unfilled]\n", encoding="utf-8")
        code, _, err = self.send("main", "2026-10-15", agency=self.other)
        self.assertEqual(code, 1)
        self.assertIn("placeholder", err)

    # --- input admission (RF0145-7) ---------------------------------------------------------------
    @contextlib.contextmanager
    def payload_probe(self, *, growth=None, read_error=None, after_growth=None):
        """Forward real stat/open/read calls; inject growth after the real size preflight."""
        original_stat, original_open = Path.stat, Path.open
        observed = {"stats": 0, "reads": [], "handles": [], "grew": False}
        store = self.store

        def checked_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == store and kwargs.get("follow_symlinks", True):
                observed["stats"] += 1
                # is_file performs the first stat; the explicit size check is the second.
                if observed["stats"] == 2 and growth is not None:
                    self.assertLessEqual(result.st_size, kit_track.MAX_BYTES)
                    with original_open(store, "wb") as writer:
                        writer.write(growth)
                    observed["grew"] = True
                    if after_growth is not None:
                        after_growth()
            return result

        @contextlib.contextmanager
        def opened(path, *args, **kwargs):
            with original_open(path, *args, **kwargs) as handle:
                if path != store or (args[0] if args else kwargs.get("mode")) != "rb":
                    yield handle
                    return
                observed["handles"].append(handle)

                class ForwardingReader:
                    def read(inner, size=-1):
                        if read_error is not None:
                            raise read_error
                        data = handle.read(size)
                        observed["reads"].append((size, len(data)))
                        return data

                yield ForwardingReader()

        with mock.patch.object(Path, "stat", checked_stat), mock.patch.object(Path, "open", opened):
            yield observed
        self.assertTrue(all(handle.closed for handle in observed["handles"]))

    def assert_bounded(self, observed, length):
        self.assertEqual(observed["reads"], [(kit_track.MAX_BYTES + 1, length)])
        self.assertEqual(len(observed["handles"]), 1)
        self.assertTrue(observed["handles"][0].closed)

    def test_repeat_reader_bounds_actual_payload_after_real_size_check(self):
        self.sent_main()
        large = self.pretty(self.filler(self.LIMIT + 4096, "input"))
        self.assertGreater(len(large), self.LIMIT)
        empty = self.pretty({})
        for payload in (empty + b" " * self.LIMIT, large):
            with self.subTest(kind="whitespace" if payload.startswith(empty) else "entries"):
                self.store.write_bytes(empty)
                with self.payload_probe(growth=payload) as observed:
                    with self.assertRaisesRegex(ValueError, "unreadable: larger than 1 MiB$"):
                        kit_repeat.read_repeats(self.root)
                self.assertTrue(observed["grew"])
                self.assert_bounded(observed, self.LIMIT + 1)

    def test_repeat_reader_sentinel_precedes_decode_and_json(self):
        empty = self.pretty({})
        # A valid truncated JSON prefix must not be accepted; invalid UTF-8 must not mask size.
        # A multibyte document has fewer characters than bytes, but still exceeds the byte cap.
        multibyte = ('{"schema_version":1,"requests":{},"' + 'é' * (self.LIMIT // 2) + '":0}').encode()
        self.assertLess(len(multibyte.decode()), self.LIMIT)
        for payload in (empty + b" " * self.LIMIT, b"\xff" + b" " * self.LIMIT, multibyte):
            with self.subTest(prefix=payload[:20]):
                self.store.write_bytes(empty)
                with self.payload_probe(growth=payload) as observed, \
                        mock.patch.object(kit_repeat.json, "loads", wraps=json.loads) as parsed:
                    with self.assertRaisesRegex(ValueError, "unreadable: larger than 1 MiB$"):
                        kit_repeat.read_repeats(self.root)
                    parsed.assert_not_called()
                self.assert_bounded(observed, self.LIMIT + 1)

    def test_repeat_reader_exact_input_boundaries(self):
        self.assertEqual(kit_repeat.read_repeats(self.root), {})
        self.sent_main()
        self.repeat("bound")
        requests = kit_repeat.read_repeats(self.root)
        for entries in ({}, requests):
            document = self.pretty(entries)
            for size in (self.LIMIT - 1, self.LIMIT):
                with self.subTest(populated=bool(entries), size=size):
                    self.store.write_bytes(document + b" " * (size - len(document)))
                    with self.payload_probe() as observed:
                        self.assertEqual(kit_repeat.read_repeats(self.root), entries)
                    self.assert_bounded(observed, size)
            self.store.write_bytes(document + b" " * (self.LIMIT + 1 - len(document)))
            with self.payload_probe() as observed:
                with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                    kit_repeat.read_repeats(self.root)
            self.assertEqual(observed["reads"], [])  # static preflight still refuses before open
            self.assertEqual(observed["handles"], [])

    def test_repeat_reader_closes_binary_handle_on_every_exit(self):
        cases = ((self.pretty({}), None), (b"\xff", "not JSON without duplicate keys"),
                 (b"{", "not JSON without duplicate keys"),
                 (b'{"schema_version":1,"requests":{},"requests":{}}', "not JSON without duplicate keys"),
                 (b'{"schema_version":true,"requests":{}}', "expected schema_version 1"),
                 (b'{"schema_version":1,"requests":{"bad":{}}}', "malformed entry"))
        for payload, error in cases:
            with self.subTest(payload=payload):
                self.store.write_bytes(payload)
                with self.payload_probe() as observed:
                    if error:
                        with self.assertRaisesRegex(ValueError, error):
                            kit_repeat.read_repeats(self.root)
                    else:
                        self.assertEqual(kit_repeat.read_repeats(self.root), {})
                self.assert_bounded(observed, len(payload))
        failure = OSError("synthetic read failure")
        with self.payload_probe(read_error=failure) as observed:
            with self.assertRaises(OSError) as caught:
                kit_repeat.read_repeats(self.root)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(observed["handles"]), 1)
        self.assertTrue(observed["handles"][0].closed)

    def growth_cli_refused(self, call):
        original = self.store.read_bytes()
        after = {}

        def snapshot_after_injection():
            after["tree"] = self.snapshot()
            after["mtimes"] = {p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}

        with self.payload_probe(growth=original + b" " * self.LIMIT,
                                after_growth=snapshot_after_injection) as observed, \
                mock.patch("socket.socket", side_effect=AssertionError("network used")):
            code, out, err = call()
        self.assertTrue(observed["grew"])
        # Snapshot reads performed by the injector are not metadata acquisition by production.
        self.assertEqual(observed["reads"][-1], (self.LIMIT + 1, self.LIMIT + 1))
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn("kit/repeat-drafts.json unreadable: larger than 1 MiB", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.snapshot(), after["tree"])
        self.assertEqual({p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}, after["mtimes"])
        self.assertEqual(list(self.kit.glob(".repeat-drafts.*")), [])

    def test_post_stat_growth_refuses_real_repeat_without_outputs(self):
        self.sent_main()
        self.store.write_bytes(self.pretty({}))
        self.growth_cli_refused(lambda: run("kit-repeat", "--directory", self.root, "--agency", self.agency,
                                          "--request", "main", "--as", "growth", "--through", "2026-11-01"))
        self.assertFalse(self.draft("growth").exists())

    def test_post_stat_growth_refuses_named_send_before_store_write(self):
        self.sent_main()
        self.repeat("bound")
        self.growth_cli_refused(lambda: self.send("bound", "2026-11-01"))
        self.assertNotIn(f"{self.agency}#bound", kit_track.read_sent(self.root))

    def test_post_stat_growth_refuses_coverage_without_report(self):
        self.sent_main()
        self.repeat("bound")
        original = self.store.read_bytes()
        for flags in ((), ("--csv",)):
            with self.subTest(flags=flags):
                self.store.write_bytes(original)
                self.growth_cli_refused(lambda: run("kit-coverage", "--directory", self.root, *flags))

    def test_repeat_reader_nonregular_legacy_refusals(self):
        for target in (self.tmp / "missing", self.draft()):
            self.store.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                kit_repeat.read_repeats(self.root)
            self.store.unlink()
        self.store.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            kit_repeat.read_repeats(self.root)

    # --- store capacity (RF1635-2): the writer never produces bytes read_repeats refuses -------------------------
    LIMIT = 1 << 20  # the reader's real limit, not patched
    FULL = "would be larger than 1 MiB; no follow-on draft was written; existing requests were kept"

    @staticmethod
    def pretty(requests):  # the store format on main, written out independently of kit_repeat
        return (json.dumps({"schema_version": 1, "requests": dict(sorted(requests.items()))}, indent=2) + "\n").encode()

    def filler(self, prospective, new, keep=None):
        """Schema-valid stored entries that, with ``keep`` and the follow-on ``new``, serialize to ``prospective`` bytes.

        Dates and hashes are fixed-width, so a stand-in for the new entry has its exact size; names are lengthened
        (up to the 40-character request limit) to land on the exact byte."""
        self.assertEqual(kit_track.MAX_BYTES, self.LIMIT)
        sha = self.sent_entry()["draft_sha256"]
        meta = {"follows": "main", "date_from": "2026-10-01", "date_to": "2026-11-01",
                "source_sha256": sha, "draft_sha256": sha}
        fixed = {**(keep or {}), f"{self.agency}#{new}": meta}
        base = len(self.pretty(fixed))
        step = len(self.pretty({**fixed, f"{self.agency}#f00000": meta})) - base
        count, extra = divmod(prospective - base, step)
        names = []
        for i in range(count):
            pad = min(34, extra)
            extra -= pad
            names.append(f"f{i:05d}" + "x" * pad)
        self.assertEqual(extra, 0)
        entries = {f"{self.agency}#{name}": dict(meta) for name in names}
        self.assertEqual(len(self.pretty({**fixed, **entries})), prospective)
        return entries

    def stamps(self):
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.kit.rglob("*") if p.is_file()}

    def test_repeat_store_growth_refused_before_draft_creation(self):
        self.sent_main()
        entries = self.filler(self.LIMIT + 100, "capacity")  # a near-full store that one more entry takes past 1 MiB
        self.store.write_bytes(self.pretty(entries))
        self.store.chmod(0o600)
        self.assertLess(self.store.stat().st_size, self.LIMIT)
        before = self.stamps()
        self.refused(lambda: self.repeat("capacity"), "kit/repeat-drafts.json " + self.FULL)
        self.assertEqual(self.stamps(), before)  # bytes and mtimes of the store, sent.json and every draft
        self.assertFalse(os.path.lexists(self.draft("capacity")))
        self.assertEqual(list(self.kit.glob(".repeat-drafts.*")), [])
        self.assertEqual(kit_repeat.read_repeats(self.root), entries)  # still readable, no history dropped

    def test_compact_store_expansion_is_checked_after_serialization(self):
        self.sent_main()
        entries = self.filler(self.LIMIT + 2000, "grow")
        compact = json.dumps({"schema_version": 1, "requests": entries}, separators=(",", ":")).encode()
        self.assertLess(len(compact), self.LIMIT)  # the old file fits ...
        self.assertGreater(len(self.pretty(entries)), self.LIMIT)  # ... but its rewrite in the writer's format would not
        self.store.write_bytes(compact)
        self.refused(lambda: self.repeat("grow"), self.FULL)
        self.assertEqual(self.store.read_bytes(), compact)  # existing formatting kept, not rewritten
        self.assertEqual(kit_repeat.read_repeats(self.root), entries)

    def test_repeat_store_exact_byte_limit_round_trips(self):
        self.sent_main()
        sha = self.sent_entry()["draft_sha256"]
        for size, new in ((self.LIMIT - 1, "edge1"), (self.LIMIT, "edge2")):
            entries = self.filler(size, new)
            self.store.write_bytes(self.pretty(entries))
            out = self.repeat(new)
            meta = {"follows": "main", "date_from": "2026-10-01", "date_to": "2026-11-01",
                    "source_sha256": sha, "draft_sha256": out["draft_sha256"]}
            expected = {**entries, f"{self.agency}#{new}": meta}
            self.assertEqual(self.store.read_bytes(), self.pretty(expected), size)  # the final newline included
            self.assertEqual(self.store.stat().st_size, size)
            self.assertEqual(hashlib.sha256(self.draft(new).read_bytes()).hexdigest(), out["draft_sha256"])
            for path in (self.store, self.draft(new)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(kit_repeat.read_repeats(self.root), dict(sorted(expected.items())))
        entries = self.filler(self.LIMIT + 1, "edge3")
        self.store.write_bytes(self.pretty(entries))
        self.refused(lambda: self.repeat("edge3"), self.FULL)
        self.assertFalse(os.path.lexists(self.draft("edge3")))

    def test_capacity_cli_refusal_keeps_existing_chain_usable(self):
        self.sent_main()
        self.repeat("bound")  # a valid follow-on made while the store was small
        bound = kit_repeat.read_repeats(self.root)
        self.store.write_bytes(self.pretty({**bound, **self.filler(self.LIMIT + 1, "capacity", keep=bound)}))
        before = self.stamps()
        with mock.patch("socket.socket", side_effect=AssertionError("network used")):
            code, out, err = run("kit-repeat", "--directory", self.root, "--agency", self.agency, "--request", "main",
                                 "--as", "capacity", "--through", "2026-11-01")
        self.assertEqual(code, 1)
        self.assertIn("kit/repeat-drafts.json would be larger than 1 MiB", err)
        self.assertNotIn("Unsent draft written", out)
        self.assertFalse(os.path.lexists(self.draft("capacity")))
        self.assertEqual(self.stamps(), before)
        code, _, err = run("kit-coverage", "--directory", self.root)
        self.assertEqual(code, 0, err)
        code, _, err = self.send("bound", "2026-11-01")  # the earlier draft can still be recorded
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent_entry("bound")["follows"], "main")

    def test_small_repeat_serialization_and_send_stay_unchanged(self):
        self.sent_main()
        source = self.draft().read_text(encoding="utf-8")
        self.assertEqual(source.count("2026-01-01 through 2026-09-30"), 2)  # the range line and the Subject
        sha = self.sent_entry()["draft_sha256"]
        stored = {}
        for new in ("small", "small2"):
            out = self.repeat(new)
            body = source.replace("2026-01-01 through 2026-09-30", "2026-10-01 through 2026-11-01").encode()
            self.assertEqual(self.draft(new).read_bytes(), body)
            stored[f"{self.agency}#{new}"] = {"follows": "main", "date_from": "2026-10-01", "date_to": "2026-11-01",
                                               "source_sha256": sha, "draft_sha256": hashlib.sha256(body).hexdigest()}
            self.assertEqual(out["draft_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(self.store.read_bytes(), self.pretty(stored))
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertEqual(self.send("small", "2026-11-01")[0], 0)
        self.assertEqual(self.sent_entry("small")["follows"], "main")
        _, csv_out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertEqual(len([r for r in csv_out.splitlines() if r.startswith(self.agency + ",") and ",small," in r]), 1)

    def test_capacity_validation_precedes_output_io(self):
        self.sent_main()
        self.store.write_bytes(self.pretty(self.filler(self.LIMIT + 1, "spied")))
        target = self.draft("spied")
        with mock.patch.object(kit_repeat.os, "open", wraps=os.open) as opened, \
                mock.patch.object(kit_repeat.tempfile, "mkstemp", wraps=tempfile.mkstemp) as made, \
                mock.patch.object(kit_repeat.os, "replace", wraps=os.replace) as replaced:
            self.refused(lambda: self.repeat("spied"), self.FULL)
        self.assertEqual([c for c in opened.call_args_list if str(c.args[0]) == str(target)], [])
        made.assert_not_called()
        replaced.assert_not_called()
        # under the limit a failed replace still rolls back: no draft, the store and its folder untouched
        self.store.write_bytes(self.pretty(self.filler(self.LIMIT, "spied")))
        before = self.snapshot()
        with mock.patch("campaign_tool.kit_repeat.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.repeat("spied")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
