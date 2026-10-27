"""Overview buckets match a direct group-by, including after width doubles.

Brute force assigns each finite point to ``step // W_final`` in append order.
The streaming merge has to land on the same buckets for any chunk size and
any step order. Values here are small integers so the float64 sum is exact.
"""

from __future__ import annotations

import math
import random
import struct

import pytest

from agenticml.store.overview import MAGIC, Overview, _BUCKET, _HEADER


def brute(points, max_buckets):
    parsed = []
    finite_steps = []
    nonfinite = 0
    for step, value in points:
        value = float(value)
        parsed.append((step, value))
        if math.isfinite(value):
            finite_steps.append(step)
        else:
            nonfinite += 1
    width = 1
    if finite_steps:
        max_step = max(finite_steps)
        while max_step >= max_buckets * width:
            width *= 2
    buckets = {}
    seq = 0
    for step, value in parsed:
        if not math.isfinite(value):
            continue
        seq += 1
        index = step // width
        bucket = buckets.get(index)
        if bucket is None:
            buckets[index] = {
                "step_min": step,
                "step_max": step,
                "vmin": value,
                "vmax": value,
                "vsum": value,
                "count": 1,
                "last": value,
                "seq": seq,
            }
        else:
            bucket["step_min"] = min(bucket["step_min"], step)
            bucket["step_max"] = max(bucket["step_max"], step)
            bucket["vmin"] = min(bucket["vmin"], value)
            bucket["vmax"] = max(bucket["vmax"], value)
            bucket["vsum"] += value
            bucket["count"] += 1
            bucket["last"] = value
            bucket["seq"] = seq
    return width, nonfinite, len(parsed), buckets


def feed(points, max_buckets, seed):
    overview = Overview(max_buckets=max_buckets)
    rng = random.Random(seed)
    index = 0
    while index < len(points):
        count = rng.randint(1, 13)
        overview.add_many(points[index : index + count])
        index += count
    return overview


def assert_matches(overview, points, max_buckets, check_seq=True):
    width, nonfinite, point_count, buckets = brute(points, max_buckets)
    assert overview.width == width
    assert overview.nonfinite == nonfinite
    assert overview.point_count == point_count
    assert overview.width & (overview.width - 1) == 0
    got = overview.buckets()
    assert len(got) == len(buckets)
    assert len(got) <= max_buckets
    for bucket in got:
        assert bucket.index < max_buckets
        assert bucket.step_min // overview.width == bucket.index
        assert bucket.step_max // overview.width == bucket.index
        expected = buckets[bucket.index]
        assert bucket.step_min == expected["step_min"]
        assert bucket.step_max == expected["step_max"]
        assert bucket.vmin == expected["vmin"]
        assert bucket.vmax == expected["vmax"]
        assert bucket.vsum == expected["vsum"]
        assert bucket.count == expected["count"]
        assert bucket.last == expected["last"]
        if check_seq:
            assert bucket.seq == expected["seq"]


def test_single_bucket_collapses_every_step():
    points = [(5, 1.0), (0, 2.0), (9, float("nan")), (3, -4.0), (1, float("inf"))]
    overview = Overview(max_buckets=1)
    overview.add_many(points)
    assert_matches(overview, points, 1)
    assert len(overview.buckets()) == 1
    assert overview.buckets()[0].last == -4.0
    assert overview.nonfinite == 2


def test_traced_double_keeps_append_order_last():
    # max_buckets=2 forces a merge before the later points land.
    points = [(0, 10.0), (1, 20.0), (0, 30.0), (2, 40.0), (4, 50.0)]
    overview = feed(points, max_buckets=2, seed=1)
    assert_matches(overview, points, 2)


