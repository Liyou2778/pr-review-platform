"""Seed a few runs + feedbacks so the dashboard has something to show.

This is demo data, not a benchmark: it exercises the happy path and one
false-positive cluster so the product story is visible in one command.

Usage:  python scripts/seed_demo.py [--base-url http://127.0.0.1:8210]
"""

from __future__ import annotations

import argparse
import time

import httpx

DIFFS = {
    "acme/checkout": """diff --git a/app/checkout.py b/app/checkout.py
--- a/app/checkout.py
+++ b/app/checkout.py
@@ -10,4 +10,7 @@ def create_order(payload):
     order_id = uuid4().hex
+    api_key = "demo-key-9f8e7d6c5b4a3210"
+    total = eval(payload["total"])
+    subprocess.run("echo " + order_id, shell=True)
     return {"id": order_id, "total": total}
""",
    "acme/web": """diff --git a/web/widget.tsx b/web/widget.tsx
--- a/web/widget.tsx
+++ b/web/widget.tsx
@@ -10,2 +10,4 @@ export function Widget() {
   return (
+    <div dangerouslySetInnerHTML={{ __html: html }} />
+    <span>{eval(code)}</span>
   );
""",
    "acme/http": """diff --git a/app/client.py b/app/client.py
--- a/app/client.py
+++ b/app/client.py
@@ -1,2 +1,3 @@ import requests
 def fetch(url):
+    return requests.get(url)
""",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8210")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=20) as client:
        print("health:", client.get("/healthz").json())
        run_ids = []
        for repo, diff in DIFFS.items():
            resp = client.post(
                "/api/replay",
                json={
                    "repo_full_name": repo,
                    "pr_number": 100 + len(run_ids),
                    "diff": diff,
                    "head_sha": f"seed{int(time.time())}{len(run_ids)}",
                },
            )
            resp.raise_for_status()
            run_ids.append(resp.json()["run_id"])
            print(f"queued run {run_ids[-1]} for {repo}")

        print("\nnow run the worker, then:")
        for run_id in run_ids:
            print(f"  GET {args.base_url}/api/runs/{run_id}")
        print(f"  GET {args.base_url}/dashboard")
        print("\nafter the worker finishes, mark one finding as false positive:")
        print(f"  POST {args.base_url}/api/findings/1/feedback {{\"verdict\":\"false_positive\"}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
