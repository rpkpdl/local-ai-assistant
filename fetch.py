"""Guarded HTTP fetcher with a local page cache and an audit log.

Security properties:
  - http/https only, ports 80/443 only, no credentials in URLs
  - every hostname is resolved and *all* its addresses must be public; the
    connection is then made to that checked address (no DNS-rebinding gap).
    This runs inside the connection layer, so redirects are checked too.
  - redirects are followed manually (max 5), each hop re-validated and logged
  - no cookies, no auth headers, environment proxy settings ignored
  - response size, time and content-type are capped
  - every attempt (allowed or blocked) is written to `fetch_log`
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sqlite3
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpcore

MAX_BYTES = 2_000_000
MAX_TEXT_CHARS = 20_000
MAX_REDIRECTS = 5
TIMEOUT = 15
ALLOWED_TYPES = ("text/html", "text/plain", "application/json", "application/xml", "text/xml")
USER_AGENT = "personal-assistant/0.1 (+local)"


class BlockedURL(Exception):
    pass


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL not allowed")
    if not parts.hostname:
        raise BlockedURL("no hostname")
    if parts.port not in (None, 80, 443):
        raise BlockedURL(f"port {parts.port} not allowed")


def resolve_public(host: str, port: int) -> str:
    """Resolve host; refuse if ANY address is non-public. Returns one address."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise BlockedURL(f"cannot resolve {host}: {e}") from e
    addrs = {info[4][0] for info in infos}
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            raise BlockedURL(f"{host} resolves to non-public address {a}")
    return sorted(addrs)[0]


class GuardedBackend(httpcore.SyncBackend):
    """Validates the destination at connect time and connects to the checked IP.
    TLS still verifies the certificate against the original hostname."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = resolve_public(host, port)
        return super().connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                   socket_options=socket_options)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    lines = (line.strip() for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


class Fetcher:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.pool = httpcore.ConnectionPool(network_backend=GuardedBackend(), retries=0)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY, fetched_at REAL, status INTEGER,
                content_type TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS fetch_log (
                ts REAL, url TEXT, outcome TEXT, detail TEXT);
        """)

    def _log(self, url: str, outcome: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO fetch_log VALUES (?, ?, ?, ?)",
                        (time.time(), url, outcome, detail[:500]))
        self.db.commit()

    def cached(self, url: str, max_age_s: float) -> tuple[str, float] | None:
        row = self.db.execute(
            "SELECT text, fetched_at FROM pages WHERE url = ? AND status = 200", (url,)
        ).fetchone()
        if row and time.time() - row[1] <= max_age_s:
            return row
        return None

    def get(self, url: str, max_age_s: float = 1800) -> str:
        """Fetch url (or return a fresh-enough cached copy) as text.
        Raises BlockedURL or RuntimeError on failure."""
        if hit := self.cached(url, max_age_s):
            self._log(url, "cache_hit")
            return hit[0]

        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                check_url(current)
                status, headers, body = self._request(current)
            except BlockedURL as e:
                self._log(current, "blocked", str(e))
                raise
            except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as e:
                self._log(current, "error", repr(e))
                raise RuntimeError(f"network error: {e!r}") from e

            if status in (301, 302, 303, 307, 308) and "location" in headers:
                nxt = urljoin(current, headers["location"])
                self._log(current, "redirect", nxt)
                current = nxt
                continue
            break
        else:
            self._log(url, "error", "too many redirects")
            raise RuntimeError("too many redirects")

        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if status != 200:
            self._log(current, "http_error", str(status))
            raise RuntimeError(f"HTTP {status}")
        if ctype not in ALLOWED_TYPES:
            self._log(current, "blocked", f"content-type {ctype}")
            raise BlockedURL(f"content-type {ctype!r} not allowed")

        raw = body.decode(_charset(headers.get("content-type", "")), errors="replace")
        if ctype == "text/html":
            text = html_to_text(raw)
        elif ctype == "application/json":
            try:
                text = json.dumps(json.loads(raw), separators=(",", ":"))
            except ValueError:
                text = raw
        else:
            text = raw
        text = text[:MAX_TEXT_CHARS]

        self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
                        (url, time.time(), status, ctype, text))
        self._log(current, "ok", f"{ctype} {len(body)} bytes")
        return text

    def _request(self, url: str) -> tuple[int, dict[str, str], bytes]:
        headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(ALLOWED_TYPES)}
        with self.pool.stream("GET", url, headers=headers,
                              extensions={"timeout": {"connect": TIMEOUT, "read": TIMEOUT,
                                                      "write": TIMEOUT, "pool": TIMEOUT}}) as resp:
            hdrs = {k.decode().lower(): v.decode("latin-1") for k, v in resp.headers}
            chunks, size = [], 0
            for chunk in resp.iter_stream():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise BlockedURL(f"response larger than {MAX_BYTES} bytes")
                chunks.append(chunk)
            return resp.status, hdrs, b"".join(chunks)


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            try:
                "".encode(v.strip('"'))
                return v.strip('"')
            except LookupError:
                pass
    return "utf-8"
