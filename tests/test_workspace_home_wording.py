"""Home-screen authored wording uses the existing shared, alert-only wording checks.

Dynamic next actions remain verbatim from readiness-snapshot-v1; its emitter owns
privacy validation, not this view. These checks cover authored screen wording and
the synthetic current-readiness sentence exercised by the Worker integration test.
"""
import importlib.util
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("home_privacy", ROOT / "tools/flock_privacy.py")
assert spec is not None and spec.loader is not None
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class HomeWordingTests(unittest.TestCase):
    def test_authored_home_wording_and_current_readiness_fixture(self):
        home = (ROOT / "workers/workspace/src/views/home.ts").read_text()
        test = (ROOT / "workers/workspace/test/home-screen.test.ts").read_text()
        match = re.search(r'const ACTION = "([^"]+)";', test)
        self.assertIsNotNone(match)
        assert match is not None
        sentence = match.group(1)
        for label, text in (("home", home), ("readiness fixture", sentence)):
            with self.subTest(source=label):
                self.assertEqual(privacy.categories(text), [])
                self.assertEqual(privacy.legal_claims(text), [])


if __name__ == "__main__":
    unittest.main()
