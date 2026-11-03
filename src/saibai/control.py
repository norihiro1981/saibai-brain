"""受け手の中の「動かす側」：守りの層の判断をプラグに当て、毎分プラグの本当の状態を点検する。

- プラグを動かすのはこの組の `_apply` だけ。`_apply` は守りの層の判断（`Guard.decide`）しか受けない。
- 起動した時は必ず「切」から（`start()`）。
- 毎分プラグの状態を読み、守りの層が「切」なのに入っていれば切る（停電から戻ると前の状態に戻るため）。
- 既定のプラグは模擬（`DummyPlug`）。本物は `SAIBAI_PLUG=switchbot` の時だけ（人が付いている時）。
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from saibai.guard import Guard, Reading, Run, Verdict
from saibai.switchbot import SwitchBotClient


class Plug(Protocol):
    kind: str

    def read(self) -> bool | None: ...  # 入なら True・読めなければ None

    def set(self, on: bool) -> bool: ...  # 確かめて思いどおりなら True


class DummyPlug:
    """模擬のプラグ。テストと、人が付いていない時の既定。"""

    kind = "dummy"

    def __init__(self, on: bool = False):
        self.on = on
        self.commands: list[bool] = []

    def read(self) -> bool | None:
        return self.on

    def set(self, on: bool) -> bool:
        self.commands.append(on)
        self.on = on
        return True


class SwitchBotPlug:
    """本物のプラグ（SwitchBot プラグミニ）。命令の 3 秒後に `power` を読み直して確かめる（11/3）。"""

    kind = "switchbot"

    def __init__(self, client: SwitchBotClient, device_id: str, settle_s: float = 3.0):
        self.client = client
        self.device_id = device_id
        self.settle_s = settle_s

    def read(self) -> bool | None:
        try:
            r = self.client.status(self.device_id)
        except Exception:
            return None
        p = r.body.get("power")
        return None if p is None else p == "on"

    def set(self, on: bool) -> bool:
        try:
            r = self.client.command(self.device_id, "turnOn" if on else "turnOff")
        except Exception:
            return False
        if r.status_code != 100:
            return False
        time.sleep(self.settle_s)
        return self.read() == on


class Schedule(Protocol):
    def get(self) -> dict: ...

    def set(self, on: bool) -> dict: ...


class NoSchedule:
    def get(self) -> dict:
        return {
            "available": False,
            "reason": "定時の起動はまだ設定されていない（SAIBAI_DEPLOYMENT_ID）",
        }

    def set(self, on: bool) -> dict:
        return self.get()


class DeploymentSchedule:
    """Managed Agents の定時の起動（deployment）の止める・再開（段 2 で API で効くのを確かめた）。"""

    def __init__(self, client, deployment_id: str, cache_s: float = 60.0):
        self.client = client
        self.deployment_id = deployment_id
        self.cache_s = cache_s
        self._cached: tuple[float, dict] | None = None

    def _view(self, d) -> dict:
        return {
            "available": True,
            "on": d.status == "active",
            "status": d.status,
            "upcoming": [t.isoformat() for t in (d.schedule.upcoming_runs_at or [])[:3]],
        }

    def get(self) -> dict:
        if self._cached and time.monotonic() - self._cached[0] < self.cache_s:
            return self._cached[1]
        try:
            v = self._view(self.client.beta.deployments.retrieve(self.deployment_id))
        except Exception as e:
            v = {"available": False, "reason": f"読めない：{type(e).__name__}"}
        self._cached = (time.monotonic(), v)
        return v

    def set(self, on: bool) -> dict:
        op = self.client.beta.deployments.unpause if on else self.client.beta.deployments.pause
        try:
            v = self._view(op(self.deployment_id))
        except Exception as e:
            return {"available": False, "reason": f"切り替えられない：{type(e).__name__}"}
        self._cached = (time.monotonic(), v)
        return v


class Controller:
    def __init__(self, guard: Guard, plug: Plug, schedule: Schedule, run_dir: Path, now_fn):
        self.guard = guard
        self.plug = plug
        self.schedule = schedule
        self.now = now_fn
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.plug_actual: bool | None = None
        self.plug_checked_at: datetime | None = None
        self.last_decision_reasons: list[str] = []
        self.last_plan_verdict: dict | None = None
        self._lock = threading.RLock()

    def _log(self, kind: str, **rec) -> None:
        now = self.now()
        rec = {"at": now.isoformat(timespec="seconds"), "kind": kind, **rec}
        with (self.run_dir / f"control_{now:%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _apply(self, on: bool, why: str) -> None:
        ok = self.plug.set(on)
        self.plug_actual = on if ok else self.plug.read()
        self.plug_checked_at = self.now()
        self._log("plug_set", on=on, ok=ok, why=why, plug=self.plug.kind)

    def start(self) -> None:
        """起動した時は必ず切から。"""
        with self._lock:
            self._apply(False, "起動した時は切から")

    def observe(self, read_at: datetime, rh: float) -> None:
        with self._lock:
            self.guard.observe(Reading(read_at, rh))

    def tick(self, check_plug: bool) -> None:
        with self._lock:
            d = self.guard.decide(self.now())
            self.last_decision_reasons = d.reasons
            if d.changed:
                self._log("decide", on=d.on, reasons=d.reasons)
                self._apply(d.on, "守りの層の判断が変わった")
                return
            if check_plug:
                actual = self.plug.read()
                self.plug_actual, self.plug_checked_at = actual, self.now()
                if actual is not None and actual != d.on:
                    why = "計画に無い入を切る" if actual else "入のはずが切だった"
                    self._log("plug_mismatch", actual=actual, want=d.on)
                    self._apply(d.on, why)

    def submit(self, runs: list[Run], evidence: list[dict], source: str) -> Verdict:
        with self._lock:
            v = self.guard.submit_plan(runs, evidence, self.now())
            self.last_plan_verdict = {"source": source, "ok": v.ok, "text": v.text()}
            self._log(
                "plan",
                source=source,
                ok=v.ok,
                reasons=v.reasons,
                runs=[{"start": r.start.isoformat(), "minutes": r.minutes} for r in runs],
            )
            self.tick(check_plug=False)
            return v

    def set_humidifier(self, on: bool, minutes: int) -> dict:
        """画面のスイッチ。入＝「今から minutes 分」の計画として守りの層に出す。切＝計画を捨てる。"""
        with self._lock:
            if on:
                now = self.now()
                ev = [
                    {
                        "device": "人",
                        "metric": "画面のスイッチ",
                        "value": minutes,
                        "time": now.isoformat(),
                    }
                ]
                v = self.submit([Run(now, minutes)], ev, "画面のスイッチ")
                text = v.text()
                if v.ok and not self.guard.on:  # 受け付けても入らなかった時は、その理由まで返す
                    text = "受け付けたが、今は入れない：" + "／".join(self.last_decision_reasons)
                return {
                    "ok": v.ok and self.guard.on,
                    "text": text,
                    "on": self.guard.on,
                    "reasons": self.last_decision_reasons,
                }
            self.guard.cancel()
            self._log("cancel", source="画面のスイッチ")
            self.tick(check_plug=False)
            return {"ok": True, "text": "切った：計画を捨てた。", "on": self.guard.on}

    def stop_all(self, release: bool) -> dict:
        with self._lock:
            if release:
                self.guard.release_stop()
                self._log("release_stop")
                return {
                    "ok": True,
                    "text": "止めを解いた：次の計画から受け付ける。",
                    "on": self.guard.on,
                }
            d = self.guard.stop_all(self.now())
            self._log("stop_all")
            if d.changed or self.plug_actual is not False:
                self._apply(False, "手で止める")
            return {"ok": True, "text": "止めた：止めを解くまで入れない。", "on": self.guard.on}

    def set_schedule(self, on: bool) -> dict:
        with self._lock:
            v = self.schedule.set(on)
            self._log("schedule", on=on, result=v)
            return v

    def status(self) -> dict:
        with self._lock:
            now = self.now()
            return {
                "plug": {
                    "kind": self.plug.kind,
                    "want_on": self.guard.on,
                    "actual_on": self.plug_actual,
                    "checked_at": self.plug_checked_at.isoformat()
                    if self.plug_checked_at
                    else None,
                    "off_reasons": self.last_decision_reasons,
                },
                "guard": self.guard.snapshot(now),
                "last_plan": self.last_plan_verdict,
                "schedule": self.schedule.get(),
            }
