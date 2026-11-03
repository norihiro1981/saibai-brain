# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""手順 5：Webhook の受け口。届いた時刻と、中の timeOfSample（測った時刻）を記録する。

確かめたいこと：
- 温湿度計の値が変わらない時にも、定期的に届くか（届かなければ「止まった」と「落ち着いている」を見分けられない）。
- 測ってから届くまでの遅れ。

外からは長い合言葉の道（path）だけを受ける。合言葉は起動の度に作り、spikes/logs/（commit しない）に書く。
手元の 127.0.0.1 だけで待ち、外へはトンネルで出す。
"""

import json
import secrets
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 8787
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SECRET_PATH = "/sb-" + secrets.token_urlsafe(24)
(LOG_DIR / "s5_webhook_path.txt").write_text(SECRET_PATH, encoding="utf-8")
log_path = LOG_DIR / f"s5_webhook_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
log_f = log_path.open("a", encoding="utf-8")
last_by_type: dict[str, float] = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        got = time.time()
        if self.path != SECRET_PATH:
            self.send_response(404)
            self.end_headers()
            print(f"[{datetime.now():%H:%M:%S}] 合言葉の違う道を断った")
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        log_f.write(json.dumps({"got": got, "body": body}, ensure_ascii=False) + "\n")
        log_f.flush()

        ctx = body.get("context") or {}
        kind = ctx.get("deviceType", "?")
        sample = ctx.get("timeOfSample")
        delay = (
            f"{got - sample / 1000:.1f}s"
            if isinstance(sample, (int, float))
            else "時刻なし"
        )
        gap = f"{got - last_by_type[kind]:.0f}s" if kind in last_by_type else "初"
        last_by_type[kind] = got
        vals = {
            k: ctx[k]
            for k in ("temperature", "humidity", "battery", "powerState", "power")
            if k in ctx
        }
        print(
            f"[{datetime.now():%H:%M:%S}] {body.get('eventType')} {kind} 遅れ={delay} 前から={gap} {vals}",
            flush=True,
        )

    # 標準の 1 行の記録は出さない（道に合言葉が入るため）
    def log_message(self, *args) -> None:
        pass


print(f"127.0.0.1:{PORT} で待つ。合言葉の道は spikes/logs/s5_webhook_path.txt")
print(f"生の記録：{log_path}", flush=True)
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
