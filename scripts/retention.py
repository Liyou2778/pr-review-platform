"""Prune old runs and their children.

Usage:
  python scripts/retention.py --days 30 --dry-run   # default: report only
  python scripts/retention.py --days 30 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json

from core.config import get_settings
from core.db import SessionLocal, engine
from core.retention import cutoff_iso, prune


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=get_settings().retention_days)
    parser.add_argument("--apply", action="store_true", help="actually delete (default is dry-run)")
    args = parser.parse_args()

    cutoff = cutoff_iso(args.days)
    if not cutoff:
        print("retention disabled (--days 0)")
        return 0
    async with SessionLocal() as session:
        result = await prune(session, cutoff, dry_run=not args.apply)
    print(json.dumps({"cutoff": cutoff, **result}, ensure_ascii=False, indent=2))
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
