import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from saibai.receiver import JST, MeterLog, make_handler, read_meter
from saibai.switchbot import Reply

T0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=JST)


def rec(t: datetime, temp=19.1, rh=60, ok=True):
    r = {"read_at": t.isoformat(timespec="seconds"), "ok": ok}
    if ok:
        r |= {"temp_c": temp, "rh_pct": rh, "battery": 100}
    else:
        r["error"] = "値が返らなかった"
    return r


def test_status_age_and_unchanged_since(tmp_path):
    log = MeterLog(tmp_path)
    log.add(rec(T0, rh=60))
    log.add(rec(T0 + timedelta(seconds=30), rh=61))
    log.add(rec(T0 + timedelta(seconds=60), rh=61))
    s = log.status(T0 + timedelta(seconds=75))
    assert s["meter"]["rh_pct"] == 61
    assert s["meter"]["age_s"] == 15.0
    # 値が最後に変わったのは 30 秒目（60 秒目は同じ値）
    assert s["meter"]["unchanged_since"] == (T0 + timedelta(seconds=30)).isoformat()
    lines = (tmp_path / "meter_20261104.jsonl").read_text().splitlines()
    assert len(lines) == 3


def test_failed_read_keeps_last_good_value_and_counts_errors(tmp_path):
    log = MeterLog(tmp_path)
    log.add(rec(T0, rh=60))
    log.add(rec(T0 + timedelta(seconds=30), ok=False))
    log.add(rec(T0 + timedelta(seconds=60), ok=False))
    s = log.status(T0 + timedelta(seconds=61))
    assert s["meter"]["rh_pct"] == 60
    assert s["meter"]["age_s"] == 61.0  # 古くなっていくのが見える
    assert s["last_read_ok"] is False
    assert s["consecutive_errors"] == 2


def test_read_meter_marks_missing_values_as_failure():
    class Fake:
        def status(self, _):
            return Reply(
                http=200, status_code=190, body={}, message="device offline", roundtrip_s=0.1
            )

    r = read_meter(Fake(), "x")
    assert r["ok"] is False and r["error"] == "device offline" and r["body_keys"] == []


def test_read_meter_survives_network_error():
    class Broken:
        def status(self, _):
            raise TimeoutError("遅い")

    r = read_meter(Broken(), "x")
    assert r["ok"] is False and "TimeoutError" in r["error"]


def call_get_status(env: dict) -> dict:
    async def go():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "saibai.mcp_server"], env=env
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert sorted(t.name for t in tools.tools) == [
                "get_history",
                "get_status",
                "set_auto",
                "set_humidifier",
                "set_timer",
                "stop_all",
            ]
            assert (
                next(t for t in tools.tools if t.name == "get_status").annotations.read_only_hint
                is True
            )
            res = await s.call_tool("get_status", {})
            return res.structured_content or json.loads(res.content[0].text)

    return anyio.run(go)


def test_mcp_get_status_relays_receiver(tmp_path):
    log = MeterLog(tmp_path)
    log.add(rec(datetime.now(JST), rh=58))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(log))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = call_get_status({"SAIBAI_RECEIVER_PORT": str(httpd.server_port)})
    finally:
        httpd.shutdown()
    assert out["receiver"]["up"] is True
    assert out["meter"]["rh_pct"] == 58


def test_mcp_get_status_when_receiver_down():
    out = call_get_status({"SAIBAI_RECEIVER_PORT": "1"})  # 誰も待っていない口
    assert out["receiver"]["up"] is False
