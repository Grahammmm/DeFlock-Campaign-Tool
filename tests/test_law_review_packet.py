"""Offline tests for ``campaign_tool law review-packet`` (BF-df-law-review).

The packet is a question for a human reviewer, not a review: these tests check that it is built from the
existing review-sheet output, orders rules by their own ``review`` field, asserts nothing and changes nothing.
Run from the repository root: python3 -B -m unittest tests.test_law_review_packet -v
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, law, law_packet  # noqa: E402

AS_OF = "2026-11-03"
FILES = {"PACKET.md", "RULES.md", "WORKSHEET.csv", "RETURN.md"}
ASSERTS = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)
ADVISES = re.compile(r"\b(sue|lawsuit|file (a|an|suit)|petition|writ|demand|complain|you should)\b", re.I)


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def generated_text(text, package):
    """The packet's own sentences: every string quoted from the package removed, longest first."""
    for quoted in sorted(set(strings(package)), key=len, reverse=True):
        if len(quoted) > 2:
            text = text.replace(quoted, "")
    return text


class ReviewPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-packet-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "law", "review-packet", *arguments],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def packet(self, name="packet", *extra):
        out = self.tmp / name
        result = json.loads(self.cli("--jurisdiction", "us-ca", "--out", str(out), "--as-of", AS_OF, *extra).stdout)
        return out, result

    def synthetic_base(self, package):
        base = self.tmp / "base"
        (base / "us-ca").mkdir(parents=True)
        (base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")
        return base

    def test_clean_run_writes_four_private_files_and_worksheet_equals_review_sheet(self):
        out, result = self.packet()
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["package_status"], "draft")
        self.assertEqual({p.name for p in out.iterdir()}, FILES)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for path in out.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        sheet = self.tmp / "sheet.csv"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "review-sheet", "us-ca", "--out", str(sheet),
                        "--as-of", AS_OF], cwd=REPO_ROOT, check=True, capture_output=True, timeout=30)
        self.assertEqual((out / "WORKSHEET.csv").read_bytes(), sheet.read_bytes())

    def test_apply_sheet_round_trips_the_packet_worksheet_unchanged(self):
        out, _ = self.packet()
        package = law.load_package("us-ca")
        rows = law.read_sheet(out / "WORKSHEET.csv", package)
        self.assertEqual([row["rule_id"] for row in rows],
                         ["records_law"] + [rule["rule_id"] for rule in package["rules"]])
        proposed, report = law.apply_sheet(package, rows)
        self.assertEqual(proposed, package)
        self.assertEqual((report["changed"], report["refused"], report["text_edited"]), ([], [], []))
        self.assertEqual(report["skipped"], len(rows))
        cli_out = self.tmp / "proposed.json"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "apply-sheet", "us-ca", "--sheet",
                        str(out / "WORKSHEET.csv"), "--out", str(cli_out)], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(json.loads(cli_out.read_text(encoding="utf-8")), package)

    def test_return_instructions_bind_the_worksheet_and_a_stale_return_is_refused(self):
        out, _ = self.packet()
        instructions = (out / "RETURN.md").read_text(encoding="utf-8")
        for phrase in ("`package_sha256`", "never edit or copy it", "builds a new packet", "reassess"):
            self.assertIn(phrase, instructions)
        package = law.load_package("us-ca")
        self.assertNotIn(law.package_sha256(package), instructions)
        rows = law.read_sheet(out / "WORKSHEET.csv", package)
        self.assertEqual({row["package_sha256"] for row in rows}, {law.package_sha256(package)})
        amended = copy.deepcopy(package)
        amended["rules"][0]["citation"] += " (amended)"
        with self.assertRaisesRegex(ValueError, r"row 1 \(records_law\): worksheet belongs to a different law "
                                                r"package; regenerate review-sheet or review-packet, nothing written"):
            law.apply_sheet(amended, rows)

    def test_rules_md_lists_needs_attorney_review_first_then_likely_then_verified(self):
        out, result = self.packet()
        self.assertEqual(result["review_counts"], {"verified": 8, "likely": 1, "needs_attorney_review": 9})
        listed = re.findall(r"^## \d+\. (\S+)$", (out / "RULES.md").read_text(encoding="utf-8"), re.M)
        labels = {rule["rule_id"]: rule["review"] for rule in law.load_package("us-ca")["rules"]}
        self.assertEqual(sorted(listed), sorted(labels))
        self.assertEqual([labels[rule_id] for rule_id in listed],
                         ["needs_attorney_review"] * 9 + ["likely"] + ["verified"] * 8)

    def test_order_comes_from_the_review_field_not_the_text(self):
        package = copy.deepcopy(law.load_package("us-ca"))
        decoy = copy.deepcopy(package["rules"][1])
        decoy.update(rule_id="ca-verified-decoy", citation="Synthetic verified citation",
                     duty="Verified. This reading is verified.", review="needs_attorney_review")
        for rule in package["rules"]:
            rule["review"] = "verified"
        package["rules"].append(decoy)
        out, _ = self.packet("decoy", "--base", str(self.synthetic_base(package)))
        listed = re.findall(r"^## \d+\. (\S+)$", (out / "RULES.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual(listed[0], "ca-verified-decoy")
        self.assertEqual(listed[1:], [rule["rule_id"] for rule in package["rules"][:-1]])

    def test_reviewed_package_leaves_doctor_unchanged_and_packet_still_asserts_nothing(self):
        with mock.patch.object(law, "package_status", return_value=("draft", None)):
            draft = cli.law_package_report({"state": "CA"})
        self.assertEqual(draft, {"law_package_status": "draft", "reviewed_law_package": False})
        root = self.tmp / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        actions = cli.doctor_report(root)["next_actions"]
        self.assertEqual(actions.count(cli.LAW_REVIEW_ACTION), 1)
        self.assertIn("independent of the author", cli.LAW_REVIEW_ACTION)
        self.assertIn("campaign-tool law review-packet", cli.LAW_REVIEW_ACTION)
        with mock.patch.object(law, "package_status", return_value=("reviewed", None)):
            reviewed = cli.doctor_report(root)
        self.assertIs(reviewed["reviewed_law_package"], True)
        self.assertEqual(reviewed["next_actions"], [a for a in actions if a != cli.LAW_REVIEW_ACTION])
        package = copy.deepcopy(law.load_package("us-ca"))
        package.update(status="reviewed", reviewed_by=["synthetic-reviewer"], reviewed_at="2026-10-01")
        out, result = self.packet("reviewed", "--base", str(self.synthetic_base(package)))
        self.assertEqual(result["package_status"], "reviewed")
        for name in ("PACKET.md", "RULES.md", "RETURN.md"):
            own = generated_text((out / name).read_text(encoding="utf-8"), package)
            self.assertIsNone(ASSERTS.search(own), name)

    def test_refusals_create_nothing(self):
        existing = self.tmp / "exists"
        existing.mkdir()
        real = self.tmp / "real"
        real.mkdir()
        (self.tmp / "link").symlink_to(real, target_is_directory=True)
        for out in (existing, self.tmp / "link" / "packet", REPO_ROOT / "packet-in-repo", Path("relative-packet"),
                    Path(str(self.tmp) + "/x/../packet"), self.tmp / "missing-parent" / "packet"):
            with self.subTest(out=str(out)):
                before = sorted(os.listdir(self.tmp)), sorted(os.listdir(REPO_ROOT))
                result = self.cli("--jurisdiction", "us-ca", "--out", str(out), returncode=2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertTrue(result.stderr.startswith("Stopped: --out"))
                self.assertEqual((sorted(os.listdir(self.tmp)), sorted(os.listdir(REPO_ROOT))), before)
        self.assertEqual(os.listdir(existing), [])
        self.assertEqual(os.listdir(real), [])
        missing = self.cli("--jurisdiction", "us-zz", "--out", str(self.tmp / "zz"), returncode=2)
        self.assertTrue(missing.stderr.startswith("Stopped: No law package"))
        self.assertFalse((self.tmp / "zz").exists())

    def test_mid_packet_write_failure_leaves_no_out_and_retry_succeeds(self):
        out = self.tmp / "nospace"
        real_open = os.open

        def failing_open(target, *rest, **kwargs):
            if Path(target).name == "RULES.md":
                raise OSError(28, "Synthetic no space left on device")
            return real_open(target, *rest, **kwargs)

        args = argparse.Namespace(jurisdiction="us-ca", out=str(out), as_of=AS_OF, base=None)
        with mock.patch.object(law_packet.os, "open", failing_open), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(law_packet.main(args), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Stopped: [Errno 28] Synthetic no space left on device\n")
        self.assertFalse(os.path.lexists(out))
        _, result = self.packet("nospace")
        self.assertEqual({p.name for p in out.iterdir()}, FILES)
        self.assertEqual(result["out"], str(out))

    def run_main(self, out):
        args = argparse.Namespace(jurisdiction="us-ca", out=str(out), as_of=AS_OF, base=None)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = law_packet.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def swap_to_link(self, directory, target):
        """Move a real directory aside and leave a symlink to ``target`` at its pathname."""
        parked = Path(str(directory) + ".parked")
        os.rename(directory, parked)
        os.symlink(target, directory, target_is_directory=True)
        return parked

    def test_parent_symlink_swap_after_admission_refuses(self):
        parent, target = self.tmp / "parent", self.tmp / "target"
        parent.mkdir()
        target.mkdir()
        real_load = law.load_package

        def swapping_load(*args):
            self.swap_to_link(parent, target)
            return real_load(*args)

        with mock.patch.object(law, "load_package", swapping_load):
            code, stdout, stderr = self.run_main(parent / "packet")
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(len(stderr.splitlines()), 1)
        self.assertTrue(stderr.startswith("Stopped:"), stderr)
        self.assertEqual(os.listdir(target), [])
        self.assertEqual(os.listdir(self.tmp / "parent.parked"), [])

    def test_intermediate_ancestor_symlink_swap_refuses(self):
        ancestor, target = self.tmp / "ancestor", self.tmp / "target"
        (ancestor / "inner").mkdir(parents=True)
        (target / "inner").mkdir(parents=True)
        real_load = law.load_package

        def swapping_load(*args):
            self.swap_to_link(ancestor, target)
            return real_load(*args)

        with mock.patch.object(law, "load_package", swapping_load):
            code, stdout, stderr = self.run_main(ancestor / "inner" / "packet")
        self.assertEqual((code, stdout), (2, ""))
        self.assertTrue(stderr.startswith("Stopped:"), stderr)
        self.assertEqual(os.listdir(target / "inner"), [])
        self.assertEqual(os.listdir(self.tmp / "ancestor.parked" / "inner"), [])

    def test_directory_retarget_after_anchor_never_redirects_members(self):
        parent, target = self.tmp / "parent", self.tmp / "target"
        parent.mkdir()
        target.mkdir()
        real_open_parent = law_packet._open_parent

        def retargeting(path):
            fd = real_open_parent(path)
            self.swap_to_link(parent, target)
            return fd

        with mock.patch.object(law_packet, "_open_parent", retargeting):
            law_packet.build("us-ca", parent / "packet", AS_OF)
        self.assertEqual(os.listdir(target), [])
        self.assertEqual({p.name for p in (self.tmp / "parent.parked" / "packet").iterdir()}, FILES)

    def test_output_symlink_arrival_before_directory_open_refuses(self):
        parent, target = self.tmp / "parent", self.tmp / "target"
        parent.mkdir()
        target.mkdir()
        out = parent / "packet"
        real_mkdir = os.mkdir

        def mkdir_then_swap(name, mode, **kwargs):
            real_mkdir(name, mode, **kwargs)
            os.rename(out, parent / "moved")
            os.symlink(target, out, target_is_directory=True)

        with mock.patch.object(law_packet.os, "mkdir", mkdir_then_swap):
            code, stdout, stderr = self.run_main(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertTrue(stderr.startswith("Stopped:"), stderr)
        self.assertEqual(os.listdir(target), [])
        self.assertTrue(out.is_symlink())

    def test_failure_rollback_uses_original_directory_descriptor(self):
        parent, target = self.tmp / "parent", self.tmp / "target"
        parent.mkdir()
        target.mkdir()
        (target / "PACKET.md").write_text("sentinel", encoding="utf-8")
        out = parent / "packet"
        real_open = os.open

        def failing_open(name, *rest, **kwargs):
            if name == "RULES.md":
                os.rename(out, parent / "moved")
                os.symlink(target, out, target_is_directory=True)
                raise OSError(28, "Synthetic no space left on device")
            return real_open(name, *rest, **kwargs)

        with mock.patch.object(law_packet.os, "open", failing_open):
            code, _, stderr = self.run_main(out)
        self.assertEqual(code, 2)
        self.assertEqual((target / "PACKET.md").read_text(encoding="utf-8"), "sentinel")
        self.assertTrue(out.is_symlink())
        self.assertEqual(os.listdir(parent / "moved"), [])

    def test_destination_arrival_and_unknown_member_preserved(self):
        out = self.tmp / "packet"
        real_load = law.load_package

        def arriving_load(*args):
            out.mkdir()
            (out / "mine.txt").write_text("keep", encoding="utf-8")
            return real_load(*args)

        with mock.patch.object(law, "load_package", arriving_load):
            code, stdout, stderr = self.run_main(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(os.listdir(out), ["mine.txt"])
        out2 = self.tmp / "packet2"
        real_open = os.open

        def failing_open(name, *rest, **kwargs):
            if name == "RULES.md":
                (out2 / "unknown.txt").write_text("keep", encoding="utf-8")
                raise OSError(28, "Synthetic no space left on device")
            return real_open(name, *rest, **kwargs)

        with mock.patch.object(law_packet.os, "open", failing_open):
            self.assertEqual(self.run_main(out2)[0], 2)
        self.assertEqual(os.listdir(out2), ["unknown.txt"])

    def test_descriptor_lifetime_and_ordinary_packet_compatibility(self):
        def open_fds():
            return len(os.listdir("/proc/self/fd"))

        before = open_fds()
        law_packet.build("us-ca", self.tmp / "ok", AS_OF)
        self.assertEqual(open_fds(), before)
        real_open = os.open

        def failing_at(which):
            def failing_open(name, *rest, **kwargs):
                if name == which:
                    raise OSError(28, "Synthetic no space left on device")
                return real_open(name, *rest, **kwargs)
            return failing_open

        for which, out in (("packet-a", self.tmp / "packet-a"), ("RULES.md", self.tmp / "packet-b")):
            with self.subTest(which=which), mock.patch.object(law_packet.os, "open", failing_at(which)):
                with self.assertRaises(OSError):
                    law_packet.build("us-ca", out, AS_OF)
            self.assertEqual(open_fds(), before)
        self.assertEqual({p.name for p in (self.tmp / "ok").iterdir()}, FILES)
        self.assertFalse(os.path.lexists(self.tmp / "packet-b"))

    def test_generated_text_asserts_no_breach_and_advises_no_legal_action(self):
        out, _ = self.packet()
        package = law.load_package("us-ca")
        for name in ("PACKET.md", "RULES.md", "RETURN.md"):
            own = generated_text((out / name).read_text(encoding="utf-8"), package)
            self.assertIsNone(ASSERTS.search(own), name)
            self.assertIsNone(ADVISES.search(own), name)
        self.assertNotIn("@", (out / "RETURN.md").read_text(encoding="utf-8"))

    def test_jurisdictions_unchanged_package_still_draft_and_launch_counts_match(self):
        roots = [REPO_ROOT / "jurisdictions", law.JURISDICTIONS]
        before = [tree_digest(root) for root in roots]
        self.packet()
        self.assertEqual([tree_digest(root) for root in roots], before)
        self.assertEqual(law.package_status("us-ca"), ("draft", None))
        counts = law.summary(law.load_package("us-ca"))["review_counts"]
        launch = (REPO_ROOT / "LAUNCH.md").read_text(encoding="utf-8")
        found = re.search(r"(\d+) rules verified against leginfo text, (\d+) marked likely, (\d+) marked "
                          r"needs_attorney_review", launch)
        self.assertEqual(tuple(map(int, found.groups())) if found else "LAUNCH.md counts line missing",
                         (counts["verified"], counts["likely"], counts["needs_attorney_review"]))

    def test_ordered_rules_is_stable_within_a_label(self):
        rules = [{"rule_id": n, "review": r} for n, r in (("a", "verified"), ("b", "likely"), ("c", "verified"),
                                                          ("d", "needs_attorney_review"))]
        self.assertEqual([r["rule_id"] for r in law_packet.ordered_rules({"rules": rules})], ["d", "b", "a", "c"])

    # --- rollback ownership of created members (RF0035-5) -------------------------------------------------

    def failing_build(self, out, fail_at, before=None):
        """Run build() with a synthetic open failure at ``fail_at``; ``before(out)`` runs just before it.

        Returns (error, fds) where ``fds`` is every descriptor os.open handed out during the build.
        """
        real_open = os.open
        fds = []

        def failing_open(name, *rest, **kwargs):
            if name == fail_at:
                if before:
                    before(out)
                raise OSError(28, "Synthetic no space left on device")
            fd = real_open(name, *rest, **kwargs)
            fds.append(fd)
            return fd

        with mock.patch.object(law_packet.os, "open", failing_open):
            with self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
        return caught.exception, fds

    def assert_closed(self, fds):
        for fd in fds:
            with self.assertRaises(OSError, msg=f"fd {fd} still open"):
                os.fstat(fd)

    def park_and_replace(self, name, content=b"replacement bytes"):
        """Return a ``before`` hook that renames the created member aside and writes a different file at its name."""
        record = {}

        def hook(out):
            parked = self.tmp / ("parked-" + name)
            original = out / name
            record["original"] = original.stat()
            os.rename(original, parked)
            original.write_bytes(content)
            os.chmod(original, 0o640)
            record.update(parked=parked, replacement=original.stat(), path=original)

        hook.record = record
        return hook

    def test_rollback_preserves_replaced_regular_member(self):
        for replaced in ("PACKET.md", "RULES.md", "WORKSHEET.csv"):
            with self.subTest(replaced=replaced):
                out = self.tmp / ("packet-" + replaced)
                hook = self.park_and_replace(replaced)
                error, fds = self.failing_build(out, "RETURN.md", hook)
                record = hook.record
                self.assertEqual(error.errno, 28)
                self.assertNotEqual((record["original"].st_dev, record["original"].st_ino),
                                    (record["replacement"].st_dev, record["replacement"].st_ino))
                now = record["path"].stat()
                self.assertEqual((now.st_ino, now.st_mtime_ns, stat.S_IMODE(now.st_mode)),
                                 (record["replacement"].st_ino, record["replacement"].st_mtime_ns, 0o640))
                self.assertEqual(record["path"].read_bytes(), b"replacement bytes")
                self.assertEqual(record["parked"].stat().st_ino, record["original"].st_ino)
                self.assertTrue(record["parked"].read_bytes())
                self.assertEqual([p.name for p in out.iterdir()], [replaced])
                self.assert_closed(fds)

    def test_rollback_preserves_replacement_link_directory_and_hardlink(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_bytes(b"outside sentinel")

        def link(path):
            os.symlink(sentinel, path)

        def empty_dir(path):
            os.mkdir(path)

        def full_dir(path):
            os.mkdir(path)
            (path / "inner.txt").write_bytes(b"inner")

        def hardlink(path):
            os.link(sentinel, path)

        for label, make in (("symlink", link), ("empty-dir", empty_dir), ("full-dir", full_dir),
                            ("hardlink", hardlink)):
            with self.subTest(replacement=label):
                out = self.tmp / ("packet-" + label)

                def swap(directory, make=make):
                    os.rename(directory / "RULES.md", self.tmp / ("parked-" + directory.name))
                    make(directory / "RULES.md")

                error, fds = self.failing_build(out, "RETURN.md", swap)
                self.assertEqual(error.errno, 28)
                self.assertEqual([p.name for p in out.iterdir()], ["RULES.md"])
                entry = out / "RULES.md"
                self.assertEqual(entry.is_symlink(), label == "symlink")
                self.assertEqual(entry.is_dir() and not entry.is_symlink(), label.endswith("dir"))
                if label == "full-dir":
                    self.assertEqual((entry / "inner.txt").read_bytes(), b"inner")
                self.assertEqual(sentinel.read_bytes(), b"outside sentinel")
                self.assertTrue((self.tmp / ("parked-" + out.name)).is_file())
                self.assert_closed(fds)

    def test_rollback_cleans_other_owned_members_and_preserves_missing_entries(self):
        out = self.tmp / "packet-mixed"

        def mutate(directory):
            os.rename(directory / "PACKET.md", self.tmp / "parked-PACKET.md")
            (directory / "PACKET.md").write_bytes(b"replacement")
            os.unlink(directory / "RULES.md")
            (directory / "unrelated.txt").write_bytes(b"keep")

        error, fds = self.failing_build(out, "RETURN.md", mutate)
        self.assertEqual(error.errno, 28)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["PACKET.md", "unrelated.txt"])
        self.assertEqual((out / "unrelated.txt").read_bytes(), b"keep")
        self.assertTrue((self.tmp / "parked-PACKET.md").is_file())
        self.assert_closed(fds)
        # ordinary failure with no replacement still removes the folder, and the same path can be retried
        ordinary = self.tmp / "packet-ordinary"
        self.failing_build(ordinary, "RETURN.md")
        self.assertFalse(os.path.lexists(ordinary))
        law_packet.build("us-ca", ordinary, AS_OF)
        self.assertEqual({p.name for p in ordinary.iterdir()}, FILES)

    def test_retained_member_descriptors_survive_stream_close_until_cleanup(self):
        real_open, real_unlink = os.open, os.unlink
        members, seen, fds = {}, {}, []
        sentinel_fd = real_open(__file__, os.O_RDONLY)
        self.addCleanup(os.close, sentinel_fd)

        def recording_open(name, *rest, **kwargs):
            if name == "RETURN.md":
                for member, fd in members.items():  # earlier streams are closed; descriptors must still be open
                    now = os.fstat(fd)
                    seen[member] = (now.st_dev, now.st_ino) == (os.stat(self.tmp / "retained" / member).st_dev,
                                                                os.stat(self.tmp / "retained" / member).st_ino)
                raise OSError(28, "Synthetic no space left on device")
            fd = real_open(name, *rest, **kwargs)
            fds.append(fd)
            if name in law_packet.NAMES:
                members[name] = fd
            return fd

        checked_at_unlink = []

        def checking_unlink(name, *rest, **kwargs):
            if name in members:
                checked_at_unlink.append((name, os.fstat(members[name]).st_ino))
            return real_unlink(name, *rest, **kwargs)

        with mock.patch.object(law_packet.os, "open", recording_open), \
                mock.patch.object(law_packet.os, "unlink", checking_unlink):
            with self.assertRaises(OSError):
                law_packet.build("us-ca", self.tmp / "retained", AS_OF)
        self.assertEqual(seen, {"PACKET.md": True, "RULES.md": True, "WORKSHEET.csv": True})
        self.assertEqual([name for name, _ in checked_at_unlink], ["PACKET.md", "RULES.md", "WORKSHEET.csv"])
        self.assert_closed(fds)
        os.fstat(sentinel_fd)  # an unrelated descriptor is untouched
        # success: every descriptor opened is closed exactly once (a second close would hit an unrelated fd)
        closed, real_close = [], os.close

        def counting_close(fd):
            closed.append(fd)
            real_close(fd)

        fds.clear()

        def success_open(name, *rest, **kwargs):
            fd = real_open(name, *rest, **kwargs)
            fds.append(fd)
            return fd

        with mock.patch.object(law_packet.os, "open", success_open), mock.patch.object(law_packet.os, "close",
                                                                                      counting_close):
            law_packet.build("us-ca", self.tmp / "retained-ok", AS_OF)
        self.assertEqual(sorted(closed), sorted(fds))  # one close per open, nothing else closed
        os.fstat(sentinel_fd)

    def test_identity_inspection_and_unlink_failure_do_not_mask_original_error(self):
        real_fstat, real_stat, real_unlink, real_open = os.fstat, os.stat, os.unlink, os.open

        for kind in ("fstat", "stat", "unlink"):
            with self.subTest(kind=kind):
                out = self.tmp / ("packet-" + kind)
                fds, rules_fd = [], []

                def opening(name, *rest, **kwargs):
                    if name == "RETURN.md":
                        raise OSError(28, "Synthetic no space left on device")
                    fd = real_open(name, *rest, **kwargs)
                    fds.append(fd)
                    if name == "RULES.md":
                        rules_fd.append(fd)
                    return fd

                def fstat(fd, *rest, **kwargs):
                    if kind == "fstat" and rules_fd and fd == rules_fd[0]:
                        raise OSError(5, "Synthetic fstat failure")
                    return real_fstat(fd, *rest, **kwargs)

                def stat_(name, *rest, **kwargs):
                    if kind == "stat" and name == "RULES.md":
                        raise OSError(5, "Synthetic stat failure")
                    return real_stat(name, *rest, **kwargs)

                def unlink(name, *rest, **kwargs):
                    if kind == "unlink" and name == "RULES.md":
                        raise OSError(13, "Synthetic unlink failure")
                    return real_unlink(name, *rest, **kwargs)

                with mock.patch.multiple(law_packet.os, open=opening, fstat=fstat, stat=stat_, unlink=unlink):
                    with self.assertRaises(OSError) as caught:
                        law_packet.build("us-ca", out, AS_OF)
                self.assertEqual(caught.exception.errno, 28)  # the original failure, not the cleanup one
                self.assertEqual([p.name for p in out.iterdir()], ["RULES.md"])  # uncertain entry kept, others removed
                self.assert_closed(fds)

    def test_descriptor_closed_when_stream_wrapper_or_write_fails(self):
        real_open, real_fdopen = os.open, os.fdopen

        class FailingStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *exc):
                return self.stream.__exit__(*exc)

            def write(self, text):
                raise OSError(5, "Synthetic write failure")

        for kind in ("wrapper", "write"):
            with self.subTest(kind=kind):
                out = self.tmp / ("packet-" + kind)
                fds, by_name = [], {}

                def tracked_open(name, *rest, **kwargs):
                    fd = real_open(name, *rest, **kwargs)
                    fds.append(fd)
                    by_name[name] = fd
                    return fd

                def fdopen(fd, *rest, **kwargs):
                    if fd == by_name.get("RULES.md"):  # the second member
                        if kind == "wrapper":
                            raise OSError(5, "Synthetic wrapper failure")
                        return FailingStream(real_fdopen(fd, *rest, **kwargs))
                    return real_fdopen(fd, *rest, **kwargs)

                with mock.patch.object(law_packet.os, "open", tracked_open), \
                        mock.patch.object(law_packet.os, "fdopen", fdopen):
                    with self.assertRaises(OSError) as caught:
                        law_packet.build("us-ca", out, AS_OF)
                self.assertEqual(caught.exception.errno, 5)
                self.assertFalse(os.path.lexists(out))
                self.assert_closed(fds)

    def test_cli_replacement_refusal_preserves_data_and_original_exit(self):
        out = self.tmp / "packet-cli"
        hook = self.park_and_replace("PACKET.md")
        real_open = os.open

        def failing_open(name, *rest, **kwargs):
            if name == "RULES.md":
                hook(out)
                raise OSError(28, "Synthetic no space left on device")
            return real_open(name, *rest, **kwargs)

        with mock.patch.object(law_packet.os, "open", failing_open):
            code, stdout, stderr = self.run_main(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: [Errno 28] Synthetic no space left on device\n")
        self.assertEqual((out / "PACKET.md").read_bytes(), b"replacement bytes")
        # a retry on the same path refuses rather than overwriting; a fresh path succeeds
        code, stdout, stderr = self.run_main(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: --out already exists, not overwriting\n")
        self.assertEqual((out / "PACKET.md").read_bytes(), b"replacement bytes")
        code, stdout, stderr = self.run_main(self.tmp / "packet-fresh")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "review_required")

    # --- output directory identity admission (RF0845-2) -----------------------------------------------------

    CHANGED = "--out directory changed before writing; nothing written"

    def seam_build(self, out, at_output_open=None, after_output_open=None, fail_member=None):
        """Run build() with hooks at the real output-directory open; returns (error, opened names, fds).

        Everything else (stat, open, rename, write, rollback) is the real syscall.
        """
        real_open = os.open
        opened, fds = [], []

        def hooked_open(name, *rest, **kwargs):
            if name == out.name and at_output_open:
                at_output_open(out)
            if name == fail_member:
                raise OSError(28, "Synthetic no space left on device")
            fd = real_open(name, *rest, **kwargs)
            fds.append(fd)
            if name == out.name or name in law_packet.NAMES:  # the parent walk is not a packet open
                opened.append(name)
            if name == out.name and after_output_open:
                after_output_open(out, fd)
            return fd

        error = None
        with mock.patch.object(law_packet.os, "open", hooked_open):
            try:
                law_packet.build("us-ca", out, AS_OF)
            except (OSError, ValueError) as exc:
                error = exc
        return error, opened, fds

    def substitute_directory(self, out):
        """Park the directory build just made and put a different real 0700 directory at its name."""
        parked, foreign = Path(str(out) + ".parked"), Path(str(out) + ".foreign")
        foreign.mkdir(mode=0o700)
        self.foreign_inode = foreign.stat().st_ino
        os.rename(out, parked)
        os.rename(foreign, out)
        self.assertNotEqual(parked.stat().st_ino, out.stat().st_ino)

    def assert_untouched(self, out):
        self.assertEqual(os.listdir(out), [])
        self.assertEqual(os.listdir(Path(str(out) + ".parked")), [])
        self.assertEqual(out.stat().st_ino, self.foreign_inode)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)

    def test_real_directory_substitution_before_output_open_refuses(self):
        out = self.tmp / "packet"
        error, opened, fds = self.seam_build(out, at_output_open=self.substitute_directory)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), self.CHANGED)
        self.assertEqual(opened, ["packet"])  # no member was opened
        self.assert_untouched(out)
        self.assert_closed(fds)

    def test_unverified_replacement_never_becomes_rollback_owner(self):
        out = self.tmp / "packet"
        error, opened, fds = self.seam_build(out, at_output_open=self.substitute_directory, fail_member="RULES.md")
        self.assertEqual(str(error), self.CHANGED)  # admission failed before the member trap was reachable
        self.assertEqual(opened, ["packet"])
        self.assertTrue(out.is_dir())
        self.assert_untouched(out)
        self.assert_closed(fds)

    def test_output_identity_mismatch_cli_has_no_success(self):
        out = self.tmp / "packet-cli"
        real_open = os.open

        def hooked_open(name, *rest, **kwargs):
            if name == out.name:
                self.substitute_directory(out)
            return real_open(name, *rest, **kwargs)

        with mock.patch.object(law_packet.os, "open", hooked_open):
            code, stdout, stderr = self.run_main(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: " + self.CHANGED + "\n")
        self.assert_untouched(out)

    def test_candidate_fstat_and_reobservation_failures_close_without_cleanup(self):
        real_fstat, real_stat = os.fstat, os.stat
        for case in ("first stat", "candidate fstat", "second stat", "entry missing", "entry replaced"):
            out = self.tmp / case.replace(" ", "-")
            stats, candidate = [], []

            def counting_stat(path, *rest, **kwargs):
                if path == out.name and kwargs.get("dir_fd") is not None:
                    stats.append(path)
                    if (case, len(stats)) in (("first stat", 1), ("second stat", 2)):
                        raise OSError(5, "Synthetic inspection failure")
                return real_stat(path, *rest, **kwargs)

            def counting_fstat(fd):
                if case == "candidate fstat" and fd in candidate:
                    raise OSError(5, "Synthetic inspection failure")
                return real_fstat(fd)

            def after_open(path, fd):
                candidate.append(fd)
                if case in ("entry missing", "entry replaced"):
                    os.rename(path, Path(str(path) + ".parked"))
                if case == "entry replaced":
                    path.mkdir(mode=0o700)

            with self.subTest(case=case), \
                    mock.patch.object(law_packet.os, "stat", counting_stat), \
                    mock.patch.object(law_packet.os, "fstat", counting_fstat), \
                    mock.patch.object(law_packet.os, "rmdir", side_effect=AssertionError("rmdir")), \
                    mock.patch.object(law_packet.os, "unlink", side_effect=AssertionError("unlink")):
                error, opened, fds = self.seam_build(out, after_output_open=after_open)
            if case == "entry replaced":
                self.assertEqual(str(error), self.CHANGED)
            else:
                self.assertIsInstance(error, OSError)
                self.assertNotIsInstance(error, AssertionError)
            self.assertNotIn("PACKET.md", opened)  # no member written
            self.assert_closed(fds)
            held = Path(str(out) + ".parked") if case in ("entry missing", "entry replaced") else out
            self.assertEqual(os.listdir(held), [])
            if case == "entry replaced":
                self.assertEqual(os.listdir(out), [])

    def test_output_symlink_file_and_missing_entry_refuse_before_members(self):
        target = self.tmp / "target"
        target.mkdir()

        def to_symlink(out):
            os.rename(out, Path(str(out) + ".parked"))
            os.symlink(target, out, target_is_directory=True)

        def to_file(out):
            os.rename(out, Path(str(out) + ".parked"))
            out.write_bytes(b"keep")

        def to_missing(out):
            os.rename(out, Path(str(out) + ".parked"))

        for name, hook in (("symlink", to_symlink), ("file", to_file), ("missing", to_missing)):
            out = self.tmp / name
            with self.subTest(name=name):
                error, opened, fds = self.seam_build(out, at_output_open=hook)
                self.assertIsInstance(error, (OSError, ValueError))
                self.assertEqual(opened, [])
                self.assertEqual(os.listdir(Path(str(out) + ".parked")), [])
                self.assertEqual(os.listdir(target), [])
                self.assertEqual(out.is_symlink(), name == "symlink")
                self.assertEqual(out.is_file(), name == "file")
                self.assertEqual(os.path.lexists(out), name != "missing")
                if name == "file":
                    self.assertEqual(out.read_bytes(), b"keep")

    def test_verified_anchor_remains_authority_after_late_retarget(self):
        real_open = os.open

        def swapping_open(fail):
            def hooked_open(name, *rest, **kwargs):
                if name == fail[0]:
                    self.substitute_directory(fail[1])
                    if fail[2]:
                        raise OSError(28, "Synthetic no space left on device")
                return real_open(name, *rest, **kwargs)
            return hooked_open

        out = self.tmp / "late"
        with mock.patch.object(law_packet.os, "open", swapping_open(("PACKET.md", out, False))):
            law_packet.build("us-ca", out, AS_OF)  # completion goes through the retained descriptor
        self.assertEqual(os.listdir(out), [])  # foreign directory untouched
        self.assertEqual({p.name for p in Path(str(out) + ".parked").iterdir()}, FILES)

        out = self.tmp / "late-fail"
        with mock.patch.object(law_packet.os, "open", swapping_open(("RULES.md", out, True))):
            with self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
        self.assertEqual(caught.exception.errno, 28)
        self.assertEqual(os.listdir(out), [])
        self.assertEqual(out.stat().st_ino, self.foreign_inode)  # foreign entry never removed
        self.assertEqual(os.listdir(Path(str(out) + ".parked")), [])  # owned member removed, folder kept

    def test_descriptor_admission_lifetime_and_ordinary_compatibility(self):
        def open_fds():
            return len(os.listdir("/proc/self/fd"))

        before = open_fds()
        result = law_packet.build("us-ca", self.tmp / "ok", AS_OF)
        self.assertEqual(open_fds(), before)
        self.assertEqual(result["files"], list(law_packet.NAMES))
        self.assertEqual(stat.S_IMODE((self.tmp / "ok").stat().st_mode), 0o700)
        for path in (self.tmp / "ok").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        error, _, _ = self.seam_build(self.tmp / "mismatch", at_output_open=self.substitute_directory)
        self.assertEqual(str(error), self.CHANGED)
        self.assertEqual(open_fds(), before)
        error, _, _ = self.seam_build(self.tmp / "member", fail_member="RULES.md")
        self.assertIsInstance(error, OSError)
        self.assertEqual(error.errno, 28)
        self.assertFalse(os.path.lexists(self.tmp / "member"))
        self.assertEqual(open_fds(), before)
        _, retry = self.packet("member")
        self.assertEqual(retry["out"], str(self.tmp / "member"))


if __name__ == "__main__":
    unittest.main()
