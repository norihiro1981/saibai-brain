# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""手順 6：Webhook の登録・確かめ・削除。

  uv run --script s6_webhook_setup.py setup https://xxxx.trycloudflare.com
  uv run --script s6_webhook_setup.py query
  uv run --script s6_webhook_setup.py delete

URL には合言葉の道が入るので、画面には出さない（先頭だけ見せる）。
setup は、ほかの URL がもう登録されていたら止まる（ほかの用途の Webhook を壊さないため）。
delete は、この試しの合言葉の道を含む URL だけを消す。
"""

import sys
from pathlib import Path

from sb_client import LOG_DIR, log_writer, post

W = "/webhook"
log = log_writer("s6_webhook_setup")


def secret_path() -> str:
    return (LOG_DIR / "s5_webhook_path.txt").read_text(encoding="utf-8").strip()


def mask(url: str) -> str:
    return url.split("/sb-")[0] + "/sb-…" if "/sb-" in url else url


def registered() -> list[str]:
    res = post(f"{W}/queryWebhook", {"action": "queryUrl"})
    log(
        {
            "step": "queryUrl",
            "http": res["http"],
            "statusCode": res["data"].get("statusCode"),
        }
    )
    body = res["data"].get("body") or {}
    return list(body.get("urls") or [])


cmd = sys.argv[1] if len(sys.argv) > 1 else "query"

if cmd == "setup":
    base = sys.argv[2].rstrip("/")
    url = base + secret_path()
    if others := [u for u in registered() if u != url]:
        raise SystemExit(
            f"ほかの URL がもう登録されている：{[mask(u) for u in others]}。止める。"
        )
    res = post(
        f"{W}/setupWebhook", {"action": "setupWebhook", "url": url, "deviceList": "ALL"}
    )
    log({"step": "setup", "http": res["http"], "data": res["data"]})
    print(
        f"setup：HTTP {res['http']} {res['data'].get('statusCode')} {res['data'].get('message')}  {mask(url)}"
    )
elif cmd == "query":
    urls = registered()
    print(f"登録：{[mask(u) for u in urls]}")
    if urls:
        res = post(f"{W}/queryWebhook", {"action": "queryDetails", "urls": urls})
        for d in res["data"].get("body") or []:
            print({**d, "url": mask(d.get("url", ""))})
elif cmd == "delete":
    mine = secret_path()
    for u in registered():
        if mine in u:
            res = post(f"{W}/deleteWebhook", {"action": "deleteWebhook", "url": u})
            log({"step": "delete", "http": res["http"], "data": res["data"]})
            print(
                f"delete：HTTP {res['http']} {res['data'].get('statusCode')} {res['data'].get('message')}  {mask(u)}"
            )
    print(f"残り：{[mask(u) for u in registered()]}")
else:
    raise SystemExit(f"知らない命令：{cmd}")

print(f"生の記録：{Path(log.path)}")
