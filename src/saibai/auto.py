"""オート：定時の起動（deployments）1 回＝振り返りのセッション→計画のセッション（設計書 §4）。

- 定時の起動が起こすのは振り返りのセッション（分析役・経験＝書ける・記録＝読むだけ）。
- 受け手は起動の記録（deployment_runs）を見に行き、振り返りが止まったら計画のセッション（制御役＋専門家・
  全部読むだけ）を起こす。制御役の `submit_plan` は見に行って受け取り、守りの層（Controller.submit）を通して返す。
- 外から入る口は無い（Webhook は使わない）。鍵は作業場に渡さない＝機械を動かすのは手元だけ。
- 1 回の起動の費用は振り返りと計画を分けて `run/auto_YYYYMMDD.jsonl` に残す。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from saibai import config
from saibai.agents import store_resource
from saibai.guard import Run
from saibai.weather import prompt_block

REVIEW_PROMPT = (
    "振り返りの時間です（オートの定時の起動）。\n"
    "1. /mnt/memory/saibai-experience/experience.md を読み、「最後に振り返った時刻」を確かめる。\n"
    "2. /mnt/memory/saibai-actions/ の操作の記録から、その時刻より後の操作を、/mnt/memory/saibai-meter/ の"
    "温湿度の記録と合わせて Python（pandas）で読み、「入」ごとに入れる前の VPD と 15・30・60 分の差を計算する。"
    "誰が（手動／定時／オート）・断られた計画とその理由・外からの変化・記録なし（穴）も数える。"
    "まだ 60 分たっていない入は、たった分だけを書き「途中」と付ける。\n"
    "3. 経験を書き直す：前の版を読んだ上で、「操作の効き目（事実）」の表に新しい行を足し、「傾向」の数（もとにした回の数）を"
    "直し、「最後に振り返った時刻」を今回数えた最後の時刻にする。真似でなく効き目だけ。新しい操作が無ければ、その旨と時刻だけ直す。\n"
    "3b. 外の天気（農家が年々覚える物）：/mnt/memory/saibai-meter/weather/ に外の 1 時間値（YYYY-MM-DD.csv）と最新の予報"
    "（forecast.md）がある。操作の記録の what=計画 の weather は、その計画の時に見た予報の要約。"
    "「雨の予報で控えた→部屋の湿度は本当に上がったか」「外の湿度が上がってから部屋の湿度が何時間遅れで上がるか（相関と遅れ）」"
    "「予報は当たったか（翌日の外の降水と比べる）」を Python で数え、経験の「外の天気」の節に、もとにした回の数と一緒に書く。"
    "この部屋に外の天気が効くかは決めつけない（回が少なければ「まだ分からない」と書く）。外の記録が無ければ飛ばす。\n"
    "4. 最後に、今回足した行と変えた傾向を 5 行以内で書いて終わる。"
)


TO_PERSON_HINT = "水やりなど機械に触らない事で人に伝えたいことがあれば、submit_plan の to_person に数値と時刻を引いて数行で（任意・機械は動かない）。"


def plan_prompt(now: datetime, status: dict, target: dict, weather: dict | None = None) -> str:
    """weather＝Weather.summary()（無ければ「天気は無し」と書く）。生の JSON は入れない。"""
    m = status.get("meter") or {}
    g = status.get("guard") or {}
    return (
        f"今は {now.isoformat(timespec='minutes')}（JST）。オートの計画の時間です。\n"
        f"今の値（受け手が読んだ時刻 {m.get('read_at')}）：気温 {m.get('temp_c')}℃・湿度 {m.get('rh_pct')}%・"
        f"VPD {m.get('vpd_kpa')} kPa。\n"
        f"狙いの VPD の帯：{target['vpd_low_kpa']}〜{target['vpd_high_kpa']} kPa（帯の中なら入れなくてよい）。\n"
        f"守りの層（今の組の線）：{g.get('lines_text')}（今日 {g.get('used_today_min')} 分動いた）。"
        f"センサーの疑い：{g.get('sensor_problems') or 'なし'}。\n"
        + prompt_block(weather)
        + "次の起動（約 3 時間後）までの計画を submit_plan で 1 つ出してください。"
        "/mnt/memory/saibai-experience/experience.md の経験（効き目）と、必要なら温湿度の記録を読み、"
        "理由にはどの経験の行を使ったかと数値と時刻を引いてください。"
        + TO_PERSON_HINT
        + "結果を受けたら一言で締めてください。"
    )


def timer_prompt(
    now: datetime,
    at: datetime,
    default_minutes: int,
    status: dict,
    target: dict,
    weather: dict | None,
) -> str:
    """定時・長さは AI：開始の時刻は人が決めている＝分だけを決めさせる依頼文。"""
    m = status.get("meter") or {}
    g = status.get("guard") or {}
    return (
        f"今は {now.isoformat(timespec='minutes')}（JST）。定時の長さを決める時間です（定時・長さは AI）。\n"
        f"開始は {at.isoformat(timespec='minutes')} に人が決めている＝変えない。決めるのは何分動かすかだけ。"
        f"人の既定は {default_minutes} 分（答えが無ければこの分で動く）。\n"
        f"今の値（受け手が読んだ時刻 {m.get('read_at')}）：気温 {m.get('temp_c')}℃・湿度 {m.get('rh_pct')}%・"
        f"VPD {m.get('vpd_kpa')} kPa。狙いの VPD の帯：{target['vpd_low_kpa']}〜{target['vpd_high_kpa']} kPa。\n"
        f"守りの層（今の組の線）：{g.get('lines_text')}（今日 {g.get('used_today_min')} 分動いた）。"
        f"センサーの疑い：{g.get('sensor_problems') or 'なし'}。\n"
        + prompt_block(weather)
        + f"submit_plan を 1 回出す：runs は start={at.isoformat(timespec='minutes')} の 1 件だけ（分は 1〜"
        f"{g.get('max_continuous_min')}）。今日は入れないなら runs を空。"
        "/mnt/memory/saibai-experience/experience.md の効き目（入れて 15・30・60 分で VPD がいくつ動いたか）から分を出し、"
        "理由にはどの経験の行を使ったかと数値と時刻を引く。結果を受けたら一言で締める。"
    )


def check_timer_plan(runs: list[Run], at: datetime, now: datetime) -> str | None:
    """定時・長さは AI の計画を確かめる。開始の時刻は変えさせない。通れば None。"""
    if now >= at:
        return f"断った：開始の {at:%H:%M} を過ぎた（既定の分で動かしている）。"
    if len(runs) > 1:
        return f"断った：定時は 1 回だけ（開始は {at:%H:%M}・分だけ決める）。"
    if runs and abs((runs[0].start - at).total_seconds()) >= 60:
        return f"断った：開始は {at:%H:%M} に決まっている（{runs[0].start:%H:%M} は違う）＝分だけ決めて出し直す。"
    return None


def parse_plan(inp: dict) -> tuple[list[Run], list[dict], str | None]:
    """制御役の submit_plan の入力を、守りの層の形にする。読めなければ理由を返す。"""
    try:
        runs = [
            Run(datetime.fromisoformat(r["start"]), int(r["minutes"]), "オート")
            for r in inp.get("runs") or []
        ]
    except (KeyError, TypeError, ValueError) as e:
        return (
            [],
            [],
            f"断った：計画が読めない（{type(e).__name__}）。runs は start（+09:00 付き）と minutes。",
        )
    return runs, list(inp.get("evidence") or []), None


def usage_of(s) -> dict:
    u = s.usage.model_dump(mode="json") if getattr(s, "usage", None) else {}
    return {
        "list_cost_cents": (u.get("list_cost") or {}).get("amount"),
        "output_tokens": u.get("output_tokens"),
        "cache_read_input_tokens": u.get("cache_read_input_tokens"),
        "active_seconds": u.get("active_seconds"),
    }


class AutoRunner:
    """起動の記録を見に行く裏の糸。`ctrl` は受け手の Controller（守りの層を通す口）。"""

    def __init__(
        self,
        client,
        ids: dict,
        ctrl,
        run_dir: Path,
        now_fn,
        sync=None,
        poll_s: float = 15.0,
        meter_dir: Path | None = None,
    ):
        self.meter_dir = meter_dir or run_dir  # 温湿度の手元の記録の場所
        self.client = client
        self.ids = ids
        self.ctrl = ctrl
        self.run_dir = run_dir
        self.now = now_fn
        self.sync = sync
        self.poll_s = poll_s
        self.started_at = now_fn()
        self.state_file = run_dir / "auto_state.json"
        try:
            self.state = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            self.state = {"done_runs": [], "kicked_for": None}
        self.current: dict | None = None
        self.last: dict | None = None  # 最後の 1 回（画面に出す）

    def _log(self, **rec) -> None:
        now = self.now()
        rec = {"at": now.isoformat(timespec="seconds"), **rec}
        with (self.run_dir / f"auto_{now:%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False))

    # ---- 見に行く ----
    def wait_session(self, sid: str, on_tool=None, deadline_s: float = 900) -> str:
        """止まるまで出来事を見に行く。custom tool には on_tool で答える。止まった理由を返す。"""
        answered: set[str] = set()
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            evs = list(self.client.beta.sessions.events.list(session_id=sid))
            for ev in evs:
                if ev.type == "agent.custom_tool_use" and ev.id not in answered:
                    answered.add(ev.id)
                    text = on_tool(ev) if on_tool else "この道具は今は使えない"
                    self.client.beta.sessions.events.send(
                        session_id=sid,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": ev.id,
                                "content": [{"type": "text", "text": text}],
                            }
                        ],
                    )
            last = evs[-1] if evs else None
            if last is not None and last.type == "session.status_terminated":
                return "terminated"
            if last is not None and last.type == "session.status_idle":
                stop = last.stop_reason.type
                if stop != "requires_action":
                    return stop
            time.sleep(self.poll_s)
        return "timeout"

    def last_text(self, sid: str) -> str:
        texts = []
        for ev in self.client.beta.sessions.events.list(session_id=sid):
            if ev.type == "agent.message":
                texts = [b.text for b in ev.content if b.type == "text"]
        return "\n".join(texts)

    # ---- 1 回の起動 ----
    def handle_run(self, review_sid: str, run_id: str | None = None) -> dict:
        rec: dict = {"run_id": run_id, "review_session": review_sid, "plans": []}
        self.current = rec
        rec["review_stop"] = self.wait_session(review_sid)
        rec["review_usage"] = usage_of(self.client.beta.sessions.retrieve(session_id=review_sid))
        rec["review_text"] = self.last_text(review_sid)[-1500:]
        if self.sync:
            self.sync.kick_meter()  # 計画のセッションの前にも、温湿度を最新まで写す
        status = self.ctrl.status() | {"meter": self.meter_now()}
        weather = self.ctrl.weather_summary()
        rec["weather"] = weather
        target = config.load()["target"]
        s = self._plan_session(
            f"オートの計画 {self.now():%m/%d %H:%M}",
            plan_prompt(self.now(), status, target, weather),
        )
        rec["plan_session"] = s.id

        def on_tool(ev) -> str:
            if ev.name != "submit_plan":
                return "この道具は無い"
            runs, evidence, err = parse_plan(ev.input)
            if err:
                rec["plans"].append({"input": ev.input, "ok": False, "text": err})
                return err
            v = self.ctrl.submit(
                runs,
                evidence,
                "オート",
                extra={
                    "reason": ev.input.get("reason"),
                    "to_person": ev.input.get("to_person"),
                    "weather": weather,
                },
            )
            text = v.text() if runs else ("受け付けた：今回は入れない。" if v.ok else v.text())
            rec["plans"].append({"input": ev.input, "ok": v.ok, "text": text})
            return text

        rec["plan_stop"] = self.wait_session(s.id, on_tool)
        rec["plan_usage"] = usage_of(self.client.beta.sessions.retrieve(session_id=s.id))
        rec["plan_text"] = self.last_text(s.id)[-800:]
        rec["experience_versions"] = self.experience_versions()
        self._log(kind="auto_run", **rec)
        self.experience_info(cache_s=0)
        self.current, self.last = None, rec
        return rec

    def _plan_session(self, title: str, prompt: str):
        """計画のセッション（制御役＋専門家・置き場は全部読むだけ）を起こし、最初の依頼文を送る。"""
        ids = self.ids
        s = self.client.beta.sessions.create(
            agent=ids["control"],
            environment_id=ids["env"],
            title=title,
            budget={"type": "limit", "max_list_cost": {"amount": "100", "currency": "USD"}},
            resources=[
                store_resource(
                    ids["experience"], "read_only", "経験（振り返りが書いた効き目）。読むだけ。"
                ),
                store_resource(ids["meter"], "read_only", "温湿度の記録。読むだけ。"),
                store_resource(ids["actions"], "read_only", "操作の記録。読むだけ。"),
                store_resource(
                    ids["knowledge"], "read_only", "栽培の知識。専門家が読む。読むだけ。"
                ),
            ],
        )
        self.client.beta.sessions.events.send(
            session_id=s.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
        )
        return s

    # ---- 定時・長さは AI（v0.7）----
    def ask_timer(self, at: datetime, default_minutes: int) -> dict:
        """開始の時刻（人が決めた）の約 10 分前に呼ぶ。制御役に分だけを決めさせ、受け付けた計画は
        守りの層に「定時」として入る。返り＝{ok, minutes, reason, text}（ok=False なら受け手は既定の分で動かす）。"""
        rec: dict = {"start": at.isoformat(), "default_minutes": default_minutes, "plans": []}
        result = {"ok": False, "minutes": None, "reason": None, "text": "答えが来ない"}
        weather = self.ctrl.weather_summary()
        status = self.ctrl.status() | {"meter": self.meter_now()}
        target = config.load()["target"]
        if self.sync:
            self.sync.kick_meter()
        prompt = timer_prompt(self.now(), at, default_minutes, status, target, weather)
        s = self._plan_session(f"定時の長さ {at:%m/%d %H:%M}", prompt)
        rec["plan_session"] = s.id

        def on_tool(ev) -> str:
            if ev.name != "submit_plan":
                return "この道具は無い"
            runs, evidence, err = parse_plan(ev.input)
            err = err or check_timer_plan(runs, at, self.now())
            if err:
                rec["plans"].append({"input": ev.input, "ok": False, "text": err})
                result["text"] = err
                return err
            runs = [Run(r.start, r.minutes, "定時") for r in runs]
            reason = ev.input.get("reason")
            v = self.ctrl.submit(
                runs,
                evidence,
                "定時",
                extra={
                    "reason": reason,
                    "to_person": ev.input.get("to_person"),
                    "weather": weather,
                    "by": "AI の分",
                },
            )
            minutes = sum(r.minutes for r in runs)
            text = (
                v.text() if runs else ("受け付けた：今日は入れない（0 分）。" if v.ok else v.text())
            )
            rec["plans"].append({"input": ev.input, "ok": v.ok, "text": text})
            result.update(text=text)
            if v.ok:
                result.update(ok=True, minutes=minutes, reason=reason)
                # 開始の直前に受け付けた時も、既定の分で上書きしないよう、すぐ受け手に知らせる
                self.ctrl.note_timer_ai(at.date().isoformat(), result)
            return text

        # 開始の時刻までに答えが来なければ、受け手は既定の分で動かす（ここも開始で見に行くのをやめる）
        deadline = max(30.0, (at - self.now()).total_seconds())
        rec["plan_stop"] = self.wait_session(s.id, on_tool, deadline_s=deadline)
        try:
            rec["plan_usage"] = usage_of(self.client.beta.sessions.retrieve(session_id=s.id))
        except Exception:
            pass
        rec["result"] = result
        self._log(kind="timer_ai", **rec)
        return result

    def meter_now(self) -> dict:
        f = self.meter_dir / f"meter_{self.now():%Y%m%d}.jsonl"
        if f.exists():
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ok"):
                    return r
        return {}

    def experience_versions(self) -> list[dict]:
        out = []
        for v in self.client.beta.memory_stores.memory_versions.list(self.ids["experience"]):
            d = v.model_dump(mode="json")
            by = d.get("created_by") or {}
            out.append(
                {
                    "id": d.get("id"),
                    "operation": d.get("operation"),
                    "created_at": d.get("created_at"),
                    "by": by.get("type"),
                    "session_id": by.get("session_id"),
                }
            )
        return out

    # ---- 裏の糸 ----
    def poll_once(self) -> None:
        dep = self.ids.get("deployment")
        if not dep:
            return
        d = self.client.beta.deployments.retrieve(dep)
        upcoming = (d.schedule.upcoming_runs_at or [None])[0]
        if self.sync and upcoming and d.status == "active":
            # セッションを始める前にも 1 回写す（起動の 3 分前から）
            if (
                upcoming - timedelta(minutes=3) <= self.now()
                and self.state.get("kicked_for") != upcoming.isoformat()
            ):
                self.sync.kick_meter()
                self.state["kicked_for"] = upcoming.isoformat()
                self._save()
        for r in self.client.beta.deployment_runs.list(deployment_id=dep):
            sid = getattr(r, "session_id", None)
            created = getattr(r, "created_at", None)
            if not sid or r.id in self.state["done_runs"]:
                continue
            if created and created < self.started_at:
                continue  # 受け手が起きる前の起動は扱わない（試しの起動と取り合わない）
            self.state["done_runs"].append(r.id)
            self._save()
            try:
                self.handle_run(sid, r.id)
            except Exception as e:  # 1 回の起動が失敗しても、守りと記録は止めない
                self._log(kind="auto_error", run_id=r.id, error=f"{type(e).__name__}: {e}")
                self.current = None

    def start(self) -> threading.Event:
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    self.experience_info()
                    self.poll_once()
                except Exception as e:
                    self._log(kind="auto_poll_error", error=f"{type(e).__name__}: {e}")
                stop.wait(60)

        threading.Thread(target=loop, daemon=True).start()
        return stop

    def experience_info(self, cache_s: float = 300.0) -> dict | None:
        """画面に小さく出す：経験の版の数・最新の版（誰が・いつ）・傾向の行。5 分は覚えておく。"""
        c = getattr(self, "_exp_cache", None)
        if c and time.monotonic() - c[0] < cache_s:
            return c[1]
        try:
            vs = self.experience_versions()
            mem = next(
                m
                for m in self.client.beta.memory_stores.memories.list(
                    self.ids["experience"], path_prefix="/", view="full"
                )
                if getattr(m, "type", "memory") == "memory" and m.path == "/experience.md"
            )
            lines = mem.content.splitlines()
            trend = []
            if "## 傾向（もとにした回の数つき）" in lines:
                for line in lines[lines.index("## 傾向（もとにした回の数つき）") + 1 :]:
                    if line.startswith("## "):
                        break
                    if line.startswith("| ") and not line.startswith(("| 季節", "|---")):
                        trend.append(line)
            last_rev = next(
                (x.split("：", 1)[1] for x in lines if x.startswith("- 最後に振り返った時刻：")),
                None,
            )
            info = {
                "versions": len(vs),
                "latest": vs[0] if vs else None,
                "trend": trend[:4],
                "last_reviewed": last_rev,
            }
        except Exception as e:
            info = {"error": f"{type(e).__name__}"}
        self._exp_cache = (time.monotonic(), info)
        return info

    def status(self) -> dict:
        last = self.last or {}
        return {
            # 置き場を読むのは裏の糸だけ（ここは守りの層の錠の中で呼ばれる＝通信を待たない）
            "experience": (getattr(self, "_exp_cache", None) or (0, None))[1],
            "running": self.current is not None,
            "last": {
                k: last.get(k)
                for k in (
                    "run_id",
                    "review_stop",
                    "plan_stop",
                    "plans",
                    "review_usage",
                    "plan_usage",
                )
            }
            if last
            else None,
        }


def ensure_deployment(client, ids: dict, save) -> dict:
    """オートの定時の起動（振り返りのセッションを起こす）。作った時は止めておく（入れるのは人＝set_auto）。"""
    if "deployment" in ids:
        return ids
    d = client.beta.deployments.create(
        name="saibai-オート（振り返り→計画）",
        agent=ids["analyst"],
        environment_id=ids["env"],
        resources=[
            store_resource(
                ids["experience"], "read_write", "経験。振り返りの結果をここに書き直す。"
            ),
            store_resource(ids["meter"], "read_only", "温湿度の記録。読むだけ。"),
            store_resource(ids["actions"], "read_only", "操作の記録。読むだけ。"),
        ],
        initial_events=[
            {"type": "user.message", "content": [{"type": "text", "text": REVIEW_PROMPT}]}
        ],
        schedule={"type": "cron", "expression": "0 */3 * * *", "timezone": "Asia/Tokyo"},
        budget={"type": "limit", "max_list_cost": {"amount": "50", "currency": "USD"}},
    )
    ids["deployment"] = d.id
    save(ids)
    client.beta.deployments.pause(d.id)
    return ids
