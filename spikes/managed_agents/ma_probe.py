"""段 2：Managed Agents を叩いて確かめる（試し用・本体のコードからは使わない）。

書き方は Claude Code の claude-api の手引き（Managed Agents の節・Python）に従った。
鍵は python-dotenv でプログラムの中だけで読み、画面にもログにも出さない。
作ったエージェント・環境・置き場の ID は spikes/logs/ma_ids.json に貯めて使い回す（毎回作らない）。
生の記録（受けた出来事の全部）は spikes/logs/ に JSON Lines で書く。

使い方：uv run python spikes/managed_agents/ma_probe.py <手順>
  setup     環境と 3 体のエージェントと 2 つの記憶の置き場を作る（あれば使い回す）
  s1        分析役に短い記録の要約を頼む
  s2        制御役が「運転の計画を出す」を呼ぶ→手元が 1 回目は断り 2 回目は受け付ける
  s3        制御役が専門家に 1 問渡す（名簿）＋記憶の置き場（知識は読むだけ・ノートは書ける）
  s5        定時の起動：作る→止める→再開→止める→手で 1 回起こし、custom tool の呼び出しを見に行って答える
  versions  栽培ノートの版の一覧
"""

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "spikes" / "logs"
IDS = LOG_DIR / "ma_ids.json"
BUDGET_CENTS = "100"  # 1 回のセッションの上限＝$1（試しなので小さく）

load_dotenv(ROOT / ".env")
client = anthropic.Anthropic()

PLAN_TOOL = {
    "type": "custom",
    "name": "submit_plan",
    "description": (
        "加湿器の運転の計画を手元の受け手に出す。受け手は決まった規則（守りの層）で確かめ、"
        "「受け付けた」か「断った・理由」を返す。断られたら理由を読んで、直した計画を出し直してよい。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "運転を始める時刻（JST の ISO 8601）"},
            "minutes": {"type": "integer", "description": "運転する分"},
            "reason": {"type": "string", "description": "理由（人が読む文）"},
            "evidence": {
                "type": "array",
                "description": "根拠にした数値と時刻",
                "items": {
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "metric": {"type": "string"},
                        "value": {"type": "number"},
                        "time": {"type": "string"},
                    },
                    "required": ["device", "metric", "value", "time"],
                },
            },
        },
        "required": ["start", "minutes", "reason", "evidence"],
    },
}


def toolset(enabled: list[str]) -> dict:
    """作業場の道具のうち、名を挙げた物だけを入れる（Web の道具は使わない）。"""
    return {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
        "configs": [{"name": n, "enabled": True} for n in enabled],
    }


def load_ids() -> dict:
    return json.loads(IDS.read_text()) if IDS.exists() else {}


