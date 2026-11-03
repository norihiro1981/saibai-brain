"""守りの層：計画を入／切に変える、決まった規則だけのコード（設計書 §5）。AI には判断させない。

使い方は 2 つだけ。
- `submit_plan(...)`：計画（いつ・何分）を確かめ、受け付けるか、理由を付けて断る。
- `decide(now)`：毎分呼ぶ。今プラグを入れておいてよいかを決める。

「止める」と「起動した時は切から」は全部に優先する。上限は「次の判断まで最大 1 tick 入ったまま」を見込んで、
その 1 tick を足しても越えない所で切る＝判断が tick ごとに来る限り、どんな計画でも上限を越えない。
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from saibai import config

JST = ZoneInfo("Asia/Tokyo")
MIN = timedelta(minutes=1)


@dataclass(frozen=True)
class Limits:
    """守りの層の線。値と理由は config/saibai.toml の [guard] に書く（ここに数を置かない）。"""

    max_continuous: timedelta
    max_daily: timedelta
    no_start_rh: float
    stop_rh: float
    min_gap: timedelta
    stale_after: timedelta
    stuck_after: timedelta
    no_rise_after: timedelta
    no_rise_pt: float
    jump_pt: float
    jump_within: timedelta
    jump_recent: timedelta
    plan_timeout: timedelta
    tick: timedelta
    horizon: timedelta


def limits_from_config(kind: str, cfg: dict | None = None) -> Limits:
    """kind＝"real"（本物の実機）か "replay"（再生）。"""
    g = (cfg or config.load())["guard"]
    m = lambda k: timedelta(minutes=g[k])  # noqa: E731
    h = lambda k: timedelta(hours=g[k])  # noqa: E731
    return Limits(
        max_continuous=timedelta(minutes=g[kind]["max_continuous_min"]),
        max_daily=timedelta(minutes=g[kind]["max_daily_min"]),
        no_start_rh=float(g["no_start_rh"]),
        stop_rh=float(g["stop_rh"]),
        min_gap=m("min_gap_min"),
        stale_after=m("stale_after_min"),
        stuck_after=m("stuck_after_min"),
        no_rise_after=m("no_rise_after_min"),
        no_rise_pt=float(g["no_rise_pt"]),
        jump_pt=float(g["jump_pt"]),
        jump_within=m("jump_within_min"),
        jump_recent=m("jump_recent_min"),
        plan_timeout=h("plan_timeout_h"),
        tick=m("tick_min"),
        horizon=h("horizon_h"),
    )


# 再生（模擬の栽培室）の線
REPLAY = limits_from_config("replay")
# 本物の実機の線（人が付く日）＝再生より短く
REAL = limits_from_config("real")


@dataclass(frozen=True)
class Reading:
    at: datetime
    rh: float
    temp: float | None = None


@dataclass(frozen=True)
class Run:
    start: datetime
    minutes: int
    source: str = "オート"  # 誰が出したか（手動／定時／オート）＝操作の記録の「誰が」

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: list[str]

    def text(self) -> str:
        if self.ok:
            return "受け付けた：守りの層の線をすべて満たす。"
        return "断った：" + "／".join(self.reasons)


@dataclass(frozen=True)
class Decision:
    on: bool
    changed: bool
    reasons: list[str]  # 切にした（入れなかった）理由。入の時は空
    source: str = (
        ""  # 変わった時の「誰が」：入＝その運転を出した人・予定どおりの切＝同じ・ほかの切＝守りの層
    )
    minutes: int | None = None  # 入にした時、その運転の予定の分


def jst_day(t: datetime) -> date:
    return t.astimezone(JST).date()


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=JST)
    return start, start + timedelta(days=1)


def overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> timedelta:
    return max(timedelta(0), min(a1, b1) - max(a0, b0))


def is_humidity(metric: str) -> bool:
    m = metric.lower()
    return "rh" in m or "humid" in m or "湿度" in m


@dataclass
class Guard:
    limits: Limits
    on: bool = False  # 起動した時は必ず切から
    on_since: datetime | None = None
    last_off: datetime | None = None
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    last_plan_at: datetime | None = None
    manual_stop: bool = False
    rh_at_on: float | None = None  # 入れた時の湿度（効かない運転を見るため）
    on_source: str = ""  # 今の入を出した人
    # 30 秒ごとの読みで 200 分＝止まりの線（90 分）より長く持つ
    readings: deque = field(default_factory=lambda: deque(maxlen=400))

    # ---- センサー ----
    def observe(self, r: Reading) -> None:
        if self.readings and r.at <= self.readings[-1].at:
            return
        self.readings.append(r)

    def sensor_problems(self, now: datetime) -> list[str]:
        """湿度の読みが信じられない理由の一覧（空なら信じてよい）。"""
        L = self.limits
        if not self.readings:
            return ["湿度の読みが無い"]
        last = self.readings[-1]
        out = []
        age = now - last.at
        if age > L.stale_after:
            out.append(
                f"最新の湿度が {age.total_seconds() / 60:.0f} 分古い（{L.stale_after.seconds // 60} 分まで）"
            )
        # 温度と湿度の組が最後に変わった時刻。手元の記録の中で一度も変わっていなければ最初の読みの時刻
        rs = self.readings
        n = len(rs)
        # 後ろから、温度と湿度の組が同じ値の続く所までさかのぼる（記録の中で一度も変わらなければ最初の読み）
        changed_at = rs[0].at
        for i in range(n - 1, 0, -1):
            if (rs[i - 1].rh, rs[i - 1].temp) != (rs[i].rh, rs[i].temp):
                changed_at = rs[i].at
                break
        if last.at - changed_at >= L.stuck_after:
            t = "" if last.temp is None else f"・{last.temp}℃"
            out.append(f"温度と湿度が {changed_at:%H:%M} から {last.rh}%{t} のまま変わらない")
        # 跳びは新しい方から、jump_recent の内だけを見る
        for i in range(n - 1, 0, -1):
            a, b = rs[i - 1], rs[i]
            if now - b.at > L.jump_recent:
                break
            if b.at - a.at <= L.jump_within and abs(b.rh - a.rh) >= L.jump_pt:
                out.append(f"湿度が {a.at:%H:%M}→{b.at:%H:%M} で {a.rh}→{b.rh}% と跳んだ")
                break
        return out

    def current_rh(self) -> float | None:
        return self.readings[-1].rh if self.readings else None

    # ---- 運転の時間の帳簿 ----
    def used_on(self, d: date, now: datetime) -> timedelta:
        d0, d1 = day_bounds(d)
        total = sum((overlap(a, b, d0, d1) for a, b in self.intervals), timedelta(0))
        if self.on and self.on_since:
            total += overlap(self.on_since, now, d0, d1)
        return total

    def restore(
        self, intervals: list[tuple[datetime, datetime]], last_off: datetime | None
    ) -> None:
        """起こし直した時、記録から読み戻した帳簿（動いた区間・最後に切った時刻）を入れる。"""
        self.intervals = list(intervals)
        self.last_off = last_off

    def _prune(self, now: datetime) -> None:
        keep_from = now - timedelta(days=2)
        self.intervals = [(a, b) for a, b in self.intervals if b > keep_from]
        self.runs = [r for r in self.runs if r.end > now]

    # ---- 計画 ----
    def submit_plan(
        self, runs: list[Run], evidence: list[dict], now: datetime, source: str | None = None
    ) -> Verdict:
        """計画を確かめる。受け付けたら、同じ出し手（手動／定時／オート）の前の計画だけを置き換える
        （ほかの出し手の運転は残し、合わせた全体で間と 1 日の上限を確かめる）。runs が空＝この出し手は入れない。"""
        L = self.limits
        src = source or (runs[0].source if runs else "オート")
        reasons: list[str] = []
        if self.manual_stop:
            return Verdict(False, ["手で止めている（止めを解くまで計画は受けない）"])
        if not evidence:
            reasons.append("根拠の数値と時刻が無い")
        problems = self.sensor_problems(now)
        if problems and any(is_humidity(str(e.get("metric", ""))) for e in evidence):
            reasons.append("根拠の湿度が疑わしい：" + "・".join(problems))
        if any(r.start.tzinfo is None for r in runs):
            return Verdict(False, ["始まりの時刻に時差（+09:00）が付いていない"])
        runs = sorted(runs, key=lambda r: r.start)
        others = [r for r in self.runs if r.source != src and r.end > now]
        for r in runs:
            if not 1 <= r.minutes <= L.max_continuous / MIN:
                reasons.append(
                    f"{r.start:%H:%M} から {r.minutes} 分（1 回は 1〜{L.max_continuous // MIN} 分）"
                )
            if r.start < now - L.tick:
                reasons.append(f"{r.start:%m/%d %H:%M} は過ぎている")
            if r.start > now + L.horizon:
                reasons.append(
                    f"{r.start:%m/%d %H:%M} は先すぎる（{L.horizon // timedelta(hours=1)} 時間まで）"
                )
        combined = sorted(others + runs, key=lambda r: r.start)
        for a, b in zip(combined, combined[1:], strict=False):
            if a in others and b in others:
                continue  # すでに受け付けた運転どうし
            if b.start - a.end < L.min_gap:
                reasons.append(
                    f"{a.end:%H:%M} に切って {b.start:%H:%M} に入れる＝間が {L.min_gap // MIN} 分に足りない"
                )
        if runs and not self.on and self.last_off and runs[0].start - self.last_off < L.min_gap:
            reasons.append(
                f"{self.last_off:%H:%M} に切ったばかり＝{(self.last_off + L.min_gap):%H:%M} より前には入れない"
            )
        days: dict[date, timedelta] = {}
        # ほかの出し手の運転は、これから動く分だけを足す（動いた分は used に入っている）
        ahead = [(max(r.start, now), r.end) for r in others] + [(r.start, r.end) for r in runs]
        for a0, a1 in ahead:
            d = jst_day(a0)
            while True:
                d0, d1 = day_bounds(d)
                part = overlap(a0, a1, d0, d1)
                if part <= timedelta(0):
                    break
                days[d] = days.get(d, timedelta(0)) + part
                d += timedelta(days=1)
        for d, planned in days.items():
            used = self.used_on(d, now)
            if used + planned > L.max_daily:
                reasons.append(
                    f"{d:%m/%d} は使った {used // MIN} 分＋計画 {planned // MIN} 分が 1 日の上限 {L.max_daily // MIN} 分を越える"
                )
        rh = self.current_rh()
        starts_now = bool(runs) and runs[0].start <= now + L.tick
        if starts_now and rh is not None and rh >= L.no_start_rh:
            reasons.append(f"今の湿度が {rh}%（{L.no_start_rh:.0f}% 以上では入れない）")
        # 今すぐ始める計画は、根拠が湿度でなくても、センサーが疑わしい間は decide が入れない
        # ＝受け付けたと返して入らない、を起こさないよう、ここで断る
        if starts_now and problems and not any(r.startswith("根拠の湿度") for r in reasons):
            reasons.append("今はセンサーが疑わしいので入れない：" + "・".join(problems))
        if reasons:
            return Verdict(False, reasons)
        self.runs = combined
        self.last_plan_at = now
        return Verdict(True, [])

    def cancel(self) -> None:
        """今の計画を捨てる（次の判断で切）。"""
        self.runs = []

    def stop_all(self, now: datetime) -> Decision:
        """手で止める：すべてに優先。止めを解くまで入れない。"""
        self.manual_stop = True
        self.runs = []
        return self.decide(now)

    def release_stop(self) -> None:
        self.manual_stop = False

    # ---- 判断（毎分） ----
    def _active_run(self, now: datetime) -> Run | None:
        for r in self.runs:
            if r.start <= now < r.end:
                return r
        return None

    def decide(self, now: datetime) -> Decision:
        L = self.limits
        self._prune(now)
        off: list[str] = []
        drop_run = False
        rh = self.current_rh()
        if self.manual_stop:
            off.append("手で止めている")
        problems = self.sensor_problems(now)
        off += problems
        if self.last_plan_at is None or now - self.last_plan_at >= L.plan_timeout:
            if self.runs or self.on:
                off.append(f"計画が {L.plan_timeout // timedelta(hours=1)} 時間来ていない")
            self.runs = []
        if not problems and rh is not None and rh >= L.stop_rh:
            off.append(f"湿度が {rh}%（{L.stop_rh:.0f}% で切る）")
            drop_run = True
        run = self._active_run(now)
        if run is None:
            off.append("今は計画の運転の時間ではない")
        used_today = self.used_on(jst_day(now), now)
        if used_today + L.tick > L.max_daily:
            off.append(f"今日は {used_today // MIN} 分動いた（1 日 {L.max_daily // MIN} 分まで）")
            drop_run = True
        if self.on:
            cont = now - self.on_since
            if cont + L.tick > L.max_continuous:
                off.append(
                    f"続けて {cont // MIN} 分動いた（続けて {L.max_continuous // MIN} 分まで）"
                )
                drop_run = True
            rise_ok = rh is None or self.rh_at_on is None or rh - self.rh_at_on >= L.no_rise_pt
            if cont >= L.no_rise_after and not rise_ok:
                off.append(
                    f"入れて {cont // MIN} 分たつのに湿度が {self.rh_at_on}→{rh}%"
                    f"（{L.no_rise_pt:.0f} pt も上がらない）"
                )
                drop_run = True
        else:
            if rh is not None and rh >= L.no_start_rh:
                off.append(f"湿度が {rh}%（{L.no_start_rh:.0f}% 以上では入れない）")
            if self.last_off and now - self.last_off < L.min_gap:
                off.append(f"{self.last_off:%H:%M} に切ってから {L.min_gap // MIN} 分たっていない")
        if drop_run and run is not None:
            self.runs = [r for r in self.runs if r is not run]

        want = not off
        changed = want != self.on
        source, minutes = "", None
        if changed and want:
            self.on, self.on_since, self.rh_at_on = True, now, rh
            self.on_source = source = run.source
            minutes = run.minutes
        elif changed:
            self.intervals.append((self.on_since, now))
            self.on, self.on_since, self.last_off = False, None, now
            # 予定の分が終わっただけなら、その運転を出した人の切。ほかの理由なら守りの層の切
            source = self.on_source if off == ["今は計画の運転の時間ではない"] else "守りの層"
        return Decision(self.on, changed, off, source, minutes)

    def snapshot(self, now: datetime) -> dict:
        return {
            "on": self.on,
            "on_since": self.on_since.isoformat() if self.on_since else None,
            "manual_stop": self.manual_stop,
            "used_today_min": round(self.used_on(jst_day(now), now) / MIN, 1),
            "max_daily_min": self.limits.max_daily // MIN,
            "max_continuous_min": self.limits.max_continuous // MIN,
            "runs": [
                {"start": r.start.isoformat(), "minutes": r.minutes, "source": r.source}
                for r in self.runs
            ],
            "last_plan_at": self.last_plan_at.isoformat() if self.last_plan_at else None,
            "sensor_problems": self.sensor_problems(now),
        }
