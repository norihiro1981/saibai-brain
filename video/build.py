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
ORDER = "O⓪A①②③④⑤⑥⑦⑧⑨⑩"  # O＝冒頭の実写・A＝計器盤から本物の加湿器を動かす実写
AIVIS = "http://127.0.0.1:10101"
VOICE_ID = 1937616896  # にせ（低めで落ち着いた講義調・ACML 1.0＝商用可・クレジット任意）
LEAD, GAP = 0.5, 0.45  # 場面の頭の間・行と行の間
TAIL = {"⓪": 1.0, "⑩": 4.5}  # 場面の後ろの間（ほかは 0.8）
# 冒頭の実写の並び（手元だけ＝git に入らない）。無ければ札で代える。形：
# {"parts": [{"clip": "norihiro_sample/IMG_0001.mov", "from": 0, "to": 30, "gain_db": 12},
#            {"black": 1.0}, {"title": 2.5, "turn": true}]}
# turn＝BGM がここで長調に移る（無ければ ① の頭）
# "after": [秒, gain_db]＝実写の音をその秒から 0.6 秒かけて gain_db に変える（「寒い」の後を下げる）
# "artifact"：{"clip": ..., "from": 0, "to": 18, "gain_db": 12, "beats": {"on": 0.3, "mist": 4.6, "off": 9.4, "auto": 14.3}}
# beats＝実写の中の時刻（入れる・霧が出る・切る・オートを入れる）＝札を出す時刻
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
        m = re.match(r"## ([⓪①②③④⑤⑥⑦⑧⑨⑩])", line)
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
  if (!__init) { __init = true; M = $("main"); if (M) { M.style.transform = "none"; } if (typeof __vSetup === "function") __vSetup(); }
  subs(t); if (typeof __vRender === "function") await __vRender(t);
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
        # 場面の setup・render は名を替える（計器盤にも render があり、上書きすると記録を描かなくなる）
        + js.replace("function render(", "function __vRender(").replace(
            "function setup(", "function __vSetup("
        )
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
        # 11/5 14:50 の本物の天気（run/weather_20261105.jsonl のアメダスと 11 時発表の予報）
        s["weather"] = {
            "ok": True,
            "line": "外（近くのアメダス 14:50）21.1℃・66%｜今日 11/5：晴れ 後 くもり 夜 雨・降水確率 最大 60%",
            "reason": None,
            "error": None,
        }
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
  <text id="bandl" x="10" fill="#8fc09f" font-size="24">ちょうどいい空気</text>
  <path id="red" fill="rgba(239,125,111,.45)"/>
  <g id="hum"></g>
  <path id="out" fill="none" stroke="#7fb3d4" stroke-width="4" stroke-dasharray="10 8"/>
  <path id="room" fill="none" stroke="#e8efe9" stroke-width="5"/>
  <g id="ax" fill="#98a79d" font-size="22"></g>
  <text id="outl" fill="#7fb3d4" font-size="26">外の湿度</text>
  <text id="rooml" fill="#e8efe9" font-size="26">部屋の空気の乾き</text>
