"""Minimal protobuf wire-format decoder for untrusted input.

``decode(buf)`` reads one message level into ``{field_number: [raw, ...]}``
without a schema. Raw values by wire type:

* varint (0): ``int``, unsigned (use :func:`as_int64` / :func:`as_bool`)
* 64-bit (1): 8 ``bytes`` (:func:`as_double`, :func:`as_fixed64`)
* length-delimited (2): ``bytes`` (a string, a nested message, or packed values)
* 32-bit (5): 4 ``bytes`` (:func:`as_float`, :func:`as_fixed32`)

Nested messages are decoded by the caller with ``message.child(raw)``, which
counts the depth and refuses to go past :data:`MAX_DEPTH`. Every length is
checked against the bytes that remain, and one decode call reads at most
:data:`MAX_FIELDS` fields. Anything malformed raises :class:`ProtobufError`.

Memory: ``decode`` does not copy the buffer. Long length-delimited values are
``memoryview`` slices of it (so a nested message costs no copy at any depth);
short ones and fixed-width ones are ``bytes``. A decoded message therefore
keeps the whole input buffer alive; drop nested messages as soon as they are
processed instead of keeping a chain of them.

Repeated numeric fields may arrive packed (one length-delimited value) or
unpacked (one value per tag); the ``repeated_*`` helpers accept both.
"""

from __future__ import annotations

import struct

MAX_DEPTH = 32
MAX_FIELDS = 1_000_000  # fields in one decode() call; bounds the Python objects a hostile file can make
_INLINE = 64  # length-delimited values up to this size are copied to bytes
_MAX_FIELD_NUMBER = (1 << 29) - 1
_U64 = (1 << 64) - 1


class ProtobufError(ValueError):
    """The bytes are not a well-formed protobuf message."""


class Message(dict):
    """``{field_number: [raw, ...]}`` plus the nesting depth it was decoded at."""

    depth = 0

    def child(self, raw) -> Message:
        """Decode a length-delimited ``raw`` value as a nested message."""
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise ProtobufError("nested message must be length-delimited")
        return decode(raw, depth=self.depth + 1)

    def first(self, field, default=None):
        values = self.get(field)
        return values[0] if values else default

    def last(self, field, default=None):
        values = self.get(field)
        return values[-1] if values else default


def _varint(buf, pos: int, end: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= end:
            raise ProtobufError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value & _U64, pos
        shift += 7
        if shift >= 70:
            raise ProtobufError("varint longer than 10 bytes")


def decode(buf, *, depth: int = 0) -> Message:
    """Decode one message level. ``depth`` is the nesting depth of ``buf``."""
    if depth > MAX_DEPTH:
        raise ProtobufError(f"messages nested deeper than {MAX_DEPTH}")
    data = memoryview(buf)
    if data.format != "B" or data.ndim != 1:
        data = data.cast("B")
    end = len(data)
    pos = 0
    count = 0
    message = Message()
    message.depth = depth
    while pos < end:
        count += 1
        if count > MAX_FIELDS:
            raise ProtobufError(f"more than {MAX_FIELDS} fields in one message")
        tag, pos = _varint(data, pos, end)
        field = tag >> 3
        wire = tag & 7
        if field == 0 or field > _MAX_FIELD_NUMBER:
            raise ProtobufError(f"bad field number {field}")
        if wire == 0:
            value, pos = _varint(data, pos, end)
        elif wire == 1:
            if end - pos < 8:
                raise ProtobufError("truncated 64-bit field")
            value = bytes(data[pos : pos + 8])
            pos += 8
        elif wire == 2:
            length, pos = _varint(data, pos, end)
            if length > end - pos:
                raise ProtobufError("length-delimited field runs past the end")
            value = data[pos : pos + length]
            if length <= _INLINE:
                value = bytes(value)
            pos += length
        elif wire == 5:
            if end - pos < 4:
                raise ProtobufError("truncated 32-bit field")
            value = bytes(data[pos : pos + 4])
            pos += 4
        else:
            raise ProtobufError(f"unsupported wire type {wire}")
        message.setdefault(field, []).append(value)
    return message


def _varint_value(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtobufError("unexpected wire type for field")
    return value


def _bytes_value(raw, size: int | None = None) -> bytes:
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ProtobufError("unexpected wire type for field")
    data = bytes(raw)
    if size is not None and len(data) != size:
        raise ProtobufError("unexpected wire type for field")
    return data


def as_int64(value: int) -> int:
    """Signed int64 from a varint (two's complement)."""
    value = _varint_value(value)
    return value - (1 << 64) if value >= (1 << 63) else value


def as_int32(value: int) -> int:
    """Signed int32 from a varint; negative values are sign-extended to 64 bits."""
    value = _varint_value(value) & 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def as_bool(value: int) -> bool:
    return bool(_varint_value(value))


def as_double(raw: bytes) -> float:
    return struct.unpack("<d", _bytes_value(raw, 8))[0]


def as_float(raw: bytes) -> float:
    return struct.unpack("<f", _bytes_value(raw, 4))[0]


def as_fixed64(raw: bytes) -> int:
    return struct.unpack("<Q", _bytes_value(raw, 8))[0]


def as_fixed32(raw: bytes) -> int:
    return struct.unpack("<I", _bytes_value(raw, 4))[0]


def as_str(raw: bytes) -> str:
    """UTF-8 text; undecodable bytes are replaced, never an error."""
    return _bytes_value(raw).decode("utf-8", errors="replace")


def _fixed_values(values, size: int, code: str) -> list:
    blob = b"".join(_bytes_value(v) for v in values)
    if len(blob) % size:
        raise ProtobufError("packed fixed-width field has a partial value")
    return [item[0] for item in struct.iter_unpack(code, blob)]


def repeated_floats(values) -> list[float]:
    """Packed and unpacked ``float`` (fixed32) values, in order."""
    return _fixed_values(values, 4, "<f")


def repeated_doubles(values) -> list[float]:
    """Packed and unpacked ``double`` (fixed64) values, in order."""
    return _fixed_values(values, 8, "<d")


def repeated_fixed32(values) -> list[int]:
    return _fixed_values(values, 4, "<I")


def repeated_fixed64(values) -> list[int]:
    return _fixed_values(values, 8, "<Q")


def repeated_varints(values) -> list[int]:
    """Packed and unpacked varints (unsigned), in order."""
    out: list[int] = []
    for item in values:
        if isinstance(item, int) and not isinstance(item, bool):
            out.append(item)
            continue
        data = _bytes_value(item)
        pos = 0
        while pos < len(data):
            number, pos = _varint(data, pos, len(data))
            out.append(number)
    return out


def repeated_int64(values) -> list[int]:
    return [as_int64(v) for v in repeated_varints(values)]


def repeated_int32(values) -> list[int]:
    return [as_int32(v) for v in repeated_varints(values)]
