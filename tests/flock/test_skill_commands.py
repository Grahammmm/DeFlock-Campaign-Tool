"""Every command a flock skill quotes names a real tool, and a verb that tool's --help lists (RF0850-3)."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOLS, SKILLS = ROOT / "tools", ROOT / "skills"
COMMAND = re.compile(r"python3(?: -B)? tools/(flock_[a-z_]+\.py)(?: ([a-z][a-z-]*|--check))?")
NOT_IN_SKILLS = {"flock_review_verify.py": "run by flock_review.py's verify stage, not quoted by hand",
                 "flock_privacy.py": "pattern module loaded by the report, page and publish tools; no CLI"}


def collect(skills):
    """(SKILL path, line, tool, verb or None) for every quoted command in skills/flock-*/SKILL.md."""
    return [(path, n, m[1], m[2]) for path in sorted(Path(skills).glob("flock-*/SKILL.md"))
            for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) for m in COMMAND.finditer(text)]


def problems(commands, tools):
    """`<SKILL path>:<line>: <tool> [verb]` for a missing tool, a --help exit other than 0, or a verb --help omits."""
    bad, helps = [], {}
    with tempfile.TemporaryDirectory() as cwd:
        for path, line, tool, verb in commands:
            if tool not in helps and (Path(tools) / tool).is_file():
                done = subprocess.run([sys.executable, "-B", str(Path(tools) / tool), "--help"], cwd=cwd, timeout=30,
                                      capture_output=True, text=True, check=False)
                helps[tool] = done.stdout + done.stderr if done.returncode == 0 else None
            usage = helps.get(tool)
            if usage is None or (verb and not re.search(rf"(?<![\w-]){re.escape(verb)}(?![\w-])", usage)):
                bad.append(f"{path}:{line}: {tool}" + (f" {verb}" if verb else ""))
    return bad


def coverage(commands, tools, exempt):
    """Each flock tool is quoted by a skill or exempt with a reason, never both."""
    quoted = {tool for _, _, tool, _ in commands}
    names = sorted(p.name for p in Path(tools).glob("flock_*.py"))
    return ([f"{t}: not quoted by any flock skill and not in NOT_IN_SKILLS" for t in names if t not in quoted | set(exempt)]
            + [f"{t}: in NOT_IN_SKILLS but quoted by a flock skill" for t in sorted(quoted & set(exempt))])


class SkillCommandsTest(unittest.TestCase):
    def skill(self, *lines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "skills" / "flock-x" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_real_skills_match_tools(self):
        commands = collect(SKILLS)
        self.assertGreaterEqual(len(commands), 30)
        self.assertEqual(problems(commands, TOOLS), [])
        self.assertEqual(coverage(commands, TOOLS, NOT_IN_SKILLS), [])

    def test_missing_tool_and_unknown_verb_report_skill_line(self):
        p = self.skill("`python3 tools/flock_nope.py status`", "prose", "```", "python3 tools/flock_sweep.py frobnicate", "```")
        self.assertEqual(problems(collect(p.parents[1]), TOOLS), [f"{p}:1: flock_nope.py status", f"{p}:4: flock_sweep.py frobnicate"])

    def test_check_bare_and_dash_b_forms(self):
        p = self.skill("python3 tools/flock_facts.py --check X", "`python3 tools/flock_review_golden.py`",
                       "python3 -B tools/flock_requests.py --check P")
        commands = collect(p.parents[1])
        self.assertEqual([c[1:] for c in commands], [(1, "flock_facts.py", "--check"), (2, "flock_review_golden.py", None),
                                                     (3, "flock_requests.py", "--check")])
        self.assertEqual(problems(commands, TOOLS), [])
        tools = self.skill().parents[2]  # a bare command checks only that the tool exists and --help exits 0
        (tools / "flock_ok.py").write_text("print('usage: none')\n", encoding="utf-8")
        (tools / "flock_fail.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
        p = self.skill("python3 tools/flock_ok.py", "python3 tools/flock_fail.py")
        self.assertEqual(problems(collect(p.parents[1]), tools), [f"{p}:2: flock_fail.py"])

    def test_coverage_unquoted_and_dishonest_exemption(self):
        tools = self.skill().parents[2]
        for name in ("flock_a.py", "flock_b.py", "flock_c.py"):
            (tools / name).touch()
        self.assertEqual(coverage([("S", 1, "flock_a.py", None), ("S", 2, "flock_c.py", "run")], tools, {"flock_c.py": "why"}),
                         ["flock_b.py: not quoted by any flock skill and not in NOT_IN_SKILLS",
                          "flock_c.py: in NOT_IN_SKILLS but quoted by a flock skill"])

    def test_help_exits_zero_and_bad_arguments_unchanged(self):
        for tool, bad in (("flock_law_policy.py", ["versions"]), ("flock_requests.py", ["bogus", "x"])):
            for args in (["--help"], ["-h"], bad):
                done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
                out, err = (done.stdout, done.stderr) if args != bad else (done.stderr, done.stdout)
                self.assertEqual((done.returncode, err, out.count("\n")), (2 if args == bad else 0, "", 1), (tool, args))
                self.assertTrue(out.startswith(f"usage: {tool} --check PATH"), (tool, args, out))
