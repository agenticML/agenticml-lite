"""Tests for caller-frame name and loss resolution."""

from __future__ import annotations

import functools
import sys

from agenticml._frame_inspect import (
    infer_variable_name,
    normalize_component_name,
    resolve_loss_from_backward_frame,
)
from agenticml.auto_tracking import AutoTracker


class _LossTensor:
    def backward(self):
        return sys._getframe(1)


def test_infer_variable_name_finds_local():
    sentinel = object()
    assert infer_variable_name(sentinel) == "sentinel"


def test_infer_variable_name_skips_agenticml_frames():
    sentinel = object()

    def wrapped():
        return infer_variable_name(sentinel)

    assert wrapped() == "sentinel"


def test_normalize_component_name_strips_one_suffix():
    assert normalize_component_name("train_loader") == "train"
    assert normalize_component_name("opt_optimizer") == "opt"
    assert normalize_component_name("sched_optim") == "sched"
    assert normalize_component_name("pair_loss") == "pair"
    assert normalize_component_name("main_criterion") == "main"
    assert normalize_component_name("val_dataset") == "val"
    assert normalize_component_name("student_model") == "student"
    assert normalize_component_name("criterion") == "criterion"
    assert normalize_component_name(None) == "default"
    assert normalize_component_name("   ") == "default"


def test_claim_name_dedupes_per_kind():
    class _Run:
        _tracked = {("model", 1): "default"}

    tracker = AutoTracker(_Run())
    assert tracker._claim_name("model", "default") == "default_2"
    assert tracker._claim_name("model", "default") == "default_3"
    assert tracker._claim_name("optimizer", "default") == "default"


def _resolve_calling(train):
    frame = train()
    return resolve_loss_from_backward_frame(frame)


def test_resolve_assigned_callable():
    def criterion(pred, target):
        return _LossTensor()

    def train():
        pred, target = 1, 2
        loss = criterion(pred, target)
        return loss.backward()

    resolved = _resolve_calling(train)
    assert resolved["evidence"] == "ast_assignment"
    assert resolved["callable"] is criterion
    assert resolved["expression"] == "criterion(pred, target)"
    assert resolved["line"] is not None
    assert resolved["file"]


def test_resolve_attribute_callable():
    class F:
        @staticmethod
        def cross_entropy(pred, target):
            return _LossTensor()

    def train():
        pred, target = 1, 2
        loss = F.cross_entropy(pred, target)
        return loss.backward()

    resolved = _resolve_calling(train)
    assert resolved["evidence"] == "ast_assignment"
    assert resolved["callable"] is F.cross_entropy


def test_resolve_partial_call():
    def mse(pred, target, weight=1):
        return _LossTensor()

    def train():
        pred, target = 1, 2
        loss = functools.partial(mse, weight=2)(pred, target)
        return loss.backward()

    resolved = _resolve_calling(train)
    assert resolved["evidence"] == "ast_assignment"
    assert isinstance(resolved["callable"], functools.partial)
    assert resolved["callable"].func is mse
    assert resolved["callable"].keywords == {"weight": 2}


def test_resolve_inline_expression():
    class _Num:
        def __sub__(self, other):
            return self

        def __pow__(self, power):
            return _LossTensor()

    def train():
        pred, target = _Num(), _Num()
        loss = ((pred - target) ** 2)
        return loss.backward()

    resolved = _resolve_calling(train)
    assert resolved["evidence"] == "inline_expression"
    assert resolved["callable"] is None
    assert resolved["expression"] is not None
    assert "pred" in resolved["expression"]
