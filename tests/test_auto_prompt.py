"""計画の依頼文のテスト（モデルは呼ばない）。"""

from datetime import datetime, timedelta

from saibai.auto import plan_prompt
from saibai.guard import DEMO, JST, REAL, Guard

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=JST)
TARGET = {"vpd_low_kpa": 0.4, "vpd_high_kpa": 0.8}
METER = {"read_at": NOW.isoformat(), "temp_c": 18.0, "rh_pct": 70, "vpd_kpa": 0.62}


def status_of(limits) -> dict:
    return {"meter": METER, "guard": Guard(limits).snapshot(NOW - timedelta(minutes=1))}


def test_demo_prompt_has_no_75_or_85():
    text = plan_prompt(NOW, status_of(DEMO), TARGET)
    assert "75%" not in text and "85%" not in text
    assert "続けて 60 分まで" in text and "1 日 120 分まで" in text


def test_real_prompt_keeps_75_and_85():
    text = plan_prompt(NOW, status_of(REAL), TARGET)
    assert "75% 以上では入れない" in text and "85% で切る" in text
