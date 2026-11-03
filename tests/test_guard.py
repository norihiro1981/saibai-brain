"""守りの層のテスト。芯は「どんな計画が来ても上限を越えない」をランダムに大量に流して確かめるもの。"""

import random
from datetime import datetime, timedelta

import pytest

from saibai.guard import JST, REAL, REPLAY, Guard, Limits, Reading, Run, jst_day

T0 = datetime(2026, 8, 15, 5, 0, tzinfo=JST)
EV = [{"device": "node_a", "metric": "temp_c", "value": 27.0, "time": T0.isoformat()}]
EV_RH = [{"device": "node_a", "metric": "rh_pct", "value": 60.0, "time": T0.isoformat()}]


def fresh_guard(limits: Limits = REPLAY, rh: float = 60.0, now: datetime = T0) -> Guard:
    g = Guard(limits)
    # 15 分より前から少しずつ動いている、健全な読み
    for i in range(20):
        g.observe(Reading(now - timedelta(minutes=19 - i), rh + (i % 2) * 0.5))
    return g


def tick_until(g: Guard, start: datetime, minutes: int, rh: float = 60.0):
    log = []
    for i in range(minutes + 1):
        t = start + timedelta(minutes=i)
        g.observe(Reading(t, rh + (i % 2) * 0.5))
        log.append((t, g.decide(t)))
    return log


# ---- 1 本ずつの規則 ----


def test_starts_off():
    g = Guard(REAL)
    assert g.on is False
    assert g.decide(T0).on is False


def test_simple_plan_runs_inside_window():
    g = fresh_guard()
    assert g.submit_plan([Run(T0 + timedelta(minutes=5), 20)], EV, T0).ok
    log = tick_until(g, T0, 40)
    on_times = [t for t, d in log if d.on]
    assert on_times[0] == T0 + timedelta(minutes=5)
    assert len(on_times) == 20


def test_no_start_at_75_and_stop_at_85():
    g = fresh_guard(rh=76)
    v = g.submit_plan([Run(T0, 30)], EV, T0)
    assert not v.ok and "75%" in v.text()
    g = fresh_guard(rh=60)
    assert g.submit_plan([Run(T0, 60)], EV, T0).ok
    assert g.decide(T0).on
    for i, v in enumerate([65, 70, 75, 80], start=1):  # 運転中は 75% を越えても続ける
        g.observe(Reading(T0 + timedelta(minutes=i), v))
        assert g.decide(T0 + timedelta(minutes=i)).on
    t = T0 + timedelta(minutes=5)
    g.observe(Reading(t, 85.0))
    d = g.decide(t)
    assert not d.on and any("85%" in r for r in d.reasons)
    # 湿度が下がっても、その回の残りでは入れ直さない
    for i in range(6, 50):
        t = T0 + timedelta(minutes=i)
        g.observe(Reading(t, 60 + (i % 2)))
        assert not g.decide(t).on


def test_continuous_limit_real():
    g = fresh_guard(REAL)
    assert g.submit_plan([Run(T0, 60)], EV, T0).ok
    log = tick_until(g, T0, 70)
    assert sum(d.on for _, d in log) == 60


def test_plan_longer_than_continuous_limit_is_rejected():
    g = fresh_guard(REAL)
    v = g.submit_plan([Run(T0, 61)], EV, T0)
    assert not v.ok and "1〜60 分" in v.text()


def test_daily_limit_rejected_at_submit():
    g = fresh_guard(REAL)
    runs = [Run(T0, 60), Run(T0 + timedelta(minutes=90), 60), Run(T0 + timedelta(minutes=180), 10)]
    v = g.submit_plan(runs, EV, T0)
    assert not v.ok and "1 日の上限 120 分" in v.text()


def test_gap_between_runs():
    g = fresh_guard()
    v = g.submit_plan([Run(T0, 30), Run(T0 + timedelta(minutes=40), 30)], EV, T0)
    assert not v.ok and "間が 30 分に足りない" in v.text()


def test_stale_reading_turns_off_and_blocks():
    g = fresh_guard()
    assert g.submit_plan([Run(T0, 60)], EV, T0).ok
    assert g.decide(T0).on
    # 読みが来なくなって 6 分
    d = g.decide(T0 + timedelta(minutes=6))
    assert not d.on and any("古い" in r for r in d.reasons)


def test_stuck_sensor_rejects_plan_based_on_humidity():
    g = Guard(REPLAY)
    for i in range(20):
        g.observe(Reading(T0 - timedelta(minutes=19 - i), 62.0))  # 19 分同じ値
    v = g.submit_plan([Run(T0 + timedelta(minutes=5), 20)], EV_RH, T0)
    assert not v.ok and "変わらない" in v.text()
    assert not g.decide(T0).on


def test_jump_rejects_plan_based_on_humidity():
    g = fresh_guard()
    g.observe(Reading(T0 + timedelta(seconds=30), 75.0))  # 60 → 75 を 30 秒で
    v = g.submit_plan([Run(T0 + timedelta(minutes=5), 20)], EV_RH, T0 + timedelta(seconds=40))
    assert not v.ok and "跳んだ" in v.text()


def test_no_plan_for_three_hours_turns_off():
    g = fresh_guard()
    assert g.submit_plan([Run(T0, 180)], EV, T0).ok
    log = tick_until(g, T0, 185)
    on = [t for t, d in log if d.on]
    assert on[-1] < T0 + timedelta(hours=3)


