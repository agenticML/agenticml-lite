"""CLI commands that edit a run after it has ended."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .launch import NonFiniteJSONError, parse_config_pair, parse_json_value
from .store.layout import RunLockedError


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fail(command, message, code=1):
    print(f"agenticml {command}: {message}", file=sys.stderr)
    return code


def _lock_message(command, run_id):
    if command == "edit":
        return f"run {run_id} is still running; edit it from the script"
    return f"run {run_id} is still running"


def _amend_fail(command, run_id, root, exc):
    if isinstance(exc, FileNotFoundError):
        return _fail(command, f"no run {run_id!r} in {root}")
    if isinstance(exc, RunLockedError):
        return _fail(command, _lock_message(command, run_id))
    return _fail(command, str(exc))


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join("" if item is None else str(item) for item in value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _field_changed(old, new, key) -> bool:
    if (key in old) != (key in new):
        return True
    return _canon(old.get(key)) != _canon(new.get(key))


def _parse_json_object(raw, option):
    try:
        value = parse_json_value(raw, option)
    except NonFiniteJSONError:
        raise
    except ValueError:
        raise ValueError(f"{option} must be a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError(f"{option} must be a JSON object")
    return value


def _edit_has_options(args) -> bool:
    return any((
        args.tag,
        args.untag,
        args.notes is not None,
        args.group is not None,
        args.job_type is not None,
        args.config,
        args.summary,
        args.unset_summary,
    ))


def cmd_edit(args):
    from .store.layout import resolve_root
    from .store.writer import RunWriter

    if not _edit_has_options(args):
        return _fail(
            "edit",
            "at least one of --tag, --untag, --notes, --group, --job-type, "
            "--config, --summary, --unset-summary is required",
            2,
        )
    if any(tag == "" for tag in args.tag or []):
        return _fail("edit", "--tag must not be empty", 2)
    if args.group is not None and args.group == "":
        return _fail("edit", "--group cannot be cleared", 2)
    if args.job_type is not None and args.job_type == "":
        return _fail("edit", "--job-type cannot be cleared", 2)
    try:
        config_pairs = [parse_config_pair(raw, "--config") for raw in args.config or []]
        summary_pairs = [parse_config_pair(raw, "--summary") for raw in args.summary or []]
    except ValueError as exc:
        return _fail("edit", str(exc), 2)

    root = resolve_root(args.dir)
    run_id = args.run_id
    try:
        writer = RunWriter.amend(root, run_id)
    except (OSError, ValueError) as exc:
        return _amend_fail("edit", run_id, root, exc)
    try:
        try:
            writer._reject_pruned()
        except ValueError:
            return _fail(
                "edit",
                f"run {run_id} was pruned after sync; edits would not reach the server",
            )
        meta = writer._meta
        changed = {}
        old_values = {}
        config_lines = []
        summary_lines = []

        if args.tag or args.untag:
            original = list(meta["tags"]) if isinstance(meta.get("tags"), list) else []
            tags = list(original)
            for tag in args.tag or []:
                if tag not in tags:
                    tags.append(tag)
            for tag in args.untag or []:
                tags = [item for item in tags if item != tag]
            if tags != original:
                changed["tags"] = tags
                old_values["tags"] = original

        if args.notes is not None and (args.notes or "") != (meta.get("notes") or ""):
            changed["notes"] = args.notes
            old_values["notes"] = meta.get("notes")

        if args.group is not None and args.group != meta.get("group"):
            changed["group"] = args.group
            old_values["group"] = meta.get("group")

        if args.job_type is not None and args.job_type != meta.get("job_type"):
            changed["job_type"] = args.job_type
            old_values["job_type"] = meta.get("job_type")

        current_summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
        if args.summary or args.unset_summary:
            summary = dict(current_summary)
            for key, value in summary_pairs:
                summary[key] = value
            for key in args.unset_summary or []:
                if key not in summary:
                    print(f"agenticml edit: summary key {key!r} is not set", file=sys.stderr)
                else:
                    del summary[key]
            if _canon(summary) != _canon(current_summary):
                changed["summary"] = summary
                old_values["summary"] = dict(current_summary)

        current_config = meta.get("config") if isinstance(meta.get("config"), dict) else {}
        config = None
        if config_pairs:
            config = dict(current_config)
            for key, value in config_pairs:
                config[key] = value
            if _canon(config) == _canon(current_config):
                config = None

        if not changed and config is None:
            print("nothing to change")
            return 0

        if config is not None:
            writer.set_config(config)
            keys = {key for key, _value in config_pairs}
            for key in keys:
                if _field_changed(current_config, config, key):
                    config_lines.append(f"config.{key}: {_fmt(config.get(key))}")

        if changed:
            writer.record_meta_update(changed)
            writer.set_meta_fields(**changed)
            if "tags" in changed:
                print(f"tags: {_fmt(old_values['tags'])} -> {_fmt(changed['tags'])}")
            if "notes" in changed:
                print(f"notes: {_fmt(old_values['notes'])} -> {_fmt(changed['notes'])}")
            if "group" in changed:
                print(f"group: {_fmt(old_values['group'])} -> {_fmt(changed['group'])}")
            if "job_type" in changed:
                print(f"job_type: {_fmt(old_values['job_type'])} -> {_fmt(changed['job_type'])}")
            if "summary" in changed:
                old_summary = old_values["summary"]
                new_summary = changed["summary"]
                for key in sorted(set(old_summary) | set(new_summary), key=str):
                    if _field_changed(old_summary, new_summary, key):
                        if key in new_summary:
                            summary_lines.append(f"summary.{key}: {_fmt(new_summary[key])}")
                        else:
                            summary_lines.append(f"summary.{key}:")
        for line in config_lines:
            print(line)
        for line in summary_lines:
            print(line)
        return 0
    except (OSError, ValueError, TypeError) as exc:
        return _fail("edit", str(exc))
    finally:
        writer.close()


def cmd_artifact(args):
    from .store.layout import resolve_root
    from .store.writer import RunWriter

    if args.metadata is not None:
        try:
            metadata = _parse_json_object(args.metadata, "--metadata")
        except ValueError as exc:
            return _fail("artifact", str(exc), 2)
    else:
        metadata = {}

    path = Path(args.path)
    if path.is_dir():
        return _fail("artifact", f"{path} is a directory")
    if not path.is_file():
        return _fail("artifact", f"no such file: {path}")

    root = resolve_root(args.dir)
    run_id = args.run_id
    try:
        writer = RunWriter.amend(root, run_id)
    except (OSError, ValueError) as exc:
        return _amend_fail("artifact", run_id, root, exc)
    try:
        try:
            writer._reject_pruned()
        except ValueError:
            return _fail(
                "artifact",
                f"run {run_id} was pruned after sync; edits would not reach the server",
            )
        name = args.name or path.stem
        record = writer.add_artifact(path, name, args.type, metadata, copy=bool(args.copy))
        how = "copied" if record.get("copy") else "by reference"
        print(f"recorded artifact {record['name']} v{record['version']} ({how}) in {run_id}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        return _fail("artifact", str(exc))
    finally:
        writer.close()


def cmd_finish(args):
    import time

    from .store.layout import resolve_root
    from .store.reader import RunReader, _effective_status
    from .store.writer import RunWriter

    root = resolve_root(args.dir)
    run_id = args.run_id
    try:
        reader = RunReader(root, run_id)
        old = _effective_status(
            reader.meta(), str(reader.run_dir / "meta.json"), str(reader.run_dir), time.time(),
        )
    except FileNotFoundError:
        return _fail("finish", f"no run {run_id!r} in {root}")
    except (OSError, ValueError) as exc:
        return _fail("finish", str(exc))

    try:
        writer = RunWriter.amend(root, run_id)
    except (OSError, ValueError) as exc:
        return _amend_fail("finish", run_id, root, exc)
    try:
        try:
            writer._reject_pruned()
        except ValueError:
            return _fail(
                "finish",
                f"run {run_id} was pruned after sync; its status cannot be changed",
            )
        status = args.status
        if writer._meta.get("status") == status:
            print(f"run {run_id} is already {status}")
            return 0
        summary = writer._meta.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        writer.finish(status, summary)
        print(f"{run_id}: {old} -> {status}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        return _fail("finish", str(exc))
    finally:
        writer.close()


def cmd_note(args):
    from .store.layout import resolve_root
    from .store.notes import NoteConflict, read_note, write_note

    root = resolve_root(args.dir)
    run_id = args.run_id
    replace = args.note_text is not None or args.note_file is not None or args.clear
    text = None
    if args.note_file is not None:
        try:
            if args.note_file == "-":
                text = sys.stdin.read()
            else:
                text = Path(args.note_file).read_text(encoding="utf-8")
        except OSError as exc:
            return _fail("note", f"cannot read {args.note_file}: {exc}")
    try:
        if not replace:
            note = read_note(root, run_id)
            source = note.get("source")
            if source == "none":
                return 0
            text = note.get("text") or ""
            if text:
                print(text, end="" if text.endswith("\n") else "\n")
            if source == "launch":
                print("(launch note from agenticml.init)", file=sys.stderr)
            return 0
        if args.clear:
            text = ""
        elif args.note_file is None:
            text = args.note_text
        current = read_note(root, run_id)
        write_note(root, run_id, text, current["revision"])
        return 0
    except FileNotFoundError:
        return _fail("note", f"no run {run_id!r} in {root}")
    except NoteConflict:
        return _fail("note", "note changed while saving; try again")
    except (OSError, ValueError) as exc:
        return _fail("note", str(exc))
