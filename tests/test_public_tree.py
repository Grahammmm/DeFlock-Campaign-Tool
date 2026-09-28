"""Scanner fixtures are synthetic and assembled, never real credentials."""
import unittest
from tools.check_public_tree import violations


class PublicTreeTests(unittest.TestCase):
    def test_normal_source_is_allowed(self):
        self.assertEqual(violations("module.py", b"print('hello')\n"), [])

    def test_private_artifact_path_is_rejected(self):
        self.assertEqual(violations("private/message.eml", b"synthetic")[0][2], "private_path")

    def test_token_is_detected_without_echoing_value(self):
        token = b"ghp_" + b"a" * 36
        hits = violations("example.txt", b"line one\n" + token)
        self.assertEqual(hits, [("example.txt", 2, "github_token")])
        self.assertNotIn(token.decode(), repr(hits))

    def test_signed_link_is_detected(self):
        value = b"https://example.invalid/file?" + b"X-Amz-Signature=" + b"a" * 64
        self.assertEqual(violations("example.txt", value)[0][2], "signed_url")

    def test_private_key_and_host_paths_are_rejected(self):
        key = b"-----BEGIN " + b"PRIVATE KEY-----"
        self.assertEqual(violations("example.txt", key)[0][2], "private_key")
        path = b"/workspace/" + b"flockbot/work"
        self.assertEqual(violations("example.txt", path)[0][2], "pilot_host_path")


if __name__ == "__main__":
    unittest.main()
