"""Resolve caller variable names and loss expressions without importing torch."""

from __future__ import annotations

import ast
import functools
import inspect
import linecache
import sys
import textwrap
import types
from typing import Any

from .component_tracking import sanitize_component_name

# Longest first so "_optimizer" is removed before "_optim".
_TRAILING_SUFFIXES = (
    "_optimizer",
    "_criterion",
    "_dataset",
    "_loader",
    "_model",
    "_optim",
    "_loss",
)
_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
)
_MISSING = object()


def infer_variable_name(
    obj: Any, start_frame: types.FrameType | None = None
) -> str | None:
    """Return the nearest non-agenticml variable name bound to ``obj``."""
    try:
        frame: Any = start_frame if start_frame is not None else sys._getframe(1)
    except ValueError:
        return None
    seen: set[int] = set()
    while frame is not None and id(frame) not in seen:
        seen.add(id(frame))
        try:
            internal = _is_agenticml_frame(frame)
            name = None if internal else _bound_name(frame, obj)
            nxt = getattr(frame, "f_back", None)
        except Exception:
            break
        if name is not None:
            return name
        frame = nxt
    return None


def normalize_component_name(raw: str | None) -> str:
    """Strip one trailing component suffix and sanitize the name."""
    if not isinstance(raw, str):
        return "default"
    text = raw.strip()
    for suffix in _TRAILING_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if not text.strip():
        return "default"
    return sanitize_component_name(text)


def resolve_loss_from_backward_frame(
    frame: types.FrameType,
    loss_tensor_name: str | None = None,
) -> dict[str, Any]:
    """Find the loss callable assigned in the frame that called ``backward()``."""
    try:
        return _resolve_loss(frame, loss_tensor_name)
    except Exception:
        return _failure_result(frame)


def _is_agenticml_frame(frame: Any) -> bool:
    try:
        module = frame.f_globals.get("__name__", "")
    except Exception:
        return False
    return isinstance(module, str) and module.startswith("agenticml")


def _bound_name(frame: Any, obj: Any) -> str | None:
    for attr in ("f_locals", "f_globals"):
        name = _mapping_name(getattr(frame, attr, None), obj)
        if name is not None:
            return name
    return None


def _mapping_name(mapping: Any, obj: Any) -> str | None:
    if mapping is None:
        return None
    try:
        items = list(mapping.items())
    except Exception:
        return None
    for key, value in items:
        if isinstance(key, str) and value is obj:
            return key
    return None


def _resolve_loss(frame: Any, loss_tensor_name: str | None) -> dict[str, Any]:
    filename, lineno = _frame_file_line(frame)
    parsed = _parse_frame(frame)
    if parsed is None or lineno is None:
        return _result(
            "inline_expression",
            None,
            _line_text(filename, lineno, None, 0),
            filename,
            lineno,
        )
    source, tree, offset = parsed
    found = _find_loss_expr(tree, lineno, offset, loss_tensor_name)
    if found is None:
        return _result(
            "inline_expression",
            None,
            _line_text(filename, lineno, source, offset),
            filename,
            lineno,
        )
    expr, line = found
    expression = _segment(source, expr)
    if expression is None:
        expression = _line_text(filename, line, source, offset)
    resolved = _callable_from_expr(expr, frame)
    if resolved is not None:
        return _result("ast_assignment", resolved, expression, filename, line)
    return _result("inline_expression", None, expression, filename, line)


def _failure_result(frame: Any) -> dict[str, Any]:
    filename, lineno = _frame_file_line(frame)
    return _result(
        "inline_expression",
        None,
        _line_text(filename, lineno, None, 0),
        filename,
        lineno,
    )


def _result(
    evidence: str,
    resolved: Any,
    expression: str | None,
    filename: str | None,
    line: int | None,
) -> dict[str, Any]:
    return {
        "evidence": evidence,
        "callable": resolved,
        "expression": expression,
        "file": filename,
        "line": line,
    }


