"""Media wrappers — values that the Run knows to upload as media events
instead of as scalar metrics.

Currently supports:
- Image: a PIL.Image, numpy array, filesystem path, bytes, or file-like object.

Heavier types (Tables, Audio, Histograms) belong in later phases; the same
pattern (wrapper class with a `_to_upload` method) extends naturally.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("agenticml.media")


def _write_temp_bytes(payload: bytes, suffix: str = ".png") -> str:
    """Write ``payload`` to a temp file and delete it if the write fails."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    name = tmp.name
    try:
        tmp.write(payload)
        tmp.close()
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        Path(name).unlink(missing_ok=True)
        raise
    return name


def _save_temp_png(save) -> str:
    """Write a temp PNG and delete it if ``save`` raises."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    name = tmp.name
    try:
        tmp.close()
        save(name)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise
    return name


class _Media:
    """Base for objects the Run uploads via /log_media instead of as floats."""

    kind: str = "media"

    def _to_upload(self) -> tuple[str, str, str]:
        """Return (file_path, content_type, suggested_filename).

        File at file_path may be a tempfile; the caller is responsible for
        cleanup if needed.
        """
        raise NotImplementedError

    def _upload_is_temporary(self) -> bool:
        return False

    def caption(self) -> str | None:
        return getattr(self, "_caption", None)


class Image(_Media):
    """Log an image at a given (key, step).

    Accepts:
        - filesystem path
        - bytes, or a file-like object whose ``read()`` returns bytes
        - numpy.ndarray (HxW or HxWx{1,3,4})
        - PIL.Image.Image
    """

    kind = "image"

    def __init__(self, data, caption: str | None = None):
        self._data = data
        self._caption = caption

    def _upload_is_temporary(self) -> bool:
        return not isinstance(self._data, (str, os.PathLike))

    def _to_upload(self) -> tuple[str, str, str]:
        # Path on disk
        if isinstance(self._data, (str, os.PathLike)):
            path = Path(self._data)
            content_type = mimetypes.guess_type(path.name)[0] or "image/png"
            return str(path), content_type, path.name

        # Raw bytes
        if isinstance(self._data, (bytes, bytearray)):
            return _write_temp_bytes(bytes(self._data)), "image/png", "image.png"

        # File-like. Read once and store the bytes; callers close their own handle.
        read = getattr(self._data, "read", None)
        if callable(read):
            payload = read()
            if not isinstance(payload, (bytes, bytearray)):
                raise TypeError(
                    f"file-like Image.read() must return bytes, got {type(payload)!r}"
                )
            return _write_temp_bytes(bytes(payload)), "image/png", "image.png"

        # PIL.Image — duck-type check; do not import PIL eagerly
        save = getattr(self._data, "save", None)
        if callable(save):
            path = _save_temp_png(lambda name: self._data.save(name, format="PNG"))
            return path, "image/png", "image.png"

        # numpy array — encode via PIL if available, else raise
        if hasattr(self._data, "shape") and hasattr(self._data, "dtype"):
            try:
                from PIL import Image as PILImage
            except ImportError as exc:
                raise RuntimeError(
                    "logging numpy arrays as Image requires Pillow"
                ) from exc
            arr = self._data
            if arr.dtype != "uint8":
                # Best-effort scale to 0-255.
                import numpy as np

                arr = np.clip(arr, 0, 1) * 255 if arr.max() <= 1.0 else np.clip(arr, 0, 255)
                arr = arr.astype("uint8")
            img = PILImage.fromarray(arr)
            path = _save_temp_png(lambda name: img.save(name, format="PNG"))
            return path, "image/png", "image.png"

        raise TypeError(f"unsupported Image data type: {type(self._data)!r}")


def is_media(value) -> bool:
    return isinstance(value, _Media)
