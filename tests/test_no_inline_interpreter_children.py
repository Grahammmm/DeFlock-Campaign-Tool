"""Tests never pass inline source to a child interpreter.

Unattended workers run under a command guard that refuses ``python -c``,
``sh -c`` and similar inline-source launches. Children therefore run named
scripts under ``tests/child_fixtures/``. This contract is AST-only: it parses
every ``.py`` file under ``tests/`` and the inert detector cases, and never
executes, imports or compiles to bytecode any scanned code.
"""
import ast
from pathlib import Path
import re
import unittest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
CASES = TESTS / "child_fixtures" / "detector_cases"

PYTHON_NAME = re.compile(r"(python|pypy)[0-9.]*")
SHELL_NAMES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
PYTHON_INLINE = re.compile(r"-[bBdEhiIOPqRsSuvVx]*c")
PYTHON_MODULE = re.compile(r"-[bBdEhiIOPqRsSuvVx]*m")
SHELL_INLINE = re.compile(r"-[a-z]*c[a-z]*")
SHELL_CALLS = frozenset({"os.system", "os.popen", "subprocess.getoutput",
                         "subprocess.getstatusoutput", "asyncio.create_subprocess_shell"})
NESTED_HINTS = ("sys.executable", "subprocess", "os.system", "os.popen", "os.exec", "os.spawn",
                "multiprocessing", "pty", "python", "/bin/", "shell=")
NESTED_DEPTH = 2

# Findings that were reviewed and stay: (repo-relative path, qualified name, kind).
REVIEWED_EXCEPTIONS = frozenset({
    # Module import only; every child is a fork-context Process, never spawn.
    ("tests/records/test_canonical_mail_resolution.py", "<module>", "MULTIPROCESSING"),
    # Module import only; every child is a fork-context Process, never spawn.
    ("tests/records/test_mail_adoption_resolution.py", "<module>", "MULTIPROCESSING"),
    # Rejected stage-config data for the validator; never executed.
    ("tests/test_records_runtime.py", "StageContractTests.test_target_config_rejections", "STDIN"),
    # Rejected stage-config data for the validator; never executed.
    ("tests/test_records_runtime.py", "StageContractTests.test_target_config_rejections", "SHELL"),
})

# Files that still launch inline-source children; empty once all are converted.
KNOWN_REMAINING = frozenset({
    "tests/test_records_run_stages.py",
})