</svg>
<div id="hand" class="chip amber" style="width:430px;font-size:26px">人が気づいて入れたのは、午後</div>
<div id="burd" style="font-size:76px;font-weight:700;color:#fff;white-space:nowrap;padding:6px 34px;background:rgba(150,40,30,.72);border:3px solid #ef7d6f;border-radius:18px;text-shadow:0 3px 14px rgba(0,0,0,.6)">乾きすぎ＝作物に負担</div>
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
  // 書ききった後に、帯からはみ出した赤い所の上へ大きく
  const pk = R.reduce((a, p) => (p[1] > a[1] ? p : a), R[0]);
  const bw = $("#burd").offsetWidth;
  pos("#burd", 160 + X(pk[0]) - bw / 2, 240 + (YV(.8) + YV(pk[1])) / 2 - 50);
  fade("#burd", BK(P(t, ce(1) + .1, .6)), 20, .85);
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

    # A 計器盤（Artifact）から本物の加湿器を動かす実写 ------------------------------
    def pA(cues, total):
        a = rec.get("artifact") or {}
        b = a.get("beats", {})
        body = f"""
<video id="vid" src="{a.get("uri", "")}" muted preload="auto" playsinline
  style="left:0;top:0;width:1920px;height:1080px;object-fit:cover"></video>
<div id="tag" class="chip" style="right:40px;top:40px;font-size:36px;font-weight:700;padding:14px 26px;white-space:nowrap">実写　計器盤（Artifact）→ 本物の加湿器</div>
<div class="ring" id="r0"></div><div class="ring" id="r1"></div><div class="ring" id="r2"></div>
<div class="box" id="b0"></div><div class="box" id="b1"></div><div class="box" id="b2"></div>
<svg id="arr" width="1920" height="1080" style="left:0;top:0;overflow:visible">
  <defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="#f0b45a"/></marker></defs>
  <path id="ap" fill="none" stroke="#f0b45a" stroke-width="6" stroke-dasharray="14 10" marker-end="url(#ah)"/>
</svg>
<div id="t0" class="chip amber tel">押す：手動の「入れる」</div>
<div id="t1" class="chip tel">少し遅れて、霧が出る ↓</div>
<div id="t2" class="chip amber tel">押す：手動の「切る」</div>
<div id="t3" class="chip amber tel">押す：オートの「入れる」</div>
<div id="s0" class="chip act" style="font-size:52px;font-weight:700">手動操作：入れる</div>
<div id="s1" class="chip act" style="font-size:52px;font-weight:700">本物の加湿器が動く</div>
<div id="s2" class="chip act" style="font-size:52px;font-weight:700">手動操作：切る</div>
<div id="s3" class="chip act amber" style="font-size:52px;font-weight:700">オート動作に移行<span style="font-size:34px;font-weight:500;color:#cfd9d1">　→　あとは AI が 3 時間ごとに決める</span></div>
<style>
.ring {{ position:absolute; width:120px; height:120px; margin:-60px 0 0 -60px; border-radius:50%;
  border:7px solid #f0b45a; box-shadow:0 0 30px rgba(240,180,90,.8); opacity:0; }}
.act {{ left:0; right:0; margin:0 auto; width:fit-content; top:880px; white-space:nowrap; padding:18px 40px; }}
.tel {{ right:40px; top:136px; font-size:42px; font-weight:700; white-space:nowrap; padding:14px 28px; }}
.box {{ position:absolute; border:6px solid #f0b45a; border-radius:14px; opacity:0;
  box-shadow:0 0 0 9999px rgba(0,0,0,.28), 0 0 30px rgba(240,180,90,.9); }}
</style>
"""
        js = f"""
const FROM = {a.get("from", 0)}, B = {json.dumps(b)};
// 押した所（1920×1080 の画面の上）：[押す時刻, 中心 x, 中心 y, 幅, 高さ, 次の時刻]
const RING = [[B.on, 686, 144, 124, 70, B.mist], [B.off, 765, 144, 100, 70, B.auto], [B.auto, 474, 324, 118, 66, T + 1]];
function win(t, a, z) {{ return E(P(t, a, .35)) * (1 - P(t, z - .35, .35)); }}
async function render(t) {{
  fade("#tag", win(t, .2, T + 1), 0);
  fade("#s0", win(t, B.on, B.mist - .1), 0);
  fade("#s1", win(t, B.mist + .2, B.off - .1), 0);
  fade("#s2", win(t, B.off, B.auto - .2), 0);
  fade("#s3", win(t, B.auto, T + 1), 0);
  fade("#t0", win(t, B.on, B.mist - .1), 0);
  fade("#t1", win(t, B.mist + .2, B.off - .1), 0);
  fade("#t2", win(t, B.off, B.auto - .2), 0);
  fade("#t3", win(t, B.auto, T + 1), 0);
  let arrow = 0, ax = 0, ay = 0, tel = null;
  RING.forEach(([at, x, y, bw, bh, nx], i) => {{
    // 輪は 0.9 秒ごとに広がり直す（押した後 2.6 秒まで）
    const r = $("#r" + i), since = t - at + .15;
    const k = since > 0 && since < 2.7 ? (since % .9) / .9 : -1;
    r.style.left = x + "px"; r.style.top = y + "px";
    r.style.opacity = k >= 0 ? (1 - k) : 0;
    r.style.transform = `scale(${{.6 + 1.4 * E(k)}})`;
    // 枠は次の操作まで残す（周りを少し暗く）
    const b = $("#b" + i), on = win(t, at - .1, nx - .1);
    b.style.left = (x - bw / 2) + "px"; b.style.top = (y - bh / 2) + "px";
    b.style.width = bw + "px"; b.style.height = bh + "px";
    b.style.opacity = on; b.style.transform = `scale(${{L(1.6, 1, BK(P(t, at - .1, .45)))}})`;
    if (on > arrow) {{ arrow = on; ax = x + bw / 2 + 14; ay = y; tel = "#t" + (i === 0 ? 0 : i + 1); }}
  }});
  if (tel) {{
    const e = $(tel).getBoundingClientRect();
    const sx = e.left - 12, sy = e.top + e.height / 2;
    $("#ap").setAttribute("d", `M ${{sx}} ${{sy}} C ${{sx - 180}} ${{sy}}, ${{ax + 160}} ${{ay}}, ${{ax}} ${{ay}}`);
  }}
  $("#ap").style.opacity = arrow;
  $("#v-black").style.opacity = 1 - P(t, 0, .4) + P(t, T - .4, .4);
  await seek($("#vid"), FROM + t);
}}"""
        return plain(body, js, cues, total, {})

    # ① 画面でできること：計器盤の 4 つの行 ---------------------------------------------
    def pUI(cues, total):
        X = 1180
        rows = [
            ("手動", "人が、今すぐ入れる・切る"),
            ("定時", "毎日の時刻を決めて動かす"),
            ("オート", "時刻も長さも、AI が決める"),
            ("止める", "何より先に、人が止められる"),
        ]
        chips = "".join(
            f'<div id="u{i}" class="chip{" amber" if i == 2 else (" red" if i == 3 else "")}" '
            f'style="left:{X}px;top:{300 + i * 150}px;width:640px;font-size:30px">'
            f'<b style="font-size:38px">{a}</b>　{b}</div>'
            for i, (a, b) in enumerate(rows)
        )
        body = f"""
<div id="k" class="kicker" style="left:{X}px;top:80px">計器盤（Artifact）</div>
<div id="h" class="head" style="left:{X}px;top:124px;font-size:54px">画面ひとつで、4 通り</div>
{chips}
"""
        js = """
let OC;
function setup() { OC = off(".controls"); }
function render(t) {
  cam(70, 150 - OC.y);
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  const ts = [w(0, "手で") - .2, w(0, "時刻") - .2, w(0, "AI") - .2, w(1, "止める") - .2];
  ts.forEach((a, i) => {
    fade("#u" + i, BK(P(t, a, .5)), 16, .92);
    const nx = i < 3 ? ts[i + 1] : T + 1;
    hl(".controls .mode:nth-of-type(" + (i + 1) + ")", t > a && t < nx);
  });
}"""
        return dash(rec, body, js, cues, total, {})

    # ① 種明かし：Managed Agents（雲）と手元 ---------------------------------------
    def p1(cues, total):
        def ag(i, model, role, y):
            return (
                f'<div id="a{i}" class="chip" style="left:200px;top:{y}px;width:600px;padding:18px 26px">'
                f'<b style="font-size:36px">{role}</b><span class="num" style="margin-left:18px;color:#8fc09f;font-size:26px">{model}</span></div>'
            )

        body = f"""
<div id="cloud" class="card" style="left:140px;top:150px;width:900px;height:680px;border:3px dashed #f0b45a;background:rgba(240,180,90,.04)">
  <div style="font-size:34px;font-weight:700;color:#f0b45a">Claude Managed Agents</div>
  <div class="mut" style="font-size:22px;margin-top:4px">クラウド＝考える所（Anthropic の側）・3 時間ごとに起きる</div>
</div>
{ag(0, "Sonnet 5.5", "分析役", 290)}
{ag(1, "Opus 5.5", "制御役", 400)}
{ag(2, "Haiku 5.5", "専門家", 510)}
<div id="mem" class="chip amber" style="left:200px;top:650px;width:600px;font-size:26px">記憶の置き場：経験（版が残る）・記録・知識</div>
<div id="local" class="card" style="left:1240px;top:150px;width:540px;height:680px;border:3px solid #8fc09f">
  <div style="font-size:34px;font-weight:700;color:#8fc09f">ローカル</div>
  <div class="mut" style="font-size:22px;margin-top:4px">動かす所</div>
</div>
<div id="l0" class="chip" style="left:1300px;top:290px;width:420px;font-size:30px;font-weight:700">受け手</div>
<div id="l1" class="chip red" style="left:1300px;top:420px;width:420px;font-size:30px;font-weight:700">守りの層</div>
<div id="l2" class="chip" style="left:1300px;top:550px;width:420px;font-size:30px;font-weight:700">加湿器</div>
<div id="key" class="chip red" style="left:1280px;top:690px;width:460px;font-size:26px">機械の鍵は、ローカルだけ<br><span class="mut" style="font-size:20px">クラウドの AI は、計画を出すだけ</span></div>
<div id="up" style="left:1060px;top:350px;width:160px;text-align:center;font-size:22px;color:#7fb3d4">← 記録<br><span style="font-size:18px" class="mut">温湿度・操作・天気</span></div>
<div id="dn" style="left:1060px;top:470px;width:160px;text-align:center;font-size:22px;color:#f0b45a">計画 →<br><span style="font-size:18px" class="mut">submit_plan</span></div>
"""
        js = """
function render(t) {
  fade("#cloud", E(P(t, w(0, "Managed") - .3, .6)), 20);
  for (let i = 0; i < 3; i++) fade("#a" + i, BK(P(t, w(0, "3 体") + i * .2, .5)), 20, .92);
  fade("#mem", E(P(t, w(0, "3 体") + .9, .5)), 10);
  fade("#local", E(P(t, w(1, "ローカル") - .4, .6)), 20);
  for (let i = 0; i < 3; i++) fade("#l" + i, E(P(t, w(1, "ローカル") - .1 + i * .18, .4)), 12);
  fade("#up", E(P(t, w(1, "考える") , .5)), 0); fade("#dn", E(P(t, w(1, "動かす") - .2, .5)), 0);
  fade("#key", BK(P(t, w(1, "鍵") - .2, .5)), 12, .92);
}"""
        return plain(body, js, cues, total, {})

    # ② 目指すところ ------------------------------------------------------------
    def p2(cues, total):
        body = """
<div id="k" class="kicker" style="left:160px;top:120px">目指すところ</div>
<div id="g0" class="card" style="left:160px;top:250px;width:760px;height:460px;padding:44px 48px">
  <div class="num" style="font-size:40px;color:#f0b45a">1</div>
  <div style="font-size:52px;font-weight:700;line-height:1.35;margin-top:10px">天気を先読みして、<br>事前に手を打つ</div>
  <div class="mut" style="font-size:24px;margin-top:26px">予報と部屋の記録から、崩れる前に計画する</div>
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

    # ⑥ 画面で見る：先を読んで決める（11/5 15:04 の本物の答え） ----------------------------
    def p3(cues, total):
        X = 1180
        body = f"""
