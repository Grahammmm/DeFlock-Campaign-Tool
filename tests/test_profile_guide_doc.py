"""DF1107-15: docs/PROFILE-GUIDE.md. Local links and anchors, the guide's own fenced commands run in document order against
a synthetic campaign in temporary storage with outbound connections blocked, and its JSON fragments merged into one
synthetic agency profile and checked by the real ``content-check``. Synthetic data only; nothing is fetched or sent.

Run from the repository root: python3 -B -m unittest tests.test_profile_guide_doc -v

Conventions the guide keeps and these tests read: ```sh blocks are commands run in order ($CAMPAIGN and $CSV are filled in
here); the one ```json block with an "agency_id" is appended to content/agencies.json; every other ```json block is an
object whose keys are merged into that agency's profile, in document order; the ```text block with a ``name,state,kind``
header is the sharing CSV.
"""
import contextlib
import copy
import io
import json
import re
import shlex
import shutil
import smtplib
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402

GUIDE, README = REPO_ROOT / "docs" / "PROFILE-GUIDE.md", REPO_ROOT / "README.md"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_NETWORK = AssertionError("no network in these tests")
FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.S | re.M)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
AGENCY = "ca-example-profile-pd"


def read(path):
    return Path(path).read_text(encoding="utf-8")


def slug(heading):
    heading = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading))


def anchors(text):
    seen, found = {}, set()
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            base = slug(match.group(1))
            found.add(base if base not in seen else f"{base}-{seen[base]}")
            seen[base] = seen.get(base, 0) + 1
    return found


def check_links(source, text):
    """Problems for every local link in ``text`` (as if it were the file ``source``)."""
    problems = []
    blanked = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    for number, line in enumerate(blanked.splitlines(), 1):
        for target in LINK.findall(line):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue
            name, _, fragment = target.partition("#")
            path = (Path(source).parent / name).resolve() if name else Path(source).resolve()
            where = f"{Path(source).name}:{number}: {target}"
            if not path.exists():
                problems.append(f"{where}: no such file")
            elif fragment and (path.suffix != ".md" or fragment not in anchors(read(path))):
                problems.append(f"{where}: no such heading")
    return problems


def fences(text):
    return [(m.group(1), m.group(2)) for m in FENCE.finditer(text)]


