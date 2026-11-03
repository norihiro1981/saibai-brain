"""受け手（手元に常駐）：温湿度計をクラウドから 30 秒ごとに読み、読んだ時刻つきで貯める。

- Claude アプリの外（ターミナル・自動起動）で動かす＝アプリや会話を閉じても記録が止まらない。
- 今の状態は 127.0.0.1 の HTTP（GET /status）でだけ出す。MCP サーバーはこれに聞く薄い口（設計書 §9）。
- クラウドの値には測った時刻が付かない＝ここで付けるのは「読んだ時刻」。値が最後に変わった時刻も持ち、
  止まりの見分け（設計書 §8 の宿題）に使う。
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from saibai import config
from saibai.control import Controller, DeploymentSchedule, DummyPlug, NoSchedule, SwitchBotPlug
from saibai.guard import REAL, Guard
from saibai.switchbot import SwitchBotClient
from saibai.vpd import vpd_kpa

JST = ZoneInfo("Asia/Tokyo")
READ_EVERY_S = config.load()["receiver"]["read_every_s"]
DEFAULT_PORT = 8765
ROOT = Path(__file__).resolve().parents[2]


def now_jst() -> datetime:
    return datetime.now(JST)


class MeterLog:
    """読みを JSON Lines に貯め、最新の 1 件と「値が最後に変わった時刻」を覚える。"""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = now_jst()
        self.latest: dict | None = None  # 最後に読めた値
        self.last_attempt: dict | None = None  # 最後の試み（失敗も含む）
        self.changed_at: datetime | None = None
        self.consecutive_errors = 0
        self._lock = threading.Lock()

    def add(self, rec: dict) -> None:
        with self._lock:
            self.last_attempt = rec
            if rec["ok"]:
                prev = self.latest
                same = prev is not None and all(prev[k] == rec[k] for k in ("temp_c", "rh_pct"))
                if not same:
                    self.changed_at = datetime.fromisoformat(rec["read_at"])
                self.latest = rec
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
            day = rec["read_at"][:10].replace("-", "")
            with (self.run_dir / f"meter_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def status(self, now: datetime) -> dict:
        with self._lock:
            m = None
            if self.latest:
                read_at = datetime.fromisoformat(self.latest["read_at"])
                m = {
                    "temp_c": self.latest["temp_c"],
                    "rh_pct": self.latest["rh_pct"],
                    "vpd_kpa": self.latest.get("vpd_kpa"),
                    "battery": self.latest.get("battery"),
                    "read_at": self.latest["read_at"],
                    "age_s": round((now - read_at).total_seconds(), 1),
                    "unchanged_since": self.changed_at.isoformat() if self.changed_at else None,
                }
            return {
                "now": now.isoformat(),
                "receiver": {
                    "up": True,
                    "started_at": self.started_at.isoformat(),
                    "read_every_s": READ_EVERY_S,
                },
                "meter": m,
                "last_read_ok": self.last_attempt["ok"] if self.last_attempt else None,
                "last_error": None
                if not self.last_attempt or self.last_attempt["ok"]
                else self.last_attempt.get("error"),
                "consecutive_errors": self.consecutive_errors,
            }


def read_meter(client: SwitchBotClient, meter_id: str) -> dict:
    """1 回読む。失敗しても例外にせず、印を付けた記録を返す。"""
    read_at = now_jst().isoformat(timespec="seconds")
    try:
        r = client.status(meter_id)
    except Exception as e:  # 通信の失敗も記録に残して次の回に回す
        return {"read_at": read_at, "ok": False, "error": f"{type(e).__name__}: {e}"}
    b = r.body
    ok = r.http == 200 and r.status_code == 100 and "humidity" in b
    rec = {
        "read_at": read_at,
        "ok": ok,
        "http": r.http,
        "status_code": r.status_code,
        "roundtrip_s": round(r.roundtrip_s, 3),
        "temp_c": b.get("temperature"),
        "rh_pct": b.get("humidity"),
        "battery": b.get("battery"),
    }
    if ok and rec["temp_c"] is not None:
        rec["vpd_kpa"] = vpd_kpa(float(rec["temp_c"]), float(rec["rh_pct"]))
    if not ok:
        rec["error"] = r.message or "値が返らなかった"
        rec["body_keys"] = sorted(b)  # 宿題 ①：ハブを抜いた時に何が返るかを残す
    return rec


def history(run_dir: Path, now: datetime, hours: float = 24.0) -> dict:
    """チャート用：温度・湿度・VPD（読めた回だけ）と操作の記録。2 分より空いた所は null の点を挟む（穴＝線でつながない）。
    6 時間より長い時は 1 分に 1 点に間引く（MCP の返りを小さく）。"""
    gap = timedelta(minutes=config.load()["receiver"]["gap_after_min"])
    since = now - timedelta(hours=hours)
    step = timedelta(seconds=55) if hours > 6 else timedelta(0)
    pts: list = []
    acts: list = []
    prev = None
    days = sorted({(since + timedelta(days=i)).date() for i in range(int(hours // 24) + 2)})
    for d in days:
        if d > now.date():
            break
        for name, out in ((f"meter_{d:%Y%m%d}.jsonl", "m"), (f"control_{d:%Y%m%d}.jsonl", "c")):
            f = run_dir / name
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if out == "c":
                    if r.get("kind") == "action" and datetime.fromisoformat(r["at"]) >= since:
                        acts.append(
                            {
                                k: r.get(k)
                                for k in (
                                    "at",
                                    "who",
                                    "what",
                                    "minutes",
                                    "accepted",
                                    "reasons",
                                    "plug_after",
                                    "note",
                                    "gap_start",
                                    "gap_end",
                                )
                            }
                        )
                    continue
                if not r.get("ok"):
                    continue
                t = datetime.fromisoformat(r["read_at"])
                if t < since:
                    continue
                if prev is not None and t - prev > gap:
                    pts.append([prev.isoformat(timespec="seconds"), None, None, None])
                if prev is not None and step and t - prev < step:
                    continue
                pts.append([r["read_at"], r.get("temp_c"), r.get("rh_pct"), r.get("vpd_kpa")])
                prev = t
    return {
        "now": now.isoformat(timespec="seconds"),
        "hours": hours,
        "columns": ["read_at", "temp_c", "rh_pct", "vpd_kpa"],
        "points": pts,
        "actions": acts,
        "target": config.load()["target"],
    }


def make_handler(log: MeterLog, ctrl: Controller | None = None):
    """127.0.0.1 だけで待つ口。POST はすべて Controller を通る＝守りの層を通る。"""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj: dict, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # http.server の決まった名
            if self.path.startswith("/history"):
                q = parse_qs(urlparse(self.path).query)
                hours = min(48.0, max(0.5, float((q.get("hours") or ["24"])[0])))
                self._send(history(log.run_dir, now_jst(), hours))
                return
            if self.path != "/status":
                self.send_error(404)
                return
            out = log.status(now_jst())
            if ctrl:
                out |= ctrl.status()
            self._send(out)

        def do_POST(self):
            if ctrl is None:
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._send({"ok": False, "text": "JSON が読めない"}, 400)
                return
            if self.path == "/humidifier":
                minutes = int(req.get("minutes", 30))
                self._send(ctrl.set_humidifier(bool(req.get("on")), minutes))
            elif self.path == "/stop":
                self._send(ctrl.stop_all(bool(req.get("release", False))))
            elif self.path in ("/auto", "/schedule"):
                self._send(ctrl.set_schedule(bool(req.get("on"))))
            elif self.path == "/timer":
                m = req.get("minutes")
                self._send(
                    ctrl.set_timer(
                        bool(req.get("on")), req.get("start"), None if m is None else int(m)
                    )
                )
            else:
                self.send_error(404)

        def log_message(self, *args):  # 毎回の問い合わせは画面に出さない
            pass

    return Handler


def start_memory_sync(run_dir: Path):
    """記憶の置き場（run/agents.json に ID があれば）へ、温湿度を 5 分ごと・操作を 1 件ずつ写す。"""
    ids_file = run_dir / "agents.json"
    if not ids_file.exists():
        return None
    ids = json.loads(ids_file.read_text())
    if "meter" not in ids or "actions" not in ids:
        return None
    import anthropic

    from saibai.memory_sync import MemorySync, start_background

    ms = MemorySync(anthropic.Anthropic(), ids["meter"], ids["actions"], run_dir, now_jst)
    stop, on_action = start_background(ms)
    print(f"置き場へ写す：温湿度＝5 分ごと（{ids['meter']}）・操作＝1 件ずつ（{ids['actions']}）")
    return ms, on_action


def main() -> None:
    load_dotenv(ROOT / ".env")
    client = SwitchBotClient(os.environ["SWITCHBOT_TOKEN"], os.environ["SWITCHBOT_SECRET"])
    meter_id = os.environ.get("SWITCHBOT_METER_ID") or client.find_device("MeterPlus")
    port = int(os.environ.get("SAIBAI_RECEIVER_PORT", DEFAULT_PORT))
    run_dir = Path(os.environ.get("SAIBAI_RUN_DIR", ROOT / "run"))
    log = MeterLog(run_dir)

    # 本物のプラグは、人が付いている時に SAIBAI_PLUG=switchbot を付けて起動した時だけ
    if os.environ.get("SAIBAI_PLUG") == "switchbot":
        plug_id = os.environ.get("SWITCHBOT_PLUG_ID") or client.find_device("Plug Mini (JP)")
        plug = SwitchBotPlug(client, plug_id)
    else:
        plug = DummyPlug()
    # オートの入切＝定時の起動（run/agents.json の deployment）の止める・再開
    ids = (
        json.loads((run_dir / "agents.json").read_text())
        if (run_dir / "agents.json").exists()
        else {}
    )
    schedule = NoSchedule()
    if dep := ids.get("deployment") or os.environ.get("SAIBAI_DEPLOYMENT_ID"):
        import anthropic

        schedule = DeploymentSchedule(anthropic.Anthropic(), dep)
    ctrl = Controller(Guard(REAL), plug, schedule, run_dir, now_jst)
    sync = start_memory_sync(run_dir)
    if sync:
        ctrl.on_action.append(sync[1])
    ctrl.start()
    if ids.get("deployment") and ids.get("control"):
        import anthropic

        from saibai.auto import AutoRunner

        ctrl.auto = AutoRunner(
            anthropic.Anthropic(), ids, ctrl, run_dir, now_jst, sync=sync[0] if sync else None
        )
        ctrl.auto.start()
        print(f"オート：定時の起動 {ids['deployment']} を見に行く（振り返り→計画）")

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(log, ctrl))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        f"受け手を起動：http://127.0.0.1:{port}/status・{READ_EVERY_S} 秒ごとに読む・記録＝{run_dir}"
    )
    print(
        f"プラグ＝{plug.kind}（起動したので切にした）・守りの層＝実機の線（続けて 60 分・1 日 120 分）"
    )

    gap_after = timedelta(minutes=config.load()["receiver"]["gap_after_min"])
    prev = now_jst()
    n = 0
    while True:
        t0 = time.monotonic()
        # Mac が眠った・止まっていた間は読みが抜ける（クラウドに過去の値の口は無い）＝穴として残す
        if (t := now_jst()) - prev > gap_after:
            ctrl.note_gap(prev, t)
        prev = t
        rec = read_meter(client, meter_id)
        log.add(rec)
        if rec["ok"]:
            ctrl.observe(
                datetime.fromisoformat(rec["read_at"]), float(rec["rh_pct"]), rec.get("temp_c")
            )
        # 判断は 30 秒ごと（守りの層の 1 tick＝60 秒より短い）・プラグの点検は毎分
        ctrl.tick(check_plug=n % 2 == 0)
        n += 1
        mark = "○" if rec["ok"] else "×"
        print(
            f"{rec['read_at']} {mark} {rec.get('temp_c')}℃ {rec.get('rh_pct')}% "
            f"加湿器={'入' if ctrl.guard.on else '切'} {rec.get('error', '')}"
        )
        time.sleep(max(0.0, READ_EVERY_S - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
