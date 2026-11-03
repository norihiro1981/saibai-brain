"""受け手の動かす側のテスト。本物のプラグは叩かない（模擬のプラグだけ）。"""

import json
import threading
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
            ctrl.observe(self.t, rh + (i % 2) * 0.5)
            ctrl.tick(check_plug=check_plug)


def make(tmp_path, plug_on=True):
    clock = Clock()
    plug = DummyPlug(on=plug_on)  # 停電から戻って「入」のまま、を模す
    ctrl = Controller(Guard(REAL), plug, NoSchedule(), tmp_path, clock)
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
    ctrl, plug, clock = make(tmp_path)
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