def sections(text):
    parts = re.split(r"^## (.*)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def run(*arguments):
    """Call the real ``cli.main``; (exit code, stdout, stderr)."""
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
        try:
            result = cli.main()
            code = result if isinstance(result, int) else 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class GuideTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "getaddrinfo", side_effect=NO_NETWORK),
                   mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK),
                   mock.patch.object(smtplib.SMTP, "__init__", side_effect=NO_NETWORK)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-guide-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.agencies = self.root / "content" / "agencies.json"
        self.guide = read(GUIDE)

    # --- helpers ---------------------------------------------------------------------------------------------------
    def profile_from(self, text):
        """The synthetic agency with every profile fragment of ``text`` merged in document order."""
        agency, merged = None, {}
        for kind, body in fences(text):
            if kind != "json":
                continue
            value = json.loads(body)
            if "agency_id" in value:
                agency = value
            else:
                merged.update(value)
        self.assertIsNotNone(agency, "the guide has no agency fixture")
        agency = copy.deepcopy(agency)
        agency["profile"].update(merged)
        return agency

    def install(self, agency):
        listing = json.loads(read(self.agencies))
        listing.append(agency)
        self.agencies.write_text(json.dumps(listing), encoding="utf-8")

    def walk(self, text):
        """Run the guide's sh blocks in order, merging the fixture before the first command that needs it; the list of
        (command, exit code, stderr) for every command."""
        csv = next((body for kind, body in fences(text) if kind == "text" and body.startswith("name,state,kind")), "")
        csv_path = self.base / "sharing.csv"
        csv_path.write_text(csv, encoding="utf-8")
        mapping = {"CAMPAIGN": str(self.root), "CSV": str(csv_path)}
        results, installed = [], False
        for kind, body in fences(text):
            if kind != "sh":
                continue
            for line in re.sub(r"\\\n\s*", " ", body).splitlines():
                if not line.strip():
                    continue
                argv = shlex.split(line)
                if argv[:3] != ["python3", "-m", "campaign_tool"]:
                    raise AssertionError(f"not a campaign_tool command: {line}")
                if not installed and argv[3] != "content-check":
                    self.install(self.profile_from(text))
                    installed = True
                arguments = [re.sub(r"\$([A-Z]+)", lambda m: mapping[m.group(1)], token) for token in argv[3:]]
                code, _, err = run(*arguments)
                results.append((" ".join(argv[3:]), code, err))
        return results

    def check(self, *arguments):
        return run("content-check", "--directory", self.root, *arguments)

    # --- tests -----------------------------------------------------------------------------------------------------
    def test_task_order_topics_and_reference_links(self):
        text = self.guide
        self.assertEqual(check_links(GUIDE, text), [])
        self.assertEqual(check_links(README, read(README)), [])
        self.assertIn("(docs/PROFILE-GUIDE.md)", read(README))
        self.assertIn("heading-skip", text)  # the known build --check limit is stated, not hidden
        headings = re.findall(r"^## (\d+)\. (.*)$", text, re.M)
        self.assertEqual([int(n) for n, _ in headings], list(range(1, 13)))
        keywords = ["identify", "policy", "posted policy", "contract term", "contract against", "paid for", "transparency",
                    "receives data", "reports", "outside a meeting", "archived copy", "check, build"]
        for (_, title), keyword in zip(headings, keywords):
            self.assertIn(keyword, title.lower())
        parts = sections(text)
        required = {2: ["alpr_policy"], 3: ["policy_check"], 4: ["profile.contract", "amount_usd"],
                    5: ["contract_check"], 6: ["profile.funding"], 7: ["profile.transparency", "flock_transparency_slug"],
                    8: ["agency-sharing", "profile.sharing"], 9: ["profile.reports"], 10: ["agency-action add"],
                    11: ["archived_url", "archived_on"], 12: ["--stale-days", "build"]}
        for number, title in headings:
            body = parts[f"{number}. {title}"]
            self.assertIn("SITE-CONTENT.md#", body, f"task {number} links its SITE-CONTENT section")
            for needle in required.get(int(number), []):
                self.assertIn(needle, body, f"task {number}")
        for phrase in ("fetches no source", "never fetches", "legal conclusion", "official source", "`checked_on` means",
                       "nothing here is legal advice", "does **not** read a", "absent"):
            self.assertIn(phrase.lower(), text.lower(), phrase)

    def test_every_documented_command_and_the_fixture_pass_offline(self):
        results = self.walk(self.guide)
        self.assertGreaterEqual(len(results), 8)
        for command, code, err in results:
            if command.startswith("build ") and command.endswith("--check") and code != 0:
                # the one limit the guide states: the agency page with a recorded action skips a heading level
                self.assertIn("heading-skip", err, f"{command}: {err}")
                continue
            self.assertEqual(code, 0, f"{command}: {err}")
        names = [command.split()[0] for command, _, _ in results]
        for needed in ("content-check", "doctor", "build", "content"):
            self.assertIn(needed, names)
        stored = next(a for a in json.loads(read(self.agencies)) if a["agency_id"] == AGENCY)
        self.assertEqual(len(stored["profile"]["sharing"]["recipients"]), 2)
        for field in ("alpr_policy", "policy_check", "contract", "contract_check", "funding", "transparency", "reports"):
            self.assertIn(field, stored["profile"])
        self.assertEqual(len(json.loads(read(self.root / "content" / "agency-actions.json"))["actions"]), 1)

    def test_unknown_flag_and_broken_anchor_fail(self):
        results = self.walk(self.guide.replace("--stale-days 90", "--stale-day 90", 1))
        self.assertTrue(any(code != 0 for _, code, _ in results))
        broken = self.guide.replace("SITE-CONTENT.md#agencies-cards-and-the-picker", "SITE-CONTENT.md#no-such-heading", 1)
        self.assertEqual(len(check_links(GUIDE, broken)), 1)
        self.assertTrue(check_links(GUIDE, broken)[0].endswith("no such heading"))

    def test_bad_values_are_refused_by_the_real_field_and_corrected_fixture_accepted(self):
        good = self.profile_from(self.guide)
        cases = [("profile.contract.cost.amount_usd", lambda p: p["contract"]["cost"].update(amount_usd=12345.6)),
                 ("profile.contract.checked_on", lambda p: p["contract"].update(checked_on="2999-01-01")),
                 ("profile.contract_check.contract_url",
                  lambda p: p["contract_check"].update(contract_url="https://example.invalid/other.pdf")),
                 ("profile.policy_check.policy_url",
                  lambda p: p["policy_check"].update(policy_url="https://example.invalid/other.pdf")),
                 ("profile.contract: needs source_url", lambda p: p["contract"].pop("source_url")),
                 ("profile.alpr_policy.archived_on", lambda p: p["alpr_policy"].update(archived_on="2026-10-21"))]
        original = read(self.agencies)
        for field, change in cases:
            with self.subTest(field=field):
                self.agencies.write_text(original, encoding="utf-8")
                agency = copy.deepcopy(good)
                change(agency["profile"])
                self.install(agency)
                code, out, err = self.check()
                self.assertNotEqual(code, 0)
                self.assertIn(field, out + err)
        self.agencies.write_text(original, encoding="utf-8")
        self.install(good)
        self.assertEqual(self.check()[0], 0)

    def test_profile_dates_are_not_named_by_doctor_and_missing_is_not_zero(self):
        agency = self.profile_from(self.guide)
        agency["profile"]["alpr_policy"]["checked_on"] = "2026-01-02"
        agency["profile"]["policy_check"]["checked_on"] = "2026-01-03"
        for key in ("archived_url", "archived_on"):
            agency["profile"]["alpr_policy"].pop(key)
        self.install(agency)
        code, out, err = run("doctor", "--directory", self.root, "--stale-days", 1)
        self.assertNotIn(AGENCY, out + err)  # the baseline's freshness enumerator excludes profile dates, as the guide says
        self.assertIn("does **not** read a", self.guide)
        # a transparency number the portal did not show stays out of the record instead of becoming 0
        snapshot = self.profile_from(self.guide)["profile"]["transparency"][0]
        self.assertNotIn("hotlist_alerts", snapshot)

    def test_sharing_csv_failures_write_nothing(self):
        self.install(self.profile_from(self.guide))
        before = read(self.agencies)
        bad = self.base / "bad.csv"
        url = "https://transparency.flocksafety.com/example-profile-pd"
        bad.write_text("name,state\nOnly Two Columns,CA\n", encoding="utf-8")
        code, _, _ = run("content", "agency-sharing", "--directory", self.root, "--agency", AGENCY, "--csv", bad,
                         "--checked-on", "2026-10-27", "--source-url", url)
        self.assertNotEqual(code, 0)
        self.assertEqual(read(self.agencies), before)
        good = self.base / "good.csv"
        good.write_text("name,state,kind\nNeighbour PD (fictional),CA,local\n", encoding="utf-8")
        args = ["content", "agency-sharing", "--directory", self.root, "--agency", AGENCY, "--csv", good,
                "--checked-on", "2026-10-27", "--source-url", url]
        self.assertEqual(run(*args)[0], 0)
        self.assertNotEqual(run(*args)[0], 0)  # a second list needs --replace
        self.assertEqual(run(*args, "--replace")[0], 0)


if __name__ == "__main__":
    unittest.main()
