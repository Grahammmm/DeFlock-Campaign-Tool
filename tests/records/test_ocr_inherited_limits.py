"""Mocked OCR pre-exec ceilings; no OS limits or native children are changed."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from campaign_tool.records.extract import ocr


class OCRInheritedLimitsTests(unittest.TestCase):
    def setUp(self):
        self.bounds = {"address-space": (-1, -1), "file-size": (-1, -1)}
        self.resource = SimpleNamespace(
            RLIMIT_AS="address-space", RLIMIT_FSIZE="file-size", RLIM_INFINITY=-1,
            getrlimit=Mock(side_effect=self.bounds.__getitem__), setrlimit=Mock(),
        )
        self.enterContext(patch.dict("sys.modules", {"resource": self.resource}))
        self.umask = self.enterContext(patch.object(ocr.os, "umask"))

    def assert_caps(self, memory, file_size):
        ocr._limit_child()
        self.assertEqual(self.resource.getrlimit.call_args_list,
                         [call("address-space"), call("file-size")])
        self.assertEqual(self.resource.setrlimit.call_args_list,
                         [call("address-space", (memory, memory)),
                          call("file-size", (file_size, file_size))])
        self.umask.assert_called_once_with(0o077)

    def test_unlimited_inheritance_uses_default_ceilings(self):
        self.assert_caps(ocr.CHILD_MEMORY_BYTES, ocr.CHILD_FILE_BYTES)

    def test_looser_finite_inheritance_uses_default_ceilings(self):
        self.bounds["address-space"] = (4 * 1024**3, 5 * 1024**3)
        self.bounds["file-size"] = (1024**3, 2 * 1024**3)
        self.assert_caps(ocr.CHILD_MEMORY_BYTES, ocr.CHILD_FILE_BYTES)

    def test_memory_preserves_tighter_soft_limit(self):
        self.bounds["address-space"] = (1024**3, 2 * 1024**3)
        self.assert_caps(1024**3, ocr.CHILD_FILE_BYTES)

    def test_memory_preserves_finite_hard_limit_with_infinity_sentinel(self):
        # Defensively cover the hard-bound branch even with an unlimited soft value.
        self.bounds["address-space"] = (-1, 2 * 1024**3)
        self.assert_caps(2 * 1024**3, ocr.CHILD_FILE_BYTES)

    def test_file_size_preserves_tighter_soft_limit(self):
        self.bounds["file-size"] = (1024**2, 2 * 1024**2)
        self.assert_caps(ocr.CHILD_MEMORY_BYTES, 1024**2)

    def test_file_size_preserves_finite_hard_limit_with_infinity_sentinel(self):
        self.bounds["file-size"] = (-1, 2 * 1024**2)
        self.assert_caps(ocr.CHILD_MEMORY_BYTES, 2 * 1024**2)

    def test_zero_limit_is_preserved(self):
        self.bounds["file-size"] = (0, 1024**2)
        self.assert_caps(ocr.CHILD_MEMORY_BYTES, 0)

    def test_memory_and_file_ceilings_are_independent(self):
        self.bounds["address-space"] = (2 * 1024**3, 2 * 1024**3)
        self.bounds["file-size"] = (64 * 1024**2, -1)
        self.assert_caps(2 * 1024**3, 64 * 1024**2)

    def test_memory_query_failure_propagates_without_setting_limits(self):
        failure = OSError("synthetic query refusal")
        self.resource.getrlimit.side_effect = failure
        with self.assertRaises(OSError) as raised:
            ocr._limit_child()
        self.assertIs(raised.exception, failure)
        self.resource.setrlimit.assert_not_called()
        self.umask.assert_not_called()

    def test_file_query_failure_does_not_restore_or_relax_memory(self):
        failure = OSError("synthetic file query refusal")
        self.resource.getrlimit.side_effect = [(1024**3, 2 * 1024**3), failure]
        with self.assertRaises(OSError) as raised:
            ocr._limit_child()
        self.assertIs(raised.exception, failure)
        self.resource.setrlimit.assert_called_once_with("address-space", (1024**3, 1024**3))
        self.umask.assert_not_called()

    def test_memory_set_failure_propagates_without_fallback(self):
        failure = ValueError("synthetic memory set refusal")
        self.resource.setrlimit.side_effect = failure
        with self.assertRaises(ValueError) as raised:
            ocr._limit_child()
        self.assertIs(raised.exception, failure)
        self.resource.getrlimit.assert_called_once_with("address-space")
        self.resource.setrlimit.assert_called_once_with(
            "address-space", (ocr.CHILD_MEMORY_BYTES, ocr.CHILD_MEMORY_BYTES))
        self.umask.assert_not_called()

    def test_file_set_failure_does_not_restore_or_relax_memory(self):
        failure = OSError("synthetic file set refusal")
        self.bounds["address-space"] = (1024**3, 2 * 1024**3)
        self.resource.setrlimit.side_effect = [None, failure]
        with self.assertRaises(OSError) as raised:
            ocr._limit_child()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.resource.setrlimit.call_args_list,
                         [call("address-space", (1024**3, 1024**3)),
                          call("file-size", (ocr.CHILD_FILE_BYTES, ocr.CHILD_FILE_BYTES))])
        self.umask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
