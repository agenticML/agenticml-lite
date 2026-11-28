"""Recognise image bytes by their magic numbers."""

from __future__ import annotations


def sniff_image_type(data) -> str | None:
    """MIME type for PNG, JPEG, GIF or WEBP bytes, else ``None``.

    Only the header is looked at; the bytes are not decoded or validated.
    """
    head = bytes(data[:12])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None
