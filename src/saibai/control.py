"""受け手の中の「動かす側」：守りの層の判断をプラグに当て、毎分プラグの本当の状態を点検する。

- プラグを動かすのはこの組の `_apply` だけ。`_apply` は守りの層の判断（`Guard.decide`）しか受けない。
- 起動した時は必ず「切」から（`start()`）。
- 毎分プラグの状態を読み、守りの層が「切」なのに入っていれば切る（停電から戻ると前の状態に戻るため）。
- 既定のプラグは模擬（`DummyPlug`）。本物は `SAIBAI_PLUG=switchbot` の時だけ（人が付いている時）。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from saibai import config
from saibai.guard import Guard, Reading, Run, Verdict
from saibai.switchbot import SwitchBotClient
from saibai.vpd import vpd_kpa


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


def read_back(run_dir: Path, now: datetime) -> dict:
    """起こし直した時の帳簿を `control_*.jsonl` から読み戻す（昨日と今日の 2 日分）。

    プラグへの命令（`plug_set`）を順に辿り、入→切の区間と最後に切った時刻を出す。
    最後が入のまま終わっていれば（受け手が入のまま落ちた）、その区間は最後の記録の時刻で閉じる。
    """
    recs: list[dict] = []
    for d in (now - timedelta(days=1), now):
        f = run_dir / f"control_{d:%Y%m%d}.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue  # 落ちた時の書きかけの行
    intervals: list[tuple[datetime, datetime]] = []
    on_since: datetime | None = None
    last_off: datetime | None = None
    last_at: datetime | None = None
    plug: bool | None = None
    for r in recs:
        at = datetime.fromisoformat(r["at"])
        last_at = at
        if r.get("kind") == "plug_set":
            if r["on"] and on_since is None:
                on_since = at
            elif not r["on"] and on_since is not None:
                intervals.append((on_since, at))
                on_since, last_off = None, at
            plug = r.get("plug_after", r["on"] if r.get("ok") else None)
        elif r.get("kind") == "action" and r.get("plug_after") is not None:
            plug = r["plug_after"]
    # 受け手が生きていた最後の時刻＝温湿度の読みの記録の最後（操作が無い間も 30 秒ごとに残る）
    for d in (now - timedelta(days=1), now):
        f = run_dir / f"meter_{d:%Y%m%d}.jsonl"
        if f.exists():
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    at = datetime.fromisoformat(json.loads(line)["read_at"])
                except (ValueError, KeyError):
                    continue
                last_at = max(last_at, at) if last_at else at
                break
    if on_since is not None and last_at is not None:
        intervals.append((on_since, last_at))
        last_off = last_at
    return {"intervals": intervals, "last_off": last_off, "last_at": last_at, "plug": plug}


class Controller:
    """守りの層とプラグの間。操作はすべてここで「操作の記録」（`kind=action`）に 1 件ずつ残す。

    操作の記録の 1 件＝時刻・誰が（手動／定時／オート／守りの層／外から／受け手）・何を（入・切・計画・
    止める・止めを解く・記録なし）・予定の分・受け付けた／断った（理由）・3 秒後に読み直したプラグの状態・
    その時の温度・湿度・VPD（設計書 §5）。
    """

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
        self.latest: tuple[float | None, float] | None = None  # 最後の読み（温度・湿度）
        self.last_gap: tuple[datetime, datetime] | None = None
        self.on_action = []  # 操作の記録が 1 件できるたびに呼ぶ（置き場への写し・段 B）
        self.auto = None  # オートの起動を見に行く糸（段 C・receiver が付ける）
        self._quiet = False
        self._lock = threading.RLock()

    def _log(self, kind: str, **rec) -> dict:
        now = self.now()
        rec = {"at": now.isoformat(timespec="seconds"), "kind": kind, **rec}
        with (self.run_dir / f"control_{now:%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def _action(self, who: str, what: str, **rec) -> dict:
        temp, rh = self.latest if self.latest else (None, None)
        vpd = None if temp is None or rh is None else vpd_kpa(temp, rh)
        rec = self._log(
            "action",
            who=who,
            what=what,
            **{"minutes": None, "accepted": None, "reasons": [], "plug_after": None} | rec,
            temp_c=temp,
            rh_pct=rh,
            vpd_kpa=vpd,
        )
        for f in self.on_action:
            try:
                f(rec)
            except Exception:  # 写しが失敗しても守りは止めない（手元が正本）
                pass
        return rec

    def _apply(self, on: bool, why: str) -> bool | None:
        ok = self.plug.set(on)
        self.plug_actual = on if ok else self.plug.read()
        self.plug_checked_at = self.now()
        self._log(
            "plug_set", on=on, ok=ok, why=why, plug=self.plug.kind, plug_after=self.plug_actual
        )
        return self.plug_actual

    def start(self) -> None:
        """起動した時は必ず切から。前の記録から帳簿を読み戻し、止まっていた間を穴として残す。"""
        with self._lock:
            now = self.now()
            back = read_back(self.run_dir, now)
            self.guard.restore(back["intervals"], back["last_off"])
            gap_after = timedelta(minutes=config.load()["receiver"]["gap_after_min"])
            if back["last_at"] and now - back["last_at"] > gap_after:
                self.note_gap(back["last_at"], now)
            before = self.plug.read()
            if before is not None and back["plug"] is not None and before != back["plug"]:
                self._action(
                    "外から",
                    "入" if before else "切",
                    plug_after=before,
                    note=self._gap_note() or "起動した時に記録と違っていた",
                )
            after = self._apply(False, "起動した時は切から")
            self._action("守りの層", "切", reasons=["起動した時は切から"], plug_after=after)

    def note_gap(self, start: datetime, end: datetime) -> None:
        """最後の記録から空いた間を 1 件残す（振り返りはこの間にかかる操作の効き目を数えない）。"""
        with self._lock:
            self.last_gap = (start, end)
            self._action(
                "受け手",
                "記録なし",
                gap_start=start.isoformat(timespec="seconds"),
                gap_end=end.isoformat(timespec="seconds"),
                note=f"{start:%m/%d %H:%M} から {end:%m/%d %H:%M} まで記録なし（受け手が止まっていた）",
            )

    def _gap_note(self) -> str | None:
        if self.last_gap and self.now() - self.last_gap[1] <= timedelta(minutes=5):
            a, b = self.last_gap
            return f"時刻は記録なしの間（{a:%H:%M}〜{b:%H:%M}）のどこか"
        return None

    def observe(self, read_at: datetime, rh: float, temp: float | None = None) -> None:
        with self._lock:
            self.latest = (temp, rh)
            self.guard.observe(Reading(read_at, rh, temp))

    def tick(self, check_plug: bool) -> None:
        with self._lock:
            d = self.guard.decide(self.now())
            self.last_decision_reasons = d.reasons
            if d.changed:
                self._log("decide", on=d.on, reasons=d.reasons)
                after = self._apply(d.on, "守りの層の判断が変わった")
                if not self._quiet:
                    reasons = d.reasons
                    if not d.on and d.source != "守りの層":
                        reasons = ["予定の分が終わった"]
                    self._action(
                        d.source,
                        "入" if d.on else "切",
                        minutes=d.minutes,
                        accepted=True,
                        reasons=[] if d.on else reasons,
                        plug_after=after,
                    )
                return
            if check_plug:
                actual = self.plug.read()
                self.plug_actual, self.plug_checked_at = actual, self.now()
                if actual is not None and actual != d.on:
                    why = "計画に無い入を切る" if actual else "入のはずが切だった"
                    self._log("plug_mismatch", actual=actual, want=d.on)
                    after = self._apply(d.on, why)
                    self._action(
                        "外から",
                        "入" if actual else "切",
                        reasons=[why],
                        plug_after=after,
                        note=self._gap_note() or "毎分の点検で記録に無い変化を見つけた",
                    )

    def submit(self, runs: list[Run], evidence: list[dict], source: str) -> Verdict:
        """計画を守りの層に出す（オートのエージェント・定時のタイマー）。"""
        with self._lock:
            v = self.guard.submit_plan(runs, evidence, self.now(), source)
            self.last_plan_verdict = {"source": source, "ok": v.ok, "text": v.text()}
            if not self._quiet:
                self._action(
                    source,
                    "計画",
                    accepted=v.ok,
                    reasons=v.reasons,
                    runs=[{"start": r.start.isoformat(), "minutes": r.minutes} for r in runs],
                    minutes=sum(r.minutes for r in runs),
                )
            self.tick(check_plug=False)
            return v

    def set_humidifier(self, on: bool, minutes: int) -> dict:
        """画面のトグル（手動）。入＝「今から minutes 分」の計画として守りの層に出す。切＝計画を捨てる。"""
        with self._lock:
            if on:
                now = self.now()
                ev = [
                    {
                        "device": "人",
                        "metric": "画面のトグル",
                        "value": minutes,
                        "time": now.isoformat(),
                    }
                ]
                self._quiet = True  # 計画と入を 1 件の「手動・入」にまとめて残す
                try:
                    v = self.submit([Run(now, minutes, "手動")], ev, "手動")
                finally:
                    self._quiet = False
                text = v.text()
                reasons = v.reasons
                if v.ok and not self.guard.on:  # 受け付けても入らなかった時は、その理由まで返す
                    text = "受け付けたが、今は入れない：" + "／".join(self.last_decision_reasons)
                    reasons = self.last_decision_reasons
                ok = v.ok and self.guard.on
                self._action(
                    "手動",
                    "入",
                    minutes=minutes,
                    accepted=ok,
                    reasons=reasons,
                    plug_after=self.plug_actual,
                )
                return {
                    "ok": ok,
                    "text": text,
                    "on": self.guard.on,
                    "reasons": self.last_decision_reasons,
                }
            was_on = self.guard.on
            self.guard.cancel()
            self._quiet = True
            try:
                self.tick(check_plug=False)
            finally:
                self._quiet = False
            self._action(
                "手動",
                "切",
                accepted=True,
                reasons=[] if was_on else ["もともと切だった（計画を捨てた）"],
                plug_after=self.plug_actual,
            )
            return {"ok": True, "text": "切った：計画を捨てた。", "on": self.guard.on}

    def stop_all(self, release: bool) -> dict:
        with self._lock:
            if release:
                self.guard.release_stop()
                self._action("手動", "止めを解く", accepted=True)
                return {
                    "ok": True,
                    "text": "止めを解いた：次の計画から受け付ける。",
                    "on": self.guard.on,
                }
            d = self.guard.stop_all(self.now())
            if d.changed or self.plug_actual is not False:
                self._apply(False, "手で止める")
            self._action("手動", "止める", accepted=True, plug_after=self.plug_actual)
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
                "auto": self.auto.status() if self.auto else None,
            }
