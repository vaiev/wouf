#!/usr/bin/env python3
"""An egress allowlist proxy for bot containers: HTTP CONNECT to port 443 and plain GET/HEAD to port 80, only for
host names on an allowlist, and only to public addresses. Standard library only, Python 3.9+.

Usage: egress_proxy.py [--bind ADDR] [--port 3128] [--unix PATH] [--allow-file FILE]... [--allow HOST]... [--log FILE]
       egress_proxy.py --relay PATH [--bind ADDR] [--port 3128]
See README.md for the allowlist format and the log line."""

from __future__ import annotations

import argparse
import ipaddress
import re
import os
import selectors
import signal
import socket
import sys
import threading
import time
from typing import Callable, Iterable, Optional

LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(rf"(?=.{{1,253}}$){LABEL}(?:\.{LABEL})*", re.ASCII)
LOGGABLE = re.compile(r"[a-z0-9.:\[\]-]{1,255}", re.ASCII)
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)
HEAD_END = re.compile(rb"\r?\n\r?\n")
# 100.64/10 (CGNAT, RFC 6598) and 64:ff9b::/96 (NAT64, RFC 6052), built from integers on purpose.
CGNAT = ipaddress.IPv4Network((0x64400000, 10))
NAT64 = ipaddress.IPv6Network(("64:ff9b::", 96))
NAT64_LOCAL = ipaddress.IPv6Network(("64:ff9b:1::", 48))
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te",
              "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
PHRASES = {200: "OK", 400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 408: "Request Timeout",
           414: "URI Too Long", 431: "Request Header Fields Too Large", 502: "Bad Gateway",
           503: "Service Unavailable", 505: "HTTP Version Not Supported"}


def normalize(host: str) -> str:
    host = host.lower()
    return host[:-1] if host.endswith(".") else host


def is_ip_literal(host: str) -> bool:
    """True for anything a resolver could read as an address: IPv6, dotted, integer, octal or hex forms."""
    if host.startswith("[") or ":" in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    last = normalize(host).rsplit(".", 1)[-1]
    if last[:1].isdigit():
        return True
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError):
        return False


def is_hostname(host: str) -> bool:
    return bool(HOSTNAME.fullmatch(normalize(host))) and not is_ip_literal(host)


