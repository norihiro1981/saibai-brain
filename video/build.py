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
ORDER = "⓪O①②③④⑤⑥⑦"  # O＝冒頭の実写の札
AIVIS = "http://127.0.0.1:10101"
VOICE_ID = 1937616896  # にせ（低めで落ち着いた講義調・ACML 1.0＝商用可・クレジット任意）
LEAD, GAP = 0.5, 0.45  # 場面の頭の間・行と行の間
TAIL = {"⓪": 1.6, "⑦": 4.0}  # 場面の後ろの間（ほかは 0.8）
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
        m = re.match(r"## ([⓪①②③④⑤⑥⑦])", line)
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
    (BUILD / "records.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


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
window.__frame = t => {
  if (!__init) { __init = true; M = $("main"); if (M) { M.style.transform = "none"; } if (typeof setup === "function") setup(); }
  subs(t); if (typeof render === "function") render(t);
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
    cost = rec["cost"]

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

    # ⓪ 課題 ------------------------------------------------------------
    def p0(cues, total):
        body = """
<div id="k0" class="kicker" style="left:160px;top:190px">仕事として主に農業をしている人（基幹的農業従事者）</div>
<div id="yr" class="num" style="left:160px;top:262px;font-size:40px;color:#98a79d">2000 年</div>
<div id="ghost" style="left:160px;top:340px;width:1400px;height:110px;border:3px dashed #3a4a40;border-radius:12px"></div>
<div id="bar" style="left:160px;top:340px;width:1400px;height:110px;border-radius:12px;background:linear-gradient(90deg,#8fc09f,#5f8f70)"></div>
<div id="num" style="left:160px;top:470px;font-size:120px;font-weight:700;line-height:1"><span id="n" class="num">240.0</span><span style="font-size:52px;margin-left:12px">万人</span></div>
<div id="half" style="left:1000px;top:500px;font-size:40px;color:#f0b45a;font-weight:700">25 年で半分以下</div>
<div id="age" style="left:160px;top:640px;font-size:50px;font-weight:700">平均 <span id="an" class="num" style="font-size:96px">60.0</span> 歳</div>
<div id="old" style="left:720px;top:680px;font-size:36px;color:#98a79d">65 歳以上が 7 割</div>
<div id="moon" style="left:1560px;top:110px;width:120px;height:120px;border-radius:50%;box-shadow:-26px 14px 0 0 #f3e6c4"></div>
<div id="src" class="mut" style="left:160px;top:860px;font-size:22px">出典：令和 7 年版 食料・農業・農村白書（農林水産省）・2025 年 103.6 万人・平均 67.7 歳</div>
"""
        js = """
function render(t) {
  const k = E(P(t, .1, .8));
  ["#k0", "#yr", "#ghost", "#bar", "#num", "#src"].forEach(s => fade(s, k));
  const sh = EIO(P(t, c(0) + .5, ce(0) - c(0) - .3));
  $("#bar").style.width = L(1400, 1400 * 103.6 / 240, sh) + "px";
  $("#n").textContent = L(240, 103.6, sh).toFixed(1);
  $("#yr").textContent = (sh < 1 ? Math.round(L(2000, 2025, sh)) : 2025) + " 年";
  fade("#half", E(P(t, w(0, "半分"), .6)), 16);
  const a = E(P(t, c(1), 1.4));
  fade("#age", E(P(t, c(1) - .1, .5)));
  $("#an").textContent = L(60, 67.7, a).toFixed(1);
  fade("#old", E(P(t, c(1) + 1.3, .6)), 16);
  const dim = 1 - .7 * P(t, w(1, "それでも"), .9);
  $("#v-layer").style.opacity = dim;
  fade("#moon", E(P(t, w(1, "夜"), 1.0)), 0, .8);
  $("#moon").style.opacity = E(P(t, w(1, "夜"), 1.0)) / dim;
  $("#v-black").style.opacity = P(t, T - 1.3, 1.1);
}"""
        return plain(body, js, cues, total, {})

    # 冒頭の札 ------------------------------------------------------------
    def p_open(cues, total):
        body = """<div style="left:0;top:420px;width:1920px;text-align:center;font-size:44px;color:#e4ebe5">冒頭：実写（作り手）約 35 秒
<div style="font-size:28px;color:#98a79d;margin-top:14px">暗がりで加湿器を入れる → 昼の手作業 → 暗転 → SwitchBot でひとりでに入る</div></div>"""
        return plain(body, "function render(t){}", cues, total, {})

    # 冒頭の題・暗転 ------------------------------------------------------------
    def p_title(cues, total):
        body = """<div id="ttl" style="left:0;top:400px;width:1920px;text-align:center">
<div style="font-size:104px;font-weight:700;letter-spacing:.14em">栽培室の頭脳</div></div>"""
        js = 'function render(t){ fade("#ttl", E(P(t, .2, .8)) * (1 - P(t, T - .6, .5)), 12); }'
        return plain(body, js, cues, total, {})

    def p_black(cues, total):
        return plain("", "function render(t){}", cues, total, {})

    # ① 種明かしと問い --------------------------------------------------------
    def p1(cues, total):
        body = """
<div id="who" class="kicker" style="left:1200px;top:230px">決めているのは</div>
<div id="b0" class="chip" style="left:1200px;top:290px;width:620px"><b style="font-size:34px">Sonnet 5.5</b>　分析役</div>
<div id="b1" class="chip amber" style="left:1200px;top:400px;width:620px"><b style="font-size:34px">Opus 5.5</b>　制御役</div>
<div id="b2" class="chip" style="left:1200px;top:510px;width:620px"><b style="font-size:34px">Haiku 5.5</b>　専門家</div>
<div id="nt" class="mut" style="left:1200px;top:630px;font-size:24px">タイマーではなく、3 体の Claude のエージェント</div>
<div id="dim" style="left:0;top:0;width:1920px;height:1080px;background:#0d1310;opacity:0"></div>
<div id="q" class="head" style="left:0;top:330px;width:1920px;text-align:center;font-size:92px;line-height:1.35">機械を AI に任せて、<br>大丈夫？</div>
"""
        js = """
let MW;
function setup() { MW = M.getBoundingClientRect().width; }
function render(t) {
  const s = L(.8, 1, E(P(t, 0, 1.4)));
  const mv = EIO(P(t, c(0) + .6, 1.1));
  cam(L((1920 - MW * s) / 2, 70, mv), L(60, 40, mv), s);
  M.style.opacity = E(P(t, 0, .6));
  fade("#who", E(P(t, w(0, "タイマー"), .5)));
  for (let i = 0; i < 3; i++) fade("#b" + i, BK(P(t, w(0, "3 体") + i * .22, .55)), 30, .9);
  fade("#nt", E(P(t, w(0, "Claude"), .6)));
  $("#dim").style.opacity = .82 * E(P(t, c(1) - .1, .6));
  fade("#q", BK(P(t, c(1) + .15, .7)), 20, .92);
}"""
        return dash(rec, body, js, cues, total, {})

    # ② 答え 1：少しずつ任せる ----------------------------------------------------
    def p2(cues, total):
        steps = ["手動", "定時", "長さだけ AI", "オート"]
        say = ["手動", "定時", "長さだけ", "オート"]
        ladder = "".join(
            f'<div id="st{i}" style="left:{1200 + i * 160}px;top:{700 - (100 + i * 90)}px;width:146px;height:{100 + i * 90}px;'
            f"border-radius:12px;background:#1d2721;border:2px solid #34443a;display:flex;align-items:flex-start;justify-content:center;"
            f'padding-top:14px;font-size:26px;font-weight:700;text-align:center;line-height:1.25">{s}</div>'
            for i, s in enumerate(steps)
        )
        body = f"""
<div id="k" class="kicker" style="left:1200px;top:110px">答え 1</div>
<div id="h" class="head" style="left:1200px;top:156px">少しずつ任せる</div>
{ladder}
<div id="exp" class="card" style="left:1200px;top:740px;width:630px;height:110px;padding:18px 26px;font-size:30px;font-weight:700">経験<span id="plus" class="num" style="margin-left:18px;color:#f0b45a">＋1 回</span>
<div class="mut" style="font-size:20px;font-weight:400">入れる前と後の VPD の動き</div></div>
<div id="cur" style="width:44px;height:44px;border-radius:50%;border:4px solid #f0b45a;background:rgba(240,180,90,.25)"></div>
<div id="rec" class="chip amber" style="width:560px;font-size:24px">手動・入・30 分（その時の温湿度と VPD）</div>
"""
        js = f"""
const SAY = {json.dumps(say, ensure_ascii=False)};
const ROWS = [".controls .mode:nth-of-type(1)", ".controls .mode:nth-of-type(2)", "label[for=timerAi]", ".controls .mode:nth-of-type(3)"];
let O;
function setup() {{ O = off(".controls"); }}
function render(t) {{
  cam(70, L(40, 70 - O.y, EIO(P(t, .2, 1.4))));
  fade("#k", E(P(t, c(0), .5))); fade("#h", E(P(t, c(0) + .15, .6)));
  let lit = -1;
  for (let i = 0; i < 4; i++) {{
    const ti = w(1, SAY[i]), k = BK(P(t, ti - .1, .5));
    fade("#st" + i, Math.max(.25, E(P(t, c(0) + .4 + i * .12, .5)) * .35) + 0 * k, 0);
    const el = $("#st" + i);
    el.style.opacity = L(.3, 1, k) * E(P(t, c(0) + .3, .5));
    el.style.background = k > .01 ? `linear-gradient(180deg, rgba(240,180,90,${{.85 * k}}), rgba(143,192,159,${{.55 * k}}))` : "#1d2721";
    el.style.color = k > .5 ? "#0d1310" : "#e8efe9";
    el.style.transform = `translateY(${{(1 - k) * 14}}px)`;
    if (t >= ti - .1) lit = i;
  }}
  ROWS.forEach((r, i) => hl(r, i === lit && t < w(1, "人が押した")));
  fade("#exp", E(P(t, w(1, "人が押した") - .3, .5)));
  const tp = w(1, "人が押した"), b = center("#btnOn");
  const kc = P(t, tp, .3);
  const cu = $("#cur"); pos(cu, b.x - 22, b.y - 22);
  cu.style.opacity = kc * (1 - P(t, tp + .9, .3)); cu.style.transform = `scale(${{L(1.6, 1, E(kc))}})`;
  hl("#btnOn", t > tp && t < tp + 1.0);
  const ex = $("#exp").getBoundingClientRect();
  fly("#rec", {{ x: b.x + 150, y: b.y + 60 }}, {{ x: ex.left + 40, y: ex.top - 70 }}, P(t, tp + .5, 1.3));
  const arrive = P(t, tp + 1.8, .4);
  $("#rec").style.opacity = cl(P(t, tp + .5, .25)) * (1 - arrive);
  fade("#plus", BK(arrive), 10, .6);
  $("#exp").style.borderColor = arrive > 0 && arrive < 1 ? "#f0b45a" : "#2e3a32";
}}"""
        return dash(rec, body, js, cues, total, {})

    # ③ 答え 2：根拠 ----------------------------------------------------------
    def p3(cues, total):
        X = 1180
        body = f"""
<div id="k" class="kicker" style="left:{X}px;top:60px">答え 2</div>
<div id="h" class="head" style="left:{X}px;top:100px;font-size:50px">根拠を持って決める</div>
<div id="cap" class="mut" style="left:{X}px;top:186px;font-size:24px">11/5 15:04 の回（本物の答え）</div>
<div id="c1" class="chip" style="width:680px">14:54 の VPD <b class="num">0.851</b> kPa<span class="mut">（帯 0.4〜0.8 の上）</span></div>
<div id="c2" class="chip" style="width:680px">経験：秋の傾向 <b class="num">26</b> 回・15 分で <b class="num">−0.238</b></div>
<div id="barbox" style="left:{X}px;top:470px;width:680px;height:96px">
  <div id="bar" style="position:absolute;left:0;top:0;height:96px;width:680px;border-radius:12px;background:#2a3a30;border:2px solid #4a5c50"></div>
  <div id="barl" style="position:absolute;left:24px;top:22px;font-size:34px;font-weight:700;white-space:nowrap">定時の 60 分</div>
</div>
<div id="c3" class="chip" style="width:680px">夜は雨の予報<span class="mut">（18 時の降水確率 60%）</span></div>
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
  const tv = w(2, "VPD") - .7, ta = w(2, "秋の") - .6, td = w(2, "だから") - .5;
  cam(70, track(t, [[0, yT], [tv, yG], [ta, yE], [td, yT]]));
  fade("#k", E(P(t, c(0), .5))); fade("#h", E(P(t, c(0) + .15, .6))); fade("#cap", E(P(t, c(0) + .5, .6)));
  hl(".controls .mode:nth-of-type(2)", t > c(1) && t < tv);
  hl("label[for=timerAi]", t > w(1, "何分") && t < tv);
  hl(".gauge.vpd", t > w(2, "VPD") - .2 && t < ta);
  hl("#expTable tbody tr:nth-child(2)", t > w(2, "26") - .3 && t < td);
  hl("#timerWhy", t > td + .9);
  const g = center(".gauge.vpd");
  fly("#c1", g, {{ x: X, y: 230 }}, P(t, w(2, "0.851"), 1.0));
  const r = center("#expTable tbody tr:nth-child(2)");
  fly("#c2", r, {{ x: X, y: 350 }}, P(t, w(2, "26"), 1.0));
  fade("#barbox", E(P(t, td - .2, .5)));
  const sh = EIO(P(t, w(2, "20"), .9));
  $("#bar").style.width = L(680, 680 * 20 / 60, sh) + "px";
  $("#bar").style.background = sh > 0 ? `rgba(240,180,90,${{L(.15, .9, sh)}})` : "#2a3a30";
  $("#barl").textContent = sh > .5 ? "AI の分：20 分" : "定時の 60 分";
  $("#barl").style.left = (sh > .5 ? 680 * 20 / 60 + 24 : 24) + "px";
  fade("#c3", E(P(t, w(3, "天気"), .6))); pos("#c3", X, 600);
  fade("#c4", E(P(t, w(3, "経験に無い") - .3, .6))); pos("#c4", X, 720);
  $("#mk").style.setProperty("--m", (100 * E(P(t, w(3, "推測") - .1, .7))) + "%");
}}"""
        return dash(rec, body, js, cues, total, {}, patch=timer_patch)

    # ④ 3 体のチーム ------------------------------------------------------------
    def p4(cues, total):
        lock = (
            '<svg width="30" height="34" viewBox="0 0 30 34" style="vertical-align:-5px;margin-right:8px">'
            '<rect x="3" y="14" width="24" height="18" rx="4" fill="#f0b45a"/>'
            '<path d="M8 14V10a7 7 0 0 1 14 0v4" stroke="#f0b45a" stroke-width="4" fill="none"/></svg>'
        )

        def card(i, x, model, role, sess):
            return (
                f'<div id="a{i}" class="card" style="left:{x}px;top:300px;width:440px;height:250px">'
                f'<div class="mut" style="font-size:24px">{sess}</div>'
                f'<div style="font-size:52px;font-weight:700;margin:6px 0 4px">{role}</div>'
                f'<div class="num" style="font-size:30px;color:#8fc09f">{model}</div>'
                f'<div id="lk{i}" style="position:absolute;left:30px;top:196px;font-size:24px;color:#f0b45a">{lock}読むだけ</div></div>'
            )

        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">オートの中身</div>
<div id="h" class="head" style="left:140px;top:126px">3 体のチーム</div>
<div id="clk" style="left:1560px;top:80px;width:150px;height:150px;border-radius:50%;border:5px solid #8fc09f">
  <div id="hand" style="position:absolute;left:69px;top:16px;width:6px;height:58px;background:#f0b45a;border-radius:3px;transform-origin:3px 58px"></div>
</div>
<div id="clkl" style="left:1730px;top:130px;font-size:30px;font-weight:700">3 時間ごと</div>
{card(0, 140, "Sonnet 5.5", "分析役", "振り返りのセッション")}
{card(1, 740, "Opus 5.5", "制御役", "計画のセッション")}
{card(2, 1340, "Haiku 5.5", "専門家", "栽培の知識")}
<div id="ar1" style="left:590px;top:420px;width:140px;height:6px;background:#8fc09f;border-radius:3px"></div>
<div id="ask" class="chip" style="left:1190px;top:330px;width:140px;padding:8px 12px;font-size:22px;text-align:center">聞く →</div>
<div id="ans" class="chip" style="left:1190px;top:450px;width:140px;padding:8px 12px;font-size:22px;text-align:center">← 答え</div>
<div id="book" class="card" style="left:140px;top:600px;width:440px;height:200px">
  <div style="font-size:32px;font-weight:700">経験</div>
  <div class="mut" style="font-size:22px">操作の前後で VPD がどう動いたか</div>
  <div id="l0" style="position:absolute;left:30px;top:122px;height:6px;background:#8fc09f;border-radius:3px"></div>
  <div id="l1" style="position:absolute;left:30px;top:146px;height:6px;background:#8fc09f;border-radius:3px"></div>
  <div id="l2" style="position:absolute;left:30px;top:170px;height:6px;background:#8fc09f;border-radius:3px"></div>
</div>
<div id="rd" style="left:590px;top:690px;width:150px;border-top:4px dashed #f0b45a"></div>
<div id="plan" class="chip amber" style="left:760px;top:620px;width:400px;font-size:30px;font-weight:700">計画 → 守りの層へ</div>
<div id="cost" style="left:1340px;top:620px;width:440px">
  <div style="font-size:26px" class="mut">1 回の起動</div>
  <div style="font-size:64px;font-weight:700">約 <span id="cn" class="num">0</span>¢</div>
  <div class="mut" style="font-size:20px">振り返り {cost["review"]:g}¢＋計画 {cost["plan"]:g}¢（11/4 の記録）</div>
</div>
<div id="nt" class="mut" style="left:140px;top:850px;font-size:20px">図：本番は Claude Console のセッションの録画を重ねる</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#nt", E(P(t, .6, .6)));
  for (let i = 0; i < 3; i++) fade("#a" + i, E(P(t, .3 + i * .15, .6)), 30);
  const t3 = w(0, "3 時間ごと");
  fade("#clk", E(P(t, t3 - .3, .4)), 0, .8); fade("#clkl", E(P(t, t3 - .2, .4)));
  $("#hand").style.transform = `rotate(${90 * EIO(P(t, t3, 1.2))}deg)`;
  const glow = (i, a, b) => { $("#a" + i).style.borderColor = t > a && t < b ? "#f0b45a" : "#2e3a32"; };
  const ts = w(0, "Sonnet"), tw = w(0, "経験を書き直す");
  glow(0, ts - .1, c(1));
  fade("#book", E(P(t, ts + .3, .5)));
  [0, 1, 2].forEach(i => { $("#l" + i).style.width = (380 * [1, .8, .6][i] * EIO(P(t, tw + i * .35, .5))) + "px"; });
  const ta = c(1);
  $("#ar1").style.opacity = E(P(t, ta - .2, .4)); $("#ar1").style.transformOrigin = "0 0";
  $("#ar1").style.transform = `scaleX(${EIO(P(t, ta - .2, .6))})`;
  glow(1, ta, ce(1) + .3);
  const th = w(1, "Haiku");
  fade("#ask", E(P(t, th, .4)), 0); fade("#ans", E(P(t, th + .8, .4)), 0);
  glow(2, th + .2, th + 1.4);
  fade("#plan", BK(P(t, w(1, "計画"), .6)), 20, .9);
  const tl = c(2);
  fade("#rd", E(P(t, tl, .5)), 0);
  fade("#lk0", 0); fade("#lk1", BK(P(t, tl + .4, .5)), 8, .6); fade("#lk2", BK(P(t, tl + .6, .5)), 8, .6);
  glow(0, w(2, "分析役") - .1, w(2, "1 回"));
  const tc = w(2, "1 回");
  fade("#cost", E(P(t, tc - .3, .5)));
  $("#cn").textContent = Math.round(L(0, 10, E(P(t, tc, 1.0))));
}"""
        return plain(body, js, cues, total, {})

    # ⑤ 答え 3：越えられない壁 -----------------------------------------------------
    def p5(cues, total):
        tail = rec["pytest"][-1:]
        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">答え 3</div>
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
    <div style="font-size:44px;font-weight:700">プラグ</div><div class="mut" style="font-size:24px">SwitchBot → 加湿器</div></div>
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
  // 上限を越える計画が当たって跳ね返る
  const ta = w(1, "計画は") + .2, hit = ta + 1.2;
  const a = P(t, ta, 1.2);
  const shake = t > hit && t < hit + .5 ? Math.sin((t - hit) * 60) * 10 * (1 - (t - hit) / .5) : 0;
  pos("#pa", L(470, 560, EIO(a)) + shake, 300); $("#pa").style.opacity = cl(a * 5) * (1 - P(t, w(1, "断られたら") + .2, .3));
  $("#wall").style.borderColor = t > hit && t < w(1, "返ります") + .3 ? "#ef7d6f" : (t > w(1, "返ります") + 1.4 && t < w(1, "返ります") + 2.4 ? "#6cc28b" : "#2e3a32");
  const tn = w(1, "断られたら");
  const n = P(t, tn, 1.1);
  pos("#no", L(780, 300, EIO(n)), 560); $("#no").style.opacity = cl(n * 5) * (1 - P(t, c(2) + .4, .4));
  // 直した計画が壁を抜けてプラグへ
  const tb = w(1, "返ります") + .5, b = P(t, tb, 1.8);
  pos("#pb", L(470, 1500, EIO(b)), 300); $("#pb").style.opacity = cl(b * 5) * (1 - P(t, tb + 2.2, .3));
  const on = P(t, tb + 1.8, .3);
  $("#plug").style.borderColor = on > 0 ? "#6cc28b" : "#2e3a32";
  $("#plug").style.boxShadow = `0 0 ${60 * on}px rgba(108,194,139,${.6 * on})`;
  // ランダムなテスト
  const tt = Math.max(c(2) + .6, tb + 2.2);
  $("#dia").style.opacity = 1 - .8 * P(t, tt, .5);
  fade("#term", E(P(t, tt, .6)), 40);
  $("#rn").textContent = Math.round(L(0, 120, EIO(P(t, tt + .6, 2.2))));
  fade("#pt", E(P(t, tt + 2.9, .4)), 8);
}"""
        return plain(body, js, cues, total, {})

    # ⑥ 誰に効くか ------------------------------------------------------------
    def p6(cues, total):
        dots = "".join(
            f'<div class="dot" style="position:absolute;left:{180 + (i % 32) * 49}px;top:{560 + (i // 32) * 49}px;width:22px;height:22px;border-radius:50%;background:#26302a"></div>'
            for i in range(32 * 6)
        )
        parts = [
            ("ラズパイ", "温湿度を記録する"),
            ("SwitchBot のプラグ", "加湿器を入れる・切る"),
            ("Claude の API", "1 回 10〜19¢ × 1 日 8 回"),
        ]
        cards = "".join(
            f'<div id="p{i}" class="card" style="left:{140 + i * 560}px;top:300px;width:520px;height:190px">'
            f'<div style="font-size:42px;font-weight:700">{a}</div><div class="mut" style="font-size:26px;margin-top:10px">{b}</div></div>'
            for i, (a, b) in enumerate(parts)
        )
        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">誰に効くか</div>
<div id="h" class="head" style="left:140px;top:126px">要るのは、これだけ</div>
{cards}
<div id="tag" style="left:1380px;top:200px;font-size:40px;font-weight:700;color:#0d1310;background:#f0b45a;border-radius:14px;padding:10px 26px;transform-origin:50% 50%">1 日 1 ドルほど</div>
<div id="h2" class="head" style="left:140px;top:420px;font-size:50px">決まりを書けない小さな現場でも、少しずつ任せられる</div>
<div id="dots" style="left:0;top:0">{dots}</div>
"""
        js = """
function render(t) {
  const up = EIO(P(t, c(1) - .2, .8));
  fade("#k", E(P(t, .1, .5)));
  fade("#h", E(P(t, .25, .6)) * (1 - up));
  const say = ["ラズパイ", "SwitchBot", "API"];
  for (let i = 0; i < 3; i++) {
    const k = BK(P(t, w(0, say[i]) - .15, .6));
    const el = $("#p" + i);
    el.style.opacity = E(P(t, w(0, say[i]) - .15, .4)) * (1 - .7 * up);
    el.style.top = L(300, 190, up) + "px";
    el.style.transform = `translateY(${(1 - k) * 40}px) scale(${L(1, .82, up)})`;
    el.style.transformOrigin = "0 0";
  }
  const tg = BK(P(t, w(0, "1 日"), .6));
  $("#tag").style.opacity = cl(tg) * (1 - up); $("#tag").style.transform = `rotate(-4deg) scale(${L(.6, 1, tg)})`;
  fade("#h2", E(P(t, c(1), .7)));
  const t0 = w(1, "小さな現場") - .4;
  $$(".dot").forEach((d, i) => {
    const col = i % 32, row = Math.floor(i / 32);
    const k = E(P(t, t0 + col * .07 + row * .05 + ((i * 37) % 11) * .03, .5));
    d.style.background = k > 0 ? `rgba(240,180,90,${L(.15, 1, k)})` : "#26302a";
    d.style.boxShadow = `0 0 ${18 * k}px rgba(240,180,90,${.55 * k})`;
    d.style.opacity = E(P(t, c(1) + .1, .6));
  });
}"""
        return plain(body, js, cues, total, {})

    # ⑦ 締め ------------------------------------------------------------
    def p7(cues, total):
        log = rec["gitlog"]
        lines = "".join(f"<div>{esc(x[:64])}</div>" for x in log)
        body = f"""
<div id="k" class="kicker" style="left:140px;top:80px">作り方</div>
<div id="h" class="head" style="left:140px;top:126px">6 日間、Claude Code と</div>
<div id="cnt" class="mut" style="left:1100px;top:150px;font-size:28px">期間中の commit {len(log)} 回（期間は 11/2 12:00 から）</div>
<div id="term" style="left:140px;top:250px;width:1640px;height:600px;background:#070a08;border:2px solid #2e3a32;border-radius:18px;overflow:hidden">
  <div id="lines" style="position:absolute;left:40px;top:30px;font:26px/1.6 'IBM Plex Mono','Zen Kaku Gothic New',monospace;color:#cfe3d4;white-space:pre">{lines}</div>
</div>
<div id="night" class="mut" style="left:1500px;top:60px;font-size:22px;z-index:2">本番：ここで暗がりの実写に戻る</div>
<div id="ttl" style="left:0;top:380px;width:1920px;text-align:center;z-index:2">
  <div style="font-size:96px;font-weight:700;letter-spacing:.12em">栽培室の頭脳</div>
  <div class="mut" style="font-size:34px;margin-top:24px;letter-spacing:.1em">Built with Claude: Japan 2026</div>
</div>
"""
        js = """
function render(t) {
  fade("#k", E(P(t, .1, .5))); fade("#h", E(P(t, .25, .6))); fade("#cnt", E(P(t, .6, .6)));
  fade("#term", E(P(t, .3, .6)), 30);
  const el = $("#lines"), over = Math.max(0, el.offsetHeight - 540);
  el.style.transform = `translateY(${-over * EIO(P(t, .8, ce(0) - .8))}px)`;
  const blk = P(t, c(1) - .3, .7);
  $("#v-black").style.opacity = blk;
  $("#night").style.opacity = blk * (1 - P(t, ce(1) + .2, .4));
  fade("#ttl", E(P(t, ce(1) + .5, 1.0)), 16);
}"""
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
            pg.wait_for_load_state("networkidle")
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
            pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready")
            pg.wait_for_timeout(800)  # 計器盤が記録を描き終えるのを待つ
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
