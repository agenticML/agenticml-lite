"""Import TensorBoard event files (``events.out.tfevents.*``) from a log directory.

Every directory under PATH (PATH itself included) that holds ``*tfevents*``
files is one run, named by its path relative to PATH as TensorBoard shows it.
The files are read in filename order with the standard library only: TFRecord
framing, a masked CRC-32C and the protobuf wire format (``_protobuf``).

Field numbers, checked against TensorFlow's ``event.proto``, ``summary.proto``,
``tensor.proto``, ``types.proto`` and TensorBoard's ``hparams/plugin_data.proto``
(the copies bundled with tensorboardX 2.6.5, and TensorBoard 2.21 for the
cross-check):

* ``Event``: ``wall_time`` 1 (double), ``step`` 2 (int64), ``graph_def`` 4,
  ``summary`` 5, ``meta_graph_def`` 9.
* ``Summary``: ``value`` 1 (repeated).
* ``Summary.Value``: ``tag`` 1, ``simple_value`` 2 (float),
  ``obsolete_old_style_histogram`` 3, ``image`` 4, ``histo`` 5, ``audio`` 6,
  ``tensor`` 8, ``metadata`` 9.
* ``SummaryMetadata``: ``plugin_data`` 1 -> ``PluginData`` (``plugin_name`` 1,
  ``content`` 2).
* ``Summary.Image``: ``height`` 1, ``width`` 2, ``colorspace`` 3,
  ``encoded_image_string`` 4.
* ``TensorProto``: ``dtype`` 1, ``tensor_shape`` 2 (``dim`` 2 -> ``size`` 1),
  ``tensor_content`` 4, ``float_val`` 5, ``double_val`` 6, ``int_val`` 7,
  ``string_val`` 8, ``int64_val`` 10, ``half_val`` 13.
* ``DataType``: ``DT_FLOAT`` 1, ``DT_DOUBLE`` 2, ``DT_INT32`` 3,
  ``DT_STRING`` 7, ``DT_INT64`` 9, ``DT_BFLOAT16`` 14, ``DT_HALF`` 19.
* ``HParamsPluginData``: ``version`` 1, ``session_start_info`` 3 ->
  ``SessionStartInfo.hparams`` 1 (``map<string, google.protobuf.Value>``,
  entry ``key`` 1, ``value`` 2); ``Value``: ``null_value`` 1,
  ``number_value`` 2 (double), ``string_value`` 3, ``bool_value`` 4.

Nothing in the source is changed. All input is untrusted: a record longer than
64 MiB is skipped with one warning, a record whose data checksum is wrong is
skipped (one warning per file), a short final record ends its file quietly,
and a message that cannot be decoded is skipped.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ImportedRun, ImportOptions, ImportSourceError
from . import _protobuf as pb
from ._crc32c import masked_crc32c
from ._media import sniff_image_type

logger = logging.getLogger("agenticml.import")

SOURCE = "tensorboard"

MAX_RECORD = 64 * 1024 * 1024  # a longer TFRecord is skipped with a warning
_HEADER = 12  # uint64 length + uint32 masked crc of the length
_MAX_UNIX = 253402300799  # 9999-12-31T23:59:59Z
_RECENT_SECONDS = 120  # "may still be writing" when the newest file changed this recently
_CHILD_MAX_BYTES = 1024 * 1024  # an add_hparams child directory is small
_CHILD_MAX_EVENTS = 10_000
_KEEP_LAST_MAX = 1024 * 1024  # keep the last record's bytes for the end-time check up to this size

DT_FLOAT, DT_DOUBLE, DT_INT32, DT_STRING, DT_INT64, DT_BFLOAT16, DT_HALF = 1, 2, 3, 7, 9, 14, 19

# What a hostile record can raise while it is decoded; the record is skipped and counted.
_BAD_RECORD = (pb.ProtobufError, struct.error, ValueError, TypeError, OverflowError, MemoryError)

# plugin_name -> label used in "skipped N <label> tags"
_KIND = {
    "histograms": "histogram",
    "audio": "audio",
    "text": "text",
    "pr_curves": "pr_curve",
    "images": "image",
    "mesh": "mesh",
    "custom_scalars": "custom_scalars",
}


def _blob(raw):
    """``raw`` itself when it is length-delimited bytes, else :class:`ProtobufError`.

    ``bytes(5)`` is five zero bytes, so a field sent with the wrong wire type
    must never reach ``bytes()`` or a slice unchecked.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise pb.ProtobufError("unexpected wire type for field")
    return raw