def save_ids(ids: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    IDS.write_text(json.dumps(ids, ensure_ascii=False, indent=2))


def log_writer(stem: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"ma_{stem}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    f = path.open("a", encoding="utf-8")

    def write(rec) -> None:
        if hasattr(rec, "model_dump"):
            rec = rec.model_dump(mode="json")
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        f.flush()

    write.path = path  # type: ignore[attr-defined]
    return write


def setup() -> dict:
    ids = load_ids()
    if "env" not in ids:
        env = client.beta.environments.create(
            name="saibai-probe",
            config={"type": "cloud", "networking": {"type": "limited"}},
        )
        ids["env"] = env.id
    if "analyst" not in ids:
        a = client.beta.agents.create(
            name="記録の分析役",
            description="栽培室の記録（温度・湿度・土の水分）を集計し、傾向を数値と時刻つきで返す。",
            model="claude-sonnet-5-5",
            system=(
                "あなたは栽培室の記録の分析役です。渡された記録だけを根拠に、日本語で短く答えます。"
                "傾向を述べる時は必ず数値と時刻を引きます。推測は推測と書きます。"
            ),
            tools=[toolset(["bash", "read", "glob", "grep"])],
        )
        ids["analyst"] = a.id
    if "expert" not in ids:
        e = client.beta.agents.create(
            name="栽培の専門家",
            description=(
                "コーヒーの幼木の栽培の知識を、/mnt/memory/ の知識の置き場から読んで答える。"
                "聞きたいこと・読む場所・返す形を書いて渡すこと。"
            ),
            model="claude-sonnet-5-5",
            system=(
                "あなたは栽培の専門家です。答えは /mnt/memory/ の下にある知識の置き場に書かれた事だけを根拠にし、"
                "出どころ（ファイル名と見出し）を必ず添えます。置き場に無い事は「知識に無い」と書きます。日本語で短く。"
            ),
            tools=[toolset(["read", "glob", "grep"])],
        )
        ids["expert"] = e.id
    if "control" not in ids:
        c = client.beta.agents.create(
            name="制御役",
            description="栽培室の運転を決める取りまとめ役。",
            model="claude-opus-5-5",
            system=(
                "あなたは栽培室の制御役です。加湿器の運転を決め、道具 submit_plan で計画を出します。"
                "計画の理由には必ず数値と時刻を引きます。submit_plan が断られたら、理由を読んで直した計画を出し直します。"
                "作物の知識が要る時は「栽培の専門家」に、読む場所・聞きたいこと・返す形を書いて 1 問ずつ渡します。"
                "数が多い仕事を並べて流す仕組み（ワークフロー）は使いません。日本語で短く。"
            ),
            tools=[toolset(["read", "glob", "grep", "write", "edit"]), PLAN_TOOL],
            multiagent={
                "type": "multiagent_20261001",
                "subagents": {
                    "type": "enabled",
                    "predefined_agents": [ids["expert"]],
                    "inline_agents": {"type": "disabled"},
                },
                "workflows": {"type": "disabled"},
            },
        )
        ids["control"] = c.id
    if "knowledge" not in ids:
        k = client.beta.memory_stores.create(
            name="saibai-knowledge",
            description="コーヒーの幼木の栽培の知識の素（出どころつき）。読むだけ。",
        )
        client.beta.memory_stores.memories.create(
            k.id,
            path="/knowledge_seed.md",
            content=(ROOT / "data" / "knowledge_seed.md").read_text(encoding="utf-8"),
        )
        ids["knowledge"] = k.id
    if "notebook" not in ids:
        n = client.beta.memory_stores.create(
            name="saibai-notebook",
            description="栽培ノート。運転で学んだことを 1 行ずつ足していく。",
        )
        client.beta.memory_stores.memories.create(
            n.id, path="/notebook.md", content="# 栽培ノート\n"
        )
        ids["notebook"] = n.id
    save_ids(ids)
    print(json.dumps(ids, ensure_ascii=False, indent=2))
    return ids


def run(session_id: str, first_text: str, log, on_tool=None) -> None:
    """流れを開いてから最初の言葉を送り、custom tool には on_tool で答える。止まるまで読む。"""
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": first_text}]}],
        )
        for ev in stream:
            log(ev)
            t = ev.type
            if t == "agent.message":
                for b in ev.content:
                    if b.type == "text":
                        print(f"\n[発言] {b.text}")
            elif t == "agent.custom_tool_use":
                print(f"\n[道具] {ev.name} {json.dumps(ev.input, ensure_ascii=False)}")
                answer = on_tool(ev) if on_tool else "この道具は今は使えない"
                print(f"[手元の答え] {answer}")
                client.beta.sessions.events.send(
                    session_id=session_id,
                    events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": ev.id,
                        "content": [{"type": "text", "text": answer}],
                    }],
                )
            elif t in ("agent.thread_message_sent", "agent.thread_message_received"):
                who = getattr(ev, "to_agent_name", None) or getattr(ev, "from_agent_name", None)
                text = " ".join(b.text for b in ev.content if getattr(b, "type", "") == "text")
                print(f"\n[{t.split('.')[-1]} {who}] {text[:400]}")
            elif t in ("agent.tool_use",):
                print(f"[作業場] {ev.name} {json.dumps(ev.input, ensure_ascii=False)[:200]}")
            elif t == "session.error":
                print(f"[誤り] {ev}")
            elif t == "session.status_idle":
                if ev.stop_reason.type != "requires_action":
                    print(f"\n--- 止まった：{ev.stop_reason.type}")
                    break
            elif t == "session.status_terminated":
                print("\n--- 終わった")
                break