<div id="k" class="kicker" style="left:{X}px;top:60px">画面で見る</div>
<div id="h" class="head" style="left:{X}px;top:100px;font-size:50px">先を読んで決める</div>
<div id="cap" class="mut" style="left:{X}px;top:186px;font-size:24px">11/5 15:04 の回（本物の答え）</div>
<div id="cw" class="chip" style="width:680px;border-left-color:#7fb3d4">外の天気：夜は雨の予報<span class="mut">（18 時の降水確率 60%）</span></div>
<div id="ch" class="chip" style="width:680px">人の操作：今日 13:15〜14:15 の 60 分（手動）<br>乾き <b class="num">0.925 → 0.793</b><span class="mut">（効きが弱かった）</span></div>
<div id="c2" class="chip" style="width:680px">経験：秋の傾向 <b class="num">26</b> 回・15 分で <b class="num">−0.238</b></div>
<div id="barbox" style="left:{X}px;top:600px;width:680px;height:96px">
  <div id="bar" style="position:absolute;left:0;top:0;height:96px;width:680px;border-radius:12px;background:#2a3a30;border:2px solid #4a5c50"></div>
  <div id="barl" style="position:absolute;left:24px;top:22px;font-size:34px;font-weight:700;white-space:nowrap">定時の 60 分</div>
