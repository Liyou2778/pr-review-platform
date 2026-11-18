"""Run the worker for at most N jobs, then exit.

Useful for CI smoke tests and for demos where you do not want a long-lived
process.  ``--drain`` keeps going until the queue is empty.

Usage:  python scripts/worker_once.py [--max N] [--drain]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from apps.worker.main import recover_stale, run_once


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=1)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    await recover_stale()

    processed = 0
    while processed < args.max:
        run_id = await run_once(timeout_s=1.0)
        if run_id is None:
            if args.drain:
                break
            break
        processed += 1
        print(f"processed run {run_id}")
    print(f"done: {processed} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
