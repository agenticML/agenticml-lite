"""CRC-32C (Castagnoli), the checksum TFRecord files use.

Table driven and pure Python so the core install needs no extra package.
``crc32c(b"123456789") == 0xE3069283``.
"""

from __future__ import annotations

_POLY = 0x82F63B78  # reflected 0x1EDC6F41


def _make_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _make_table()


def crc32c(data, crc: int = 0) -> int:
    """CRC-32C of ``data``. Pass a previous result as ``crc`` to continue a stream."""
    table = _TABLE
    crc ^= 0xFFFFFFFF
    for byte in memoryview(data).cast("B"):
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def masked_crc32c(data) -> int:
    """The masked CRC TFRecord stores after the length and after the data."""
    crc = crc32c(data)
    return ((((crc >> 15) | (crc << 17)) & 0xFFFFFFFF) + 0xA282EAD8) & 0xFFFFFFFF