</div>
<div id="c4" class="chip amber" style="width:680px;font-size:26px">「外の天気がこの部屋にどれだけ効くかの行は経験に無いので、<span id="mk" class="mark">これは推測</span>」</div>
"""
        js = f"""
const X = {X};
const MAN = ".controls .mode:nth-of-type(1)", TIM = ".controls .mode:nth-of-type(2)", EXR = "#expTable tbody tr:nth-child(2)";
let OW, OM, OE, OT;
function setup() {{
  OW = off("#weather"); OM = off(MAN); OE = off(".exp"); OT = off(TIM);
}}
function render(t) {{
  const yW = 330 - OW.y, yM = 330 - OM.y, yE = 300 - OE.y, yT = 330 - OT.y;
  const tw = c(0) + .1, th = w(1, "人の操作") - .3, te = w(1, "経験") - .4, td = c(2) - .3;
  cam(70, track(t, [[0, yW], [th, yM], [te, yE], [td, yT]], .7));
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cap", E(P(t, .6, .6)));
  hl("#weather", t > tw && t < th + .3);
  hl(MAN, t > th + .6 && t < te + .3);
  hl(EXR, t > te + .6 && t < td + .3);
  hl(TIM, t > td + .6 && t < c(3) - .2);
  hl("#timerWhy", t > c(3) - .2);
  fly("#cw", center("#weather"), {{ x: X, y: 230 }}, P(t, tw + .5, .9));
  fly("#ch", center(MAN), {{ x: X, y: 330 }}, P(t, th + .7, .9));
  fly("#c2", center(EXR), {{ x: X, y: 470 }}, P(t, te + .7, .9));
  fade("#barbox", E(P(t, td, .5)));
  const sh = EIO(P(t, td + .9, .8));
  $("#bar").style.width = L(680, 680 * 20 / 60, sh) + "px";
  $("#bar").style.background = sh > 0 ? `rgba(240,180,90,${{L(.15, .9, sh)}})` : "#2a3a30";
  $("#barl").textContent = sh > .5 ? "AI の分：20 分" : "定時の 60 分";
  $("#barl").style.left = (sh > .5 ? 680 * 20 / 60 + 24 : 24) + "px";
  fade("#c4", E(P(t, c(3) - .3, .6))); pos("#c4", X, 730);
  $("#mk").style.setProperty("--m", (100 * E(P(t, w(3, "推測") - .1, .7))) + "%");
}}"""
        return dash(rec, body, js, cues, total, {}, patch=timer_patch)

    # ④ 経験が育つ：データが回る輪（具体例は出さない） ---------------------------------
    def p4(cues, total):
        # 輪の上の箱：(id, 名, 小さな字, x, y, 側)  側＝cloud（Managed Agents）／local（ローカル）
        nodes = [
            ("n0", "記録", "温湿度・操作・天気", 150, 250, "local"),
            ("n1", "分析役", "Sonnet 5.5・振り返る", 650, 250, "cloud"),
            ("n2", "経験", "記憶の置き場・版が残る", 1150, 250, "cloud"),
            ("n3", "制御役", "Opus 5.5・読むだけ", 1150, 600, "cloud"),
            ("n4", "守りの層", "決まった規則", 650, 600, "local"),
            ("n5", "加湿器", "ローカルだけが動かす", 150, 600, "local"),
        ]
        box = "".join(
            f'<div id="{i}" class="card" style="left:{x}px;top:{y}px;width:360px;height:150px;padding:22px 28px;'
            f'border:3px solid {"#f0b45a" if side == "cloud" else "#8fc09f"}">'
            f'<div style="font-size:40px;font-weight:700">{a}</div><div class="mut" style="font-size:22px;margin-top:6px">{b}</div>'
            + (
                '<div id="lv" style="position:absolute;left:0;bottom:0;height:8px;background:#f0b45a;border-radius:0 0 0 16px"></div>'
                if i == "n2"
                else ""
            )
            + "</div>"
            for i, a, b, x, y, side in nodes
        )
        body = f"""
