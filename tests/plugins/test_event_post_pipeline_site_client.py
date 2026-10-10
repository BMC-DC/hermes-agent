"""Tests for the site-service client: flag-gated OFF by default, and fail-open on every kind of failure (task 07 / W03)."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from plugins.platforms.event_post_pipeline import site_client as sc
from plugins.platforms.event_post_pipeline.site_client import SiteClient, SiteClientConfig

PACK = {"date": "2026-09-02", "description": "Volunteer appreciation", "quote": "q"}


class _Fake:
    """Programmable loopback HTTP server; counts requests and records bodies."""

    def __init__(self):
        self.hits = 0
        self.bodies: list[bytes] = []
        self.mode = "ok"
        self.delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status=200, obj=None, raw=None, headers=None):
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                outer.hits += 1
                n = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(self.rfile.read(n) if n else b"")
                time.sleep(outer.delay)
                m = outer.mode
                if m == "500":
                    return self._reply(500, {"error": "internal"})
                if m == "badjson":
                    return self._reply(raw=b"<html>nope")
                if m == "list":
                    return self._reply(obj=[1, 2])
                if m == "schema":
                    return self._reply(obj={"schema": 2, "status": "ok"})
                if m == "huge":
                    return self._reply(raw=b'{"schema":1,"text":"' + b"x" * (sc.MAX_RESPONSE_BYTES + 10) + b'"}')
                if m == "redirect":
                    return self._reply(302, {}, headers={"Location": "http://127.0.0.1:1/x"})
                if m == "400":
                    return self._reply(400, {"error": "pack must be a JSON object"})
                if m == "malformed":
                    return self._reply(obj={"schema": 1, "text": "t", "data": "not-a-dict"})
                if self.path == "/v1/health":
                    return self._reply(obj={"status": "ok", "schema": 1, "corpus": {"pages": 201, "content_hash": "abc", "built_at": outer.built}, "index_built_at": outer.built})
                if self.path == "/v1/context-pack":
                    return self._reply(obj={"schema": 1, "corpus_hash": "abc", "text": "# brief", "request_id": "r1",
                                            "data": {"candidates": [{"page_id": "meditate-with-a-monk-dc"}], "missing_info": {"ready_to_draft": outer.ready}}})
                if self.path == "/v1/verify-links":
                    return self._reply(obj={"schema": 1, "corpus_hash": "abc", "text": "report", "data": {"ok": outer.verify_ok}})
                return self._reply(404, {"error": "nf"})

            do_GET = do_POST = _handle

        self.built = "2020-01-01T00:00:00Z"
        self.ready = True
        self.verify_ok = True
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    f = _Fake()
    yield f
    f.close()


def client(url, **kw):
    return SiteClient(SiteClientConfig(enabled=True, base_url=url, **kw))


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------ flag gating
def test_default_config_is_off_and_opens_no_socket(fake):
    for extra in (None, {}, {"site_service": None}, {"site_service": "yes"}, {"site_service": {}}, {"site_service": {"enabled": "true"}},
                  {"site_service": {"enabled": 1}}, {"site_service": {"url": fake.url}}):
        c = SiteClient.from_extra(extra)
        assert c.config.enabled is False
        assert c.health().available is False and c.health().reason == "disabled"
        assert c.context_pack(PACK).reason == "disabled" and c.context_pack(PACK).ready_to_draft is True
        assert c.verify_links("x").passed is None
    assert fake.hits == 0


def test_from_extra_parses_and_sanitizes():
    c = SiteClientConfig.from_extra({"site_service": {"enabled": True, "url": "http://127.0.0.1:9999/", "timeout_s": 2, "stale_after_days": 3}})
    assert (c.enabled, c.base_url, c.timeout_s, c.stale_after_days) == (True, "http://127.0.0.1:9999", 2.0, 3)
    c = SiteClientConfig.from_extra({"site_service": {"enabled": True, "timeout_s": 999, "stale_after_days": -1, "url": ""}})
    assert (c.timeout_s, c.stale_after_days, c.base_url) == (sc.DEFAULT_TIMEOUT_S, sc.DEFAULT_STALE_AFTER_DAYS, sc.DEFAULT_URL)
    assert SiteClientConfig.from_extra({"site_service": {"enabled": True, "timeout_s": True}}).timeout_s == sc.DEFAULT_TIMEOUT_S


@pytest.mark.parametrize("url", ["http://example.com:8650", "https://127.0.0.1:8650", "http://10.0.0.5:8650", "ftp://127.0.0.1", "not a url", "http://0.0.0.0:8650"])
def test_non_loopback_url_disables_client(url):
    c = client(url)
    assert c.context_pack(PACK).available is False and "loopback" in c.context_pack(PACK).reason


# ------------------------------------------------------------------ happy path
def test_health_context_pack_verify(fake):
    c = client(fake.url)
    h = c.health()
    assert h.available and h.corpus_hash == "abc" and h.stale is True
    r = c.context_pack(PACK, exclude=["blog/x"], as_of="2026-09-02", max_tokens=1500)
    assert r.available and r.text == "# brief" and r.candidate_ids == ["meditate-with-a-monk-dc"] and r.ready_to_draft is True
    sent = json.loads(fake.bodies[-1])
    assert sent == {"schema": 1, "pack": PACK, "exclude": ["blog/x"], "as_of": "2026-09-02", "max_tokens": 1500}
    v = c.verify_links("md", candidates=["a"])
    assert v.available and v.passed is True
    assert json.loads(fake.bodies[-1]) == {"schema": 1, "draft": "md", "candidates": ["a"]}
    fake.verify_ok = False
    assert c.verify_links({"blog": "x"}).passed is False
    assert "candidates" not in json.loads(fake.bodies[-1])
    assert c.verify_links("md", candidates=[]).available
    assert json.loads(fake.bodies[-1])["candidates"] == []                  # empty list = closed empty list, preserved
    fake.ready = False
    assert c.context_pack(PACK).ready_to_draft is False


def test_health_stale_flag(fake):
    fake.built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert client(fake.url).health().stale is False


def test_health_is_cached_including_failures(fake):
    c = client(fake.url)
    c.health()
    c.health()
    assert fake.hits == 1
    c.health(force=True)
    assert fake.hits == 2
    d = client(free_port_url())
    t0 = time.monotonic()
    assert d.health().available is False
    d.health()
    assert time.monotonic() - t0 < 2


# ------------------------------------------------------------------ fail open
def test_service_down_is_unavailable_not_an_exception():
    c = client(free_port_url())
    for r in (c.health(), c.context_pack(PACK), c.verify_links("x")):
        assert r.available is False and r.reason
    assert c.context_pack(PACK).ready_to_draft is True and c.verify_links("x").passed is None and c.context_pack(PACK).candidate_ids == []


@pytest.mark.parametrize("mode,reason", [("500", "http 500"), ("badjson", "invalid json"), ("list", "unexpected json shape"), ("schema", "schema mismatch"),
                                         ("huge", "response too large"), ("redirect", "http 302"), ("malformed", "malformed reply")])
def test_bad_replies_are_unavailable(fake, mode, reason):
    fake.mode = mode
    c = client(fake.url)
    for r in (c.context_pack(PACK), c.verify_links("x")):
        assert r.available is False and reason in r.reason, r.reason
        assert r.passed is None and r.ready_to_draft is True


def test_redirect_is_not_followed(fake):
    fake.mode = "redirect"
    client(fake.url).context_pack(PACK)
    assert fake.hits == 1


def test_400_keeps_the_service_message(fake):
    fake.mode = "400"
    r = client(fake.url).context_pack(PACK)
    assert r.available is False and "http 400" in r.reason and "pack must be a JSON object" in r.reason


def test_slow_service_times_out_quickly(fake):
    fake.delay = 1.5
    c = client(fake.url, timeout_s=0.3)
    t0 = time.monotonic()
    r = c.context_pack(PACK)
    assert r.available is False and r.reason == "timeout" and time.monotonic() - t0 < 1.2


def test_oversize_request_not_sent(fake):
    r = client(fake.url).context_pack({"description": "a" * (sc.MAX_REQUEST_BYTES + 1)})
    assert r.available is False and r.reason == "request too large" and fake.hits == 0


def test_unserializable_request_is_unavailable(fake):
    assert client(fake.url).verify_links({"x": object()}).reason == "request not serializable"


def test_no_request_body_in_logs(fake, caplog):
    fake.mode = "500"
    with caplog.at_level("INFO", logger=sc.logger.name):
        client(fake.url).context_pack({"description": "PRIVATE-TEXT-777"})
    assert "PRIVATE-TEXT-777" not in caplog.text and "unavailable" in caplog.text


def test_proxy_env_is_ignored(fake, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    assert client(fake.url).health().available is True
