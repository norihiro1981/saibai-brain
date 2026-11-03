"""受け手の動かす側のテスト。本物のプラグは叩かない（模擬のプラグだけ）。"""

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx

from saibai.control import Controller, DummyPlug, NoSchedule
from saibai.guard import JST, REAL, Guard
from saibai.receiver import MeterLog, make_handler


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 5, 9, 0, tzinfo=JST)

    def __call__(self):
        return self.t

    def step(self, ctrl: Controller, minutes: int, rh: float = 60.0, check_plug: bool = True):
        for i in range(minutes):
            self.t += timedelta(minutes=1)
            ctrl.observe(self.t, rh + (i % 2) * 0.5, 15.0)
            ctrl.tick(check_plug=check_plug)


def make(tmp_path, plug_on=True, limits=REAL):
    clock = Clock()
    plug = DummyPlug(on=plug_on)  # 停電から戻って「入」のまま、を模す
    ctrl = Controller(Guard(limits), plug, NoSchedule(), tmp_path, clock)
    ctrl.start()
    clock.step(ctrl, 20)  # 健全な読みを 20 分
    return ctrl, plug, clock


def test_start_turns_plug_off(tmp_path):
    ctrl, plug, _ = make(tmp_path, plug_on=True)
    assert plug.commands[0] is False and plug.on is False


