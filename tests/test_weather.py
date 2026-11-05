"""外の天気のテスト（気象庁は叩かない＝形を写した作りの JSON）。地点の番号はリポジトリに書かない＝作りの番号。"""

import json
from datetime import datetime, timedelta

from saibai.auto import plan_prompt
from saibai.control import Controller, DummyPlug, NoSchedule
from saibai.guard import JST, REAL, Guard, Run
from saibai.weather import Weather, parse_forecast, prompt_block

NOW = datetime(2026, 11, 5, 14, 35, tzinfo=JST)
PLACE = {
    "amedas_station": "99999",
    "forecast_office": "000000",
    "forecast_area": "000010",
    "forecast_temp_point": "99998",
}
AMEDAS = {
    "99999": {
        "temp": [21.9, 0],
        "humidity": [60, 0],
        "precipitation1h": [0.0, 0],
        "precipitation24h": [0.0, 0],
        "sun1h": [0.3, 0],
    }
}
FC = [
    {
        "reportDatetime": "2026-11-05T11:00:00+09:00",
        "timeSeries": [
            {
                "timeDefines": [
                    "2026-11-05T11:00:00+09:00",
                    "2026-11-06T00:00:00+09:00",
                    "2026-11-07T00:00:00+09:00",
                ],
                "areas": [
                    {
                        "area": {"name": "区域", "code": "000010"},
                        "weathers": ["晴れ　後　くもり", "雨　時々　くもり", "晴れ"],
                    }
                ],
            },
            {
                "timeDefines": [
                    "2026-11-05T12:00:00+09:00",
                    "2026-11-05T18:00:00+09:00",
                    "2026-11-06T00:00:00+09:00",
                    "2026-11-06T06:00:00+09:00",
                    "2026-11-06T12:00:00+09:00",
                    "2026-11-06T18:00:00+09:00",
                ],
                "areas": [
                    {"area": {"code": "000010"}, "pops": ["10", "20", "60", "80", "90", "70"]}
                ],
            },
            {
                "timeDefines": [
                    "2026-11-05T09:00:00+09:00",
                    "2026-11-05T00:00:00+09:00",
                    "2026-11-06T00:00:00+09:00",
                    "2026-11-06T09:00:00+09:00",
                ],
                "areas": [{"area": {"code": "99998"}, "temps": ["23", "23", "15", "18"]}],
            },
        ],
    }
]


def fake_fetch(calls):
    def fetch(url):
        calls.append(url)
        if url.endswith("latest_time.txt"):
            return "2026-11-05T14:30:00+09:00"
        if "/amedas/" in url:
            return AMEDAS
        return FC

    return fetch


def weather(tmp_path, fetch=None, place=PLACE):
    w = Weather(place, tmp_path, lambda: NOW, fetch or fake_fetch([]))
    w.poll()
    return w


def test_forecast_parse_tomorrow_rain():
    fc = parse_forecast(FC, "000010", "99998")
    d = {x["date"]: x for x in fc["days"]}
    assert d["2026-11-06"]["weather"] == "雨 時々 くもり"
    assert d["2026-11-06"]["pops"][2] == ["12:00", 90]
    assert (d["2026-11-06"]["tmin"], d["2026-11-06"]["tmax"]) == (15, 18)
    assert "tmin" not in d["2026-11-05"] and d["2026-11-05"]["tmax"] == 23


def test_tomorrow_rain_is_in_plan_prompt_without_raw_json(tmp_path):
    w = weather(tmp_path)
    text = plan_prompt(
        NOW,
        {"meter": {}, "guard": Guard(REAL).snapshot(NOW)},
        {"vpd_low_kpa": 0.4, "vpd_high_kpa": 0.8},
        w.summary(),
    )
    assert "明日 11/6：雨 時々 くもり" in text and "12時 90%" in text
    assert "気温 21.9℃・湿度 60%" in text
    assert "runs を空にして" in text
    # 生の JSON（括弧・キーの名・地点の番号）は入れない
    for raw in ("{", "}", "weathers", "timeDefines", "99999", "000010"):
        assert raw not in text, raw


