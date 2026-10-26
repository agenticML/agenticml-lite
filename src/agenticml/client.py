from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("agenticml.client")

DEFAULT_BASE_URL = "https://api.agenticml.xyz"
DEFAULT_OFFLINE_DIR = Path.home() / ".agenticml" / "offline"


class _PresignUnsupported(Exception):
    """Server predates the presign endpoints; use the multipart path."""


class _OfflineClient:
    """Drop-in for `Client` that writes payloads to a local JSONL journal.

    The `agenticml sync` CLI replays the journal against a real server later.
    Each run lives in its own directory so concurrent offline runs don't
    interleave events.
    """

    def __init__(self, root: Path, run_id_hint: str | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id_hint
        self._run_dir: Path | None = None

    def _ensure_run_dir(self) -> Path:
        if self._run_dir is None:
            assert self._run_id is not None, "init_run must run before journaling"
            self._run_dir = self.root / self._run_id
            self._run_dir.mkdir(parents=True, exist_ok=True)
        return self._run_dir

    def _append(self, kind: str, payload: dict):
        run_dir = self._ensure_run_dir()
        line = json.dumps({"kind": kind, "ts": time.time(), "payload": payload})
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # API mirroring Client
    def init_run(self, payload):
        if self._run_id is None:
            self._run_id = f"offline-{uuid.uuid4()}"
        self._append("init", payload)
        return self._run_id

    def log(self, payload):
        self._append("log", payload)

    def log_batch(self, items):
        if not items:
            return
        self._append("log_batch", {"items": items})

    def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
        self._append("finalize", {
            "run_id": run_id,
            "summary": summary,
            "status": status,
            "snapshot_hash": snapshot_hash,
        })

    def upload_code(self, run_id, manifest_json, files):
        # File bytes are too large to journal; store them on disk alongside the
        # journal and reference by path.
        run_dir = self._ensure_run_dir()
        code_dir = run_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        stored = []
        for item in files:
            target = code_dir / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["content"])
            stored.append({
                "path": item["path"],
                "content_type": item["content_type"],
                "sha256": item.get("sha256"),
                "source": item.get("source", "code"),
                "stored_at": str(target.relative_to(run_dir)),
            })
        self._append("upload_code", {
            "run_id": run_id,
            "manifest": json.loads(manifest_json),
            "files": stored,
        })
        return {"status": "offline", "files": len(stored)}

    def artifacts_exists(self, sha256s):
        return []

    def log_artifact(self, run_id, name, type, metadata, file_path, content_type):
        run_dir = self._ensure_run_dir()
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        target = artifacts_dir / f"{name}-{Path(file_path).name}"
        target.write_bytes(Path(file_path).read_bytes())
        self._append("log_artifact", {
            "run_id": run_id,
            "name": name,
            "type": type,
            "metadata": metadata,
            "stored_at": str(target.relative_to(run_dir)),
            "content_type": content_type,
            "filename": Path(file_path).name,
        })
        return {"status": "offline", "name": name}

    def log_media(self, run_id, key, step, kind, caption, file_path, content_type):
        run_dir = self._ensure_run_dir()
        media_dir = run_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / f"{key}-step_{step}-{Path(file_path).name}"
        target.write_bytes(Path(file_path).read_bytes())
        self._append("log_media", {
            "run_id": run_id,
            "key": key,
            "step": step,
            "kind": kind,
            "caption": caption,
            "stored_at": str(target.relative_to(run_dir)),
            "content_type": content_type,
            "filename": Path(file_path).name,
        })
        return {"status": "offline", "key": key, "step": step}

    def get_run(self, run_id):
        return None  # offline cannot resolve resume; caller must handle


