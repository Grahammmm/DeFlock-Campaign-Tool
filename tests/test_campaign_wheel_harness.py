"""Regressions for the clean-wheel harness interpreter selection."""
import tempfile
import unittest
from pathlib import Path
from tools.test_campaign_wheel import selected_python, require_virtual_environment

class WheelHarnessTests(unittest.TestCase):
    def test_symlink_executable_is_not_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "base-python"
            base.touch()
            selected = root / "venv" / "bin" / "python"
            selected.parent.mkdir(parents=True)
            selected.symlink_to(base)
            self.assertEqual(selected_python(selected), str(selected))
            self.assertNotEqual(selected_python(selected), str(base))
    def test_accepts_selected_virtual_environment(self):
        require_virtual_environment("/fixture/venv/bin/python",
            dict(executable="/fixture/venv/bin/python", prefix="/fixture/venv", base_prefix="/base"))
    def test_rejects_base_interpreter(self):
        with self.assertRaisesRegex(RuntimeError, "separate virtual environment"):
            require_virtual_environment("/base/python",
                dict(executable="/base/python", prefix="/base", base_prefix="/base"))
    def test_rejects_different_executable(self):
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            require_virtual_environment("/fixture/venv/bin/python",
                dict(executable="/base/python", prefix="/fixture/venv", base_prefix="/base"))

if __name__ == "__main__":
    unittest.main()