def _shortest_float32(number: float) -> float:
    """The shortest decimal that reads back as the same float32 (0.9, not 0.8999999761581421)."""
    if number != number or number in (float("inf"), float("-inf")):
        return number
    try:
        target = struct.pack("<f", number)
    except OverflowError:
        return number
    for digits in range(6, 10):
        candidate = float(f"{number:.{digits}g}")
        try:
            if struct.pack("<f", candidate) == target:
                return candidate
        except OverflowError:
            break
    return number


def _plausible(wall) -> bool:
    return isinstance(wall, float) and 0 < wall < _MAX_UNIX


# ---------------------------------------------------------------- framing


def _iter_records(path: Path, warn, *, check_crc: bool = True):
    """Yield ``(offset, payload)`` for each well-formed TFRecord in ``path``.

    A bad length checksum loses the framing, so the rest of the file is dropped
    with a warning. A bad data checksum skips that record. A record longer than
    ``MAX_RECORD`` is skipped (never read into memory). A short final record
    ends the file quietly.
    """
    bad_data = 0
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        pos = 0
        while True:
            header = handle.read(_HEADER)
            if len(header) < _HEADER:
                break
            length = int.from_bytes(header[:8], "little")
            if masked_crc32c(header[:8]) != int.from_bytes(header[8:], "little"):
                warn(f"{path.name}: bad record header at byte {pos}; the rest of the file is skipped")
                break
            end = pos + _HEADER + length + 4
            if length > MAX_RECORD:
                warn(f"{path.name}: skipped a record longer than 64 MiB at byte {pos}")
                if end > size:
                    break
                handle.seek(end)
                pos = end
                continue
            if end > size:
                break  # short final record: the file is still being written
            data = handle.read(length + 4)
            if len(data) < length + 4:
                break
            payload = memoryview(data)[:length]
            if check_crc and masked_crc32c(payload) != int.from_bytes(data[length:], "little"):
                bad_data += 1
            else:
                yield pos, payload
            pos = end
    if bad_data:
        warn(
            f"{path.name}: skipped {bad_data} record{'s' if bad_data != 1 else ''} "
            "with a wrong checksum"
        )


