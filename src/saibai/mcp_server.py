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
        "栽培室の今の状態：温度・湿度・読んだ時刻（測った時刻ではない）・値が最後に変わった時刻・"
        "受け手が生きているか。受け手が止まっていれば receiver.up=false。"
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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
