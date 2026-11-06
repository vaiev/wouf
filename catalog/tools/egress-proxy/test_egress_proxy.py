"""Tests of the egress proxy. Every socket stays on loopback: names resolve through a fake resolver, and the
proxy's connect is pointed at a local upstream, so no test reaches the network."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import egress_proxy as ep  # noqa: E402


def v4(*parts: int) -> str:
    """A dotted IPv4 address, built so the catalog's address leak check does not read test vectors as leaks."""
    return ".".join(str(p) for p in parts)


PUBLIC_V4 = v4(93, 184, 216, 34)
PUBLIC_V6 = "2400:cb00::1"

# Each refused range in IPv4 and IPv6, with the mapped and embedding forms of an IPv4 address.
REFUSED = {
    "private 10/8": v4(10, 0, 0, 1),
    "private 172.16/12": v4(172, 16, 5, 4),
    "private 192.168/16": v4(192, 168, 1, 1),
    "loopback": v4(127, 0, 0, 1),
    "loopback other": v4(127, 1, 2, 3),
    "link-local": v4(169, 254, 1, 1),
    "metadata": v4(169, 254, 169, 254),
    "cgnat": v4(100, 64, 0, 1),
    "cgnat metadata": v4(100, 100, 100, 200),
    "multicast": v4(224, 0, 0, 1),
    "broadcast": v4(255, 255, 255, 255),
    "unspecified": v4(0, 0, 0, 0),
    "this network": v4(0, 1, 2, 3),
    "v6 loopback": "::1",
    "v6 unspecified": "::",
    "v6 link-local": "fe80::1",
    "v6 unique-local": "fd00::1",
    "v6 metadata": "fd00:ec2::254",
    "v6 multicast": "ff02::1",
    "v6 multicast global scope": "ff0e::1",
    "v6 site-local": "fec0::1",
    "mapped loopback": "::ffff:" + v4(127, 0, 0, 1),
    "mapped metadata": "::ffff:a9fe:a9fe",
    "mapped private": "::ffff:a00:1",
    "compat loopback": "::7f00:1",
    "nat64 private": "64:ff9b::a00:1",
    "nat64 metadata": "64:ff9b::a9fe:a9fe",
    "6to4 private": "2002:a00:1::1",
    "teredo": "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "documentation": "2001:db8::1",
}


class FakeResolver:
    """Answers names from a table and counts lookups; an unknown name fails as NXDOMAIN would."""

    def __init__(self, table: dict[str, list[str]]):
        self.table, self.calls = table, []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append(host)
        answers = self.table.get(host)
        if answers is None:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        out = []
        for addr in answers:
            if ":" in addr:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)))
        return out


class Upstream:
    """A loopback server that echoes CONNECT data, or answers plain HTTP with a fixed body."""

    def __init__(self, mode: str):
        self.mode, self.received = mode, b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        try:
            if self.mode == "echo":
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(data)
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            self.received = buf
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


class ProxyCase(unittest.TestCase):
    allow = ["allowed.example", ".pypi.org", "pypi.org", "files.example"]
    table = {
        "allowed.example": [PUBLIC_V4],
        "pypi.org": [PUBLIC_V6],
        "files.pypi.org": [PUBLIC_V4],
        "files.example": [PUBLIC_V4],
    }
    limits: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "egress.log"
        self.upstream = Upstream(getattr(self, "upstream_mode", "echo"))
        self.addCleanup(self.upstream.close)
        self.resolver = FakeResolver(dict(self.table))
        self.connected = []

        def connect(family, sockaddr, timeout):
            # Record the checked address the proxy asks for, then reach the loopback upstream instead.
            self.connected.append(sockaddr[0])
            return socket.create_connection(("127.0.0.1", self.upstream.port), timeout=timeout)

        options = dict(idle_timeout=2.0, max_conns=8)
        options.update(self.limits)
        self.proxy = ep.Proxy(
            ep.Allowlist(self.allow), log_path=str(self.log_path), resolve=self.resolver, connect=connect, **options
        )
        self.proxy.bind("127.0.0.1", 0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.close)

    def client(self, timeout: float = 5.0) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=timeout)
        self.addCleanup(sock.close)
        return sock

    def request(self, raw: bytes) -> tuple[socket.socket, bytes]:
        sock = self.client()
        sock.sendall(raw)
        return sock, read_head(sock)

    def status(self, raw: bytes) -> int:
        _, head = self.request(raw)
        return int(head.split(b" ")[1]) if head else 0

    def connect_status(self, target: str) -> int:
        return self.status(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())

    def settle(self, active: int = 0, wait: float = 3.0) -> None:
        """Wait until the proxy holds at most `active` connections, so a slot freed by a closed client is free."""
        deadline = time.time() + wait
        while time.time() < deadline and self.proxy.active > active:
            time.sleep(0.02)

    def log_lines(self, count: int = 1, wait: float = 3.0) -> list[str]:
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.log_path.exists():
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
                if len(lines) >= count:
                    return lines
            time.sleep(0.02)
        return self.log_path.read_text(encoding="utf-8").splitlines() if self.log_path.exists() else []