def _read_record_at(path: Path, offset: int):
    """The payload of the record at ``offset`` (already verified by an earlier pass)."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(offset)
        header = handle.read(_HEADER)
        if len(header) < _HEADER:
            return None
        length = int.from_bytes(header[:8], "little")
        if length > MAX_RECORD or offset + _HEADER + length > size:
            return None
        return handle.read(length)


# ---------------------------------------------------------------- decoding


def _event_wall_step(event: pb.Message) -> tuple[float, int]:
    wall = pb.as_double(event.last(1)) if 1 in event else 0.0
    step = pb.as_int64(event.last(2)) if 2 in event else 0
    return wall, step


def _tensor_dims(tensor: pb.Message) -> list[int] | None:
    raw = tensor.first(2)
    if raw is None:
        return []
    shape = tensor.child(raw)
    if 3 in shape and pb.as_bool(shape.last(3)):
        return None  # unknown rank
    dims = []
    for dim_raw in shape.get(2, ()):
        dim = shape.child(dim_raw)
        dims.append(pb.as_int64(dim.last(1)) if 1 in dim else 0)
    return dims


def _tensor_scalar(tensor: pb.Message) -> float | None:
    """The one number in a numeric ``TensorProto``, else ``None``."""
    dtype = tensor.last(1, 0)
    if not isinstance(dtype, int):
        return None
    dims = _tensor_dims(tensor)
    if dims is None or len(dims) > 8:
        return None
    count = 1
    for size in dims:
        count *= size
    if count != 1:
        return None
    content = tensor.last(4)
    if content is not None:
        blob = _blob(content)
        if len(blob) > 8:
            return None
        blob = bytes(blob)
        codes = {DT_FLOAT: "<f", DT_DOUBLE: "<d", DT_INT32: "<i", DT_INT64: "<q", DT_HALF: "<e"}
        if dtype == DT_BFLOAT16:
            if len(blob) != 2:
                return None
            return struct.unpack("<f", b"\x00\x00" + blob)[0]
        code = codes.get(dtype)
        if code is None or len(blob) != struct.calcsize(code):
            return None
        return float(struct.unpack(code, blob)[0])
    if dtype == DT_FLOAT:
        values = pb.repeated_floats(tensor.get(5, ()))
    elif dtype == DT_DOUBLE:
        values = pb.repeated_doubles(tensor.get(6, ()))
    elif dtype == DT_INT32:
        values = pb.repeated_int32(tensor.get(7, ()))
    elif dtype == DT_INT64:
        values = pb.repeated_int64(tensor.get(10, ()))
    elif dtype in (DT_HALF, DT_BFLOAT16):
        bits = [v & 0xFFFF for v in pb.repeated_varints(tensor.get(13, ()))]
        if dtype == DT_HALF:
            values = [struct.unpack("<e", struct.pack("<H", b))[0] for b in bits]
        else:
            values = [struct.unpack("<f", struct.pack("<I", b << 16))[0] for b in bits]
    else:
        return None
    if len(values) != 1:
        return None
    return float(values[0])


def _image_tensor_items(strings) -> list | None:
    """``[width, height, png, ...]`` from a TF2 ``images`` plugin tensor, else ``None``."""
    if len(strings) < 3:
        return None
    strings = [_blob(item) for item in strings]
    try:
        for dim in strings[:2]:
            int(bytes(dim).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    return list(strings[2:])


@dataclass
class _Extracted:
    scalars: list = field(default_factory=list)  # (tag, value)
    images: list = field(default_factory=list)  # (tag, bytes, content_type) when wanted
    has_image: bool = False
    bad_images: int = 0
    skipped: list = field(default_factory=list)  # (kind, tag)
    float32: set = field(default_factory=set)  # tags whose value came from a float32 simple_value
    hparams_tensor: bool = False  # a TF2 hparams summary (a string tensor) was seen


def _kind(plugin: str | None, default: str) -> str:
    if not plugin:
        return default
    return _KIND.get(plugin, plugin)


def _extract_value(val: pb.Message, plugins: dict, want_images: bool, res: _Extracted) -> None:
    tag = pb.as_str(val.last(1, b"")) if 1 in val else ""
    plugin = None
    meta_raw = val.last(9)
    if meta_raw is not None:
        meta = val.child(meta_raw)
        data_raw = meta.last(1)
        if data_raw is not None:
            name = pb.as_str(meta.child(data_raw).last(1, b""))
            if name:
                # TF2 writers may send the metadata with the first event of a tag only.
                plugins.setdefault(tag, name)
    plugin = plugins.get(tag)

    if 2 in val:
        res.scalars.append((tag, pb.as_float(val.last(2))))
        res.float32.add(tag)
    elif 4 in val:
        image = val.child(val.last(4))
        raw = image.last(4)
        if raw is None:
            return
        raw = _blob(raw)
        res.has_image = True
        if want_images:
            content_type = sniff_image_type(raw)
            if content_type is None:
                res.bad_images += 1
            else:
                res.images.append((tag, bytes(raw), content_type))
    elif 8 in val:
        tensor = val.child(val.last(8))
        dtype = tensor.last(1, 0)
        if dtype == DT_STRING:
            if plugin == "hparams":
                # the TF2 hparams writer: the values are imported as config, not skipped
                res.hparams_tensor = True
                return
            items = _image_tensor_items(tensor.get(8, ())) if plugin in (None, "images") else None
            if items is None:
                res.skipped.append((_kind(plugin, "text"), tag))
                return
            res.has_image = True
            if want_images:
                for raw in items:
                    content_type = sniff_image_type(raw)
                    if content_type is None:
                        res.bad_images += 1
                    else:
                        res.images.append((tag, bytes(raw), content_type))
            return
        number = _tensor_scalar(tensor) if plugin in (None, "scalars") else None
        if number is None:
            res.skipped.append((_kind(plugin, "tensor"), tag))
        else:
            res.scalars.append((tag, number))
    elif 5 in val or 3 in val:
        res.skipped.append(("histogram", tag))
    elif 6 in val:
        res.skipped.append(("audio", tag))
    # else: metadata only (the hparams plugin writes values like this)


def _extract(event: pb.Message, plugins: dict, want_images: bool) -> _Extracted:
    res = _Extracted()
    if 4 in event or 9 in event:
        res.skipped.append(("graph", "graph"))
    for raw in event.get(5, ()):
        summary = event.child(raw)
        for value_raw in summary.get(1, ()):
            _extract_value(summary.child(value_raw), plugins, want_images, res)
    return res


def _plain_number(number: float):
    if number == number and abs(number) < 2**53 and number == int(number):
        return int(number)
    return number


def _hparams_from_event(event: pb.Message) -> dict:
    """Hyperparameters from the ``hparams`` plugin's ``session_start_info`` values."""
    found: dict = {}
    for raw in event.get(5, ()):
        summary = event.child(raw)
        for value_raw in summary.get(1, ()):
            val = summary.child(value_raw)
            meta_raw = val.last(9)
            if meta_raw is None:
                continue
            meta = val.child(meta_raw)
            data_raw = meta.last(1)
            if data_raw is None:
                continue
            plugin_data = meta.child(data_raw)
            if pb.as_str(plugin_data.last(1, b"")) != "hparams":
                continue
            content = plugin_data.last(2)
            if content is None:
                continue
            outer = pb.decode(_blob(content))
            info_raw = outer.last(3)
            if info_raw is None:
                continue
            info = outer.child(info_raw)
            for entry_raw in info.get(1, ()):
                entry = info.child(entry_raw)
                key = pb.as_str(entry.last(1, b""))
                value_raw2 = entry.last(2)
                if value_raw2 is None:
                    continue
                value = entry.child(value_raw2)
                if 4 in value:
                    found[key] = pb.as_bool(value.last(4))
                elif 3 in value:
                    found[key] = pb.as_str(value.last(3))
                elif 2 in value:
                    found[key] = _plain_number(pb.as_double(value.last(2)))
                elif 1 in value:
                    found[key] = None
    return found


