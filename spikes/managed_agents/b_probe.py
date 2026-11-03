"""段 B の試し：分析役が置き場の記録から操作の前後の VPD の差を出せるか・読むだけの置き場に書くと断られるか。

試しの置き場（probe_*）に、秋の記録の 1 日（10/4・node_a・00〜08 時）と、その日の加湿器の入切を入れる。
本物の温湿度の置き場は使わない（記録が混ざらないように）。
使い方：uv run python spikes/managed_agents/b_probe.py
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

LOG = ROOT / "spikes" / "logs" / f"ma_b_probe_{datetime.now():%Y%m%d_%H%M%S}.jsonl"

load_dotenv(ROOT / ".env")
client = anthropic.Anthropic()


def log(rec) -> None:
    if hasattr(rec, "model_dump"):
        rec = rec.model_dump(mode="json")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    ids = A.load_ids()
    A.ensure_env(client, ids)
    A.ensure_stores(client, ids, prefix="probe_", only=("meter", "actions", "experience"))
    A.ensure_agents(client, ids)
    if not ids.get("probe_seeded"):
        for path, content in A.season_meter_hours("autumn", "2026-10-04").items():
            if path.endswith(
                (
                    "/00.csv",
                    "/01.csv",
                    "/02.csv",
                    "/03.csv",
                    "/04.csv",
                    "/05.csv",
                    "/06.csv",
                    "/07.csv",
                    "/08.csv",
                )
            ):
                A.put(client, ids["probe_meter"], path, content)
        acts = A.season_actions("autumn", "2026-10-04")
        A.put(
            client,
            ids["probe_actions"],
            "/actions/2026-10-04.jsonl",
            "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in acts),
        )
        A.put(client, ids["probe_experience"], "/experience.md", "# 経験\n\n（まだ無い）\n")
        ids["probe_seeded"] = True
        A.save_ids(ids)

    s = client.beta.sessions.create(
        agent=ids["analyst"],
        environment_id=ids["env"],
        title="段 B 置き場を読んで効き目を出す",
        budget={"type": "limit", "max_list_cost": {"amount": "100", "currency": "USD"}},
        resources=[
            A.store_resource(ids["probe_meter"], "read_only", "温湿度の記録（試し：秋の 10/4）。"),
            A.store_resource(ids["probe_actions"], "read_only", "操作の記録（試し：秋の 10/4）。"),
            A.store_resource(
                ids["probe_experience"], "read_only", "経験（このセッションでは読むだけ）。"
            ),
        ],
    )
    print(f"session {s.id}")
    text = (
        "2 つ頼みます。\n"
        "1. 操作の記録の 10/4 の「入」それぞれについて、入れる前の VPD と、入れて 15・30・60 分の VPD と差を、"
        "温湿度の記録から Python で計算して表で見せてください（使ったファイルの道と行の時刻も）。\n"
        "2. 試しに、経験の置き場の /experience.md の末尾に「- 試し」の 1 行を書き足してみてください。"
        "断られたら、その誤りの文をそのまま見せてください（書けたら、書けたと書いてください）。"
    )
    with client.beta.sessions.events.stream(session_id=s.id) as stream:
        client.beta.sessions.events.send(
            session_id=s.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )
        for ev in stream:
            log(ev)
            if ev.type == "agent.message":
                for b in ev.content:
                    if b.type == "text":
                        print(f"\n[発言] {b.text}")
            elif ev.type == "agent.tool_use":
                print(f"[作業場] {ev.name} {json.dumps(ev.input, ensure_ascii=False)[:300]}")
            elif ev.type == "agent.tool_result":
                print(
                    f"[結果] {json.dumps(ev.model_dump(mode='json').get('content'), ensure_ascii=False)[:300]}"
                )
            elif ev.type == "session.status_idle" and ev.stop_reason.type != "requires_action":
                break
            elif ev.type == "session.status_terminated":
                break
    fin = client.beta.sessions.retrieve(session_id=s.id)
    log({"session_final": fin.model_dump(mode="json")})
    print(f"\n費用：{fin.usage.model_dump(mode='json') if fin.usage else None}")
    vs = list(client.beta.memory_stores.memory_versions.list(ids["probe_experience"]))
    print(f"経験の版：{[(v.operation, v.created_by.type) for v in vs]}")
    print(f"生の記録：{LOG}")


if __name__ == "__main__":
    main()