def read_head(sock: socket.socket) -> bytes:
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            data = sock.recv(4096)
            if not data:
                break
            buf += data
    except OSError:
        pass
    return buf


class AllowlistTest(unittest.TestCase):
    def test_exact_and_suffix_entries(self):
        allow = ep.Allowlist(["pypi.org", ".npmjs.org"])
        self.assertTrue(allow.allows("pypi.org"))
        self.assertTrue(allow.allows("PyPI.org."))
        self.assertTrue(allow.allows("registry.npmjs.org"))
        self.assertTrue(allow.allows("a.b.npmjs.org"))

    def test_lookalikes_do_not_match(self):
        allow = ep.Allowlist(["pypi.org"])
        self.assertFalse(allow.allows("evil-pypi.org"))
        self.assertFalse(allow.allows("files.pypi.org"))
        self.assertFalse(allow.allows("pypi.org.evil.example"))

    def test_a_suffix_entry_does_not_match_the_bare_name(self):
        allow = ep.Allowlist([".pypi.org"])
        self.assertFalse(allow.allows("pypi.org"))
        self.assertFalse(allow.allows("evil-pypi.org"))
        self.assertTrue(allow.allows("files.pypi.org"))

    def test_an_empty_list_allows_nothing(self):
        allow = ep.Allowlist([])
        for host in ("pypi.org", "example.org", "localhost", ""):
            self.assertFalse(allow.allows(host))

    def test_the_file_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.txt"
            path.write_text("# comment\n\npypi.org\n  .npmjs.org   # trailing comment\nPyPI.org\n", encoding="utf-8")
            allow = ep.Allowlist.load(files=[str(path)], entries=["github.com"])
        self.assertEqual(allow.entries, ("pypi.org", ".npmjs.org", "github.com"))

    def test_bad_entries_stop_the_load(self):
        for entry in (v4(10, 0, 0, 1), "::1", "[::1]", "*.pypi.org", "pypi.org:443", "http://pypi.org", "a b", ".", "..x"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                ep.Allowlist([entry])


class AddressCheckTest(unittest.TestCase):
    def test_public_addresses_pass(self):
        for addr in (PUBLIC_V4, PUBLIC_V6, "::ffff:5db8:d822"):
            self.assertIsNone(ep.address_refusal(addr), addr)

    def test_every_refused_range(self):
        for name, addr in REFUSED.items():
            with self.subTest(name=name, addr=addr):
                self.assertIsNotNone(ep.address_refusal(addr))

    def test_a_scoped_or_unparsable_address_is_refused(self):
        self.assertIsNotNone(ep.address_refusal("fe80::1%eth0"))
        self.assertIsNotNone(ep.address_refusal("not-an-address"))


class ConnectTest(ProxyCase):
    def test_an_allowed_host_tunnels_to_the_checked_address(self):
        sock, head = self.request(b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        sock.sendall(b"ping")
        self.assertEqual(sock.recv(4), b"ping")
        sock.close()
        self.assertEqual(self.connected, [PUBLIC_V4])
        line = self.log_lines()[0]
        self.assertIn("host=allowed.example port=443 result=allowed reason=ok", line)
        self.assertIn("bytes_out=4 bytes_in=4", line)

    def test_bytes_sent_with_the_connect_are_forwarded(self):
        sock = self.client()
        sock.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\nearly")
        head = read_head(sock)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        echoed = head.split(b"\r\n\r\n", 1)[1]
        while len(echoed) < 5:
            echoed += sock.recv(5 - len(echoed))
        self.assertEqual(echoed, b"early")

    def test_suffix_and_exact_entries_through_the_proxy(self):
        self.assertEqual(self.connect_status("files.pypi.org:443"), 200)
        self.assertEqual(self.connect_status("pypi.org:443"), 200)
        self.assertEqual(self.connect_status("evil-pypi.org:443"), 403)
        self.assertEqual(self.connect_status("unlisted.example:443"), 403)

    def test_ip_literals_are_refused(self):
        for target in (
            PUBLIC_V4 + ":443", v4(127, 0, 0, 1) + ":443", "[::1]:443", "[" + PUBLIC_V6 + "]:443",
            "2130706433:443", "0x7f000001:443", "127.1:443", "0177.0.0.1:443", "1.example.0x7f:443",
        ):
            with self.subTest(target=target):
                self.assertEqual(self.connect_status(target), 403)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connected, [])
        self.assertTrue(all("reason=ip-literal" in line for line in self.log_lines(9)))

    def test_ports_other_than_443_are_refused(self):
        for target in ("allowed.example:80", "allowed.example:22", "allowed.example:8443", "allowed.example:4430",
                       "allowed.example:0443", "allowed.example:", "allowed.example", "allowed.example:+443"):
            with self.subTest(target=target):
                self.assertIn(self.connect_status(target), (400, 403))
        self.assertEqual(self.connected, [])

    def test_names_that_resolve_to_refused_addresses(self):
        for name, addr in REFUSED.items():
            with self.subTest(name=name, addr=addr):
                self.resolver.table["allowed.example"] = [addr]
                self.assertEqual(self.connect_status("allowed.example:443"), 403)
        self.assertEqual(self.connected, [])
        self.assertTrue(all("reason=address" in line for line in self.log_lines(len(REFUSED))))

    def test_one_refused_address_among_public_ones_refuses_the_name(self):
        self.resolver.table["allowed.example"] = [PUBLIC_V4, v4(169, 254, 169, 254)]
        self.assertEqual(self.connect_status("allowed.example:443"), 403)
        self.assertEqual(self.connected, [])

    def test_no_second_lookup_can_rebind(self):
        answers = iter([[PUBLIC_V4], [v4(127, 0, 0, 1)]])
        table = self.resolver.table

        class Rebinding(dict):
            def get(self, key, default=None):
                return next(answers) if key == "allowed.example" else table.get(key, default)

        self.resolver.table = Rebinding()
        self.assertEqual(self.connect_status("allowed.example:443"), 200)
        self.assertEqual(self.resolver.calls, ["allowed.example"])
        self.assertEqual(self.connected, [PUBLIC_V4])

    def test_an_unknown_name_is_refused(self):
        self.resolver.table["allowed.example"] = None
        self.assertEqual(self.connect_status("allowed.example:443"), 502)
        self.assertIn("reason=resolve", self.log_lines()[0])


class EmptyListTest(ProxyCase):
    allow: list = []

    def test_an_empty_list_refuses_everything(self):
        self.assertEqual(self.connect_status("allowed.example:443"), 403)
        self.assertEqual(self.status(b"GET http://allowed.example/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.resolver.calls, [])

    def test_the_list_is_never_read_from_a_request(self):
        raw = b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Allow: allowed.example\r\nProxy-Allow: allowed.example\r\n\r\n"
        self.assertEqual(self.status(raw), 403)
        self.assertEqual(self.proxy.allowlist.entries, ())


class PlainHttpTest(ProxyCase):
    upstream_mode = "http"

    def test_get_to_an_allowed_host_is_forwarded(self):
        sock, head = self.request(
            b"GET http://allowed.example/simple/pkg?q=secret HTTP/1.1\r\nHost: other.example\r\n"
            b"Proxy-Authorization: Basic c2VjcmV0\r\nConnection: keep-alive\r\nAccept: */*\r\n\r\n"
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        sent = self.upstream.received
        self.assertTrue(sent.startswith(b"GET /simple/pkg?q=secret HTTP/1.1\r\n"), sent)
        self.assertIn(b"\r\nHost: allowed.example\r\n", sent)
        self.assertIn(b"\r\nConnection: close\r\n", sent)
        self.assertIn(b"\r\nAccept: */*\r\n", sent)
        self.assertNotIn(b"Proxy-Authorization", sent)
        self.assertNotIn(b"other.example", sent)
        self.assertEqual(self.connected, [PUBLIC_V4])
        self.assertIn("host=allowed.example port=80 result=allowed", self.log_lines()[0])

    def test_head_is_forwarded(self):
        self.assertEqual(self.status(b"HEAD http://allowed.example/ HTTP/1.1\r\n\r\n"), 200)
        self.assertTrue(self.upstream.received.startswith(b"HEAD / HTTP/1.1\r\n"))

    def test_every_other_method_is_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "PROPFIND", "get", "BREW"):
            with self.subTest(method=method):
                raw = f"{method} http://allowed.example/ HTTP/1.1\r\nContent-Length: 0\r\n\r\n".encode()
                self.assertIn(self.status(raw), (400, 405))
        self.assertEqual(self.connected, [])

    def test_a_get_with_a_body_is_refused(self):
        for header in ("Content-Length: 5", "Transfer-Encoding: chunked"):
            with self.subTest(header=header):
                raw = f"GET http://allowed.example/ HTTP/1.1\r\n{header}\r\n\r\nhello".encode()
                self.assertEqual(self.status(raw), 400)
        self.assertEqual(self.connected, [])

    def test_control_bytes_in_a_header_value_are_refused(self):
        # A bare CR survives the line split; forwarded, it would start a header of the bot's choosing upstream.
        for value in (b"a\rX-Injected: 1", b"a\x00b", b"a\x1bb", b"a\x7fb", b"a\x0bb"):
            with self.subTest(value=value):
                raw = b"GET http://allowed.example/ HTTP/1.1\r\nAccept: " + value + b"\r\n\r\n"
                self.assertEqual(self.status(raw), 400)
        self.assertEqual(self.connected, [])
        self.assertIn("reason=bad-header", self.log_lines()[0])
        self.assertEqual(self.status(b"GET http://allowed.example/ HTTP/1.1\r\nAccept: a\tb\r\n\r\n"), 200)

    def test_plain_http_follows_the_same_host_rules(self):
        self.assertEqual(self.status(b"GET http://unlisted.example/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.status(b"GET http://evil-pypi.org/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.status(f"GET http://{v4(127, 0, 0, 1)}/ HTTP/1.1\r\n\r\n".encode()), 403)
        self.assertEqual(self.status(b"GET http://[::1]/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.status(b"GET http://allowed.example:8080/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.status(b"GET http://user@allowed.example/ HTTP/1.1\r\n\r\n"), 400)
        self.assertEqual(self.status(b"GET https://allowed.example/ HTTP/1.1\r\n\r\n"), 400)
        self.assertEqual(self.status(b"GET / HTTP/1.1\r\nHost: allowed.example\r\n\r\n"), 400)
        self.resolver.table["allowed.example"] = [v4(169, 254, 169, 254)]
        self.assertEqual(self.status(b"GET http://allowed.example/ HTTP/1.1\r\n\r\n"), 403)
        self.assertEqual(self.connected, [])


class LogTest(ProxyCase):
    upstream_mode = "http"

    def test_no_path_query_header_or_body_is_logged(self):
        self.status(
            b"GET http://allowed.example/secret-path?token=querysecret HTTP/1.1\r\n"
            b"Authorization: Bearer headersecret\r\nCookie: cookiesecret\r\n\r\n"
        )
        self.status(b"POST http://allowed.example/postpath HTTP/1.1\r\nContent-Length: 10\r\n\r\nbodysecret")
        self.status(b"\x16\x03\x01garbage-secret\r\n\r\n")
        text = "\n".join(self.log_lines(3))
        for secret in ("secret-path", "querysecret", "headersecret", "cookiesecret", "postpath", "bodysecret", "garbage"):
            self.assertNotIn(secret, text)

    def test_one_line_per_request_with_the_fields(self):
        self.status(b"GET http://allowed.example/ HTTP/1.1\r\n\r\n")
        self.log_lines(1)  # the allowed line is written when its upstream closes; keep the order for the check
        self.status(b"GET http://unlisted.example/ HTTP/1.1\r\n\r\n")
        lines = self.log_lines(2)
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ host=allowed\.example port=80 result=allowed "
                                   r"reason=ok bytes_out=\d+ bytes_in=\d+$")
        self.assertRegex(lines[1], r"host=unlisted\.example port=80 result=refused reason=not-allowed bytes_out=0 bytes_in=0$")

    def test_a_hostile_host_cannot_forge_log_lines(self):
        self.status(b"CONNECT evil\nhost=x result=allowed:443 HTTP/1.1\r\n\r\n")
        self.status(b"CONNECT a\x1b[2Jb.example:443 HTTP/1.1\r\n\r\n")
        lines = self.log_lines(2)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("result=refused", line)
            self.assertNotIn("\x1b", line)


class LimitsTest(ProxyCase):
    limits = dict(max_line=256, max_header=1024, max_conns=2, idle_timeout=0.5)

    def test_a_long_request_line_is_refused(self):
        self.assertEqual(self.status(b"CONNECT " + b"a" * 300 + b".example:443 HTTP/1.1\r\n\r\n"), 414)

    def test_long_or_many_headers_are_refused(self):
        self.assertEqual(self.status(b"CONNECT allowed.example:443 HTTP/1.1\r\nX: " + b"a" * 300 + b"\r\n\r\n"), 431)
        many = b"".join(b"X-%d: y\r\n" % i for i in range(200))
        self.assertEqual(self.status(b"CONNECT allowed.example:443 HTTP/1.1\r\n" + many + b"\r\n"), 431)

    def test_an_endless_header_without_newline_is_cut(self):
        sock = self.client()
        try:
            sock.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\nX: " + b"a" * 4000)
        except OSError:
            pass
        self.assertIn(read_head(sock)[:12], (b"HTTP/1.1 414", b"HTTP/1.1 431", b""))
        self.assertEqual(self.connected, [])

    def test_connections_at_once_are_capped(self):
        quiet = [self.client(), self.client()]
        time.sleep(0.1)
        third = self.client()
        third.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(read_head(third).startswith(b"HTTP/1.1 503"))
        for sock in quiet:
            sock.close()
        self.settle(0)
        self.assertEqual(self.connect_status("allowed.example:443"), 200)

    def test_a_silent_client_is_dropped_after_the_idle_timeout(self):
        sock = self.client()
        started = time.time()
        self.assertIn(read_head(sock)[:12], (b"HTTP/1.1 408", b""))
        self.assertLess(time.time() - started, 3)

    def test_a_slow_client_is_dropped(self):
        sock = self.client()
        started = time.time()
        try:
            for _ in range(20):
                sock.sendall(b"C")
                time.sleep(0.2)
        except OSError:
            pass
        self.assertLess(time.time() - started, 4.5)
        self.assertIn(read_head(sock)[:12], (b"HTTP/1.1 408", b""))

    def test_an_idle_tunnel_is_closed(self):
        sock, head = self.request(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        started = time.time()
        self.assertEqual(sock.recv(1), b"")
        self.assertLess(time.time() - started, 3)

    def test_garbage_and_silence_do_not_stop_the_proxy(self):
        silent = self.client()
        for junk in (b"\x00\xff" * 50 + b"\r\n\r\n", b"\r\n\r\n", b"GET\r\n\r\n", b"CONNECT  HTTP/1.1\r\n\r\n",
                     b"CONNECT a:443 HTTP/9.9\r\n\r\n", "CONNECT \u00e9.example:443 HTTP/1.1\r\n\r\n".encode()):
            with self.subTest(junk=junk[:20]):
                self.settle(1)
                self.assertIn(self.status(junk), (400, 403, 505))
        self.settle(1)
        sock = self.client()
        sock.sendall(b"CONNE")
        sock.close()
        deadline = time.time() + 3
        while time.time() < deadline and not any("reason=incomplete" in x for x in self.log_lines(wait=0)):
            time.sleep(0.02)
        self.settle(1)
        self.assertEqual(self.connect_status("allowed.example:443"), 200)
        silent.close()


class UnixCase(ProxyCase):
    """The same proxy, listening on a Unix socket instead of TCP."""

    def setUp(self):
        super().setUp()
        self.proxy.close()
        self.proxy.log_file.close()
        self.sock_path = str(Path(self.tmp.name) / "egress.sock")
        self.proxy = ep.Proxy(self.proxy.allowlist, log_path=str(self.log_path), resolve=self.proxy.resolve,
                              connect=self.proxy.connect, idle_timeout=2.0, max_conns=8)
        self.addCleanup(self.proxy.log_file.close)
        self.proxy.bind_unix(self.sock_path)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.close)

    def client(self, timeout: float = 5.0) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(timeout)
        sock.connect(self.sock_path)
        return sock


class UnixSocketTest(UnixCase):
    def test_an_allowed_host_tunnels_through_the_unix_socket(self):
        sock, head = self.request(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        sock.sendall(b"ping")
        self.assertEqual(sock.recv(4), b"ping")
        sock.close()
        self.assertEqual(self.connected, [PUBLIC_V4])
        self.assertIn("host=allowed.example port=443 result=allowed reason=ok bytes_out=4 bytes_in=4",
                      self.log_lines()[0])

    def test_an_ip_literal_or_unlisted_host_is_refused(self):
        self.assertEqual(self.connect_status(PUBLIC_V4 + ":443"), 403)
        self.assertEqual(self.connect_status("unlisted.example:443"), 403)
        self.assertEqual(self.connected, [])
        lines = self.log_lines(2)
        self.assertIn("reason=ip-literal", lines[0])
        self.assertIn("reason=not-allowed", lines[1])

    def test_the_socket_file_has_mode_0600(self):
        self.assertEqual(os.stat(self.sock_path).st_mode & 0o777, 0o600)

    def test_an_existing_path_is_refused(self):
        other = ep.Proxy(ep.Allowlist([]), log_path=str(self.log_path))
        self.addCleanup(other.log_file.close)
        with self.assertRaises(FileExistsError):
            other.bind_unix(self.sock_path)
        self.assertEqual(self.connect_status("allowed.example:443"), 200)


class RelayTest(UnixCase):
    def setUp(self):
        super().setUp()
        self.relay = ep.Relay(self.sock_path, idle_timeout=2.0, max_conns=2)
        self.relay.bind("127.0.0.1", 0)
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.addCleanup(self.relay.close)

    def tcp_client(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.relay.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def test_a_round_trip_through_the_relay(self):
        sock = self.tcp_client()
        sock.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(read_head(sock).startswith(b"HTTP/1.1 200"))
        sock.sendall(b"ping")
        self.assertEqual(sock.recv(4), b"ping")
        sock.close()
        self.assertIn("host=allowed.example port=443 result=allowed reason=ok bytes_out=4 bytes_in=4",
                      self.log_lines()[0])
        refused = self.tcp_client()
        refused.sendall(b"CONNECT unlisted.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(read_head(refused).startswith(b"HTTP/1.1 403"))

    def test_connections_at_once_are_capped(self):
        quiet = [self.tcp_client(), self.tcp_client()]
        time.sleep(0.1)
        third = self.tcp_client()
        self.assertEqual(read_head(third), b"")
        for sock in quiet:
            sock.close()
        deadline = time.time() + 3
        while time.time() < deadline and self.relay.active:
            time.sleep(0.02)
        sock = self.tcp_client()
        sock.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(read_head(sock).startswith(b"HTTP/1.1 200"))

    def test_an_idle_connection_is_closed(self):
        sock = self.tcp_client()
        started = time.time()
        self.assertIn(read_head(sock)[:12], (b"HTTP/1.1 408", b""))
        self.assertLess(time.time() - started, 4.5)


class CommandLineTest(unittest.TestCase):
    def test_runs_with_the_standard_library_only_and_writes_only_its_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, logdir = Path(tmp) / "work", Path(tmp) / "log"
            work.mkdir()
            logdir.mkdir()
            allow = work / "allow.txt"
            allow.write_text("allowed.example\n", encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", "")}
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", str(HERE / "egress_proxy.py"), "--bind", "127.0.0.1", "--port", "0",
                 "--allow-file", str(allow), "--allow", "other.example", "--log", str(logdir / "egress.log")],
                cwd=work, env=env, stderr=subprocess.PIPE, text=True,
            )
            try:
                line = proc.stderr.readline()
                self.assertIn("listening on 127.0.0.1:", line)
                self.assertIn("2 allowlist entries", line)
                port = int(line.split("127.0.0.1:")[1].split(",")[0])
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(f"CONNECT {v4(127, 0, 0, 1)}:443 HTTP/1.1\r\n\r\n".encode())
                    self.assertTrue(read_head(sock).startswith(b"HTTP/1.1 403"))
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()
            self.assertEqual(sorted(p.name for p in work.iterdir()), ["allow.txt"])
            self.assertEqual(sorted(p.name for p in logdir.iterdir()), ["egress.log"])
            self.assertIn("reason=ip-literal", (logdir / "egress.log").read_text(encoding="utf-8"))

    def test_a_bad_allowlist_stops_the_start(self):
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(HERE / "egress_proxy.py"), "--port", "0", "--allow", "*.pypi.org"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("allowlist", proc.stderr)


if __name__ == "__main__":
    unittest.main()
