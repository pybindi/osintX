import argparse
import contextlib
import hashlib
import io
import unittest
from unittest.mock import patch
from urllib.error import URLError

import osintX as app


class ValidationTests(unittest.TestCase):
    def test_ports(self):
        self.assertEqual(app._parse_ports("80,443,80,8000-8002"), [80, 443, 8000, 8001, 8002])
        for value in ("", "80,", "0", "65536", "4-1", "1-65535", "abc"):
            with self.subTest(value=value), self.assertRaises(app.UserError):
                app._parse_ports(value)

    def test_domains(self):
        self.assertEqual(app.normalize_domain("Example.COM."), "example.com")
        self.assertTrue(app.normalize_domain("пример.рф").startswith("xn--"))
        for value in ("", "https://example.com", "-bad.com", "a..com", "a_b.com"):
            with self.subTest(value=value), self.assertRaises(app.UserError):
                app.normalize_domain(value)

    def test_timeouts(self):
        self.assertEqual(app.valid_timeout("2.5"), 2.5)
        for value in ("0", "-1", "nan", "inf", "61", "x"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                app.valid_timeout(value)

    def test_private_ip_does_not_call_service(self):
        with patch.object(app, "get_json") as request:
            with self.assertRaises(app.UserError):
                app.cmd_ip(argparse.Namespace(address="127.0.0.1", timeout=8, json=False))
            request.assert_not_called()


class ServiceTests(unittest.TestCase):
    def test_network_failure_has_readable_error(self):
        with patch.object(app, "urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(app.UserError, "unavailable"):
                app.get_json("https://example.com", 8)

    def test_null_mx_is_reported(self):
        with patch.object(app, "run_dig", return_value=["0 ."]), patch.object(app, "out") as output:
            app.cmd_email(argparse.Namespace(address="test@example.com", timeout=8, json=False))
            self.assertTrue(output.call_args.args[0]["mail_disabled_null_mx"])

    def test_dns_filters_record_type(self):
        response = {"Status": 0, "Answer": [{"type": 5, "data": "alias."}, {"type": 1, "data": "1.2.3.4"}]}
        with patch.object(app, "get_json", return_value=response):
            self.assertEqual(app.run_dig("example.com", "A", 8), ["1.2.3.4"])

    def test_dns_failure_is_not_empty_result(self):
        for status in (2, 3, None):
            with patch.object(app, "get_json", return_value={"Status": status}):
                with self.assertRaises(app.UserError):
                    app.run_dig("example.com", "A", 8)

    def test_dns_empty_is_valid(self):
        with patch.object(app, "get_json", return_value={"Status": 0}):
            self.assertEqual(app.run_dig("example.com", "MX", 8), [])

    def test_json_rejects_wrong_shape(self):
        with patch.object(app, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"[]"
            with self.assertRaises(app.UserError):
                app.get_json("https://example.com", 8)

    def test_json_rejects_oversized_reply(self):
        with patch.object(app, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"x" * (app.MAX_RESPONSE + 1)
            with self.assertRaises(app.UserError):
                app.get_json("https://example.com", 8)


class MenuTests(unittest.TestCase):
    def run_menu(self, values):
        output = io.StringIO()
        with patch("builtins.input", side_effect=values), contextlib.redirect_stdout(output):
            app.menu()
        return output.getvalue()

    def test_exit(self):
        self.assertIn("Choose a number", self.run_menu(["0"]))

    def test_invalid_choice_recovers(self):
        self.assertIn("Invalid choice", self.run_menu(["wrong", "0"]))

    def test_eof_inside_operation_exits_without_traceback(self):
        self.run_menu(["1", EOFError()])

    def test_hash_preserves_spaces_and_returns_to_menu(self):
        output = self.run_menu(["6", "", " hello ", "", "0"])
        self.assertIn(hashlib.sha256(b" hello ").hexdigest(), output)

    def test_empty_text_hash(self):
        output = self.run_menu(["6", "", "", "", "0"])
        self.assertIn(hashlib.sha256(b"").hexdigest(), output)

    def test_error_returns_to_menu(self):
        output = self.run_menu(["1", "not-an-ip", "", "0"])
        self.assertIn("Invalid IP", output)


class AddedToolsTests(unittest.TestCase):
    def test_rdap_null_nameservers(self):
        with patch.object(app, "get_json", return_value={"nameservers": None}), patch.object(app, "out") as output:
            app.cmd_whois(argparse.Namespace(target="example.com", timeout=8, json=False))
            self.assertEqual(output.call_args.args[0]["nameservers"], [])

    def test_telegram_normalization(self):
        for value in ("@ExampleUser", "ExampleUser", "https://t.me/ExampleUser", "t.me/ExampleUser"):
            self.assertEqual(app.normalize_telegram_username(value), "ExampleUser")
        for value in ("https://evil.test/name", "t.me/+123456789", "@bad name", "t.me/user/123", "t.me/share", "t.me/test?x=1"):
            with self.subTest(value=value), self.assertRaises(app.UserError):
                app.normalize_telegram_username(value)

    def test_phone_normalization(self):
        self.assertEqual(app.normalize_phone("+1 (202) 555-0123"), "+12025550123")
        for value in ("2025550123", "+012345678", "+123", "+1234567890123456", "+1234567 ext 4"):
            with self.subTest(value=value), self.assertRaises(app.UserError):
                app.normalize_phone(value)

    def test_phone_metadata_is_offline_and_has_no_telegram(self):
        with patch.object(app, "out") as output, patch.object(app, "urlopen") as request:
            app.cmd_phone(argparse.Namespace(number="+12025550123", json=False))
            result = output.call_args.args[0]
            self.assertEqual(result["country_calling_code"], 1)
            self.assertEqual(result["region_code"], "US")
            self.assertTrue(result["valid_numbering_pattern"])
            self.assertFalse(result["network_requests"])
            self.assertNotIn("telegram", str(result).lower())
            request.assert_not_called()

    def test_phone_invalid_country_code(self):
        with self.assertRaises(app.UserError):
            app.cmd_phone(argparse.Namespace(number="+999123456789", json=False))

    def test_phone_possible_but_invalid_pattern_is_not_claimed_valid(self):
        with patch.object(app, "out") as output:
            app.cmd_phone(argparse.Namespace(number="+12001230101", json=False))
            self.assertFalse(output.call_args.args[0]["valid_numbering_pattern"])

    def test_nested_output_is_readable(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app.out({"events": [{"action": "registration", "date": "2020-01-01"}]}, False)
        self.assertIn("    action: registration", output.getvalue())
        self.assertNotIn("{'action'", output.getvalue())

    def test_metadata_parser(self):
        parser = app.PublicPageMetadata()
        parser.feed('<meta content="Name &amp; Test" property="og:title"><meta name="og:description" content="Public bio">')
        self.assertEqual(parser.values["og:title"], "Name & Test")

    def test_generic_telegram_page_remains_unverified(self):
        with patch.object(app, "urlopen") as request, patch.object(app, "out") as output:
            response = request.return_value.__enter__.return_value
            response.url = "https://t.me/example"
            response.read.return_value = b'<meta property="og:title" content="Telegram: Contact">'
            app.cmd_telegram(argparse.Namespace(username="example", timeout=8, json=False))
            self.assertEqual(output.call_args.args[0]["account_status"], "unverified")

    def test_offline_url_parser(self):
        with patch.object(app, "out") as output, patch.object(app, "urlopen") as request:
            app.cmd_url(argparse.Namespace(url="https://example.com:8443/path?q=x#part", json=False))
            self.assertEqual(output.call_args.args[0]["port"], 8443)
            request.assert_not_called()

    def test_invalid_ports_rejected_before_dns(self):
        with patch.object(app.socket, "getaddrinfo") as resolver:
            with self.assertRaises(app.UserError):
                app.cmd_port(argparse.Namespace(host="example.com", ports="1-65535", timeout=8))
            resolver.assert_not_called()

    def test_new_cli_commands(self):
        parser = app.build_parser()
        for command, value in (("telegram", "example"), ("phone", "+12025550123"), ("reverse", "8.8.8.8"), ("url", "example.com")):
            args = parser.parse_args([command, value])
            self.assertTrue(callable(args.func))


if __name__ == "__main__":
    unittest.main()