# ---------------------------------------------------------------- scanning


@dataclass
class _Prescan:
    start: float | None = None
    end: float | None = None
    config: dict = field(default_factory=dict)
    size: int = 0
    records: int = 0


def _prescan(files: list[Path], warn) -> _Prescan:
    """Start and end wall times and hparams, without decoding every event.

    Records are read but only their first field is looked at, plus a byte
    search for ``hparams`` to find the hyperparameter record; this is about as
    fast as the disk.
    """
    info = _Prescan()
    last = None  # (wall, data, length)
    for path in files:
        try:
            handle = open(path, "rb")
        except OSError as exc:
            warn(f"{path.name}: cannot read ({exc})")
            continue
        with handle:
            size = os.fstat(handle.fileno()).st_size
            info.size += size
            pos = 0
            while True:
                header = handle.read(_HEADER)
                if len(header) < _HEADER:
                    break
                length = int.from_bytes(header[:8], "little")
                if masked_crc32c(header[:8]) != int.from_bytes(header[8:], "little"):
                    break
                end = pos + _HEADER + length + 4
                if end > size:
                    break
                if length > MAX_RECORD:
                    handle.seek(end)
                    pos = end
                    continue
                data = handle.read(length + 4)
                pos = end
                if len(data) < length + 4:
                    break
                info.records += 1
                wall = None
                if length >= 9 and data[0] == 0x09:
                    wall = struct.unpack_from("<d", data, 1)[0]
                else:
                    try:
                        event = pb.decode(memoryview(data)[:length])
                        wall = _event_wall_step(event)[0]
                    except _BAD_RECORD:
                        wall = None
                crc = int.from_bytes(data[length:], "little")
                if _plausible(wall):
                    if info.start is None and masked_crc32c(memoryview(data)[:length]) == crc:
                        info.start = wall
                    if length <= _KEEP_LAST_MAX:
                        last = (wall, data, length)
                if b"hparams" in data and masked_crc32c(memoryview(data)[:length]) == crc:
                    try:
                        info.config.update(_hparams_from_event(pb.decode(memoryview(data)[:length])))
                    except _BAD_RECORD:
                        pass
    if last is not None:
        wall, data, length = last
        if masked_crc32c(memoryview(data)[:length]) == int.from_bytes(data[length:], "little"):
            info.end = wall
    return info


