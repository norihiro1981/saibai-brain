"""デモ動画の試し（冒頭の実写の後）を組み立てる。

使い方：uv run python video/build.py [--opening 秒]

- ナレーションは video/script.md の各場面の「> 」の行（1 行＝字幕 1 枚）。
- 計器盤は artifact/status.html に、受け手の今の /status・/history（127.0.0.1 の読むだけの口）と
  試しの記録（spikes/logs・run）を流し込み、Chrome の headless で撮る。プラグも API も叩かない。
- 声は macOS の say（仮の声）。つなぐのは ffmpeg。
- 出来上がりは video/out/draft.mp4、途中の物は video/assets/build/（どちらも git に入らない）。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "video" / "assets" / "build"
OUT = ROOT / "video" / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VOICE = "Kyoko"
RATE = 190  # 読み上げの速さ（say の -r）
GAP_S = 0.35  # 字幕と字幕の間
W, H = 1920, 1080

# 読み上げだけ直す語（字幕はそのまま）
READ = {
    "Claude Code": "クロード・コード",
    "Claude": "クロード",
    "Sonnet": "ソネット",
    "Opus": "オーパス",
    "Haiku": "ハイク",
    "VPD": "ブイピーディー",
    "API": "エーピーアイ",
    "Python": "パイソン",
    "AI": "エーアイ",
}


# ---------------------------------------------------------------- 台本


def read_script() -> dict[str, list[str]]:
    """script.md の「## ①…」の場面ごとに、ナレーションの行を返す。"""
    scenes: dict[str, list[str]] = {}
    cur = None
    for line in (ROOT / "video" / "script.md").read_text().splitlines():
        m = re.match(r"## ([①-⑦])", line)
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


