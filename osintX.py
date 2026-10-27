"""osintX - cli wire."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import re
import socket
import sys
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

APP = "osintX"
VERSION = "1.3.0"
UA = f"{APP}/{VERSION}"
DEFAULT_TIMEOUT = 8.0
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DIG_TYPES = ("A", "AAAA", "MX", "TXT", "NS", "CNAME")
PORT_DEFAULT = "22,80,443,3389,8080"
MAX_RESPONSE = 1024 * 1024


class UserError(ValueError):
    """An actionable input or service error, without an internal traceback."""


def normalize_domain(value: str) -> str:
    name = value.strip().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UserError("Invalid domain name.") from exc
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise UserError("Enter a domain such as example.com, without https:// or a path.")
    return name


def valid_timeout(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be a number.") from exc
    if not math.isfinite(number) or not 0 < number <= 60:
        raise argparse.ArgumentTypeError("Timeout must be greater than 0 and at most 60 seconds.")
    return number


def out(data: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    def render(key: str, value: Any, depth: int = 0) -> None:
        indent = "  " * depth
        if value is None or value == "" or value == []:
            print(f"{indent}{key}: no data")
            return
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if isinstance(value, dict):
            print(f"{indent}{key}:")
            for child, item in value.items():
                render(str(child), item, depth + 1)
        elif isinstance(value, (list, tuple)):
            print(f"{indent}{key}:")
            for index, item in enumerate(value, 1):
                render(str(index), item, depth + 1)
        else:
            print(f"{indent}{key}: {value}")

    for key, value in data.items():
        render(key, value)


def get_json(url: str, timeout: float) -> dict[str, Any]:
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise UserError("The service response is too large.")
        data = json.loads(raw.decode("utf-8"))
    except UserError:
        raise
    except HTTPError as exc:
        raise UserError(f"The service returned HTTP {exc.code}. Try again later.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UserError("Service unavailable: check your connection or try again later.") from exc
    except (ValueError, UnicodeError) as exc:
        raise UserError("The service returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise UserError("Unexpected service response format.")
    return data


def run_dig(name: str, qtype: str, timeout: float) -> list[str]:
    # HTTPS DNS removes the dependency on Unix utilities on Windows.
    data = get_json("https://dns.google/resolve?" + urlencode({"name": name, "type": qtype}), timeout)
    status = data.get("Status")
    if status == 3:
        raise UserError(f"Domain {name} does not exist (NXDOMAIN).")
    if status != 0:
        raise UserError(f"DNS error: status {status}.")
    types = {"A": 1, "AAAA": 28, "MX": 15, "TXT": 16, "NS": 2, "CNAME": 5}
    answers = data.get("Answer", [])
    if not isinstance(answers, list):
        raise UserError("Unexpected DNS response format.")
    return [str(item["data"]) for item in answers
            if isinstance(item, dict) and item.get("type") == types[qtype] and "data" in item]


def cmd_ip(args: argparse.Namespace) -> None:
    address = args.address.strip()
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise UserError("Invalid IP address. Example: 8.8.8.8")
    if not ipaddress.ip_address(address).is_global:
        raise UserError("Public geolocation is unavailable for local and reserved IP addresses.")
    data = get_json(f"https://ipwho.is/{address}", args.timeout)
    if not data.get("success", False):
        raise UserError(f"Lookup failed: {data.get('message', 'no data')}")
    connection = data.get("connection") or {}
    timezone = data.get("timezone") or {}
    if not isinstance(connection, dict) or not isinstance(timezone, dict):
        raise UserError("Unexpected geolocation response format.")
    result = {
        "ip": data.get("ip"),
        "success": data.get("success"),
        "type": data.get("type"),
        "continent": data.get("continent"),
        "country": data.get("country"),
        "country_code": data.get("country_code"),
        "region": data.get("region"),
        "city": data.get("city"),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "isp": connection.get("isp"),
        "org": connection.get("org"),
        "asn": connection.get("asn"),
        "timezone": timezone.get("id"),
        "timezone_details": timezone,
        "connection_details": connection,
        "continent_code": data.get("continent_code"),
        "postal_code": data.get("postal"),
        "calling_code": data.get("calling_code"),
        "country_capital": data.get("capital"),
        "country_borders": data.get("borders"),
        "source": "https://ipwho.is/",
        "note": "IP geolocation describes a network allocation, not an exact device or person location.",
    }
    out(result, args.json)


def cmd_domain(args: argparse.Namespace) -> None:
    name = normalize_domain(args.name)
    result: dict[str, Any] = {"domain": name}
    for qtype in DIG_TYPES:
        result[qtype.lower()] = run_dig(name, qtype, args.timeout)
    out(result, args.json)


def _scan_port(host: str, port: int, timeout: float) -> int | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return port
    except (socket.timeout, TimeoutError, OSError):
        return None


def _parse_ports(raw: str) -> list[int]:
    if not raw.strip() or len(raw) > 32768:
        raise UserError("Enter ports such as 80,443 or 8000-8010.")
    ports: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            raise UserError("Empty item in the port list.")
        if "-" in item:
            start_s, _, end_s = item.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError:
                raise UserError(f"Invalid port range: {item}")
            if not (1 <= start <= 65535 and 1 <= end <= 65535 and start <= end):
                raise UserError(f"Invalid port range: {item}")
            if end - start + 1 > 4096:
                raise UserError("A maximum of 4096 ports is allowed per scan.")
            ports.update(range(start, end + 1))
        else:
            try:
                port = int(item)
            except ValueError:
                raise UserError(f"Invalid port: {item}")
            if not 1 <= port <= 65535:
                raise UserError(f"Port must be within 1-65535: {port}")
            ports.add(port)
        if len(ports) > 4096:
            raise UserError("A maximum of 4096 ports is allowed per scan.")
    return sorted(ports)


def cmd_port(args: argparse.Namespace) -> None:
    host = args.host.strip()
    if not host:
        raise UserError("Enter a host address or domain.")
    ports = _parse_ports(args.ports)
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UserError("Could not resolve the host address.") from exc
    if not ports:
        raise SystemExit("no ports")
    if len(ports) > 4096:
        raise SystemExit("too many ports")
    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=min(64, len(ports))) as pool:
        futures = {pool.submit(_scan_port, host, port, args.timeout): port for port in ports}
        for future in as_completed(futures):
            port = future.result()
            if port is not None:
                open_ports.append(port)
    out({"host": host, "open": sorted(open_ports)}, args.json)


def cmd_http(args: argparse.Namespace) -> None:
    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlsplit(url)
    try:
        parsed.port
    except ValueError as exc:
        raise UserError("Invalid URL port.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UserError("Enter an HTTP/HTTPS URL without a username or password.")
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=args.timeout) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            result = {
                "url": url,
                "status": resp.status,
                "final": resp.url,
                "server": resp.headers.get("Server"),
                "content_type": resp.headers.get("Content-Type"),
                "title": title.group(1).strip() if title else "",
            }
    except HTTPError as exc:
        result = {"url": url, "status": exc.code, "final": exc.url, "error": str(exc.reason)}
    except (URLError, TimeoutError, OSError) as exc:
        raise UserError("Website unavailable: check the URL, connection, and certificate.") from exc
    out(result, args.json)


def cmd_email(args: argparse.Namespace) -> None:
    address = args.address.strip()
    ok = bool(EMAIL_RE.match(address))
    if not ok:
        raise UserError("Invalid email format. Example: name@example.com")
    domain = address.rsplit("@", 1)[1] if ok else ""
    domain = normalize_domain(domain)
    mx = run_dig(domain, "MX", args.timeout) if domain else []
    out({"email": address, "format_ok": ok, "mx": mx,
         "mail_disabled_null_mx": any(item.strip() == "0 ." for item in mx),
         "note": "MX records do not confirm that a specific mailbox exists."}, args.json)


def cmd_hash(args: argparse.Namespace) -> None:
    digest = hashlib.new(args.algo, args.text.encode("utf-8")).hexdigest()
    out({"algo": args.algo, "length": len(digest), "hash": digest}, args.json)


def cmd_whois(args: argparse.Namespace) -> None:
    target = normalize_domain(args.target)
    data = get_json("https://rdap.org/domain/" + quote(target, safe=""), args.timeout)
    nameservers = data.get("nameservers") or []
    if not isinstance(nameservers, list):
        raise UserError("Unexpected RDAP nameserver response format.")
    out({"domain": target, "handle": data.get("handle"),
         "status": data.get("status", []), "events": data.get("events", []),
         "nameservers": [item.get("ldhName", "") for item in nameservers
                          if isinstance(item, dict)]}, args.json)


class PublicPageMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = dict(attrs)
        key = attributes.get("property") or attributes.get("name")
        value = attributes.get("content")
        if key in {"og:title", "og:description", "og:image"} and value:
            self.values[key] = value


def normalize_telegram_username(value: str) -> str:
    username = value.strip()
    if username.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        url = username if "://" in username else "https://" + username
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment:
            raise UserError("Enter a username or profile URL without query parameters.")
        username = parsed.path.strip("/")
    username = username.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        raise UserError("Enter a Telegram username: 4-32 letters, digits or underscores, starting with a letter.")
    if username.lower() in {"joinchat", "share", "proxy", "socks", "addstickers", "login", "iv"}:
        raise UserError("This is a Telegram service link, not a profile username.")
    return username


def cmd_telegram(args: argparse.Namespace) -> None:
    username = normalize_telegram_username(args.username)
    url = "https://t.me/" + username
    try:
        with urlopen(Request(url, headers={"User-Agent": UA}), timeout=args.timeout) as response:
            if urlsplit(response.url).hostname != "t.me":
                raise UserError("Telegram redirected outside t.me; profile metadata was not accepted.")
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise UserError("Telegram page exceeds the response size limit.")
    except HTTPError as exc:
        raise UserError(f"Telegram returned HTTP {exc.code}; account existence is not confirmed.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UserError("Telegram is unavailable. Check your connection or try again later.") from exc
    metadata = PublicPageMetadata()
    metadata.feed(raw.decode("utf-8", "replace"))
    out({"username": username, "public_url": url,
         "page_title": metadata.values.get("og:title"),
         "page_description": metadata.values.get("og:description"),
         "page_image": metadata.values.get("og:image"),
         "account_status": "unverified",
         "note": "Public page metadata only. HTTP 200 or a generic contact page does not prove an account exists."}, args.json)


def normalize_phone(value: str) -> str:
    value = value.strip()
    if len(value) > 64 or not re.fullmatch(r"\+[0-9 ()-]+", value):
        raise UserError("Use international format, for example +1 202 555 0123. Extensions are not supported.")
    number = re.sub(r"[ ()-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
        raise UserError("Use + followed by 7-15 digits, with a nonzero country code.")
    return number


def cmd_phone(args: argparse.Namespace) -> None:
    number = normalize_phone(args.number)
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone
    except ImportError as exc:
        raise UserError("Phone metadata requires phonenumbers. Run: python -m pip install -r requirements.txt") from exc
    try:
        parsed = phonenumbers.parse(number, None)
    except phonenumbers.NumberParseException as exc:
        raise UserError("Unknown country calling code or invalid phone number.") from exc
    number_type = phonenumbers.number_type(parsed)
    type_names = {getattr(phonenumbers.PhoneNumberType, name): name.lower()
                  for name in ("FIXED_LINE", "MOBILE", "FIXED_LINE_OR_MOBILE", "TOLL_FREE",
                               "PREMIUM_RATE", "SHARED_COST", "VOIP", "PERSONAL_NUMBER",
                               "PAGER", "UAN", "VOICEMAIL", "UNKNOWN")}
    out({"phone": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
         "international_format": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
         "national_format": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL),
         "country_calling_code": parsed.country_code,
         "region_code": phonenumbers.region_code_for_number(parsed),
         "country": geocoder.country_name_for_number(parsed, "en"),
         "numbering_area": geocoder.description_for_number(parsed, "en"),
         "number_type": type_names.get(number_type, "unknown"),
         "possible_number": phonenumbers.is_possible_number(parsed),
         "valid_numbering_pattern": phonenumbers.is_valid_number(parsed),
         "original_carrier": carrier.name_for_number(parsed, "en"),
         "numbering_time_zones": list(timezone.time_zones_for_number(parsed)),
         "source": f"libphonenumber offline metadata via phonenumbers {phonenumbers.__version__}",
         "network_requests": False,
         "limitations": ["A valid pattern does not confirm assignment, activity, or ownership.",
                         "Numbering area and time zones are not the current device location.",
                         "The original carrier may differ after number portability."]}, args.json)


def cmd_reverse(args: argparse.Namespace) -> None:
    try:
        address = str(ipaddress.ip_address(args.address.strip()))
    except ValueError as exc:
        raise UserError("Enter a valid IPv4 or IPv6 address.") from exc
    name = ipaddress.ip_address(address).reverse_pointer
    data = get_json("https://dns.google/resolve?" + urlencode({"name": name, "type": "PTR"}), args.timeout)
    if data.get("Status") not in {0, 3}:
        raise UserError(f"Reverse DNS failed: status {data.get('Status')}.")
    answers = data.get("Answer", [])
    if not isinstance(answers, list):
        raise UserError("Unexpected reverse DNS response format.")
    out({"ip": address, "ptr": [item.get("data") for item in answers
         if isinstance(item, dict) and item.get("type") == 12]}, args.json)


def cmd_url(args: argparse.Namespace) -> None:
    value = args.url.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UserError("Invalid URL or port.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UserError("Enter an HTTP or HTTPS URL.")
    out({"scheme": parsed.scheme, "hostname": parsed.hostname,
         "port": port or (443 if parsed.scheme == "https" else 80),
         "path": parsed.path or "/", "query": parsed.query,
         "fragment": parsed.fragment, "contains_credentials": parsed.username is not None,
         "note": "Local parsing only; no network request was sent."}, args.json)


def menu_extra(handler: Any, field: str, label: str) -> None:
    value = prompt(label)
    if not value:
        print("Nothing entered. Returning to the menu.")
        return
    handler(argparse.Namespace(**{field: value, "json": False, "timeout": DEFAULT_TIMEOUT}))


def menu_telegram() -> None:
    menu_extra(cmd_telegram, "username", "Telegram username or t.me profile URL")


def menu_phone() -> None:
    menu_extra(cmd_phone, "number", "Phone in international format (+country code)")


def menu_reverse() -> None:
    menu_extra(cmd_reverse, "address", "IP address for reverse DNS")


def menu_url() -> None:
    menu_extra(cmd_url, "url", "URL to inspect without visiting it")


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        print()
        raise
    return value or default


def menu_ip() -> None:
    address = prompt("Public IP address (e.g. 8.8.8.8)")
    if not address:
        print("No IP address entered. Returning to the menu.")
        return
    cmd_ip(argparse.Namespace(address=address, json=False, timeout=DEFAULT_TIMEOUT))


def menu_domain() -> None:
    name = prompt("Domain (e.g. example.com)")
    if not name:
        print("No domain entered. Returning to the menu.")
        return
    cmd_domain(argparse.Namespace(name=name, json=False, timeout=DEFAULT_TIMEOUT))


def menu_port() -> None:
    host = prompt("Host address or domain")
    if not host:
        print("No host entered. Returning to the menu.")
        return
    ports = prompt("Ports (comma-separated or a range)", PORT_DEFAULT)
    cmd_port(argparse.Namespace(host=host, ports=ports, json=False, timeout=DEFAULT_TIMEOUT))


def menu_http() -> None:
    url = prompt("Website URL (e.g. https://example.com)")
    if not url:
        print("No website URL entered. Returning to the menu.")
        return
    cmd_http(argparse.Namespace(url=url, json=False, timeout=DEFAULT_TIMEOUT))


def menu_email() -> None:
    address = prompt("Email (e.g. name@example.com)")
    if not address:
        print("No email entered. Returning to the menu.")
        return
    cmd_email(argparse.Namespace(address=address, json=False, timeout=DEFAULT_TIMEOUT))


def menu_hash() -> None:
    algo = prompt("Algorithm: md5 / sha1 / sha256 / sha512", "sha256").lower()
    if algo not in {"md5", "sha1", "sha256", "sha512"}:
        print("Unknown algorithm. Choose md5, sha1, sha256, or sha512.")
        return
    text = input("Text to hash (spaces are preserved): ")
    cmd_hash(argparse.Namespace(algo=algo, text=text, json=False, timeout=DEFAULT_TIMEOUT))


def menu_whois() -> None:
    target = prompt("Domain for registration lookup (e.g. example.com)")
    if not target:
        print("No domain entered. Returning to the menu.")
        return
    cmd_whois(argparse.Namespace(target=target, json=False, timeout=DEFAULT_TIMEOUT))


MENU = {
    "1": ("Public IP information", menu_ip),
    "2": ("Domain DNS records", menu_domain),
    "3": ("TCP port check", menu_port),
    "4": ("Website information", menu_http),
    "5": ("Email format and mail domain check", menu_email),
    "6": ("Text hashing", menu_hash),
    "7": ("Domain registration data (RDAP)", menu_whois),
    "8": ("Telegram public username page", menu_telegram),
    "9": ("Phone number details (country, carrier, type, time zones)", menu_phone),
    "10": ("Reverse DNS lookup", menu_reverse),
    "11": ("Inspect URL components (offline)", menu_url),
    "0": ("Exit", None),
}


def menu() -> None:
    while True:
        print()
        print(f"{APP} {VERSION}")
        print("=" * 60)
        print("Choose a number, then follow the prompts.")
        for number, (title, _) in MENU.items():
            print(f"  {number}. {title}")
        print("=" * 60)
        try:
            choice = prompt("Your choice")
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0":
            return
        entry = MENU.get(choice)
        if not entry:
            print("Invalid choice. Choose one of the displayed numbers.")
            continue
        label, handler = entry
        if handler is None:
            return
        try:
            print(f"\n--- {label} ---")
            handler()
        except EOFError:
            return
        except UserError as exc:
            print(f"Error: {exc}")
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
            else:
                raise
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
        except Exception as exc:
            print(f"Execution error: {exc}")
        try:
            input("\nPress Enter to return to the menu...")
        except (EOFError, KeyboardInterrupt):
            return


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="json output")
    parser.add_argument("--timeout", type=valid_timeout, default=DEFAULT_TIMEOUT, help="timeout seconds (0–60)")


def build_parser() -> argparse.ArgumentParser:
    epilog = """examples:
  osintX.py ip 8.8.8.8
  osintX.py domain example.com
  osintX.py port --host scanme.nmap.org --ports 22,80,443
  osintX.py http https://example.com
  osintX.py email test@example.com
  osintX.py hash --algo sha256 --text hello
  osintX.py whois example.com
