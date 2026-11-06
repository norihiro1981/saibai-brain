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


def test_timer_submits_daily_run_through_guard(tmp_path):
    """定時（AI なし）：毎日の開始の時刻に「定時」の計画を出し、守りの層を通って入り、予定の分で切れる。"""
    ctrl, plug, clock = make(tmp_path)  # 09:20
    r = ctrl.set_timer(True, "09:25", 10)
    assert r["ok"] and ctrl.timer["on"]
    assert not ctrl.set_timer(True, "25:00")["ok"]
    assert not ctrl.set_timer(True, None, 90)["ok"]  # 1 回の上限（60 分）を越える
    step_humidifying(ctrl, clock, 6)  # 09:26
    assert plug.on and actions(tmp_path)[-1]["who"] == "定時"
    step_humidifying(ctrl, clock, 11)
    assert plug.on is False
    end = actions(tmp_path)[-1]
    assert end["who"] == "定時" and end["what"] == "切" and end["reasons"] == ["予定の分が終わった"]
    step_humidifying(ctrl, clock, 30)
    assert plug.on is False  # 同じ日にもう一度は出さない


def test_timer_turned_on_after_start_waits_for_tomorrow(tmp_path):
    ctrl, plug, clock = make(tmp_path)  # 09:20
    ctrl.set_timer(True, "09:00", 10)
    step_humidifying(ctrl, clock, 3)
    assert plug.on is False and ctrl.timer["fired_on"] == clock.t.date().isoformat()


def test_history_breaks_lines_at_gaps(tmp_path):
    from saibai.receiver import history

    t0 = datetime(2026, 11, 5, 9, 0, tzinfo=JST)
    with (tmp_path / "meter_20261105.jsonl").open("w") as f:
        for i in list(range(10)) + list(range(20, 25)):  # 10 分の穴
            t = t0 + timedelta(minutes=i)
            f.write(
                json.dumps(
                    {
                        "read_at": t.isoformat(),
                        "ok": True,
                        "temp_c": 15.0,
                        "rh_pct": 67,
                        "vpd_kpa": 0.563,
                    }
                )
                + "\n"
            )
    h = history(tmp_path, t0 + timedelta(minutes=30), hours=1)
    nulls = [p for p in h["points"] if p[1] is None]
    assert len(nulls) == 1 and len(h["points"]) == 16 and h["target"]["vpd_high_kpa"] == 0.8


def test_manual_on_reports_plug_failure(tmp_path):
    """守りの層が受け付けても、プラグが入らなければ「済み」と言わず、断られた理由を返す（11/5）。"""
    ctrl, plug, clock = make(tmp_path)
    plug.fail_with = "turnOn を断られた：HTTP 200・statusCode 190・テスト"
    r = ctrl.set_humidifier(True, 30)
    assert not r["ok"] and "プラグが入らなかった" in r["text"] and "statusCode 190" in r["text"]
    assert plug.on is False
    st = ctrl.status()["plug"]
    assert st["want_on"] is True and st["actual_on"] is False and "190" in st["error"]
    lines = [
        json.loads(x)
        for x in (tmp_path / f"control_{clock.t:%Y%m%d}.jsonl").read_text().splitlines()
    ]
    failed = [x for x in lines if x["kind"] == "plug_set" and not x["ok"]]
    assert failed and "190" in failed[-1]["error"]
    manual = [x for x in lines if x["kind"] == "action" and x["who"] == "手動"][-1]
    assert manual["accepted"] is False and "190" in manual["reasons"][0]
    # 命令が通るようになれば、毎分の点検で入れ直して理由は消える
    plug.fail_with = None
    clock.t += timedelta(minutes=1)
    ctrl.tick(check_plug=True)
    assert plug.on is True and ctrl.status()["plug"]["error"] is None


class _Reply:
    def __init__(self, http, status_code, message, body=None):
        self.http, self.status_code, self.message, self.body = (
            http,
            status_code,
            message,
            body or {},
        )


class _Client:
    def __init__(self, cmd_reply, power="off"):
        self.cmd_reply, self.power = cmd_reply, power

    def command(self, device_id, command):
        if isinstance(self.cmd_reply, Exception):
            raise self.cmd_reply
        return self.cmd_reply

    def status(self, device_id):
        return _Reply(200, 100, "success", {"power": self.power})