def load_records() -> dict:
    status = get_json("/status")
    history = get_json("/history?hours=24")
    timer_ai = next(
        d
        for d in jsonl(ROOT / "spikes/logs/timer_run/auto_20261105.jsonl")
        if d.get("kind") == "timer_ai"
    )
    auto_run = next(
        d for d in jsonl(ROOT / "run/auto_20261104.jsonl") if d.get("kind") == "auto_run"
    )
    refused = [
        d
        for f in sorted((ROOT / "run").glob("control_*.jsonl"))
        for d in jsonl(f)
        if d.get("kind") == "action" and d.get("accepted") is False
    ]
    pytest = (
        subprocess.run(
            ["uv", "run", "pytest", "-q", "tests/test_guard.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    gitlog = subprocess.run(
        ["git", "log", "--reverse", "--format=%ad  %s", "--date=format:%m/%d %H:%M"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    rec = dict(
        status=status,
        history=history,
        timer_ai=timer_ai,
        auto_run=auto_run,
        refused=refused,
        pytest=pytest,
        gitlog=gitlog,
    )
    (BUILD / "records.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


# ---------------------------------------------------------------- 絵


BASE_CSS = """
.v-sub { position: fixed; left: 0; right: 0; bottom: 0; height: 150px; display: flex; align-items: center; justify-content: center;
  background: rgba(15,20,17,.86); color: #fff; font: 700 44px/1.35 "Hiragino Sans", sans-serif; letter-spacing: .02em;
  padding: 0 80px; text-align: center; z-index: 99; text-wrap: balance; }
.v-side { position: fixed; top: 60px; right: 70px; width: 640px; z-index: 98; display: grid; gap: 18px;
  font-family: "Hiragino Sans", sans-serif; color: #1d2620; }
.v-side .v-h { font: 700 30px/1.3 "Hiragino Sans", sans-serif; color: #3f6b4e; letter-spacing: .06em; }
.v-side .v-c { background: #fff; border: 2px solid #d5ddd6; border-left: 8px solid #3f6b4e; border-radius: 10px;
  padding: 16px 22px; font-size: 26px; line-height: 1.55; }
.v-side .v-c.hot { border-color: #b23b2e; border-left-color: #b23b2e; }
.v-side .v-c.dim { opacity: .35; }
.v-note { position: fixed; top: 18px; right: 70px; font: 500 20px "Hiragino Sans", sans-serif; color: #a86a12; z-index: 99; }
"""

DASH_CSS = """
html, body { overflow: hidden !important; height: 1080px; }
body { padding: 20px 0 0 50px !important; margin: 0; }
main { zoom: 1.32; margin: 0; max-width: 760px !important; }
.v-hl { outline: 4px solid #e2a64a; outline-offset: 4px; border-radius: 8px; box-shadow: 0 0 0 12px rgba(226,166,74,.18); }
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


def side(h: str, cards: list[tuple[str, str]]) -> str:
    body = "".join(f'<div class="v-c {cls}">{c}</div>' for c, cls in cards)
    return f'<div class="v-side"><div class="v-h">{html.escape(h)}</div>{body}</div>'


def dash_page(
    rec: dict, sub: str, *, status_patch=None, highlight=(), scroll_to=None, side_html="", note=""
) -> str:
    status = json.loads(json.dumps(rec["status"]))
    if status_patch:
        status_patch(status)
    src = (ROOT / "artifact" / "status.html").read_text()
    src = src.replace("<html", "<html data-theme=light", 1) if "<html" in src else src
    src = src.replace(
        '<meta charset="utf-8">', '<meta charset="utf-8">' + shim(status, rec["history"]), 1
    )
    hl = "".join(f'document.querySelector("{q}")?.classList.add("v-hl");' for q in highlight)
    # スクロールの代わりに body の上の余白で寄せる（目当ての物の上端を画面の 380px に）
    sc = (
        (
            f'{{ const e = document.querySelector("{scroll_to}"); if (e) {{ const top = e.getBoundingClientRect().top; '
            f'if (top > 380) document.querySelector("main").style.marginTop = (-(top - 380) / 1.32) + "px"; }} }}'
        )
        if scroll_to
        else ""
    )
    extra = f"""
<style>{BASE_CSS}{DASH_CSS}</style>
{side_html}
{f'<div class="v-note">{html.escape(note)}</div>' if note else ""}
<div class="v-sub">{html.escape(sub)}</div>
<script>document.documentElement.dataset.theme = "light";
setTimeout(() => {{ {hl} {sc} }}, 400);</script>"""
    return src.replace("</main>", "</main>" + extra, 1)


def plain_page(sub: str, body: str, note: str = "") -> str:
    return f"""<!DOCTYPE html><meta charset="utf-8"><style>
body {{ margin: 0; width: {W}px; height: {H}px; background: #f3f5f1; color: #1d2620; font-family: "Hiragino Sans", sans-serif; overflow: hidden; }}
.stage {{ position: absolute; inset: 60px 70px 190px 70px; }}
.term {{ background: #141a16; color: #cfe3d4; border-radius: 14px; padding: 28px 34px; font: 24px/1.5 "SF Mono", Menlo, monospace; white-space: pre; overflow: hidden; max-height: 700px; box-sizing: border-box; }}
.term b {{ color: #8fc09f; font-weight: 600; }} .term i {{ color: #e2a64a; font-style: normal; }}
.flow {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 26px; align-items: stretch; }}
.box {{ background: #fff; border: 2px solid #d5ddd6; border-top: 8px solid #3f6b4e; border-radius: 12px; padding: 20px 22px; font-size: 25px; line-height: 1.55; }}
.box h4 {{ margin: 0 0 8px; font-size: 30px; }} .box small {{ color: #5d6b62; font-size: 21px; display: block; }}
.box.on {{ border-color: #e2a64a; box-shadow: 0 0 0 10px rgba(226,166,74,.18); }}
.box.guard {{ border-top-color: #b23b2e; }}
.arrow {{ font-size: 40px; color: #5d6b62; text-align: center; }}
.quote {{ margin-top: 30px; background: #fff; border-left: 8px solid #3f6b4e; border-radius: 8px; padding: 18px 26px; font-size: 25px; line-height: 1.6; }}
.title {{ font-size: 40px; font-weight: 700; margin-bottom: 24px; letter-spacing: .04em; }}
{BASE_CSS}</style>
<div class="stage">{body}</div>
{f'<div class="v-note">{html.escape(note)}</div>' if note else ""}
<div class="v-sub">{html.escape(sub)}</div>"""


def shoot(page: str, png: Path) -> None:
    src = png.with_suffix(".html")
    src.write_text(page)
    subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={W},{H}",
            "--virtual-time-budget=4000",
            f"--screenshot={png}",
            src.as_uri(),
        ],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------- 場面ごとの絵


def frames(rec: dict, scenes: dict[str, list[str]]):
    """(場面, 字幕, 絵の HTML) を順に返す。"""
    t = rec["timer_ai"]
    plan = t["plans"][0]["input"]
    reason = plan["reason"]
    ar = rec["auto_run"]
    exp = rec["status"]["auto"]["experience"]

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
                "reason": reason,
                "ok": True,
            },
        }

    # ① 種明かし
    for sub in scenes["①"]:
        yield (
            "①",
            sub,
            dash_page(
                rec,
                sub,
                side_html=side(
                    "栽培室の計器盤",
                    [
                        ("元織物工場の一室でコーヒーの幼木を育てています", ""),
                        ("温湿度は 30 秒ごと・受け手が手元に記録", ""),
                        ("Mac の Claude アプリの Artifact", ""),
                    ],
                ),
            ),
        )

    # ② 任せる 4 つの段
    steps = ["手動", "定時", "定時・長さは AI", "オート"]
    rows = [
        ".controls .mode:nth-of-type(1)",
        ".controls .mode:nth-of-type(2)",
        "#timerAi",
        ".controls .mode:nth-of-type(3)",
    ]
    subs = scenes["②"]
    for i, sub in enumerate(subs):
        k = [0, 3, 0][i] if len(subs) == 3 else min(i, 3)
        hot = rows[:4] if i == 1 else [rows[0]]
        cards = [
            (f"{'→ ' if j else ''}{s}", "hot" if (i == 1 or j == 0) else "dim")
            for j, s in enumerate(steps)
        ]
        if i == 2:
            cards.append(("操作の記録：手動・入・30 分・その時の温湿度と VPD", ""))
        yield (
            "②",
            sub,
            dash_page(
                rec, sub, highlight=hot, scroll_to=rows[k], side_html=side("任せる 4 つの段", cards)
            ),
        )

    # ③ 長さは AI
    cards3 = [
        ("14:54 の VPD 0.851 kPa（帯 0.4〜0.8 の上）", ""),
        ("経験：秋の傾向 26 回・15 分で −0.238", ""),
        ("→ 60 分では下がりすぎる → <b>20 分</b>", ""),
        ("夜は雨の予報（18 時の降水確率 60%）", ""),
        ("「外の天気がこの部屋にどれだけ効くかの行は経験に無いので、これは推測」", ""),
    ]
    for i, sub in enumerate(scenes["③"]):
        show = [
            (c, "hot" if j == min(i * 2, 4) or (i == 2 and j >= 3) else "")
            for j, (c, _) in enumerate(cards3[: [1, 3, 5][min(i, 2)]])
        ]
        yield (
            "③",
            sub,
            dash_page(
                rec,
                sub,
                status_patch=timer_patch,
                highlight=["#timerWhy"],
                scroll_to="#timerWhy",
                side_html=side("11/5 15:04 の回（本物の答え）", show),
                note="試し：計器盤に 11/5 15:04 の本物の答えを流し込んだ写し",
            ),
        )

    # ④ オートの中身
    rv = ar["review_usage"]["list_cost_cents"] if "review_usage" in ar else "6"
    pv = ar["plan_usage"]["list_cost_cents"] if "plan_usage" in ar else "11"
    boxes = [
        ("定時の起動", "毎 3 時間・東京", ""),
        (
            "振り返り",
            f"Sonnet 5.5（分析役）<small>作業場の Python で前後の VPD を計算・経験＝書ける・{rv}¢</small>",
            "",
        ),
        (
            "計画",
            f"Opus 5.5（制御役）＋ Haiku 5.5（専門家）<small>経験も記録も知識も読むだけ・{pv}¢</small>",
            "",
        ),
        ("submit_plan", "手元の受け手が受け取る<small>→ 守りの層 → プラグ</small>", ""),
    ]
    q4 = html.escape(ar["plans"][0]["input"]["reason"][:150]) + "…"
    for i, sub in enumerate(scenes["④"]):
        on = [0, 1, 2, 2][min(i, 3)]
        body = (
            '<div class="title">オートの 1 回（11/4 03:09・本物の起動の記録から作った図）</div><div class="flow">'
            + "".join(
                f'<div class="box{" on" if j == on else ""}"><h4>{h}</h4>{b}</div>'
                for j, (h, b, _) in enumerate(boxes)
            )
            + "</div>"
        )
        if i >= 3:
            body += f'<div class="quote">経験の版 {exp["versions"]}：最初は記録から計算した物（手元のコード）・後の版は振り返りのセッションが書いた</div>'
        else:
            body += f'<div class="quote">制御役の理由：{q4}</div>'
        yield (
            "④",
            sub,
            plain_page(sub, body, note="試し：本番は Claude Console のセッションの録画に替える"),
        )

    # ⑤ 守りの層
    r = rec["refused"][-1] if rec["refused"] else None
    tail = [x for x in rec["pytest"] if x.strip()][-3:]
    for i, sub in enumerate(scenes["⑤"]):
        if i < 2:
            body = (
                '<div class="title">AI は機械に触らない</div><div class="flow">'
                '<div class="box"><h4>計画</h4>手動・定時・オート<small>どれも同じ口</small></div>'
                '<div class="box guard on"><h4>守りの層</h4>決まった規則のコード<small>連続 60 分・1 日 120 分・古い値で動かさない・15 分で湿度が上がらなければ切る・止めるが最優先</small></div>'
                '<div class="box"><h4>プラグ</h4>SwitchBot<small>受け手だけが命令する</small></div>'
                '<div class="box"><h4>加湿器</h4><small>起動した時は必ず切から</small></div></div>'
            )
        elif i == 2 and r:
            body = (
                '<div class="title">断られた時は理由を返す（11/5 の本物の記録）</div><div class="term">'
                + html.escape(f"{r['at'][11:19]}  {r['who']}  {r['what']} {r['minutes']} 分\n")
                + "<i>"
                + html.escape("断った：" + "／".join(r["reasons"]))
                + "</i>\n\n"
                + html.escape(
                    "AI の計画も同じ文が道具の結果として返り、直して出し直せる\n（11/4 の試しで、制御役が理由を読んで出し直した）"
                )
                + "</div>"
            )
        else:
            body = (
                '<div class="title">どんな計画でも上限を越えない（ランダムに流すテスト）</div><div class="term">'
                "<b>$ uv run pytest -q tests/test_guard.py</b>\n"
                + html.escape("\n".join(tail))
                + "</div>"
                '<div class="quote">ランダムに 120 通り × 2 日（湿度は歩き・跳び・止まり・読めない区間・はみ出した計画も混ぜる）を、'
                "再生・実機・デモの組で流す → 連続・1 日の合計・間・止める・センサーの線を一度も越えない</div>"
            )
        yield "⑤", sub, plain_page(sub, body)

    # ⑥ 結果（仮）
    for sub in scenes["⑥"]:
        body = (
            '<div class="title">夏と秋の再生・4 通りの比較</div><div class="flow">'
            + "".join(
                f'<div class="box"><h4>{n}</h4><small>まだ</small></div>'
                for n in ["実運転", "閾値だけ", "1 体", "チーム"]
            )
            + "</div>"
        )
        yield "⑥", sub, plain_page(sub, body, note="仮：再生と評価はまだ作っていない")

    # ⑦ 作り方と締め
    log = rec["gitlog"]
    shown = log[:8] + ["…"] + log[-5:] if len(log) > 14 else log
    for i, sub in enumerate(scenes["⑦"]):
        if i == 0:
            body = (
                f'<div class="title">期間中の commit（{len(log)} 回・期間は 11/2 12:00 から）</div><div class="term">'
                + html.escape("\n".join(x[:70] for x in shown))
                + "</div>"
            )
            yield "⑦", sub, plain_page(sub, body)
        else:
            yield (
                "⑦",
                sub,
                plain_page(
                    sub,
                    '<div style="position:absolute;inset:-60px -70px -190px -70px;background:#0b0e0c"></div>'
                    '<div style="position:absolute;top:300px;width:100%;text-align:center;color:#8fc09f;font-size:30px">（冒頭の暗がりのカットに戻る）</div>',
                ),
            )


# ---------------------------------------------------------------- 声とつなぎ


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out)


def speak(text: str, path: Path) -> float:
    for a, b in READ.items():
        text = text.replace(a, b)
    text = text.replace("−", "マイナス").replace("¢", "セント")
    subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", str(path), text], check=True)
    return duration(path)


def segment(png: Path, audio: Path | None, secs: float, out: Path) -> None:
    v = ["-loop", "1", "-framerate", "30", "-i", str(png)]
    a = ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    ff(
        *v,
        *a,
        "-t",
        f"{secs:.2f}",
        "-af",
        f"volume=8dB,apad=pad_dur={GAP_S}",
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        str(out),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--opening", type=float, default=3.0, help="冒頭の実写の代わりの札の秒（本番は 45）"
    )
    ap.add_argument("--frames-only", action="store_true", help="絵だけ撮る（声とつなぎをしない）")
    args = ap.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for old in BUILD.glob("f*"):
        old.unlink()

    scenes = read_script()
    rec = load_records()
    segs, report = [], []

    card = BUILD / "f000.png"
    shoot(
        plain_page(
            "",
            '<div style="position:absolute;inset:-60px -70px -190px -70px;background:#0b0e0c"></div>'
            '<div style="position:absolute;top:380px;width:100%;text-align:center;color:#e4ebe5;font-size:44px">'
            f'冒頭：実写（門脇）約 45 秒<br><span style="font-size:28px;color:#98a79d">この試しでは {args.opening:g} 秒の札</span></div>',
        ),
        card,
    )
    seg = BUILD / "f000.mp4"
    segment(card, None, args.opening, seg)
    segs.append(seg)

    for n, (scene, sub, page) in enumerate(frames(rec, scenes), start=1):
        png, aud, mp4 = BUILD / f"f{n:03d}.png", BUILD / f"f{n:03d}.aiff", BUILD / f"f{n:03d}.mp4"
        shoot(page, png)
        if args.frames_only:
            continue
        secs = speak(sub, aud) + GAP_S
        segment(png, aud, secs, mp4)
        segs.append(mp4)
        report.append((scene, secs, len(sub)))
        print(f"{scene} {secs:5.1f} 秒  {sub}")

    if args.frames_only:
        print(f"絵だけ撮った：{BUILD}")
        return
    lst = BUILD / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segs))
    out = OUT / "draft.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))

    print("\n場面ごとの秒：")
    for s in "①②③④⑤⑥⑦":
        print(f"  {s} {sum(x[1] for x in report if x[0] == s):5.1f} 秒")
    body = sum(x[1] for x in report)
    print(f"冒頭の後 {body:.1f} 秒＋冒頭 45 秒＝本番の見込み {body + 45:.1f} 秒（3 分＝180 秒）")
    print(f"出来上がり：{out}（{duration(out):.1f} 秒）")


if __name__ == "__main__":
    main()