def _frame_file_line(frame: Any) -> tuple[str | None, int | None]:
    filename = None
    lineno = None
    try:
        raw_name = getattr(getattr(frame, "f_code", None), "co_filename", None)
        if isinstance(raw_name, str) and raw_name:
            filename = raw_name
    except Exception:
        filename = None
    try:
        raw_line = getattr(frame, "f_lineno", None)
        if isinstance(raw_line, int) and raw_line >= 1:
            lineno = raw_line
    except Exception:
        lineno = None
    return filename, lineno


def _parse_frame(frame: Any) -> tuple[str, ast.AST, int] | None:
    filename, _lineno = _frame_file_line(frame)
    if filename:
        source = _cached_source(filename)
        if source:
            try:
                return source, ast.parse(source), 0
            except (SyntaxError, ValueError):
                pass
    code = None
    try:
        code = getattr(frame, "f_code", None)
    except Exception:
        return None
    if code is None:
        return None
    try:
        lines, start = inspect.getsourcelines(code)
    except (OSError, TypeError):
        return None
    source = textwrap.dedent("".join(lines))
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(start, int) or start < 1:
        start = 1
    return source, tree, start - 1


def _cached_source(filename: str) -> str | None:
    try:
        linecache.checkcache(filename)
        lines = linecache.getlines(filename)
    except Exception:
        return None
    if not lines:
        return None
    return "".join(lines)


def _find_loss_expr(
    tree: ast.AST,
    file_lineno: int,
    offset: int,
    loss_tensor_name: str | None,
) -> tuple[ast.expr, int] | None:
    scope = _enclosing_scope(tree, file_lineno, offset)
    if loss_tensor_name is not None:
        return _last_binding(scope, loss_tensor_name, file_lineno, offset)
    call = _backward_call(scope, file_lineno, offset)
    if call is None or not isinstance(call.func, ast.Attribute):
        return None
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        return _last_binding(scope, receiver.id, file_lineno, offset)
    if isinstance(receiver, ast.NamedExpr):
        line = _file_line(receiver, offset)
        if line is None:
            return None
        return receiver.value, line
    line = _file_line(receiver, offset)
    if line is None:
        return None
    return receiver, line


def _enclosing_scope(tree: ast.AST, file_lineno: int, offset: int) -> ast.AST:
    best: ast.AST = tree
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPE_NODES):
            continue
        start = _file_line(node, offset)
        end = _file_end(node, offset)
        if start is None or end is None or not (start <= file_lineno <= end):
            continue
        span = end - start
        if best_span is None or span <= best_span:
            best = node
            best_span = span
    return best


def _backward_call(scope: ast.AST, file_lineno: int, offset: int) -> ast.Call | None:
    best: ast.Call | None = None
    best_span: int | None = None
    for node in _iter_nodes(scope):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "backward":
            continue
        start = _file_line(node, offset)
        end = _file_end(node, offset)
        if start is None or end is None or not (start <= file_lineno <= end):
            continue
        span = end - start
        if best_span is None or span < best_span:
            best = node
            best_span = span
    return best


def _last_binding(
    scope: ast.AST, name: str, file_lineno: int, offset: int
) -> tuple[ast.expr, int] | None:
    found: tuple[ast.expr, int] | None = None
    for node in _iter_nodes(scope):
        value = _binding_value(node, name)
        if value is None:
            continue
        line = _file_line(node, offset)
        if line is None or line > file_lineno:
            continue
        if found is None or line >= found[1]:
            found = (value, line)
    return found


def _binding_value(node: ast.AST, name: str) -> ast.expr | None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if _target_binds(target, name):
                return node.value
        return None
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        if _target_binds(node.target, name):
            return node.value
        return None
    if (
        isinstance(node, ast.NamedExpr)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    ):
        return node.value
    return None


def _target_binds(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds(elt, name) for elt in target.elts)
    if isinstance(target, ast.Starred):
        return _target_binds(target.value, name)
    return False


