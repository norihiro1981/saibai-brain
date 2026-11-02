"""SwitchBot API v1.1 を叩く小さな部品（試し用・本体のコードからは使わない）。

署名の作り方は公式 README（OpenWonderLabs/SwitchBotAPI）の「Python 3 example code」に従う。
鍵は python-dotenv でプログラムの中だけで読み、画面にもログにも出さない。
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

API = "https://api.switch-bot.com/v1.1"
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"

load_dotenv()


def _headers() -> dict:
    token = os.environ["SWITCHBOT_TOKEN"]
    secret = os.environ["SWITCHBOT_SECRET"]
    t = str(int(round(time.time() * 1000)))
    nonce = str(uuid.uuid4())
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{token}{t}{nonce}".encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "Authorization": token,
        "sign": sign,
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }


def _call(method: str, path: str, body: dict | None = None) -> dict:
    """叩いて JSON を返す。応答の Date ヘッダと往復の時間も付ける（鍵の入ったヘッダは返さない）。"""
    sent = time.time()
    r = httpx.request(method, API + path, headers=_headers(), json=body, timeout=15)
    got = time.time()
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text}
    return {
        "http": r.status_code,
        "server_date": r.headers.get("date"),
        "sent": sent,
        "got": got,
        "data": data,
    }


def get(path: str) -> dict:
    return _call("GET", path)


def post(path: str, body: dict) -> dict:
    return _call("POST", path, body)


def device_id(env_name: str, device_type: str) -> str:
    """.env にあればそれを、無ければ一覧から種類で引く（ID をファイルに書かないため）。"""
    if v := os.environ.get(env_name):
        return v
    for d in get("/devices")["data"]["body"]["deviceList"]:
        if d.get("deviceType") == device_type:
            return d["deviceId"]
    raise SystemExit(f"{device_type} が一覧にありません")


def log_writer(stem: str):
    """spikes/logs/ に JSON Lines で生の記録を書く関数を返す。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    f = path.open("a", encoding="utf-8")

    def write(rec: dict) -> None:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

    write.path = path  # type: ignore[attr-defined]
    return write
