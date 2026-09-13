"""Shared HTTP client: retries, polite rate limiting, and an on-disk cache.

Every upstream here is a free public endpoint with no contract, so the client
assumes failure is normal: it retries transient errors, gives up quickly on
permanent ones, and lets callers degrade to a partial slate rather than crash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

CACHE_DIR = Path(os.environ.get("PARLAYBOT_CACHE", ".cache"))


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        min_interval: float = 0.15,
        cache_dir: Path | None = None,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call = 0.0

    # -- internals ---------------------------------------------------------

    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_call = time.time()

    def _cache_path(self, url: str, params: dict | None, ttl_tag: str) -> Path:
        key = hashlib.sha256(
            f"{url}|{json.dumps(params or {}, sort_keys=True)}|{ttl_tag}".encode()
        ).hexdigest()[:32]
        return self.cache_dir / f"{key}.json"

    # -- public ------------------------------------------------------------

    def get_json(
        self,
        url: str,
        params: dict | None = None,
        *,
        headers: dict | None = None,
        cache_ttl: float = 0.0,
        ttl_tag: str = "",
    ) -> Any | None:
        """GET JSON with optional disk caching. Returns None on permanent failure."""
        path = self._cache_path(url, params, ttl_tag)
        if cache_ttl > 0 and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < cache_ttl:
                try:
                    return json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    pass

        data = self._request_json(url, params, headers)
        if data is not None and cache_ttl > 0:
            try:
                path.write_text(json.dumps(data))
            except OSError:
                log.debug("cache write failed for %s", url)
        return data

    def _request_json(
        self, url: str, params: dict | None, headers: dict | None
    ) -> Any | None:
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("request error %s (%s/%s): %s", url, attempt + 1,
                            self.retries, exc)
                time.sleep(1.5 * (attempt + 1) + random.random())
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("non-JSON body from %s", url)
                    return None

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2.0 * (attempt + 1) + random.random()
                log.warning("HTTP %s from %s, retrying in %.1fs",
                            resp.status_code, url, wait)
                time.sleep(wait)
                continue

            log.warning("HTTP %s from %s, giving up", resp.status_code, url)
            return None

        return None

    def get_text(self, url: str, *, cache_ttl: float = 0.0) -> str | None:
        path = self._cache_path(url, None, "text")
        if cache_ttl > 0 and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < cache_ttl:
                try:
                    return path.read_text()
                except OSError:
                    pass

        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("request error %s: %s", url, exc)
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                if cache_ttl > 0:
                    try:
                        path.write_text(resp.text)
                    except OSError:
                        pass
                return resp.text
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            return None
        return None

    def first_ok(self, urls: list[str], *, cache_ttl: float = 0.0) -> str | None:
        """Try candidate URLs in order, return the first body that comes back.

        Upstream file-naming conventions drift between seasons; this keeps a
        rename from taking the whole sport offline.
        """
        for url in urls:
            body = self.get_text(url, cache_ttl=cache_ttl)
            if body:
                log.info("using %s", url)
                return body
        return None
