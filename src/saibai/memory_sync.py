"""手元の正本の記録を、記憶の置き場（Managed Agents の memory store）へ写す（設計書 §4）。

- 温湿度の記録：1 時間 1 ファイルの CSV（`/meter/YYYY-MM-DD/HH.csv`）。5 分ごとに今の時間のファイルを
  書き直し（前の版の sha を precondition に）、時間が変わったら前の時間を最後に 1 回写して閉じる。
- 操作の記録：1 日 1 ファイル（`/actions/YYYY-MM-DD.jsonl`）。操作が 1 件できるたびに、その日の手元の
  操作の記録を丸ごと写す＝手元と同じ中身になる（送り損ねた分は次の回に自然に入る）。
- 置き場は写し。消えても手元から作り直せる。鍵は入れない（記録に鍵は無い）。
- 写しが失敗しても守りは止めない。失敗した物は「まだ」の印のまま残し、次の回に送り直す。
"""

import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
METER_COLS = ("read_at", "temp_c", "rh_pct", "vpd_kpa")


def meter_path(hour: datetime) -> str:
    return f"/meter/{hour:%Y-%m-%d}/{hour:%H}.csv"


def actions_path(day: datetime) -> str:
    return f"/actions/{day:%Y-%m-%d}.jsonl"


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # 落ちた時の書きかけの行
    return out


def meter_csv(run_dir: Path, hour: datetime) -> str:
    """手元の温湿度の記録から、その 1 時間の読めた行だけを CSV にする（読めなかった回は行にしない＝穴）。"""
    lines = [",".join(METER_COLS)]
    key = hour.strftime("%Y-%m-%dT%H")
    for r in _jsonl(run_dir / f"meter_{hour:%Y%m%d}.jsonl"):
        if r.get("ok") and r.get("read_at", "").startswith(key):
            lines.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in METER_COLS))
    return "\n".join(lines) + "\n"


def actions_jsonl(run_dir: Path, day: datetime) -> str:
    recs = [r for r in _jsonl(run_dir / f"control_{day:%Y%m%d}.jsonl") if r.get("kind") == "action"]
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


class MemorySync:
    """置き場への写し。`client` は anthropic.Anthropic（テストでは模擬）。"""

    def __init__(self, client, meter_store: str, actions_store: str, run_dir: Path, now_fn):
        self.client = client
        self.meter_store = meter_store
        self.actions_store = actions_store
        self.run_dir = run_dir
        self.now = now_fn
        self.state_file = run_dir / "memory_sync.json"
        self.state = self._load_state()
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_ok_at: datetime | None = None

    # ---- 印（まだ送れていない物）----
    def _load_state(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            s = {}
        return {"dirty_hours": s.get("dirty_hours", []), "dirty_days": s.get("dirty_days", [])}

    def _save_state(self) -> None:
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False))

    def _mark(self, key: str, value: str) -> None:
        if value not in self.state[key]:
            self.state[key].append(value)

    # ---- 置き場の 1 ファイルを中身ごと書く ----
    def _put(self, store: str, path: str, content: str) -> None:
        """在れば前の版の sha を precondition にして書き直し、無ければ作る。中身が同じなら何もしない。"""
        found = None
        prefix = path.rsplit("/", 1)[0] + "/"
        for m in self.client.beta.memory_stores.memories.list(store, path_prefix=prefix):
            if getattr(m, "type", "memory") == "memory" and m.path == path:
                found = m
                break
        if found is None:
            self.client.beta.memory_stores.memories.create(store, path=path, content=content)
            return
        if found.content_sha256 == hashlib.sha256(content.encode()).hexdigest():
            return
        self.client.beta.memory_stores.memories.update(
            found.id,
            memory_store_id=store,
            content=content,
            precondition={"type": "content_sha256", "content_sha256": found.content_sha256},
        )

    # ---- 呼ぶ口 ----
    def kick_meter(self) -> None:
        """5 分ごと・セッションを始める前：今の時間（と、閉じていない前の時間）を写す。"""
        now = self.now().astimezone(JST)
        hour = now.replace(minute=0, second=0, microsecond=0)
        with self._lock:
            self._mark("dirty_hours", hour.isoformat())
            # 今の時間より前の「まだ」は、最後の 1 回を写して閉じる
            self._flush("dirty_hours", self._push_hour, keep=hour.isoformat())

    def kick_actions(self, rec: dict | None = None) -> None:
        """操作が 1 件できるたび：その日の操作の記録を写す。"""
        at = datetime.fromisoformat(rec["at"]) if rec else self.now()
        with self._lock:
            self._mark("dirty_days", at.astimezone(JST).date().isoformat())
            self._flush("dirty_days", self._push_day)

    def _push_hour(self, key: str) -> None:
        hour = datetime.fromisoformat(key)
        self._put(self.meter_store, meter_path(hour), meter_csv(self.run_dir, hour))

    def _push_day(self, key: str) -> None:
        day = datetime.fromisoformat(key)
        content = actions_jsonl(self.run_dir, day)
        if content:
            self._put(self.actions_store, actions_path(day), content)

    def _flush(self, key: str, push, keep: str | None = None) -> None:
        left = []
        for v in sorted(self.state[key]):
            try:
                push(v)
                self.last_ok_at = self.now()
                if v == keep:
                    left.append(v)  # 今の時間はまだ閉じない（次の 5 分も書き足す）
            except Exception as e:  # 通信の失敗も、precondition の食い違いも、次の回に送り直す
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                left.append(v)
        self.state[key] = left
        self._save_state()

    def put_weather(self, w) -> None:
        """外の天気（v0.7）を温湿度の置き場の /weather/ に写す：今日の 1 時間値の CSV と最新の予報の要約。
        失敗しても次の更新（10 分後）で丸ごと書き直す＝印は要らない。"""
        now = self.now().astimezone(JST)
        with self._lock:
            try:
                self._put(self.meter_store, f"/weather/{now:%Y-%m-%d}.csv", w.hourly_csv(now))
                self._put(self.meter_store, "/weather/forecast.md", w.forecast_md())
                self.last_ok_at = self.now()
            except Exception as e:
                self.last_error = f"天気：{type(e).__name__}: {str(e)[:200]}"

    def status(self) -> dict:
        return {
            "pending_hours": list(self.state["dirty_hours"]),
            "pending_days": list(self.state["dirty_days"]),
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
        }


def start_background(sync: MemorySync, every: timedelta = timedelta(minutes=5)):
    """5 分ごとに温湿度を写す裏の糸。操作の記録は `kick_actions` を裏の糸で呼ぶ（守りの層を待たせない）。"""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            sync.kick_meter()
            stop.wait(every.total_seconds())

    threading.Thread(target=loop, daemon=True).start()

    def on_action(rec: dict) -> None:
        threading.Thread(target=sync.kick_actions, args=(rec,), daemon=True).start()

    return stop, on_action