def test_manual_stop_wins_over_everything():
    g = fresh_guard()
    assert g.submit_plan([Run(T0, 60)], EV, T0).ok
    assert g.decide(T0).on
    d = g.stop_all(T0 + timedelta(seconds=10))
    assert not d.on and d.changed
    assert not g.submit_plan([Run(T0 + timedelta(minutes=40), 10)], EV, T0).ok
    g.release_stop()
    assert g.submit_plan([Run(T0 + timedelta(minutes=40), 10)], EV, T0 + timedelta(minutes=1)).ok


def test_empty_plan_is_accepted_and_resets_timer():
    g = fresh_guard()
    assert g.submit_plan([], EV, T0).ok
    assert g.last_plan_at == T0


def test_naive_datetime_rejected():
    g = fresh_guard()
    v = g.submit_plan([Run(datetime(2026, 8, 15, 6, 0), 10)], EV, T0)
    assert not v.ok


# ---- ランダムに大量に流す ----


def independent_sensor_ok(hist: list[tuple[datetime, float]], now: datetime, L: Limits) -> bool:
    """守りの層と別に書いた、センサーを信じてよいかの判定（テストの側の物差し・跳びは見ない）。"""
    if not hist or now - hist[-1][0] > L.stale_after:
        return False
    last_t, last_v = hist[-1]
    first_same = last_t
    for t, v in reversed(hist):  # 後ろから、同じ値が続く所までさかのぼる
        if v != last_v:
            break
        first_same = t
    return last_t - first_same < L.stuck_after


def random_runs(rng: random.Random, now: datetime) -> list[Run]:
    runs = []
    t = now + timedelta(minutes=rng.randint(-5, 120))
    for _ in range(rng.randint(0, 5)):
        runs.append(Run(t, rng.choice([0, 1, 5, 15, 30, 59, 60, 61, 120, 180, 181, 400])))
        t += timedelta(minutes=rng.randint(-30, 240))
    return runs


def simulate(seed: int, limits: Limits, days: int = 2):
    rng = random.Random(seed)
    g = Guard(limits)
    now = T0
    rh = rng.uniform(40, 90)
    hist: list[tuple[datetime, float]] = []
    log = []  # (時刻, 入か, 入れた直前の湿度, センサーの健全, 手で止め中)
    end = T0 + timedelta(days=days)
    stuck_until = blackout_until = now
    while now < end:
        now += timedelta(seconds=rng.randint(20, 60))  # 判断は 1 分以内ごと
        # 湿度：ふだんは歩く・時々跳ぶ・時々止まる・時々読めない
        if now >= blackout_until and rng.random() < 0.002:
            blackout_until = now + timedelta(minutes=rng.randint(3, 20))
        if now >= stuck_until and rng.random() < 0.003:
            stuck_until = now + timedelta(minutes=rng.randint(10, 40))
        if now >= stuck_until:
            rh += (3.0 if g.on else -1.0) * rng.random() + rng.gauss(0, 0.7)
            if rng.random() < 0.003:
                rh += rng.choice([-1, 1]) * rng.uniform(10, 30)
            rh = round(min(100, max(20, rh)), 1)
        if now >= blackout_until:
            g.observe(Reading(now, rh))
            hist.append((now, rh))
        # 計画・手の操作がランダムに来る
        if rng.random() < 0.02:
            ev = EV_RH if rng.random() < 0.5 else EV
            g.submit_plan(random_runs(rng, now), ev, now)
        if rng.random() < 0.0015:
            g.stop_all(now)
        if g.manual_stop and rng.random() < 0.01:
            g.release_stop()
        was_on = g.on
        d = g.decide(now)
        log.append(
            (
                now,
                d.on,
                was_on,
                hist[-1][1] if hist else None,  # 守りの層が見られるのは読めた値だけ
                independent_sensor_ok(hist, now, limits),
                g.manual_stop,
            )
        )
    return log


def intervals_from(log):
    out, start = [], None
    for t, on, *_ in log:
        if on and start is None:
            start = t
        elif not on and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, log[-1][0]))
    return out


@pytest.mark.parametrize("limits", [REPLAY, REAL], ids=["再生", "実機"])
def test_random_plans_never_break_limits(limits):
    total_on = 0
    for seed in range(120):
        log = simulate(seed, limits)
        assert log[0][1] is False or log[0][2] is False  # 起動は切から
        ivs = intervals_from(log)
        total_on += len(ivs)
        for a, b in ivs:
            assert b - a <= limits.max_continuous, (seed, a, b)
        for (_, b), (c, _) in zip(ivs, ivs[1:], strict=False):
            assert c - b >= limits.min_gap, (seed, b, c)
        per_day: dict = {}
        for a, b in ivs:
            t = a
            while t < b:  # 日をまたぐ運転は日ごとに分ける
                nxt = min(
                    b, datetime.combine(jst_day(t) + timedelta(days=1), datetime.min.time(), JST)
                )
                per_day[jst_day(t)] = per_day.get(jst_day(t), timedelta(0)) + (nxt - t)
                t = nxt
        for d, used in per_day.items():
            assert used <= limits.max_daily, (seed, d, used)
        for t, on, was_on, rh, sensor_ok, manual in log:
            if on:
                assert not manual, (seed, t)
                assert sensor_ok, (seed, t)
                assert rh is not None and rh < limits.stop_rh, (seed, t, rh)
                if not was_on:
                    assert rh < limits.no_start_rh, (seed, t, rh)
    assert total_on > 50  # ちゃんと入る場面もあった（何もしない守りではない）
