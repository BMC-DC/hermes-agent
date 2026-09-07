"""In-process counters + loopback GET /metrics endpoint for the Hermes gateway."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

# Upper bounds (ms); +Inf overflow slot. Total vs time-to-first-token differ.
LATENCY_BUCKETS_MS = [250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000]
RESPONSE_LATENCY_BUCKETS_MS = [50, 100, 200, 400, 800, 1500, 3000, 6000, 15000, 30000]

BOOT_ID = secrets.token_hex(8)

# Pairs past the cap fold into a shared "other/other" entry.
MAX_PROVIDER_KEYS = 200

_lock = threading.Lock()

_messages_inbound = 0
_tool_requests = 0
_tool_errors = 0

_provider_stats: dict[str, dict] = {}


def _new_buckets(bounds: list[int]) -> list[int]:
    return [0] * (len(bounds) + 1)


def _create_stat(provider: str, model: str) -> dict:
    return {
        "provider": provider,
        "model": model,
        "requests": 0,
        "errors": 0,
        "latency_ms": 0,
        "latency_buckets": _new_buckets(LATENCY_BUCKETS_MS),
        "response_latency_ms": 0,
        "response_latency_buckets": _new_buckets(RESPONSE_LATENCY_BUCKETS_MS),
    }


def _get_provider_stat(provider: str | None, model: str | None) -> dict:
    p = provider.strip() if provider and provider.strip() else "unknown"
    m = model.strip() if model and model.strip() else "unknown"
    key = f"{p}/{m}"
    existing = _provider_stats.get(key)
    if existing is not None:
        return existing
    if len(_provider_stats) >= MAX_PROVIDER_KEYS:
        overflow = _provider_stats.get("other/other")
        if overflow is None:
            overflow = _create_stat("other", "other")
            _provider_stats["other/other"] = overflow
        return overflow
    created = _create_stat(p, m)
    _provider_stats[key] = created
    return created


def _latency_bucket_index(ms: int, bounds: list[int]) -> int:
    for i, bound in enumerate(bounds):
        if ms <= bound:
            return i
    return len(bounds)


def _record_bucketed_latency(ms: float, stat: dict, sum_key: str, buckets_key: str, bounds: list[int]) -> None:
    bounded = max(0, int(ms))
    stat[sum_key] += bounded
    stat[buckets_key][_latency_bucket_index(bounded, bounds)] += 1


def record_inbound_message() -> None:
    global _messages_inbound
    with _lock:
        _messages_inbound += 1


def record_tool_request() -> None:
    global _tool_requests
    with _lock:
        _tool_requests += 1


def record_tool_error() -> None:
    global _tool_errors
    with _lock:
        _tool_errors += 1


def record_provider_request(
    provider: str | None, model: str | None, latency_ms: float, ok: bool
) -> None:
    with _lock:
        stat = _get_provider_stat(provider, model)
        _record_bucketed_latency(latency_ms, stat, "latency_ms", "latency_buckets", LATENCY_BUCKETS_MS)
        stat["requests"] += 1
        if not ok:
            stat["errors"] += 1


def record_response_latency(provider: str | None, model: str | None, latency_ms: float) -> None:
    with _lock:
        stat = _get_provider_stat(provider, model)
        _record_bucketed_latency(
            latency_ms, stat, "response_latency_ms", "response_latency_buckets", RESPONSE_LATENCY_BUCKETS_MS
        )


def get_snapshot() -> dict:
    with _lock:
        providers = [
            {
                "provider": s["provider"],
                "model": s["model"],
                "requests": s["requests"],
                "errors": s["errors"],
                "latency_ms": s["latency_ms"],
                "latency_buckets": list(s["latency_buckets"]),
                "response_latency_ms": s["response_latency_ms"],
                "response_latency_buckets": list(s["response_latency_buckets"]),
            }
            for s in _provider_stats.values()
        ]
        return {
            "boot_id": BOOT_ID,
            "collected_at": int(time.time() * 1000),
            "messages_inbound": _messages_inbound,
            "tool_requests": _tool_requests,
            "tool_errors": _tool_errors,
            "latency_buckets_ms": list(LATENCY_BUCKETS_MS),
            "response_latency_buckets_ms": list(RESPONSE_LATENCY_BUCKETS_MS),
            "providers": providers,
        }


# Unauthenticated by design: loopback bind is the trust boundary.
_server_lock = threading.Lock()
_server_started = False

DEFAULT_METRICS_HOST = "127.0.0.1"
DEFAULT_METRICS_PORT = 8643


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404, "not found")
            return
        try:
            body = json.dumps(get_snapshot()).encode("utf-8")
        except Exception:
            self.send_error(500, "snapshot failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs) -> None:
        return


def start_metrics_server(host: str | None = None, port: int | None = None) -> bool:
    global _server_started

    if os.getenv("HERMES_METRICS_DISABLED", "").lower() in {"1", "true", "yes"}:
        return False

    with _server_lock:
        if _server_started:
            return True

        bind_host = host or os.getenv("HERMES_METRICS_HOST") or DEFAULT_METRICS_HOST
        bind_port = port or _env_port() or DEFAULT_METRICS_PORT

        try:
            httpd = ThreadingHTTPServer((bind_host, bind_port), _MetricsHandler)
        except OSError as e:
            logger.warning("metrics server: bind %s:%d failed: %s", bind_host, bind_port, e)
            return False

        httpd.daemon_threads = True
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="hermes-metrics-server",
            daemon=True,
        )
        thread.start()
        _server_started = True
        logger.info("metrics server listening on http://%s:%d/metrics", bind_host, bind_port)
        return True


def _env_port() -> int | None:
    raw = os.getenv("HERMES_METRICS_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
