"""RF0804-4: ``content agency-sharing`` is confined to the selected campaign's own content directory.

Two disjoint fictional campaigns are copied from the repository example into a temporary directory; nothing outside it is
read or written, and no network, build or publication is involved.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content, content_sharing
from campaign_tool.content import MAX_JSON_BYTES, ContentError
from campaign_tool.content_sharing import SharingImportError, import_sharing

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
AGENCY = "ca-cedar-police"
URL = "https://transparency.flocksafety.com/cedar-police"
CHECKED = "2026-10-31"
BASIC = "name,state,kind\nCedar Hills PD,CA,local\nReno Test PD,NV,local\nFictional Federal Office,US,federal\n"


def tree(root):
    """Every path under ``root`` (links not followed) with kind, mode, mtime, bytes or link target."""
    found = {}
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = os.path.join(here, name)
            info = os.lstat(path)
            body = os.readlink(path) if stat.S_ISLNK(info.st_mode) else (Path(path).read_bytes() if stat.S_ISREG(info.st_mode) else None)
            mtime = None if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns  # a directory's own mtime moves with any entry change
            found[os.path.relpath(path, root)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), mtime, body)
    return found


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-boundary-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.a, self.b = self.tmp / "campaign-a", self.tmp / "campaign-b"
        for root in (self.a, self.b):
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            path = root / "content" / "agencies.json"
            path.write_text(json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.csv = self.tmp / "sharing.csv"
        self.csv.write_text(BASIC, encoding="utf-8", newline="")

    def run_import(self, root=None, **kwargs):
        arguments = dict(agency_id=AGENCY, csv_path=self.csv, checked_on=CHECKED, source_url=URL, today=TODAY)
        arguments.update(kwargs)
        return import_sharing(root or self.a, **arguments)

    def link_a_content_to_b(self):
        shutil.rmtree(self.a / "content")
        (self.a / "content").symlink_to(self.b / "content")

    def cli(self, root):
        arguments = [sys.executable, "-B", "-m", "campaign_tool", "content", "agency-sharing", "--directory", str(root), "--agency", AGENCY,
                     "--csv", str(self.csv), "--checked-on", CHECKED, "--source-url", URL]
        return subprocess.run(arguments, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def assertRefusedUntouched(self, root, *fragments, **kwargs):
        before = tree(self.tmp)
        with self.assertRaises(SharingImportError) as caught:
            self.run_import(root, **kwargs)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertNotIn(str(self.tmp), str(caught.exception))
        self.assertEqual(before, tree(self.tmp))
        return str(caught.exception)


class ContentDirectoryTests(Base):
    def test_content_symlink_refuses_without_reading_or_mutating_other_campaign(self):
        self.link_a_content_to_b()
        message = self.assertRefusedUntouched(self.a, "content/")
        self.assertNotIn("campaign-b", message)
        self.assertTrue((self.a / "content").is_symlink())
        self.assertNotIn(b"Reno Test PD", (self.b / "content" / "agencies.json").read_bytes())

    def test_dangling_and_non_directory_content_refuse(self):
        for make in (lambda p: p.symlink_to(self.tmp / "nowhere"),               # dangling link
                     lambda p: p.write_text("not a directory"),                  # a file
                     lambda p: os.mkfifo(p)):                                    # a special file: must not block
            with self.subTest(make=make):
                if (self.a / "content").is_dir() and not (self.a / "content").is_symlink():
                    shutil.rmtree(self.a / "content")
                elif os.path.lexists(self.a / "content"):
                    (self.a / "content").unlink()
                make(self.a / "content")
                self.assertRefusedUntouched(self.a, "content/")
        (self.a / "content").unlink()
        self.assertRefusedUntouched(self.a, "content/")  # missing

    def test_real_command_refuses_a_linked_content_directory_with_one_stopped_line(self):
        self.link_a_content_to_b()
        before = tree(self.tmp)
        done = self.cli(self.a)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr.count("Stopped:"), 1, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("content/", done.stderr)
        for leaked in ("Traceback", "campaign-b", URL, str(self.tmp)):
            self.assertNotIn(leaked, done.stderr + done.stdout)
        self.assertEqual(before, tree(self.tmp))

    def test_content_directory_swap_does_not_redirect_write(self):
        aside = self.a / "content-original"
        original = (self.a / "content" / "agencies.json").read_bytes()

        def swap():
            (self.a / "content").rename(aside)
            (self.a / "content").symlink_to(self.b / "content")
        before_b = tree(self.b)
        with mock.patch.object(content_sharing, "_before_install", side_effect=swap):
            with self.assertRaises(SharingImportError) as caught:
                self.run_import()
        self.assertIn("content/ changed", str(caught.exception))
        self.assertEqual(before_b, tree(self.b))  # the other campaign: nothing written, no temporary file
        self.assertEqual((aside / "agencies.json").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in aside.iterdir() if p.name.endswith(".tmp")), [])
        self.assertTrue((self.a / "content").is_symlink())


class TargetFileTests(Base):
    def target(self):
        return self.a / "content" / "agencies.json"

    def test_leaf_nonregular_and_bounded_target_reads(self):
        target = self.target()
        good = target.read_bytes()
        outside = self.tmp / "outside.json"
        outside.write_bytes(good)

        def linked():
            target.unlink()
            target.symlink_to(outside)

        def dangling():
            target.unlink()
            target.symlink_to(self.tmp / "nowhere.json")

        def fifo():
            target.unlink()
            os.mkfifo(target)

        def directory():
            target.unlink()
            target.mkdir()

        def missing():
            target.unlink()
        cases = {"linked": (linked, "content/agencies.json"), "dangling": (dangling, "content/agencies.json"),
                 "fifo": (fifo, "regular file"), "directory": (directory, "regular file"), "missing": (missing, "not found"),
                 "bad utf-8": (lambda: target.write_bytes(b"[\xff]"), "UTF-8 JSON"), "bad json": (lambda: target.write_bytes(b"[{"), "UTF-8 JSON"),
                 "one over the cap": (lambda: target.write_bytes(good + b" " * (MAX_JSON_BYTES + 1 - len(good))), "too large")}
        for label, (make, fragment) in cases.items():
            with self.subTest(label):
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    target.rmdir()
                elif os.path.lexists(target):
                    target.unlink()
                target.write_bytes(good)
                make()
                self.assertRefusedUntouched(self.a, fragment)
        target.unlink()
        target.write_bytes(good + b" " * (MAX_JSON_BYTES - len(good)))  # exactly the cap is still read
        self.assertEqual(target.stat().st_size, MAX_JSON_BYTES)
        self.assertEqual(self.run_import()["recipients"], 3)

    def swap_cases(self):
        target = self.target()

        def replaced():
            new = target.with_name("other.json")
            new.write_text("[]\n")
            os.replace(new, target)
            return b"[]\n"

        def removed():
            target.unlink()
            return None

        def retargeted():
            outside = self.tmp / "outside.json"
            outside.write_text("[]\n")
            target.unlink()
            target.symlink_to(outside)
            return "link"

        def edited_in_place():  # same size, mtime put back: the change still shows in the bytes and ctime
            data, info = target.read_bytes(), target.stat()
            edited = data.replace(b"Cedar", b"Cadar", 1)
            with open(target, "r+b") as handle:
                handle.write(edited)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            return edited
        return {"replaced": replaced, "removed": removed, "retargeted": retargeted, "edited in place": edited_in_place}

    def test_target_swap_or_in_place_edit_preserves_newer_bytes(self):
        for replace in (False, True):
            for label, change in self.swap_cases().items():
                with self.subTest(label=label, replace=replace):
                    shutil.rmtree(self.a)
                    shutil.copytree(self.b, self.a)
                    if replace:
                        self.run_import()  # a list already there
                    held = {}
                    with mock.patch.object(content_sharing, "_before_install", side_effect=lambda c=change: held.update(now=c())):
                        with self.assertRaises(SharingImportError) as caught:
                            self.run_import(replace=replace)
                    self.assertIn("nothing was written", str(caught.exception))
                    now = held["now"]
                    if now is None:
                        self.assertFalse(os.path.lexists(self.target()))
                    elif now == "link":
                        self.assertTrue(self.target().is_symlink())
                        self.assertEqual((self.tmp / "outside.json").read_text(), "[]\n")
                    else:
                        self.assertEqual(self.target().read_bytes(), now)
                    self.assertEqual([p.name for p in self.target().parent.iterdir() if p.name.endswith(".tmp")], [])


class FailureTests(Base):
    def test_write_fsync_replace_failures_preserve_previous_file(self):
        target = self.a / "content" / "agencies.json"
        os.chmod(target, 0o640)
        bystander = self.a / "content" / ".agencies.json.bystander.tmp"
        bystander.write_text("not ours")
        before = tree(self.a)
        for name in ("write", "fsync", "replace"):
            with self.subTest(name):
                opened, real_open, real_close = set(), os.open, os.close

                def tracking_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.add(fd)
                    return fd

                def tracking_close(fd):
                    opened.discard(fd)
                    real_close(fd)
                with mock.patch.object(os, "open", tracking_open), mock.patch.object(os, "close", tracking_close), \
                        mock.patch.object(os, name, side_effect=OSError(28, "No space left on device")):
                    with self.assertRaises(OSError):
                        self.run_import()
                self.assertEqual(opened, set())  # every descriptor closed
                self.assertEqual(before, tree(self.a))  # bytes, mode and mtime kept; only the bystander temp file remains
                self.assertEqual(bystander.read_text(), "not ours")


class NormalImportTests(Base):
    def test_normal_import_and_explicit_replacement_are_unchanged(self):
        content_modes = {root: stat.S_IMODE(os.stat(root / "content").st_mode) for root in (self.a, self.b)}
        base = json.loads((self.a / "content" / "agencies.json").read_text())
        before_b = tree(self.b)
        self.csv.write_bytes(b"\xef\xbb\xbf" + "name,state,kind\nZulu PD,NV,local\nCaf\u00e9 PD,CA,local\n".encode("utf-8"))
        self.assertEqual(self.run_import(self.a), {"agency_id": AGENCY, "recipients": 2, "checked_on": CHECKED, "source_url": URL})
        self.assertEqual(before_b, tree(self.b))
        text = (self.a / "content" / "agencies.json").read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(text, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual([r["name"] for r in next(a for a in document if a["agency_id"] == AGENCY)["profile"]["sharing"]["recipients"]],
                         ["Caf\u00e9 PD", "Zulu PD"])  # sorted by state then name
        self.assertEqual(stat.S_IMODE(os.stat(self.a / "content" / "agencies.json").st_mode), 0o600)
        self.run_import(self.b)  # the other campaign, on its own
        after_a = tree(self.a)
        with self.assertRaises(SharingImportError):
            self.run_import(self.a)  # a list exists: needs --replace
        self.csv.write_text(BASIC, encoding="utf-8", newline="")
        self.assertEqual(self.run_import(self.a, replace=True)["recipients"], 3)
        document = json.loads((self.a / "content" / "agencies.json").read_text(encoding="utf-8"))
        for agency in document:
            if agency["agency_id"] == AGENCY:
                del agency["profile"]["sharing"]
        for agency in base:
            if agency["agency_id"] == AGENCY:
                agency.get("profile", {}).pop("sharing", None)
        self.assertEqual(document, base)  # every other agency and field as it was
        self.assertNotEqual(after_a, tree(self.a))
        for root in (self.a, self.b):
            self.assertEqual(stat.S_IMODE(os.stat(root / "content").st_mode), content_modes[root])
        self.assertTrue(issubclass(SharingImportError, ContentError))


PAD = 20000  # the per-field plain-text limit: padding is spread over many valid profiles, none truncated


def canonical(document):
    """The writer's format, computed here independently: two-space indent, raw UTF-8, one trailing newline."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def compact(document):
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def padded(document, total, encode=canonical):
    """``document`` plus retained selected profiles whose ASCII summaries make ``encode`` exactly ``total`` bytes."""
    count = 0
    while len(encode(document + retained(count))) + count * PAD < total:
        count += 1
    extra = retained(count)
    remaining = total - len(encode(document + extra))
    assert 0 <= remaining <= count * PAD, remaining
    for entry in extra:
        entry["profile"]["summary"] = "a" * min(PAD, remaining)
        remaining -= len(entry["profile"]["summary"])
    result = document + extra
    assert len(encode(result)) == total
    return result


