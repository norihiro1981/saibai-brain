"""再生と評価（設計書 v0.8 §7）：同じ窓を 4 通り（実運転・閾値だけ・1 体・チーム）で流す。

- 窓＝季節ごとに 7 日（夏 9/3〜9/9・秋 10/20〜10/26）。起動＝再生の時計で 3 時間ごと（窓 1 つで 56 回）。
- 部屋＝模擬の栽培室（sim.Room＝記録＋加湿器の差）。判断＝守りの層の再生の組（75%／85% は残す）。
- **先読みの禁止**：各回に渡すのはその時刻までの記録だけ（`World` が作る・テストで確かめる）。
  再生ごとに記憶の置き場（温湿度・操作・経験）を新しく作る。経験の最初の版は窓の始まりより前の記録だけから。
- 実運転と閾値はトークン 0。API を使うのは 1 体とチームだけ。1 回の上限 $0.50・窓 1 つ×1 通りで $10 を越えそうなら止める。
- 各回の答え（submit_plan の入力）は `run/replay/<窓>_<通り>/wakes.jsonl` に残す＝落ちても続きから（同じ入力なら同じ結果）。
"""

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from saibai import config, sim
from saibai.auto import REVIEW_PROMPT, parse_plan, plan_prompt, usage_of
from saibai.experience import seed_markdown
from saibai.guard import REPLAY, Guard, Reading, Run
from saibai.weather_history import SOURCE as WEATHER_SOURCE
from saibai.weather_history import hourly_csv_until, load_season, replay_summary

JST = sim.JST
ROOT = sim.ROOT
RESULTS = ROOT / "results"
RUN_DIR = ROOT / "run" / "replay"
VARIANTS = ("actual", "threshold", "single", "team")
VARIANT_JA = {"actual": "実運転", "threshold": "閾値だけ", "single": "1 体", "team": "チーム"}
SEASON_JA = {"summer": "夏", "autumn": "秋"}
WAKE_EVERY = timedelta(hours=3)
THRESHOLD_EVERY = timedelta(minutes=10)
THRESHOLD_RH = 70.0  # 知識の §1「アラビカの生育条件 RH 70〜80%」の下の端（設計書 §7）
THRESHOLD_MIN = 30  # 加湿器は 30 分で +12〜16 pt（11/3）
HISTORY_BEFORE = timedelta(days=2)  # 窓の前に渡す記録（その日までの記録の一部）
BUDGET_REVIEW_CENTS = "15"
BUDGET_PLAN_CENTS = "35"
BUDGET_SINGLE_CENTS = "50"  # 1 回の上限 $0.50（振り返り＋計画）
STOP_CENTS = 1000  # 窓 1 つ×1 通りで $10
WATER_WORDS = re.compile(r"(水やり|灌水|水を|給水)")
HOLD_WORDS = re.compile(r"(控え|減ら|待つ|待って|見送|延ば|あけ|空け|やらない|しない)")
SOIL_WORDS = re.compile(r"(土|soil)")
METER_STORE_DESC = (
    "栽培室の温湿度の記録（再生：模擬の栽培室の node_a と土の水分 soil_a）。"
    "/meter/YYYY-MM-DD/HH.csv が 1 時間 1 ファイル、列は read_at（JST）,temp_c,rh_pct,vpd_kpa,soil_raw。"
    "soil_raw は幼木 1 鉢の土の水分の生の数（単位なし・大きいほど湿っている）。"
    "/weather/YYYY-MM-DD.csv は近くのアメダスの毎正時の値、/weather/forecast.md は予報の要約。"
)
ACTIONS_STORE_DESC = (
    "加湿器の操作の記録（再生）と人の作業の記録。/actions/YYYY-MM-DD.jsonl、1 行 1 件。"
    "鍵＝at（JST）・who（オート／閾値／守りの層／人（作業の記録））・what（入／切／計画／水やり／その他）・"
    "minutes・accepted・reasons・rh_pct・vpd_kpa。人の作業の記録は note に中身、end は終わった時刻（まだなら無い）。"
)
EXPERIENCE_STORE_DESC = (
    "運転の経験：加湿器の操作ごとの効き目（入れた→何分で VPD がいくつ動いたか）と傾向（もとにした回の数つき）。"
    "/experience.md 1 つ。お手本の操作ではなく、効き目の事実だけ。"
)


