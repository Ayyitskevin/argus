"""Homelab xAI daily budget guard."""

import json
import os
import tempfile
from datetime import date

import pytest

_TMP = tempfile.mkdtemp(prefix="argus-xai-budget-")
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_VISION_BACKEND"] = "mock"

from app import config  # noqa: E402
from app.xai_budget import XaiBudgetError, check_budget, record_cost, today_snapshot  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "XAI_DAILY_BUDGET_USD", 1.0)
    monkeypatch.setattr(config, "XAI_ESTIMATED_COST_PER_IMAGE", 0.05)
    monkeypatch.setattr(config, "XAI_LEDGER_ENABLED", True)
    ledger = config.DATA_DIR / ".argus-xai-costs.jsonl"
    if ledger.exists():
        ledger.unlink()


def test_budget_allows_when_under_cap():
    check_budget(images=1)


def test_budget_blocks_when_cap_exceeded():
    record_cost(0.98, image_path="a.jpg")
    with pytest.raises(XaiBudgetError):
        check_budget(images=1)


def test_today_snapshot_reflects_ledger():
    record_cost(0.25, image_path="b.jpg")
    snap = today_snapshot()
    assert snap["enabled"] is True
    assert snap["spent_usd"] >= 0.25
    assert snap["remaining_usd"] is not None


def test_unlimited_budget_when_zero_cap(monkeypatch):
    monkeypatch.setattr(config, "XAI_DAILY_BUDGET_USD", 0)
    record_cost(5.0)
    snap = today_snapshot()
    assert snap["enabled"] is False
    check_budget(images=100)