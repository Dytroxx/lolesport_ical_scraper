from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 lolesports-ical/0.1"
)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_uid(
    *,
    league_slug: str,
    match_start_utc_iso: str,
    team1: str,
    team2: str,
    stage: str | None,
    match_url: str,
) -> str:
    base = "|".join(
        [
            league_slug,
            match_start_utc_iso,
            team1.strip(),
            team2.strip(),
            (stage or "").strip(),
            match_url.strip(),
        ]
    )
    return f"{sha256_hex(base)[:32]}@lolesports"


def ensure_tzaware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def isoformat_z(dt: datetime) -> str:
    dt_utc = ensure_tzaware_utc(dt)
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RetryConfig:
    max_attempts: int = 5
    base_delay_s: float = 0.8
    max_delay_s: float = 10.0


class RateLimiter:
    def __init__(self, min_interval_s: float = 1.0) -> None:
        self.min_interval_s = float(min_interval_s)
        self._last_by_host: Dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_by_host.get(host)
        if last is None:
            self._last_by_host[host] = now
            return
        delta = now - last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last_by_host[host] = time.monotonic()


class DiskCache:
    def __init__(self, cache_dir: Path, ttl_s: int = 60 * 60) -> None:
        self.cache_dir = cache_dir
        self.ttl_s = int(ttl_s)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path_for_key(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if payload.get("v") != 2:
            return None
        ts = payload.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        if (time.time() - float(ts)) > self.ttl_s:
            return None
        return payload

    def set(self, key: str, *, status: int, headers: Dict[str, str], body: bytes) -> None:
        path = self._path_for_key(key)
        # httpx will try to decode content if Content-Encoding is present.
        # We cache the already-decoded `resp.content`, so strip encoding headers.
        headers_clean = dict(headers)
        for h in ("content-encoding", "transfer-encoding", "content-length"):
            headers_clean.pop(h, None)
        payload = {
            "v": 2,
            "ts": time.time(),
            "status": status,
            "headers": headers_clean,
            "body_b64": body.decode("latin1"),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")


class Fetcher:
    def __init__(
        self,
        *,
        cache: DiskCache,
        rate_limiter: RateLimiter,
        retry: RetryConfig | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_s: float = 20.0,
    ) -> None:
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.retry = retry or RetryConfig()
        self.client = httpx.Client(timeout=timeout_s, headers={"User-Agent": user_agent}, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def _cache_key(self, url: str, params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> str:
        params_items: Iterable[Tuple[str, Any]] = sorted((params or {}).items())
        headers_items: Iterable[Tuple[str, str]] = sorted((headers or {}).items())
        raw = json.dumps({"url": url, "params": list(params_items), "headers": list(headers_items)}, sort_keys=True)
        return sha256_hex(raw)

    def get(self, url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        host = httpx.URL(url).host or ""
        key = self._cache_key(url, params, headers)
        cached = self.cache.get(key)
        if cached is not None:
            status = int(cached.get("status", 200))
            body = str(cached.get("body_b64", "")).encode("latin1")
            resp_headers = {str(k): str(v) for k, v in (cached.get("headers") or {}).items()}
            return httpx.Response(status_code=status, headers=resp_headers, content=body, request=httpx.Request("GET", url))

        attempt = 0
        while True:
            attempt += 1
            self.rate_limiter.wait(host)
            try:
                resp = self.client.get(url, params=params, headers=headers)
            except httpx.RequestError as e:
                if attempt >= self.retry.max_attempts:
                    raise
                delay = min(self.retry.max_delay_s, self.retry.base_delay_s * (2 ** (attempt - 1)))
                delay *= random.uniform(0.8, 1.2)
                time.sleep(delay)
                continue

            if resp.status_code in (429,) or (500 <= resp.status_code <= 599):
                if attempt >= self.retry.max_attempts:
                    resp.raise_for_status()
                delay = min(self.retry.max_delay_s, self.retry.base_delay_s * (2 ** (attempt - 1)))
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                delay *= random.uniform(0.8, 1.2)
                time.sleep(delay)
                continue

            resp.raise_for_status()
            self.cache.set(key, status=resp.status_code, headers=dict(resp.headers), body=resp.content)
            return resp