def ts_iso(t) -> str:
    return pd.Timestamp(t).isoformat()


# ---- 渡す記録（先読みの禁止）----
@dataclass
class World:
    """エージェントに渡す物を、再生の時計 now より前の物だけから作る。"""

    S: sim.Season
    first: datetime  # 窓の始まり
    weather: list[dict]
    meter_rows: list[tuple] = field(
        default_factory=list
    )  # (t, temp, rh, vpd, soil)＝時計より前の読み
    actions: list[dict] = field(default_factory=list)  # その再生の操作の記録

    @classmethod
    def build(cls, S: sim.Season, i_first: int, weather: list[dict]) -> "World":
        w = cls(S=S, first=S.ts[i_first].to_pydatetime(), weather=weather)
        start = w.first - HISTORY_BEFORE
        for i in range(int(S.ts.searchsorted(pd.Timestamp(start))), i_first):
            t = S.ts[i].to_pydatetime()
            vpd = round(float(sim.vpd_from(S.temp[i], S.rh[i])), 3)
            w.meter_rows.append((t, float(S.temp[i]), float(S.rh[i]), vpd, w.soil_at(t)))
        return w

    def soil_at(self, t: datetime) -> float | None:
        s = self.S.soil
        i = s.index.searchsorted(pd.Timestamp(t), side="right") - 1
        if i < 0 or pd.Timestamp(t) - s.index[i] > pd.Timedelta(minutes=3):
            return None
        return float(s.iloc[i])

    def add_reading(self, t, temp, rh, vpd) -> None:
        self.meter_rows.append((t, temp, rh, vpd, self.soil_at(t)))

    # 温湿度：時計より前に閉じた時間のファイル
    def meter_files(self, now: datetime) -> dict[str, str]:
        hours: dict[pd.Timestamp, list[str]] = {}
        cut = pd.Timestamp(now).floor("h")
        for t, temp, rh, vpd, soil in self.meter_rows:
            if t >= now:
                continue
            h = pd.Timestamp(t).floor("h")
            if h >= cut:
                continue  # まだ閉じていない時間は渡さない
            soil_s = "" if soil is None else f"{soil:g}"
            hours.setdefault(h, []).append(
                f"{t.isoformat()},{temp:.2f},{rh:.2f},{vpd:.3f},{soil_s}"
            )
        return {
            f"/meter/{h:%Y-%m-%d}/{h:%H}.csv": "read_at,temp_c,rh_pct,vpd_kpa,soil_raw\n"
            + "\n".join(rows)
            + "\n"
            for h, rows in hours.items()
        }

    # 人の作業の記録：その時刻までに書けた物だけ（実際の加湿器の入切は渡さない）
    def human_records(self, now: datetime) -> list[dict]:
        out = []
        start = pd.Timestamp(self.first - HISTORY_BEFORE)
        for r in self.S.acts.itertuples():
            if r.target == "hum_a" or r.start_jst < start or r.start_jst > pd.Timestamp(now):
                continue
            end = None if pd.isna(r.end_jst) else r.end_jst
            later = r.certainty in ("estimated", "inferred_from_data")
            if (later or (end is not None and r.kind != "other")) and (
                end if end is not None else r.start_jst
            ) > pd.Timestamp(now):
                continue  # 後からまとめて書いた物・終わりの時刻が要る物は、終わりを過ぎてから
            rec = {
                "at": r.start_jst.isoformat(),
                "kind": "human",
                "who": "人（作業の記録）",
                "what": {"watering": "水やり"}.get(r.kind, "その他"),
                "target": r.target,
                "note": r.note if isinstance(r.note, str) else "",
            }
            for k in ("method",):
                v = getattr(r, k)
                if isinstance(v, str) and v:
                    rec[k] = v
            if not pd.isna(r.amount):
                rec["amount"] = r.amount
            if end is not None and end <= pd.Timestamp(now):
                rec["end"] = end.isoformat()
            elif end is not None:
                rec["note"] += "（終わりはまだ記録されていない）"
            out.append(rec)
        return out

    def action_files(self, now: datetime) -> dict[str, str]:
        recs = [a for a in self.actions if a["at"] <= now.isoformat()] + self.human_records(now)
        days: dict[str, list[str]] = {}
        for a in sorted(recs, key=lambda a: a["at"]):
            days.setdefault(a["at"][:10], []).append(json.dumps(a, ensure_ascii=False, default=str))
        return {f"/actions/{d}.jsonl": "\n".join(v) + "\n" for d, v in days.items()}

    def weather_summary(self, now: datetime) -> dict:
        return replay_summary(self.weather, now)

    def weather_files(self, now: datetime) -> dict[str, str]:
        out = {}
        d = (self.first - HISTORY_BEFORE).date()
        while d <= now.date():
            out[f"/weather/{d:%Y-%m-%d}.csv"] = hourly_csv_until(self.weather, d, now)
            d += timedelta(days=1)
        s = self.weather_summary(now)
        out["/weather/forecast.md"] = (
            "# 予報（再生：その先の実際の値から作った要約＝予報が当たった場合）\n\n"
            + (s["text"] or "予報は無し")
            + f"\n\n{WEATHER_SOURCE}\n"
        )
        return out

    def files(self, now: datetime) -> dict[str, dict[str, str]]:
        """置き場ごとのファイル（温湿度の置き場には天気も入る＝受け手と同じ）。"""
        return {
            "meter": self.meter_files(now) | self.weather_files(now),
            "actions": self.action_files(now),
        }

    def last_times(self, now: datetime) -> dict[str, str | None]:
        """渡す物の最後の時刻（テスト用）。予報の要約は除く。"""
        meter = [
            t
            for t, *_ in self.meter_rows
            if t < now and pd.Timestamp(t).floor("h") < pd.Timestamp(now).floor("h")
        ]
        acts = self.human_records(now) + [a for a in self.actions if a["at"] <= now.isoformat()]
        times = [a["at"] for a in acts] + [a["end"] for a in acts if "end" in a]
        w = [r["at"] for r in self.weather if r["at"] <= now]
        return {
            "meter": max(meter).isoformat() if meter else None,
            "actions": max(times) if times else None,
            "weather": max(w).isoformat() if w else None,
        }

    def experience_seed(self) -> str:
        return seed_markdown(before=self.first)