def _classify_child(files: list[Path]) -> dict | None:
    """Metrics of an ``add_hparams`` child directory, or ``None`` when it is a real run.

    A child holds only the hparams record and the ``add_hparams`` metric
    scalars, each tag once. Anything else (images, other summaries, a tag
    logged twice, many events) makes it a run of its own.
    """
    metrics: dict[str, float] = {}
    events = 0
    plugins: dict = {}
    for path in files:
        try:
            for _offset, payload in _iter_records(path, lambda _msg: None):
                events += 1
                if events > _CHILD_MAX_EVENTS:
                    return None
                try:
                    event = pb.decode(payload)
                    res = _extract(event, plugins, False)
                except _BAD_RECORD:
                    return None
                if res.has_image or res.skipped or res.hparams_tensor:
                    return None
                for tag, value in res.scalars:
                    if tag in metrics:
                        return None
                    metrics[tag] = _shortest_float32(value) if tag in res.float32 else value
        except OSError:
            return None
    return metrics


class _RunStream:
    """The lazy ``points`` and ``media`` of one run, sharing one scan.

    The points pass reads every record once and remembers where the images
    are, so the media pass seeks to those records instead of reading the logs
    again. Used out of order (media before points), the media pass scans for
    itself.
    """

    def __init__(self, name: str, files: list[Path], options: ImportOptions):
        self.name = name
        self.files = files
        self.options = options
        self.image_spots: list[tuple[int, int]] = []
        self.complete = False

    def warn(self, message: str) -> None:
        self.options.warn(f"{self.name}: {message}")

    def points(self) -> Iterator[tuple[float, int, dict]]:
        plugins: dict = {}
        skipped: dict[str, set] = {}
        bad_events = 0
        for index, path in enumerate(self.files):
            try:
                for offset, payload in _iter_records(path, self.warn):
                    try:
                        event = pb.decode(payload)
                        wall, step = _event_wall_step(event)
                        res = _extract(event, plugins, False)
                    except _BAD_RECORD:
                        bad_events += 1
                        continue
                    for kind, tag in res.skipped:
                        skipped.setdefault(kind, set()).add(tag)
                    if res.has_image:
                        self.image_spots.append((index, offset))
                    if not res.scalars:
                        continue
                    batch: dict[str, float] = {}
                    for tag, value in res.scalars:
                        if tag in batch:
                            yield wall, step, batch
                            batch = {}
                        batch[tag] = value
                    yield wall, step, batch
            except OSError as exc:
                self.warn(f"{path.name}: cannot read ({exc}); the rest of it is skipped")
        self.complete = True
        if bad_events:
            self.warn(
                f"skipped {bad_events} event{'s' if bad_events != 1 else ''} that could not be decoded"
            )
        if skipped:
            parts = [
                f"{len(tags)} {kind} tag{'s' if len(tags) != 1 else ''}"
                for kind, tags in sorted(skipped.items())
            ]
            self.warn("skipped " + ", ".join(parts))

    def _media_events(self) -> Iterator[tuple[float, int, _Extracted]]:
        plugins: dict = {}
        if self.complete:
            for index, offset in self.image_spots:
                try:
                    payload = _read_record_at(self.files[index], offset)
                    if payload is None:
                        continue
                    event = pb.decode(payload)
                    wall, step = _event_wall_step(event)
                    yield wall, step, _extract(event, plugins, True)
                except (OSError, *_BAD_RECORD):
                    continue
            return
        for path in self.files:
            try:
                for _offset, payload in _iter_records(path, lambda _msg: None):
                    try:
                        event = pb.decode(payload)
                        wall, step = _event_wall_step(event)
                        res = _extract(event, plugins, True)
                    except _BAD_RECORD:
                        continue
                    if res.images or res.bad_images:
                        yield wall, step, res
            except OSError:
                continue

    def media(self) -> Iterator[tuple[float, int, str, bytes, str, str | None]]:
        bad = 0
        for wall, step, res in self._media_events():
            bad += res.bad_images
            for tag, data, content_type in res.images:
                yield wall, step, tag, data, content_type, None
        if bad:
            self.warn(f"skipped {bad} image{'s' if bad != 1 else ''} in an unsupported format")