def report_cost(session_id: str, log) -> None:
    s = client.beta.sessions.retrieve(session_id=session_id)
    log({"session_final": s.model_dump(mode="json")})
    print(f"費用：{s.usage.model_dump(mode='json') if s.usage else None}")
    print(f"Console：https://platform.claude.com/workspaces/default/sessions/{session_id}")


def sample_rows(n: int = 30) -> str:
    """夏の記録の頭から node_a と soil_a を n 行ずつ（短い記録の要約の試し用）。"""
    out, counts = [], {"node_a": 0, "soil_a": 0}
    with (ROOT / "data" / "summer_2026-08-15_2026-09-11.csv").open() as f:
        r = csv.reader(f)
        out.append(",".join(next(r)))
        for row in r:
            if row[1] in counts and counts[row[1]] < n:
                counts[row[1]] += 1
                out.append(",".join(row))
            if all(v >= n for v in counts.values()):
                break
    return "\n".join(out)


def s1(ids: dict) -> None:
    log = log_writer("s1_summary")
    s = client.beta.sessions.create(
        agent=ids["analyst"], environment_id=ids["env"], title="s1 記録の要約",
        budget={"type": "limit", "max_list_cost": {"amount": BUDGET_CENTS, "currency": "USD"}},
    )
    print(f"session {s.id} status={s.status}")
    run(s.id, "次の記録（30 分ほど）を 3 行で要約してください。\n\n" + sample_rows(), log)
    report_cost(s.id, log)
    print(f"生の記録：{log.path}")


def s2(ids: dict) -> None:
    log = log_writer("s2_custom_tool")
    s = client.beta.sessions.create(
        agent=ids["control"], environment_id=ids["env"], title="s2 計画の道具の往復",
        budget={"type": "limit", "max_list_cost": {"amount": BUDGET_CENTS, "currency": "USD"}},
    )
    calls = []

    def on_tool(ev) -> str:
        calls.append(ev.input)
        if len(calls) == 1:
            return (
                "断った：開始の時刻の node_a の湿度が 76.2%（入れない線は 75% 以上）。"
                "湿度が 75% を下回る時刻から始める計画なら受け付ける。"
            )
        return "受け付けた：守りの層の線をすべて満たす。"

    text = (
        "今は 2026-08-15 09:00 JST です。最近の node_a（室の空気の基準）の値：\n"
        "08:00 RH 70.1% / 08:30 RH 73.5% / 09:00 RH 76.2%（気温 27.5℃）。\n"
        "このあと 10:00 には冷房で RH 68% まで下がる見込みです。\n"
        "今日の午前の加湿器の計画を 1 つ submit_plan で出してください。結果を受けたら一言で締めてください。"
    )
    run(s.id, text, log, on_tool)
    print(f"\n計画を出した回数：{len(calls)}")
    report_cost(s.id, log)
    print(f"生の記録：{log.path}")


def s3(ids: dict) -> None:
    log = log_writer("s3_roster_memory")
    s = client.beta.sessions.create(
        agent=ids["control"], environment_id=ids["env"], title="s3 名簿と記憶の置き場",
        budget={"type": "limit", "max_list_cost": {"amount": BUDGET_CENTS, "currency": "USD"}},
        resources=[
            {
                "type": "memory_store", "memory_store_id": ids["knowledge"], "access": "read_only",
                "instructions": "栽培の知識。専門家の役が読む。書き換えない。",
            },
            {
                "type": "memory_store", "memory_store_id": ids["notebook"], "access": "read_write",
                "instructions": "栽培ノート。制御役が学んだことを 1 行ずつ足す。",
            },
        ],
    )
    text = (
        "2 つ頼みます。計画は出さないでください（submit_plan は使わない）。\n"
        "1. 栽培の専門家に 1 問だけ渡してください：「コーヒーの幼木で、土の水分が多すぎる時に起きる事と、"
        "その時に避ける事は何か」。読む場所は /mnt/memory/saibai-knowledge/、返す形は 3 行以内＋出どころ。\n"
        "2. 答えを受けたら、/mnt/memory/saibai-notebook/notebook.md の末尾に"
        "「- 2026-11-04 試し：<答えの要点を 1 行>」を 1 行だけ足してください。\n"
        "終わったら、専門家の答えと足した行をそのまま見せてください。"
    )
    run(s.id, text, log)
    report_cost(s.id, log)
    versions(ids)
    print(f"生の記録：{log.path}")