def _refusal(ip) -> Optional[str]:
    if ip.version == 6:
        if ip.ipv4_mapped:
            return _refusal(ip.ipv4_mapped)
        if ip in NAT64:
            return _refusal(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour or ip.teredo or ip in NAT64_LOCAL:
            return "address-tunnel"
        if ip.is_site_local:
            return "address-private"
    if ip.is_multicast:
        return "address-multicast"
    if ip.is_loopback:
        return "address-loopback"
    if ip.is_link_local:
        return "address-link-local"
    if ip.version == 4 and ip in CGNAT:
        return "address-cgnat"
    if ip.is_unspecified or ip.is_reserved:
        return "address-reserved"
    if ip.is_private or not ip.is_global:
        return "address-private"
    return None


def address_refusal(address: str) -> Optional[str]:
    """None for a public unicast address, else the reason it is refused. Mapped and embedded IPv4 is unwrapped."""
    if "%" in address:
        return "address-scoped"
    try:
        return _refusal(ipaddress.ip_address(address))
    except ValueError:
        return "address-invalid"


class Allowlist:
    """Host names matched exactly (`pypi.org`) or by suffix (`.pypi.org` matches `files.pypi.org`, not `pypi.org`)."""

    def __init__(self, entries: Iterable[str]):
        seen = []
        for raw in entries:
            entry = normalize(raw.strip())
            name = entry[1:] if entry.startswith(".") else entry
            if not is_hostname(name):
                raise ValueError(f"allowlist: not a host name or .suffix: {raw.strip()!r}")
            if entry not in seen:
                seen.append(entry)
        self.entries = tuple(seen)

    @classmethod
    def load(cls, files: Iterable[str] = (), entries: Iterable[str] = ()) -> "Allowlist":
        found = []
        for path in files:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        found.append(line)
        return cls(found + list(entries))

    def allows(self, host: str) -> bool:
        if not is_hostname(host):
            return False
        host = normalize(host)
        return any(host.endswith(e) if e.startswith(".") else host == e for e in self.entries)


class Refuse(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def listen_tcp(host: str, port: int) -> socket.socket:
    host = "" if host == "*" else host
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.settimeout(0.5)
    return sock


def listen_unix(path: str) -> socket.socket:
    """A Unix socket at a new path, mode 0600 from the start; an existing path (a stale socket too) is refused."""
    if os.path.lexists(path):
        raise FileExistsError(f"{path} exists")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    except OSError:
        sock.close()
        raise
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(128)
    sock.settimeout(0.5)
    return sock


def default_connect(family: int, sockaddr: tuple, timeout: float) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class Proxy:
    def __init__(self, allowlist: Allowlist, log_path: Optional[str] = None, resolve: Callable = socket.getaddrinfo,
                 connect: Callable = default_connect, idle_timeout: float = 60.0, connect_timeout: float = 10.0,
                 max_conns: int = 64, max_line: int = 8192, max_header: int = 32768, max_headers: int = 100):
        self.allowlist, self.resolve, self.connect = allowlist, resolve, connect
        self.idle_timeout, self.connect_timeout = idle_timeout, connect_timeout
        self.max_conns, self.max_line, self.max_header, self.max_headers = max_conns, max_line, max_header, max_headers
        self.log_file = open(log_path, "a", encoding="utf-8") if log_path else sys.stderr
        self.active, self.port, self.sock, self.unix_path = 0, 0, None, None
        self._lock, self._log_lock = threading.Lock(), threading.Lock()
        self._busy = threading.BoundedSemaphore(max(1, max_conns))
        self._closed = False

    # Server

    def bind(self, host: str, port: int) -> None:
        self.sock = listen_tcp(host, port)
        self.port = self.sock.getsockname()[1]

    def bind_unix(self, path: str) -> None:
        self.sock = listen_unix(path)
        self.unix_path = path

    def serve_forever(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed:
                    return
                time.sleep(0.1)  # out of file descriptors or a transient error: keep serving
                continue
            with self._lock:
                admitted = self.active < self.max_conns
                if admitted:
                    self.active += 1
            target = self._handle if admitted else self._refuse_busy
            if admitted or self._busy.acquire(blocking=False):
                threading.Thread(target=target, args=(conn,), daemon=True).start()
            else:
                conn.close()

    def close(self) -> None:
        self._closed = True
        if self.sock:
            self.sock.close()
        if self.unix_path:
            try:
                os.unlink(self.unix_path)
            except OSError:
                pass
            self.unix_path = None

    # One connection

    def _refuse_busy(self, conn: socket.socket) -> None:
        try:
            self._log({"host": "-", "port": "-", "result": "refused", "reason": "busy", "out": 0, "in": 0})
            self._reply(conn, 503)
        finally:
            conn.close()
            self._busy.release()

    def _handle(self, conn: socket.socket) -> None:
        rec = {"host": "-", "port": "-", "result": "refused", "reason": "error", "out": 0, "in": 0}
        upstream, status = [], 0
        try:
            conn.settimeout(self.idle_timeout)
            method, target, headers, rest = self._read_request(conn)
            if method == "CONNECT":
                self._open_tunnel(conn, target, rest, rec, upstream)
            else:
                self._forward_http(conn, method, target, headers, rec, upstream)
        except Refuse as refused:
            rec["reason"], status = refused.reason, refused.status
        except OSError:
            pass  # a reset or timeout ends the tunnel; the record keeps its reason
        finally:
            for sock in upstream:
                sock.close()
            self._log(rec)  # before the answer, so the line is written when the client reads it
            if status:
                self._reply(conn, status)
            conn.close()
            with self._lock:
                self.active -= 1

    def _read_request(self, conn: socket.socket):
        buf, deadline = b"", time.monotonic() + self.idle_timeout
        while True:
            end = HEAD_END.search(buf)
            if end:
                break
            if b"\n" not in buf and len(buf) > self.max_line:
                raise Refuse(414, "line-too-long")
            if len(buf) > self.max_line + self.max_header:
                raise Refuse(431, "header-too-large")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Refuse(408, "timeout")
            conn.settimeout(min(self.idle_timeout, left))
            try:
                data = conn.recv(4096)
            except socket.timeout:
                raise Refuse(408, "timeout") from None
            if not data:
                raise Refuse(0, "closed" if not buf else "incomplete")
            buf += data
        conn.settimeout(self.idle_timeout)
        head, rest = buf[:end.start()], buf[end.end():]
        lines = [line.rstrip(b"\r") for line in head.split(b"\n")]
        if len(lines[0]) > self.max_line:
            raise Refuse(414, "line-too-long")
        if len(lines) - 1 > self.max_headers or sum(len(x) + 2 for x in lines[1:]) > self.max_header or \
                any(len(x) > self.max_line for x in lines[1:]):
            raise Refuse(431, "header-too-large")
        try:
            text = [line.decode("ascii") for line in lines]
        except UnicodeDecodeError:
            raise Refuse(400, "bad-request") from None
        parts = text[0].split(" ")
        if len(parts) != 3 or not TOKEN.fullmatch(parts[0]) or not parts[1] or \
                any(ord(c) < 0x21 or ord(c) == 0x7F for c in parts[1]):
            raise Refuse(400, "bad-request")
        if parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
            raise Refuse(505 if parts[2].startswith("HTTP/") else 400, "bad-version")
        headers = []
        for line in text[1:]:
            name, sep, value = line.partition(":")
            if not sep or not TOKEN.fullmatch(name):
                raise Refuse(400, "bad-header")
            headers.append((name, value.strip()))
        return parts[0], parts[1], headers, rest

    def _check_host(self, host: str, port: str, want: str, rec: dict) -> None:
        rec["host"] = normalize(host) if LOGGABLE.fullmatch(normalize(host)) else "-"
        rec["port"] = port if port.isdigit() and len(port) <= 5 else "-"
        if is_ip_literal(host):
            raise Refuse(403, "ip-literal")
        if port != want:
            raise Refuse(403, "port")
        if not is_hostname(host):
            raise Refuse(400, "bad-host")
        if not self.allowlist.allows(host):
            raise Refuse(403, "not-allowed")

    def _dial(self, host: str, port: int) -> socket.socket:
        """Resolve once, refuse if any answer is not public, and connect to exactly the checked addresses."""
        try:
            infos = self.resolve(normalize(host), port, 0, socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise Refuse(502, "resolve") from None
        if not infos:
            raise Refuse(502, "resolve")
        for info in infos:
            reason = address_refusal(str(info[4][0]))
            if reason:
                raise Refuse(403, reason)
        for family, _, _, _, sockaddr in infos:
            try:
                upstream = self.connect(family, sockaddr, self.connect_timeout)
                upstream.settimeout(self.idle_timeout)
                return upstream
            except OSError:
                continue
        raise Refuse(502, "upstream")

    def _open_tunnel(self, conn: socket.socket, target: str, rest: bytes, rec: dict, opened: list) -> None:
        host, sep, port = target.rpartition(":")
        if not sep:
            host, port = target, ""
        self._check_host(host, port, "443", rec)
        upstream = self._dial(host, 443)
        opened.append(upstream)
        rec.update(result="allowed", reason="ok")
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
            rec["out"] += len(rest)
        self._relay(conn, upstream, rec, both=True)

    def _forward_http(self, conn, method: str, target: str, headers: list, rec: dict, opened: list) -> None:
        if not target.lower().startswith("http://"):
            raise Refuse(400, "bad-target")
        rest = target[7:]
        cut = min([i for i in (rest.find(c) for c in "/?#") if i >= 0] or [len(rest)])
        authority, path = rest[:cut], rest[cut:].split("#", 1)[0] or "/"
        path = "/" + path if path.startswith("?") else path
        if "@" in authority:
            raise Refuse(400, "userinfo")
        host, sep, port = authority.rpartition(":")
        if not sep or authority.endswith("]"):
            host, port = authority, "80"
        if method not in ("GET", "HEAD"):
            rec["host"] = normalize(host) if LOGGABLE.fullmatch(normalize(host)) else "-"
            raise Refuse(405, "method")
        self._check_host(host, port, "80", rec)
        # A control byte in a value (a bare CR survives the line split) would end the header upstream; tab is allowed.
        if any(ord(c) < 0x20 and c != "\t" or ord(c) == 0x7F for _, value in headers for c in value):
            raise Refuse(400, "bad-header")
        names = {name.lower() for name, _ in headers}
        length = [v for n, v in headers if n.lower() == "content-length"]
        if "transfer-encoding" in names or any(v != "0" for v in length):
            raise Refuse(400, "body")
        listed = {t.strip().lower() for n, v in headers if n.lower() == "connection" for t in v.split(",")}
        kept = [f"{n}: {v}\r\n" for n, v in headers if n.lower() not in HOP_BY_HOP | listed]
        request = (f"{method} {path} HTTP/1.1\r\nHost: {normalize(host)}\r\n" + "".join(kept) +
                   "Connection: close\r\n\r\n").encode("ascii")
        upstream = self._dial(host, 80)
        opened.append(upstream)
        rec.update(result="allowed", reason="ok")
        upstream.sendall(request)
        rec["out"] += len(request)
        self._relay(conn, upstream, rec, both=False)  # no later client bytes reach upstream: no pipelined smuggling

    def _relay(self, conn: socket.socket, upstream: socket.socket, rec: dict, both: bool) -> None:
        selector = selectors.DefaultSelector()
        selector.register(upstream, selectors.EVENT_READ, (conn, "in"))
        if both:
            selector.register(conn, selectors.EVENT_READ, (upstream, "out"))
        try:
            while True:
                events = selector.select(self.idle_timeout)
                if not events:
                    return  # idle
                for key, _ in events:
                    other, direction = key.data
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
                    rec[direction] += len(data)
        finally:
            selector.close()

    def _reply(self, conn: socket.socket, status: int) -> None:
        """Send the status, then drop what the client already sent, so it reads the answer instead of a reset."""
        try:
            conn.settimeout(1.0)
            conn.sendall(f"HTTP/1.1 {status} {PHRASES[status]}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                         .encode("ascii"))
            conn.shutdown(socket.SHUT_WR)
            conn.setblocking(False)
            for _ in range(16):
                if not conn.recv(65536):
                    break
        except OSError:
            pass

    def _log(self, rec: dict) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = (f"{stamp} host={rec['host']} port={rec['port']} result={rec['result']} reason={rec['reason']} "
                f"bytes_out={rec['out']} bytes_in={rec['in']}\n")
        with self._log_lock:
            try:
                self.log_file.write(line)
                self.log_file.flush()
            except (OSError, ValueError):
                pass


class Relay:
    """Pipes each TCP connection's bytes both ways to a Unix socket: no parsing, no log, the same limits."""

    def __init__(self, target: str, idle_timeout: float = 60.0, connect_timeout: float = 10.0, max_conns: int = 64):
        self.target, self.idle_timeout, self.connect_timeout, self.max_conns = target, idle_timeout, connect_timeout, \
            max_conns
        self.active, self.port, self.sock = 0, 0, None
        self._lock = threading.Lock()
        self._closed = False

    def bind(self, host: str, port: int) -> None:
        self.sock = listen_tcp(host, port)
        self.port = self.sock.getsockname()[1]

    def serve_forever(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed:
                    return
                time.sleep(0.1)
                continue
            with self._lock:
                admitted = self.active < self.max_conns
                if admitted:
                    self.active += 1
            if admitted:
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            else:
                conn.close()

    def close(self) -> None:
        self._closed = True
        if self.sock:
            self.sock.close()

    def _handle(self, conn: socket.socket) -> None:
        target = None
        try:
            target = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target.settimeout(self.connect_timeout)
            target.connect(self.target)
            self._pipe(conn, target)
        except OSError:
            pass
        finally:
            if target:
                target.close()
            conn.close()
            with self._lock:
                self.active -= 1

    def _pipe(self, conn: socket.socket, target: socket.socket) -> None:
        """Both directions until both ends are done or nothing moves for the idle timeout; an EOF is passed on."""
        conn.settimeout(self.idle_timeout)
        target.settimeout(self.idle_timeout)
        selector = selectors.DefaultSelector()
        selector.register(conn, selectors.EVENT_READ, target)
        selector.register(target, selectors.EVENT_READ, conn)
        open_ends = 2
        try:
            while open_ends:
                events = selector.select(self.idle_timeout)
                if not events:
                    return
                for key, _ in events:
                    data = key.fileobj.recv(65536)
                    if data:
                        key.data.sendall(data)
                        continue
                    selector.unregister(key.fileobj)
                    open_ends -= 1
                    try:
                        key.data.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
        finally:
            selector.close()


def serve(server, banner: str) -> int:
    signal.signal(signal.SIGTERM, lambda *_: server.close())
    print(f"egress-proxy: {banner}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.close()
    return 0


def run_relay(args) -> int:
    relay = Relay(args.relay, idle_timeout=args.idle_timeout, connect_timeout=args.connect_timeout,
                  max_conns=args.max_conns)
    relay.bind(args.bind, args.port)
    return serve(relay, f"relaying {args.bind}:{relay.port} to unix:{args.relay}")


def run_proxy(args) -> int:
    try:
        allowlist = Allowlist.load(args.allow_file, args.allow)
    except (OSError, ValueError) as error:
        print(f"egress-proxy: allowlist: {error}", file=sys.stderr)
        return 2
    proxy = Proxy(allowlist, log_path=args.log, idle_timeout=args.idle_timeout, connect_timeout=args.connect_timeout,
                  max_conns=args.max_conns, max_line=args.max_line, max_header=args.max_header)
    if args.unix:
        try:
            proxy.bind_unix(args.unix)
        except OSError as error:
            print(f"egress-proxy: --unix: {error}, refusing to use it", file=sys.stderr)
            return 2
        where = f"unix:{args.unix}"
    else:
        proxy.bind(args.bind, args.port)
        where = f"{args.bind}:{proxy.port}"
    return serve(proxy, f"listening on {where}, {len(allowlist.entries)} allowlist entries")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="egress-proxy", description=__doc__.split("\n\n")[0])
    parser.add_argument("--bind", default="127.0.0.1", help="address to listen on, '*' for all IPv4 (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--unix", metavar="PATH", help="listen on a new Unix socket at PATH (mode 0600) instead of TCP")
    parser.add_argument("--relay", metavar="PATH",
                        help="relay mode: pipe each TCP connection on --bind/--port to the Unix socket at PATH")
    parser.add_argument("--allow-file", action="append", default=[], help="allowlist file, may repeat")
    parser.add_argument("--allow", action="append", default=[], help="allowlist entry, may repeat")
    parser.add_argument("--log", help="log file, appended (default standard error)")
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--max-conns", type=int, default=64)
    parser.add_argument("--max-line", type=int, default=8192)
    parser.add_argument("--max-header", type=int, default=32768)
    args = parser.parse_args(argv)
    if args.unix and args.relay:
        parser.error("--unix and --relay exclude each other")
    return run_relay(args) if args.relay else run_proxy(args)


if __name__ == "__main__":
    sys.exit(main())
