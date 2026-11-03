"""センサーの自己点検：コードで決定的に、疑わしい区間を拾う（AI は使わない）。

規則は一般の物だけ（特定の日付に合わせない）。
- 止まった値（stuck）：同じ値が長く続く
- 跳び（spike）：隣り合う読みで大きく動き、すぐ戻る
- 段差（step）：隣り合う読みで大きく動き、そのまま戻らない
- 欠け（gap）：読みの間が 5 分を超える
- 食い違い（disagree）：同じ部屋の 2 台の差が、いつもの差から大きく外れる

出力＝疑わしい区間の一覧（機器・種類・値の名・始まり・終わり・数値の根拠）。
使い方：uv run python -m saibai.selfcheck summer  → results/selfcheck_summer.json
"""

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from saibai.data import by_device, load_readings, season_files

# 値ごとの線。読みは約 61 秒ごと（data/README_data.md）
STUCK = {
    # 小数 2 桁の温湿度が 15 分まったく同じなのは、部屋が動いていても起きにくい（守りの層の線と同じ）
    "temp_c": pd.Timedelta(minutes=15),
    "rh_pct": pd.Timedelta(minutes=15),
    # CO2 は整数で、人のいない夜は数十分同じこともある＝長めに 60 分
    "co2_ppm": pd.Timedelta(minutes=60),
    # 土の水分の生の数はふだん ±数十で揺れる（夏の頭 30 分で 747〜836）＝30 分同じは止まりの疑い
    "soil_raw": pd.Timedelta(minutes=30),
}
JUMP = {
    # 1 回の読み（約 1 分）で 3℃ は空調の吹き出しでも出にくい
    "temp_c": 3.0,
    # 1 分で 10 pt は加湿器でも出ない（加湿器は 30 分で +12〜16 pt・11/3）
    "rh_pct": 10.0,
    # 締め切った部屋で 1 分に 300 ppm は人が息を吹きかけでもしない限り出ない
    "co2_ppm": 300.0,
    # 土の水分はふだん 1 回で ±50 前後揺れる＝その 4 倍ほど
    "soil_raw": 200.0,
}
NEIGHBOR = pd.Timedelta(minutes=2)  # 「隣り合う読み」とみなす間（1 回抜けても拾う）
SETTLE = pd.Timedelta(minutes=30)  # 跳びか段差かを、前後 30 分の中央値で分ける
GAP = pd.Timedelta(minutes=5)  # README の「穴」と同じ線
# 食い違い：5 分の中央値の差が、その差の 24 時間の中央値から外れた量
DISAGREE = {"temp_c": 2.0, "rh_pct": 10.0}
DISAGREE_FOR = pd.Timedelta(minutes=30)  # これより長く外れ続けたら拾う
PAIRS = [("node_a", "node_b"), ("node_a", "node_c")]


@dataclass
class Finding:
    device: str
    kind: str
    metric: str | None
    start: str
    end: str
    evidence: dict


def _iso(t: pd.Timestamp) -> str:
    return t.isoformat()


def find_stuck(dev: str, s: pd.Series, metric: str) -> list[Finding]:
    s = s.dropna()
    if s.empty:
        return []
    run_id = (s != s.shift()).cumsum()
    out = []
    for _, g in s.groupby(run_id):
        span = g.index[-1] - g.index[0]
        if span >= STUCK[metric]:
            out.append(
                Finding(
                    dev,
                    "stuck",
                    metric,
                    _iso(g.index[0]),
                    _iso(g.index[-1]),
                    {
                        "value": float(g.iloc[0]),
                        "minutes": round(span.total_seconds() / 60, 1),
                        "readings": int(len(g)),
                        "limit_min": STUCK[metric].total_seconds() / 60,
                    },
                )
            )
    return out


def find_jumps(dev: str, s: pd.Series, metric: str) -> list[Finding]:
    s = s.dropna()
    if len(s) < 2:
        return []
    dt = s.index.to_series().diff()
    dv = s.diff()
    hits = s.index[(dv.abs() >= JUMP[metric]) & (dt <= NEIGHBOR)]
    out = []
    for t in hits:
        i = s.index.get_loc(t)
        t_prev, v_prev, v = s.index[i - 1], float(s.iloc[i - 1]), float(s.iloc[i])
        before = s[(s.index >= t_prev - SETTLE) & (s.index <= t_prev)].median()
        after_win = s[(s.index > t) & (s.index <= t + SETTLE)]
        after = after_win.median() if len(after_win) else v
        shifted = abs(after - before) >= JUMP[metric] / 2
        if shifted:
            kind, end = "step", t
        else:
            # 跳び：元の水準に戻るまでを区間にする
            back = after_win[(after_win - before).abs() < JUMP[metric] / 2]
            kind, end = "spike", (back.index[0] if len(back) else t)
        out.append(
            Finding(
                dev,
                kind,
                metric,
                _iso(t_prev),
                _iso(end),
                {
                    "from": v_prev,
                    "to": v,
                    "delta": round(v - v_prev, 3),
                    "limit": JUMP[metric],
                    "median_before_30m": round(float(before), 3),
                    "median_after_30m": round(float(after), 3),
                },
            )
        )
    return out