"""
    parser = argparse.ArgumentParser(
        prog=APP,
        description="osintX - cli osint bench",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ip = sub.add_parser("ip", help="ip geolocation")
    p_ip.add_argument("address", help="ipv4 or ipv6")
    _add_common(p_ip)
    p_ip.set_defaults(func=cmd_ip)

    p_domain = sub.add_parser("domain", help="dns records")
    p_domain.add_argument("name", help="domain name")
    _add_common(p_domain)
    p_domain.set_defaults(func=cmd_domain)

    p_port = sub.add_parser("port", help="tcp port scan")
    p_port.add_argument("--host", required=True, help="host or ip")
    p_port.add_argument("--ports", default=PORT_DEFAULT, help="ports list, e.g. 22,80,443 or 1-1024")
    _add_common(p_port)
    p_port.set_defaults(func=cmd_port)

    p_http = sub.add_parser("http", help="http status and title")
    p_http.add_argument("url", help="url or host")
    _add_common(p_http)
    p_http.set_defaults(func=cmd_http)

    p_email = sub.add_parser("email", help="email format and mx")
    p_email.add_argument("address", help="email address")
    _add_common(p_email)
    p_email.set_defaults(func=cmd_email)

    p_hash = sub.add_parser("hash", help="hash text")
    p_hash.add_argument("--algo", required=True, choices=["md5", "sha1", "sha256", "sha512"])
    p_hash.add_argument("--text", required=True, help="text to hash")
    _add_common(p_hash)
    p_hash.set_defaults(func=cmd_hash)

    p_whois = sub.add_parser("whois", help="whois lookup")
    p_whois.add_argument("target", help="domain or ip")
    _add_common(p_whois)
    p_whois.set_defaults(func=cmd_whois)

    for command, field, description, handler in (
        ("telegram", "username", "Read public Telegram page metadata", cmd_telegram),
        ("phone", "number", "Offline phone numbering metadata: country, carrier, type and time zones", cmd_phone),
        ("reverse", "address", "Reverse DNS lookup", cmd_reverse),
        ("url", "url", "Inspect URL components without visiting it", cmd_url),
    ):
        command_parser = sub.add_parser(command, help=description)
        command_parser.add_argument(field)
        _add_common(command_parser)
        command_parser.set_defaults(func=handler)

    return parser


def main() -> int:
    if len(sys.argv) == 1:
        try:
            menu()
        except KeyboardInterrupt:
            print()
            return 130
        return 0
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