<div id="k" class="kicker" style="left:150px;top:70px">設計の考え 1</div>
<div id="h" class="head" style="left:150px;top:110px;font-size:50px">回すほど、経験がたまる</div>
<svg id="ring" width="1920" height="1080" style="left:0;top:0;overflow:visible">
  <path id="loop" d="M 330 325 L 1330 325 L 1330 675 L 330 675 Z" fill="none" stroke="#34443a" stroke-width="6" stroke-linejoin="round"/>
  <g id="dots"></g>
</svg>
{box}
<div id="ex" class="card" style="left:1600px;top:600px;width:240px;height:150px;padding:22px 24px;border:3px solid #f0b45a">
  <div style="font-size:34px;font-weight:700">専門家</div><div class="mut" style="font-size:20px;margin-top:6px">Haiku 5.5・知識</div></div>
<div id="exl" style="left:1515px;top:650px;font-size:30px;color:#f0b45a">⇄</div>
<div id="rd" style="left:1350px;top:470px;font-size:22px;color:#f0b45a">読むだけ ↓</div>
<div id="wr" style="left:1015px;top:270px;font-size:22px;color:#6cc28b">書く →</div>
<div id="lap" class="mut" style="left:640px;top:440px;width:640px;text-align:center;font-size:26px">1 周＝3 時間ごとの起動</div>
<div id="kw" class="head" style="left:0;top:800px;width:1920px;text-align:center;font-size:60px;color:#f0b45a">動かすほど、賢くなる</div>
"""
        js = """
let LEN = 0;
function setup() {
  LEN = $("#loop").getTotalLength();
  $("#dots").innerHTML = Array.from({ length: 14 }, () => '<circle r="9" fill="#f0b45a"/>').join("");
}
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  const show = [["#n0", c(0)], ["#n1", w(0, "分析役") - .2], ["#n2", w(0, "経験") - .2],
                ["#n3", c(1)], ["#ex", w(1, "専門家") - .2], ["#n4", w(1, "計画") - .1], ["#n5", w(1, "計画") + .2]];
  show.forEach(([id, ts]) => fade(id, BK(P(t, ts, .5)), 16, .92));
  fade("#exl", E(P(t, w(1, "専門家"), .4)), 0);
  fade("#wr", E(P(t, w(0, "書き足す") - .2, .4)), 0);
  fade("#rd", E(P(t, w(1, "読み") - .2, .4)), 0);
  fade("#lap", E(P(t, c(2) - .6, .5)), 0);
  $("#loop").style.opacity = E(P(t, c(0) + .3, .8));
  // 粒が輪を回る。最後の行で速くなり、経験の箱が満ちていく
  const t0 = c(0) + .6;
  const fast = P(t, c(2) - .3, 1.2);
  const run = Math.max(0, t - t0);
  const dist = run * 260 + Math.max(0, t - (c(2) - .3)) * 700 * EIO(fast);
  $$("#dots circle").forEach((d, i) => {
    const on = cl((dist - i * 70) / 60);
    const pt = $("#loop").getPointAtLength(((dist - i * 70) % LEN + LEN) % LEN);
    d.setAttribute("cx", pt.x); d.setAttribute("cy", pt.y); d.setAttribute("opacity", on * .95);
  });
  const laps = dist / LEN;
  $("#lv").style.width = Math.min(360, 40 + laps * 70) + "px";
  const glow = Math.min(1, laps / 4);
  $("#n2").style.boxShadow = `0 0 ${80 * glow}px rgba(240,180,90,${.45 * glow})`;
  fade("#kw", BK(P(t, w(2, "動かせば") - .2, .7)), 20, .9);
}"""
        return plain(body, js, cues, total, {})

    # ⑤ 設計の考え 2：外の天気と人の操作を取り込む（輪と同じ形の図） -------------------------
    def pWH(cues, total):
        nodes = [
            ("w0", "外の天気", "近くのアメダス・予報", 150, 250, "local"),
            ("w1", "制御役", "先を読んで、控える・長くする", 1290, 250, "cloud"),
            ("h0", "人の操作", "手動・定時", 150, 600, "local"),
            ("h1", "分析役", "1 回ずつ効き目を計算", 720, 600, "cloud"),
            ("h2", "経験", "記憶の置き場・版が残る", 1290, 600, "cloud"),
        ]
        box = "".join(
            f'<div id="{i}" class="card" style="left:{x}px;top:{y}px;width:{420 if i == "w1" else 360}px;height:150px;padding:22px 28px;'
            f'border:3px solid {"#f0b45a" if side == "cloud" else "#8fc09f"}">'
            f'<div style="font-size:40px;font-weight:700">{a}</div><div class="mut" style="font-size:22px;margin-top:6px">{b}</div>'
            + (
                '<div id="lv" style="position:absolute;left:0;bottom:0;height:8px;background:#f0b45a;border-radius:0 0 0 16px"></div>'
                if i == "h2"
                else ""
            )
            + "</div>"
            for i, a, b, x, y, side in nodes
        )
        body = f"""
<div id="k" class="kicker" style="left:150px;top:70px">設計の考え 2</div>
<div id="h" class="head" style="left:150px;top:110px;font-size:50px">外の天気と、人の操作を取り込む</div>
<svg id="paths" width="1920" height="1080" style="left:0;top:0;overflow:visible">
  <path id="pw" d="M 510 325 L 1290 325" fill="none" stroke="#2c4250" stroke-width="6"/>
  <path id="ph" d="M 510 675 L 1470 675 L 1470 400" fill="none" stroke="#34443a" stroke-width="6" stroke-linejoin="round"/>
  <g id="dw"></g><g id="dh"></g>