# ---- 1 回の再生 ----
class Replay:
    def __init__(self, season: str, variant: str, policy=None, fit: dict | None = None):
        assert variant in VARIANTS
        self.season, self.variant = season, variant
        self.S = sim.load(season)
        p = (fit or sim.load_fit())[season]
        self.i0, self.i1 = sim.window_index(self.S, *sim.WINDOWS[season])
        self.room = sim.Room(self.S, k=p["kh_per_h"], g=p["g"], start=self.i0, end=self.i1)
        self.fit = {k: p[k] for k in ("k_per_h", "kh_per_h", "s", "g")}
        self.guard = Guard(REPLAY)
        # 守りの層は窓の前の記録も見ている（止まり・跳びの線のため）
        for i in range(max(0, self.i0 - 400), self.i0):
            self.guard.observe(
                Reading(self.S.ts[i].to_pydatetime(), float(self.S.rh[i]), float(self.S.temp[i]))
            )
        self.world = World.build(self.S, self.i0, load_season(season))
        self.policy = policy
        self.series: list[tuple] = []  # (t, temp, rh, vpd, on)
        self.plans: list[dict] = []  # 守りの層に出した計画（誰が・入力・受け付けたか）
        self.wakes: list[dict] = []
        self.guard_offs: list[dict] = []  # 守りの層が切った回
        self.stopped: str | None = None

    # 操作の記録（エージェントにも渡す）
    def _action(self, t: datetime, who: str, what: str, **rec) -> None:
        _, temp, rh, vpd = self.room.reading()
        self.world.actions.append(
            {
                "at": t.isoformat(),
                "kind": "action",
                "who": who,
                "what": what,
                **rec,
                "temp_c": round(temp, 2),
                "rh_pct": rh,
                "vpd_kpa": vpd,
            }
        )

    def submit(self, inp: dict, t: datetime, source: str) -> tuple[bool, str]:
        """submit_plan の入力を守りの層に出す（to_person は読まない＝機械は動かない）。"""
        runs, evidence, err = parse_plan(inp)
        if err:
            self.plans.append(
                {"at": t.isoformat(), "who": source, "input": inp, "ok": False, "text": err}
            )
            return False, err
        runs = [Run(r.start, r.minutes, source) for r in runs]
        v = self.guard.submit_plan(runs, evidence, t, source)
        text = v.text() if runs else ("受け付けた：今回は入れない。" if v.ok else v.text())
        self.plans.append(
            {"at": t.isoformat(), "who": source, "input": inp, "ok": v.ok, "text": text}
        )
        self._action(
            t,
            source,
            "計画",
            accepted=v.ok,
            reasons=v.reasons,
            runs=[{"start": r.start.isoformat(), "minutes": r.minutes} for r in runs],
            minutes=sum(r.minutes for r in runs),
            reason=inp.get("reason"),
            to_person=inp.get("to_person"),
        )
        return v.ok, text

    def status(self, t: datetime) -> dict:
        _, temp, rh, vpd = self.room.reading()
        return {
            "meter": {
                "read_at": t.isoformat(timespec="seconds"),
                "temp_c": round(temp, 2),
                "rh_pct": rh,
                "vpd_kpa": vpd,
            },
            "guard": self.guard.snapshot(t),
        }

    def _threshold(self, t: datetime) -> None:
        _, _, rh, _ = self.room.reading()
        if self.guard.on or self.guard._active_run(t) is not None or rh >= THRESHOLD_RH:
            return
        self.submit(
            {
                "runs": [{"start": t.isoformat(), "minutes": THRESHOLD_MIN}],
                "reason": f"湿度 {rh}% が {THRESHOLD_RH:.0f}% を下回った",
                "evidence": [
                    {"device": "node_a", "metric": "rh_pct", "value": rh, "time": t.isoformat()}
                ],
            },
            t,
            "閾値",
        )

    def run(self) -> dict:
        S, room = self.S, self.room
        first = S.ts[self.i0].to_pydatetime().replace(hour=0, minute=0, second=0)
        next_wake, next_check = first, first
        while True:
            t, temp, rh, vpd = room.reading()
            if self.variant == "actual":
                on = bool(S.hum[room.i])
            else:
                self.guard.observe(Reading(t, rh, temp))
                if self.variant == "threshold" and t >= next_check:
                    self._threshold(t)
                    while next_check <= t:
                        next_check += THRESHOLD_EVERY
                if self.policy is not None and t >= next_wake and not self.stopped:
                    self.wakes.append(self.policy.wake(self, t))
                    while next_wake <= t:
                        next_wake += WAKE_EVERY
                d = self.guard.decide(t)
                on = d.on
                if d.changed:
                    who = d.source or "守りの層"
                    if on:
                        self._action(t, who, "入", minutes=d.minutes, accepted=True)
                    else:
                        self._action(t, who, "切", accepted=True, reasons=d.reasons)
                        if who == "守りの層":
                            self.guard_offs.append({"at": t.isoformat(), "reasons": d.reasons})
            self.series.append((t, temp, rh, vpd, on))
            self.world.add_reading(t, temp, rh, vpd)
            if not room.step(on):
                break
        return self.result()

    # ---- 物差し ----
    def result(self) -> dict:
        tgt = config.load()["target"]
        lo, hi = tgt["vpd_low_kpa"], tgt["vpd_high_kpa"]
        ts = [s[0] for s in self.series]
        vpd = np.array([s[3] for s in self.series])
        on = np.array([s[4] for s in self.series])
        dt = np.array(
            [min(5.0, (b - a).total_seconds() / 60) for a, b in zip(ts, ts[1:], strict=False)]
            + [1.0]
        )
        h = lambda m: round(float(dt[m].sum() / 60), 1)  # noqa: E731
        starts = int(np.sum(on[1:] & ~on[:-1]) + (1 if on[0] else 0))
        findings = selfcheck_windows(self.season, ts[0], ts[-1])
        lured, lured_on = lured_counts(self.plans, findings, ts, on, dt)
        costs = [w.get("cost_cents") or 0 for w in self.wakes]
        tp = [
            {"at": p["at"], "text": p["input"].get("to_person")}
            for p in self.plans
            if isinstance(p.get("input"), dict) and p["input"].get("to_person")
        ]
        return {
            "season": self.season,
            "variant": self.variant,
            "window": sim.WINDOWS[self.season],
            "sim": self.fit,
            "guard": REPLAY.label(),
            "metrics": {
                "hours": h(np.ones(len(vpd), bool)),
                "vpd_in_band_h": h((vpd >= lo) & (vpd <= hi)),
                "vpd_above_h": h(vpd > hi),
                "vpd_below_h": h(vpd < lo),
                "humidifier_min": round(float(dt[on].sum()), 0),
                "switch_on_count": starts,
                "plans": len(self.plans),
                "plans_refused": sum(1 for p in self.plans if not p["ok"]),
                "guard_offs": len(self.guard_offs),
                "fault_windows": len(findings),
                "lured_plans": lured,
                "on_min_in_fault": lured_on,
                "wakes": len(self.wakes),
                "cost_cents": round(float(sum(costs)), 1),
                "cost_per_wake_cents": round(float(np.mean(costs)), 2) if costs else 0.0,
            },
            "to_person": tp,
            "water_hold_first": water_hold_first(tp),
            "plans": self.plans,
            "wakes": self.wakes,
            "guard_off_list": self.guard_offs,
            "stopped": self.stopped,
        }

    def series_csv(self, every_min: int = 10) -> str:
        rows = ["at,temp_c,rh_pct,vpd_kpa,humidifier"]
        last = None
        for t, temp, rh, vpd, on in self.series:
            if last is None or (t - last) >= timedelta(minutes=every_min):
                rows.append(
                    f"{t.isoformat(timespec='minutes')},{temp:.2f},{rh:.2f},{vpd:.3f},{int(on)}"
                )
                last = t
        return "\n".join(rows) + "\n"


