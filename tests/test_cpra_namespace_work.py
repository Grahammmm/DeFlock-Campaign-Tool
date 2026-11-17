import re
import unittest
from unittest import mock

from campaign_tool import cpra_cites
from campaign_tool.cpra_cites import find_former_cpra_cites

_REAL_SEARCH = re.search


class _Metered(str):
    reads = 0

    def __getitem__(self, key):
        type(self).reads += len(self) if isinstance(key, slice) and key == slice(None) else (
            len(range(*key.indices(len(self)))) if isinstance(key, slice) else 1)
        return str.__getitem__(self, key)


def _scan(text):
    """Return (spans, work) where work = searched chars + sliced/read chars."""
    searched = [0]

    def search(pattern, string, *args, **kwargs):
        searched[0] += len(string)
        return _REAL_SEARCH(pattern, string, *args, **kwargs)

    metered = _Metered(text)
    _Metered.reads = 0
    with mock.patch.object(re, "search", search):
        spans = find_former_cpra_cites(metered)
    return spans, searched[0] + _Metered.reads


class NamespaceWorkTests(unittest.TestCase):
    def assertBounded(self, text):
        spans, work = _scan(text)
        self.assertLessEqual(work, 32 * len(text), "work %d for %d chars" % (work, len(text)))
        return spans

    def test_current_marker_namespace_work_is_bounded(self):
        for n in (1000, 2000, 4000):
            self.assertEqual(self.assertBounded("section 7922.535; " * n), [])

    def test_former_and_other_code_marker_work_is_bounded(self):
        for n in (1000, 2000, 4000):
            unit = "section 6254; "
            self.assertEqual(self.assertBounded(unit * n),
                             [(i * len(unit), i * len(unit) + 12) for i in range(n)])
            self.assertEqual(self.assertBounded("Penal Code section 6254; " * n), [])

    def test_long_unicode_whitespace_does_not_require_prefix_copies(self):
        ws = " \t\n\u2003\u00a0" * 20000
        text = "Penal Code" + ws + "section 6254"
        self.assertEqual(self.assertBounded(text), [])
        text = "Penal Code" + ws + "section 6254. section 6255"
        start = text.index("section 6255")
        self.assertEqual(self.assertBounded(text), [(start, start + 12)])

    def test_code_suffix_preserves_real_unicode_word_boundary(self):
        cases = {
            "xCode section 6254": [(6, 18)],
            "éCode section 6254": [(6, 18)],
            "_Code section 6254": [(6, 18)],
            "9Code section 6254": [(6, 18)],
            "!Code section 6254": [],
            "Code section 6254": [],
            "CODE\u00a0section 6254": [],
            "code\nsection 6254": [],
            "Code": [],
            "de section 6254": [(3, 15)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_former_cpra_cites(text), expected)

    def test_explicit_government_namespace_and_nonstring_controls(self):
        self.assertEqual(find_former_cpra_cites("é Government Code section 6254"), [(2, 30)])
        self.assertEqual(find_former_cpra_cites("Gov. Code § 6250"), [(0, 16)])
        self.assertEqual(find_former_cpra_cites("Government Code 7922.535"), [])
        self.assertEqual(find_former_cpra_cites("section 6277"), [])
        for bad in ("", None, b"section 6254", 6254):
            self.assertEqual(find_former_cpra_cites(bad), [])

    def test_suffix_checks_do_not_mutate_or_reorder_spans(self):
        sep = " \n\t" * 50
        text = ("section 6254(a)" + sep + "Civil Code" + sep + "section 6254" + sep +
                "Gov Code section 6255" + sep + "section 7922.535" + sep + "§ 6276.48")
        expected = ["section 6254(a)", "Gov Code section 6255", "§ 6276.48"]
        spans = find_former_cpra_cites(text)
        self.assertEqual([text[a:b] for a, b in spans], expected)
        self.assertEqual(spans, sorted(spans))


if __name__ == "__main__":
    unittest.main()