</svg>
{box}
<div id="lw" style="left:600px;top:270px;width:600px;text-align:center;font-size:24px;color:#7fb3d4">今の外・今日と明日の予報 →</div>
<div id="lh" style="left:520px;top:700px;width:200px;text-align:center;font-size:20px;color:#8fc09f">誰が・何分・<br>前後の乾き</div>
<div id="lx" style="left:1090px;top:620px;font-size:22px;color:#6cc28b">書く →</div>
<div id="lr" style="left:1490px;top:470px;font-size:22px;color:#f0b45a">読む ↑</div>
<div id="kw" class="head" style="left:0;top:800px;width:1920px;text-align:center;font-size:60px;color:#f0b45a">天気と人のカンが、AI の判断になる</div>
"""
        js = """
let LW = 0, LH = 0;
function setup() {
  LW = $("#pw").getTotalLength(); LH = $("#ph").getTotalLength();
  $("#dw").innerHTML = Array.from({ length: 7 }, () => '<circle r="9" fill="#7fb3d4"/>').join("");
  $("#dh").innerHTML = Array.from({ length: 10 }, () => '<circle r="9" fill="#8fc09f"/>').join("");
}
function flow(sel, path, len, dist, gap) {
  $$(sel + " circle").forEach((d, i) => {
    const x = dist - i * gap, on = x > 0 ? 1 : 0;
    const pt = $(path).getPointAtLength(((x % len) + len) % len);
    d.setAttribute("cx", pt.x); d.setAttribute("cy", pt.y); d.setAttribute("opacity", on * .95);
  });
}
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  const tw = c(0), th = c(1);
  fade("#w0", BK(P(t, tw, .5)), 16, .92); fade("#w1", BK(P(t, w(0, "制御役") - .2, .5)), 16, .92);
  fade("#lw", E(P(t, tw + .6, .4)), 0); $("#pw").style.opacity = E(P(t, tw + .3, .6));
  fade("#h0", BK(P(t, th, .5)), 16, .92); fade("#h1", BK(P(t, w(1, "分析役") - .2, .5)), 16, .92);
  fade("#h2", BK(P(t, w(1, "経験") - .2, .5)), 16, .92);
  fade("#lh", E(P(t, th + .5, .4)), 0); fade("#lx", E(P(t, w(1, "書き足す") - .2, .4)), 0);
  fade("#lr", E(P(t, c(2) - .3, .4)), 0); $("#ph").style.opacity = E(P(t, th + .3, .6));
  const fast = t > c(2) - .3 ? (t - (c(2) - .3)) * 500 * EIO(P(t, c(2) - .3, 1.2)) : 0;
  flow("#dw", "#pw", LW, Math.max(0, t - tw - .6) * 240 + fast, 110);
  const dh = Math.max(0, t - th - .6) * 240 + fast;
  flow("#dh", "#ph", LH, dh, 120);
  $("#lv").style.width = Math.min(360, 30 + dh / LH * 90) + "px";
  const glow = cl(dh / LH / 3);
  $("#h2").style.boxShadow = `0 0 ${70 * glow}px rgba(240,180,90,${.4 * glow})`;
  fade("#kw", BK(P(t, c(2) - .2, .7)), 20, .9);
}"""
        return plain(body, js, cues, total, {})

    # ⑤ 越えられない壁 ------------------------------------------------------------
    def p5(cues, total):
        body = """
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
    <div style="position:absolute;left:24px;top:330px;font-size:28px;line-height:1.8;color:#cfd9d1">続けて 60 分まで<br>1 日 120 分まで<br><b style="color:#ef7d6f">「止める」が最優先</b></div>
  </div>
  <div id="plug" class="card" style="position:absolute;left:1480px;top:330px;width:300px;height:200px;text-align:center;padding-top:40px">
    <div style="font-size:44px;font-weight:700">加湿器</div><div class="mut" style="font-size:24px">受け手だけが動かす</div></div>
  <div id="pa" class="chip red" style="position:absolute;width:250px;font-size:28px;font-weight:700">入 90 分</div>
  <div id="no" class="chip red" style="position:absolute;width:330px;font-size:22px">断った：続けて 60 分まで</div>
  <div id="pb" class="chip" style="position:absolute;width:250px;font-size:28px;font-weight:700">入 20 分</div>