def find_gaps(dev: str, idx: pd.DatetimeIndex) -> list[Finding]:
    if len(idx) < 2:
        return []
    d = idx.to_series().diff()
    out = []
    for t in idx[d > GAP]:
        i = idx.get_loc(t)
        out.append(
            Finding(
                dev,
                "gap",
                None,
                _iso(idx[i - 1]),
                _iso(t),
                {"minutes": round((t - idx[i - 1]).total_seconds() / 60, 1), "limit_min": 5},
            )
        )
    return out


def find_disagreement(devs: dict[str, pd.DataFrame]) -> list[Finding]:
    out = []
    for a, b in PAIRS:
        if a not in devs or b not in devs:
            continue
        for metric, thr in DISAGREE.items():
            if metric not in devs[a] or metric not in devs[b]:
                continue
            sa = devs[a][metric].resample("5min").median()
            sb = devs[b][metric].resample("5min").median()
            diff = (sa - sb).dropna()
            if diff.empty:
                continue
            usual = diff.rolling("24h", min_periods=12).median()
            off = (diff - usual).abs() > thr
            run_id = (off != off.shift()).cumsum()
            for _, g in diff[off].groupby(run_id[off]):
                span = g.index[-1] - g.index[0] + pd.Timedelta(minutes=5)
                if span >= DISAGREE_FOR:
                    worst = (g - usual[g.index]).abs().idxmax()
                    out.append(
                        Finding(
                            f"{a}/{b}",
                            "disagree",
                            metric,
                            _iso(g.index[0]),
                            _iso(g.index[-1] + pd.Timedelta(minutes=5)),
                            {
                                "diff_at_worst": round(float(g[worst]), 2),
                                "usual_diff": round(float(usual[worst]), 2),
                                "worst_at": _iso(worst),
                                "limit": thr,
                                "minutes": round(span.total_seconds() / 60),
                            },
                        )
                    )
    return out


MERGE_WITHIN = pd.Timedelta(minutes=30)  # 30 分以内に続く同じ種類の拾い物は 1 つの区間にまとめる


def merge(findings: list[Finding]) -> list[Finding]:
    """同じ機器・値・種類で、前の終わりから 30 分以内に始まる物を 1 区間にまとめる（回数と最大の跳びを残す）。"""
    out: list[Finding] = []
    last: dict[tuple, Finding] = {}
    for f in sorted(findings, key=lambda f: (f.device, f.metric or "", f.kind, f.start)):
        key = (f.device, f.metric, f.kind)
        prev = last.get(key)
        if prev and pd.Timestamp(f.start) <= pd.Timestamp(prev.end) + MERGE_WITHIN:
            prev.end = max(prev.end, f.end, key=pd.Timestamp)
            ev = prev.evidence
            ev["count"] = ev.get("count", 1) + 1
            if "delta" in f.evidence:
                ev["max_abs_delta"] = max(
                    ev.get("max_abs_delta", abs(ev["delta"])), abs(f.evidence["delta"])
                )
            continue
        f = Finding(f.device, f.kind, f.metric, f.start, f.end, dict(f.evidence))
        out.append(f)
        last[key] = f
    return out


def check(df: pd.DataFrame) -> list[Finding]:
    devs = by_device(df)
    out: list[Finding] = []
    for dev, g in devs.items():
        out += find_gaps(dev, g.index)
        for metric in STUCK:
            if metric in g:
                out += find_stuck(dev, g[metric], metric)
                out += find_jumps(dev, g[metric], metric)
    out += find_disagreement(devs)
    out = merge(out)
    return sorted(out, key=lambda f: (f.start, f.device, f.kind))


def summarize(findings: list[Finding]) -> dict[str, int]:
    c: dict[str, int] = {}
    for f in findings:
        k = f"{f.device}:{f.kind}:{f.metric or '-'}"
        c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items()))


def main() -> None:
    season = sys.argv[1] if len(sys.argv) > 1 else "summer"
    env, _ = season_files(season)
    findings = check(load_readings(env))
    out = Path(__file__).resolve().parents[2] / "results" / f"selfcheck_{season}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "source": env.name,
                "summary": summarize(findings),
                "findings": [asdict(f) for f in findings],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"{season}：{len(findings)} 区間 → {out}")
    for k, n in summarize(findings).items():
        print(f"  {k} {n}")


if __name__ == "__main__":
    main()
