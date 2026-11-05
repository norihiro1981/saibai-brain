"""MCP サーバー（stdio）：常駐の受け手に localhost で聞く薄い口。

Mac の Claude アプリが起動し、アプリを閉じると止まる。守りと記録は受け手が持つので、
この口が止まっても記録は止まらない（設計書 §9）。
"""

import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from saibai.receiver import DEFAULT_PORT

server = MCPServer(
    name="saibai",
    instructions="栽培室の受け手に聞く口。温湿度は受け手がクラウドを 30 秒ごとに読んだ値。",
)


def receiver_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('SAIBAI_RECEIVER_PORT', DEFAULT_PORT)}"


@server.tool(
    description=(
        "栽培室の今の状態：温度・湿度・VPD・読んだ時刻（測った時刻ではない）・値が最後に変わった時刻・"
        "受け手が生きているか・プラグ・守りの層・定時（timer）・オート（schedule と auto・経験の版）。"
        "受け手が止まっていれば receiver.up=false。"
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
)
def get_status() -> dict:
    try:
        r = httpx.get(receiver_url() + "/status", timeout=3.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"receiver": {"up": False, "error": f"受け手に届かない：{type(e).__name__}"}}


def _post(path: str, body: dict) -> dict:
    try:
        r = httpx.post(receiver_url() + path, json=body, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "text": f"受け手に届かない：{type(e).__name__}（何も動かしていない）"}


@server.tool(
    description=(
        "加湿器の入・切。入は「今から minutes 分」の計画として守りの層に出す＝湿度・連続・1 日の上限・"
        "入と入の間で断られることがある（断った時は text に理由）。切は今の計画を捨てる。"
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def set_humidifier(on: bool, minutes: int = 30) -> dict:
    return _post("/humidifier", {"on": on, "minutes": minutes})


@server.tool(
    description="止める：すべてに優先して加湿器を切り、止めを解くまで計画を受けない。release=true で止めを解く。",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def stop_all(release: bool = False) -> dict:
    return _post("/stop", {"release": release})


@server.tool(
    description=(
        "オートの入・切＝エージェントの定時の起動（毎 3 時間・振り返り→経験→計画）の再開・止める。"
        "オートの計画も守りの層を通る。"
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def set_auto(on: bool) -> dict:
    return _post("/auto", {"on": on})


@server.tool(
    description=(
        "定時の入・切と、毎日の開始の時刻（HH:MM）・運転の分。時刻になると「今から minutes 分」の計画を"
        "守りの層に出す（断られることがある）。ai_minutes=true＝長さを AI に任せる：開始の 10 分前に制御役が"
        "分を決め、答えが無ければ minutes（既定）で動く。省いた引数は今の設定のまま。"
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def set_timer(
    on: bool, start: str | None = None, minutes: int | None = None, ai_minutes: bool | None = None
) -> dict:
    return _post("/timer", {"on": on, "start": start, "minutes": minutes, "ai_minutes": ai_minutes})


@server.tool(
    description=(
        "チャート用の記録：直近 hours 時間（0.5〜48）の温度・湿度・VPD（points＝[読んだ時刻, 温度, 湿度, VPD]）"
        "と操作の記録（actions）・狙いの VPD の帯（target）。値が null の点は記録の穴＝線でつながない。"
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
)
def get_history(hours: float = 24.0) -> dict:
    try:
        r = httpx.get(receiver_url() + "/history", params={"hours": hours}, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"points": [], "actions": [], "error": f"受け手に届かない：{type(e).__name__}"}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