</div>
<div id="big" style="left:1180px;top:118px;width:620px;text-align:right;font-size:60px;font-weight:700;color:#6cc28b">上限は、越えない</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  fade("#ai", E(P(t, .3, .6))); fade("#plug", E(P(t, .45, .6)));
  const tx = w(0, "触らない");
  const wallIn = w(1, "守りの層") - .4;
  $("#direct").style.opacity = E(P(t, .6, .5)) * (1 - P(t, wallIn, .4));
  fade("#x", BK(P(t, tx - .2, .5)) * (1 - P(t, wallIn, .4)), 0, .5);
  fade("#wall", BK(P(t, wallIn, .6)), 0, .85);
  const ta = wallIn + .5, hit = ta + 1.2;
  const a = P(t, ta, 1.2);
  const shake = t > hit && t < hit + .5 ? Math.sin((t - hit) * 60) * 10 * (1 - (t - hit) / .5) : 0;
  pos("#pa", L(470, 560, EIO(a)) + shake, 300); $("#pa").style.opacity = cl(a * 5) * (1 - P(t, hit + .6, .3));
  $("#wall").style.borderColor = t > hit && t < hit + 1.6 ? "#ef7d6f" : (t > hit + 2.6 && t < hit + 3.6 ? "#6cc28b" : "#2e3a32");
  const n = P(t, hit + .4, 1.1);
  pos("#no", L(780, 300, EIO(n)), 560); $("#no").style.opacity = cl(n * 5) * (1 - P(t, hit + 2.4, .4));
  const tb = hit + 1.7, b = P(t, tb, 1.8);
  pos("#pb", L(470, 1500, EIO(b)), 300); $("#pb").style.opacity = cl(b * 5) * (1 - P(t, tb + 2.2, .3));
  const on = P(t, tb + 1.8, .3);
  $("#plug").style.borderColor = on > 0 ? "#6cc28b" : "#2e3a32";
  $("#plug").style.boxShadow = `0 0 ${60 * on}px rgba(108,194,139,${.6 * on})`;
  fade("#big", BK(P(t, w(2, "上限") - .2, .6)), 20, .9);
}"""
        return plain(body, js, cues, total, {})

    # ⑦ 腕試し：過去の記録で流し直す（流れの図 1 本と秋の棒 2 本） ---------------------
    def p6(cues, total):
        tb = rec["replay"]
        hu, ai = tb["秋"]["実運転"], tb["秋"]["チーム"]
        sh, sa = tb["夏"]["実運転"], tb["夏"]["チーム"]
        steps = [
            ("過去の記録", "夏と秋・4 日ずつ"),
            ("AI に 1 日ずつ渡す", "先の記録は見せない"),
            ("人の実際と比べる", "ちょうどいい空気だった時間"),
        ]
        flow = "".join(
            f'<div id="f{i}" class="card" style="left:{160 + i * 560}px;top:250px;width:480px;height:150px;text-align:center;padding-top:28px">'
            f'<div style="font-size:36px;font-weight:700">{a}</div><div class="mut" style="font-size:24px;margin-top:8px">{b}</div></div>'
            for i, (a, b) in enumerate(steps)
        )
        arrows = "".join(
            f'<div id="fa{i}" style="left:{650 + i * 560}px;top:290px;font-size:56px;color:#f0b45a">→</div>'
            for i in range(2)
        )
        W0 = 1100  # 96 時間の棒の長さ
        body = f"""
<div id="k" class="kicker" style="left:160px;top:80px">結果</div>
<div id="h" class="head" style="left:160px;top:124px">過去の記録で、腕試し</div>
{flow}{arrows}
<div id="cap" style="left:160px;top:470px;font-size:34px;font-weight:700">秋の 4 日：ちょうどいい空気だった時間<span class="mut" style="font-size:24px;font-weight:400">（96 時間のうち）</span></div>
<div id="lh" style="left:160px;top:560px;font-size:36px;font-weight:700;width:120px">人</div>
<div id="bh" style="left:300px;top:552px;height:64px;width:0;background:#8fc09f;border-radius:0 12px 12px 0"></div>
<div id="vh" class="num" style="top:560px;font-size:36px;font-weight:700">{hu:g} 時間</div>
<div id="la" style="left:160px;top:670px;font-size:36px;font-weight:700;width:120px;color:#f0b45a">AI</div>
<div id="ba" style="left:300px;top:662px;height:64px;width:0;background:#f0b45a;border-radius:0 12px 12px 0"></div>
<div id="va" class="num" style="top:670px;font-size:36px;font-weight:700;color:#f0b45a">{ai:g} 時間</div>
<div id="plus" class="chip amber" style="left:1500px;top:600px;font-size:44px;font-weight:700">＋{ai - hu:.1f} 時間</div>
<div id="su" class="chip red" style="left:160px;top:790px;font-size:30px">夏は、まだ人に届かない<span class="mut" style="font-size:24px">（人 {sh:g}・AI {sa:g} 時間）</span></div>
<div id="nt" class="mut" style="left:1000px;top:808px;font-size:20px;width:780px">腕試し＝本物の記録に、加湿器の差だけを模擬の式で足した部屋</div>
"""
        js = f"""
