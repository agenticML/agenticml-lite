"""Workspace-scoped immutable objects on disk or S3."""
import base64
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from agenticml.store import layout, objects
from agenticml.store.errors import ObjectStoreUnavailable
from .db.workspaces import require_workspace_id
from .workspaces import store_root

logger = logging.getLogger('agenticml.server.objects')


def _validate(workspace_id, sha):
    require_workspace_id(workspace_id)
    if not layout.valid_sha(sha):
        raise ValueError('invalid sha')


class ObjectBackend:
    def missing(self, workspace_id, shas):
        require_workspace_id(workspace_id)
        return [sha for sha in dict.fromkeys(shas) if not self.exists(workspace_id, sha)]


class FileObjects(ObjectBackend):
    name = 'file'

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def _path(self, workspace_id, sha):
        _validate(workspace_id, sha)
        return layout.object_path(store_root(self.data_dir, workspace_id), sha)

    def exists(self, workspace_id, sha):
        self._path(workspace_id, sha)
        return objects.has(store_root(self.data_dir, workspace_id), sha)

    def put(self, workspace_id, sha, path, size):
        dest = self._path(workspace_id, sha)
        if (dest.exists() or dest.is_symlink()) and not stat.S_ISREG(dest.lstat().st_mode):
            raise ValueError('unsafe object destination')
        if self.exists(workspace_id, sha):
            os.utime(dest, None, follow_symlinks=False)
            return False
        try:
            dest.parent.mkdir(parents=True)
            created_prefix = True
        except FileExistsError:
            created_prefix = False
        self._path(workspace_id, sha)
        if created_prefix:
            layout.fsync_dir(dest.parent.parent)
        os.replace(path, dest)
        layout.fsync_dir(dest.parent)
        return True

    def open(self, workspace_id, sha):
        self._path(workspace_id, sha)
        from agenticml.ui.server import _open_regular_object
        return _open_regular_object(store_root(self.data_dir, workspace_id), sha)

    def size(self, workspace_id, sha):
        path = self._path(workspace_id, sha)
        return path.lstat().st_size if self.exists(workspace_id, sha) else None

    def delete(self, workspace_id, sha):
        path = self._path(workspace_id, sha)
        path.unlink()
        layout.fsync_dir(path.parent)

    def delete_workspace(self, workspace_id):
        require_workspace_id(workspace_id)

    def describe(self):
        return 'file'


