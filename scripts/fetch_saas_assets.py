#!/usr/bin/env python3
"""Download allowlisted hero images for the SaaS portal (safe fetch only)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.safe_fetch import sync_saas_catalog  # noqa: E402


def main() -> int:
    dest = ROOT / "static" / "saas"
    written = sync_saas_catalog(dest)
    print(f"Wrote {len(written)} assets to {dest}")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())