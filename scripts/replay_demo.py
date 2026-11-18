"""Demo driver: post a replay request to a running API and print the result.

Usage:  python scripts/replay_demo.py [--base-url http://127.0.0.1:8210] [--wait]
"""

from __future__ import annotations

import argparse
import json
import time

import httpx

DEMO_DIFF = """diff --git a/app/checkout.py b/app/checkout.py
--- a/app/checkout.py
+++ b/app/checkout.py
@@ -10,4 +10,7 @@ def create_order(payload):
     order_id = uuid4().hex
+    api_key = "demo-key-9f8e7d6c5b4a3210"
+    total = eval(payload["total"])
+    subprocess.run("echo " + order_id, shell=True)
     return {"id": order_id, "total": total}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8210")
    parser.add_argument("--repo", default="acme/demo-checkout")
    parser.add_argument("--pr", type=int, default=101)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=20) as client:
        health = client.get("/healthz").json()
        print("health:", json.dumps(health, ensure_ascii=False))

        resp = client.post(
            "/api/replay",
            json={
                "repo_full_name": args.repo,
                "pr_number": args.pr,
                "diff": DEMO_DIFF,
                "head_sha": f"demo{int(time.time())}",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        run_id = payload["run_id"]
        print("run:", payload)

        if not args.wait:
            print(f"now start the worker, then: GET {args.base_url}/api/runs/{run_id}")
            return 0

        for _ in range(60):
            detail = client.get(f"/api/runs/{run_id}").json()
            if detail["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)
        print(json.dumps(detail, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