const W0 = {W0}, HU = {hu}, AI = {ai};
function bar(id, vid, v, k) {{
  const ww = W0 * v / 96 * EIO(k);
  $(id).style.width = ww + "px";
  pos(vid, 300 + ww + 24, parseFloat($(vid).style.top)); $(vid).style.opacity = P(k, .6, .4);
}}
function render(t) {{
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6)));
  const t0 = c(0);
  for (let i = 0; i < 3; i++) fade("#f" + i, BK(P(t, t0 + .2 + i * .6, .5)), 16, .92);
  for (let i = 0; i < 2; i++) fade("#fa" + i, E(P(t, t0 + .5 + i * .6, .4)), 0);
  const tb = c(1) - .3;
  fade("#cap", E(P(t, tb, .5))); fade("#lh", E(P(t, tb, .4))); fade("#la", E(P(t, tb + .2, .4)));
  bar("#bh", "#vh", HU, P(t, tb + .2, 1.2));
  bar("#ba", "#va", AI, P(t, w(1, "人より") - .1, 1.4));
  fade("#plus", BK(P(t, w(1, "保てた") - .2, .5)), 12, .9);
  fade("#su", E(P(t, c(2) - .2, .5)), 10); fade("#nt", E(P(t, c(2) + .2, .5)));
}}"""
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
  fade("#n0", BK(P(t, c(0) - .1, .6)), 30, .92);
  fade("#n1", BK(P(t, w(0, "水やり") - .1, .6)), 30, .92);
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
        "A": pA,
        "①": pUI,
        "②": p1,
        "③": p2,
        "④": p4,
        "⑤": pWH,
        "⑥": p3,
        "⑦": p5,
        "⑧": p6,
        "⑨": p7,
        "⑩": p8,
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


def page_clip(name: str) -> dict | None:
    """opening.json の name（"ending"・"artifact"）の実写を、画面の中で送れる mp4（音なし）にする。"""
    if not OPENING.exists():
        return None
    e = json.loads(OPENING.read_text()).get(name)
    if not e:
        return None
    e["src"] = ROOT / "video" / "assets" / e["clip"]
    src = sdr(e["src"])
    mp4 = src.with_suffix(".mp4")
    if not mp4.exists() or mp4.stat().st_mtime < src.stat().st_mtime:
        ff("-i", str(src), "-an", "-c:v", "copy", "-movflags", "+faststart", str(mp4))
    e["uri"] = mp4.as_uri()
    if "to" in e:
        e["len"] = e["to"] - e["from"]
    return e


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


def timeline(scenes, keys, args, use_aivis, opening, artifact=None):
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
        if key == "A":  # 言葉なしの実写（札だけ）
            d = round(artifact["len"] * args.fps) / args.fps
            artifact["at"] = t0
            plan.append((key, [], d, t0))
            t0 += d
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


def clip_gain(c: dict) -> str:
    """実写の音の大きさ。after があれば、その秒から 0.6 秒かけて下げる（上げる）。"""
    g0 = 10 ** (c.get("gain_db", 0) / 20)
    if "after" not in c:
        return f"volume={g0:.4f}"
    at, db = c["after"]
    g1 = 10 ** (db / 20)
    return f"volume='{g0:.4f}+({g1:.4f}-{g0:.4f})*clip((t-{at})/0.6,0,1)':eval=frame"


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
        # loudnorm の後は時刻を振り直す（そのままだと amix で実写の音が 1 秒ほど遅れて混ざる）
        "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB[vn]"
    )
    n = len(voices)
    for j, c in enumerate(clips):
        ins += ["-i", str(c["src"])]
        ms = int(c["at"] * 1000)
        flt.append(
            f"[{n + j}:a:0]atrim={c['from']}:{c['to']},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=channel_layouts=stereo,{clip_gain(c)},"
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
    ap.add_argument("--speed", type=float, default=1.05)
    ap.add_argument("--bgm-db", type=float, default=-12.0, help="BGM の大きさ（声の無い所・dB）")
    ap.add_argument("--no-bgm", action="store_true")
    args = ap.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    use_aivis = aivis_ok()
    print("声：", "AivisSpeech" if use_aivis else "say（AivisSpeech が動いていない）")
    scenes = read_script()
    rec = load_records()
    rec["ending"] = page_clip("ending")
    rec["artifact"] = page_clip("artifact")
    pages = scene_pages(rec)
    keys = [k for k in ORDER if not args.only or k in args.only]
    opening = opening_parts(args.fps) if "O" in keys else None
    if not rec["artifact"]:
        keys = [k for k in keys if k != "A"]
    plan, voices, total = timeline(scenes, keys, args, use_aivis, opening, rec["artifact"])

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
            pg.wait_for_function(
                "!document.querySelector('#plugState') ||"
                " document.querySelector('#plugState').textContent.trim() !== '—'",
                timeout=15000,
            )
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
        marks = [t0 for k, _, _, t0 in plan if k != "O"]  # A の頭（①の前）で長調に移る
        turn = [p["at"] for p in (opening or []) if p.get("turn")]
        if turn and len(marks) > 1:
            marks[1] = turn[0]
        subprocess.run(
            ["uv", "run", "python", str(ROOT / "video" / "bgm.py"), f"{total:.3f}", str(bgm)]
            + [f"{m:.3f}" for m in marks],
            check=True,
        )
    clips = [p for p in (opening or []) if "clip" in p]
    if "A" in keys:
        clips.append(rec["artifact"])
    narration(voices, total, audio, bgm, args.bgm_db, clips)
    out = OUT / ("part.mp4" if args.only else "draft.mp4")
    ff("-i", str(video), "-i", str(audio), "-c:v", "copy", "-c:a", "copy", "-shortest", str(out))
    print("\n場面ごとの秒：", "・".join(f"{k} {d:.1f}" for k, _, d, _ in plan))
    body = sum(d for k, _, d, _ in plan if k != "O")  # A を含む
    op = sum(p["len"] for p in opening) if opening else 35.0
    print(f"冒頭の外 {body:.1f} 秒＋冒頭 {op:.1f} 秒＝{body + op:.1f} 秒（3 分＝180 秒）")
    print(f"出来上がり：{out}")


if __name__ == "__main__":
    main()