class _S3Stream:
    def __init__(self, backend, response):
        self.backend = backend
        self.body = response['Body']
        self.size = response['ContentLength']

    def read(self, n=-1):
        n = None if n is None or n < 0 else n
        chunks = []
        remaining = n
        while remaining is None or remaining > 0:
            chunk = self.backend._call(self.body.read, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        return b"".join(chunks)

    def close(self):
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class S3Objects(ObjectBackend):
    name = 's3'

    def __init__(self, bucket, prefix, client):
        self.bucket, self.prefix, self.client = bucket, prefix, client
        self.kms_key = os.environ.get('AGENTICML_SERVER_S3_KMS_KEY_ID')

    def _key(self, workspace_id, sha):
        _validate(workspace_id, sha)
        return f'{self.prefix + "/" if self.prefix else ""}{workspace_id}/objects/{sha[:2]}/{sha}'

    def _call(self, method, *args, _precondition=False, **kwargs):
        from botocore.exceptions import (ClientError, EndpointConnectionError,
                                        ConnectTimeoutError, ReadTimeoutError,
                                        IncompleteReadError, ResponseStreamingError,
                                        ConnectionClosedError)
        try:
            from urllib3.exceptions import ProtocolError
            protocol_errors = (ProtocolError,)
        except ImportError:
            protocol_errors = ()
        try:
            return method(*args, **kwargs)
        except ClientError as exc:
            code = str(exc.response.get('Error', {}).get('Code', ''))
            status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if code in ('404', 'NoSuchKey', 'NotFound'):
                raise FileNotFoundError() from exc
            if _precondition and (code in ('412', 'PreconditionFailed') or status == 412):
                return None
            logger.exception('S3 request failed for %s', self.describe())
            raise ObjectStoreUnavailable('object store unavailable') from exc
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError,
                IncompleteReadError, ResponseStreamingError, ConnectionClosedError) + protocol_errors as exc:
            logger.exception('S3 request failed for %s', self.describe())
            raise ObjectStoreUnavailable('object store unavailable') from exc

    def size(self, workspace_id, sha):
        key = self._key(workspace_id, sha)
        try:
            return self._call(self.client.head_object, Bucket=self.bucket, Key=key)['ContentLength']
        except FileNotFoundError:
            return None

    def exists(self, workspace_id, sha):
        return self.size(workspace_id, sha) is not None

    def missing(self, workspace_id, shas):
        require_workspace_id(workspace_id)
        shas = list(dict.fromkeys(shas))
        for sha in shas:
            _validate(workspace_id, sha)
        with ThreadPoolExecutor(max_workers=8) as pool:
            present = pool.map(lambda sha: self.exists(workspace_id, sha), shas)
            return [sha for sha, exists in zip(shas, present) if not exists]

    def put(self, workspace_id, sha, path, size):
        key = self._key(workspace_id, sha)
        if self.exists(workspace_id, sha):
            return False
        encryption = ({'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': self.kms_key}
                      if self.kms_key else {'ServerSideEncryption': 'AES256'})
        with layout.open_nofollow(path) as body:
            result = self._call(self.client.put_object, _precondition=True, Bucket=self.bucket, Key=key,
                                Body=body, ContentLength=size, IfNoneMatch='*',
                                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
                                **encryption)
        return result is not None

    def open(self, workspace_id, sha):
        return _S3Stream(self, self._call(self.client.get_object, Bucket=self.bucket,
                                        Key=self._key(workspace_id, sha)))

    def delete(self, workspace_id, sha):
        self._call(self.client.delete_object, Bucket=self.bucket, Key=self._key(workspace_id, sha))

    def delete_workspace(self, workspace_id):
        require_workspace_id(workspace_id)
        prefix = f'{self.prefix + "/" if self.prefix else ""}{workspace_id}/'
        token = None
        while True:
            args = {'ContinuationToken': token} if token else {}
            page = self._call(self.client.list_objects_v2, Bucket=self.bucket, Prefix=prefix, **args)
            keys = [{'Key': item['Key']} for item in page.get('Contents', [])]
            for start in range(0, len(keys), 1000):
                result = self._call(self.client.delete_objects, Bucket=self.bucket,
                                    Delete={'Objects': keys[start:start + 1000]})
                if result.get('Errors'):
                    logger.error('S3 deletion failed: %s', result['Errors'])
                    raise ObjectStoreUnavailable('object store unavailable')
            if not page.get('IsTruncated'):
                break
            token = page['NextContinuationToken']

    def describe(self):
        return f's3://{self.bucket}' + (f'/{self.prefix}' if self.prefix else '')


def open_backend(url, data_dir):
    if not url or url == 'file://':
        return FileObjects(data_dir)
    parsed = urlsplit(url)
    if parsed.scheme == 'file':
        if parsed.netloc or parsed.query or parsed.fragment or Path(parsed.path).resolve() != Path(data_dir).resolve():
            raise ValueError('objects must stay in the per-workspace store')
        return FileObjects(data_dir)
    if parsed.scheme != 's3':
        raise ValueError('--objects must be file:// or s3://bucket/prefix')
    bucket = parsed.netloc
    prefix = parsed.path[1:]
    try:
        ipaddress.ip_address(bucket)
        is_ip = True
    except ValueError:
        is_ip = False
    if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket) or is_ip
            or '..' in bucket or '.-' in bucket or '-.' in bucket
            or bucket.startswith(('xn--', 'sthree-', 'amzn-s3-demo-'))
            or bucket.endswith(('-s3alias', '--ol-s3', '.mrap', '--x-s3', '--table-s3'))):
        raise ValueError('invalid S3 bucket name')
    if parsed.query or parsed.fragment or (prefix and any(p in ('', '.', '..') for p in prefix.split('/'))):
        raise ValueError('invalid S3 prefix')
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ValueError("S3 objects need the [s3] extra: pip install 'agenticml-lite[s3]'") from exc
    client = boto3.client('s3', endpoint_url=os.environ.get('AGENTICML_SERVER_S3_ENDPOINT_URL') or None,
                          config=Config(retries={'mode': 'standard', 'max_attempts': 5},
                                        connect_timeout=5, read_timeout=60))
    return S3Objects(bucket, prefix, client)
