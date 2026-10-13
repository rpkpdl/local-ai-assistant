"""The SSRF guard: URL checks, DNS checks at connect time, redirects and content types.

DNS is faked with monkeypatch, so these run offline.
"""

import socket
import sqlite3

import pytest

from assistant.web.fetch import BlockedURL, Fetcher, check_url, html_to_text, resolve_public


def fake_dns(monkeypatch, *addrs):
    """Make every hostname resolve to `addrs`."""
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (a, port)) for a in addrs]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def fetcher():
    return Fetcher(sqlite3.connect(":memory:"))


def log(fetcher):
    return [row[0] for row in fetcher.db.execute("SELECT outcome FROM fetch_log")]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/",
    "gopher://example.com/",
    "http://user:pass@example.com/",
    "http://example.com:22/",
    "https://example.com:8443/",
    "http:///no-host",
])
def test_check_url_rejects(url):
    with pytest.raises(BlockedURL):
        check_url(url)


@pytest.mark.parametrize("url", ["http://example.com/", "https://example.com:443/path?q=1"])
def test_check_url_allows(url):
    check_url(url)


@pytest.mark.parametrize("addr", [
    "127.0.0.1",        # loopback
    "10.1.2.3",         # private
    "172.16.0.1",       # private
    "192.168.1.1",      # private
    "169.254.169.254",  # cloud metadata endpoint
    "100.64.0.1",       # carrier-grade NAT
    "0.0.0.0",
    "::1",
    "fd00::1",          # IPv6 unique local
])
def test_resolve_public_rejects_non_public(monkeypatch, addr):
    fake_dns(monkeypatch, addr)
    with pytest.raises(BlockedURL, match="non-public"):
        resolve_public("internal.example", 443)


def test_resolve_public_rejects_if_any_address_is_private(monkeypatch):
    fake_dns(monkeypatch, "93.184.215.14", "10.0.0.1")
    with pytest.raises(BlockedURL):
        resolve_public("mixed.example", 443)


def test_resolve_public_allows_public(monkeypatch):
    fake_dns(monkeypatch, "93.184.215.14")
    assert resolve_public("example.com", 443) == "93.184.215.14"


def test_fetch_blocks_private_ip_at_connect_time(monkeypatch, fetcher):
    """The check runs inside the connection layer, on the address actually connected to."""
    fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(BlockedURL):
        fetcher.get("http://looks-public.example/")
    assert log(fetcher) == ["blocked"]


def test_redirect_to_private_host_is_blocked(monkeypatch, fetcher):
    fake_dns(monkeypatch, "10.0.0.1")
    real_request = Fetcher._request

    def request(self, url):
        if url == "http://public.example/":
            return 302, {"location": "http://internal.example/admin"}, b""
        return real_request(self, url)  # second hop goes through the real guarded connection

    monkeypatch.setattr(Fetcher, "_request", request)
    with pytest.raises(BlockedURL):
        fetcher.get("http://public.example/")
    assert log(fetcher) == ["redirect", "blocked"]


def test_redirect_to_disallowed_scheme_is_blocked(monkeypatch, fetcher):
    monkeypatch.setattr(Fetcher, "_request",
                        lambda self, url: (302, {"location": "file:///etc/passwd"}, b""))
    with pytest.raises(BlockedURL, match="scheme"):
        fetcher.get("http://public.example/")


def test_disallowed_content_type_is_blocked(monkeypatch, fetcher):
    monkeypatch.setattr(Fetcher, "_request",
                        lambda self, url: (200, {"content-type": "image/png"}, b"\x89PNG"))
    with pytest.raises(BlockedURL, match="content-type"):
        fetcher.get("http://public.example/logo.png")


def test_allowed_page_is_cached_and_logged(monkeypatch, fetcher):
    calls = []

    def request(self, url):
        calls.append(url)
        return 200, {"content-type": "application/json"}, b'{"temp": 21}'

    monkeypatch.setattr(Fetcher, "_request", request)
    assert fetcher.get("https://api.example/weather") == '{"temp":21}'
    assert fetcher.get("https://api.example/weather") == '{"temp":21}'
    assert len(calls) == 1
    assert log(fetcher) == ["ok", "cache_hit"]


def test_html_to_text_drops_scripts_and_styles():
    html = "<html><head><title>t</title></head><body><script>steal()</script>" \
           "<style>p{}</style><p>Hello</p><p>world</p></body></html>"
    assert html_to_text(html) == "Hello\nworld"