def test_random_orders_and_chunks_match_brute_force():
    rng = random.Random(7)
    base = []
    for _ in range(3000):
        base.append((rng.randrange(0, 80_000), rng.randrange(-50, 50)))
    base[5] = (base[5][0], float("nan"))
    base[6] = (base[6][0], float("inf"))
    base[7] = (base[7][0], float("-inf"))
    for trial in range(4):
        points = list(base)
        rng.shuffle(points)
        overview = feed(points, max_buckets=128, seed=100 + trial)
        assert_matches(overview, points, 128)
        restored = Overview.from_bytes(overview.to_bytes())
        assert_matches(restored, points, 128, check_seq=False)
        extended = list(points) + [(999_999, 7)]
        overview.add(999_999, 7)
        assert_matches(overview, extended, 128)


def test_bytes_round_trip_layout():
    empty = Overview().to_bytes()
    assert empty[:4] == MAGIC
    assert len(empty) == 40
    overview = Overview()
    overview.add(1, 2.0)
    overview.add(3, float("nan"))
    blob = overview.to_bytes()
    assert len(blob) == 40 + 36
    restored = Overview.from_bytes(blob)
    assert restored.point_count == 2
    assert restored.nonfinite == 1
    assert restored.buckets()[0].vmin == 2.0
    with pytest.raises(ValueError):
        Overview.from_bytes(b"nope")
    with pytest.raises(ValueError):
        Overview.from_bytes(b"XXXX" + b"\x00" * 40)


def test_downsample_preserves_counts_and_span():
    overview = Overview()
    for step in range(500):
        overview.add(step, float(step))
    full = overview.buckets()
    assert len(full) == 500
    down = overview.downsample(10)
    assert len(down) == 10
    assert sum(bucket.count for bucket in down) == 500
    assert down[0].step_min == full[0].step_min
    assert down[-1].step_max == full[-1].step_max
    assert down[0].vmin == min(bucket.vmin for bucket in full[: len(full) // 10 or 1])
    assert overview.downsample(len(full)) == full
    with pytest.raises(ValueError):
        overview.downsample(0)


def test_from_bytes_rejects_nan_vmin_and_unsorted_buckets():
    overview = Overview()
    overview.add(0, 1.0)
    overview.add(2, 3.0)
    good = overview.to_bytes()
    assert Overview.from_bytes(good).point_count == 2
    nan_vmin = bytearray(good)
    struct.pack_into("<f", nan_vmin, _HEADER.size + 16, float("nan"))
    with pytest.raises(ValueError):
        Overview.from_bytes(bytes(nan_vmin))
    backwards = bytearray(good)
    struct.pack_into("<f", backwards, _HEADER.size + 16, 5.0)
    struct.pack_into("<f", backwards, _HEADER.size + 20, 1.0)
    with pytest.raises(ValueError):
        Overview.from_bytes(bytes(backwards))
    first = good[_HEADER.size : _HEADER.size + _BUCKET.size]
    second = good[_HEADER.size + _BUCKET.size : _HEADER.size + 2 * _BUCKET.size]
    swapped = good[: _HEADER.size] + second + first
    with pytest.raises(ValueError):
        Overview.from_bytes(swapped)


def test_reject_old_overview_version():
    overview = Overview()
    overview.add(0, 1.0)
    blob = bytearray(overview.to_bytes())
    struct.pack_into("<H", blob, 4, 1)
    with pytest.raises(ValueError, match="unsupported overview version 1"):
        Overview.from_bytes(bytes(blob))


def test_bad_steps_and_width_limit():
    overview = Overview()
    with pytest.raises(TypeError):
        overview.add(1.5, 1.0)
    with pytest.raises(TypeError):
        overview.add(True, 1.0)
    with pytest.raises(ValueError):
        overview.add(-1, 1.0)
    with pytest.raises(ValueError):
        Overview(max_buckets=0)


def test_overview_directed_rounding():
    overview = Overview()
    overview.add(0, 0.1)
    b = overview.to_bytes()
    restored = Overview.from_bytes(b)
    bucket = restored.buckets()[0]
    assert bucket.vmin <= 0.1 <= bucket.vmax
    for val in [-0.1, -1.0, -1e-5, 1e-5, 0.3, 0.7]:
        ov = Overview()
        ov.add(0, val)
        res = Overview.from_bytes(ov.to_bytes()).buckets()[0]
        assert res.vmin <= val <= res.vmax
