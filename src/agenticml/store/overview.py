"""Fixed-size summary of a scalar series.

A chart cannot draw a million points, and a reader should not have to scan
them to draw one. ``Overview`` keeps at most ``max_buckets`` buckets. Bucket
width ``W`` starts at 1 and doubles when a step would land at index
``>= max_buckets``. Doubling merges bucket ``2i`` with ``2i+1`` into ``i``,
which is exactly ``step // (2W)`` for every point already stored, so the
result matches grouping every point by ``step // W_final``.

``last`` is the latest finite value appended into that bucket, not the value
at ``step_max``. A sequence number on each bucket makes the merge pick the
same value a single pass at the final width would. Non-finite values are
counted and left out of the buckets so one NaN cannot blank a chart.

``vsum`` is a float64 sum. For values whose partial sums are exact in
float64 (integers inside the 53-bit mantissa) the sum matches a direct
group-by. Wildly different magnitudes can differ in the last bits because
left-then-right is not the same addition order as a fully interleaved scan;
the bucket bounds, counts, and ``last`` do not depend on that.
"""

from __future__ import annotations

import math
import operator
import struct
from dataclasses import dataclass

MAGIC = b"AMLO"
VERSION = 2
DEFAULT_MAX_BUCKETS = 2048
OVERVIEW_MIN_POINTS = 65536

# Header is 40 bytes, each bucket 36 bytes. Little-endian, standard size (no pad).
# See docs/FORMAT.md for the field list. Width is uint64 because a uint32
# step with max_buckets=1 needs W = 2**32, which does not fit in uint32.
_HEADER = struct.Struct("<4sHHIIQQQ")
_BUCKET = struct.Struct("<IIIIfffd")
assert _HEADER.size == 40
assert _BUCKET.size == 36


@dataclass(frozen=True)
class Bucket:
    """One overview bucket, or a downsample merge of several.

    ``index`` is ``step // width`` for a real bucket. A downsampled bucket
    spans several indexes; ``index`` is then the first of them. ``seq`` is
    the 1-based finite-point counter at the moment ``last`` was appended.
    """

    index: int
    step_min: int
    step_max: int
    vmin: float
    vmax: float
    vsum: float
    count: int
    last: float
    seq: int = 0

    @property
    def mean(self) -> float:
        return self.vsum / self.count


class _Acc:
    """Mutable bucket. Not part of the public result."""

    __slots__ = (
        "step_min",
        "step_max",
        "vmin",
        "vmax",
        "vsum",
        "count",
        "last",
        "seq",
    )

    def __init__(self, step: int, value: float, seq: int) -> None:
        self.step_min = step
        self.step_max = step
        self.vmin = value
        self.vmax = value
        self.vsum = value
        self.count = 1
        self.last = value
        self.seq = seq

    def add(self, step: int, value: float, seq: int) -> None:
        if step < self.step_min:
            self.step_min = step
        elif step > self.step_max:
            self.step_max = step
        if value < self.vmin:
            self.vmin = value
        elif value > self.vmax:
            self.vmax = value
        self.vsum += value
        self.count += 1
        self.last = value
        self.seq = seq

    def freeze(self, index: int) -> Bucket:
        return Bucket(
            index=index,
            step_min=self.step_min,
            step_max=self.step_max,
            vmin=self.vmin,
            vmax=self.vmax,
            vsum=self.vsum,
            count=self.count,
            last=self.last,
            seq=self.seq,
        )

    @classmethod
    def restore(
        cls,
        step_min: int,
        step_max: int,
        vmin: float,
        vmax: float,
        vsum: float,
        count: int,
        last: float,
        seq: int,
    ) -> _Acc:
        acc = cls.__new__(cls)
        acc.step_min = step_min
        acc.step_max = step_max
        acc.vmin = vmin
        acc.vmax = vmax
        acc.vsum = vsum
        acc.count = count
        acc.last = last
        acc.seq = seq
        return acc


def _as_step(step: int) -> int:
    if isinstance(step, bool):
        raise TypeError("step must be an integer")
    try:
        value = operator.index(step)
    except TypeError:
        raise TypeError("step must be an integer") from None
    if value < 0:
        raise ValueError("step must be >= 0")
    return value


def _merge_acc(left: _Acc, right: _Acc) -> _Acc:
    """Union of two sibling buckets. ``vsum`` adds left then right."""
    if left.seq >= right.seq:
        last, seq = left.last, left.seq
    else:
        last, seq = right.last, right.seq
    return _Acc.restore(
        step_min=left.step_min if left.step_min < right.step_min else right.step_min,
        step_max=left.step_max if left.step_max > right.step_max else right.step_max,
        vmin=left.vmin if left.vmin < right.vmin else right.vmin,
        vmax=left.vmax if left.vmax > right.vmax else right.vmax,
        vsum=left.vsum + right.vsum,
        count=left.count + right.count,
        last=last,
        seq=seq,
    )