def _iter_nodes(root: ast.AST):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, _SCOPE_NODES)
        ]
        stack.extend(reversed(children))


def _file_line(node: ast.AST, offset: int) -> int | None:
    lineno = getattr(node, "lineno", None)
    if not isinstance(lineno, int):
        return None
    return lineno + offset


def _file_end(node: ast.AST, offset: int) -> int | None:
    end = getattr(node, "end_lineno", None)
    if not isinstance(end, int):
        return _file_line(node, offset)
    return end + offset


def _segment(source: str, node: ast.AST) -> str | None:
    try:
        text = ast.get_source_segment(source, node)
    except Exception:
        return None
    if not text:
        return None
    stripped = text.strip()
    return stripped or None


def _line_text(
    filename: str | None,
    lineno: int | None,
    source: str | None,
    offset: int,
) -> str | None:
    if source is not None and lineno is not None:
        index = lineno - offset - 1
        lines = source.splitlines()
        if 0 <= index < len(lines):
            text = lines[index].strip()
            if text:
                return text
    if not filename or lineno is None:
        return None
    try:
        text = linecache.getline(filename, lineno).strip()
    except Exception:
        return None
    return text or None


def _callable_from_expr(expr: ast.expr, frame: Any) -> Any:
    try:
        if not isinstance(expr, ast.Call):
            return None
        resolved = _callable_from_call(expr, frame)
    except Exception:
        return None
    if _is_callable(resolved):
        return resolved
    return None


def _callable_from_call(call: ast.Call, frame: Any) -> Any:
    func = call.func
    if isinstance(func, ast.Call):
        return _partial_from_call(func, frame)
    return _resolve_callee(func, frame)


def _partial_from_call(call: ast.Call, frame: Any) -> Any:
    if _resolve_callee(call.func, frame) is not functools.partial:
        return None
    if not call.args:
        return None
    wrapped = _static_value(call.args[0], frame)
    if not _is_callable(wrapped):
        return None
    extra: list[Any] = []
    for arg in call.args[1:]:
        value = _static_value(arg, frame)
        if value is _MISSING:
            return wrapped
        extra.append(value)
    keywords: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            return wrapped
        value = _static_value(keyword.value, frame)
        if value is _MISSING:
            return wrapped
        keywords[keyword.arg] = value
    try:
        return functools.partial(wrapped, *extra, **keywords)
    except Exception:
        return wrapped


def _resolve_callee(expr: ast.expr, frame: Any) -> Any:
    if isinstance(expr, (ast.Name, ast.Attribute)):
        return _eval_expr(expr, frame)
    return _MISSING


def _eval_expr(expr: ast.expr, frame: Any) -> Any:
    if isinstance(expr, ast.Name):
        return _lookup(expr.id, frame)
    if isinstance(expr, ast.Attribute):
        base = _eval_expr(expr.value, frame)
        if base is _MISSING:
            return _MISSING
        try:
            return getattr(base, expr.attr)
        except Exception:
            return _MISSING
    return _MISSING


def _static_value(expr: ast.expr, frame: Any) -> Any:
    if isinstance(expr, ast.Constant):
        return expr.value
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.operand, ast.Constant):
        value = expr.operand.value
        if isinstance(expr.op, ast.USub):
            try:
                return -value
            except Exception:
                return _MISSING
        if isinstance(expr.op, ast.UAdd):
            return value
    if isinstance(expr, (ast.Name, ast.Attribute)):
        return _eval_expr(expr, frame)
    return _MISSING


def _lookup(name: str, frame: Any) -> Any:
    for attr in ("f_locals", "f_globals"):
        mapping = getattr(frame, attr, None)
        if mapping is None:
            continue
        try:
            if name in mapping:
                return mapping[name]
        except Exception:
            continue
    return _MISSING


def _is_callable(obj: Any) -> bool:
    if obj is None or obj is _MISSING:
        return False
    try:
        return callable(obj)
    except Exception:
        return False
