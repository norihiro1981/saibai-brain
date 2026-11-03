"""自己点検のテスト：作った小さな記録に癖を仕込み、拾うこと・普通の記録では拾わないことを確かめる。"""

import numpy as np
import pandas as pd

from saibai.data import load_actions, load_readings, season_files
from saibai.selfcheck import check

START = pd.Timestamp("2026-08-20T00:00:30+09:00").tz_convert("Asia/Tokyo")


def normal_record(hours: int = 30, seed: int = 0) -> pd.DataFrame:
    """2 台の温湿度計と土の水分計の、ふつうに揺れる記録（61 秒ごと）。"""
    rng = np.random.default_rng(seed)
    n = hours * 60
    t = START + pd.to_timedelta(np.arange(n) * 61, unit="s")
    day = np.sin(np.arange(n) / (24 * 60 / 61) * 2 * np.pi)
    temp = 26 + 2 * day + rng.normal(0, 0.05, n)
    rh = 80 - 6 * day + rng.normal(0, 0.3, n)
    rows = []
    for dev, dt, drh in (("node_a", 0.0, 0.0), ("node_c", -0.5, 4.0)):
        rows.append(
            pd.DataFrame(
                {
                    "timestamp": t,
                    "device_id": dev,
                    "temp_c": (temp + dt).round(2),
                    "rh_pct": (rh + drh + rng.normal(0, 0.2, n)).round(2),
                }
            )
        )
    soil = 900 + rng.normal(0, 20, n)
    rows.append(pd.DataFrame({"timestamp": t, "device_id": "soil_a", "soil_raw": soil.round()}))
    df = pd.concat(rows, ignore_index=True)
    for c in ["temp_c", "rh_pct", "vpd_kpa", "co2_ppm", "soil_raw"]:
        if c not in df:
            df[c] = np.nan
    return df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)


def kinds(findings, device=None):
    return sorted(
        {(f.device, f.kind, f.metric) for f in findings if device is None or f.device == device}
    )


def rows_of(df, dev):
    return df.index[df["device_id"] == dev]


def test_normal_record_has_no_findings():
    assert check(normal_record()) == []


def test_stuck_value_is_found():
    df = normal_record()
    idx = rows_of(df, "node_c")[600:630]  # 30 分ほど同じ値
    df.loc[idx, "rh_pct"] = 100.0
    f = [x for x in check(df) if x.kind == "stuck"]
    assert [(x.device, x.metric) for x in f] == [("node_c", "rh_pct")]
    assert f[0].evidence["value"] == 100.0 and f[0].evidence["minutes"] >= 15


def test_spike_and_step_are_told_apart():
    df = normal_record()
    a = rows_of(df, "node_a")
    df.loc[a[300], "rh_pct"] += 15  # 1 回だけ跳んで戻る
    df.loc[a[900:], "temp_c"] += 4  # 段差のまま戻らない
    got = kinds(check(df), "node_a")
    assert ("node_a", "spike", "rh_pct") in got
    assert ("node_a", "step", "temp_c") in got


def test_gap_over_five_minutes():
    df = normal_record()
    drop = rows_of(df, "soil_a")[500:507]  # 7 回分≒7 分の欠け
    df = df.drop(drop)
    f = [x for x in check(df) if x.kind == "gap"]
    assert [(x.device, x.evidence["minutes"] > 5) for x in f] == [("soil_a", True)]


def test_four_minute_hole_is_not_a_gap():
    df = normal_record()
    df = df.drop(rows_of(df, "soil_a")[500:503])  # 3 回抜け≒4 分
    assert [x for x in check(df) if x.kind == "gap"] == []


def test_disagreement_between_two_meters():
    df = normal_record()
    c = rows_of(df, "node_c")
    df.loc[c[1500:1560], "rh_pct"] -= (
        14  # 1 時間だけ片方がずれる（段差としても拾われるが、ここでは食い違いを見る）
    )
    got = kinds(check(df))
    assert ("node_a/node_c", "disagree", "rh_pct") in got


def test_burst_of_spikes_is_merged_into_one_interval():
    df = normal_record()
    s = rows_of(df, "soil_a")
    for k in range(800, 860, 4):  # 1 時間のあいだ 4 分おきに跳ぶ
        df.loc[s[k], "soil_raw"] = 300
    f = [x for x in check(df) if x.device == "soil_a"]
    assert len(f) == 1 and f[0].evidence["count"] >= 10


def test_loaders_give_aware_jst_times():
    env, act = season_files("autumn")
    r = load_readings(env)
    assert str(r["timestamp"].dt.tz) == "Asia/Tokyo"
    assert set(r["device_id"].unique()) == {"node_a", "node_c", "soil_a"}  # 秋は node_b が無い
    a = load_actions(act)
    assert str(a["start_jst"].dt.tz) == "Asia/Tokyo"
    assert a["kind"].isin(["humidifier_on", "watering", "other"]).all()
