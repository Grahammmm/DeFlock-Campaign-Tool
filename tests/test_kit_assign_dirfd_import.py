"""kit_assign records descriptor-relative support by name, so a lazy first import under a replaced os.open is safe.

An earlier test that replaces ``os.open`` while a command first imports kit_assign must not leave every later
request-owner read in the same process refusing with "file changed or could not be read".
Run from the repository root: python3 -B -m unittest tests.test_kit_assign_dirfd_import -v
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_assign  # noqa: E402,F401  (makes the package and its dependencies importable)


class ImportUnderReplacedOpenTests(unittest.TestCase):
    def test_first_import_while_os_open_is_replaced_keeps_dir_fd_support(self):
        saved = sys.modules.pop("campaign_tool.kit_assign")
        try:
            with mock.patch.object(os, "open", side_effect=OSError("replaced")):
                fresh = importlib.import_module("campaign_tool.kit_assign")
            self.assertEqual(fresh._DIR_FD, os.open in os.supports_dir_fd)
            self.assertTrue(fresh._DIR_FD, "this platform supports descriptor-relative open")
        finally:
            sys.modules["campaign_tool.kit_assign"] = saved
            setattr(sys.modules["campaign_tool"], "kit_assign", saved)


if __name__ == "__main__":
    unittest.main()