class Client:
    def __init__(self, base_url=None, timeout=5, max_retries=3, api_key=None):
        resolved_base = base_url or os.environ.get("AGENTICML_HOST") or DEFAULT_BASE_URL
        resolved_key = api_key if api_key is not None else os.environ.get("AGENTICML_API_KEY")

        self.base_url = resolved_base.rstrip("/")
        self.timeout = timeout
        self.api_key = resolved_key

        self.session = requests.Session()
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key

        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PATCH"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def init_run(self, payload):
        res = self.session.post(
            f"{self.base_url}/init",
            json=payload,
            timeout=self.timeout,
        )
        res.raise_for_status()
        body = res.json()
        return body["run_id"]

    def log(self, payload):
        res = self.session.post(
            f"{self.base_url}/log",
            json=payload,
            timeout=self.timeout,
        )
        res.raise_for_status()

    def log_batch(self, items):
        if not items:
            return
        res = self.session.post(
            f"{self.base_url}/log/batch",
            json={"items": items},
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

    def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/finalize",
            json={
                "summary": summary,
                "status": status,
                "snapshot_hash": snapshot_hash,
            },
            timeout=max(self.timeout, 10),
        )
        res.raise_for_status()

    def get_run(self, run_id):
        res = self.session.get(
            f"{self.base_url}/runs/{run_id}",
            timeout=self.timeout,
        )
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()

    def artifacts_exists(self, sha256s):
        if not sha256s:
            return []
        res = self.session.post(
            f"{self.base_url}/artifacts/exists",
            json={"sha256s": list(sha256s)},
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json().get("existing", [])

    def upload_code(self, run_id, manifest_json, files):
        """Upload the code snapshot straight to object storage.

        The server signs one URL per file, the bytes go client-to-bucket, and a
        second call records them. Nothing passes through the API process, which
        previously buffered every file in memory before writing it out again.

        Falls back to the legacy multipart endpoint when the server does not
        expose presigning, so an older deployment keeps working.
        """
        if not files:
            return {"status": "ok", "registered": []}

        try:
            return self._upload_code_presigned(run_id, files)
        except _PresignUnsupported:
            logger.info("server has no presign endpoint; using multipart upload")
            return self._upload_code_multipart(run_id, manifest_json, files)

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

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
            json={"files": to_upload},
            timeout=max(self.timeout, 30),
        )
        if res.status_code == 404:
            raise _PresignUnsupported()
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

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/register",
            json={"files": descriptors},
            timeout=max(self.timeout, 30),
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
                retry_urls = self.session.post(
                    f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
                    json={"files": recoverable},
                    timeout=max(self.timeout, 30),
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
                res = self.session.post(
                    f"{self.base_url}/runs/{run_id}/artifacts/register",
                    json={"files": descriptors},
                    timeout=max(self.timeout, 30),
                )

        res.raise_for_status()
        return res.json()

    def _upload_code_multipart(self, run_id, manifest_json, files):
        multipart_files = [
            (
                "files",
                (item["path"], item["content"], item["content_type"]),
            )
            for item in files
        ]
        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/code",
            data={"manifest": manifest_json},
            files=multipart_files,
            timeout=max(self.timeout, 60),
        )
        res.raise_for_status()
        return res.json()

    def log_artifact(self, run_id, name, type, metadata, file_path, content_type):
        """Upload a versioned artifact straight to object storage.

        Falls back to the multipart endpoint on an older server, where the file
        is read into the API process in full — fine for a plot, an OOM kill for
        a checkpoint.
        """
        try:
            return self._log_artifact_presigned(
                run_id, name, type, metadata, file_path, content_type
            )
        except _PresignUnsupported:
            logger.info("server has no presign endpoint; using multipart upload")
            return self._log_artifact_multipart(
                run_id, name, type, metadata, file_path, content_type
            )

    @staticmethod
    def _hash_file(file_path):
        """Hash in chunks. Reading a checkpoint whole to hash it would recreate
        on the client exactly the problem this change removes from the server."""
        digest = hashlib.sha256()
        size = 0
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _log_artifact_presigned(self, run_id, name, type, metadata, file_path, content_type):
        sha256, size_bytes = self._hash_file(file_path)
        filename = Path(file_path).name

        res = self.session.post(
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
        if res.status_code == 404:
            raise _PresignUnsupported()
        res.raise_for_status()

        uploads = res.json().get("uploads", [])
        if uploads:
            upload = uploads[0]
            # Streamed from disk: requests sends the file handle without
            # materialising it, so memory stays flat regardless of size.
            with open(file_path, "rb") as fh:
                put = requests.put(
                    upload["url"],
                    data=fh,
                    headers={**(upload.get("headers") or {}),
                             "Content-Length": str(size_bytes)},
                    timeout=max(self.timeout, 600),
                )
            put.raise_for_status()

        res = self.session.post(
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
        if res.status_code == 404:
            raise _PresignUnsupported()
        res.raise_for_status()
        return res.json()

    def _log_artifact_multipart(self, run_id, name, type, metadata, file_path, content_type):
        with open(file_path, "rb") as fh:
            files = {"file": (Path(file_path).name, fh, content_type)}
            data = {
                "name": name,
                "type": type,
                "metadata": json.dumps(metadata or {}),
            }
            res = self.session.post(
                f"{self.base_url}/runs/{run_id}/log_artifact",
                data=data,
                files=files,
                timeout=max(self.timeout, 60),
            )
        res.raise_for_status()
        return res.json()

    def log_media(self, run_id, key, step, kind, caption, file_path, content_type):
        with open(file_path, "rb") as fh:
            files = {"file": (Path(file_path).name, fh, content_type)}
            data = {
                "key": key,
                "step": str(step),
                "kind": kind,
                "caption": caption or "",
            }
            res = self.session.post(
                f"{self.base_url}/runs/{run_id}/log_media",
                data=data,
                files=files,
                timeout=max(self.timeout, 30),
            )
        res.raise_for_status()
        return res.json()


def resolve_offline_dir(offline_dir=None) -> Path:
    """Resolve the offline asset root.

    Precedence: ``offline_dir=`` > ``AGENTICML_OFFLINE_DIR`` > ``~/.agenticml/offline``.
    The result is expanduser'd and resolved to an absolute path.
    """
    if offline_dir is not None:
        raw = offline_dir
    else:
        raw = os.environ.get("AGENTICML_OFFLINE_DIR", str(DEFAULT_OFFLINE_DIR))
    return Path(raw).expanduser().resolve()


def make_client(
    mode: str,
    base_url=None,
    api_key=None,
    run_id_hint=None,
    offline_dir=None,
) -> object:
    """Factory that returns a real or offline client based on mode."""
    if mode == "offline":
        root = resolve_offline_dir(offline_dir)
        return _OfflineClient(root, run_id_hint=run_id_hint)
    return Client(base_url=base_url, api_key=api_key)