def selfcheck_windows(season: str, a: datetime, b: datetime) -> list[dict]:
    """自己点検の区間のうち、窓の中で、渡した機器（node_a・soil_a）が入る物。"""
    f = RESULTS / f"selfcheck_{season}.json"
    if not f.exists():
        return []
    out = []
    for x in json.loads(f.read_text())["findings"]:
        s, e = datetime.fromisoformat(x["start"]), datetime.fromisoformat(x["end"])
        devs = x["device"].split("/")
        if e >= a and s <= b and ({"node_a", "soil_a"} & set(devs)):
            out.append(x | {"devices": devs})
    return out


def lured_counts(plans, findings, ts, on, dt) -> tuple[int, float]:
    """故障の区間の値を根拠に引いて入れた計画の数・故障の区間に入っていた運転の分。"""
    n = 0
    for p in plans:
        inp = p.get("input") or {}
        if not p["ok"] or not inp.get("runs"):
            continue
        hit = False
        for e in inp.get("evidence") or []:
            try:
                et = datetime.fromisoformat(str(e.get("time")))
            except ValueError:
                continue
            for f in findings:
                if str(e.get("device")) in f["devices"] and (
                    datetime.fromisoformat(f["start"]) <= et <= datetime.fromisoformat(f["end"])
                ):
                    hit = True
        n += hit
    minutes = 0.0
    for f in findings:
        s, e = datetime.fromisoformat(f["start"]), datetime.fromisoformat(f["end"])
        m = np.array([s <= t <= e for t in ts])
        minutes += float(dt[m & on].sum())
    return n, round(minutes, 0)


