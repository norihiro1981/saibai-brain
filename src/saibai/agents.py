"""Managed Agents の側の組み立て：記憶の置き場・エージェント・環境（設計書 §4）。

書き方は Claude Code の claude-api の手引き（Managed Agents の節）に従った（段 2 で叩いて確かめた形）。
作った ID は `run/agents.json`（手元だけ・commit しない）に貯めて使い回す。鍵は python-dotenv で読む。

役ごとの置き場の読み書き（段 B で資料を確かめて決めた・設計書 §4）：
同じセッションの名簿の中のエージェントは 1 つの作業場（ファイル）を共にし、置き場の読み書きは
セッションにつなぐ時に 1 つだけ決まる＝役ごとには分けられない。そこで 1 回の起動を 2 段にする。
- 振り返りのセッション（分析役）：経験＝書ける・温湿度と操作の記録＝読むだけ
- 計画のセッション（制御役＋専門家）：経験も記録も知識も読むだけ
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from saibai.data import load_actions, load_readings, season_files

ROOT = Path(__file__).resolve().parents[2]
IDS = Path(os.environ.get("SAIBAI_RUN_DIR", ROOT / "run")) / "agents.json"

# description はモデルに向けて書く（セッションの頭に入る）
STORES = {
    "meter": (
        "saibai-meter",
        "栽培室の温湿度の記録（受け手がクラウドから 30 秒ごとに読んだ値の写し）。"
        "/meter/YYYY-MM-DD/HH.csv が 1 時間 1 ファイル、列は read_at（JST・読んだ時刻）,temp_c,rh_pct,vpd_kpa。"
        "行が 2 分より空いている所は記録の穴（受け手が止まっていた）＝線でつながない。",
    ),
    "actions": (
        "saibai-actions",
        "加湿器の操作の記録（受け手が 1 件ずつ写す）。/actions/YYYY-MM-DD.jsonl、1 行 1 件。"
        "鍵＝at（JST）・who（手動／定時／オート／守りの層／外から／受け手）・what（入／切／計画／止める／記録なし）・"
        "minutes（予定の分）・accepted（受け付けた／断った）・reasons（理由）・plug_after（3 秒後に読み直したプラグ）・"
        "temp_c・rh_pct・vpd_kpa（その時の値）。what=記録なし は gap_start〜gap_end の間の記録が無い印。",
    ),
    "experience": (
        "saibai-experience",
        "運転の経験：加湿器の操作ごとの効き目（入れた→何分で VPD がいくつ動いたか・狙いに届いたか・断られたか）"
        "と、そこから引いた傾向（もとにした回の数つき）。/experience.md 1 つ。お手本の操作ではなく、効き目の事実だけ。",
    ),
    "knowledge": (
        "saibai-knowledge",
        "コーヒーの幼木の栽培の知識の素（出どころつき）。湿り気の帯・VPD・季節の注意・加湿と土の水。読むだけ。",
    ),
}


def load_ids() -> dict:
    return json.loads(IDS.read_text()) if IDS.exists() else {}


def save_ids(ids: dict) -> None:
    IDS.parent.mkdir(parents=True, exist_ok=True)
    IDS.write_text(json.dumps(ids, ensure_ascii=False, indent=2))


def ensure_stores(
    client, ids: dict, prefix: str = "", only=("meter", "actions", "experience", "knowledge")
) -> dict:
    """置き場を作る（あれば使い回す）。prefix を付けると試しの別の置き場になる。"""
    for key in only:
        k = prefix + key
        if k in ids:
            continue
        name, desc = STORES[key]
        s = client.beta.memory_stores.create(
            name=(prefix + name) if prefix else name, description=desc
        )
        ids[k] = s.id
        if key == "knowledge":
            client.beta.memory_stores.memories.create(
                s.id,
                path="/knowledge_seed.md",
                content=(ROOT / "data" / "knowledge_seed.md").read_text(encoding="utf-8"),
            )
        save_ids(ids)
    return ids


def toolset(enabled: list[str]) -> dict:
    """作業場の道具のうち、名を挙げた物だけを入れる（Web の道具は使わない）。"""
    return {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
        "configs": [{"name": n, "enabled": True} for n in enabled],
    }


def ensure_env(client, ids: dict) -> dict:
    if "env" not in ids:
        env = client.beta.environments.create(
            name="saibai",
            # 外へは出さない。pandas が無い時に入れられるよう、パッケージの置き場だけ開ける
            config={
                "type": "cloud",
                "networking": {"type": "limited", "allow_package_managers": True},
            },
        )
        ids["env"] = env.id
        save_ids(ids)
    return ids


def store_resource(store_id: str, access: str, instructions: str) -> dict:
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": access,
        "instructions": instructions,
    }


# ---- 夏と秋の記録を、置き場の形に直す（最初の経験・試し用）----
def season_meter_hours(season: str, day: str, device: str = "node_a") -> dict[str, str]:
    """その日の記録を 1 時間 1 ファイルの CSV（置き場の温湿度の記録と同じ列）にする。"""
    env, _ = season_files(season)
    df = load_readings(env)
    d = df[(df.device_id == device) & (df.timestamp.dt.strftime("%Y-%m-%d") == day)]
    out = {}
    for hour, g in d.groupby(d.timestamp.dt.floor("h")):
        lines = ["read_at,temp_c,rh_pct,vpd_kpa"]
        for r in g.itertuples():
            lines.append(f"{r.timestamp.isoformat()},{r.temp_c},{r.rh_pct},{r.vpd_kpa}")
        out[f"/meter/{hour:%Y-%m-%d}/{hour:%H}.csv"] = "\n".join(lines) + "\n"
    return out


def season_actions(season: str, day: str | None = None) -> list[dict]:
    """作業の記録の加湿器の入切を、操作の記録の形にする（who＝手動・お手本ではない印つき）。"""
    _, act = season_files(season)
    a = load_actions(act)
    a = a[a.kind == "humidifier_on"]
    if day:
        a = a[a.start_jst.dt.strftime("%Y-%m-%d") == day]
    out = []
    note = "夏秋の作業の記録から（水のやりすぎを疑う期間＝お手本ではない・効き目だけを見る）"
    for r in a.itertuples():
        minutes = (
            None if pd.isna(r.end_jst) else int((r.end_jst - r.start_jst).total_seconds() // 60)
        )
        out.append(
            {
                "at": r.start_jst.isoformat(),
                "kind": "action",
                "who": "手動",
                "what": "入",
                "minutes": minutes,
                "accepted": True,
                "note": note,
            }
        )
        if not pd.isna(r.end_jst):
            out.append(
                {
                    "at": r.end_jst.isoformat(),
                    "kind": "action",
                    "who": "手動",
                    "what": "切",
                    "accepted": True,
                    "reasons": ["予定の分が終わった"],
                    "note": note,
                }
            )
    return out


def put(client, store: str, path: str, content: str) -> None:
    """在れば書き直し（前の版の sha を precondition に）、無ければ作る。"""
    for m in client.beta.memory_stores.memories.list(
        store, path_prefix=path.rsplit("/", 1)[0] + "/"
    ):
        if getattr(m, "type", "memory") == "memory" and m.path == path:
            client.beta.memory_stores.memories.update(
                m.id,
                memory_store_id=store,
                content=content,
                precondition={"type": "content_sha256", "content_sha256": m.content_sha256},
            )
            return
    client.beta.memory_stores.memories.create(store, path=path, content=content)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


PLAN_TOOL = {
    "type": "custom",
    "name": "submit_plan",
    "description": (
        "加湿器の運転の計画を手元の受け手に出す（オート）。受け手は決まった規則（守りの層）で確かめ、"
        "「受け付けた」か「断った・理由」を返す。断られたら理由を読んで、直した計画を出し直してよい。"
        "加湿しないと決めた時は runs を空にして理由を書く。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "runs": {
                "type": "array",
                "description": "運転（空なら今回は入れない）",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "string",
                            "description": "始める時刻（JST の ISO 8601・+09:00 付き）",
                        },
                        "minutes": {"type": "integer", "description": "運転する分"},
                    },
                    "required": ["start", "minutes"],
                },
            },
            "reason": {
                "type": "string",
                "description": "理由（人が読む文・数値と時刻・使った経験の行）",
            },
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
        "required": ["runs", "reason", "evidence"],
    },
}

ANALYST_SYSTEM = (
    "あなたは栽培室の記録の分析役です。/mnt/memory/ の下の置き場（温湿度の記録・操作の記録・経験）を、"
    "作業場の Python（pandas。無ければ pip で入れるか csv で）で読んで計算します。数を頭で数えず、必ずコードで出します。"
    "操作の効き目＝入れる前の VPD と、入れて 15・30・60 分の VPD の差（記録の時刻が最も近い行・前後 2 分以内）。"
    "記録の穴（行が 2 分より空く所・what=記録なし の間）にかかる操作は効き目を数えず「穴にかかる」と書きます。"
    "過去の操作をお手本にしません。数えるのは「この操作でこう動いた」の事実だけです。"
    "述べる時は必ず数値と時刻を引き、推測は推測と書きます。日本語で短く。"
)

CONTROL_SYSTEM = (
    "あなたは栽培室の制御役（オート）です。加湿器の運転を、狙いの VPD の帯に向けて決め、道具 submit_plan で出します。"
    "まず /mnt/memory/ の経験（/experience.md）と今の値を読み、どの経験の行を使ったかを理由に引きます。"
    "経験は効き目の事実です。過去にいつ入れていたかは真似しません。"
    "帯の中なら入れない判断（runs を空）もよい計画です。理由には必ず数値と時刻を引きます。"
    "submit_plan が断られたら、理由を読んで直した計画を出し直します。"
    "作物の知識が要る時は「栽培の専門家」に、読む場所・聞きたいこと・返す形を書いて 1 問ずつ渡します。"
    "ワークフローは使いません。日本語で短く。"
)

EXPERT_SYSTEM = (
    "あなたは栽培の専門家です。答えは /mnt/memory/ の下にある知識の置き場に書かれた事だけを根拠にし、"
    "出どころ（ファイル名と見出し）を必ず添えます。置き場に無い事は「知識に無い」と書きます。日本語で短く。"
)


# 役ごとのモデルと effort（設計書 §4）。指定しないと Sonnet 5.5 は high が既定で高くつく（nori・11/4）。
# effort は model の中に書く（手引き managed-agents-core「Effort on the agent model」）。
# 落とし穴：セッションごとに model を差し替える（agent_with_overrides）時は、差し替えの model にも effort を書く
# （差し替えは model を丸ごと置き換える＝書かないとその model の既定の effort で走る）。
MODELS = {
    "control": {"id": "claude-opus-5-5", "effort": "medium"},  # 計画を決める
    "analyst": {"id": "claude-sonnet-5-5", "effort": "medium"},  # 振り返り（計算は作業場の Python）
    "expert": {"id": "claude-haiku-5-5", "effort": "medium"},  # 知識を読むだけ
}


def sync_models(client, ids: dict) -> list[str]:
    """作ってあるエージェントのモデルと effort を MODELS にそろえる（違う物だけ更新＝新しい版になる）。"""
    out = []
    for role, want in MODELS.items():
        a = client.beta.agents.retrieve(ids[role])
        got = a.model.model_dump(mode="json") if hasattr(a.model, "model_dump") else {"id": a.model}
        eff = got.get("effort")
        eff = eff.get("type") if isinstance(eff, dict) else eff
        if got.get("id") == want["id"] and eff == want["effort"]:
            out.append(f"{role}：そのまま（{want['id']}・{want['effort']}・版 {a.version}）")
            continue
        u = client.beta.agents.update(ids[role], version=a.version, model=want)
        out.append(
            f"{role}：{got.get('id')}・{eff} → {want['id']}・{want['effort']}（版 {a.version}→{u.version}）"
        )
    return out + repin(client, ids)


def repin(client, ids: dict) -> list[str]:
    """版の留め直し。名簿（制御役→専門家）と定時の起動（→分析役）は、保存した時の版に留まる
    （11/4 に確かめた：エージェントを更新しても、定時の起動は分析役の版 1・名簿は専門家の版 1 のままだった）。"""
    out = []
    expert = client.beta.agents.retrieve(ids["expert"]).version
    c = client.beta.agents.retrieve(ids["control"])
    pinned = c.multiagent.model_dump(mode="json")["subagents"]["predefined_agents"][0].get(
        "version"
    )
    if pinned != expert:
        u = client.beta.agents.update(
            ids["control"],
            version=c.version,
            multiagent={
                "type": "multiagent_20261001",
                "subagents": {
                    "type": "enabled",
                    "predefined_agents": [
                        {"type": "agent", "id": ids["expert"], "version": expert}
                    ],
                    "inline_agents": {"type": "disabled"},
                },
                "workflows": {"type": "disabled"},
            },
        )
        out.append(f"名簿：専門家の版 {pinned}→{expert}（制御役の版 {c.version}→{u.version}）")
    if dep := ids.get("deployment"):
        analyst = client.beta.agents.retrieve(ids["analyst"]).version
        d = client.beta.deployments.retrieve(dep)
        if d.agent.version != analyst:
            client.beta.deployments.update(
                dep, agent={"type": "agent", "id": ids["analyst"], "version": analyst}
            )
            out.append(f"定時の起動：分析役の版 {d.agent.version}→{analyst}")
    return out


def ensure_agents(client, ids: dict) -> dict:
    if "analyst" not in ids:
        a = client.beta.agents.create(
            name="記録の分析役",
            description="置き場の温湿度と操作の記録を Python で読み、操作の効き目を数値と時刻つきで出し、経験を書き直す。",
            model=MODELS["analyst"],
            system=ANALYST_SYSTEM,
            tools=[toolset(["bash", "read", "write", "edit", "glob", "grep"])],
        )
        ids["analyst"] = a.id
        save_ids(ids)
    if "expert" not in ids:
        e = client.beta.agents.create(
            name="栽培の専門家",
            description=(
                "コーヒーの幼木の栽培の知識を、/mnt/memory/ の知識の置き場から読んで答える。"
                "聞きたいこと・読む場所・返す形を書いて渡すこと。"
            ),
            model=MODELS["expert"],
            system=EXPERT_SYSTEM,
            tools=[toolset(["read", "glob", "grep"])],
        )
        ids["expert"] = e.id
        save_ids(ids)
    if "control" not in ids:
        c = client.beta.agents.create(
            name="制御役",
            description="栽培室の加湿器の運転を、経験と今の値から決める取りまとめ役（オート）。",
            model=MODELS["control"],
            system=CONTROL_SYSTEM,
            tools=[toolset(["bash", "read", "glob", "grep"]), PLAN_TOOL],
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
        save_ids(ids)
    return ids


def main() -> None:
    """uv run saibai-agents setup：環境・置き場 4 つ・エージェント 3 体を作る（あれば使い回す）。
    uv run saibai-agents models：作ってあるエージェントのモデルと effort を MODELS にそろえる。"""
    import sys

    import anthropic
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    client = anthropic.Anthropic()
    ids = load_ids()
    cmd = (sys.argv[1:] or ["setup"])[0]
    if cmd == "setup":
        ensure_env(client, ids)
        ensure_stores(client, ids)
        ensure_agents(client, ids)
        print(json.dumps({k: v for k, v in ids.items() if not k.startswith("probe")}, indent=2))
    elif cmd == "models":  # uv run saibai-agents models：役ごとのモデルと effort をそろえる
        print("\n".join(sync_models(client, ids)))
