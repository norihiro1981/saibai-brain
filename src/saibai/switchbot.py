"""SwitchBot のクラウド API（v1.1）の小さな口。

署名は公式 README の「Python 3 example code」の形（token+t+nonce を HMAC-SHA256 して base64）。
README の本文の書き方（token+t を大文字に）では通らなかった（11/3 の試し・docs/dev_log.md）。
鍵は呼ぶ側が python-dotenv で読んで渡す。この口は鍵を表示もログもしない。
"""

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

import httpx

API = "https://api.switch-bot.com/v1.1"


@dataclass
class Reply:
    """1 回の呼び出しの返り。鍵の入ったヘッダは持たない。"""

    http: int
    status_code: int | None  # SwitchBot の statusCode（100 が成功）
    body: dict
    message: str | None
    roundtrip_s: float


class SwitchBotClient:
    def __init__(self, token: str, secret: str, timeout: float = 15.0):
        self._token = token
        self._secret = secret
        self._http = httpx.Client(base_url=API, timeout=timeout)

    def _headers(self) -> dict:
        t = str(int(round(time.time() * 1000)))
        nonce = str(uuid.uuid4())
        sign = base64.b64encode(
            hmac.new(
                self._secret.encode(), f"{self._token}{t}{nonce}".encode(), hashlib.sha256
            ).digest()
        ).decode()
        return {
            "Authorization": self._token,
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "Content-Type": "application/json; charset=utf8",
        }

    def _call(self, method: str, path: str, body: dict | None = None) -> Reply:
        sent = time.monotonic()
        r = self._http.request(method, path, headers=self._headers(), json=body)
        took = time.monotonic() - sent
        try:
            data = r.json()
        except ValueError:
            data = {}
        return Reply(
            http=r.status_code,
            status_code=data.get("statusCode"),
            body=data.get("body") or {},
            message=data.get("message"),
            roundtrip_s=took,
        )

    def devices(self) -> Reply:
        return self._call("GET", "/devices")

    def status(self, device_id: str) -> Reply:
        return self._call("GET", f"/devices/{device_id}/status")

    def command(self, device_id: str, command: str) -> Reply:
        """入切の命令。本体では守りの層の中からだけ呼ぶ。"""
        return self._call(
            "POST",
            f"/devices/{device_id}/commands",
            {"command": command, "parameter": "default", "commandType": "command"},
        )

    def find_device(self, device_type: str) -> str:
        """機器の ID をファイルに書かないため、その都度一覧から種類で引く。"""
        for d in self.devices().body.get("deviceList", []):
            if d.get("deviceType") == device_type:
                return d["deviceId"]
        raise LookupError(f"{device_type} が機器の一覧にありません")
