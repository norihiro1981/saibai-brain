"""外の天気（設計書 v0.7 §2・§4）：気象庁のアメダスと予報を受け手が取り、要約をコードで作る（モデルは呼ばない）。

- 今の外＝近くのアメダス（10 分ごと）。予報＝兵庫県の予報の工場の区域（発表は 5・11・17 時＝発表の後に 1 回）。
- 地点の番号・区域の番号はリポジトリに書かない（兵庫県より細かい地名になる）＝`private/weather_place.toml`
  （commit しない）か `SAIBAI_WEATHER_PLACE` の指すファイル。無ければ「天気は無し」で今までどおり動く。
- 公式の API ではない（気象庁の画面の裏の JSON）＝形が変わる・取れない時も例外で止めず「取れていない」を返す。
- エージェントには生の JSON を渡さない。渡すのは数行の字（`prompt_block`）だけ。
- 出典：気象庁ホームページ（アメダス・天気予報）を加工して作成（公共データ利用規約 第 1.0 版）。
"""

import json
import os
import re
import threading
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
BASE = "https://www.jma.go.jp/bosai"
SOURCE = "出典：気象庁ホームページ（アメダス・天気予報）を加工して作成"
AMEDAS_LABEL = "近くのアメダス"
FORECAST_LABEL = "兵庫県の予報"
AMEDAS_EVERY = timedelta(minutes=10)
AMEDAS_TOO_OLD = timedelta(minutes=60)  # これより古い外の値は「今の外」と言わない
FORECAST_TOO_OLD = timedelta(hours=24)
PUBLISH_HOURS = (5, 11, 17)
PUBLISH_DELAY = timedelta(minutes=10)  # 発表の時刻から少し待って取る
DAY_NAMES = ("今日", "明日", "あさって")


def load_place() -> dict | None:
    """地点の設定（リポジトリの外）。無い・読めない時は None＝天気は無し。"""
    path = Path(os.environ.get("SAIBAI_WEATHER_PLACE", ROOT / "private" / "weather_place.toml"))
    try:
        with path.open("rb") as f:
            p = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    keys = ("amedas_station", "forecast_office", "forecast_area", "forecast_temp_point")
    return {k: str(p[k]) for k in keys} if all(k in p for k in keys) else None


def _clean(text: str) -> str:
    return re.sub(r"[\s　]+", " ", text).strip()


def _num(v):
    """アメダスの値は [値, 品質の印]。印が 0 以外か値が無い時は None。"""
    if isinstance(v, list) and v and v[0] is not None and (len(v) < 2 or v[1] == 0):
        return v[0]
    return None


# ---- 形をほどく（生の JSON → 小さな dict）----
def parse_amedas(map_json: dict, station: str, at: datetime) -> dict | None:
    s = map_json.get(station)
    if not isinstance(s, dict):
        return None
    obs = {
        "at": at.isoformat(timespec="minutes"),
        "temp_c": _num(s.get("temp")),
        "rh_pct": _num(s.get("humidity")),
        "precip_1h_mm": _num(s.get("precipitation1h")),
        "precip_24h_mm": _num(s.get("precipitation24h")),
        "sun_1h_h": _num(s.get("sun1h")),
    }
    return obs if obs["temp_c"] is not None or obs["rh_pct"] is not None else None