def retained(count):
    return [{"agency_id": f"ca-retained-{i}-police", "name": f"Retained Police {i}", "profile": {"summary": ""}} for i in range(count)]


def with_sharing(document, recipients):
    """A copy of ``document`` with the sharing list the import is expected to store for ``AGENCY``."""
    result = json.loads(json.dumps(document))
    for entry in result:
        if entry["agency_id"] == AGENCY:
            entry.setdefault("profile", {})["sharing"] = {"checked_on": CHECKED, "source_url": URL, "recipients": recipients}
    return result


def without_sharing(document):
    result = json.loads(json.dumps(document))
    for entry in result:
        if entry["agency_id"] == AGENCY:
            entry["profile"].pop("sharing", None)
    return result


ONE_CSV = "name,state,kind\nCedar Hills PD,CA,local\n"
ONE = [{"name": "Cedar Hills PD", "state": "CA", "kind": "local"}]
CAP_REFUSAL = f"content/agencies.json: import would exceed {MAX_JSON_BYTES} bytes; nothing was written"
ENCODING_REFUSAL = "content/agencies.json: import cannot be encoded as UTF-8; nothing was written"


class SizeLimitTests(Base):
    """RF1835-2: the complete prospective document is measured in written bytes before any temporary file exists."""

    def setUp(self):
        super().setUp()
        self.target = self.a / "content" / "agencies.json"
        self.example = json.loads(self.target.read_text(encoding="utf-8"))
        self.csv.write_text(ONE_CSV, encoding="utf-8", newline="")

    def readable(self):
        """The bounded content reader (as build uses it) still admits the stored file."""
        return content.load_agencies(content._read_json(self.target), "content/agencies.json", TODAY)

    def candidate(self, size, recipients=ONE):
        """Write an original whose import of ``recipients`` produces exactly ``size`` canonical bytes; -> expected bytes."""
        expected = padded(with_sharing(self.example, recipients), size)
        self.target.write_bytes(canonical(without_sharing(expected)))
        self.readable()
        return canonical(expected)

    def assertRefusedBeforeOutput(self, message, **kwargs):
        info = os.stat(self.target)
        with mock.patch.object(content_sharing.secrets, "token_hex", wraps=content_sharing.secrets.token_hex) as naming, \
                mock.patch.object(content_sharing, "_before_install") as install, \
                mock.patch.object(os, "fsync", wraps=os.fsync) as fsync, mock.patch.object(os, "replace", wraps=os.replace) as replace:
            got = self.assertRefusedUntouched(self.a, **kwargs)
        self.assertEqual(got, message)
        for seam in (naming, install, fsync, replace):
            seam.assert_not_called()  # no temporary name, file, fsync or replacement was ever made
        now = os.stat(self.target)
        self.assertEqual((now.st_ino, now.st_mode, now.st_mtime_ns, now.st_size), (info.st_ino, info.st_mode, info.st_mtime_ns, info.st_size))
        self.readable()
        return got

    def test_canonical_near_cap_sharing_add_refuses_without_touching_store(self):
        self.candidate(MAX_JSON_BYTES + 1)
        self.assertLess(self.target.stat().st_size, MAX_JSON_BYTES)  # the old file is readable and below the cap
        self.assertRefusedBeforeOutput(CAP_REFUSAL)
        stored = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertNotIn("sharing", next(a for a in stored if a["agency_id"] == AGENCY)["profile"])

    def test_compact_input_expansion_is_checked_as_written(self):
        document = padded(self.example, MAX_JSON_BYTES - 100, compact)
        self.target.write_bytes(compact(document))
        self.readable()
        self.assertGreater(len(canonical(with_sharing(document, ONE))), MAX_JSON_BYTES)
        self.assertRefusedBeforeOutput(CAP_REFUSAL)
        self.assertEqual(self.target.read_bytes(), compact(document))

    def test_candidate_exact_limit_and_one_byte_neighbors(self):
        for size in (MAX_JSON_BYTES - 1, MAX_JSON_BYTES, MAX_JSON_BYTES + 1):
            with self.subTest(size=size):
                expected = self.candidate(size)
                self.assertEqual(len(expected), size)
                self.assertTrue(expected.endswith(b"}\n]\n"))
                if size > MAX_JSON_BYTES:
                    self.assertRefusedBeforeOutput(CAP_REFUSAL)
                    continue
                self.assertEqual(self.run_import(), {"agency_id": AGENCY, "recipients": 1, "checked_on": CHECKED, "source_url": URL})
                self.assertEqual(self.target.read_bytes(), expected)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
                self.readable()
                self.assertEqual(sorted(p.name for p in self.target.parent.iterdir() if p.name.endswith(".tmp")), [])

    def test_non_ascii_content_counts_utf8_bytes_not_characters(self):
        self.csv.write_text("name,state,kind\nCaf\u00e9 PD,CA,local\n", encoding="utf-8", newline="")
        cafe = [{"name": "Caf\u00e9 PD", "state": "CA", "kind": "local"}]
        # 34 summaries of 20,000 three-byte characters each: the candidate is far below the cap in characters.
        self.example += [{"agency_id": f"ca-wide-{i}-police", "name": f"Wide Police {i}", "profile": {"summary": "\u540d" * PAD}}
                         for i in range(34)]
        for size in (MAX_JSON_BYTES, MAX_JSON_BYTES + 1):
            with self.subTest(size=size):
                written = self.candidate(size, cafe)
                self.assertLess(len(written.decode("utf-8")), MAX_JSON_BYTES - 1_000_000)
                if size > MAX_JSON_BYTES:
                    self.assertRefusedBeforeOutput(CAP_REFUSAL)
                    continue
                self.run_import()
                self.assertEqual(self.target.read_bytes(), written)  # raw UTF-8, not escapes
                self.assertIn("Caf\u00e9 PD".encode("utf-8"), written)
                self.readable()

    def test_replace_larger_and_smaller_sharing_lists_obey_same_cap(self):
        two = [{"name": "Cedar Hills PD", "state": "CA", "kind": "local"}, {"name": "Reno Test PD", "state": "NV", "kind": "local"}]
        # The near-cap campaign also holds a deselected agency whose profile is kept exactly as it is.
        aside = {"agency_id": "ca-set-aside-police", "name": "Set Aside", "selected": False, "profile": {"summary": "kept <as is>"}}
        self.example.append(aside)
        expected = padded(with_sharing(self.example, two), MAX_JSON_BYTES)
        self.target.write_bytes(canonical(expected))
        self.readable()
        self.csv.write_text(BASIC, encoding="utf-8", newline="")  # three recipients: larger than the stored two
        self.assertRefusedBeforeOutput(CAP_REFUSAL, replace=True)
        self.assertEqual(self.target.read_bytes(), canonical(expected))
        self.csv.write_text(ONE_CSV, encoding="utf-8", newline="")  # one recipient: smaller
        self.assertEqual(self.run_import(replace=True)["recipients"], 1)
        stored = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(self.target.read_bytes(), canonical(with_sharing(expected, ONE)))
        self.assertIn(aside, stored)
        self.assertEqual([len(a.get("profile", {}).get("summary", "")) for a in stored], [len(a.get("profile", {}).get("summary", "")) for a in expected])
        self.readable()

    def test_encoding_refusal_is_safe_and_pre_temp(self):
        # An escaped lone surrogate in an unrelated summary is admitted by the reader but cannot be written as UTF-8.
        self.example.append({"agency_id": "ca-surrogate-police", "name": "Surrogate", "profile": {"summary": "x\ud800y"}})
        self.target.write_bytes((json.dumps(self.example, indent=2) + "\n").encode("ascii"))
        self.readable()
        message = self.assertRefusedBeforeOutput(ENCODING_REFUSAL)
        self.assertNotIn("ud800", message.lower())
        self.assertNotIn("surrogate", message)

    def test_cli_oversize_refuses_and_no_partial_artifacts(self):
        self.candidate(MAX_JSON_BYTES + 1)
        original, before = self.target.read_bytes(), tree(self.tmp)
        first = self.cli(self.a)
        again = self.cli(self.a)
        for done in (first, again):
            self.assertEqual(done.returncode, 1, done.stderr)  # the CLI's existing exit for every agency-sharing refusal
            self.assertEqual(done.stdout, "")
            self.assertEqual(done.stderr.splitlines(), [f"Stopped: {CAP_REFUSAL}"])
        self.assertEqual(before, tree(self.tmp))
        self.assertEqual(self.target.read_bytes(), original)
        self.readable()

    def test_cli_import_at_exactly_the_cap_is_read_back(self):
        expected = self.candidate(MAX_JSON_BYTES)
        done = self.cli(self.a)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Recorded only. Nothing was fetched or published.", done.stdout)
        self.assertEqual(self.target.read_bytes(), expected)
        self.readable()

    def test_successful_import_and_boundary_failures_keep_existing_installer_contract(self):
        self.candidate(MAX_JSON_BYTES)
        os.chmod(self.target, 0o640)
        before = tree(self.a)
        for name in ("write", "fsync", "replace"):
            with self.subTest(name), mock.patch.object(os, name, side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    self.run_import()
                self.assertEqual(before, tree(self.a))
        self.run_import()
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.readable()


if __name__ == "__main__":
    unittest.main()