# ---------------------------------------------------------------- discovery


def _find_run_dirs(top: Path, options: ImportOptions) -> dict[Path, list[Path]]:
    def on_error(exc: OSError) -> None:
        options.warn(f"cannot read {exc.filename}: {exc.strerror or exc}")

    found: dict[Path, list[Path]] = {}
    for directory, dirnames, filenames in os.walk(top, onerror=on_error):
        dirnames.sort()
        files = sorted(
            Path(directory, name)
            for name in filenames
            if "tfevents" in name and os.path.isfile(os.path.join(directory, name))
        )
        if files:
            found[Path(directory)] = files
    return found


def _run_name(directory: Path, top: Path) -> str:
    if directory == top:
        return top.name or "tensorboard"
    return directory.relative_to(top).as_posix()


def discover(path: str, options: ImportOptions) -> Iterator[ImportedRun]:
    """One :class:`ImportedRun` per directory under ``path`` that holds event files."""
    top = Path(path)
    if not top.is_dir():
        raise ImportSourceError(
            f"{path} is not a directory; give the log directory that holds "
            "`events.out.tfevents.*` files"
        )
    top = top.resolve()
    dirs = _find_run_dirs(top, options)
    if not dirs:
        raise ImportSourceError(f"no `events.out.tfevents.*` files under {path}")

    names = {directory: _run_name(directory, top) for directory in dirs}
    scans: dict[Path, _Prescan] = {}
    for directory, files in dirs.items():
        scan = _prescan(files, lambda msg, n=names[directory]: options.warn(f"{n}: {msg}"))
        if scan.start is None:
            options.warn(f"{names[directory]}: no readable events; skipped")
            continue
        scans[directory] = scan
    if not scans:
        raise ImportSourceError(f"no readable events in the `tfevents` files under {path}")

    # add_hparams writes a child directory with only the hparams and the metric
    # scalars; fold it into its parent run instead of making a run.
    candidates: dict[Path, dict] = {}
    for directory, scan in scans.items():
        if (
            directory != top
            and scan.config
            and scan.size <= _CHILD_MAX_BYTES
            and directory.parent in scans
        ):
            metrics = _classify_child(dirs[directory])
            if metrics is not None:
                candidates[directory] = metrics
    merged: dict[Path, list[Path]] = {}
    # oldest first, so "later values win" means later in time, not later by name
    for directory in sorted(candidates, key=lambda d: (scans[d].start, d.name)):
        parent = directory.parent
        if parent in candidates:
            continue  # a chain of children: keep them as runs
        merged.setdefault(parent, []).append(directory)

    now = time.time()
    location = str(top)
    project = top.name or "tensorboard"
    runs: list[ImportedRun] = []
    for directory, scan in scans.items():
        if any(directory in children for children in merged.values()):
            continue
        name = names[directory]
        config = dict(scan.config)
        summary: dict = {}
        children = merged.get(directory, [])
        for child in children:
            config.update(scans[child].config)
            summary.update(candidates[child])
        if len(children) > 1:
            options.warn(
                f"{name}: {len(children)} add_hparams directories were merged into this run; "
                "later values win"
            )
        try:
            newest = max(os.stat(f).st_mtime for f in dirs[directory])
        except OSError:
            newest = 0.0
        if now - newest < _RECENT_SECONDS:
            options.warn(
                f"{name}: an event file changed in the last 2 minutes; the run may still be writing"
            )
        start = datetime.fromtimestamp(scan.start, timezone.utc)
        end = None
        if scan.end is not None:
            end = datetime.fromtimestamp(max(scan.end, scan.start), timezone.utc)
        stream = _RunStream(name, dirs[directory], options)
        runs.append(
            ImportedRun(
                source=SOURCE,
                location=location,
                source_run_id=name,
                name=name,
                project=project,
                started_at=start,
                ended_at=end,
                status="finished",
                source_status=None,
                config=config,
                summary=summary,
                tags=[],
                points=stream.points(),
                media=stream.media(),
            )
        )
    return iter(runs)
