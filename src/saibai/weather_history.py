"""過去の外の天気（設計書 v0.8 §2・§6）：気象庁「過去の気象データ」の 1 時間値を取り、公開してよい形にする。

- 頁＝`stats/etrn/view/hourly_a1.php?prec_no=<県>&block_no=<地点>&year&month&day&view=` の `table#tablefix1`
  （見出し 2 行＋24 行・列＝時・降水量・気温・露点温度・蒸気圧・湿度・平均風速・風向・日照時間・降雪・積雪）。
- 地点の番号はリポジトリに書かない＝`private/weather_place.toml` の `history_prec_no`・`history_block_no`。
- 取るのは区間の分だけ・1 頁ごとに 1 秒あける・取った HTML は `run/jma_hourly/`（手元）に置いて取り直さない。
- 公開の形＝毎正時の気温・湿度・前 1 時間の降水・日照だけ（地点の名と番号は書かない・出典の 1 行）。
- **過去の予報は手に入らない**＝再生では「その先の実際の値から作った要約」を予報として渡す（本物より有利）。
  字の形は受け手の要約（weather.py の summary）にそろえる＝依頼文の読み方を変えない。
"""

import csv
import html as html_lib
import io
import os
import re
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from saibai.weather import AMEDAS_LABEL, FORECAST_LABEL, prompt_block  # noqa: F401

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
URL = (
    "https://www.data.jma.go.jp/stats/etrn/view/hourly_a1.php"
    "?prec_no={prec}&block_no={block}&year={y}&month={m}&day={d}&view="
)
SOURCE = "出典：気象庁ホームページ（過去の気象データ）を加工して作成"
COLS = ("at", "temp_c", "rh_pct", "precip_1h_mm", "sun_1h_h")
# 区間（data/ の記録と同じ）
SPANS = {
    "summer": (date(2026, 8, 15), date(2026, 9, 11)),
    "autumn": (date(2026, 10, 3), date(2026, 10, 30)),
}
# 天気の文の決め方（コード）：1 日の降水がこれ以上＝雨・日照がこれ以上＝晴れ・ほか＝くもり
RAIN_MM = 1.0
SUNNY_H = 6.0


def load_history_place() -> dict | None:
    path = Path(os.environ.get("SAIBAI_WEATHER_PLACE", ROOT / "private" / "weather_place.toml"))
    try:
        with path.open("rb") as f:
            p = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if "history_prec_no" in p and "history_block_no" in p:
        return {"prec": str(p["history_prec_no"]), "block": str(p["history_block_no"])}
    return None


# ---- 頁を読む ----
def _cell_value(text: str, zero_dash: bool = False) -> float | None:
    """欠け（`///`・`×`・空・`#` 付きの疑問値）は None。`)` 付き（準正常値）は値を使う。
    `--`（現象なし）は降水・日照では 0、ほかは None。"""
    t = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip().replace("　", "")
    if t in ("--",):
        return 0.0 if zero_dash else None
    if not t or "///" in t or "×" in t or "#" in t:
        return None
    m = re.match(r"^-?\d+(\.\d+)?", t)
    return float(m.group(0)) if m else None


