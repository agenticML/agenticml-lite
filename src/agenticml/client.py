"""Protocol v2 HTTP client for an agenticml server."""
from __future__ import annotations

import math
import os
import time

import requests
from requests.adapters import HTTPAdapter

from . import __version__
from .auth import normalize_api_key, normalize_host

_CONNECT_TIMEOUT = 10
_MIB = 1024 * 1024
_MISSING_CHUNK = 10_000
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_USER_AGENT = f"agenticml-lite/{__version__}"


class ProtocolError(ValueError):
    """The host answered, but it is not a protocol 2 agenticml server."""


class SyncStopped(Exception):
    """The caller asked the client to stop between HTTP requests."""


class Conflict(Exception):
    """A 409 from the server. ``payload`` is the parsed JSON body (dict)."""

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, dict) else {}
        error = self.payload.get("error")
        super().__init__(error if isinstance(error, str) else "conflict")


def _session():
    session = requests.Session()
    session.headers["User-Agent"] = _USER_AGENT
    adapter = HTTPAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _retry_after_seconds(response, cap=30.0):
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, cap)


def _json_object(response):
    try:
        value = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Client:
    def __init__(self, base_url=None, api_key=None, *, timeout=30,
                 max_retries=3, insecure=False, stop=None):
        resolved = base_url if base_url is not None else (os.environ.get("AGENTICML_HOST") or None)
        if not resolved:
            raise ValueError("no server host: pass base_url or set AGENTICML_HOST")
        resolved_key = api_key if api_key is not None else (os.environ.get("AGENTICML_API_KEY") or None)
        if resolved_key is not None:
            resolved_key = normalize_api_key(resolved_key)

        self.base_url = normalize_host(resolved, insecure=insecure)
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_key = resolved_key
        self.stop = stop
        self.session = _session()
        if resolved_key:
            self.session.headers["Authorization"] = f"Bearer {resolved_key}"

    def __repr__(self):
        return f"Client({self.base_url!r})"

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _backoff(self, attempt, retry_after=None):
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(0.5 * (2 ** attempt), 30.0)
        if delay:
            if self.stop is not None:
                if self.stop.wait(delay):
                    raise SyncStopped()
            else:
                time.sleep(delay)

    def _request(self, method, url, *, timeout=None, rewind=None, **kwargs):
        timeout = timeout if timeout is not None else (_CONNECT_TIMEOUT, self.timeout)
        attempts = 1 + max(self.max_retries, 0)
        last_error = None
        for attempt in range(attempts):
            if self.stop is not None and self.stop.is_set():
                raise SyncStopped()
            if rewind is not None:
                handle, start = rewind
                handle.seek(start)
            try:
                response = self.session.request(
                    method, url, allow_redirects=False, timeout=timeout, **kwargs
                )
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_error = exc
                if attempt + 1 == attempts:
                    raise
                self._backoff(attempt)
                continue
            if 300 <= response.status_code < 400:
                # Location is omitted: it would name a target that might be
                # sensitive, and the API key must not follow it.
                response.close()
                raise ValueError("server redirected; refusing to send the API key")
            if response.status_code in _RETRY_STATUSES:
                if attempt + 1 == attempts:
                    response.raise_for_status()
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response, cap=60.0)
                elif response.status_code == 503:
                    retry_after = _retry_after_seconds(response)
                else:
                    retry_after = None
                response.close()
                self._backoff(attempt, retry_after)
                continue
            return response
        raise last_error

    def _raise_http(self, response):
        response.raise_for_status()
        raise requests.HTTPError(
            f"unexpected status: {response.status_code}", response=response,
        )

    def _conflict_or_http(self, response):
        if response.status_code == 409:
            raise Conflict(_json_object(response) or {})
        if not (200 <= response.status_code < 300):
            self._raise_http(response)
        return response

    def whoami(self) -> dict:
        response = self._request("GET", f"{self.base_url}/v2/whoami")
        if (response.status_code == 401
                or response.status_code >= 500
                or response.status_code in (408, 429)):
            response.raise_for_status()
        body = _json_object(response)
        if response.status_code != 200 or body is None or body.get("protocol") != 2:
            raise ProtocolError(
                f"{self.base_url} is not an agenticml server with protocol 2"
            )
        return body

    def missing(self, hashes) -> list[str]:
        hashes = list(hashes)
        found = set()
        for start in range(0, len(hashes), _MISSING_CHUNK):
            chunk = hashes[start:start + _MISSING_CHUNK]
            response = self._request(
                "POST", f"{self.base_url}/v2/objects/missing", json={"objects": chunk},
            )
            if not (200 <= response.status_code < 300):
                self._raise_http(response)
            body = _json_object(response) or {}
            found.update(body.get("missing") or ())
        return [sha for sha in dict.fromkeys(hashes) if sha in found]

    def put_object(self, sha256, handle, size) -> bool:
        try:
            start = handle.tell()
        except (AttributeError, OSError):
            start = 0
        read_timeout = max(self.timeout, 60 + size / _MIB)
        response = self._request(
            "PUT", f"{self.base_url}/v2/objects/{sha256}",
            data=handle,
            timeout=(_CONNECT_TIMEOUT, read_timeout),
            rewind=(handle, start),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )
        if response.status_code == 201:
            return True
        if response.status_code == 200:
            return False
        self._raise_http(response)

    def run_state(self, run_id) -> dict | None:
        response = self._request("GET", f"{self.base_url}/v2/runs/{run_id}/state")
        if response.status_code == 404:
            return None
        if not (200 <= response.status_code < 300):
            self._raise_http(response)
        return response.json()

    def lineage_state(self, run_id) -> dict:
        response = self._conflict_or_http(self._request(
            "GET", f"{self.base_url}/v2/runs/{run_id}/lineage",
        ))
        return response.json()

    def put_lineage(self, run_id, handle, size) -> dict:
        response = self._conflict_or_http(self._request(
            "PUT", f"{self.base_url}/v2/runs/{run_id}/lineage", data=handle,
            timeout=(_CONNECT_TIMEOUT, max(self.timeout, 60 + size / _MIB)),
            rewind=(handle, handle.tell()),
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
        ))
        return response.json()

    def put_file(self, run_id, path, data: bytes) -> dict:
        response = self._conflict_or_http(self._request(
            "PUT", f"{self.base_url}/v2/runs/{run_id}/files/{path}", data=data,
        ))
        return response.json()

    def append(self, run_id, path, offset, data: bytes, *, truncate=False) -> int:
        query = f"offset={int(offset)}"
        if truncate:
            query += "&truncate=1"
        response = self._conflict_or_http(self._request(
            "POST",
            f"{self.base_url}/v2/runs/{run_id}/append/{path}?{query}",
            data=data,
        ))
        return response.json()["size"]

    def commit(self, run_id, status, fingerprint, sizes) -> dict:
        response = self._conflict_or_http(self._request(
            "POST", f"{self.base_url}/v2/runs/{run_id}/commit",
            json={"status": status, "fingerprint": fingerprint, "sizes": sizes},
        ))
        return response.json()