def _merge_buckets(group: list[Bucket]) -> Bucket:
    acc = group[0]
    for bucket in group[1:]:
        if acc.seq >= bucket.seq:
            last, seq = acc.last, acc.seq
        else:
            last, seq = bucket.last, bucket.seq
        acc = Bucket(
            index=acc.index,
            step_min=acc.step_min if acc.step_min < bucket.step_min else bucket.step_min,
            step_max=acc.step_max if acc.step_max > bucket.step_max else bucket.step_max,
            vmin=acc.vmin if acc.vmin < bucket.vmin else bucket.vmin,
            vmax=acc.vmax if acc.vmax > bucket.vmax else bucket.vmax,
            vsum=acc.vsum + bucket.vsum,
            count=acc.count + bucket.count,
            last=last,
            seq=seq,
        )
    return acc


def _step_down_f32(f: float) -> float:
    b = struct.pack("<f", f)
    u = struct.unpack("<I", b)[0]
    if u == 0 or u == 0x80000000:  # +-0.0
        return struct.unpack("<f", struct.pack("<I", 0x80000001))[0]
    if u & 0x80000000:  # negative: stepping down means larger magnitude
        if u == 0xFF800000:  # -inf
            return f
        u += 1
    else:  # positive: stepping down means smaller magnitude
        u -= 1
    return struct.unpack("<f", struct.pack("<I", u))[0]


def _step_up_f32(f: float) -> float:
    b = struct.pack("<f", f)
    u = struct.unpack("<I", b)[0]
    if u == 0 or u == 0x80000000:  # +-0.0
        return struct.unpack("<f", struct.pack("<I", 0x00000001))[0]
    if u & 0x80000000:  # negative: stepping up means smaller magnitude
        if u == 0x80000001:  # -min_subnormal steps up to 0.0
            return 0.0
        u -= 1
    else:  # positive: stepping up means larger magnitude
        if u == 0x7F800000:  # +inf
            return f
        u += 1
    return struct.unpack("<f", struct.pack("<I", u))[0]