def parse_hourly_html(page: str, day: date) -> list[dict]:
    """`table#tablefix1` の 24 行を、毎正時の dict にする（時 24＝翌日 0 時）。読めなければ空。"""
    m = re.search(r'<table[^>]*id="tablefix1"[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 9:
            continue  # 見出しの行
        try:
            hour = int(re.sub(r"<[^>]+>", "", tds[0]).strip())
        except ValueError:
            continue
        at = datetime(day.year, day.month, day.day, tzinfo=JST) + timedelta(hours=hour)
        rows.append(
            {
                "at": at,
                "precip_1h_mm": _cell_value(tds[1], zero_dash=True),
                "temp_c": _cell_value(tds[2]),
                "rh_pct": _cell_value(tds[5]),
                "sun_1h_h": _cell_value(tds[8], zero_dash=True),
            }
        )
    return rows


# ---- 取る（区間の分だけ・1 秒あける・取り直さない）----
def _http(url: str) -> str:
    import httpx

    r = httpx.get(url, timeout=20.0, headers={"User-Agent": "saibai-replay"})
    r.raise_for_status()
    return r.text


def fetch_span(
    start: date,
    end: date,
    place: dict,
    cache_dir: Path,
    fetch=_http,
    sleep=time.sleep,
    wait_s: float = 1.0,
) -> tuple[list[dict], int]:
    """区間の毎日の頁を読む。手元に在る頁は取り直さない。返り＝（毎正時の行・取った頁の数）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows, fetched = [], 0
    d = start
    while d <= end:
        f = cache_dir / f"hourly_{d:%Y%m%d}.html"
        if not f.exists():
            if fetched:
                sleep(wait_s)
            url = URL.format(prec=place["prec"], block=place["block"], y=d.year, m=d.month, d=d.day)
            f.write_text(fetch(url), encoding="utf-8")
            fetched += 1
        rows += parse_hourly_html(f.read_text(encoding="utf-8"), d)
        d += timedelta(days=1)
    return rows, fetched


# ---- 公開の形 ----
def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    return f"{v:g}"


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    buf.write(f"# {SOURCE}・近くのアメダスの毎正時の値（降水と日照は前 1 時間）・欠けは空欄\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLS)
    for r in sorted(rows, key=lambda r: r["at"]):
        w.writerow([_fmt(r.get(c)) for c in COLS])
    return buf.getvalue()


def public_path(season: str) -> Path:
    a, b = SPANS[season]
    return ROOT / "data" / f"weather_hourly_{a:%Y-%m-%d}_{b:%Y-%m-%d}.csv"


def load_hourly(path: Path | str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        lines = [x for x in f if not x.startswith("#")]
    for r in csv.DictReader(lines):
        out.append(
            {
                "at": datetime.fromisoformat(r["at"]).astimezone(JST),
                **{c: (float(r[c]) if r[c] != "" else None) for c in COLS[1:]},
            }
        )
    return out


def load_season(season: str) -> list[dict]:
    p = public_path(season)
    return load_hourly(p) if p.exists() else []


# ---- 再生に渡す天気（時計より前の値だけ＋先の実際の値から作った「予報」）----
def until(rows: list[dict], now: datetime) -> list[dict]:
    """時計より前（その時刻ちょうどまで）の毎正時の値。"""
    return [r for r in rows if r["at"] <= now]


def _day_rows(rows: list[dict], d: date) -> list[dict]:
    """その日の 1〜24 時の値（前 1 時間の降水と日照がその日の分になる）。"""
    d0 = datetime(d.year, d.month, d.day, tzinfo=JST)
    return [r for r in rows if d0 < r["at"] <= d0 + timedelta(days=1)]


def day_actual(rows: list[dict], d: date, after: datetime | None = None) -> dict | None:
    """その日の実際の値の要約（after を渡すとその時刻より後の分だけ＝今日の残り）。"""
    rs = _day_rows(rows, d)
    if after is not None:
        rs = [r for r in rs if r["at"] > after]
    if not rs:
        return None
    temps = [r["temp_c"] for r in rs if r["temp_c"] is not None]
    prec = [r["precip_1h_mm"] for r in rs if r["precip_1h_mm"] is not None]
    sun = [r["sun_1h_h"] for r in rs if r["sun_1h_h"] is not None]
    p, s = round(sum(prec), 1), round(sum(sun), 1)
    weather = "雨" if p >= RAIN_MM else ("晴れ" if s >= SUNNY_H * len(rs) / 24 else "くもり")
    return {
        "date": d.isoformat(),
        "weather": f"{weather}（降水 {p:g} mm・日照 {s:g} 時間）",
        **({"tmin": round(min(temps)), "tmax": round(max(temps))} if temps else {}),
    }


def replay_summary(rows: list[dict], now: datetime, n_days: int = 3) -> dict:
    """受け手の Weather.summary() と同じ形。予報＝その先の実際の値から作った要約（本物より有利）。"""
    from saibai.weather import day_text, now_text

    past = until(rows, now)
    obs = None
    if past and now - past[-1]["at"] <= timedelta(hours=2):
        o = past[-1]
        obs = {
            "at": o["at"].isoformat(timespec="minutes"),
            "temp_c": o["temp_c"],
            "rh_pct": o["rh_pct"],
            "precip_1h_mm": o["precip_1h_mm"],
            "precip_24h_mm": round(
                sum(
                    r["precip_1h_mm"] or 0 for r in past if r["at"] > o["at"] - timedelta(hours=24)
                ),
                1,
            ),
            "sun_1h_h": o["sun_1h_h"],
        }
    today = now.astimezone(JST).date()
    days = []
    for k in range(n_days):
        d = today + timedelta(days=k)
        a = day_actual(rows, d, after=now if k == 0 else None)
        if a:
            days.append((k, a))
    lines = []
    if obs:
        lines.append(now_text(obs))
    if days:
        lines.append(
            f"予報（{FORECAST_LABEL}・再生：その先の実際の値から作った要約＝予報が当たった場合）："
        )
        names = ("今日（この後）", "明日", "あさって")
        lines += ["- " + day_text(a, names[k]) for k, a in days]
    return {
        "ok": bool(obs or days),
        "obs": obs,
        "report_at": None,
        "text": "\n".join(lines),
        "reason": None if (obs or days) else "再生の区間の外",
    }


def hourly_csv_until(rows: list[dict], d: date, now: datetime) -> str:
    """置き場の /weather/YYYY-MM-DD.csv と同じ形（受け手の hourly_csv）。時計より前の行だけ。"""
    out = [",".join(COLS)]
    d0 = datetime(d.year, d.month, d.day, tzinfo=JST)
    for r in rows:
        if d0 <= r["at"] < d0 + timedelta(days=1) and r["at"] <= now:
            out.append(",".join(_fmt(r.get(c)) for c in COLS))
    return "\n".join(out) + "\n"


def main() -> None:
    """uv run saibai-weather-history：夏と秋の区間を取り（手元に在る頁は取り直さない）、data/ に公開の形で書く。"""
    place = load_history_place()
    if not place:
        raise SystemExit("private/weather_place.toml に history_prec_no・history_block_no が無い")
    cache = ROOT / "run" / "jma_hourly"
    for season, (a, b) in SPANS.items():
        rows, n = fetch_span(a, b, place, cache)
        p = public_path(season)
        p.write_text(to_csv(rows), encoding="utf-8")
        miss = sum(1 for r in rows if r["temp_c"] is None or r["rh_pct"] is None)
        print(f"{season}：{len(rows)} 行（取った頁 {n}・気温か湿度の欠け {miss}）→ {p.name}")