def dotted(node):
    """``a.b.c`` for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Bindings(ast.NodeVisitor):
    """Names bound in one scope: imports, assignments and (unknown) parameters."""

    def __init__(self, scope):
        self.imports, self.values = {}, {}
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = scope.args
            for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                             arguments.vararg, arguments.kwarg]:
                if argument is not None:
                    self.values[argument.arg] = None
        for node in (scope.body if isinstance(scope.body, list) else [scope.body]):
            self.visit(node)

    def visit_FunctionDef(self, node):
        self.values.setdefault(node.name, None)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_Import(self, node):
        for alias in node.names:
            self.imports[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if node.module and not node.level:
                self.imports[alias.asname or alias.name] = node.module + "." + alias.name

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.values.setdefault(target.id, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.values.setdefault(node.target.id, node.value)
        self.generic_visit(node)


class Detector:
    """Collect (qualified name, kind) findings from one parsed module."""

    def __init__(self, tree, depth=0):
        self.tree, self.depth, self.findings = tree, depth, set()
        self.scopes = [_Bindings(tree)]

    def lookup(self, table, name):
        """Innermost function scope first, then the module (class bodies skipped)."""
        for bindings in reversed(self.scopes):
            if name in getattr(bindings, table):
                return getattr(bindings, table)[name], True
        return None, False

    # Name resolution -------------------------------------------------
    def qualified(self, node):
        name = dotted(node)
        if name is None:
            return None
        head, _, rest = name.partition(".")
        target, found = self.lookup("imports", head)
        head = target if found else head
        return head + ("." + rest if rest else "")

    def resolve(self, node, seen=()):
        """Follow a Name to its single same-module binding."""
        while isinstance(node, ast.Name) and node.id not in seen:
            value, found = self.lookup("values", node.id)
            if not found or value is None:
                break
            seen = seen + (node.id,)
            node = value
        return node

    def literal(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def interpreter(self, node):
        """"python", "shell" or None for an interpreter-valued expression."""
        node = self.resolve(node)
        if self.qualified(node) == "sys.executable":
            return "python"
        if isinstance(node, ast.Call) and self.qualified(node.func) == "shutil.which" and node.args:
            return self.interpreter_name(self.literal(node.args[0]))
        return self.interpreter_name(self.literal(node))

    @staticmethod
    def interpreter_name(text):
        if not text:
            return None
        base = text.rsplit("/", 1)[-1]
        if PYTHON_NAME.fullmatch(base):
            return "python"
        if base in SHELL_NAMES:
            return "shell"
        return None

    def elements(self, node, seen=()):
        """Flatten a sequence expression; unknown pieces stay as nodes."""
        if isinstance(node, ast.Name):
            value, _ = self.lookup("values", node.id)
            if node.id in seen or value is None:
                return None
            return self.elements(value, seen + (node.id,))
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    out.extend(self.elements(item.value, seen) or [item])
                else:
                    out.append(item)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.elements(node.left, seen), self.elements(node.right, seen)
            if left is not None and right is not None:
                return left + right
        return None

    # Sequence scan ---------------------------------------------------
    def scan_sequence(self, items, scope, command):
        for index, item in enumerate(items):
            kind = self.interpreter(item)
            if kind is None:
                continue
            position = index + 1
            while True:
                if position >= len(items):
                    if command:
                        self.findings.add((scope, "STDIN"))
                    break
                text = self.literal(items[position])
                if text is None:
                    break
                if kind == "python":
                    if text == "-":
                        self.findings.add((scope, "STDIN"))
                        break
                    if PYTHON_INLINE.match(text):
                        self.findings.add((scope, "INLINE"))
                        break
                    if text == "--" or PYTHON_MODULE.fullmatch(text) or not text.startswith("-"):
                        break
                    position += 2 if text in ("-W", "-X") else 1
                else:
                    if SHELL_INLINE.fullmatch(text):
                        self.findings.add((scope, "SHELL_INLINE"))
                        break
                    if text == "--" or not text.startswith("-"):
                        break
                    position += 1

    # Tree walk -------------------------------------------------------
    def run(self):
        self.walk(self.tree, "<module>")
        return self.findings

    def walk(self, node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name if scope == "<module>" else scope + "." + child.name
            function = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if function:
                self.scopes.append(_Bindings(child))
            self.visit(child, inner)
            self.walk(child, inner)
            if function:
                self.scopes.pop()

    def visit(self, node, scope):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            for name in names:
                root = name.split(".")[0]
                if root == "multiprocessing":
                    self.findings.add((scope, "MULTIPROCESSING"))
                elif root == "pty":
                    self.findings.add((scope, "PTY"))
            if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "ProcessPoolExecutor" for alias in node.names):
                self.findings.add((scope, "PROCESS_POOL"))
        elif isinstance(node, (ast.List, ast.Tuple)):
            items = self.elements(node)
            if items is not None:
                self.scan_sequence(items, scope, command=False)
        elif isinstance(node, ast.BinOp):
            items = self.elements(node)
            if items is not None:
                self.scan_sequence(items, scope, command=False)
        elif isinstance(node, ast.Attribute) and node.attr == "ProcessPoolExecutor":
            self.findings.add((scope, "PROCESS_POOL"))
        elif isinstance(node, ast.Name) and node.id == "ProcessPoolExecutor":
            self.findings.add((scope, "PROCESS_POOL"))
        elif isinstance(node, ast.Call):
            self.visit_call(node, scope)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.visit_source(node.value, scope)

    def visit_call(self, node, scope):
        name = self.qualified(node.func) or ""
        if name in SHELL_CALLS:
            self.findings.add((scope, "SHELL_CALL"))
        for keyword in node.keywords:
            if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value in (False, None)):
                self.findings.add((scope, "SHELL"))
        method = name.rsplit(".", 1)[-1]
        if method in ("get_context", "set_start_method"):
            argument = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "method"), None)
            text = self.literal(argument) if argument is not None else None
            if text is not None and text != "fork":
                self.findings.add((scope, "START_METHOD"))
        if isinstance(node.func, ast.Attribute) and method in ("append", "insert", "extend") and node.args:
            values = node.args[1:2] if method == "insert" else node.args[:1]
            for value in values:
                pieces = self.elements(value) if method == "extend" else [value]
                if any(PYTHON_INLINE.fullmatch(self.literal(p) or "") for p in pieces or ()):
                    self.findings.add((scope, "DYNAMIC_FLAG"))
        # Positional argv such as os.execl(...) or create_subprocess_exec(...).
        self.scan_sequence([a for a in node.args if not isinstance(a, ast.Starred)], scope, command=False)
        # A literal argv passed directly to a call is a command: bare stdin counts.
        for argument in [*node.args, *(k.value for k in node.keywords)]:
            if isinstance(argument, (ast.List, ast.Tuple)):
                items = self.elements(argument)
                if items is not None:
                    self.scan_sequence(items, scope, command=True)

    def visit_source(self, text, scope):
        if self.depth >= NESTED_DEPTH or not ("\n" in text or ";" in text):
            return
        if not any(hint in text for hint in NESTED_HINTS):
            return
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return
        for _, kind in Detector(tree, self.depth + 1).run():
            self.findings.add((scope, kind if kind.startswith("NESTED_") else "NESTED_" + kind))


def scan_text(text, filename="<case>"):
    return Detector(ast.parse(text, filename)).run()


def census():
    """Every finding under tests/ as (repo-relative path, scope, kind)."""
    found = set()
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        for scope, kind in scan_text(path.read_text(encoding="utf-8"), relative):
            found.add((relative, scope, kind))
    return found


def load_cases():
    """Inert detector cases: a ``# expect:`` header line, then Python source."""
    cases = {}
    for path in sorted(CASES.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        header = text.splitlines()[0]
        if not header.startswith("# expect:"):
            raise ValueError("detector case without expectation: " + path.name)
        expected = header[len("# expect:"):].split()
        cases[path.name] = (text, frozenset() if expected == ["none"] else frozenset(expected))
    return cases


class NoInlineInterpreterChildrenTests(unittest.TestCase):
    def setUp(self):
        self.findings = census()

    def test_unreviewed_findings_are_exactly_the_known_remaining_files(self):
        unreviewed = {path for path, scope, kind in self.findings
                      if (path, scope, kind) not in REVIEWED_EXCEPTIONS}
        self.assertEqual(unreviewed, set(KNOWN_REMAINING),
                         sorted(f for f in self.findings if f not in REVIEWED_EXCEPTIONS))

    def test_every_reviewed_exception_still_matches(self):
        self.assertEqual(set(REVIEWED_EXCEPTIONS) - self.findings, set())

    def test_detector_qualification_cases(self):
        cases = load_cases()
        self.assertTrue(any(expected for _, expected in cases.values()))
        self.assertTrue(any(not expected for _, expected in cases.values()))
        for name, (text, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual({kind for _, kind in scan_text(text, name)}, set(expected))

    def test_fixture_files_follow_the_layout(self):
        for path in sorted((TESTS / "child_fixtures").glob("*.py")):
            if path.name == "__init__.py":
                continue
            with self.subTest(fixture=path.name):
                self.assertFalse(path.name.startswith("test"))
                tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
                self.assertIsNotNone(ast.get_docstring(tree))
                body = tree.body[1:]
                self.assertEqual(ast.dump(body[0]), ast.dump(ast.parse("import sys").body[0]))
                self.assertEqual(ast.dump(body[1]), ast.dump(ast.parse(
                    "if not sys.flags.isolated: sys.path[0] = ''").body[0]))
                self.assertTrue(any(
                    isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
                    for node in body))


if __name__ == "__main__":
    unittest.main()