def parse_forecast(fc_json: list, area: str, temp_point: str) -> dict | None:
    """予報の JSON から、区域の 3 日の天気の文・6 時間ごとの降水確率・気温・週間の降水確率を取り出す。"""
    try:
        short = fc_json[0]
        report_at = short["reportDatetime"]
        days: dict[str, dict] = {}
        for ts in short["timeSeries"]:
            for a in ts["areas"]:
                code = a["area"]["code"]
                for i, t in enumerate(ts["timeDefines"]):
                    d = t[:10]
                    if code == area and "weathers" in a:
                        days.setdefault(d, {})["weather"] = _clean(a["weathers"][i])
                    elif code == area and "pops" in a and a["pops"][i] != "":
                        days.setdefault(d, {}).setdefault("pops", []).append(
                            [t[11:16], int(a["pops"][i])]
                        )
                    elif code == temp_point and "temps" in a and a["temps"][i] != "":
                        # 00:00 の項＝朝の最低・09:00 の項＝日中の最高（今日の朝が過ぎると両方に最高が入る）
                        key = "tmax" if t[11:13] == "09" else "tmin"
                        days.setdefault(d, {})[key] = int(a["temps"][i])
        for v in days.values():
            if "tmax" in v and v.get("tmin") == v["tmax"]:
                v.pop("tmin")  # 今日の分は最高が 2 つ入っている
        if len(fc_json) > 1:  # 週間：降水確率と信頼度・最低と最高
            week = fc_json[1]["timeSeries"]
            for ts in week:
                for a in ts["areas"]:
                    code = a["area"]["code"]
                    for i, t in enumerate(ts["timeDefines"]):
                        d = t[:10]
                        if code == area and "pops" in a and a["pops"][i] != "":
                            days.setdefault(d, {})["week_pop"] = int(a["pops"][i])
                            rel = (a.get("reliabilities") or [""] * (i + 1))[i]
                            if rel:
                                days[d]["reliability"] = rel
                        if code == temp_point and a.get("tempsMax", [""])[i : i + 1] not in (
                            [""],
                            [],
                        ):
                            days.setdefault(d, {}).setdefault("tmax", int(a["tempsMax"][i]))
                        if code == temp_point and a.get("tempsMin", [""])[i : i + 1] not in (
                            [""],
                            [],
                        ):
                            days.setdefault(d, {}).setdefault("tmin", int(a["tempsMin"][i]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not days:
        return None
    return {"report_at": report_at, "days": [{"date": d} | days[d] for d in sorted(days)]}


# ---- 字にする（エージェントと画面に渡すのはこれだけ）----
def _md(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


def day_text(day: dict, name: str = "") -> str:
    head = f"{name} {_md(day['date'])}" if name else _md(day["date"])
    parts = [day.get("weather") or "天気の文なし（週間）"]
    if day.get("pops"):
        parts.append("降水確率 " + "／".join(f"{int(t[:2])}時 {p}%" for t, p in day["pops"]))
    elif "week_pop" in day:
        rel = f"（信頼度 {day['reliability']}）" if day.get("reliability") else ""
        parts.append(f"降水確率 {day['week_pop']}%{rel}")
    temps = []
    if "tmin" in day:
        temps.append(f"最低 {day['tmin']}℃")
    if "tmax" in day:
        temps.append(f"最高 {day['tmax']}℃")
    if temps:
        parts.append("・".join(temps))
    return f"{head}：" + "・".join(parts)


def max_pop(day: dict) -> int | None:
    pops = [p for _, p in day.get("pops") or []]
    if pops:
        return max(pops)
    return day.get("week_pop")


def now_text(obs: dict) -> str:
    def f(v, unit, nd=1):
        return "—" if v is None else f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"

    return (
        f"外（{AMEDAS_LABEL}・{obs['at'][11:16]}）：気温 {f(obs['temp_c'], '℃')}・湿度 {f(obs['rh_pct'], '%')}・"
        f"前 1 時間の降水 {f(obs['precip_1h_mm'], ' mm')}・前 24 時間 {f(obs['precip_24h_mm'], ' mm')}"
    )


def forecast_lines(fc: dict, now: datetime, n: int = 3) -> list[str]:
    today = now.astimezone(JST).date().isoformat()
    days = [d for d in fc["days"] if d["date"] >= today][:n]
    out = []
    for d in days:
        k = (datetime.fromisoformat(d["date"]).date() - now.astimezone(JST).date()).days
        out.append("- " + day_text(d, DAY_NAMES[k] if 0 <= k < len(DAY_NAMES) else ""))
    return out


class Weather:
    """外の天気の今の要約を持つ。`fetch` は URL → JSON（テストでは模擬）。記録は run/weather_YYYYMMDD.jsonl。"""

    def __init__(self, place: dict | None, run_dir: Path, now_fn, fetch=None):
        self.place = place
        self.run_dir = run_dir
        self.now = now_fn
        self.fetch = fetch or self._http
        self.obs: dict | None = None
        self.forecast: dict | None = None
        self.forecast_fetched_at: datetime | None = None
        self.last_error: str | None = None if place else "地点の設定が無い"
        self.on_update = []  # 新しい値が入るたびに呼ぶ（置き場への写し）
        self._lock = threading.Lock()
        self._read_back()

    @staticmethod
    def _http(url: str):
        r = httpx.get(url, timeout=10.0, headers={"User-Agent": "saibai-receiver"})
        r.raise_for_status()
        return r.text if url.endswith(".txt") else r.json()

    def _log(self, rec: dict) -> None:
        now = self.now()
        with (self.run_dir / f"weather_{now:%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _read_back(self) -> None:
        """起こし直した時、今日と昨日の記録から最新の外の値と予報を読み戻す。"""
        now = self.now()
        for d in (now - timedelta(days=1), now):
            f = self.run_dir / f"weather_{d:%Y%m%d}.jsonl"
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "amedas":
                    self.obs = r["obs"]
                elif r.get("kind") == "forecast":
                    self.forecast = r["forecast"]
                    self.forecast_fetched_at = datetime.fromisoformat(r["fetched_at"])

    # ---- 取る ----
    def poll(self) -> None:
        """10 分ごとに呼ぶ。失敗しても例外にしない（last_error に残す）。"""
        if not self.place:
            return
        changed = False
        try:
            changed |= self._poll_amedas()
        except Exception as e:
            self.last_error = f"アメダス：{type(e).__name__}"
        try:
            if self._forecast_due():
                changed |= self._poll_forecast()
        except Exception as e:
            self.last_error = f"予報：{type(e).__name__}"
        if changed:
            for f in self.on_update:
                try:
                    f(self)
                except Exception:
                    pass

    def _poll_amedas(self) -> bool:
        latest = str(self.fetch(f"{BASE}/amedas/data/latest_time.txt")).strip()
        at = datetime.fromisoformat(latest).astimezone(JST)
        if self.obs and self.obs["at"] == at.isoformat(timespec="minutes"):
            return False
        m = self.fetch(f"{BASE}/amedas/data/map/{at:%Y%m%d%H%M}00.json")
        obs = parse_amedas(m, self.place["amedas_station"], at)
        if obs is None:
            self.last_error = "アメダス：地点の値が無い（形が変わったかもしれない）"
            return False
        with self._lock:
            self.obs = obs
            self.last_error = None
        self._log({"kind": "amedas", "obs": obs})
        return True

    def _forecast_due(self) -> bool:
        if self.forecast is None or self.forecast_fetched_at is None:
            return True
        now = self.now().astimezone(JST)
        # 直近の発表（＋待ち）より前に取った物なら、取り直す
        marks = [
            now.replace(hour=h, minute=0, second=0, microsecond=0) + PUBLISH_DELAY
            for h in PUBLISH_HOURS
        ]
        marks = [m for m in marks if m <= now] or [
            now.replace(hour=PUBLISH_HOURS[-1], minute=0, second=0, microsecond=0)
            - timedelta(days=1)
            + PUBLISH_DELAY
        ]
        return self.forecast_fetched_at < max(marks)

    def _poll_forecast(self) -> bool:
        p = self.place
        raw = self.fetch(f"{BASE}/forecast/data/forecast/{p['forecast_office']}.json")
        fc = parse_forecast(raw, p["forecast_area"], p["forecast_temp_point"])
        now = self.now()
        if fc is None:
            self.last_error = "予報：区域の値が無い（形が変わったかもしれない）"
            return False
        with self._lock:
            new = self.forecast is None or fc != self.forecast
            self.forecast, self.forecast_fetched_at = fc, now
            self.last_error = None
        self._log(
            {"kind": "forecast", "fetched_at": now.isoformat(timespec="seconds"), "forecast": fc}
        )
        return new

    # ---- 渡す ----
    def fresh_obs(self) -> dict | None:
        if not self.obs:
            return None
        if self.now() - datetime.fromisoformat(self.obs["at"]) > AMEDAS_TOO_OLD:
            return None
        return self.obs

    def fresh_forecast(self) -> dict | None:
        if not self.forecast or not self.forecast_fetched_at:
            return None
        if self.now() - self.forecast_fetched_at > FORECAST_TOO_OLD:
            return None
        return self.forecast

    def summary(self) -> dict:
        """計画に渡し、操作の記録に残す要約（字と少しの数）。"""
        now = self.now()
        obs, fc = self.fresh_obs(), self.fresh_forecast()
        lines = []
        if obs:
            lines.append(now_text(obs))
        if fc:
            lines.append(
                f"予報（{FORECAST_LABEL}・{fc['report_at'][5:16].replace('T', ' ')} 発表）："
            )
            lines += forecast_lines(fc, now)
        return {
            "ok": bool(obs or fc),
            "obs": obs,
            "report_at": fc["report_at"] if fc else None,
            "text": "\n".join(lines),
            "reason": None if (obs or fc) else (self.last_error or "まだ取れていない"),
        }

    def status(self) -> dict:
        """画面の 1 行：今の外と明日の予報。"""
        s = self.summary()
        now = self.now().astimezone(JST)
        parts = []
        if s["obs"]:
            o = s["obs"]
            parts.append(f"外（{AMEDAS_LABEL} {o['at'][11:16]}）{o['temp_c']}℃・{o['rh_pct']}%")
        fc = self.fresh_forecast()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        if fc:
            d = next((x for x in fc["days"] if x["date"] == tomorrow), None)
            if d:
                mp = max_pop(d)
                pop = "" if mp is None else f"・降水確率 最大 {mp}%"
                parts.append(f"明日 {_md(tomorrow)}：{d.get('weather') or '—'}{pop}")
        return {
            "ok": s["ok"],
            "line": "｜".join(parts),
            "reason": s["reason"],
            "error": self.last_error,
            "source": SOURCE,
        }

    # ---- 置き場へ写す物 ----
    def hourly_csv(self, day: datetime) -> str:
        """その日のアメダスの 1 時間値（毎正時の観測）。列＝時刻・気温・湿度・前 1 時間の降水・日照。"""
        cols = ("at", "temp_c", "rh_pct", "precip_1h_mm", "sun_1h_h")
        rows = [",".join(cols)]
        f = self.run_dir / f"weather_{day:%Y%m%d}.jsonl"
        if f.exists():
            seen = set()
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                o = r.get("obs") if r.get("kind") == "amedas" else None
                if o and o["at"][14:16] == "00" and o["at"] not in seen:
                    seen.add(o["at"])
                    rows.append(",".join("" if o.get(c) is None else str(o.get(c)) for c in cols))
        return "\n".join(rows) + "\n"

    def forecast_md(self) -> str:
        fc = self.fresh_forecast()
        if not fc:
            return (
                f"# 予報\n\n予報は取れていない（{self.last_error or '理由不明'}）。\n\n{SOURCE}\n"
            )
        lines = forecast_lines(fc, self.now(), n=7)
        return (
            f"# 予報（{FORECAST_LABEL}・{fc['report_at']} 発表・受け手が "
            f"{self.forecast_fetched_at:%m/%d %H:%M} に取った）\n\n"
            + "\n".join(lines)
            + f"\n\n{SOURCE}\n"
        )


def prompt_block(summary: dict | None) -> str:
    """計画の依頼文に入れる数行。天気が無ければそう書く（それでも計画は出す）。"""
    if not summary or not summary.get("ok"):
        why = (summary or {}).get("reason") or "取れていない"
        return f"外の天気：天気は無し（{why}）。今の部屋の値と経験だけで決めてください。\n"
    return (
        summary["text"]
        + "\n先の天気の読み方：今日だけを見ると入れたい時でも、先の天気（例：明日は雨で外の湿度が上がる）を読んで"
        "控える・短くする／長くするを選んでよい。「今日は入れない」も正しい計画＝runs を空にして理由を書く。"
        "天気を使ったら evidence に device「アメダス」か「予報 兵庫県」の行を入れる。"
        "ただし、この部屋に外の天気がどれだけ効くかは決めつけない＝経験に外の天気の行があれば引き、無ければ推測と書く。\n"
    )


def start_background(w: Weather, every: timedelta = AMEDAS_EVERY) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            w.poll()
            stop.wait(every.total_seconds())

    threading.Thread(target=loop, daemon=True).start()
    return stop