def water_hold_first(tp: list[dict]) -> dict | None:
    """「水を控える」を土の水分の数値と時刻を引いて言えた最初の回（字で拾う・人が見て確かめる）。"""
    for x in tp:
        t = x["text"] or ""
        if (
            WATER_WORDS.search(t)
            and HOLD_WORDS.search(t)
            and SOIL_WORDS.search(t)
            and re.search(r"\d", t)
        ):
            return x
    return None


# ---- エージェントの通り（1 体・チーム）----
class AgentPolicy:
    """再生の起動 1 回＝置き場に時計より前の記録を写す→セッション（チームは振り返り→計画の 2 段）。
    答えは wakes.jsonl に残し、続きから流す時はそれを使う（API を呼ばない）。"""

    def __init__(
        self,
        variant: str,
        season: str,
        client=None,
        ids: dict | None = None,
        out_dir: Path | None = None,
    ):
        from saibai.agents import load_ids

        self.variant, self.season = variant, season
        self.client = client
        self.ids = ids if ids is not None else load_ids()
        self.dir = out_dir or RUN_DIR / f"{season}_{variant}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.cache = {}
        f = self.dir / "wakes.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                self.cache[r["wake"]] = r
        self.uploaded: dict[str, str] = {}
        self.total_cents = sum((r.get("cost_cents") or 0) for r in self.cache.values())
        self.n_paid = len(self.cache)
        self.max_new: int | None = None  # 試し：この数だけ新しく叩いたら止める
        self.new = 0

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def ensure_stores(self, world: World) -> None:
        """再生ごとに新しい置き場（同じ名＝作業場の道は受け手と同じ）。経験の最初の版は窓より前の記録だけ。"""
        from saibai.agents import put

        c = self.client
        for key, name, desc in (
            ("meter", "saibai-meter", METER_STORE_DESC),
            ("actions", "saibai-actions", ACTIONS_STORE_DESC),
            ("experience", "saibai-experience", EXPERIENCE_STORE_DESC),
        ):
            if key not in self.state:
                s = c.beta.memory_stores.create(name=name, description=desc)
                self.state[key] = s.id
                self._save()
        if not self.state.get("seeded"):
            put(c, self.state["experience"], "/experience.md", world.experience_seed())
            self.state["seeded"] = True
            self._save()

    def upload(self, world: World, now: datetime) -> int:
        from saibai.agents import put

        n = 0
        for key, files in world.files(now).items():
            for path, content in files.items():
                k = f"{key}:{path}"
                if self.uploaded.get(k) == content:
                    continue
                put(self.client, self.state[key], path, content)
                self.uploaded[k] = content
                n += 1
        return n

    def _resources(self, exp_access: str, knowledge: bool) -> list[dict]:
        from saibai.agents import store_resource

        r = [
            store_resource(
                self.state["experience"],
                exp_access,
                "経験。振り返りの結果をここに書き直す。"
                if exp_access == "read_write"
                else "経験。読むだけ。",
            ),
            store_resource(
                self.state["meter"], "read_only", "温湿度（と土の水分・外の天気）の記録。読むだけ。"
            ),
            store_resource(
                self.state["actions"], "read_only", "操作の記録と人の作業の記録。読むだけ。"
            ),
        ]
        if knowledge:
            r.append(store_resource(self.ids["knowledge"], "read_only", "栽培の知識。読むだけ。"))
        return r

    def _session(self, agent: str, title: str, prompt: str, resources, cents: str):
        c = self.client
        s = c.beta.sessions.create(
            agent=agent,
            environment_id=self.ids["env"],
            title=title,
            budget={"type": "limit", "max_list_cost": {"amount": cents, "currency": "USD"}},
            resources=resources,
        )
        c.beta.sessions.events.send(
            session_id=s.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
        )
        return s

    def _runner(self, now):
        from saibai.auto import AutoRunner

        return AutoRunner(self.client, self.ids, None, self.dir, lambda: now, poll_s=5.0)

    def wake(self, rp: Replay, now: datetime) -> dict:
        key = now.isoformat()
        if key in self.cache:  # 続きから：同じ答えを同じ順に出し直す（API を呼ばない）
            rec = self.cache[key]
            for inp in rec["tool_inputs"]:
                rp.submit(inp, now, "オート")
            return rec
        if self.n_paid >= 4 and self.total_cents / self.n_paid * 56 > STOP_CENTS:
            rp.stopped = f"{now:%m/%d %H:%M} で止めた：1 回の平均 {self.total_cents / self.n_paid:.1f}¢×56 回が $10 を越える見込み"
            return {"wake": key, "skipped": rp.stopped}
        if self.max_new is not None and self.new >= self.max_new:
            rp.stopped = f"試し：新しい起動 {self.max_new} 回で止めた（{now:%m/%d %H:%M} の前）"
            return {"wake": key, "skipped": rp.stopped}
        self.new += 1
        self.ensure_stores(rp.world)
        n_up = self.upload(rp.world, now)
        runner = self._runner(now)
        weather = rp.world.weather_summary(now)
        target = config.load()["target"]
        soil = rp.world.soil_at(now)
        head = (
            f"（再生：記録の再生です。再生の時計は {now.isoformat(timespec='minutes')}（JST）。"
            "置き場の記録はこの時刻の前まで＝先の記録はありません。）\n"
        )
        soil_line = (
            f"土の水分（soil_a・幼木 1 鉢・単位なし・大きいほど湿っている）：今 {soil:g}。"
            "温湿度の記録の soil_raw 列に過去の値、操作の記録に人の水やりがある。\n"
            if soil is not None
            else ""
        )
        prompt_plan = head + soil_line + plan_prompt(now, rp.status(now), target, weather)
        rec: dict = {"wake": key, "uploaded": n_up, "tool_inputs": [], "texts": []}
        usages = []

        def on_tool(ev) -> str:
            if ev.name != "submit_plan":
                return "この道具は無い"
            rec["tool_inputs"].append(ev.input)
            ok, text = rp.submit(ev.input, now, "オート")
            rec["texts"].append(text)
            return text

        if self.variant == "team":
            s1 = self._session(
                self.ids["analyst"],
                f"再生 {self.season} チーム 振り返り {now:%m/%d %H:%M}",
                head
                + REVIEW_PROMPT
                + "\n（温湿度の記録には soil_raw＝土の水分の列もある。人の作業の記録は who=人（作業の記録）。）",
                self._resources("read_write", knowledge=False),
                BUDGET_REVIEW_CENTS,
            )
            rec["review_stop"] = runner.wait_session(s1.id, deadline_s=900)
            usages.append(usage_of(self.client.beta.sessions.retrieve(session_id=s1.id)))
            rec["review_text"] = runner.last_text(s1.id)[-600:]
            s2 = self._session(
                self.ids["control"],
                f"再生 {self.season} チーム 計画 {now:%m/%d %H:%M}",
                prompt_plan,
                self._resources("read_only", knowledge=True),
                BUDGET_PLAN_CENTS,
            )
            rec["plan_stop"] = runner.wait_session(s2.id, on_tool, deadline_s=900)
            usages.append(usage_of(self.client.beta.sessions.retrieve(session_id=s2.id)))
            rec["plan_text"] = runner.last_text(s2.id)[-600:]
            rec["sessions"] = [s1.id, s2.id]
        else:
            prompt = (
                head
                + "まず振り返り、それから計画です（あなた 1 体で両方）。\n【振り返り】\n"
                + REVIEW_PROMPT
                + "\n【計画】\n"
                + soil_line
                + plan_prompt(now, rp.status(now), target, weather)
            )
            s = self._session(
                self.ids["single"],
                f"再生 {self.season} 1 体 {now:%m/%d %H:%M}",
                prompt,
                self._resources("read_write", knowledge=True),
                BUDGET_SINGLE_CENTS,
            )
            rec["plan_stop"] = runner.wait_session(s.id, on_tool, deadline_s=1200)
            usages.append(usage_of(self.client.beta.sessions.retrieve(session_id=s.id)))
            rec["plan_text"] = runner.last_text(s.id)[-600:]
            rec["sessions"] = [s.id]
        rec["usage"] = usages
        rec["cost_cents"] = round(sum(float(u.get("list_cost_cents") or 0) for u in usages), 2)
        self.total_cents += rec["cost_cents"]
        self.n_paid += 1
        with (self.dir / "wakes.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.cache[key] = rec
        print(
            f"{self.season} {self.variant} {now:%m/%d %H:%M}：計画 {len(rec['tool_inputs'])} 回・"
            f"{rec['cost_cents']}¢（計 {self.total_cents:.0f}¢）",
            flush=True,
        )
        return rec


# ---- 書き出し ----
def write_result(rp: Replay, res: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    p = RESULTS / f"replay_{rp.season}_{rp.variant}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    (RESULTS / f"replay_{rp.season}_{rp.variant}_series.csv").write_text(rp.series_csv())
    return p


def table_markdown() -> str:
    rows = []
    for season in ("summer", "autumn"):
        for v in VARIANTS:
            f = RESULTS / f"replay_{season}_{v}.json"
            if not f.exists():
                rows.append(
                    f"| {SEASON_JA[season]} | {VARIANT_JA[v]} | （まだ流していない） | | | | | | | |"
                )
                continue
            r = json.loads(f.read_text())
            m = r["metrics"]
            cost = (
                "0"
                if v in ("actual", "threshold")
                else f"{m['cost_per_wake_cents']}¢（計 {m['cost_cents']}¢・{m['wakes']} 回）"
            )
            stop = "・途中で止めた" if r.get("stopped") else ""
            rows.append(
                f"| {SEASON_JA[season]} | {VARIANT_JA[v]}{stop} | {m['vpd_in_band_h']} 時間／{m['hours']} | "
                f"{m['vpd_above_h']}／{m['vpd_below_h']} | {m['humidifier_min']:.0f} 分 | {m['switch_on_count']} | "
                f"{m['plans_refused']}／{m['plans']} | {m['guard_offs']} | {m['lured_plans']}（区間 {m['fault_windows']}・その間の運転 {m['on_min_in_fault']:.0f} 分） | {cost} |"
            )
    head = (
        "| 季節 | 通り | VPD が帯（0.4〜0.8）に入っていた時間 | 帯の上／下（時間） | 加湿器の運転 | 入れた回 | "
        "守りの層に断られた計画／出した計画 | 守りの層が切った回 | 故障に釣られた計画 | 1 回あたりの費用 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    return head + "\n".join(rows) + "\n"


def main() -> None:
    """uv run saibai-replay <summer|autumn> <actual|threshold|single|team>／uv run saibai-replay table"""
    args = sys.argv[1:]
    if args and args[0] == "table":
        (RESULTS / "replay_table.md").write_text(table_markdown(), encoding="utf-8")
        print(table_markdown())
        return
    season, variant = args[0], args[1]
    policy = None
    if variant in ("single", "team"):
        import anthropic
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        policy = AgentPolicy(variant, season, client=anthropic.Anthropic())
        if len(args) > 2:  # 試し：uv run saibai-replay autumn team 1
            policy.max_new = int(args[2])
    t0 = time.monotonic()
    rp = Replay(season, variant, policy)
    res = rp.run()
    p = write_result(rp, res)
    (RESULTS / "replay_table.md").write_text(table_markdown(), encoding="utf-8")
    m = res["metrics"]
    print(
        f"{season} {variant}：{json.dumps(m, ensure_ascii=False)}（{time.monotonic() - t0:.0f} 秒）→ {p.name}"
    )
    if res["stopped"]:
        print(res["stopped"])
