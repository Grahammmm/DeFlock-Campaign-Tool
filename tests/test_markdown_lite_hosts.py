"""markdown_lite links only https targets whose host is a public DNS name; lint names the rest link-host (RF1740-10)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tool.markdown_lite import lint, main, render

ROOT = Path(__file__).resolve().parents[1]
DETAIL = "link host is not a public DNS name"
GOOD = ("https://example.org/a?b=1#c", "https://sub.example.co.uk:8443/", "https://example.org:65535/",
        "https://example.org:443", "https://example.org:1/", "https://EXAMPLE.Org", "https://a-b.example.invalid/x")
BAD_LABELS = ("https://a..b.org", "https://-x.org", "https://x-.org", "https://x.org.", "https://.x.org",
              "https://x.-org.net", "https://x.org-")
LOCAL = ("https://localhost/", "https://10.0.0.1/", "https://intranet/", "https://192.168.1.1:8443/x",
         "https://example.123/", "https://example.c/", "https://example.c0m/", "https://[::1]/",
         "https://u@example.org/")
BAD_PORTS = ("https://example.org:0/", "https://example.org:65536/", "https://example.org:99999/",
             "https://example.org:080/", "https://example.org:00443", "https://example.org:/",
             "https://example.org:4a3/")
NOT_HTTPS = ("http://example.org/", "/local", "javascript:alert(1)", "HTTPS://example.org", 'https://example.org/"x',
             "https://example.org?q=1")
LABEL_63 = "a" * 63
HOST_253 = ".".join([LABEL_63, "b" * 63, "c" * 63, "d" * 57, "org"])
HOST_254 = ".".join([LABEL_63, "b" * 63, "c" * 63, "d" * 58, "org"])


def linked(target, text="T"):
    return render(f"[{text}]({target})")


def link_codes(target):
    return [code for _, code, _ in lint(f"[T]({target})")]


class HostTests(unittest.TestCase):
    def test_public_hosts_linked_exactly_as_before(self):
        self.assertEqual(linked("https://example.org/a?b=1#c"),
                         '<p><a href="https://example.org/a?b=1#c" rel="noopener noreferrer">T</a></p>')
        self.assertEqual(linked("https://sub.example.co.uk:8443/"),
                         '<p><a href="https://sub.example.co.uk:8443/" rel="noopener noreferrer">T</a></p>')
        for target in GOOD:
            with self.subTest(target=target):
                self.assertIn(f'<a href="{target}"', linked(target))
                self.assertEqual(lint(f"[T]({target})"), [])

    def test_bad_labels_render_text_and_lint_link_host(self):
        for target in BAD_LABELS:
            with self.subTest(target=target):
                self.assertEqual(linked(target), "<p>T</p>")
                self.assertEqual(lint(f"[T]({target})"), [(1, "link-host", DETAIL)])

    def test_local_names_and_ip_literals_refused(self):
        for target in LOCAL:
            with self.subTest(target=target):
                self.assertEqual(linked(target), "<p>T</p>")
                self.assertEqual(link_codes(target), ["link-host"])

    def test_port_range_and_leading_zero(self):
        for target in BAD_PORTS:
            with self.subTest(target=target):
                self.assertEqual(linked(target), "<p>T</p>")
                self.assertEqual(link_codes(target), ["link-host"])
        for target in ("https://example.org:65535/", "https://example.org:443"):
            self.assertIn("<a href=", linked(target))

    def test_label_and_host_length_limits(self):
        self.assertEqual((len(HOST_253), len(HOST_254)), (253, 254))
        self.assertIn("<a href=", linked(f"https://{LABEL_63}.org/"))
        self.assertIn("<a href=", linked(f"https://{HOST_253}/"))
        for target in (f"https://{LABEL_63}a.org/", f"https://{HOST_254}/"):
            self.assertEqual(linked(target), "<p>T</p>")
            self.assertEqual(link_codes(target), ["link-host"])
        self.assertIn("<a href=", linked("https://" + ".".join(["a"] * 9) + ".org"))
        self.assertEqual(link_codes("https://" + ".".join(["a"] * 10) + ".org"), ["link-host"])

    def test_lint_never_echoes_host_or_text(self):
        source = "[SECRET-TEXT](https://private-host-name.lan:99999/path)\n[OTHER-TEXT](https://intranet/)"
        self.assertEqual(lint(source), [(1, "link-host", DETAIL), (2, "link-host", DETAIL)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copy.md"
            path.write_text(source, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = main(["--lint", str(path)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(status, 1)
        self.assertEqual(out.getvalue(), f"{path}:1: link-host: {DETAIL}\n{path}:2: link-host: {DETAIL}\n"
                                         "files=1 findings=2\n")
        for secret in ("SECRET-TEXT", "OTHER-TEXT", "private-host-name", "intranet", "99999", "/path"):
            self.assertNotIn(secret, text)

    def test_render_and_lint_agree_one_code_per_link(self):
        for target in GOOD + BAD_LABELS + LOCAL + BAD_PORTS + NOT_HTTPS:
            with self.subTest(target=target):
                source = f"- see [T]({target}) now"
                codes = [code for _, code, _ in lint(source) if code.startswith("link-")]
                if "<a href=" in render(source):
                    self.assertEqual(codes, [])
                else:
                    self.assertEqual(len(codes), 1)
                    self.assertIn(codes[0], ("link-dropped", "link-host"))
        self.assertEqual(link_codes("http://localhost/"), ["link-dropped"])
        for target in NOT_HTTPS:
            self.assertEqual(link_codes(target), ["link-dropped"])
        mixed = "[a](https://example.org) [b](https://x..org) [c](http://example.org) **[d](https://localhost)**"
        self.assertEqual([code for _, code, _ in lint(mixed)], ["link-dropped", "link-host", "link-host"])
        self.assertEqual(render(mixed).count("<a href="), 1)

    def test_committed_example_content_has_no_link_host(self):
        content = ROOT / "examples" / "fictional-campaign" / "content"
        fields = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.endswith("_md") and isinstance(item, str):
                        fields.append(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for path in sorted(content.rglob("*.json")):
            walk(json.loads(path.read_text(encoding="utf-8")))
        self.assertTrue(fields)
        self.assertEqual([f for f in fields if any(code == "link-host" for _, code, _ in lint(f))], [])


if __name__ == "__main__":
    unittest.main()
