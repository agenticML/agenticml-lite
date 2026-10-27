from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .auth import normalize_api_key, normalize_host

logger = logging.getLogger("agenticml.client")

DEFAULT_BASE_URL = "https://api.agenticml.xyz"


def _idempotent_retry(max_retries):
    return Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=0.5,
        backoff_max=30.0,
        respect_retry_after_header=False,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )


def _connect_retry(max_retries):
    # urllib3 Retry(connect=N, read=0, status=0, other=0): only a failure
    # that happened before the request was sent (ConnectTimeoutError,
    # including NewConnectionError) is retried.
    return Retry(
        total=max_retries,
        connect=max_retries,
        read=0,
        status=0,
        other=0,
        backoff_factor=0.5,
        backoff_max=30.0,
        respect_retry_after_header=False,
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )


def _upload_source(file_or_data, filename, default_name):
    """Normalize a path, bytes, or binary handle to ``(payload, filename)``.

    A path is returned as a ``Path`` so the caller can open it for the
    duration of the upload. Bytes are copied once. A handle is rewound when
    it supports ``seek``.
    """
    if isinstance(file_or_data, (str, Path)):
        path = Path(file_or_data)
        return path, filename or path.name
    if isinstance(file_or_data, (bytes, bytearray, memoryview)):
        return bytes(file_or_data), filename or default_name
    if hasattr(file_or_data, "seek"):
        try:
            file_or_data.seek(0)
        except (AttributeError, OSError):
            pass
    resolved = filename or getattr(file_or_data, "name", None)
    if isinstance(resolved, (str, Path)):
        resolved = Path(resolved).name
    else:
        resolved = default_name
    return file_or_data, resolved


