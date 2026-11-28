"""Read the LevelDB log format that W&B ``.wandb`` files use.

Layout, checked against wandb 0.31.2 (``wandb/sdk/internal/datastore.py`` in
the 0.2x releases, and files written by the 0.31 Go core):

* a 7 byte file header: ``b":W&B"``, the magic ``0xBEE1`` (little-endian
  ``uint16``) and a version byte (``0``);
* then physical records packed into 32 KiB blocks. Block boundaries are counted
  from the start of the file, header included. A record header is
  ``crc(4) length(2) type(1)``, all little-endian, followed by ``length``
  bytes. A block that has fewer than 7 bytes left is zero padded.
* ``type`` is FULL (1), FIRST (2), MIDDLE (3) or LAST (4); a logical record
  that spans blocks is FIRST, MIDDLE..., LAST.
* The checksum is plain ``zlib.crc32`` (not CRC-32C, and not masked) of the
  record data, seeded with ``zlib.crc32(bytes([type]))``.

All input is untrusted: every physical record is checked against its block
and its checksum, a logical record longer than :data:`MAX_RECORD` is skipped
with one warning, and a truncated final record (a file that is still being
written, or a crashed writer) ends the file quietly. Anything else that does
not parse raises :class:`LevelDBLogError`.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable, Iterator
from typing import BinaryIO

BLOCK_SIZE = 32768
RECORD_HEADER_SIZE = 7
FILE_HEADER = b":W&B" + struct.pack("<HB", 0xBEE1, 0)
FILE_HEADER_SIZE = len(FILE_HEADER)  # 7
MAX_RECORD = 64 * 1024 * 1024  # a longer logical record is skipped

FULL, FIRST, MIDDLE, LAST = 1, 2, 3, 4
_TYPE_SEEDS = {kind: zlib.crc32(bytes([kind])) for kind in (FULL, FIRST, MIDDLE, LAST)}


class LevelDBLogError(ValueError):
    """The file is not a W&B LevelDB log, or a record in it is corrupt."""


def check_file_header(fp: BinaryIO) -> None:
    """Read and validate the 7 byte W&B header (leaves ``fp`` after it)."""
    head = fp.read(FILE_HEADER_SIZE)
    if len(head) < FILE_HEADER_SIZE or head[:6] != FILE_HEADER[:6] or head[6] != 0:
        raise LevelDBLogError("not a W&B data file (bad header)")


def read_records(
    fp: BinaryIO,
    *,
    max_record: int | None = None,
    on_skip: Callable[[str], None] | None = None,
) -> Iterator[bytes]:
    """Yield each logical record of the log in ``fp`` (positioned after the file header).

    ``max_record`` defaults to :data:`MAX_RECORD` (read at call time). ``on_skip(message)``
    is called once for the first record skipped for being too long.
    """
    limit = MAX_RECORD if max_record is None else max_record
    pos = FILE_HEADER_SIZE  # offset of the next physical record in the file
    parts: list[bytes] = []
    size = 0
    in_record = False  # between FIRST and LAST
    skipping = False  # discarding the rest of an over-long record
    warned = False

    def too_long() -> None:
        nonlocal warned
        if not warned and on_skip is not None:
            warned = True
            on_skip(f"skipped a record longer than {limit // (1024 * 1024)} MiB")

    while True:
        left = BLOCK_SIZE - pos % BLOCK_SIZE
        if left < RECORD_HEADER_SIZE:
            # Block trailer padding.
            skipped = fp.read(left)
            if len(skipped) < left:
                return
            pos += left
            continue
        header = fp.read(RECORD_HEADER_SIZE)
        if len(header) < RECORD_HEADER_SIZE:
            return  # end of file (a partial header is a write in progress)
        crc, length, kind = struct.unpack("<IHB", header)
        if crc == 0 and length == 0 and kind == 0:
            # Zero filled space: skip to the next block.
            if left - RECORD_HEADER_SIZE:
                rest = fp.read(left - RECORD_HEADER_SIZE)
                if len(rest) < left - RECORD_HEADER_SIZE:
                    return
                if rest.strip(b"\x00"):
                    raise LevelDBLogError("corrupt record header")
            pos += left
            continue
        if kind not in _TYPE_SEEDS:
            raise LevelDBLogError(f"unknown record type {kind}")
        if length > left - RECORD_HEADER_SIZE:
            raise LevelDBLogError("record runs past the end of its block")
        payload = fp.read(length)
        if len(payload) < length:
            return  # truncated final record
        pos += RECORD_HEADER_SIZE + length
        if zlib.crc32(payload, _TYPE_SEEDS[kind]) & 0xFFFFFFFF != crc:
            raise LevelDBLogError(f"checksum mismatch at offset {pos - RECORD_HEADER_SIZE - length}")

        if kind == FULL:
            if in_record:
                raise LevelDBLogError("record starts inside another record")
            if length > limit:
                too_long()
                continue
            yield payload
        elif kind == FIRST:
            if in_record:
                raise LevelDBLogError("record starts inside another record")
            in_record = True
            parts = [payload]
            size = length
            skipping = size > limit
            if skipping:
                parts = []
                too_long()
        else:  # MIDDLE or LAST
            if not in_record:
                raise LevelDBLogError("record continues without a start")
            if not skipping:
                size += length
                if size > limit:
                    skipping = True
                    parts = []
                    too_long()
                else:
                    parts.append(payload)
            if kind == LAST:
                in_record = False
                if not skipping:
                    yield b"".join(parts)
                parts = []
                size = 0
                skipping = False
