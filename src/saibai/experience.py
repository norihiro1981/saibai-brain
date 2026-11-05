"""最初の経験：夏と秋の作業の記録の「加湿器を入れた回」の効き目を、コードで計算する（設計書 §4）。

- その期間は水のやりすぎを疑っている＝操作をお手本にしない。取るのは「入れたら VPD がどう動いたか」だけ。
- 効き目の決め方は分析役（エージェント）と同じ：入れる前の VPD と、入れて 15・30・60 分の VPD の差。
  使う行は狙いの時刻に最も近い読み（前後 2 分以内・無ければ数えない＝穴）。機器は node_a（室の空気の基準）。
- 経験の置き場の最初の版（api_actor）になる。後の版は振り返りのセッションが書く。
"""

import pandas as pd

from saibai import config
from saibai.data import load_actions, load_readings, season_files

AFTER = (15, 30, 60)
NEAR = pd.Timedelta(minutes=2)
SEASON_JA = {"summer": "夏", "autumn": "秋"}


def _near(s: pd.Series, t: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    i = s.index.searchsorted(t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(s):
            d = abs(s.index[j] - t)
            if d <= NEAR and (best is None or d < best[0]):
                best = (d, s.index[j], float(s.iloc[j]))
    return None if best is None else (best[1], best[2])


def effects(season: str, before=None) -> pd.DataFrame:
    """before（時刻）を渡すと、入れて 60 分の読みまでその時刻より前に収まる回だけ（再生の先読みの禁止）。
    記録もその時刻より前の行しか見ない。"""
    env, act = season_files(season)
    df = load_readings(env)
    if before is not None:
        before = pd.Timestamp(before)
        df = df[df.timestamp < before]
    a = df[df.device_id == "node_a"].set_index("timestamp").sort_index()
    vpd, temp, rh = a["vpd_kpa"].dropna(), a["temp_c"], a["rh_pct"]
    acts = load_actions(act)
    rows = []
    cutoff = before
    for r in acts[acts.kind == "humidifier_on"].itertuples():
        if cutoff is not None and r.start_jst + pd.Timedelta(minutes=max(AFTER)) >= cutoff:
            continue
        before = _near(vpd, r.start_jst - pd.Timedelta(seconds=30))
        if before is None:
            continue
        minutes = (
            None
            if pd.isna(r.end_jst) or (cutoff is not None and r.end_jst >= cutoff)
            else int((r.end_jst - r.start_jst) / pd.Timedelta(minutes=1))
        )
        row = {
            "season": SEASON_JA[season],
            "start": r.start_jst,
            "minutes": minutes,
            "temp_c": float(temp.asof(r.start_jst)),
            "rh_pct": float(rh.asof(r.start_jst)),
            "vpd_before": before[1],
        }
        for m in AFTER:
            t = r.start_jst + pd.Timedelta(minutes=m)
            got = _near(vpd, t) if minutes is None or m <= minutes else None
            row[f"d{m}"] = None if got is None else round(got[1] - before[1], 3)
        rows.append(row)
    return pd.DataFrame(rows)


def _med(g: pd.DataFrame, c: str) -> str:
    v = g[c].dropna()
    return "—" if v.empty else f"{v.median():+.3f}（{len(v)} 回）"


def _d(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:+.3f}"


def seed_markdown(before=None) -> str:
    """before を渡すと、その時刻より前の記録だけから作る（再生の経験の最初の版）。"""
    t = config.load()["target"]
    lo, hi = t["vpd_low_kpa"], t["vpd_high_kpa"]
    parts = []
    for s in ("summer", "autumn"):
        if before is not None and season_files(s)[0].stem.split("_")[1] >= str(
            pd.Timestamp(before).date()
        ):
            continue  # 区間が窓より後
        parts.append(effects(s, before))
    e = pd.concat(parts, ignore_index=True)
    lines = [
        "# 経験（加湿器の効き目）",
        "",
        f"- 狙いの VPD の帯：{lo}〜{hi} kPa（設定ファイル・知識の §1 から）",
        (
            "- 最後に振り返った時刻：（まだ。最初の版は夏と秋の作業の記録をコードで計算した物）"
            if before is None
            else f"- 最後に振り返った時刻：（まだ。最初の版は {pd.Timestamp(before):%m/%d %H:%M} より前の作業の記録をコードで計算した物）"
        ),
        "- 決め方：入れる前の VPD と、入れて 15・30・60 分の VPD の差（node_a・前後 2 分以内の読み）。"
        "穴にかかる回は数えない。",
        "- **お手本ではない**：夏と秋は水のやりすぎを疑う期間。いつ入れたかは真似せず、効き目だけを見る。",
        "",
        "## 傾向（もとにした回の数つき）",
        "",
        "| 季節 | 回 | 入れる前の VPD（中央） | 15 分の差（中央） | 30 分の差（中央） | 60 分の差（中央） | 気温（中央） |",
        "|---|---|---|---|---|---|---|",
    ]
    for s, g in e.groupby("season", sort=False):
        lines.append(
            f"| {s} | {len(g)} | {g.vpd_before.median():.3f} | {_med(g, 'd15')} | {_med(g, 'd30')} | {_med(g, 'd60')} "
            f"| {g.temp_c.median():.1f}℃ |"
        )
    above = e[e.vpd_before > hi]
    lines += [
        "",
        f"- 入れる前に帯の上（>{hi}）だった回：{len(above)} 回／{len(e)} 回。"
        f"帯の中か下で入れた回：{len(e) - len(above)} 回（効き目は数えるが、入れる要があったかは別）。",
        "",
        "## 操作の効き目（事実・1 行 1 回）",
        "",
        "| 時刻 | 誰が | 季節 | 予定の分 | 気温 | RH | 入れる前の VPD | 15 分 | 30 分 | 60 分 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in e.sort_values("start").itertuples():
        lines.append(
            f"| {r.start:%m/%d %H:%M} | 手動（記録） | {r.season} | {r.minutes or '—'} | {r.temp_c:.1f} | {r.rh_pct:.0f} "
            f"| {r.vpd_before:.3f} | {_d(r.d15)} | {_d(r.d30)} | {_d(r.d60)} |"
        )
    return "\n".join(lines) + "\n"


def last_reviewed(md: str):
    """経験の中の「最後に振り返った時刻」（ISO）。まだなら None。"""
    for line in md.splitlines():
        if line.startswith("- 最後に振り返った時刻："):
            v = line.split("：", 1)[1].strip()
            try:
                return pd.Timestamp(v).to_pydatetime()
            except ValueError:
                return None
    return None
