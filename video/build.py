"""デモ動画（冒頭の実写の外）を、動きを付けて組み立てる。

使い方：
  uv run --group video python video/build.py              # 通しで作る（video/out/draft.mp4）
  uv run --group video python video/build.py --stills     # 各行の終わりの絵だけ撮る（見直し用・速い）
  uv run --group video python video/build.py --only ③④   # 一部の場面だけ（video/out/part.mp4）

- 台本＝video/script.md の各場面（## ⓪〜⑦）の「> 」の行。1 行＝字幕 1 枚＝声 1 つ。
- 声＝手元で動く AivisSpeech Engine（127.0.0.1:10101）。動いていなければ macOS の say（仮の声）。
- 絵＝場面ごとの HTML に、時刻 t を渡すと絵が決まる関数 render(t) を書き、Playwright で
  t を 1/30 秒ずつ進めて撮る（毎回同じ絵になる）。計器盤の場面は artifact/status.html に
  受け手の今の /status・/history（127.0.0.1 の読むだけの口）と試しの記録を流し込む。
  プラグも API も叩かない。
- 出来上がりと途中の物は video/out/・video/assets/（どちらも git に入らない）。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "video" / "assets" / "build"
OUT = ROOT / "video" / "out"
W, H = 1920, 1080
ORDER = "O⓪①②③④⑤⑥⑦⑧"  # O＝冒頭の実写
AIVIS = "http://127.0.0.1:10101"
VOICE_ID = 1937616896  # にせ（低めで落ち着いた講義調・ACML 1.0＝商用可・クレジット任意）
LEAD, GAP = 0.5, 0.45  # 場面の頭の間・行と行の間
TAIL = {"⓪": 1.0, "⑧": 4.5}  # 場面の後ろの間（ほかは 0.8）
# 冒頭の実写の並び（手元だけ＝git に入らない）。無ければ札で代える。形：
# {"parts": [{"clip": "norihiro_sample/IMG_0001.mov", "from": 0, "to": 30, "gain_db": 12},
#            {"black": 1.0}, {"title": 2.5, "turn": true}]}
# turn＝BGM がここで長調に移る（無ければ ① の頭）
OPENING = ROOT / "video" / "assets" / "opening.json"

# 読み上げだけ直す語（字幕はそのまま）
READ = {
    "Claude Code": "クロードコード",
    "Claude": "クロード",
    "Sonnet": "ソネット",
    "Opus": "オーパス",
    "Haiku": "ハイク",
    "SwitchBot": "スイッチボット",
    "VPD": "ブイピーディー",
    "API": "エーピーアイ",
    "Python": "パイソン",
    "AI": "エーアイ",
}
DIGIT = "ゼロ イチ ニー サン ヨン ゴー ロク ナナ ハチ キュウ".split()


# ---------------------------------------------------------------- 台本


def read_script() -> dict[str, list[str]]:
    scenes: dict[str, list[str]] = {}
    cur = None
    for line in (ROOT / "video" / "script.md").read_text().splitlines():
        m = re.match(r"## ([⓪①②③④⑤⑥⑦⑧])", line)
        if m:
            cur = m.group(1)
            scenes[cur] = []
        elif line.startswith("## "):
            cur = None
        elif cur and line.startswith("> "):
            scenes[cur].append(line[2:].strip())
    return scenes


# ---------------------------------------------------------------- 本物の記録


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8765{path}", timeout=5) as r:
        return json.load(r)


def jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout


def load_records() -> dict:
    timer_ai = next(
        d
        for d in jsonl(ROOT / "spikes/logs/timer_run/auto_20261105.jsonl")
        if d.get("kind") == "timer_ai"
    )
    auto_run = next(
        d for d in jsonl(ROOT / "run/auto_20261104.jsonl") if d.get("kind") == "auto_run"
    )
    rec = dict(
        status=get_json("/status"),
        history=get_json("/history?hours=24"),
        timer_ai=timer_ai,
        cost=dict(
            review=float(auto_run["review_usage"]["list_cost_cents"]),
            plan=float(auto_run["plan_usage"]["list_cost_cents"]),
        ),
        pytest=[
            x for x in run("uv", "run", "pytest", "-q", "tests/test_guard.py").splitlines() if x
        ],
        gitlog=run(
            "git", "log", "--reverse", "--format=%ad  %s", "--date=format:%m/%d %H:%M"
        ).splitlines(),
    )
    rec.update(day_records("2026-10-22"), replay=replay_table())
    (BUILD / "records.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def day_records(day: str) -> dict:
    """課題の絵：その日の部屋の VPD（10 分ごと・加湿器の入）と外の湿度（毎正時）。どちらも公開の記録。"""
    import csv

    room = [
        [
            int(r["at"][11:13]) + int(r["at"][14:16]) / 60,
            float(r["vpd_kpa"]),
            r["humidifier"] == "1",
        ]
        for r in csv.DictReader((ROOT / "results/replay_autumn_actual_series.csv").open())
        if r["at"].startswith(day)
    ]
    lines = (ROOT / "data/weather_hourly_2026-10-03_2026-10-30.csv").read_text().splitlines()
    out = [
        [int(r["at"][11:13]), float(r["rh_pct"])]
        for r in csv.DictReader(x for x in lines if not x.startswith("#"))
        if r["at"].startswith(day) and r["rh_pct"]
    ]
    return {"day_room": room, "day_out": out}


def replay_table() -> dict:
    """results/replay_table.md から、季節・通りごとの「帯の中の時間」を読む。"""
    tb: dict = {}
    for line in (ROOT / "results/replay_table.md").read_text().splitlines():
        cols = [x.strip() for x in line.strip("|").split("|")]
        if len(cols) > 3 and cols[0] in ("夏", "秋"):
            tb.setdefault(cols[0], {})[cols[1]] = float(cols[2].split()[0])
    return tb


# ---------------------------------------------------------------- 声


def ffprobe_dur(path: Path) -> float:
    out = run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)
    )
    return float(out)


def for_ear(text: str) -> str:
    for a, b in READ.items():
        text = text.replace(a, b)
    # 小数は「れいてん はちごーいち」のように一つずつ読ませる
    text = re.sub(
        r"(\d+)\.(\d+)",
        lambda m: (
            ("レイ" if m.group(1) == "0" else m.group(1))
            + "テン"
            + "".join(DIGIT[int(d)] for d in m.group(2))
        ),
        text,
    )
    return (
        text.replace("歳", "サイ")
        .replace("−", "マイナス")
        .replace("¢", "セント")
        .replace("「", "")
        .replace("」", "")
    )


def aivis_ok() -> bool:
    try:
        urllib.request.urlopen(f"{AIVIS}/version", timeout=2)
        return True
    except OSError:
        return False


def speak(text: str, voice: int, speed: float, use_aivis: bool) -> Path:
    """声を作る（同じ文・声・速さなら前に作った物を使う）。"""
    ear = for_ear(text)
    key = hashlib.sha1(f"{ear}|{voice}|{speed}|{use_aivis}".encode()).hexdigest()[:12]
    path = BUILD / "voice" / f"{key}.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if use_aivis:
        import httpx

        c = httpx.Client(base_url=AIVIS, timeout=120)
        q = c.post("/audio_query", params={"speaker": voice, "text": ear}).json()
        q.update(speedScale=speed, prePhonemeLength=0.05, postPhonemeLength=0.1)
        path.write_bytes(c.post("/synthesis", params={"speaker": voice}, json=q).content)
    else:
        aiff = path.with_suffix(".aiff")
        subprocess.run(["say", "-v", "Kyoko", "-r", "190", "-o", str(aiff), ear], check=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), str(path)], check=True
        )
    return path


# ---------------------------------------------------------------- 絵の共通の部品

FONTS = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;700'
    '&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)

CSS = """
* { transition: none !important; animation: none !important; caret-color: transparent; box-sizing: border-box; }
html, body { margin: 0; width: 1920px; height: 1080px; overflow: hidden !important; }
body.v { background: #0d1310; color: #e8efe9; font-family: "Zen Kaku Gothic New", "Hiragino Sans", sans-serif; }
#v-layer { position: fixed; inset: 0; z-index: 50; pointer-events: none;
  font-family: "Zen Kaku Gothic New", "Hiragino Sans", sans-serif; color: #e8efe9; }
#v-layer > * { position: absolute; }
.num { font-family: "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }
.kicker { font-size: 30px; letter-spacing: .24em; color: #f0b45a; font-weight: 700; }
.head { font-size: 58px; font-weight: 700; letter-spacing: .04em; line-height: 1.25; }
.chip { background: #1a241e; border: 2px solid #34443a; border-left: 8px solid #8fc09f; border-radius: 12px;
  padding: 14px 22px; font-size: 27px; line-height: 1.5; color: #e8efe9; box-shadow: 0 12px 30px rgba(0,0,0,.35); }
.chip.amber { border-left-color: #f0b45a; } .chip.red { border-left-color: #ef7d6f; }
.card { background: #172019; border: 2px solid #2e3a32; border-radius: 20px; padding: 26px 30px; }
.mut { color: #98a79d; }
.mark { background: linear-gradient(90deg, rgba(240,180,90,.55) var(--m, 0%), transparent var(--m, 0%)); border-radius: 4px; padding: 0 4px; }
.hl { outline: 4px solid #f0b45a !important; outline-offset: 5px; border-radius: 8px; box-shadow: 0 0 0 14px rgba(240,180,90,.22) !important; }
#v-subbg { position: fixed; left: 0; right: 0; bottom: 0; height: 150px; z-index: 98;
  background: linear-gradient(to top, rgba(5,8,6,.94), rgba(5,8,6,.7)); }
#v-sub { position: fixed; left: 0; right: 0; bottom: 0; height: 150px; z-index: 99; display: flex; align-items: center;
  justify-content: center; padding: 0 140px; text-align: center; color: #fff; text-wrap: balance;
  font: 700 42px/1.42 "Zen Kaku Gothic New", "Hiragino Sans", sans-serif; letter-spacing: .02em; }
#v-black { position: fixed; inset: 0; background: #000; z-index: 97; opacity: 0; }
"""

LIB = r"""
const cl = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const P = (t, a, d = .6) => d <= 0 ? (t >= a ? 1 : 0) : cl((t - a) / d);
const E = x => 1 - Math.pow(1 - cl(x), 3);
const EIO = x => { x = cl(x); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
const BK = x => { x = cl(x); const a = 1.70158, b = a + 1; return 1 + b * Math.pow(x - 1, 3) + a * Math.pow(x - 1, 2); };
const L = (a, b, k) => a + (b - a) * k;
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const C = window.CUES || [], T = window.TOTAL, D = window.D || {};
const c = i => C.length ? C[Math.min(i, C.length - 1)].s : 0;
const ce = i => C.length ? C[Math.min(i, C.length - 1)].e : 0;
// 行 i の中で語が読まれる頃（字の位置で割った見込み）
const w = (i, word) => { const q = C[i]; const k = Math.max(0, q.text.indexOf(word)); return q.s + (q.e - q.s) * k / q.text.length; };
function fade(el, k, dy = 26, s0 = 1) {
  if (typeof el === "string") el = $(el); if (!el) return;
  el.style.opacity = k; el.style.visibility = k <= .001 ? "hidden" : "visible";
  el.style.transform = `translateY(${(1 - k) * dy}px) scale(${L(s0, 1, k)})`;
}
function pos(el, x, y) { if (typeof el === "string") el = $(el); el.style.left = x + "px"; el.style.top = y + "px"; }
function center(sel) { const r = $(sel).getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
// 物を a から b へ飛ばす（k＝0〜1）。着いた後は b に居る
function fly(el, a, b, k, s0 = .55) {
  if (typeof el === "string") el = $(el);
  const e = EIO(k), r = el.getBoundingClientRect();
  pos(el, L(a.x - el.offsetWidth / 2, b.x, e), L(a.y - el.offsetHeight / 2, b.y, e));
  el.style.opacity = cl(k * 4); el.style.visibility = k <= 0 ? "hidden" : "visible";
  el.style.transform = `scale(${L(s0, 1, e)})`; el.style.transformOrigin = "0 0";
}
function subs(t) {
  const el = $("#v-sub"); let cur = null;
  for (const q of C) if (t >= q.s - .12 && t < q.e + GAP_S) cur = q;
  if (!cur) { el.style.opacity = 0; return; }
  if (el.dataset.k !== String(cur.s)) { el.textContent = cur.text; el.dataset.k = cur.s; }
  el.style.opacity = Math.min(P(t, cur.s - .12, .2), 1 - P(t, cur.e + GAP_S - .2, .2));
}
// 計器盤のカメラ（main を拡大した上で、画面の x・y に置く）
const Z = 1.3; let M = null;
function cam(x, y, s = 1) { M.style.transform = `translate(${x / Z}px, ${y / Z}px) scale(${s})`; }
function off(sel) { // カメラが (0,0,1) の時の、main の左上からの位置
  const m = M.getBoundingClientRect(), r = $(sel).getBoundingClientRect(); return { x: r.left - m.left, y: r.top - m.top };
}
// keys＝[[時刻, 値], ...]：各時刻から dur 秒かけて次の値へ
function track(t, keys, dur = .9) {
  let v = keys[0][1];
  for (let i = 1; i < keys.length; i++) { const [ti, vi] = keys[i]; v = L(v, vi, EIO(P(t, ti, dur))); }
  return v;
}
function hl(sel, on) { const e = $(sel); if (e) e.classList.toggle("hl", !!on); }
let __init = false;
// 動画を x 秒へ送り、絵が出るまで待つ
async function seek(v, x) {
  x = Math.max(0, Math.min(x, v.duration - .05));
  if (Math.abs(v.currentTime - x) < .004) return;
  await new Promise(r => { v.addEventListener("seeked", r, { once: true }); v.currentTime = x; });
}
window.__frame = async t => {
  if (!__init) { __init = true; M = $("main"); if (M) { M.style.transform = "none"; } if (typeof setup === "function") setup(); }
  subs(t); if (typeof render === "function") await render(t);
  $("#v-subbg").style.opacity = C.length ? 1 : 0;
};
"""


def esc(s: str) -> str:
    return html.escape(s)


def wrap(head: str, body: str, js: str, cues: list[dict], total: float, data: dict) -> str:
    """画面の HTML に、字幕・動きの部品・場面の render を足す。"""
    return (
        head
        + f"<style>{CSS}</style>{body}"
        + '<div id="v-black"></div><div id="v-subbg"></div><div id="v-sub"></div>'
        + "<script>{"
        + f"window.CUES = {json.dumps(cues, ensure_ascii=False)}; window.TOTAL = {total:.3f}; "
        + f"window.D = {json.dumps(data, ensure_ascii=False)}; const GAP_S = {GAP};"
        + LIB
        + js
        + "}</script>"
    )


def plain(body: str, js: str, cues, total, data) -> str:
    head = f'<!DOCTYPE html><meta charset="utf-8">{FONTS}<body class="v">'
    return wrap(head, f'<div id="v-layer">{body}</div>', js, cues, total, data)


DASH_CSS = """
body { background: #0d1310 !important; padding: 0 !important; }
main { position: absolute; left: 0; top: 0; zoom: 1.3; width: 812px; max-width: 812px !important; margin: 0 !important;
  padding: 22px 26px 40px; background: #f3f5f1; border-radius: 18px; box-shadow: 0 30px 90px rgba(0,0,0,.55);
  transform-origin: 0 0; }
"""


def shim(status: dict, history: dict) -> str:
    """計器盤の claude.use("mcp") の代わり＝記録を返すだけ（何も動かさない）。"""
    data = json.dumps({"get_status": status, "get_history": history}, ensure_ascii=False)
    return f"""<script>
const __DATA = {data};
window.claude = {{ use: async () => ({{
  watchTool(server, tool, input, cb) {{ setTimeout(() => cb({{ type: "data", result: {{ payload: __DATA[tool] }} }}), 0); return () => {{}}; }},
  callTool: async () => ({{ payload: {{}} }}),
  invalidate: async () => {{}},
}}) }};
</script>"""


def dash(rec: dict, body: str, js: str, cues, total, data, patch=None) -> str:
    status = json.loads(json.dumps(rec["status"]))
    if patch:
        patch(status)
    src = (ROOT / "artifact" / "status.html").read_text()
    src = src.replace(
        '<meta charset="utf-8">',
        '<meta charset="utf-8">' + shim(status, rec["history"]) + f"<style>{DASH_CSS}</style>",
        1,
    )
    src = src.replace("<main>", '<main data-v="1">', 1)
    src += '<script>document.documentElement.dataset.theme = "light";</script>'
    return wrap(src, f'<div id="v-layer">{body}</div>', js, cues, total, data)


# ---------------------------------------------------------------- 場面


def scene_pages(rec: dict):
    """場面ごとに (HTML を作る関数) を返す。関数は (cues, total) を受ける。"""
    t_ai = rec["timer_ai"]["plans"][0]["input"]
    reason = t_ai["reason"]
    # 計器盤に出す理由は、AI の書き崩れ（「0.0 1.298」）のある文を除いた頭の 2 文
    sentences = [s + "。" for s in reason.split("。") if s and "0.0 1.298" not in s]
    short_reason = "".join(sentences[:2]) + "…"

    def timer_patch(s):
        s["meter"].update(
            temp_c=22.1, rh_pct=68, vpd_kpa=0.851, read_at="2026-11-05T14:54:10+09:00", age_s=20
        )
        s["timer"] = {
            "on": True,
            "start": "15:04",
            "minutes": 60,
            "ai_minutes": True,
            "last": {
                "date": "11/5",
                "start": "15:04",
                "minutes": 20,
                "by": "AI の分",
                "reason": short_reason,
                "ok": True,
            },
        }

    # ⓪ 課題：外の天気で崩れる（10/22 の本物の記録・数字は出さない） ------------------
    def p0(cues, total):
        body = """
<div id="k" class="kicker" style="left:160px;top:80px">課題</div>
<div id="h" class="head" style="left:160px;top:124px">外の天気で、すぐに崩れる</div>
<div id="cap" class="mut" style="left:1400px;top:150px;font-size:24px">秋の記録・ある 1 日</div>
<svg id="ch" width="1600" height="600" style="left:160px;top:240px;overflow:visible">
  <rect id="band" x="0" width="1600" fill="rgba(143,192,159,.16)"/>
  <text id="bandl" x="10" fill="#8fc09f" font-size="24">狙いの帯</text>
  <path id="red" fill="rgba(239,125,111,.45)"/>
  <g id="hum"></g>
  <path id="out" fill="none" stroke="#7fb3d4" stroke-width="4" stroke-dasharray="10 8"/>
  <path id="room" fill="none" stroke="#e8efe9" stroke-width="5"/>
  <g id="ax" fill="#98a79d" font-size="22"></g>
  <text id="outl" fill="#7fb3d4" font-size="26">外の湿度</text>
  <text id="rooml" fill="#e8efe9" font-size="26">部屋の空気の乾き（VPD）</text>
</svg>
<div id="hand" class="chip amber" style="width:430px;font-size:26px">人が気づいて入れたのは、午後</div>
<div id="burd" style="left:1400px;top:196px;font-size:30px;color:#ef7d6f;font-weight:700">帯の外＝作物に負担</div>
"""
        js = """
const R = D.room, O = D.out, H0 = 4, H1 = 24, WX = 1600, HY = 600;
const X = h => (h - H0) / (H1 - H0) * WX;
const YV = v => HY - (v - .3) / (1.4 - .3) * HY;      // 部屋の VPD
const YO = r => HY - (r - 40) / (100 - 40) * HY;     // 外の湿度
function setup() {
  $("#band").setAttribute("y", YV(.8)); $("#band").setAttribute("height", YV(.4) - YV(.8));
  $("#bandl").setAttribute("y", YV(.4) - 12);
  $("#ax").innerHTML = [6, 9, 12, 15, 18, 21].map(h => `<text x="${X(h) - 18}" y="${HY + 36}">${h} 時</text>`).join("");
  $("#hum").innerHTML = R.filter(p => p[2]).map(p => `<rect x="${X(p[0])}" y="0" width="${WX / (H1 - H0) / 6 + 1}" height="${HY}" fill="rgba(127,179,212,.18)" opacity="0"/>`).join("");
}
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cap", E(P(t, .5, .6)));
  fade("#ch", E(P(t, .3, .6)), 0);
  const h = L(H0, H1, EIO(P(t, c(0) + .2, ce(1) - c(0))));
  const rp = R.filter(p => p[0] >= H0 && p[0] <= h), op = O.filter(p => p[0] >= H0 && p[0] <= h);
  $("#room").setAttribute("d", rp.map((p, i) => (i ? "L" : "M") + X(p[0]) + "," + YV(p[1])).join(""));
  $("#out").setAttribute("d", op.map((p, i) => (i ? "L" : "M") + X(p[0]) + "," + YO(p[1])).join(""));
  const top = rp.map(p => [X(p[0]), YV(Math.max(p[1], .8))]);
  $("#red").setAttribute("d", top.length ? "M" + top[0][0] + "," + YV(.8) + top.map(q => "L" + q[0] + "," + q[1]).join("") + "L" + top[top.length - 1][0] + "," + YV(.8) + "Z" : "");
  const a = $("#outl"); a.setAttribute("x", X(6.3)); a.setAttribute("y", YO(93) - 16); a.style.opacity = E(P(t, c(0) + .8, .5));
  const b = $("#rooml"); b.setAttribute("x", X(4.2)); b.setAttribute("y", YV(.66) + 44); b.style.opacity = E(P(t, c(0) + .8, .5));
  $$("#hum rect").forEach((r, i) => { const on = R.filter(p => p[2])[i][0] <= h; r.setAttribute("opacity", on ? 1 : 0); });
  fade("#burd", E(P(t, w(1, "負担") - .2, .6)), 12);
  const first = R.find(p => p[2]);
  const tn = w(2, "人のカン") - .3;
  pos("#hand", 160 + X(first[0]) - 215, 240 + YV(1.3) - 90);
  fade("#hand", BK(P(t, tn, .6)), 16, .9);
  $("#v-black").style.opacity = P(t, T - .9, .8);
}"""
        return plain(body, js, cues, total, {"room": rec["day_room"], "out": rec["day_out"]})

    # 冒頭の札（実写が無い時） ----------------------------------------------------
    def p_open(cues, total):
        body = """<div style="left:0;top:420px;width:1920px;text-align:center;font-size:44px;color:#e4ebe5">冒頭：夜の実写（作り手）</div>"""
        return plain(body, "function render(t){}", cues, total, {})

    def p_title(cues, total):
        body = """<div id="ttl" style="left:0;top:400px;width:1920px;text-align:center">
<div style="font-size:104px;font-weight:700;letter-spacing:.14em">栽培室の頭脳</div></div>"""
        js = 'function render(t){ fade("#ttl", E(P(t, .2, .8)) * (1 - P(t, T - .6, .5)), 12); }'
        return plain(body, js, cues, total, {})

    def p_black(cues, total):
        return plain("", "function render(t){}", cues, total, {})

    # ① 種明かし ------------------------------------------------------------
    def p1(cues, total):
        body = """
<div id="who" class="kicker" style="left:1200px;top:230px">決めているのは</div>
<div id="b0" class="chip" style="left:1200px;top:290px;width:620px"><b style="font-size:34px">Sonnet 5.5</b>　分析役</div>
<div id="b1" class="chip amber" style="left:1200px;top:400px;width:620px"><b style="font-size:34px">Opus 5.5</b>　制御役</div>
<div id="b2" class="chip" style="left:1200px;top:510px;width:620px"><b style="font-size:34px">Haiku 5.5</b>　専門家</div>
<div id="nt" class="mut" style="left:1200px;top:630px;font-size:24px">人ではなく、3 体の Claude のエージェント</div>
<div id="note" class="mut" style="left:1200px;top:60px;font-size:20px">試し：本番は Artifact で加湿器が動く実写に替える</div>
"""
        js = """
let MW;
function setup() { MW = M.getBoundingClientRect().width; }
function render(t) {
  const s = L(.8, 1, E(P(t, 0, 1.4)));
  const mv = EIO(P(t, c(0) + .4, 1.1));
  cam(L((1920 - MW * s) / 2, 70, mv), L(60, 40, mv), s);
  M.style.opacity = E(P(t, 0, .6));
  fade("#note", E(P(t, .2, .5)));
  fade("#who", E(P(t, w(0, "人では") - .2, .5)));
  for (let i = 0; i < 3; i++) fade("#b" + i, BK(P(t, w(0, "3 体") + i * .22, .55)), 30, .9);
  fade("#nt", E(P(t, w(0, "Claude"), .6)));
  hl(".gauge.vpd", t > w(1, "部屋の記録") - .1 && t < w(1, "天気"));
  hl("#weather", t > w(1, "天気") - .1 && t < ce(1) + .3);
}"""
        return dash(rec, body, js, cues, total, {})

    # ② 目指すところ ------------------------------------------------------------
    def p2(cues, total):
        body = """
<div id="k" class="kicker" style="left:160px;top:120px">目指すところ</div>
<div id="g0" class="card" style="left:160px;top:250px;width:760px;height:460px;padding:44px 48px">
  <div class="num" style="font-size:40px;color:#f0b45a">1</div>
  <div style="font-size:52px;font-weight:700;line-height:1.35;margin-top:10px">天気を先読みして、<br>計画を立てる</div>
  <div class="mut" style="font-size:24px;margin-top:26px">予報と部屋の記録から、いつ・何分入れるかを決める</div>
</div>
<div id="g1" class="card" style="left:1000px;top:250px;width:760px;height:460px;padding:44px 48px">
  <div class="num" style="font-size:40px;color:#f0b45a">2</div>
  <div style="font-size:52px;font-weight:700;line-height:1.35;margin-top:10px">人のカンをデータにして、<br>生産を安定させる</div>
  <div class="mut" style="font-size:24px;margin-top:26px">人が押した 1 回も、効き目の記録として残し、育てる</div>
</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5)));
  fade("#g0", BK(P(t, w(0, "天気") - .2, .7)), 40, .94);
  fade("#g1", BK(P(t, w(1, "カン") - .3, .7)), 40, .94);
  $("#g0").style.borderColor = t < c(1) && t > w(0, "天気") ? "#f0b45a" : "#2e3a32";
  $("#g1").style.borderColor = t > w(1, "カン") ? "#f0b45a" : "#2e3a32";
}"""
        return plain(body, js, cues, total, {})

    # ③ 先読みして決める（11/5 15:04 の本物の答え） ---------------------------------
    def p3(cues, total):
        X = 1180
        body = f"""
<div id="k" class="kicker" style="left:{X}px;top:60px">目指すところ 1</div>
<div id="h" class="head" style="left:{X}px;top:100px;font-size:50px">先を読んで決める</div>
<div id="cap" class="mut" style="left:{X}px;top:186px;font-size:24px">11/5 15:04 の回（本物の答え）</div>
<div id="c1" class="chip" style="width:680px">14:54 の VPD <b class="num">0.851</b> kPa<span class="mut">（帯 0.4〜0.8 の上）</span></div>
<div id="c2" class="chip" style="width:680px">経験：秋の傾向 <b class="num">26</b> 回・15 分で <b class="num">−0.238</b></div>
<div id="barbox" style="left:{X}px;top:470px;width:680px;height:96px">
  <div id="bar" style="position:absolute;left:0;top:0;height:96px;width:680px;border-radius:12px;background:#2a3a30;border:2px solid #4a5c50"></div>
  <div id="barl" style="position:absolute;left:24px;top:22px;font-size:34px;font-weight:700;white-space:nowrap">定時の 60 分</div>
</div>
<div id="c3" class="chip" style="width:680px">夜は雨の予報<span class="mut">（18 時の降水確率 60%）</span> → 長くはしない</div>
<div id="c4" class="chip amber" style="width:680px;font-size:26px">「外の天気がこの部屋にどれだけ効くかの行は経験に無いので、<span id="mk" class="mark">これは推測</span>」</div>
"""
        js = f"""
const X = {X};
let OT, OG, OE;
function setup() {{
  OT = off(".controls .mode:nth-of-type(2)"); OG = off("#gauges"); OE = off(".exp");
}}
function render(t) {{
  const yT = 330 - OT.y, yG = 80 - OG.y, yE = 300 - OE.y;
  const tv = w(1, "VPD") - .7, ta = w(1, "秋の") - .6, td = w(1, "だから") - .5;
  cam(70, track(t, [[0, yT], [tv, yG], [ta, yE], [td, yT]]));
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cap", E(P(t, .6, .6)));
  hl(".controls .mode:nth-of-type(2)", t > .3 && t < tv);
  hl("label[for=timerAi]", t > w(0, "何分") && t < tv);
  hl(".gauge.vpd", t > w(1, "VPD") - .2 && t < ta);
  hl("#expTable tbody tr:nth-child(2)", t > w(1, "26") - .3 && t < td);
  hl("#timerWhy", t > td + .9);
  fly("#c1", center(".gauge.vpd"), {{ x: X, y: 230 }}, P(t, w(1, "0.851"), 1.0));
  fly("#c2", center("#expTable tbody tr:nth-child(2)"), {{ x: X, y: 350 }}, P(t, w(1, "26"), 1.0));
  fade("#barbox", E(P(t, td - .2, .5)));
  const sh = EIO(P(t, w(1, "20"), .9));
  $("#bar").style.width = L(680, 680 * 20 / 60, sh) + "px";
  $("#bar").style.background = sh > 0 ? `rgba(240,180,90,${{L(.15, .9, sh)}})` : "#2a3a30";
  $("#barl").textContent = sh > .5 ? "AI の分：20 分" : "定時の 60 分";
  $("#barl").style.left = (sh > .5 ? 680 * 20 / 60 + 24 : 24) + "px";
  fade("#c3", E(P(t, w(2, "雨") - .3, .6))); pos("#c3", X, 600);
  fade("#c4", E(P(t, w(2, "経験に無い") - .3, .6))); pos("#c4", X, 720);
  $("#mk").style.setProperty("--m", (100 * E(P(t, w(2, "推測") - .1, .7))) + "%");
}}"""
        return dash(rec, body, js, cues, total, {}, patch=timer_patch)

    # ④ カンがデータになり、育つ（再生の分析役の本物の書き直し） -------------------------
    def p4(cues, total):
        lock = (
            '<svg width="28" height="32" viewBox="0 0 30 34" style="vertical-align:-5px;margin-right:8px">'
            '<rect x="3" y="14" width="24" height="18" rx="4" fill="#f0b45a"/>'
            '<path d="M8 14V10a7 7 0 0 1 14 0v4" stroke="#f0b45a" stroke-width="4" fill="none"/></svg>'
        )
        rows = [
            ("11/5 13:15", "手動・入 60 分", "60 分で 0.925 → 0.793", True),
            ("10/21 15:03", "オート・入 30 分", "15 分で −0.154", False),
            ("10/22 11:30", "オート・入 15 分", "15 分で −0.128", False),
            ("10/22 18:05", "オート・入 60 分", "15 分で −0.093", False),
            ("10/23 12:05", "オート・入 60 分", "15 分で −0.137", False),
            ("10/23 15:05", "オート・入 90 分", "15 分で −0.137", False),
        ]
        rh = "".join(
            f"<div id=\"r{i}\" style=\"position:absolute;left:36px;top:{150 + i * 50}px;width:980px;font:26px/1.6 'IBM Plex Mono','Zen Kaku Gothic New',monospace;"
            f'color:{"#f0b45a" if hum else "#cfe3d4"}">{a}　{b}　<span class="mut">{c}</span></div>'
            for i, (a, b, c, hum) in enumerate(rows)
        )
        body = f"""
<div id="k" class="kicker" style="left:120px;top:60px">目指すところ 2</div>
<div id="h" class="head" style="left:120px;top:100px;font-size:50px">カンがデータになり、経験が育つ</div>
<div id="book" class="card" style="left:120px;top:200px;width:1080px;height:700px;padding:0">
  <div style="position:absolute;left:36px;top:26px;font-size:30px;font-weight:700">経験<span class="mut" style="font-size:22px;font-weight:400;margin-left:14px">効き目の表（入れる前と後の動き）</span></div>
  <div style="position:absolute;left:36px;top:84px;width:1000px;border-top:2px solid #2e3a32"></div>
  {rh}
  <div id="trend" style="position:absolute;left:36px;top:470px;font-size:30px">秋の傾向　<b id="tn" class="num" style="font-size:52px;color:#f0b45a">24</b> 回</div>
  <div id="wx" style="position:absolute;left:36px;top:560px;font-size:28px">外の天気の節　外と部屋の湿度の相関 r ＝ <b id="rv" class="num" style="color:#7fb3d4">0.23</b>
    <span id="unk" style="margin-left:18px;border:3px solid #ef7d6f;color:#ef7d6f;border-radius:8px;padding:2px 12px;font-weight:700;display:inline-block">まだ分からない</span></div>
  <div id="fix" style="position:absolute;left:36px;top:630px;font-size:26px">10/23 14:00 の回　60 分：<span id="old" style="position:relative">途中<span id="strike" style="position:absolute;left:0;top:52%;height:4px;background:#ef7d6f"></span></span>
    <span id="new" style="color:#6cc28b;font-weight:700;margin-left:12px">→ −0.026（確定）</span></div>
</div>
<div id="hu" class="chip amber" style="width:460px;font-size:26px">人がスイッチを押した 1 回</div>
<div id="an" class="card" style="left:1280px;top:200px;width:520px;height:250px">
  <div class="mut" style="font-size:22px">振り返り・3 時間ごと</div>
  <div style="font-size:46px;font-weight:700;margin:4px 0">分析役</div>
  <div class="num" style="font-size:28px;color:#8fc09f">Sonnet 5.5</div>
  <div style="position:absolute;left:30px;top:190px;font-size:24px;color:#6cc28b">経験を書ける ・ 書き直し <b id="vn" class="num">0</b> 回</div>
</div>
<div id="ct" class="card" style="left:1280px;top:560px;width:520px;height:260px">
  <div class="mut" style="font-size:22px">計画</div>
  <div style="font-size:46px;font-weight:700;margin:4px 0">制御役</div>
  <div class="num" style="font-size:28px;color:#8fc09f">Opus 5.5</div>
  <div style="position:absolute;left:30px;top:204px;font-size:24px;color:#f0b45a">{lock}読むだけ（書き換えられない）</div>
</div>
<div id="rd" style="left:1200px;top:670px;width:80px;border-top:4px dashed #f0b45a"></div>
<div id="nt" class="mut" style="left:120px;top:910px;font-size:20px">11/5 の行＝本物の手動の操作 ・ ほか＝秋の記録で再生した 4 日間（10/20〜10/23）に、分析役が自分で書いた行</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#book", E(P(t, .3, .6)), 20);
  fade("#nt", E(P(t, .8, .6)));
  // 人の 1 回が表の 1 行になる
  const t0 = w(0, "1 回") - .2;
  const r0 = $("#r0").getBoundingClientRect();
  fly("#hu", { x: 1540, y: 300 }, { x: r0.left, y: r0.top - 6 }, P(t, t0, 1.2));
  $("#hu").style.opacity = cl(P(t, t0, .2)) * (1 - P(t, t0 + 1.2, .3));
  fade("#r0", E(P(t, t0 + 1.1, .4)), 0);
  // 分析役が書き足していく
  fade("#an", E(P(t, w(1, "Sonnet") - .3, .5)), 20);
  const t2 = c(2), d2 = ce(2) - c(2);
  for (let i = 1; i < 6; i++) fade("#r" + i, E(P(t, t2 + (i - 1) * d2 * .14, .35)), 0);
  const g = EIO(P(t, t2, d2 * .75));
  fade("#trend", E(P(t, t2 - .2, .4)), 0);
  $("#tn").textContent = Math.round(L(24, 34, g));
  $("#vn").textContent = Math.round(L(0, 32, EIO(P(t, w(1, "振り返り"), ce(2) - w(1, "振り返り")))));
  $("#an").style.borderColor = t > w(1, "Sonnet") && t < ce(3) ? "#6cc28b" : "#2e3a32";
  const tw = w(2, "外の天気");
  fade("#wx", E(P(t, tw - .3, .5)), 0);
  const rs = [.23, .46, .49, .47, .58], k = P(t, tw, 2.4);
  $("#rv").textContent = rs[Math.min(4, Math.floor(k * 4.999))].toFixed(2);
  const tu = w(2, "まだ分からない");
  const su = $("#unk"); su.style.opacity = cl(P(t, tu - .2, .2)); su.style.transform = `rotate(-6deg) scale(${L(2.2, 1, BK(P(t, tu - .2, .45)))})`;
  // 書き間違いを直す
  const tf = c(3);
  fade("#fix", E(P(t, tf - .1, .4)), 0);
  $("#strike").style.width = (100 * EIO(P(t, tf + .6, .5))) + "%";
  $("#new").style.opacity = E(P(t, tf + 1.1, .4));
  // 制御役は読むだけ
  const tc = w(4, "Opus") - .2;  // 制御役
  fade("#ct", E(P(t, tc, .5)), 20); fade("#rd", E(P(t, w(4, "読むだけ") - .2, .4)), 0);
}"""
        return plain(body, js, cues, total, {})

    # ⑤ 越えられない壁 ------------------------------------------------------------
    def p5(cues, total):
        tail = rec["pytest"][-1:]
        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">安全</div>
<div id="h" class="head" style="left:140px;top:126px">越えられない壁</div>
<div id="dia" style="left:0;top:0;width:1920px;height:1080px">
  <div id="ai" class="card" style="position:absolute;left:160px;top:330px;width:300px;height:200px;text-align:center;padding-top:40px">
    <div style="font-size:50px;font-weight:700">AI</div><div class="mut" style="font-size:24px">制御役の計画</div></div>
  <div id="direct" style="position:absolute;left:470px;top:430px;width:1000px;border-top:4px dashed #5d6b62"></div>
  <div id="x" style="position:absolute;left:930px;top:370px;font-size:110px;color:#ef7d6f;font-weight:700;line-height:1">×</div>
  <div id="wall" class="card" style="position:absolute;left:820px;top:240px;width:300px;height:600px;border-width:4px">
    <div style="font-size:34px;font-weight:700;text-align:center">守りの層</div>
    <div class="mut" style="font-size:20px;text-align:center">決まった規則のコード</div>
    <div style="position:absolute;left:24px;top:330px;font-size:21px;line-height:1.75;color:#cfd9d1">連続 60 分まで<br>1 日 120 分まで<br>古い値で動かさない<br>15 分で湿度が上がらなければ切る<br><b style="color:#ef7d6f">「止める」が最優先</b></div>
  </div>
  <div id="plug" class="card" style="position:absolute;left:1480px;top:330px;width:300px;height:200px;text-align:center;padding-top:40px">
    <div style="font-size:44px;font-weight:700">加湿器</div><div class="mut" style="font-size:24px">受け手だけが動かす</div></div>
  <div id="pa" class="chip red" style="position:absolute;width:250px;font-size:28px;font-weight:700">入 90 分</div>
  <div id="no" class="chip red" style="position:absolute;width:330px;font-size:22px">断った：連続 60 分まで</div>
  <div id="pb" class="chip" style="position:absolute;width:250px;font-size:28px;font-weight:700">入 20 分</div>
</div>
<div id="term" style="left:330px;top:250px;width:1260px;height:560px;overflow:hidden;background:#070a08;border:2px solid #2e3a32;border-radius:18px;padding:34px 40px;font:28px/1.6 'IBM Plex Mono',monospace;color:#cfe3d4">
  <div style="color:#8fc09f">$ uv run pytest -q tests/test_guard.py</div>
  <div style="margin-top:22px;font-family:'Zen Kaku Gothic New',sans-serif">ランダムな計画 <b id="rn" class="num" style="color:#f0b45a">0</b> 通り × 2 日<span class="mut">（湿度の歩き・跳び・止まり・読めない区間も混ぜる）</span></div>
  <div style="font-family:'Zen Kaku Gothic New',sans-serif">上限を越えた回 <b class="num" style="color:#6cc28b">0</b></div>
  <div id="pt" style="margin-top:28px;white-space:pre;color:#6cc28b">{esc(chr(10).join(tail))}</div>
</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  fade("#ai", E(P(t, .3, .6))); fade("#plug", E(P(t, .45, .6)));
  const tx = w(0, "触りません");
  const wallIn = w(1, "守りの層") - .4;
  $("#direct").style.opacity = E(P(t, .6, .5)) * (1 - P(t, wallIn, .4));
  fade("#x", BK(P(t, tx - .2, .5)) * (1 - P(t, wallIn, .4)), 0, .5);
  fade("#wall", BK(P(t, wallIn, .6)), 0, .85);
  const ta = w(1, "計画は") + .2, hit = ta + 1.2;
  const a = P(t, ta, 1.2);
  const shake = t > hit && t < hit + .5 ? Math.sin((t - hit) * 60) * 10 * (1 - (t - hit) / .5) : 0;
  pos("#pa", L(470, 560, EIO(a)) + shake, 300); $("#pa").style.opacity = cl(a * 5) * (1 - P(t, w(1, "断られたら") + .2, .3));
  $("#wall").style.borderColor = t > hit && t < w(1, "返ります") + .3 ? "#ef7d6f" : (t > w(1, "返ります") + 1.4 && t < w(1, "返ります") + 2.4 ? "#6cc28b" : "#2e3a32");
  const n = P(t, w(1, "断られたら"), 1.1);
  pos("#no", L(780, 300, EIO(n)), 560); $("#no").style.opacity = cl(n * 5) * (1 - P(t, c(2) + .4, .4));
  const tb = w(1, "返ります") + .5, b = P(t, tb, 1.8);
  pos("#pb", L(470, 1500, EIO(b)), 300); $("#pb").style.opacity = cl(b * 5) * (1 - P(t, tb + 2.2, .3));
  const on = P(t, tb + 1.8, .3);
  $("#plug").style.borderColor = on > 0 ? "#6cc28b" : "#2e3a32";
  $("#plug").style.boxShadow = `0 0 ${60 * on}px rgba(108,194,139,${.6 * on})`;
  const tt = Math.max(c(2) + .6, tb + 2.2);
  $("#dia").style.opacity = 1 - .8 * P(t, tt, .5);
  fade("#term", E(P(t, tt, .6)), 40);
  $("#rn").textContent = Math.round(L(0, 120, EIO(P(t, tt + .6, 2.2))));
  fade("#pt", E(P(t, tt + 2.9, .4)), 8);
}"""
        return plain(body, js, cues, total, {})

    # ⑥ 結果：帯の中に保てた時間（再生の比較の表） -----------------------------------
    def p6(cues, total):
        names = ["実運転", "閾値だけ", "1 体", "チーム"]
        tb = rec["replay"]

        def group(season, x0, gid):
            bars = ""
            for i, n in enumerate(names):
                v = tb[season][n]
                col = "#f0b45a" if n == "チーム" else ("#8fc09f" if n == "実運転" else "#4a5c50")
                bars += (
                    f'<div style="position:absolute;left:{x0 + i * 170}px;top:780px;width:120px;text-align:center;font-size:24px" class="mut">{n}</div>'
                    f'<div class="bar {gid}" data-v="{v}" style="position:absolute;left:{x0 + i * 170}px;width:120px;top:770px;height:0;background:{col};border-radius:10px 10px 0 0"></div>'
                    f'<div class="val {gid}" data-v="{v}" style="position:absolute;left:{x0 + i * 170}px;width:120px;text-align:center;font-size:30px;font-weight:700" class="num">{v:g}</div>'
                )
            return bars

        body = f"""
<div id="k" class="kicker" style="left:160px;top:80px">結果</div>
<div id="h" class="head" style="left:160px;top:124px">狙いの帯の中に保てた時間</div>
<div id="cap" class="mut" style="left:160px;top:210px;font-size:24px">同じ 4 日（96 時間）を、4 通りで流し直した（時間）</div>
<div id="sa" style="left:160px;top:250px;width:700px;font-size:40px;font-weight:700">秋</div>
<div id="su" style="left:1000px;top:250px;width:700px;font-size:40px;font-weight:700">夏</div>
<div id="ga">{group("秋", 160, "ga")}</div>
<div id="gs">{group("夏", 1000, "gs")}</div>
<div id="base" style="left:140px;top:770px;width:1640px;border-top:2px solid #4a5c50"></div>
<div id="na" class="chip amber" style="left:330px;top:250px;width:520px;font-size:24px">チームが、実運転より長く保った</div>
<div id="ns" class="chip red" style="left:1170px;top:250px;width:520px;font-size:24px">チームは、実運転に届かなかった</div>
<div id="nt" class="mut" style="left:160px;top:815px;font-size:20px">再生＝本物の記録に、加湿器を入れた差だけを模擬の式で足した部屋（実運転は記録そのもの）・先の記録は渡さない</div>
<div id="wt" class="chip amber" style="left:310px;top:852px;width:1300px;font-size:27px;text-align:center;padding:8px 22px">夏 9/3：土の水分を見て「水やりを控えて」　→　人が記録に書いたのは 9/19</div>
"""
        js = """
function grow(cls, k) {
  $$(".bar." + cls).forEach(b => { const hh = 420 * b.dataset.v / 96 * EIO(k); b.style.height = hh + "px"; b.style.top = (770 - hh) + "px"; });
  $$(".val." + cls).forEach(b => { b.style.top = (770 - 420 * b.dataset.v / 96 * EIO(k) - 44) + "px"; b.style.opacity = P(k, .7, .3); });
}
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cap", E(P(t, .5, .6)));
  fade("#base", E(P(t, .4, .5)), 0); fade("#sa", E(P(t, .5, .5))); fade("#su", E(P(t, .6, .5))); fade("#nt", E(P(t, .8, .5)));
  grow("ga", P(t, w(1, "秋は") - .3, 1.4));
  grow("gs", P(t, w(1, "夏は") - .3, 1.4));
  fade("#na", BK(P(t, w(1, "長く"), .5)), 10, .9);
  fade("#ns", BK(P(t, w(1, "届いて"), .5)), 10, .9);
  const tw = c(2) - .2;
  ["#ga", "#gs", "#sa", "#su", "#base", "#na", "#ns"].forEach(s => { $(s).style.filter = `brightness(${1 - .55 * P(t, tw, .5)})`; });
  fade("#wt", BK(P(t, tw, .6)), 20, .92);
}"""
        return plain(body, js, cues, total, {})

    # ⑦ 道：今・次・最終 ------------------------------------------------------------
    def p7(cues, total):
        nodes = [
            ("今", "加湿", "本物・動いている", "#f0b45a", "solid"),
            ("次", "水やり・温度・換気", "同じ仕組みで広げる", "#8fc09f", "solid"),
            ("最終", "収量と品質の予測", "育て方の記録から先読みする", "#f3e6c4", "dashed"),
        ]
        cards = "".join(
            f'<div id="n{i}" class="card" style="left:{160 + i * 560}px;top:360px;width:500px;height:300px;border:3px {st} {col}">'
            f'<div class="kicker" style="position:static;color:{col}">{a}</div>'
            f'<div style="font-size:42px;font-weight:700;margin-top:18px;line-height:1.25;white-space:nowrap">{b}</div>'
            f'<div class="mut" style="font-size:26px;margin-top:18px">{c}</div></div>'
            for i, (a, b, c, col, st) in enumerate(nodes)
        )
        body = f"""
<div id="k" class="kicker" style="left:160px;top:120px">これから</div>
<div id="h" class="head" style="left:160px;top:166px">加湿器 1 台から、作物の出来まで</div>
<div id="path" style="left:160px;top:510px;width:1600px;height:6px;background:#34443a;border-radius:3px;transform-origin:0 0"></div>
{cards}
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  $("#path").style.transform = `scaleX(${EIO(P(t, .3, ce(1) - .3))})`;
  fade("#n0", BK(P(t, w(0, "いま") - .1, .6)), 30, .92);
  fade("#n1", BK(P(t, w(0, "次は") - .1, .6)), 30, .92);
  fade("#n2", BK(P(t, w(1, "収量") - .4, .7)), 30, .92);
  $("#n1").style.opacity = Math.min(parseFloat($("#n1").style.opacity), .75);
  const g = P(t, w(1, "収量"), 1.2);
  $("#n2").style.boxShadow = `0 0 ${70 * E(g)}px rgba(243,230,196,${.35 * E(g)})`;
}"""
        return plain(body, js, cues, total, {})

    # ⑧ 締め：作り方 → 暗がりのスイッチ（実写） → 題 ---------------------------------
    def p8(cues, total):
        log = rec["gitlog"]
        lines = "".join(f"<div>{esc(x[:64])}</div>" for x in log)
        end = rec.get("ending")
        vid = (
            f'<video id="vid" src="{end["uri"]}" muted preload="auto" playsinline '
            'style="left:0;top:0;width:1920px;height:1080px;object-fit:cover;opacity:0;z-index:1"></video>'
            if end
            else ""
        )
        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">作り方</div>
<div id="h" class="head" style="left:140px;top:126px">6 日間、Claude Code と</div>
<div id="cnt" class="mut" style="left:1100px;top:150px;font-size:28px">期間中の commit {len(log)} 回（期間は 11/2 12:00 から）</div>
<div id="term" style="left:140px;top:250px;width:1640px;height:600px;background:#070a08;border:2px solid #2e3a32;border-radius:18px;overflow:hidden">
  <div id="lines" style="position:absolute;left:40px;top:30px;font:26px/1.6 'IBM Plex Mono','Zen Kaku Gothic New',monospace;color:#cfe3d4;white-space:pre">{lines}</div>
</div>
{vid}
<div id="ttl" style="left:0;top:380px;width:1920px;text-align:center;z-index:2;text-shadow:0 4px 30px #000">
  <div style="font-size:96px;font-weight:700;letter-spacing:.12em">栽培室の頭脳</div>
  <div class="mut" style="font-size:34px;margin-top:24px;letter-spacing:.1em">Built with Claude: Japan 2026</div>
</div>
"""
        js = f"""
const FROM = {end["from"] if end else 0};
async function render(t) {{
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cnt", E(P(t, .6, .6)));
  fade("#term", E(P(t, .3, .6)), 30);
  const el = $("#lines"), over = Math.max(0, el.offsetHeight - 540);
  el.style.transform = `translateY(${{-over * EIO(P(t, .8, ce(0) - .8))}}px)`;
  const tv = c(1) - .5;
  const off = 1 - P(t, tv, .6);
  ["#k", "#h", "#cnt", "#term"].forEach(s => {{ $(s).style.opacity = parseFloat($(s).style.opacity || 1) * off; }});
  const v = $("#vid");
  if (v) {{
    v.style.opacity = E(P(t, tv, .8)) * (1 - .55 * P(t, ce(1) + .3, .8));
    if (t >= tv) await seek(v, FROM + (t - tv));
  }} else {{
    $("#v-black").style.opacity = P(t, tv, .7);
  }}
  fade("#ttl", E(P(t, ce(1) + .5, 1.0)), 16);
}}"""
        return plain(body, js, cues, total, {})

    return {
        "⓪": p0,
        "O": p_open,
        "title": p_title,
        "black": p_black,
        "①": p1,
        "②": p2,
        "③": p3,
        "④": p4,
        "⑤": p5,
        "⑥": p6,
        "⑦": p7,
        "⑧": p8,
    }


# ---------------------------------------------------------------- つなぎ


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def sdr(src: Path) -> Path:
    """iPhone の HDR（HLG）を、macOS の avconvert で SDR（bt709）に直す（作り直さない）。"""
    out = BUILD / "foot" / f"{src.stem}_sdr.mov"
    if out.exists() and out.stat().st_mtime > src.stat().st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "avconvert",
            "--source",
            str(src),
            "--preset",
            "Preset1920x1080",
            "--output",
            str(out),
            "--replace",
        ],
        check=True,
        capture_output=True,
    )
    return out


def ending_clip() -> dict | None:
    """締めの実写（opening.json の "ending"）を、画面の中で送れる mp4（音なし）にする。"""
    if not OPENING.exists():
        return None
    e = json.loads(OPENING.read_text()).get("ending")
    if not e:
        return None
    src = sdr(ROOT / "video" / "assets" / e["clip"])
    mp4 = src.with_suffix(".mp4")
    if not mp4.exists() or mp4.stat().st_mtime < src.stat().st_mtime:
        ff("-i", str(src), "-an", "-c:v", "copy", "-movflags", "+faststart", str(mp4))
    return {"uri": mp4.as_uri(), "from": e["from"]}


def opening_parts(fps: int) -> list[dict] | None:
    if not OPENING.exists():
        return None
    parts = json.loads(OPENING.read_text())["parts"]
    for p in parts:
        if "clip" in p:
            p["src"] = ROOT / "video" / "assets" / p["clip"]
            p["video"] = sdr(p["src"])
            p["len"] = p["to"] - p["from"]
        else:
            p["len"] = p.get("black") or p.get("title")
        p["len"] = round(p["len"] * fps) / fps
    return parts


def timeline(scenes, keys, args, use_aivis, opening):
    """場面ごとに 字幕の時刻（場面の頭から）と長さ を決め、声のファイルの置き場所（動画の頭から）を返す。"""
    plan, voices, t0 = [], [], 0.0
    for key in keys:
        if key == "O":
            if opening:
                for p in opening:
                    p["at"] = t0
                    t0 += p["len"]
                plan.append((key, [], sum(p["len"] for p in opening), opening[0]["at"]))
            else:
                plan.append((key, [], args.opening, t0))
                t0 += args.opening
            continue
        cues, t = [], LEAD
        for text in scenes[key]:
            wav = speak(text, args.voice, args.speed, use_aivis)
            d = float(
                run(
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(wav),
                )
            )
            cues.append({"s": round(t, 3), "e": round(t + d, 3), "text": text})
            voices.append((t0 + t, wav))
            t += d + GAP
        total = round((t - GAP + TAIL.get(key, 0.8)) * args.fps) / args.fps
        plan.append((key, cues, total, t0))
        t0 += total
    return plan, voices, t0


def narration(
    voices, total: float, out: Path, bgm: Path | None, bgm_db: float, clips: list[dict]
) -> None:
    """声を時刻に置いて 1 本にし、実写の音を足し、BGM を声と実写の音の下で下げて混ぜる。"""
    ins, flt = [], []
    for i, (at, wav) in enumerate(voices):
        ins += ["-i", str(wav)]
        ms = int(at * 1000)
        flt.append(f"[{i}]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}:all=1[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(voices)))
    flt.append(
        f"{mix}amix=inputs={len(voices)}:normalize=0,apad,atrim=0:{total:.3f},"
        "loudnorm=I=-16:TP=-1.5:LRA=11[vn]"
    )
    n = len(voices)
    for j, c in enumerate(clips):
        ins += ["-i", str(c["src"])]
        ms = int(c["at"] * 1000)
        flt.append(
            f"[{n + j}:a:0]atrim={c['from']}:{c['to']},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=channel_layouts=stereo,volume={c.get('gain_db', 0)}dB,"
            f"afade=t=out:st={c['len'] - 0.4:.3f}:d=0.4,adelay={ms}:all=1,apad,atrim=0:{total:.3f}[c{j}]"
        )
    src = "[vn]" + "".join(f"[c{j}]" for j in range(len(clips)))
    tag = "[v]" if bgm else "[out]"
    flt.append(
        f"{src}amix=inputs={1 + len(clips)}:normalize=0{tag}" if clips else f"[vn]anull{tag}"
    )
    if bgm:
        k = n + len(clips)
        ins += ["-i", str(bgm)]
        flt.append("[v]asplit=2[v1][v2]")
        flt.append(
            f"[{k}]aresample=48000,atrim=0:{total:.3f},volume={bgm_db}dB[b];"
            "[b][v2]sidechaincompress=threshold=0.03:ratio=6:attack=40:release=600:makeup=1[bd];"
            "[v1][bd]amix=inputs=2:normalize=0,alimiter=limit=0.89[out]"
        )
    ff(
        *ins,
        "-filter_complex",
        ";".join(flt),
        "-map",
        "[out]",
        "-ac",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    )


def clip_frames(p: dict, fps: int, sink) -> int:
    """実写の切り出しを 1920×1080・fps の JPEG にして sink に流す。流した枚数を返す。"""
    n = round(p["len"] * fps)
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-ss", str(p["from"]), "-i", str(p["video"]), "-t", f"{p['len']:.3f}",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
         f"fps={fps},tpad=stop_mode=clone:stop_duration=2",
         "-frames:v", str(n), "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-"],
        stdout=subprocess.PIPE,
    )  # fmt: skip
    while chunk := proc.stdout.read(1 << 20):
        sink.write(chunk)
    proc.wait()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--opening", type=float, default=3.0, help="冒頭の実写の代わりの札の秒（本番は 35）"
    )
    ap.add_argument("--only", default="", help="作る場面（例：③④）。空なら全部")
    ap.add_argument("--stills", action="store_true", help="各行の終わりの絵だけ撮る")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--voice", type=int, default=VOICE_ID, help="AivisSpeech の声（スタイル ID）")
    ap.add_argument("--speed", type=float, default=1.1)
    ap.add_argument("--bgm-db", type=float, default=-12.0, help="BGM の大きさ（声の無い所・dB）")
    ap.add_argument("--no-bgm", action="store_true")
    args = ap.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    use_aivis = aivis_ok()
    print("声：", "AivisSpeech" if use_aivis else "say（AivisSpeech が動いていない）")
    scenes = read_script()
    rec = load_records()
    rec["ending"] = ending_clip()
    pages = scene_pages(rec)
    keys = [k for k in ORDER if not args.only or k in args.only]
    opening = opening_parts(args.fps) if "O" in keys else None
    plan, voices, total = timeline(scenes, keys, args, use_aivis, opening)

    from playwright.sync_api import sync_playwright

    video = BUILD / "video.mp4"
    enc = None
    if not args.stills:
        enc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-framerate",
                str(args.fps),
                "-c:v",
                "mjpeg",
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(args.fps),
                str(video),
            ],
            stdin=subprocess.PIPE,
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--hide-scrollbars"])
        pg = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        pg.on("pageerror", lambda e: print("  画面の誤り：", e))

        def shoot(key, cues, dur):
            src = BUILD / f"scene_{key}.html"
            src.write_text(pages[key](cues, dur))
            pg.goto(src.as_uri())
            pg.wait_for_load_state("load")
            pg.evaluate("document.fonts.ready")
            n = round(dur * args.fps)
            for i in range(n):
                pg.evaluate(f"__frame({i / args.fps})")
                enc.stdin.write(pg.screenshot(type="jpeg", quality=92))
            return n

        for key, cues, dur, _ in plan:
            if key == "O" and opening:
                if args.stills:
                    print("O 実写（絵は撮らない）")
                    continue
                n = 0
                for p in opening:
                    if "clip" in p:
                        n += clip_frames(p, args.fps, enc.stdin)
                    else:
                        n += shoot("title" if "title" in p else "black", [], p["len"])
                print(f"O {dur:5.1f} 秒  {n} 枚（実写）", flush=True)
                continue
            src = BUILD / f"scene_{key}.html"
            src.write_text(pages[key](cues, dur))
            pg.goto(src.as_uri())
            pg.wait_for_load_state("load")
            pg.evaluate("document.fonts.ready")
            pg.wait_for_timeout(800)  # 計器盤が記録を描き終えるのを待つ
            pg.evaluate(
                "Promise.all([...document.querySelectorAll('video')].map(v => v.readyState >= 2 ? 1 :"
                " new Promise(r => v.addEventListener('loadeddata', r, { once: true }))))"
            )
            if args.stills:
                times = [q["e"] - 0.05 for q in cues] or [dur / 2]
                for i, ts in enumerate(times):
                    pg.evaluate(f"__frame({ts})")
                    pg.screenshot(path=str(BUILD / f"still_{key}_{i}.png"))
                print(f"{key} {dur:5.1f} 秒  絵 {len(times)} 枚")
                continue
            n = round(dur * args.fps)
            for i in range(n):
                pg.evaluate(f"__frame({i / args.fps})")
                enc.stdin.write(pg.screenshot(type="jpeg", quality=92))
            print(f"{key} {dur:5.1f} 秒  {n} 枚", flush=True)
        browser.close()
    if args.stills:
        print(f"絵：{BUILD}/still_*.png")
        return
    enc.stdin.close()
    enc.wait()

    audio = BUILD / "narration.m4a"
    bgm = None
    if not args.no_bgm:
        bgm = BUILD / "bgm.wav"
        marks = [t0 for k, _, _, t0 in plan if k != "O"]
        turn = [p["at"] for p in (opening or []) if p.get("turn")]
        if turn and len(marks) > 1:
            marks[1] = turn[0]
        subprocess.run(
            ["uv", "run", "python", str(ROOT / "video" / "bgm.py"), f"{total:.3f}", str(bgm)]
            + [f"{m:.3f}" for m in marks],
            check=True,
        )
    clips = [p for p in (opening or []) if "clip" in p]
    narration(voices, total, audio, bgm, args.bgm_db, clips)
    out = OUT / ("part.mp4" if args.only else "draft.mp4")
    ff("-i", str(video), "-i", str(audio), "-c:v", "copy", "-c:a", "copy", "-shortest", str(out))
    print("\n場面ごとの秒：", "・".join(f"{k} {d:.1f}" for k, _, d, _ in plan))
    body = sum(d for k, _, d, _ in plan if k != "O")
    op = sum(p["len"] for p in opening) if opening else 35.0
    print(f"冒頭の外 {body:.1f} 秒＋冒頭 {op:.1f} 秒＝{body + op:.1f} 秒（3 分＝180 秒）")
    print(f"出来上がり：{out}")


if __name__ == "__main__":
    main()
