"""Run the golden cases against an isolated database.

  python -m evaluation.run_golden                       # offline rule mode
  python -m evaluation.run_golden --settings-from ../data/app.db --runs 2

--settings-from copies only the workspace model settings from an existing
database (read-only); projects and documents there are never touched.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings-from", help="SQLite database to copy model settings from (read-only)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", default=str(Path(__file__).parent / "results"))
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="tongling-golden-"))
    os.environ["DATA_DIR"] = str(root)
    os.environ["DATABASE_URL"] = "sqlite:///" + str(root / "golden.db")
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    from fastapi.testclient import TestClient
    from app.main import app
    from evaluation.golden import judge, load_cases, run_case, stability

    with TestClient(app) as client:
        if args.settings_from:
            live = json.loads(sqlite3.connect(f"file:{args.settings_from}?mode=ro", uri=True)
                              .execute("select settings from workspaces").fetchone()[0])
            current = client.get("/api/settings").json()
            keep = {k: v for k, v in live.items() if k not in {"client_access", "knowledge_policy"}}
            response = client.put("/api/settings", json={**current, **keep})
            assert response.status_code == 200, response.text
        provider = client.get("/api/settings").json().get("provider")
        report = {"provider": provider, "at": time.strftime("%Y-%m-%d %H:%M"), "cases": []}
        for case in load_cases():
            runs = [run_case(client, case, args.timeout) for _ in range(args.runs)]
            report["cases"].append({"case": case["id"], "runs": runs,
                                    "verdicts": [judge(case, run) for run in runs],
                                    "stability": stability(runs)})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"golden-{provider}-{time.strftime('%Y%m%d-%H%M')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case in report["cases"]:
        for run, verdict in zip(case["runs"], case["verdicts"]):
            print(f"{case['case']}: 需審閱 {run['review_items']} 項 · 關鍵點 {verdict['key_point_coverage']} · "
                  f"驗收覆蓋 {run['criteria_coverage']} · 品質 {run['quality_score']} · {run['seconds']} 秒")
        if case["stability"]:
            print(f"  穩定度：主題一致 {case['stability']['topic_agreement']} · "
                  f"關鍵點相同 {case['stability']['key_points_identical']}")
    print(f"報告：{path}")


if __name__ == "__main__":
    sys.exit(main())