def test_switchbot_plug_keeps_reason():
    """本物のプラグの類は叩かず、偽のクライアントで返りの理由が残るのを確かめる。"""
    from saibai.control import SwitchBotPlug

    p = SwitchBotPlug(_Client(_Reply(200, 161, "device offline")), "x", settle_s=0)
    assert p.set(True) is False and "161" in p.last_error and "device offline" in p.last_error
    p = SwitchBotPlug(_Client(TimeoutError("遅い")), "x", settle_s=0)
    assert p.set(True) is False and "TimeoutError" in p.last_error
    p = SwitchBotPlug(_Client(_Reply(200, 100, "success"), power="off"), "x", settle_s=0)
    assert p.set(True) is False and "秒たっても状態が切" in p.last_error
    p = SwitchBotPlug(_Client(_Reply(200, 100, "success"), power="on"), "x", settle_s=0)
    assert p.set(True) is True and p.last_error is None


class _LateClient(_Client):
    """命令は通るが、状態が変わるのが遅い（11/6 の工場）。on_after 回目の読みで入になる。
    resend_needed＝1 回目の命令は本体に届かず、2 回目の命令の後だけ変わる。"""

    def __init__(self, on_after, resend_needed=False):
        super().__init__(_Reply(200, 100, "success"))
        self.on_after, self.resend_needed = on_after, resend_needed
        self.reads = self.commands = 0

    def command(self, device_id, command):
        self.commands += 1
        if self.resend_needed and self.commands == 2:
            self.reads = 0  # 2 回目の命令から数え直す
        return self.cmd_reply

    def status(self, device_id):
        self.reads += 1
        ready = self.reads >= self.on_after and (not self.resend_needed or self.commands >= 2)
        return _Reply(200, 100, "success", {"power": "on" if ready else "off"})


def test_switchbot_plug_waits_and_resends():
    """3 秒で変わらなくても諦めない：読み直しを重ね、それでも変わらなければ命令をもう一度送る（11/6）。"""
    from saibai.control import SwitchBotPlug

    c = _LateClient(on_after=3)
    p = SwitchBotPlug(c, "x", settle_s=0)
    assert p.set(True) is True and c.commands == 1 and p.last_error is None
    c = _LateClient(on_after=1, resend_needed=True)
    p = SwitchBotPlug(c, "x", settle_s=0)
    assert p.set(True) is True and c.commands == 2
    c = _LateClient(on_after=99)
    p = SwitchBotPlug(c, "x", settle_s=0)
    assert p.set(True) is False and c.commands == 2 and "2 回とも通ったが" in p.last_error


def test_failed_on_is_retried_as_receiver_not_outside(tmp_path):
    """入の命令が効かないまま毎分の点検で入れ直す時、「外から切った」とは書かない（11/6）。"""
    ctrl, plug, clock = make(tmp_path)
    plug.fail_with = "turnOn は 2 回とも通ったが 18 秒たっても状態が切"
    r = ctrl.set_humidifier(True, 30)
    assert not r["ok"] and "切を押さなくてよい" in r["text"]
    clock.step(ctrl, 1)
    lines = [
        json.loads(x)
        for x in (tmp_path / f"control_{clock.t:%Y%m%d}.jsonl").read_text().splitlines()
    ]
    last = [x for x in lines if x["kind"] == "action"][-1]
    assert last["who"] == "受け手" and last["what"] == "入れ直す"
    assert ctrl.guard.on is True  # 運転は途中で止めない
    plug.fail_with = None
    clock.step(ctrl, 1)
    assert plug.on is True


def test_status_does_not_wait_for_slow_plug(tmp_path):
    """プラグへの命令の最中（錠を持っている間）でも、状態は前の答えをすぐ返す。"""
    ctrl, plug, clock = make(tmp_path)
    ctrl.status()
    held, release = threading.Event(), threading.Event()

    def hold():
        with ctrl._lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    st = ctrl.status()
    release.set()
    t.join()
    assert "busy" in st and "plug" in st
    assert "busy" not in ctrl.status()
