"""段 4 の試し：定時・長さは AI を本物の計画のセッションで 1 回通す（v0.7）。

開始の時刻を「今から 10 分後」にして AutoRunner.ask_timer を呼び、制御役が分だけを決めて submit_plan を出すか、
開始の時刻を変えないか、守りの層（このプロセスの中の模擬）が受け付けるかを見る。
守りの層とプラグは模擬（DummyPlug）＝本物のプラグは動かさない。温湿度は受け手の今の記録、天気は受け手が取った記録。
使い方：uv run python spikes/managed_agents/timer_probe.py
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from saibai import agents as A  # noqa: E402
from saibai.auto import AutoRunner  # noqa: E402
from saibai.control import Controller, DummyPlug, NoSchedule  # noqa: E402
from saibai.guard import JST, REAL, Guard  # noqa: E402
from saibai.weather import Weather, load_place  # noqa: E402

load_dotenv(ROOT / ".env")
RUN = ROOT / "run"
WORK = ROOT / "spikes" / "logs" / "timer_run"


def now() -> datetime:
    return datetime.now(JST)


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    ids = A.load_ids()
    ctrl = Controller(Guard(REAL), DummyPlug(), NoSchedule(), WORK, now)
    # 受け手の直近 100 分の読みを流す（止まりの見分けに足りる長さ）
    since = now() - timedelta(minutes=100)
    for line in (RUN / f"meter_{now():%Y%m%d}.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("ok") and datetime.fromisoformat(r["read_at"]) >= since:
            ctrl.observe(datetime.fromisoformat(r["read_at"]), float(r["rh_pct"]), r.get("temp_c"))
    ctrl.start()
    # 天気は受け手が取った記録を読み戻すだけ（ここでは取りに行かない）
    ctrl.weather = Weather(load_place(), RUN, now, fetch=lambda url: 1 / 0)
    runner = AutoRunner(anthropic.Anthropic(), ids, ctrl, WORK, now, poll_s=5.0, meter_dir=RUN)
    at = (now() + timedelta(minutes=10)).replace(second=0, microsecond=0)
    print(f"開始 {at:%H:%M}・既定 60 分で制御役に聞く。天気＝{ctrl.weather.summary()['ok']}")
    res = runner.ask_timer(at, 60)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("守りの層の運転：", ctrl.guard.snapshot(now())["runs"])
    rec = json.loads((WORK / f"auto_{now():%Y%m%d}.jsonl").read_text().splitlines()[-1])
    print("止まった理由：", rec.get("plan_stop"), "・費用：", rec.get("plan_usage"))
    for p in rec["plans"]:
        print("-", p["ok"], p["text"], "\n  入力：", json.dumps(p["input"], ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