def test_unplanned_on_is_turned_off_on_next_check(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    plug.on = True  # 誰かがプラグのボタンを押した
    clock.step(ctrl, 1)
    assert plug.on is False
    lines = [json.loads(x) for x in (tmp_path / "control_20261105.jsonl").read_text().splitlines()]
    assert any(x["kind"] == "plug_mismatch" for x in lines)


def test_switch_goes_through_guard(tmp_path):
    # 本番に戻した時の線（入と入の間 30 分）で確かめる
    ctrl, plug, clock = make(tmp_path, limits=replace(REAL, min_gap=timedelta(minutes=30)))
    r = ctrl.set_humidifier(True, 30)
    assert r["ok"] and plug.on is True
    clock.step(ctrl, 31)
    assert plug.on is False  # 30 分で切れた
    r = ctrl.set_humidifier(True, 30)
    assert not r["ok"] and "切ったばかり" in r["text"]  # 間が 30 分に足りない
    assert plug.on is False


def test_switch_rejected_over_continuous_limit(tmp_path):
    ctrl, plug, _ = make(tmp_path)
    r = ctrl.set_humidifier(True, 90)
    assert not r["ok"] and "1〜60 分" in r["text"] and plug.on is False


def test_switch_rejected_when_humid(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    clock.step(ctrl, 3, rh=78)
    r = ctrl.set_humidifier(True, 30)
    assert not r["ok"] and "75%" in r["text"] and plug.on is False


def test_stop_all_wins_and_release(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    ctrl.set_humidifier(True, 30)
    assert plug.on
    ctrl.stop_all(release=False)
    assert plug.on is False
    assert not ctrl.set_humidifier(True, 10)["ok"]
    clock.step(ctrl, 5)
    assert plug.on is False
    ctrl.stop_all(release=True)
    clock.step(ctrl, 30)
    assert ctrl.set_humidifier(True, 10)["ok"] and plug.on


def test_stale_meter_turns_plug_off(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    ctrl.set_humidifier(True, 60)
    assert plug.on
    for _ in range(6):  # 温湿度計が 6 分読めない
        clock.t += timedelta(minutes=1)
        ctrl.tick(check_plug=True)
    assert plug.on is False


def test_http_routes_go_through_controller(tmp_path):
    ctrl, plug, _ = make(tmp_path)
    log = MeterLog(tmp_path / "m")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(log, ctrl))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        r = httpx.post(base + "/humidifier", json={"on": True, "minutes": 999}).json()
        assert r["ok"] is False and plug.on is False
        r = httpx.post(base + "/humidifier", json={"on": True, "minutes": 15}).json()
        assert r["ok"] is True and plug.on is True
        s = httpx.get(base + "/status").json()
        assert s["plug"]["want_on"] is True and s["guard"]["max_continuous_min"] == 60
        assert s["schedule"]["available"] is False
        r = httpx.post(base + "/stop", json={}).json()
        assert plug.on is False and httpx.get(base + "/status").json()["guard"]["manual_stop"]
    finally:
        httpd.shutdown()


def test_switch_says_why_when_sensor_is_stuck(tmp_path):
    """値が止まった疑いの時、「受け付けた」と返して入らない、をしない。"""
    ctrl, plug, clock = make(tmp_path)
    for _ in range(91):  # 温度も湿度も 91 分そろって同じ
        clock.t += timedelta(minutes=1)
        ctrl.observe(clock.t, 67.0, 15.3)
        ctrl.tick(check_plug=True)
    r = ctrl.set_humidifier(True, 1)
    assert r["ok"] is False and plug.on is False
    assert "センサーが疑わしい" in r["text"] and "変わらない" in r["text"]


def test_switch_on_off_on_after_gap(tmp_path):
    """入→切→（30 分あけて）入、が入ること。"""
    ctrl, plug, clock = make(tmp_path)
    assert ctrl.set_humidifier(True, 30)["ok"] and plug.on
    clock.step(ctrl, 1)
    ctrl.set_humidifier(False, 0)
    assert plug.on is False
    clock.step(ctrl, 30)
    r = ctrl.set_humidifier(True, 30)
    assert r["ok"] and plug.on, r


def step_humidifying(ctrl, clock, minutes: int, rh: float = 60.0):
    """加湿器が入っている間は湿度が 0.2 pt/分 上がる（15 分で上がらなければ切る線に当たらない）。"""
    for _ in range(minutes):
        clock.t += timedelta(minutes=1)
        if ctrl.guard.on:
            rh += 0.2
        ctrl.observe(clock.t, round(rh, 1), 15.0)
        ctrl.tick(check_plug=True)


def actions(tmp_path) -> list[dict]:
    lines = (tmp_path / "control_20261105.jsonl").read_text().splitlines()
    return [x for x in map(json.loads, lines) if x["kind"] == "action"]


def test_restart_keeps_daily_total(tmp_path):
    """起こし直しても 1 日の上限が数え直しにならない（帳簿を記録から読み戻す）。"""
    ctrl, plug, clock = make(tmp_path)
    assert ctrl.set_humidifier(True, 60)["ok"]
    step_humidifying(ctrl, clock, 61)
    assert ctrl.set_humidifier(True, 50)["ok"]
    step_humidifying(ctrl, clock, 51)
    used = ctrl.guard.used_on(clock.t.date(), clock.t)
    assert used >= timedelta(minutes=80)
    # 起こし直す（同じ記録の場所・新しい守りの層）
    ctrl2 = Controller(Guard(REAL), plug, NoSchedule(), tmp_path, clock)
    ctrl2.start()
    assert ctrl2.guard.used_on(clock.t.date(), clock.t) == used
    clock.step(ctrl2, 2)
    r = ctrl2.set_humidifier(True, 120 - used // timedelta(minutes=1) + 1)
    assert not r["ok"] and "1 日の上限 120 分" in r["text"]


def test_restart_after_crash_while_on(tmp_path):
    """入のまま落ちた時は、最後の記録の時刻までを動いた分に数え、そこで切ったとみなす。"""
    ctrl, plug, clock = make(tmp_path, limits=replace(REAL, min_gap=timedelta(minutes=30)))
    assert ctrl.set_humidifier(True, 60)["ok"]
    step_humidifying(ctrl, clock, 40)
    last = clock.t
    (tmp_path / "meter_20261105.jsonl").write_text(
        json.dumps({"read_at": last.isoformat(), "ok": True}) + "\n"
    )
    clock.t += timedelta(minutes=1)  # 落ちてすぐ起こし直された（穴にはならない）
    ctrl2 = Controller(
        Guard(replace(REAL, min_gap=timedelta(minutes=30))), plug, NoSchedule(), tmp_path, clock
    )
    ctrl2.start()
    assert ctrl2.guard.used_on(clock.t.date(), clock.t) == timedelta(minutes=40)
    assert ctrl2.guard.last_off == last
    assert not any(a["what"] == "記録なし" for a in actions(tmp_path))


def test_action_record_has_who_what_and_values(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    ctrl.set_humidifier(True, 2)
    clock.step(ctrl, 3)
    ctrl.set_humidifier(True, 90)  # 断られる
    acts = actions(tmp_path)
    on = next(a for a in acts if a["who"] == "手動" and a["what"] == "入")
    assert on["accepted"] is True and on["minutes"] == 2 and on["plug_after"] is True
    assert on["rh_pct"] is not None and on["vpd_kpa"] is not None
    end = next(a for a in acts if a["what"] == "切" and a["who"] == "手動")
    assert end["reasons"] == ["予定の分が終わった"] and end["plug_after"] is False
    rej = acts[-1]
    assert rej["who"] == "手動" and rej["accepted"] is False and rej["reasons"]
    # 守りの層が切った回は「守りの層」
    ctrl.set_humidifier(True, 30)
    for rh in (65, 70, 75, 80, 86):  # 1 分に 5 pt ずつ（跳びにはしない）
        clock.step(ctrl, 1, rh=rh)
    assert (
        actions(tmp_path)[-1]["who"] == "守りの層" and "85%" in actions(tmp_path)[-1]["reasons"][0]
    )


def test_unplanned_change_is_logged_as_outside(tmp_path):
    ctrl, plug, clock = make(tmp_path)
    plug.on = True
    clock.step(ctrl, 1)
    a = actions(tmp_path)[-1]
    assert a["who"] == "外から" and a["what"] == "入" and a["plug_after"] is False


def test_gap_is_left_as_a_hole_and_outside_change(tmp_path):
    """止まっていた間を「記録なし」として残し、プラグが変わっていれば「外から・空きの間のどこか」。"""
    ctrl, plug, clock = make(tmp_path)
    (tmp_path / "meter_20261105.jsonl").write_text(
        json.dumps({"read_at": clock.t.isoformat(), "ok": True}) + "\n"
    )
    clock.t += timedelta(minutes=10)  # 受け手が 10 分止まり、その間に誰かがプラグを入れた
    plug.on = True
    ctrl2 = Controller(Guard(REAL), plug, NoSchedule(), tmp_path, clock)
    ctrl2.start()
    acts = actions(tmp_path)
    hole = next(a for a in acts if a["what"] == "記録なし")
    assert hole["gap_start"] == (clock.t - timedelta(minutes=10)).isoformat()
    outside = next(a for a in acts if a["who"] == "外から")
    assert outside["what"] == "入" and "記録なしの間" in outside["note"]
    assert plug.on is False  # 起動した時は切から


def test_status_has_vpd(tmp_path):
    log = MeterLog(tmp_path)
    t = datetime(2026, 11, 5, 9, 0, tzinfo=JST)
    log.add({"read_at": t.isoformat(), "ok": True, "temp_c": 15.0, "rh_pct": 80, "vpd_kpa": 0.341})
    assert log.status(t)["meter"]["vpd_kpa"] == 0.341


def test_auto_plan_input_goes_through_guard(tmp_path):
    """オートの計画（submit_plan の入力）は守りの層を通る。読めない入力は理由つきで断る。"""
    from saibai.auto import parse_plan

    ctrl, plug, clock = make(tmp_path)
    runs, ev, err = parse_plan({"runs": [{"start": "あした", "minutes": 10}], "evidence": []})
    assert err and "読めない" in err
    start = (clock.t + timedelta(minutes=5)).isoformat()
    runs, ev, err = parse_plan(
        {
            "runs": [{"start": start, "minutes": 90}],
            "evidence": [{"metric": "vpd_kpa", "value": 0.9}],
        }
    )
    v = ctrl.submit(runs, ev, "オート")
    assert not v.ok and "1〜60 分" in v.text()
    runs, ev, _ = parse_plan(
        {
            "runs": [{"start": start, "minutes": 20}],
            "evidence": [{"metric": "vpd_kpa", "value": 0.9}],
        }
    )
    assert ctrl.submit(runs, ev, "オート").ok
    step_humidifying(ctrl, clock, 6)
    assert plug.on and actions(tmp_path)[-1]["who"] == "オート"
