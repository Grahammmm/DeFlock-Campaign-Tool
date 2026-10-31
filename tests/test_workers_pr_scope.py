"""workers.yml PR_SCOPE must cover the in-repo import closure of the .py files it names,
so a change to a module a scoped file imports cannot skip the required `workers` check green."""
import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

import campaign_tool.outbox  # noqa: F401  (ci_affected selects this test when these change)
import campaign_tool.review  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("campaign_tool", "runner", "workers", "tools", "tests")
NOT_FOUND = "scope block not found"


class ScopeError(Exception):
    pass


def parse_scope(text):
    m = re.search(r"^( *)PR_SCOPE: \|\n((?:\1 +\S.*\n|[ \t]*\n)*)", text, re.M)
    if not m or not m.group(2).split():
        raise ScopeError(NOT_FOUND)
    return m.group(2).split()


def glob_match(path, pattern):
    """Git `:(glob)` semantics: `**` crosses directories, `*` and `?` do not."""
    tokens = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    rx = "".join(tokens.get(t, re.escape(t)) for t in re.split(r"(\*\*/|\*\*|\*|\?)", pattern))
    return re.fullmatch(rx, path) is not None


def _module_files(root, parts):
    """Repo files loaded by importing dotted `parts` (package __init__s included)."""
    if not parts or parts[0] not in PACKAGES or not (root / parts[0]).is_dir():
        return []
    cands = [root.joinpath(*parts[:n]) for n in range(1, len(parts) + 1)]
    return [c.relative_to(root).as_posix() for b in cands
            for c in (b / "__init__.py", b.with_suffix(".py")) if c.is_file()]


def _imports(root, rel):
    package = rel.split("/")[:-1]
    for node in ast.walk(ast.parse((root / rel).read_text(encoding="utf-8"), rel)):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (package[:len(package) - node.level + 1] if node.level else []) + (
                node.module.split(".") if node.module else [])
            yield base
            yield from (base + [alias.name] for alias in node.names)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    raise ScopeError(f"non-literal {name} in {rel}:{node.lineno}; scope cannot be proven")
                dots = len(arg.value) - len(arg.value.lstrip("."))
                rest = [p for p in arg.value.lstrip(".").split(".") if p]
                yield (package[:len(package) - dots + 1] if dots else []) + rest


def scope_gaps(root, scope):
    """Return ({uncovered file: scope .py file importing it}, union of closures)."""
    root, gaps, union = Path(root), {}, set()
    for entry in (e for e in scope if e.endswith(".py") and not re.search(r"[*?\[]", e)):
        seen, todo = set(), [entry] + _module_files(root, entry.split("/")[:-1])
        while todo:
            rel = todo.pop()
            if rel not in seen:
                seen.add(rel)
                todo += [f for parts in _imports(root, rel) for f in _module_files(root, parts)]
        union |= seen
        for rel in sorted(seen):
            if not any(glob_match(rel, p) for p in scope):
                gaps.setdefault(rel, entry)
    return gaps, union


BASE_SCOPE = ["campaign_tool/__init__.py", "campaign_tool/review.py"]


class WorkersPrScopeTest(unittest.TestCase):
    def gaps(self, files, scope=BASE_SCOPE):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, body in {"campaign_tool/__init__.py": "", **files}.items():
            Path(tmp.name, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(tmp.name, rel).write_text(body, encoding="utf-8")
        return scope_gaps(tmp.name, scope)[0]

    def test_hidden_absolute_or_relative_import_is_reported(self):
        for src in ("import json\nfrom campaign_tool import helpers\n", "from . import helpers\n"):
            gaps = self.gaps({"campaign_tool/review.py": src, "campaign_tool/helpers.py": ""})
            self.assertEqual(gaps, {"campaign_tool/helpers.py": "campaign_tool/review.py"}, src)

    def test_transitive_import_is_reported(self):
        gaps = self.gaps({"campaign_tool/review.py": "from .a import x\n",
                          "campaign_tool/a.py": "import campaign_tool.b\n", "campaign_tool/b.py": ""},
                         BASE_SCOPE + ["campaign_tool/a.py"])
        self.assertEqual(gaps, {"campaign_tool/b.py": "campaign_tool/review.py"})

    def test_import_module_literal_resolves_and_variable_fails_closed(self):
        gaps = self.gaps({"campaign_tool/review.py": "import importlib\nimportlib.import_module('campaign_tool.dyn')\n",
                          "campaign_tool/dyn.py": ""})
        self.assertEqual(list(gaps), ["campaign_tool/dyn.py"])
        with self.assertRaisesRegex(ScopeError, "non-literal import_module in campaign_tool/review.py"):
            self.gaps({"campaign_tool/review.py": "from importlib import import_module\nimport_module(name)\n"})

    def test_glob_coverage(self):
        self.assertTrue(glob_match("workers/x/y.py", "workers/**"))
        self.assertTrue(glob_match("package-lock.json", "package*.json"))
        self.assertFalse(glob_match("campaign_tool/sub/x.py", "campaign_tool/*.py"))
        self.assertFalse(glob_match("campaign_tool/records/x.py", "campaign_tool/records/__init__.py"))
        self.assertEqual(list(self.gaps({"campaign_tool/review.py": "from .sub import x\n",
                                         "campaign_tool/sub/__init__.py": "", "campaign_tool/sub/x.py": ""},
                                        BASE_SCOPE + ["campaign_tool/*.py"])),
                         ["campaign_tool/sub/__init__.py", "campaign_tool/sub/x.py"])

    def test_missing_or_empty_scope_block_fails(self):
        for text in ("env:\n  OTHER: 1\n", "env:\n  PR_SCOPE: |\njobs:\n  x: 1\n"):
            with self.assertRaisesRegex(ScopeError, NOT_FOUND):
                parse_scope(text)
        self.assertEqual(parse_scope("env:\n  PR_SCOPE: |\n    a/**\n\n    b.py\njobs:\n"), ["a/**", "b.py"])

    def test_real_workers_scope_covers_its_import_closure(self):
        scope = parse_scope((ROOT / ".github/workflows/workers.yml").read_text(encoding="utf-8"))
        gaps, union = scope_gaps(ROOT, scope)
        if "-v" in sys.argv or "--verbose" in sys.argv:
            print("\nworkers PR_SCOPE closure:\n  " + "\n  ".join(sorted(union)), file=sys.stderr)
        self.assertLessEqual({"campaign_tool/review.py", "campaign_tool/outbox.py"}, union)
        self.assertEqual(gaps, {}, "files imported by a workers PR_SCOPE file but outside the scope:\n"
                         + "\n".join(f"  {f} (imported via {src})" for f, src in sorted(gaps.items())))
        records = {"campaign_tool/records/" + n  # CIS-DF-8b: each of these four entries is needed
                   for n in ("__init__.py", "export_clearance.py", "public_scan.py", "release_manifest.py")}
        self.assertEqual(set(scope_gaps(ROOT, [p for p in scope if p not in records])[0]), records)


if __name__ == "__main__":
    unittest.main()
