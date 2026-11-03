"""置き場への写しのテスト。本物の API は叩かない（模擬の置き場）。"""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from saibai.guard import JST
from saibai.memory_sync import MemorySync, meter_csv


class FakeMemories:
    def __init__(self):
        self.files: dict[tuple[str, str], SimpleNamespace] = {}
        self.fail = False
        self.calls = []

    def list(self, store, path_prefix="/"):
        if self.fail:
            raise ConnectionError("届かない")
        return [
            m for (s, _), m in self.files.items() if s == store and m.path.startswith(path_prefix)
        ]

    def create(self, store, path, content):
        self.calls.append(("create", path))
        m = SimpleNamespace(id=f"mem_{len(self.files)}", path=path, type="memory")
        self._set(m, content)
        self.files[(store, path)] = m

    def update(self, mem_id, memory_store_id, content, precondition):
        m = next(m for (s, _), m in self.files.items() if m.id == mem_id)
        assert precondition["content_sha256"] == m.content_sha256  # 前の版を読んでから書く
        self.calls.append(("update", m.path))
        self._set(m, content)

    @staticmethod
    def _set(m, content):
        m.content = content
        m.content_sha256 = hashlib.sha256(content.encode()).hexdigest()


def make(tmp_path, t):
    mem = FakeMemories()
    client = SimpleNamespace(beta=SimpleNamespace(memory_stores=SimpleNamespace(memories=mem)))
    clock = {"t": t}
    sync = MemorySync(client, "meter", "actions", tmp_path, lambda: clock["t"])
    return sync, mem, clock


def write_meter(tmp_path, start, n, every_s=30):
    with (tmp_path / f"meter_{start:%Y%m%d}.jsonl").open("a") as f:
        for i in range(n):
            t = start + timedelta(seconds=every_s * i)
            f.write(
                json.dumps(
                    {
                        "read_at": t.isoformat(timespec="seconds"),
                        "ok": True,
                        "temp_c": 15.0,
                        "rh_pct": 67,
                        "vpd_kpa": 0.563,
                    }
                )
                + "\n"
            )


def test_hour_csv_is_small_and_has_vpd(tmp_path):
    h = datetime(2026, 11, 5, 3, 0, tzinfo=JST)
    write_meter(tmp_path, h, 120)
    csv = meter_csv(tmp_path, h)
    assert csv.splitlines()[0] == "read_at,temp_c,rh_pct,vpd_kpa"
    assert len(csv.splitlines()) == 121 and len(csv.encode()) < 100_000


def test_meter_rewrites_current_hour_and_closes_previous(tmp_path):
    h = datetime(2026, 11, 5, 3, 0, tzinfo=JST)
    write_meter(tmp_path, h, 10)
    sync, mem, clock = make(tmp_path, h + timedelta(minutes=5))
    sync.kick_meter()
    write_meter(tmp_path, h + timedelta(minutes=5), 10)
    clock["t"] = h + timedelta(minutes=10)
    sync.kick_meter()
    assert mem.calls == [
        ("create", "/meter/2026-11-05/03.csv"),
        ("update", "/meter/2026-11-05/03.csv"),
    ]
    # 時間が変わったら、前の時間を最後に 1 回写して閉じる
    write_meter(tmp_path, h + timedelta(minutes=59), 2)
    clock["t"] = h + timedelta(hours=1, minutes=1)
    sync.kick_meter()
    assert ("update", "/meter/2026-11-05/03.csv") in mem.calls[2:]
    assert sync.state["dirty_hours"] == [(h + timedelta(hours=1)).isoformat()]
    assert len(mem.files[("meter", "/meter/2026-11-05/03.csv")].content.splitlines()) == 23


def test_failed_push_is_retried_next_time(tmp_path):
    h = datetime(2026, 11, 5, 3, 0, tzinfo=JST)
    write_meter(tmp_path, h, 5)
    (tmp_path / "control_20261105.jsonl").write_text(
        json.dumps({"at": h.isoformat(), "kind": "action", "who": "手動", "what": "入"})
        + "\n"
        + json.dumps({"at": h.isoformat(), "kind": "plug_set", "on": True})
        + "\n"
    )
    sync, mem, clock = make(tmp_path, h + timedelta(minutes=5))
    mem.fail = True
    sync.kick_actions({"at": h.isoformat()})
    sync.kick_meter()
    assert sync.last_error and sync.state["dirty_days"] == ["2026-11-05"]
    # 受け手を起こし直しても「まだ」は残っている
    sync2, _, _ = make(tmp_path, h + timedelta(minutes=10))
    sync2.client = sync.client
    mem.fail = False
    sync2.kick_actions()
    assert sync2.state["dirty_days"] == []
    body = mem.files[("actions", "/actions/2026-11-05.jsonl")].content
    assert body.count("\n") == 1 and "plug_set" not in body  # 写すのは操作の記録だけ
