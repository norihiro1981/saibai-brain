"""模擬の栽培室（設計書 v0.8 §6）：絶対湿度（AH・g/m³）だけを合わせる。温度は記録をそのまま流す。

合わせる式（k・s・g・c を決め、誤差の表を出すため）：
    dAH/dt = −k·(AH − AH_外(t)) + s + g·加湿器(t) − c·(1 − 冷房装置(t))
  - AH_外＝近くのアメダスの毎正時の気温と湿度から出した AH を時刻で直線に内挿。
  - s＝部屋の湿り気の素（冷房装置が動いている時の分を含む）。c＝冷房装置（水をかけて冷やす型）が足す分。
    冷房装置の記録は止めた時だけ＝ふだんは 1。合わせる日はずっと 1＝c は s に混ざる＝c は窓の中の止めた区間だけで決める。
再生の形（記録＋加湿器の差だけを式で足す）：
    AH_模擬 = AH_記録 + Δ・dΔ/dt = −k·Δ + g·(加湿器_模擬 − 加湿器_記録)
  冷房装置・天窓・送風機・水やり・外の天気は記録の中にそのまま入る＝4 通りに共通。
時間の単位は時（k は 1/時・s・g・c は g/m³/時）。
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from saibai.data import load_actions, load_readings, season_files
from saibai.weather_history import load_season

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
FIT_FILE = ROOT / "results" / "sim_fit.json"

# 合わせる日と評価の窓（設計書 §6・§7）。窓は合わせる日に入れない。
FIT_DAYS = {
    "summer": ("2026-08-22", "2026-09-02"),  # 200 L の給水タンクの後（入切が記録どおり）
    "autumn": ("2026-10-03", "2026-10-19"),
}
WINDOWS = {
    "summer": ("2026-09-03", "2026-09-09"),
    "autumn": ("2026-10-20", "2026-10-26"),
}
# 冷房装置を止めた区間（作業の記録の cool_a）。再開の記録が無い 9/8 15:00 は窓の終わりまで止めたと置く
COOL_OFF_OPEN_END = "2026-09-10T00:00:00+09:00"
# 模擬が合わない区間（項を足さない物）＝印だけ。終わりの無い物は区間の終わりまで
MISMATCH = [
    ("天窓を開けた", "2026-09-08T00:05:00+09:00", "2026-09-08T03:10:00+09:00"),
    ("天井の送風機を足した", "2026-09-05T16:00:00+09:00", "2026-09-12T00:00:00+09:00"),
]
SEGMENT_H = 6.0  # 合わせる時の軌跡は 6 時間ごとに記録から出直す
K_GRID = np.geomspace(0.01, 2.0, 40)  # 外への抜け（遅い）
KH_GRID = np.geomspace(0.3, 12.0, 30)  # 加湿器の霧が混ざって消える速さ（速い）


# ---- 湿り気の換算（VPD と同じ飽和水蒸気圧の式）----
def es_kpa(t):
    return 0.6108 * np.exp(17.27 * np.asarray(t, float) / (np.asarray(t, float) + 237.3))


def ah_from(temp_c, rh_pct):
    """絶対湿度 g/m³＝2167·e／(T＋273.15)・e＝RH／100·e°(T)（kPa）。"""
    t = np.asarray(temp_c, float)
    return 2167.0 * (np.asarray(rh_pct, float) / 100.0) * es_kpa(t) / (t + 273.15)


def rh_from(ah, temp_c):
    t = np.asarray(temp_c, float)
    return np.asarray(ah, float) * (t + 273.15) / (2167.0 * es_kpa(t)) * 100.0


def vpd_from(temp_c, rh_pct):
    return es_kpa(temp_c) * (1 - np.asarray(rh_pct, float) / 100.0)


# ---- 記録を 1 本の表にする ----
def intervals(acts: pd.DataFrame, kind: str, target: str, open_end=None) -> list[tuple]:
    out = []
    for r in acts[(acts.kind == kind) & (acts.target == target)].itertuples():
        end = r.end_jst if not pd.isna(r.end_jst) else open_end
        if end is not None:
            out.append((r.start_jst, pd.Timestamp(end)))
    return out


def indicator(ts: pd.DatetimeIndex, spans: list[tuple]) -> np.ndarray:
    x = np.zeros(len(ts))
    for a, b in spans:
        x[(ts >= a) & (ts < b)] = 1.0
    return x


@dataclass
class Season:
    name: str
    ts: pd.DatetimeIndex  # node_a の読みの時刻
    temp: np.ndarray
    rh: np.ndarray
    ah: np.ndarray
    ah_out: np.ndarray
    hum: np.ndarray  # 実際の加湿器（作業の記録）
    cool: np.ndarray  # 冷房装置（1＝動いていた見込み）
    soil: pd.Series  # soil_a（記録のまま・模擬しない）
    acts: pd.DataFrame
    hum_spans: list[tuple]

    def hours(self) -> np.ndarray:
        return ((self.ts - self.ts[0]).total_seconds() / 3600.0).to_numpy()


def outside_ah(ts: pd.DatetimeIndex, season: str) -> np.ndarray:
    rows = [r for r in load_season(season) if r["temp_c"] is not None and r["rh_pct"] is not None]
    if not rows:
        return np.full(len(ts), np.nan)
    t = np.array([r["at"].timestamp() for r in rows])
    a = ah_from([r["temp_c"] for r in rows], [r["rh_pct"] for r in rows])
    return np.interp(ts.asi8 / 1e9, t, a)


def load(season: str) -> Season:
    env, act = season_files(season)
    df = load_readings(env)
    a = df[df.device_id == "node_a"].dropna(subset=["temp_c", "rh_pct"]).set_index("timestamp")
    a = a[~a.index.duplicated()].sort_index()
    soil = df[df.device_id == "soil_a"].set_index("timestamp")["soil_raw"].dropna().sort_index()
    acts = load_actions(act)
    hum_spans = intervals(acts, "humidifier_on", "hum_a")
    cool_off = intervals(acts, "other", "cool_a", open_end=COOL_OFF_OPEN_END)
    ts = a.index
    cool = 1.0 - indicator(ts, cool_off) if season == "summer" else np.zeros(len(ts))
    return Season(
        name=season,
        ts=ts,
        temp=a.temp_c.to_numpy(float),
        rh=a.rh_pct.to_numpy(float),
        ah=ah_from(a.temp_c, a.rh_pct),
        ah_out=outside_ah(ts, season),
        hum=indicator(ts, hum_spans),
        cool=cool,
        soil=soil,
        acts=acts,
        hum_spans=hum_spans,
    )


# ---- 式を流す（区間ごとに一定の入力・正確な指数の更新）----
def response(hours: np.ndarray, u: np.ndarray, k: float, x0: float = 0.0) -> np.ndarray:
    """dx/dt = −k·x + u(t)（u は読みと読みの間は一定）の解。"""
    x = np.empty(len(hours))
    x[0] = x0
    dt = np.diff(hours)
    e = np.exp(-k * dt)
    f = (1 - e) / k
    for i in range(len(dt)):
        x[i + 1] = x[i] * e[i] + u[i] * f[i]
    return x


def segments(sel: np.ndarray, hours: np.ndarray, seg_h: float = SEGMENT_H) -> list[np.ndarray]:
    """選んだ読みを、6 時間ごと（と途切れ目）に分けた添字の並び。"""
    idx = np.flatnonzero(sel)
    if len(idx) == 0:
        return []
    out, cur = [], [idx[0]]
    for j in idx[1:]:
        if j != cur[-1] + 1 or hours[j] - hours[cur[0]] >= seg_h:
            out.append(np.array(cur))
            cur = [j]
        else:
            cur.append(j)
    out.append(np.array(cur))
    return [s for s in out if len(s) > 10]


def plume(S: Season, k_h: float, hum: np.ndarray | None = None) -> np.ndarray:
    """加湿器の霧の分（g＝1 の時）：dD/dt = −k_h·D + 加湿器(t)。区間の頭から通しで流す。"""
    return response(S.hours(), S.hum if hum is None else hum, k_h)


def _bases(S: Season, seg: np.ndarray, k: float, d1: np.ndarray):
    """区間を記録の AH から出直す時の、線形の部品（土台・素 s・加湿器 g・冷房装置 c）。
    d1＝plume(k_h)。出直しの時の霧の分 g·d1[頭] は土台から引く（土台は外へ k で戻る）。"""
    h = S.hours()[seg]
    decay = np.exp(-k * (h - h[0]))
    base = S.ah[seg[0]] * decay + response(h, k * S.ah_out[seg], k)
    one = response(h, np.ones(len(h)), k)
    hum = d1[seg] - d1[seg[0]] * decay
    cooloff = response(h, 1.0 - S.cool[seg], k)
    return base, one, hum, cooloff


def day_mask(S: Season, first: str, last: str) -> np.ndarray:
    d = S.ts.strftime("%Y-%m-%d")
    return (d >= first) & (d <= last)


def fit_params(S: Season, sel: np.ndarray) -> dict:
    """k（外への抜け）と k_h（霧の消え方）を格子で振り、s・g は線形の最小二乗。"""
    segs = segments(sel, S.hours())
    d1s = {kh: plume(S, kh) for kh in KH_GRID}
    best = None
    for k in K_GRID:
        parts = [_bases(S, seg, k, d1s[KH_GRID[0]]) for seg in segs]
        y = np.concatenate([S.ah[seg] - b[0] for seg, b in zip(segs, parts, strict=True)])
        one = np.concatenate([b[1] for b in parts])
        h = S.hours()
        for kh, d1 in d1s.items():
            hum = np.concatenate(
                [d1[seg] - d1[seg[0]] * np.exp(-k * (h[seg] - h[seg[0]])) for seg in segs]
            )
            X = np.column_stack([one, hum])
            coef, *_ = np.linalg.lstsq(X, y, rcond=None)
            sse = float(np.sum((y - X @ coef) ** 2))
            if best is None or sse < best[0]:
                best = (sse, float(k), float(kh), float(coef[0]), float(coef[1]))
    _, k, kh, s, g = best
    return {"k_per_h": k, "kh_per_h": kh, "s": s, "g": g, "segments": len(segs)}


def fit_c(S: Season, sel: np.ndarray, p: dict) -> float | None:
    """冷房装置を止めた区間にかかる軌跡だけで c を決める（k・k_h・s・g は決めた物）。"""
    ys, xs = [], []
    d1 = plume(S, p["kh_per_h"])
    for seg in segments(sel, S.hours()):
        if S.cool[seg].min() == 1.0:
            continue
        base, one, hum, cooloff = _bases(S, seg, p["k_per_h"], d1)
        ys.append(S.ah[seg] - base - p["s"] * one - p["g"] * hum)
        xs.append(-cooloff)
    if not ys:
        return None
    y, x = np.concatenate(ys), np.concatenate(xs)
    return float(np.dot(x, y) / np.dot(x, x))


def _track(S: Season, seg: np.ndarray, p: dict, d1: np.ndarray) -> np.ndarray:
    base, one, hum, cooloff = _bases(S, seg, p["k_per_h"], d1)
    return base + p["s"] * one + p["g"] * hum - (p.get("c") or 0.0) * cooloff


def model_track(S: Season, sel: np.ndarray, p: dict) -> np.ndarray:
    """合わせる式の軌跡（6 時間ごとに記録から出直す）。選んでいない所は NaN。"""
    m = np.full(len(S.ts), np.nan)
    d1 = plume(S, p["kh_per_h"])
    for seg in segments(sel, S.hours()):
        m[seg] = _track(S, seg, p, d1)
    return m


# ---- 誤差 ----
def _at(S: Season, t: pd.Timestamp) -> int | None:
    i = S.ts.searchsorted(t)
    for j in (i, i - 1):
        if 0 <= j < len(S.ts) and abs(S.ts[j] - t) <= pd.Timedelta(minutes=2):
            return j
    return None


def _forward(S: Season, i0: int, minutes: float, p: dict, d1) -> tuple[float, float] | None:
    """i0 の記録から出直して minutes 先まで式を流す。返り＝（記録の差・模擬の差）。"""
    j = _at(S, S.ts[i0] + pd.Timedelta(minutes=minutes))
    if j is None or j <= i0:
        return None
    m = _track(S, np.arange(i0, j + 1), p, d1)
    return float(S.ah[j] - S.ah[i0]), float(m[-1] - S.ah[i0])


def event_errors(S: Season, first: str, last: str, p: dict) -> list[dict]:
    """入れた 30 分の上がり幅・切って 30／60 分の戻り方（記録と模擬・g/m³）。"""
    out = []
    d1 = plume(S, p["kh_per_h"])
    for a, b in S.hum_spans:
        if not (first <= a.strftime("%Y-%m-%d") <= last):
            continue
        row = {"on": a.isoformat(), "off": b.isoformat()}
        i = _at(S, a)
        if i is not None and (b - a) >= pd.Timedelta(minutes=30):
            r = _forward(S, i, 30, p, d1)
            if r:
                row["rise30_rec"], row["rise30_sim"] = round(r[0], 2), round(r[1], 2)
        j = _at(S, b)
        if j is not None:
            for m in (30, 60):
                r = _forward(S, j, m, p, d1)
                if r:
                    row[f"fall{m}_rec"], row[f"fall{m}_sim"] = round(r[0], 2), round(r[1], 2)
        out.append(row)
    return out


def _stats(err: np.ndarray) -> dict:
    e = np.abs(err[~np.isnan(err)])
    if len(e) == 0:
        return {"n": 0}
    return {
        "n": int(len(e)),
        "median_abs": round(float(np.median(e)), 3),
        "p90_abs": round(float(np.percentile(e, 90)), 3),
    }


def mismatch_mask(S: Season) -> np.ndarray:
    m = np.zeros(len(S.ts), bool)
    for _, a, b in MISMATCH:
        m |= (S.ts >= pd.Timestamp(a)) & (S.ts < pd.Timestamp(b))
    return m


def fit_season(season: str) -> dict:
    S = load(season)
    fit_sel = day_mask(S, *FIT_DAYS[season])
    win_sel = day_mask(S, *WINDOWS[season])
    p = fit_params(S, fit_sel)
    p["c"] = fit_c(S, win_sel, p) if season == "summer" else None
    out = {"season": season, "fit_days": FIT_DAYS[season], "window": WINDOWS[season], **p}
    for name, sel, days in (
        ("err_fit", fit_sel, FIT_DAYS[season]),
        ("err_window", win_sel, WINDOWS[season]),
    ):
        m = model_track(S, sel, p)
        err = m - S.ah
        mm = mismatch_mask(S) & sel
        ev = event_errors(S, *days, p)
        out[name] = {
            "ah_error": _stats(err[sel]),
            "ah_error_mismatch": _stats(err[mm]),
            "ah_error_other": _stats(err[sel & ~mm]),
            "rh_error": _stats(rh_from(m, S.temp)[sel] - S.rh[sel]),
            "events": ev,
        }
    out["mismatch"] = (
        [{"what": w, "start": a, "end": b} for w, a, b in MISMATCH] if season == "summer" else []
    )
    out["ah_out_median"] = round(float(np.nanmedian(S.ah_out)), 2)
    out["ah_room_median"] = round(float(np.median(S.ah)), 2)
    return out


def fit_all(write: bool = True) -> dict:
    res = {s: fit_season(s) for s in ("summer", "autumn")}
    if write:
        FIT_FILE.parent.mkdir(exist_ok=True)
        FIT_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def load_fit() -> dict:
    return json.loads(FIT_FILE.read_text())


# ---- 再生の部屋（記録＋加湿器の差）----
class Room:
    """再生の部屋。読みの添字 i ごとに、模擬の RH を返し、次の読みまでの加湿器（模擬）を受ける。
    同じ入力なら同じ結果（乱れを使わない）。実運転（模擬の入切＝記録の入切）なら Δ＝0＝記録そのまま。"""

    def __init__(self, S: Season, k: float, g: float, start: int, end: int):
        self.S, self.k, self.g = S, k, g  # k＝霧の消え方 k_h
        self.i, self.end = start, end
        self.delta = 0.0

    def now(self) -> datetime:
        return self.S.ts[self.i].to_pydatetime()

    def reading(self) -> tuple[datetime, float, float, float]:
        """（時刻・気温・模擬の RH・模擬の VPD）"""
        S, i = self.S, self.i
        ah = max(0.1, S.ah[i] + self.delta)
        rh = float(min(100.0, rh_from(ah, S.temp[i])))
        return self.now(), float(S.temp[i]), round(rh, 2), round(float(vpd_from(S.temp[i], rh)), 3)

    def step(self, hum_on: bool) -> bool:
        """次の読みへ進む。進めなければ False（窓の終わり）。"""
        if self.i + 1 >= self.end:
            return False
        S, i = self.S, self.i
        dt = (S.ts[i + 1] - S.ts[i]).total_seconds() / 3600.0
        e = math.exp(-self.k * dt)
        u = self.g * ((1.0 if hum_on else 0.0) - S.hum[i])
        self.delta = self.delta * e + u * (1 - e) / self.k
        # 飽和を越えた分は霧になって落ちる＝Δ もそこで止める
        sat = float(ah_from(S.temp[i + 1], 100.0))
        self.delta = min(self.delta, sat - S.ah[i + 1])
        self.i += 1
        return True


def window_index(S: Season, first: str, last: str) -> tuple[int, int]:
    sel = np.flatnonzero(day_mask(S, first, last))
    return int(sel[0]), int(sel[-1]) + 1


def _md(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _med(ev: list[dict], key: str) -> tuple[str, str, str, int]:
    rec = [e[key + "_rec"] for e in ev if key + "_rec" in e]
    sm = [e[key + "_sim"] for e in ev if key + "_sim" in e]
    if not rec:
        return "—", "—", "—", 0
    err = np.median(np.abs(np.array(rec) - np.array(sm)))
    return f"{np.median(rec):+.2f}", f"{np.median(sm):+.2f}", f"{err:.2f}", len(rec)


def fit_markdown(res: dict) -> str:
    """README と動画に使う模擬の誤差の表。"""
    name = {"summer": "夏", "autumn": "秋"}
    lines = [
        "| 季節 | k（外への抜け） | k_h（霧の消え方） | g | s | c |",
        "|---|---|---|---|---|---|",
    ]
    for s, r in res.items():
        c = "—" if r["c"] is None else f"{r['c']:.2f}"
        lines.append(
            f"| {name[s]} | {r['k_per_h']:.3f}/時（{1 / r['k_per_h']:.0f} 時間） | {r['kh_per_h']:.2f}/時"
            f"（{60 / r['kh_per_h']:.0f} 分） | {r['g']:.2f} g/m³/時 | {r['s']:.2f} | {c} |"
        )
    lines += [
        "",
        "| 季節 | 日 | AH の差（中央／90%・g/m³） | RH の差（中央／90%・pt） | 入れて 30 分の上がり（記録／模擬／差の中央・回） | 切って 30 分 | 切って 60 分 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s, r in res.items():
        for part, label in (("err_fit", "合わせる日"), ("err_window", "評価の窓")):
            x = r[part]
            ah, rh = x["ah_error"], x["rh_error"]
            cells = []
            for key in ("rise30", "fall30", "fall60"):
                a, b, e, n = _med(x["events"], key)
                cells.append(f"{a}／{b}／{e}（{n}）")
            days = r["fit_days"] if part == "err_fit" else r["window"]
            lines.append(
                f"| {name[s]} | {label} {_md(days[0])}〜{_md(days[1])} | {ah['median_abs']}／{ah['p90_abs']} "
                f"| {rh['median_abs']}／{rh['p90_abs']} | " + " | ".join(cells) + " |"
            )
            if x["ah_error_mismatch"].get("n"):
                mm, ot = x["ah_error_mismatch"], x["ah_error_other"]
                lines.append(
                    f"| {name[s]} | うち印の区間（天窓・送風機） | {mm['median_abs']}／{mm['p90_abs']} "
                    f"（ほか {ot['median_abs']}／{ot['p90_abs']}） | | | | |"
                )
    return "\n".join(lines) + "\n"


def main() -> None:
    """uv run python -m saibai.sim：夏と秋で式の値を合わせ、誤差を results/sim_fit.json と .md に書く。"""
    res = fit_all()
    FIT_FILE.with_suffix(".md").write_text(fit_markdown(res), encoding="utf-8")
    print(fit_markdown(res))


if __name__ == "__main__":
    main()
