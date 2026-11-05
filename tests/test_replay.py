"""再生と評価（段 3・4）：先読みの禁止・実運転は記録そのまま・守りの層の上限・to_person で機械は動かない。"""

import json
import re
from datetime import datetime, timedelta

import pytest

from saibai import replay as rpl
from saibai.guard import MIN, REPLAY


class Probe:
    """起動のたびに、渡す物が再生の時計より前だけかを確かめる（API は呼ばない）。"""

    def __init__(self):
        self.seen = 0

    def wake(self, rp, now: datetime) -> dict:
        w = rp.world
        last = w.last_times(now)
        for k, v in last.items():
            assert v is not None, k
            assert datetime.fromisoformat(v) <= now, (k, v, now)
        assert datetime.fromisoformat(last["meter"]) < now
        files = w.files(now)
        for path, text in files["meter"].items():
            if path == "/weather/forecast.md":
                assert (
                    "その先の実際の値から作った要約" in text
                )  # 予報だけは「先の値から」と字で分かる
                continue
            for line in text.strip().splitlines()[1:]:
                assert datetime.fromisoformat(line.split(",")[0]) <= now, (path, line)
            if path.startswith("/meter/"):
                for line in text.strip().splitlines()[1:]:
                    assert datetime.fromisoformat(line.split(",")[0]) < now
        for text in files["actions"].values():
            for line in text.strip().splitlines():
                a = json.loads(line)
                assert datetime.fromisoformat(a["at"]) <= now
                if "end" in a:
                    assert datetime.fromisoformat(a["end"]) <= now
                assert a.get("target") != "hum_a"  # 実際の加湿器の入切は渡さない
        self.seen += 1
        # 今すぐ 20 分の計画（to_person つき）＝守りの層を通る
        rp.submit(
            {
                "runs": [{"start": now.isoformat(), "minutes": 20}],
                "reason": "試し",
                "to_person": "土の水分 600（03:00）が高いので水やりを控えてください",
                "evidence": [
                    {"device": "node_a", "metric": "vpd_kpa", "value": 0.9, "time": now.isoformat()}
                ],
            },
            now,
            "オート",
        )
        return {"wake": now.isoformat(), "cost_cents": 0}


@pytest.fixture(scope="module", params=["summer", "autumn"])
def probed(request):
    p = Probe()
    rp = rpl.Replay(request.param, "team", policy=p)
    res = rp.run()
    return request.param, p, rp, res


def test_every_wake_sees_only_the_past(probed):
    season, p, rp, res = probed
    assert p.seen == 56  # 7 日×8 回
    assert res["metrics"]["wakes"] == 56


def test_experience_seed_uses_only_records_before_the_window(probed):
    season, _, rp, _ = probed
    md = rp.world.experience_seed()
    first = rp.world.first
    for m in re.finditer(r"^\| (\d\d)/(\d\d) (\d\d):(\d\d) \|", md, re.M):
        t = first.replace(
            month=int(m.group(1)), day=int(m.group(2)), hour=int(m.group(3)), minute=int(m.group(4))
        )
        assert t + timedelta(minutes=60) < first, m.group(0)
    assert f"{first:%m/%d %H:%M} より前" in md


def test_guard_limits_hold_in_replay(probed):
    _, _, rp, _ = probed
    runs, start = [], None
    for t, _, _, _, on in rp.series:
        if on and start is None:
            start = t
        if not on and start is not None:
            runs.append((start, t))
            start = None
    assert runs
    for a, b in runs:
        assert b - a <= REPLAY.max_continuous + MIN
    days: dict = {}
    for a, b in runs:
        days[a.date()] = days.get(a.date(), timedelta(0)) + (b - a)
    assert max(days.values()) <= REPLAY.max_daily + 2 * MIN


def test_to_person_is_kept_but_moves_nothing(probed):
    _, _, rp, res = probed
    assert res["to_person"] and "水やりを控えて" in res["to_person"][0]["text"]
    assert res["water_hold_first"]["at"] == res["to_person"][0]["at"]
    # 同じ計画を to_person 抜きで出しても、守りの層の答えと運転は同じ
    a = rpl.Replay(rp.season, "team")
    b = rpl.Replay(rp.season, "team")
    t = a.room.now()
    base = {
        "runs": [{"start": t.isoformat(), "minutes": 20}],
        "reason": "x",
        "evidence": [
            {"device": "node_a", "metric": "vpd_kpa", "value": 0.9, "time": t.isoformat()}
        ],
    }
    ra = a.submit(base, t, "オート")
    rb = b.submit(base | {"to_person": "水やりを控える"}, t, "オート")
    assert ra == rb
    assert a.guard.runs == b.guard.runs


@pytest.mark.parametrize("season", ["summer", "autumn"])
def test_actual_replays_the_record_without_the_guard(season):
    rp = rpl.Replay(season, "actual")
    res = rp.run()
    S = rp.S
    for k, (_t, _temp, rh, _vpd, on) in enumerate(rp.series[::97]):
        i = rp.i0 + k * 97
        assert rh == pytest.approx(min(100.0, S.rh[i]), abs=0.02)
        assert on == bool(S.hum[i])
    assert res["metrics"]["plans"] == 0 and res["metrics"]["cost_cents"] == 0


def test_same_input_same_result():
    a = rpl.Replay("autumn", "threshold").run()
    b = rpl.Replay("autumn", "threshold").run()
    assert a["metrics"] == b["metrics"]


def test_water_hold_needs_water_hold_soil_and_a_number():
    assert rpl.water_hold_first([{"at": "x", "text": "湿度が高い"}]) is None
    assert rpl.water_hold_first([{"at": "x", "text": "水やりを控える"}]) is None  # 数値も土も無い
    hit = {"at": "y", "text": "土の水分 612（9/4 03:00）が高い＝水やりを 1 日待つ"}
    assert rpl.water_hold_first([{"at": "x", "text": "様子見"}, hit]) == hit
