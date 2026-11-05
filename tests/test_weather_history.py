"""過去の外の天気（段 1）：表の形を写した作り物の頁で読む・欠けを穴にする・地点の番号が公開のファイルに出ない。"""

import re
from datetime import date, datetime, timedelta

import pytest

from saibai import weather_history as wh
from saibai.weather import prompt_block

JST = wh.JST


def _row(h, prec, temp, rh, sun):
    tds = [str(h), prec, temp, "20.0", "25.0", rh, "1.0", "北", sun, "///", "///"]
    cells = "".join(f'<td class="data_0_0">{t}</td>' for t in tds)
    return f'<tr class="mtx" style="text-align:right;">{cells}</tr>'


def fake_page(rows=None) -> str:
    """気象庁の 1 時間値の頁の形（見出し 2 行＋24 行）を写した作り物。地点の名と番号も作り物。"""
    if rows is None:
        rows = [
            _row(h, "0.0", f"{20 + h * 0.1:.1f}", "80", "0.5" if 6 <= h <= 18 else "")
            for h in range(1, 25)
        ]
    head = (
        '<tr class="mtx"><th rowspan="2">時</th><th rowspan="2">降水量<br>(mm)</th><th rowspan="2">気温</th>'
        '<th rowspan="2">露点</th><th rowspan="2">蒸気圧</th><th rowspan="2">湿度</th><th colspan="2">風速・風向</th>'
        '<th rowspan="2">日照</th><th colspan="2">雪</th></tr>'
        '<tr class="mtx"><th>平均風速</th><th>風向</th><th>降雪</th><th>積雪</th></tr>'
    )
    return (
        "<html><body><h3>作り物の地点（どこか）　2026年9月4日（１時間ごとの値）</h3>"
        f'<table id="tablefix1" class="data2_s">{head}{"".join(rows)}</table></body></html>'
    )


def test_parse_24_rows_and_hour_24_is_next_midnight():
    rows = wh.parse_hourly_html(fake_page(), date(2026, 9, 4))
    assert len(rows) == 24
    assert rows[0]["at"] == datetime(2026, 9, 4, 1, tzinfo=JST)
    assert rows[-1]["at"] == datetime(2026, 9, 5, 0, tzinfo=JST)
    assert rows[0]["temp_c"] == pytest.approx(20.1)
    assert rows[0]["rh_pct"] == 80
    assert rows[0]["sun_1h_h"] is None  # 夜の日照は空＝穴
    assert rows[9]["sun_1h_h"] == 0.5


def test_missing_marks_become_holes():
    rs = [
        _row(1, "///", "///", "×", ""),
        _row(2, "--", "21.0 )", "", "--"),
        _row(3, "1.5", "21.5#", "90 ]", "0.0"),
    ]
    rows = wh.parse_hourly_html(fake_page(rs), date(2026, 9, 4))
    a, b, c = rows
    assert a["precip_1h_mm"] is None and a["temp_c"] is None and a["rh_pct"] is None
    assert b["precip_1h_mm"] == 0.0 and b["sun_1h_h"] == 0.0  # 「--」＝現象なし
    assert b["temp_c"] == 21.0  # 「)」＝準正常値は使う
    assert b["rh_pct"] is None
    assert c["temp_c"] is None  # 「#」＝疑問値は穴
    assert c["precip_1h_mm"] == 1.5


def test_no_table_reads_nothing():
    assert wh.parse_hourly_html("<html>形が変わった</html>", date(2026, 9, 4)) == []


def test_fetch_span_waits_and_does_not_refetch(tmp_path):
    got, slept = [], []

    def fetch(url):
        got.append(url)
        return fake_page()

    place = {"prec": "11", "block": "2222"}  # 作り物の番号
    rows, n = wh.fetch_span(
        date(2026, 9, 1), date(2026, 9, 3), place, tmp_path, fetch, slept.append
    )
    assert n == 3 and len(rows) == 72
    assert slept == [1.0, 1.0]  # 1 頁ごとに 1 秒（最初の前は待たない）
    assert "prec_no=11&block_no=2222&year=2026&month=9&day=1" in got[0]
    rows2, n2 = wh.fetch_span(
        date(2026, 9, 1), date(2026, 9, 3), place, tmp_path, fetch, slept.append
    )
    assert n2 == 0 and len(got) == 3 and rows2 == rows  # 取り直さない


def test_csv_round_trip_and_no_place_in_public_form(tmp_path):
    rows = wh.parse_hourly_html(fake_page(), date(2026, 9, 4))
    text = wh.to_csv(rows)
    assert text.startswith("# 出典：気象庁ホームページ")
    assert "作り物の地点" not in text and "2222" not in text
    p = tmp_path / "w.csv"
    p.write_text(text, encoding="utf-8")
    back = wh.load_hourly(p)
    assert [r["at"] for r in back] == [r["at"] for r in rows]
    assert back[0]["sun_1h_h"] is None and back[9]["sun_1h_h"] == 0.5


def test_public_files_hold_no_place_numbers():
    """data/ の公開のファイルに、地点の番号（private の設定）も頁の見出しの字も出ない。"""
    files = sorted(wh.ROOT.joinpath("data").glob("weather_hourly_*.csv"))
    if not files:
        pytest.skip("公開のファイルがまだ無い")
    place = wh.load_history_place()
    for f in files:
        text = f.read_text(encoding="utf-8")
        body = text.splitlines()[1:]
        assert body[0] == ",".join(wh.COLS)
        for line in body[1:]:
            assert re.fullmatch(r"[0-9T:+\-.,]*", line), line  # 数と時刻だけ
        if place:
            assert f"block_no={place['block']}" not in text
            for line in body[1:]:
                assert place["block"] not in line.split(",")[1:]


def _rows_two_days():
    rows = []
    t0 = datetime(2026, 9, 4, 1, tzinfo=JST)
    for i in range(72):
        t = t0 + timedelta(hours=i)
        rain = 3.0 if t.date() == date(2026, 9, 5) and 6 <= t.hour <= 9 else 0.0
        sun = (
            0.8
            if t.date() == date(2026, 9, 6) and 7 <= t.hour <= 17
            else (0.0 if 6 <= t.hour <= 18 else None)
        )
        rows.append(
            {"at": t, "temp_c": 25.0, "rh_pct": 80.0, "precip_1h_mm": rain, "sun_1h_h": sun}
        )
    return rows


def test_replay_summary_has_the_receiver_shape_and_no_past_beyond_clock():
    rows = _rows_two_days()
    now = datetime(2026, 9, 4, 15, 0, tzinfo=JST)
    s = wh.replay_summary(rows, now)
    assert s["ok"] and s["obs"]["at"] == "2026-09-04T15:00+09:00"
    assert "外（近くのアメダス・15:00）" in s["text"]
    assert "明日 9/5：雨（降水 12 mm" in s["text"]
    assert "あさって 9/6：晴れ" in s["text"]
    # 依頼文の読み方はそのまま（受け手の prompt_block を通る）
    assert "今日は入れない" in prompt_block(s)
    # 渡す 1 時間値は時計より前だけ
    csv_text = wh.hourly_csv_until(rows, date(2026, 9, 4), now)
    assert csv_text.strip().splitlines()[-1].startswith("2026-09-04T15:00")


def test_replay_summary_outside_span_says_no_weather():
    s = wh.replay_summary(_rows_two_days(), datetime(2026, 12, 1, tzinfo=JST))
    assert not s["ok"]
    assert "天気は無し" in prompt_block(s)