def test_no_weather_still_makes_a_prompt(tmp_path):
    def broken(url):
        raise TimeoutError("届かない")

    w = weather(tmp_path, fetch=broken)
    s = w.summary()
    assert not s["ok"]
    text = plan_prompt(
        NOW,
        {"meter": {}, "guard": Guard(REAL).snapshot(NOW)},
        {"vpd_low_kpa": 0.4, "vpd_high_kpa": 0.8},
        s,
    )
    assert "天気は無し" in text and "submit_plan" in text
    assert "天気は無し" in prompt_block(None)
    assert not w.status()["ok"]
    # 形が変わった時も止めない
    w2 = weather(
        tmp_path, fetch=lambda url: "2026-11-05T14:30:00+09:00" if url.endswith(".txt") else {}
    )
    assert not w2.summary()["ok"]
    # 地点の設定が無い時は取りに行かない
    calls = []
    weather(tmp_path, fetch=fake_fetch(calls), place=None)
    assert calls == []


def test_status_line_and_source(tmp_path):
    st = weather(tmp_path).status()
    assert st["ok"] and "明日 11/6：雨 時々 くもり・降水確率 最大 90%" in st["line"]
    assert "気象庁" in st["source"]


def test_forecast_fetched_once_per_issue(tmp_path):
    calls = []
    w = weather(tmp_path, fetch=fake_fetch(calls))
    w.poll()
    assert sum("/forecast/" in c for c in calls) == 1  # 次の発表（17 時）までは取り直さない


def test_read_back_after_restart(tmp_path):
    weather(tmp_path)
    w = Weather(PLACE, tmp_path, lambda: NOW + timedelta(minutes=5), lambda url: 1 / 0)
    assert w.summary()["ok"] and w.obs["temp_c"] == 21.9


def test_empty_plan_is_accepted_and_nothing_turns_on_and_weather_is_recorded(tmp_path):
    t = [NOW]
    ctrl = Controller(Guard(REAL), DummyPlug(), NoSchedule(), tmp_path, lambda: t[0])
    ctrl.start()
    for i in range(20):
        t[0] = NOW + timedelta(minutes=i)
        ctrl.observe(t[0], 60 + (i % 2) * 0.5, 15.0)
        ctrl.tick(check_plug=True)
    ctrl.weather = weather(tmp_path)
    s = ctrl.weather_summary()
    ev = [
        {"device": "予報 兵庫県", "metric": "明日の降水確率", "value": 90, "time": t[0].isoformat()}
    ]
    v = ctrl.submit(
        [], ev, "オート", extra={"reason": "明日は雨で外の湿度が上がる＝今日は控える", "weather": s}
    )
    assert v.ok
    for _ in range(30):
        t[0] += timedelta(minutes=1)
        ctrl.observe(t[0], 60.0 + (t[0].minute % 2) * 0.5, 15.0)
        ctrl.tick(check_plug=True)
    assert ctrl.plug.on is False and not ctrl.guard.on
    recs = [json.loads(x) for x in (tmp_path / "control_20261105.jsonl").read_text().splitlines()]
    plan = [r for r in recs if r.get("kind") == "action" and r["what"] == "計画"][-1]
    assert plan["accepted"] and plan["runs"] == [] and "明日 11/6：雨" in plan["weather"]["text"]
    assert "今日は控える" in plan["reason"]
    assert ctrl.status()["weather"]["ok"]


def test_empty_plan_keeps_other_sources_runs(tmp_path):
    g = Guard(REAL)
    from saibai.guard import Reading

    for i in range(20):
        g.observe(Reading(NOW - timedelta(minutes=19 - i), 60 + (i % 2) * 0.5))
    assert g.submit_plan([Run(NOW + timedelta(minutes=5), 10, "手動")], [{"x": 1}], NOW, "手動").ok
    assert g.submit_plan([], [{"x": 1}], NOW, "オート").ok
    assert [r.source for r in g.runs] == ["手動"]