def _session(api_key, retry):
    session = requests.Session()
    if api_key:
        session.headers["x-api-key"] = api_key
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class Client:
    def __init__(self, base_url=None, timeout=5, max_retries=3, api_key=None, insecure=False):
        resolved_base = base_url or os.environ.get("AGENTICML_HOST") or DEFAULT_BASE_URL
        resolved_key = api_key if api_key is not None else os.environ.get("AGENTICML_API_KEY")

        self.base_url = normalize_host(resolved_base, insecure=insecure)
        self.timeout = timeout
        if resolved_key is not None:
            resolved_key = normalize_api_key(resolved_key)
        self.api_key = resolved_key
        self.session = _session(resolved_key, _idempotent_retry(max_retries))
        # /init, /log_media, register-version, and /finalize are not idempotent.
        self._once = _session(resolved_key, _connect_retry(max_retries))

    def close(self):
        self.session.close()
        self._once.close()

    def _send(self, session, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        response = session.request(method, url, allow_redirects=False, **kwargs)
        if 300 <= response.status_code < 400:
            # The Location header is not included: it would echo a redirect target
            # that might itself be sensitive, and the API key must not follow it.
            raise ValueError("server redirected; refusing to send the API key")
        return response

    def check_auth(self):
        res = self._send(
            self.session, "GET", f"{self.base_url}/runs", params={"limit": 1},
        )
        if res.status_code in (401, 403):
            return False
        res.raise_for_status()
        if res.status_code != 200:
            raise ValueError(f"unexpected authentication status: {res.status_code}")
        return True

    def update_config(self, run_id, *, config=None, tags=None, notes=None,
                      group=None, job_type=None):
        # server: PATCH /config cannot delete keys or null group/job_type
        # (COALESCE keeps the stored value), so None fields are omitted.
        payload = {key: value for key, value in {
            "config": config, "tags": tags, "notes": notes,
            "group": group, "job_type": job_type,
        }.items() if value is not None}
        res = self._send(
            self.session, "PATCH", f"{self.base_url}/runs/{run_id}/config", json=payload,
        )
        res.raise_for_status()

    def init_run(self, payload):
        res = self._send(
            self._once, "POST", f"{self.base_url}/init", json=payload,
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()
        body = res.json()
        return body["run_id"]

    def log(self, payload):
        res = self._send(self.session, "POST", f"{self.base_url}/log", json=payload)
        res.raise_for_status()

    def log_batch(self, items):
        if not items:
            return
        res = self._send(
            self.session, "POST", f"{self.base_url}/log/batch",
            json={"items": items}, timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

    def finalize(self, run_id, summary, status="finished"):
        # server: finished_at is stamped by the server. FinalizeRequest accepts
        # summary and status only; snapshot_hash belongs on /init.
        res = self._send(
            self._once, "POST", f"{self.base_url}/runs/{run_id}/finalize",
            json={"summary": summary, "status": status},
            timeout=max(self.timeout, 10),
        )
        res.raise_for_status()

    def artifacts_exists(self, sha256s):
        if not sha256s:
            return []
        res = self._send(
            self.session, "POST", f"{self.base_url}/artifacts/exists",
            json={"sha256s": list(sha256s)},
        )
        res.raise_for_status()
        return res.json().get("existing", [])

    def upload_code(self, run_id, files):
        """Upload missing code bytes to object storage and register every file."""
        if not files:
            return {"status": "ok", "registered": []}

        already_stored: set[str] = set()
        try:
            already_stored = set(self.artifacts_exists([item["sha256"] for item in files]))
            if already_stored:
                logger.info(
                    "code dedup: %d/%d files already stored, sending metadata only",
                    len(already_stored),
                    len(files),
                )
        except Exception:
            logger.exception("artifacts_exists failed; uploading every file")

        files = [
            {**item, "stored": item["sha256"] in already_stored} for item in files
        ]
        return self._upload_code_presigned(run_id, files)

    def _upload_code_presigned(self, run_id, files):
        descriptors = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "size_bytes": len(item["content"]),
                "content_type": item["content_type"],
                "source": item.get("source", "code"),
            }
            for item in files
        ]
        to_upload = [d for d, item in zip(descriptors, files) if not item.get("stored")]

        res = self._send(
            self.session, "POST",
            f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
            json={"files": to_upload}, timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

        urls = {u["path"]: u for u in res.json().get("uploads", [])}
        content_by_path = {item["path"]: item["content"] for item in files}
        # Files the server already stores need no bytes sent — objects are
        # addressed by content hash, so registration will find them.
        skip = {item["path"] for item in files if item.get("stored")}

        for path, upload in urls.items():
            if path in skip:
                continue
            # Deliberately not self.session.put: the session carries the
            # x-api-key header, and any header beyond those the URL was signed
            # for makes S3 reject the request.
            put = requests.put(
                upload["url"],
                data=content_by_path[path],
                headers=upload.get("headers") or {},
                timeout=max(self.timeout, 120),
            )
            put.raise_for_status()

        res = self._send(
            self.session, "POST",
            f"{self.base_url}/runs/{run_id}/artifacts/register",
            json={"files": descriptors}, timeout=max(self.timeout, 30),
        )

        # The server reports content it could not find. That means a dedup hit
        # was wrong — a row exists for the hash but the object behind it is
        # gone. Upload exactly those and register once more, rather than
        # dropping the files and logging a failure.
        if res.status_code == 409:
            missing = set((res.json().get("detail") or {}).get("missing") or [])
            recoverable = [d for d in descriptors if d["path"] in missing]
            if missing and recoverable:
                logger.info("re-uploading %d file(s) the server could not find", len(recoverable))
                retry_urls = self._send(
                    self.session, "POST",
                    f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
                    json={"files": recoverable}, timeout=max(self.timeout, 30),
                )
                retry_urls.raise_for_status()
                for upload in retry_urls.json().get("uploads", []):
                    put = requests.put(
                        upload["url"],
                        data=content_by_path[upload["path"]],
                        headers=upload.get("headers") or {},
                        timeout=max(self.timeout, 120),
                    )
                    put.raise_for_status()
                res = self._send(
                    self.session, "POST",
                    f"{self.base_url}/runs/{run_id}/artifacts/register",
                    json={"files": descriptors}, timeout=max(self.timeout, 30),
                )

        res.raise_for_status()
        return res.json()

    def log_artifact(self, run_id, name, type, metadata, file_or_data, content_type, filename=None):
        """Upload a versioned artifact straight to object storage."""
        payload, resolved_filename = _upload_source(file_or_data, filename, name)
        if isinstance(payload, Path):
            with payload.open("rb") as handle:
                return self._upload_and_register_artifact(
                    run_id, name, type, metadata, handle, content_type, resolved_filename
                )
        return self._upload_and_register_artifact(
            run_id, name, type, metadata, payload, content_type, resolved_filename
        )

    def _upload_and_register_artifact(self, run_id, name, type, metadata, handle_or_bytes, content_type, filename):
        if isinstance(handle_or_bytes, bytes):
            size_bytes = len(handle_or_bytes)
            sha256 = hashlib.sha256(handle_or_bytes).hexdigest()
            put_data = handle_or_bytes
        else:
            digest = hashlib.sha256()
            size_bytes = 0
            if hasattr(handle_or_bytes, "seek"):
                try:
                    handle_or_bytes.seek(0)
                except (AttributeError, OSError):
                    pass
            for chunk in iter(lambda: handle_or_bytes.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
            sha256 = digest.hexdigest()
            if hasattr(handle_or_bytes, "seek"):
                try:
                    handle_or_bytes.seek(0)
                except (AttributeError, OSError):
                    pass
            put_data = handle_or_bytes

        res = self._send(
            self.session, "POST",
            f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
            json={"files": [{
                "path": filename,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "source": "user",
            }]},
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

        uploads = res.json().get("uploads", [])
        if uploads:
            upload = uploads[0]
            # Streamed: requests sends the handle/data directly.
            put = requests.put(
                upload["url"],
                data=put_data,
                headers={**(upload.get("headers") or {}),
                         "Content-Length": str(size_bytes)},
                timeout=max(self.timeout, 600),
            )
            put.raise_for_status()

        # server: register-version needs an idempotency key. A lost response
        # is retried as a new upload and can mint another version.
        res = self._send(
            self._once, "POST",
            f"{self.base_url}/runs/{run_id}/artifacts/register-version",
            json={
                "name": name,
                "type": type,
                "metadata": metadata or {},
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "filename": filename,
            },
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()
        return res.json()

    def log_media(self, run_id, key, step, kind, caption, file_or_data, content_type, filename=None):
        payload, resolved_filename = _upload_source(file_or_data, filename, "media")
        if isinstance(payload, Path):
            with payload.open("rb") as handle:
                return self._do_log_media(
                    run_id, key, step, kind, caption, handle, content_type, resolved_filename
                )
        return self._do_log_media(
            run_id, key, step, kind, caption, payload, content_type, resolved_filename
        )

    def _do_log_media(self, run_id, key, step, kind, caption, handle_or_bytes, content_type, filename):
        files = {"file": (filename, handle_or_bytes, content_type)}
        data = {
            "key": key,
            "step": str(step),
            "kind": kind,
            "caption": caption or "",
        }
        res = self._send(
            self._once, "POST", f"{self.base_url}/runs/{run_id}/log_media",
            data=data, files=files, timeout=max(self.timeout, 30),
        )
        res.raise_for_status()
        return res.json()
