"""模擬の栽培室（段 2）：入れると上がる・切ると記録へ戻る・同じ入力なら同じ結果・作り物の記録から式の値を取り戻す。"""

import numpy as np
import pandas as pd
import pytest

from saibai import sim


def synthetic(days=4, k=0.05, kh=2.0, s=0.2, g=5.0, temp=20.0, noise=0.0, seed=0):
    """合わせる式そのもので作った記録（1 分ごと）。加湿器は毎日 02:00〜04:00 と 14:00〜15:00。"""
    ts = pd.date_range("2026-10-01", periods=days * 1440, freq="1min", tz="Asia/Tokyo")
    hours = ((ts - ts[0]).total_seconds() / 3600).to_numpy()
    hum = (((ts.hour >= 2) & (ts.hour < 4)) | (ts.hour == 14)).astype(float)
    ah_out = 9.0 + 2.0 * np.sin(2 * np.pi * hours / 24)
    b = sim.response(hours, k * ah_out + s, k, 10.0)
    d = sim.response(hours, g * hum, kh)
    ah = b + d + np.random.default_rng(seed).normal(0, noise, len(ts))
    t = np.full(len(ts), temp)
    spans = []
    on = np.flatnonzero(np.diff(np.r_[0, hum]) == 1)
    off = np.flatnonzero(np.diff(np.r_[hum, 0]) == -1) + 1
    for a, z in zip(on, off, strict=True):
        spans.append((ts[a], ts[min(z, len(ts) - 1)]))
    return sim.Season(
        name="test",
        ts=ts,
        temp=t,
        rh=sim.rh_from(ah, t),
        ah=ah,
        ah_out=ah_out,
        hum=hum,
        cool=np.ones(len(ts)),
        soil=pd.Series(dtype=float),
        acts=pd.DataFrame(),
        hum_spans=spans,
    )


def test_humidity_conversion_round_trips():
    ah = sim.ah_from(20.0, 70.0)
    assert ah == pytest.approx(12.1, abs=0.1)  # 20℃・70% ≒ 12.1 g/m³
    assert sim.rh_from(ah, 20.0) == pytest.approx(70.0)
    assert sim.vpd_from(20.0, 70.0) == pytest.approx(0.701, abs=0.001)  # 知識の §1 の表と同じ


def test_fit_recovers_the_parameters_of_a_synthetic_room():
    S = synthetic(k=sim.K_GRID[12], kh=sim.KH_GRID[15], s=0.2, g=5.0)
    p = sim.fit_params(S, np.ones(len(S.ts), bool))
    assert p["k_per_h"] == pytest.approx(sim.K_GRID[12], rel=0.2)
    assert p["kh_per_h"] == pytest.approx(sim.KH_GRID[15], rel=0.2)
    assert p["g"] == pytest.approx(5.0, rel=0.2)
    assert p["s"] == pytest.approx(0.2, rel=0.3)


def _room(S, start=0, end=None):
    return sim.Room(S, k=2.0, g=5.0, start=start, end=end or len(S.ts))


def test_turning_on_raises_and_turning_off_returns_to_the_record():
    S = synthetic(noise=0.05)
    i0 = int(np.flatnonzero((S.ts.hour == 8) & (S.ts.minute == 0))[0])  # 記録では加湿器が切の時間
    room = _room(S, i0, i0 + 300)
    rh0 = room.reading()[2]
    for _ in range(30):
        room.step(True)
    rh30 = room.reading()[2]
    assert rh30 > rh0 + 5  # 入れると上がる（記録より湿る）
    assert room.delta > 0
    for _ in range(120):
        room.step(False)
    _, _, rh, _ = room.reading()
    assert abs(rh - S.rh[room.i]) < 0.5  # 切ると記録（＝外へ向かう部屋）へ戻る
    assert room.delta == pytest.approx(0, abs=0.05)


def test_actual_operation_reproduces_the_record_exactly():
    S = synthetic(noise=0.05)
    room = _room(S, 0, 2000)
    while True:
        t, temp, rh, vpd = room.reading()
        assert rh == pytest.approx(S.rh[room.i], abs=0.01)
        if not room.step(bool(S.hum[room.i])):
            break
    assert room.delta == 0


def test_not_running_when_the_record_ran_makes_it_drier():
    S = synthetic()
    i0 = int(np.flatnonzero((S.ts.hour == 2) & (S.ts.minute == 0))[0])
    room = _room(S, i0, i0 + 200)
    for _ in range(60):
        room.step(False)
    assert room.reading()[2] < S.rh[room.i] - 3


def test_same_input_same_result_and_rh_never_above_100():
    S = synthetic(temp=15.0)
    rng = np.random.default_rng(3)
    plan = rng.random(1500) < 0.7

    def run():
        room = _room(S, 0, 1500)
        out = []
        for on in plan:
            out.append(room.reading())
            if not room.step(bool(on)):
                break
        return out

    a, b = run(), run()
    assert a == b
    assert max(r[2] for r in a) <= 100.0