def versions(ids: dict) -> None:
    for v in client.beta.memory_stores.memory_versions.list(ids["notebook"]):
        d = v.model_dump(mode="json")
        print(f"版 {d.get('id')} {d.get('operation')} {d.get('created_at')} by={d.get('created_by')}")
    for m in client.beta.memory_stores.memories.list(ids["notebook"], path_prefix="/", view="full"):
        if m.type == "memory":
            print(f"--- {m.path}\n{m.content}")


def s5(ids: dict) -> None:
    log = log_writer("s5_deployment")
    if "deployment" not in ids:
        d = client.beta.deployments.create(
            name="saibai-毎朝の計画（試し）",
            agent=ids["control"],
            environment_id=ids["env"],
            initial_events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": (
                    "定時の起動の試しです。node_a の RH は 06:00 に 62.0%（気温 24.1℃）。"
                    "今朝の加湿器の計画を 1 つ submit_plan で出し、結果を受けたら一言で締めてください。"
                )}],
            }],
            schedule={"type": "cron", "expression": "0 6 * * *", "timezone": "Asia/Tokyo"},
            budget={"type": "limit", "max_list_cost": {"amount": "50", "currency": "USD"}},
        )
        ids["deployment"] = d.id
        save_ids(ids)
        log({"created": d.model_dump(mode="json")})
        print(f"作った：{d.id} status={d.status} 次＝{d.schedule.upcoming_runs_at}")
    dep = ids["deployment"]
    for op in ("pause", "unpause", "pause"):
        d = getattr(client.beta.deployments, op)(dep)
        log({op: d.model_dump(mode="json")})
        print(f"{op} → status={d.status} paused_reason={d.paused_reason}")

    # 止めたまま手で 1 回起こす（手の起動は止めていても使える＝資料）
    r = client.beta.deployments.run(dep)
    log({"run": r.model_dump(mode="json")})
    print(f"手で起こした：{r.model_dump(mode='json')}")
    sid = getattr(r, "session_id", None)
    if sid is None:
        runs = list(client.beta.deployment_runs.list(deployment_id=dep))
        sid = runs[0].session_id if runs else None
    print(f"セッション：{sid}")
    if not sid:
        return

    # 見に行く形：events.list を間をおいて読み、custom tool の呼び出しを探して答える
    answered, deadline = set(), time.monotonic() + 600
    while time.monotonic() < deadline:
        s = client.beta.sessions.retrieve(session_id=sid)
        evs = list(client.beta.sessions.events.list(session_id=sid))
        for ev in evs:
            if ev.type == "agent.custom_tool_use" and ev.id not in answered:
                log(ev)
                print(f"[見つけた道具の呼び出し] {ev.name} {json.dumps(ev.input, ensure_ascii=False)}")
                client.beta.sessions.events.send(
                    session_id=sid,
                    events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": ev.id,
                        "content": [{"type": "text", "text": "受け付けた：守りの層の線をすべて満たす。"}],
                    }],
                )
                answered.add(ev.id)
        last = evs[-1] if evs else None
        stop = getattr(last, "stop_reason", None)
        print(f"  status={s.status} 出来事={len(evs)} 最後={last.type if last else None}")
        if answered and last and last.type == "session.status_idle" and stop.type != "requires_action":
            for ev in evs:
                log(ev)
            break
        time.sleep(10)
    report_cost(sid, log)
    print(f"生の記録：{log.path}")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "setup"
    ids = setup() if step == "setup" else load_ids()
    {"setup": lambda i: None, "s1": s1, "s2": s2, "s3": s3, "s5": s5, "versions": versions}[step](ids)
