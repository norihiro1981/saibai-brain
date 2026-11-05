"""定時・長さは AI のテスト（モデルは呼ばない＝制御役の答えは作り物）。"""

import json
from datetime import datetime, timedelta

from saibai.auto import check_timer_plan, timer_prompt
from saibai.control import Controller, DummyPlug, NoSchedule
from saibai.guard import JST, REAL, Guard, Run


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 5, 11, 40, tzinfo=JST)

    def __call__(self):
        return self.t


def make(tmp_path, answer):
    """answer(ctrl, at, default) が制御役の代わり。AutoRunner と同じく、受け付けた計画は守りの層に「定時」で出す。"""
    clock = Clock()
    plug = DummyPlug()
    ctrl = Controller(Guard(REAL), plug, NoSchedule(), tmp_path, clock)
    ctrl._spawn = lambda f, *a: f(*a)  # 裏の糸を使わずにその場で
    ctrl.timer_ai = lambda at, default: answer(ctrl, at, default)
    ctrl.start()
    ctrl.set_timer(True, "12:00", 60, ai_minutes=True)
    return ctrl, plug, clock


def run_minutes(ctrl, clock, minutes):
    """毎分読んで判断する。入っている間は湿度が上がる（15 分で上がらなければ切る、に当てない）。"""
    rh, on_min = 60.0, 0
    for _ in range(minutes):
        clock.t += timedelta(minutes=1)
        ctrl.observe(clock.t, round(rh, 1), 15.0 + (clock.t.minute % 2) * 0.1)
        ctrl.tick(check_plug=True)
        if ctrl.guard.on:
            on_min += 1
            rh += 0.2
    return on_min


def actions(tmp_path):
    lines = (tmp_path / "control_20261105.jsonl").read_text().splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") == "action"]


def ai_says(minutes, start=None):
    def answer(ctrl, at, default):
        ev = [
            {"device": "経験", "metric": "15 分の VPD の差", "value": -0.2, "time": at.isoformat()}
        ]
        runs = [Run(start or at, minutes, "定時")] if minutes else []
        err = check_timer_plan(runs, at, ctrl.now())
        if err:
            return {"ok": False, "text": err}
        v = ctrl.submit(runs, ev, "定時", extra={"reason": "秋の傾向の行", "by": "AI の分"})
        return {"ok": v.ok, "minutes": minutes, "reason": "秋の傾向の行", "text": v.text()}

    return answer


def test_ai_minutes_are_used(tmp_path):
    asked = []

    def answer(ctrl, at, default):
        asked.append((ctrl.now(), at, default))
        return ai_says(25)(ctrl, at, default)

    ctrl, plug, clock = make(tmp_path, answer)
    on = run_minutes(ctrl, clock, 90)  # 11:40 → 13:10
    assert asked and asked[0][1].strftime("%H:%M") == "12:00" and asked[0][2] == 60
    assert asked[0][0] >= asked[0][1] - timedelta(minutes=10)  # 10 分前より前には聞かない
    assert on == 25
    assert ctrl.timer["last"]["by"] == "AI の分" and ctrl.timer["last"]["minutes"] == 25
    assert any(a["what"] == "長さは AI で動く" and a["minutes"] == 25 for a in actions(tmp_path))


def test_no_answer_runs_default_minutes(tmp_path):
    ctrl, plug, clock = make(tmp_path, lambda ctrl, at, d: {"ok": False, "text": "答えが来ない"})
    on = run_minutes(ctrl, clock, 90)
    assert on == 60
    last = ctrl.timer["last"]
    assert last["by"] == "既定の分" and last["minutes"] == 60 and "答えが来ない" in last["reason"]


def test_ai_failure_runs_default_minutes(tmp_path):
    def boom(ctrl, at, d):
        raise ConnectionError("落ちた")

    ctrl, plug, clock = make(tmp_path, boom)
    assert run_minutes(ctrl, clock, 90) == 60
    assert "聞けなかった" in ctrl.timer["last"]["reason"]


def test_plan_that_moves_the_start_is_refused_and_default_runs(tmp_path):
    ctrl, plug, clock = make(tmp_path, ai_says(30, start=datetime(2026, 11, 5, 12, 30, tzinfo=JST)))
    assert run_minutes(ctrl, clock, 120) == 60
    assert "開始は 12:00 に決まっている" in ctrl.timer["last"]["reason"]


def test_check_timer_plan():
    at = datetime(2026, 11, 5, 12, 0, tzinfo=JST)
    before = at - timedelta(minutes=8)
    assert check_timer_plan([Run(at, 30)], at, before) is None
    assert check_timer_plan([], at, before) is None  # 今日は入れない（0 分）
    assert "決まっている" in check_timer_plan([Run(at + timedelta(minutes=5), 30)], at, before)
    assert "1 回だけ" in check_timer_plan(
        [Run(at, 10), Run(at + timedelta(hours=1), 10)], at, before
    )
    assert "過ぎた" in check_timer_plan([Run(at, 30)], at, at)


def test_ai_zero_minutes_means_no_run(tmp_path):
    ctrl, plug, clock = make(tmp_path, ai_says(0))
    assert run_minutes(ctrl, clock, 90) == 0
    assert ctrl.timer["last"]["by"] == "AI の分" and ctrl.timer["last"]["minutes"] == 0


def test_timer_without_ai_unchanged(tmp_path):
    ctrl, plug, clock = make(tmp_path, ai_says(25))
    ctrl.set_timer(True, ai_minutes=False)
    assert run_minutes(ctrl, clock, 90) == 60
    assert ctrl.timer["last"]["by"] == "人の分"


def test_timer_prompt_fixes_start():
    at = datetime(2026, 11, 5, 12, 0, tzinfo=JST)
    g = Guard(REAL).snapshot(at)
    text = timer_prompt(
        at - timedelta(minutes=10),
        at,
        60,
        {"guard": g},
        {"vpd_low_kpa": 0.4, "vpd_high_kpa": 0.8},
        None,
    )
    assert "開始は 2026-11-05T12:00+09:00 に人が決めている" in text and "既定は 60 分" in text
    assert "天気は無し" in text
