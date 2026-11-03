"""段 C の試し：定時の起動を手で 1 回起こし、振り返り→経験→計画→守りの層、が 1 回の起動で通るか。

守りの層とプラグは手元のこのプロセスの中の模擬（DummyPlug）。温湿度は受け手の今の記録（run/meter_*.jsonl）を流す。
本物のプラグは動かさない。定時の起動は止めたまま（手の起動は止めていても使える）。
使い方：uv run python spikes/managed_agents/c_probe.py [回数]
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from saibai import agents as A  # noqa: E402
from saibai.auto import AutoRunner, ensure_deployment  # noqa: E402
from saibai.control import Controller, DummyPlug, NoSchedule  # noqa: E402
from saibai.experience import seed_markdown  # noqa: E402
from saibai.guard import JST, REAL, Guard  # noqa: E402

load_dotenv(ROOT / ".env")
client = anthropic.Anthropic()
WORK = ROOT / "spikes" / "logs" / "c_run"


def main(times: int) -> None:
    ids = A.load_ids()
    if not list(client.beta.memory_stores.memories.list(ids["experience"], path_prefix="/")):
        client.beta.memory_stores.memories.create(
            ids["experience"], path="/experience.md", content=seed_markdown()
        )
        print("経験の最初の版を入れた（夏と秋の記録をコードで計算）")
    ensure_deployment(client, ids, A.save_ids)
    dep = ids["deployment"]
    print(f"定時の起動 {dep} status={client.beta.deployments.retrieve(dep).status}")

    WORK.mkdir(parents=True, exist_ok=True)
    now = lambda: datetime.now(JST)  # noqa: E731
    ctrl = Controller(Guard(REAL), DummyPlug(), NoSchedule(), WORK, now)
    ctrl.start()
    for line in (ROOT / "run" / f"meter_{now():%Y%m%d}.jsonl").read_text().splitlines()[-400:]:
        r = json.loads(line)
        if r.get("ok"):
            ctrl.observe(datetime.fromisoformat(r["read_at"]), float(r["rh_pct"]), r.get("temp_c"))
    runner = AutoRunner(client, ids, ctrl, WORK, now, poll_s=10, meter_dir=ROOT / "run")
    for i in range(times):
        r = client.beta.deployments.run(dep)
        sid = getattr(r, "session_id", None)
        print(f"\n=== 起動 {i + 1}：振り返りのセッション {sid}")
        rec = runner.handle_run(sid, getattr(r, "id", None))
        print(f"振り返り：{rec['review_stop']} {rec['review_usage']}\n{rec['review_text']}")
        print(f"計画のセッション {rec['plan_session']}：{rec['plan_stop']} {rec['plan_usage']}")
        for p in rec["plans"]:
            print(f"  計画 {json.dumps(p['input'], ensure_ascii=False)[:500]}\n  → {p['text']}")
        print(f"  締め：{rec['plan_text'][-300:]}")
        print(
            "経験の版："
            + " / ".join(
                f"{v['operation']}・{v['by']}・{v['created_at']}"
                for v in rec["experience_versions"]
            )
        )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
