"""DF1107-15: docs/PROFILE-GUIDE.md. Local links and anchors, the guide's own fenced commands run in document order against
a synthetic campaign in temporary storage with outbound connections blocked, and its JSON fragments merged into one
synthetic agency profile and checked by the real ``content-check``. Synthetic data only; nothing is fetched or sent.

Run from the repository root: python3 -B -m unittest tests.test_profile_guide_doc -v

Conventions the guide keeps and these tests read: the guide is replayed in document order; ```sh blocks are commands run
where they stand ($CAMPAIGN and $CSV are filled in here); the one ```json block with an "agency_id" is appended to
content/agencies.json where it stands; every other ```json block is an object whose keys are merged into that agency's
profile where it stands; the ```text block with a ``name,state,kind`` header is the sharing CSV, and the ```text block of
task 5 is the replacement sequence, whose ``(next unit)`` lines are not commands yet.
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
    def replay(self, text, root, writers_only=False):
        """Replay the guide in document order against the campaign ``root``: the ```json block with an "agency_id" is
        appended where it stands, every other ```json block is merged into that agency's profile where it stands, and
        each ```sh line runs when reached (with ``writers_only``, only the document writers, each of which must
        succeed). The list of (command, exit code, stderr) for every command run."""
        agencies = root / "content" / "agencies.json"
        csv = next((body for kind, body in fences(text) if kind == "text" and body.startswith("name,state,kind")), "")
        csv_path = root.parent / f"{root.name}-sharing.csv"
        csv_path.write_text(csv, encoding="utf-8")
        mapping = {"CAMPAIGN": str(root), "CSV": str(csv_path)}
        results = []
        for kind, body in fences(text):
            if kind == "json":
                value, listing = json.loads(body), json.loads(read(agencies))
                if "agency_id" in value:
                    listing.append(value)
                else:
                    entry = next((a for a in listing if a.get("agency_id") == AGENCY), None)
                    self.assertIsNotNone(entry, "a profile fragment precedes the agency fixture")
                    entry["profile"].update(value)
                agencies.write_text(json.dumps(listing), encoding="utf-8")
            if kind != "sh":
                continue
            for line in re.sub(r"\\\n\s*", " ", body).splitlines():
                if not line.strip():
                    continue
                argv = shlex.split(line)
                if argv[:3] != ["python3", "-m", "campaign_tool"]:
                    raise AssertionError(f"not a campaign_tool command: {line}")
                writer = argv[3:5] == ["content", "agency-profile"] and argv[5] in ("policy", "contract")
                if writers_only and not writer:
                    continue
                arguments = [re.sub(r"\$([A-Z]+)", lambda m: mapping[m.group(1)], token) for token in argv[3:]]
                code, _, err = run(*arguments)
                if writers_only:
                    self.assertEqual(code, 0, f"{line}: {err}")
                results.append((" ".join(argv[3:]), code, err))
        return results

    def profile_from(self, text):
        """The synthetic agency exactly as the guide's own ordered replay leaves it (its JSON blocks and its document
        commands), built in a separate copy of the example campaign."""
        self.isolated = getattr(self, "isolated", 0) + 1
        root = self.base / f"profile-{self.isolated}"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        self.replay(text, root, writers_only=True)
        agency = next((a for a in json.loads(read(root / "content" / "agencies.json")) if a.get("agency_id") == AGENCY),
                      None)
        self.assertIsNotNone(agency, "the guide has no agency fixture")
        return agency

    def install(self, agency):
        listing = json.loads(read(self.agencies))
        listing.append(agency)
        self.agencies.write_text(json.dumps(listing), encoding="utf-8")

    def walk(self, text):
        """Run the guide in document order against the practice campaign; see ``replay``."""
        return self.replay(text, self.root)

    def check(self, *arguments):
        return run("content-check", "--directory", self.root, *arguments)

    # --- tests -----------------------------------------------------------------------------------------------------
    def test_task_order_topics_and_reference_links(self):
        text = self.guide
        self.assertEqual(check_links(GUIDE, text), [])
        self.assertEqual(check_links(README, read(README)), [])
        self.assertIn("(docs/PROFILE-GUIDE.md)", read(README))
        self.assertIn("heading-skip", text)  # the known build --check limit is stated, not hidden
        self.assertIn("posted-policy\nreading from task 3 is shown under an `h4` heading directly after the agency name", text)
        for stale in ("grep -n checked_on", "not profile dates", "profile dates are not reminded"):
            self.assertNotIn(stale, text)  # recheck is a command now, and doctor names profile dates
        headings = re.findall(r"^## (\d+)\. (.*)$", text, re.M)
        self.assertEqual([int(n) for n, _ in headings], list(range(1, 13)))
        keywords = ["identify", "policy", "posted policy", "contract term", "contract against", "paid for", "transparency",
                    "receives data", "reports", "outside a meeting", "archived copy", "check, build"]
        for (_, title), keyword in zip(headings, keywords):
            self.assertIn(keyword, title.lower())
        parts = sections(text)
        required = {2: ["alpr_policy", "content agency-profile policy"],
                    3: ["policy_check"], 4: ["profile.contract", "amount_usd", "content agency-profile contract"],
                    5: ["contract_check", "--replace --drop-check", "never moved to a new one"], 6: ["profile.funding"], 7: ["profile.transparency", "flock_transparency_slug"],
                    8: ["agency-sharing", "profile.sharing"], 9: ["profile.reports"], 10: ["agency-action add", "agency-action list"],
                    11: ["archived_url", "archived_on"],
                    12: ["--stale-days", "build", "agency-profile list", "agency-profile recheck", "`policy_check` before"]}
        for number, title in headings:
            body = parts[f"{number}. {title}"]
            self.assertIn("SITE-CONTENT.md#", body, f"task {number} links its SITE-CONTENT section")
            for needle in required.get(int(number), []):
                self.assertIn(needle, body, f"task {number}")
        for phrase in ("fetches no source", "never fetches", "legal conclusion", "official source", "`checked_on` means",
                       "nothing here is legal advice", "each dated object in the public", "absent"):
            self.assertIn(phrase.lower(), text.lower(), phrase)

    def test_every_documented_command_and_the_fixture_pass_offline(self):
        results = self.walk(self.guide)
        self.assertGreaterEqual(len(results), 8)
        for command, code, err in results:
            if command.startswith("build ") and command.endswith("--check") and code != 0:
                # the one limit the guide states: on this agency's page the policy reading (h4) follows the name (h2)
                self.assertIn("public/agencies/example-profile.html: heading-skip h2→h4", err, f"{command}: {err}")
                self.assertIn("found 1 accessibility issue(s)", err, f"{command}: {err}")
                continue
            self.assertEqual(code, 0, f"{command}: {err}")
        names = [command.split()[0] for command, _, _ in results]
        for needed in ("content-check", "doctor", "build", "content"):
            self.assertIn(needed, names)
        for needed in ("content agency-profile list", "content agency-profile recheck"):
            self.assertTrue(any(command.startswith(needed) for command, _, _ in results), needed)
        stored = next(a for a in json.loads(read(self.agencies)) if a["agency_id"] == AGENCY)
        # the practice rechecks repeat the recorded dates, so nothing moved
        self.assertEqual((stored["profile"]["alpr_policy"]["checked_on"], stored["profile"]["policy_check"]["checked_on"]),
                         ("2026-10-21", "2026-10-22"))
        self.assertEqual(len(stored["profile"]["sharing"]["recipients"]), 2)
        for field in ("alpr_policy", "policy_check", "contract", "contract_check", "funding", "transparency", "reports"):
            self.assertIn(field, stored["profile"])
        self.assertEqual(len(json.loads(read(self.root / "content" / "agency-actions.json"))["actions"]), 1)

    def test_documented_list_precedes_optional_remove(self):
        """DF1109B-05: task 10 lists before its optional remove, and the listed index is the one remove takes."""
        body = sections(self.guide)["10. Record agency actions outside a meeting"]
        listing = next(line for kind, block in fences(body) if kind == "sh" for line in block.splitlines()
                       if "agency-action list" in line)
        self.assertLess(body.index("agency-action list"), body.index("agency-action remove"))
        self.assertNotIn("agency-action remove", "".join(block for _, block in fences(body)))  # never rehearsed
        self.walk(self.guide)
        argv = [token.replace("$CAMPAIGN", str(self.root)) for token in shlex.split(listing)[3:]]
        self.assertEqual(run(*argv), (0, f"{AGENCY} | 1 | 2026-09-01 | Policy posted | Use policy posted on the department "
                                         "website | https://example.invalid/example-profile/policies | checked 2026-10-29\n", ""))
        self.assertIn(f"--agency {AGENCY} --index 1", body)
        self.assertEqual(len(json.loads(read(self.root / "content" / "agency-actions.json"))["actions"]), 1)

    def test_documented_replacement_sequence_drops_each_reading_and_never_rebinds_it(self):
        """DF1109B-17: task 5's replacement sequence, run after the practice walk; the old readings go, the loader and
        content-check accept the file after every command, and no hand edit is needed between them."""
        body = sections(self.guide)["5. Read the contract against the checklist"]
        block = next(block for kind, block in fences(body) if kind == "text")
        lines = block.splitlines()
        self.assertEqual([line.startswith("(next unit)") for line in lines], [False, True, False, True])
        self.walk(self.guide)
        outputs = []
        for line in lines[0::2]:
            argv = [token.replace("$CAMPAIGN", str(self.root)) for token in shlex.split(line)[3:]]
            code, out, err = run(*argv)
            self.assertEqual((code, err), (0, ""), line)
            outputs.append(out)
            self.assertEqual(self.check()[0], 0, line)
        self.assertIn("Dropped profile.policy_check (checked_on 2026-10-22); record a new reading with content "
                      "agency-profile policy-check.", outputs[0])
        self.assertIn("Removed profile.alpr_policy archived_url and archived_on", outputs[0])  # task 11's copy
        self.assertIn("Dropped profile.contract_check (checked_on 2026-10-24); record a new reading with content "
                      "agency-profile contract-check.", outputs[1])
        stored = next(a for a in json.loads(read(self.agencies)) if a["agency_id"] == AGENCY)["profile"]
        self.assertEqual(stored["alpr_policy"]["url"], "https://example.invalid/example-profile/alpr-policy-2027.pdf")
        self.assertEqual(stored["contract"]["source_url"], "https://example.invalid/example-profile/agreement-2028.pdf")
        self.assertNotIn("cost", stored["contract"])  # a full replacement, not a patch
        self.assertFalse({"policy_check", "contract_check"} & set(stored))

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

    def test_profile_dates_are_named_by_doctor_and_listed_and_missing_is_not_zero(self):
        agency = self.profile_from(self.guide)
        agency["profile"]["alpr_policy"]["checked_on"] = "2026-01-02"
        agency["profile"]["policy_check"]["checked_on"] = "2026-01-03"
        for key in ("archived_url", "archived_on"):
            agency["profile"]["alpr_policy"].pop(key)
        self.install(agency)
        code, out, err = run("doctor", "--directory", self.root, "--stale-days", 1)
        # doctor now names each dated profile object as content/agencies.json:<agency_id>.<field>, as the guide says
        self.assertIn(f"content/agencies.json:{AGENCY}.alpr_policy", out + err)
        self.assertIn(f"content/agencies.json:{AGENCY}.policy_check", out + err)
        self.assertIn("each dated object in the public", self.guide)
        # the same dates, oldest first, from the list command; the document cannot outrun its reading
        code, out, _ = run("content", "agency-profile", "list", "--directory", self.root, "--agency", AGENCY, "--json")
        self.assertEqual(code, 0)
        self.assertEqual([(r["field"], r["checked_on"]) for r in json.loads(out)][:2],
                         [("alpr_policy", "2026-01-02"), ("policy_check", "2026-01-03")])
        recheck = ["content", "agency-profile", "recheck", "--directory", self.root, "--agency", AGENCY, "--checked-on",
                   "2026-01-04", "--field"]
        code, _, err = run(*recheck, "alpr_policy")
        self.assertEqual(code, 1)
        self.assertIn("Recheck policy_check first with --checked-on 2026-01-04 (or later), then retry alpr_policy.", err)
        self.assertEqual([run(*recheck, field)[0] for field in ("policy_check", "alpr_policy")], [0, 0])
        self.assertEqual(self.check()[0], 0)
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
