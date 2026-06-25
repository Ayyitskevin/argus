"""Homelab xAI spend guard — optional daily budget for Grok vision."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from . import config

log = logging.getLogger("argus.xai_budget")


class XaiBudgetError(Exception):
    """Raised when a homelab daily budget would be exceeded."""


def _ledger_path() -> Path:
    return config.DATA_DIR / ".argus-xai-costs.jsonl"


def _today_spend() -> float:
    ledger = _ledger_path()
    if not ledger.is_file():
        return 0.0
    today = date.today().isoformat()
    total = 0.0
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("date", ""))[:10] != today:
                continue
            total += float(row.get("cost_usd") or 0.0)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read xAI ledger %s: %s", ledger, exc)
    return total


def check_budget(*, images: int = 1) -> None:
    """Preflight homelab budget before real Grok calls (0 budget = unlimited)."""
    cap = float(config.XAI_DAILY_BUDGET_USD or 0)
    if cap <= 0:
        return
    estimate = float(config.XAI_ESTIMATED_COST_PER_IMAGE) * max(1, images)
    spent = _today_spend()
    if spent + estimate > cap:
        raise XaiBudgetError(
            f"xAI daily budget exceeded: ${spent:.2f} spent + ${estimate:.2f} "
            f"estimated > ${cap:.2f} cap (ARGUS_XAI_DAILY_BUDGET_USD)"
        )


def record_cost(cost_usd: float | None, *, image_path: str = "") -> None:
    if cost_usd is None or cost_usd <= 0:
        return
    cap = float(config.XAI_DAILY_BUDGET_USD or 0)
    if cap <= 0 and not config.XAI_LEDGER_ENABLED:
        return
    ledger = _ledger_path()
    row = {
        "date": date.today().isoformat(),
        "cost_usd": round(float(cost_usd), 6),
        "image": Path(image_path).name if image_path else None,
    }
    try:
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        log.warning("could not append xAI ledger: %s", exc)


def today_snapshot() -> dict:
    cap = float(config.XAI_DAILY_BUDGET_USD or 0)
    spent = _today_spend()
    return {
        "enabled": cap > 0,
        "cap_usd": cap or None,
        "spent_usd": round(spent, 4),
        "remaining_usd": round(max(0.0, cap - spent), 4) if cap > 0 else None,
    }