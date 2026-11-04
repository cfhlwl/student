"""Security controls shared by all Gunicorn workers."""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import Iterable

from django.conf import settings
from django.core.cache import caches


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int


DEFAULT_RATE_LIMITS = {
    "submit": RateLimitRule(20, 60),
    "upload_start": RateLimitRule(20, 60),
    "photo_batch": RateLimitRule(180, 60),
    "document_batch": RateLimitRule(60, 60),
    "upload_status": RateLimitRule(120, 60),
    "upload_complete": RateLimitRule(20, 60),
    "supplement": RateLimitRule(20, 60),
}


def _networks(values: Iterable[str] | None = None):
    configured = values if values is not None else getattr(settings, "TRUSTED_PROXY_NETWORKS", ())
    for value in configured:
        try:
            yield ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError:
            continue


def is_trusted_proxy(peer: str | None) -> bool:
    try:
        address = ipaddress.ip_address(peer or "")
    except ValueError:
        return False
    return any(address in network for network in _networks())


def _valid_ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip_from_meta(meta: dict) -> str:
    peer = _valid_ip(meta.get("REMOTE_ADDR"))
    if peer and is_trusted_proxy(peer):
        for candidate in str(meta.get("HTTP_X_FORWARDED_FOR", "")).split(","):
            value = _valid_ip(candidate)
            if value:
                return value
        value = _valid_ip(meta.get("HTTP_X_REAL_IP"))
        if value:
            return value
    return peer or "unknown"


def classify_rate_limit(path: str, method: str) -> str | None:
    if method not in {"GET", "POST"}:
        return None
    clean = path.rstrip("/") or "/"
    if clean == "/submit":
        return "submit"
    if clean == "/submit/upload/start":
        return "upload_start"
    if clean.endswith("/photos") and "/submit/upload/" in clean:
        return "photo_batch"
    if clean.endswith("/documents") and "/submit/upload/" in clean:
        return "document_batch"
    if clean.endswith("/status") and "/submit/upload/" in clean:
        return "upload_status"
    if clean.endswith("/complete") and "/submit/upload/" in clean:
        return "upload_complete"
    if clean.startswith("/submit/result/") and clean.endswith("/supplement"):
        return "supplement"
    return None


def _configured_rule(name: str) -> RateLimitRule:
    raw = (getattr(settings, "SECURITY_RATE_LIMITS", {}) or {}).get(name, DEFAULT_RATE_LIMITS[name])
    return raw if isinstance(raw, RateLimitRule) else RateLimitRule(int(raw[0]), int(raw[1]))


def consume_rate_limit(*, name: str, client_ip: str, cache=None, now: float | None = None) -> tuple[bool, int]:
    rule = _configured_rule(name)
    if rule.limit <= 0 or rule.window_seconds <= 0:
        return False, rule.window_seconds
    cache = cache or caches["security_rate"]
    timestamp = time.time() if now is None else now
    bucket = int(timestamp // rule.window_seconds)
    key = f"student-collector:rate:{name}:{client_ip}:{bucket}"
    try:
        count = 1 if cache.add(key, 1, timeout=rule.window_seconds + 2) else cache.incr(key)
    except (ValueError, NotImplementedError):
        return False, rule.window_seconds
    remaining = max(1, rule.window_seconds - int(timestamp % rule.window_seconds))
    return count <= rule.limit, remaining