class Overview:
    """Sparse bucket summary. See the module docstring for the merge rule."""

    def __init__(self, max_buckets: int = DEFAULT_MAX_BUCKETS) -> None:
        if isinstance(max_buckets, bool) or not isinstance(max_buckets, int) or max_buckets < 1:
            raise ValueError("max_buckets must be an integer >= 1")
        if max_buckets > 0xFFFFFFFF:
            raise ValueError("max_buckets does not fit in uint32")
        self.max_buckets = max_buckets
        self.width = 1
        self.point_count = 0
        self.nonfinite = 0
        self._seq = 0
        self._buckets: dict[int, _Acc] = {}

    def add(self, step: int, value: float) -> None:
        """Record one point. Non-finite values increment ``nonfinite`` only."""
        step = _as_step(step)
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise TypeError("value must be a real number") from None
        self.point_count += 1
        if not math.isfinite(value):
            self.nonfinite += 1
            return
        self._seq += 1
        self._ensure_width(step)
        index = step // self.width
        acc = self._buckets.get(index)
        if acc is None:
            self._buckets[index] = _Acc(step, value, self._seq)
        else:
            acc.add(step, value, self._seq)

    def add_many(self, points) -> None:
        """Add ``(step, value)`` pairs in order. Chunking does not change the result."""
        for step, value in points:
            self.add(step, value)

    def buckets(self) -> list[Bucket]:
        """Buckets sorted by index. Each point's step maps to exactly one."""
        return [self._buckets[index].freeze(index) for index in sorted(self._buckets)]

    def downsample(self, max_points: int) -> list[Bucket]:
        """Merge consecutive buckets into at most ``max_points`` groups.

        Groups are as even as integer arithmetic allows: group ``i`` covers
        buckets ``[floor(i*n/m), floor((i+1)*n/m))``. Counts sum, so the mean
        of a group is the mean of its points, not the mean of its means.
        """
        if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
            raise ValueError("max_points must be an integer >= 1")
        items = self.buckets()
        count = len(items)
        if count <= max_points:
            return items
        merged: list[Bucket] = []
        for i in range(max_points):
            start = (i * count) // max_points
            end = ((i + 1) * count) // max_points
            merged.append(_merge_buckets(items[start:end]))
        return merged

    def to_bytes(self) -> bytes:
        """Serialize. Layout is documented in ``docs/FORMAT.md``."""
        items = self.buckets()
        try:
            header = _HEADER.pack(
                MAGIC,
                VERSION,
                0,
                self.max_buckets,
                len(items),
                self.width,
                self.point_count,
                self.nonfinite,
            )
            parts = [header]
            for bucket in items:
                # vmin, vmax, last are f32; vsum is f64
                # Directed rounding: if f32 vmin > true vmin step it down one ulp;
                # if f32 vmax < true vmax step it up one ulp.
                vmin_f = struct.unpack("<f", struct.pack("<f", bucket.vmin))[0]
                if vmin_f > bucket.vmin:
                    vmin_f = _step_down_f32(vmin_f)
                vmax_f = struct.unpack("<f", struct.pack("<f", bucket.vmax))[0]
                if vmax_f < bucket.vmax:
                    vmax_f = _step_up_f32(vmax_f)
                parts.append(
                    _BUCKET.pack(
                        bucket.index,
                        bucket.step_min,
                        bucket.step_max,
                        bucket.count,
                        vmin_f,
                        vmax_f,
                        bucket.last,
                        bucket.vsum,
                    )
                )
        except (struct.error, OverflowError) as exc:
            raise ValueError("overview does not fit the v2 record") from exc
        return b"".join(parts)

    @classmethod
    def from_bytes(cls, data: bytes) -> Overview:
        """Inverse of :meth:`to_bytes`. Raises ``ValueError`` if the bytes are not valid v2.

        Bucket ``vmin`` / ``vmax`` / ``vsum`` / ``last`` must be finite, ``vmin``
        must not exceed ``vmax``, ``count`` must be at least 1, steps must fall
        in that bucket's index, and indexes must be strictly increasing.
        The sum of bucket counts must equal ``point_count - nonfinite``.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("overview bytes required")
        blob = bytes(data)
        if len(blob) < _HEADER.size:
            raise ValueError("overview header truncated")
        try:
            (
                magic,
                version,
                _reserved,
                max_buckets,
                bucket_count,
                width,
                point_count,
                nonfinite,
            ) = _HEADER.unpack_from(blob, 0)
        except struct.error as exc:
            raise ValueError("overview header truncated") from exc
        if magic != MAGIC:
            raise ValueError("bad overview magic")
        if version != VERSION:
            raise ValueError(f"unsupported overview version {version}")
        expected = _HEADER.size + bucket_count * _BUCKET.size
        if len(blob) != expected:
            raise ValueError("overview length does not match bucket count")
        if max_buckets < 1 or width < 1 or (width & (width - 1)) != 0:
            raise ValueError("overview width is not a power of two")
        if nonfinite > point_count:
            raise ValueError("nonfinite exceeds point count")
        overview = cls(max_buckets=max_buckets)
        overview.width = width
        overview.point_count = point_count
        overview.nonfinite = nonfinite
        offset = _HEADER.size
        finite = 0
        prev_index = -1
        for _ in range(bucket_count):
            try:
                (
                    index,
                    step_min,
                    step_max,
                    count,
                    vmin,
                    vmax,
                    last,
                    vsum,
                ) = _BUCKET.unpack_from(blob, offset)
            except struct.error as exc:
                raise ValueError("overview bucket truncated") from exc
            offset += _BUCKET.size
            if index >= max_buckets or index <= prev_index:
                raise ValueError("bucket index out of range")
            prev_index = index
            if (
                not math.isfinite(vmin)
                or not math.isfinite(vmax)
                or not math.isfinite(vsum)
                or not math.isfinite(last)
            ):
                raise ValueError("bucket values must be finite")
            if count < 1 or step_min > step_max or vmin > vmax:
                raise ValueError("corrupt bucket")
            if step_min // width != index or step_max // width != index:
                raise ValueError("bucket index does not match its steps")
            overview._buckets[index] = _Acc.restore(
                step_min, step_max, vmin, vmax, vsum, count, last, finite + count
            )
            finite += count
        # Non-finite points are counted in point_count and stay out of buckets.
        if finite != point_count - nonfinite:
            raise ValueError("bucket counts do not match point count")
        # New points must sort after every restored bucket.
        overview._seq = finite
        return overview

    def _ensure_width(self, step: int) -> None:
        # One power at a time: bucket i at the new width is the union of
        # 2i and 2i+1. That keeps ``last`` equal to a direct group-by at
        # the final width, replayed in append order.
        limit = self.max_buckets * self.width
        if step < limit:
            return
        while step >= self.max_buckets * self.width:
            self._double()

    def _double(self) -> None:
        grouped: dict[int, _Acc] = {}
        # Sort so pair (2i, 2i+1) merges left then right, not dict order.
        for index, acc in sorted(self._buckets.items()):
            dest = index // 2
            prev = grouped.get(dest)
            if prev is None:
                grouped[dest] = acc
            else:
                grouped[dest] = _merge_acc(prev, acc)
        self._buckets = grouped
        self.width *= 2

    def __repr__(self) -> str:
        return (
            f"Overview(width={self.width}, buckets={len(self._buckets)}, "
            f"points={self.point_count}, nonfinite={self.nonfinite})"
        )
