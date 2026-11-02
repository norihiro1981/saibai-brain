# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""手順 4：プラグを API で 入→切 を 3 回。各回の後に状態を読み、本当に切り替わったか確かめる。

※今回だけの例外：守りの層を通さずにプラグを動かす。先は電気スタンドだけ（加湿器はつながない）。
  本体のコードからは呼ばない。始めと終わりは必ず「切」。
"""

import time
from datetime import datetime

from sb_client import device_id, get, log_writer, post

plug = device_id("SWITCHBOT_PLUG_ID", "Plug Mini (JP)")
log = log_writer("s4_plug_toggle")
WAIT = [1, 3, 6, 10]  # 命令から何秒後に状態を読むか（反映の遅れを見る）


def status(label: str) -> dict:
    res = get(f"/devices/{plug}/status")
    b = res["data"].get("body") or {}
    rest = {k: v for k, v in b.items() if k not in ("deviceId", "hubDeviceId", "deviceType", "version")}
    log({"step": label, **res})
    print(f"  [{datetime.now():%H:%M:%S}] {label:<14} {rest}")
    return b


def command(cmd: str) -> None:
    res = post(f"/devices/{plug}/commands", {"command": cmd, "parameter": "default", "commandType": "command"})
    log({"step": cmd, **res})
    d = res["data"]
    print(f"  [{datetime.now():%H:%M:%S}] {cmd:<14} HTTP {res['http']} code={d.get('statusCode')} {d.get('message')}"
          f" 往復={res['got'] - res['sent']:.2f}s body={d.get('body')}")


def run(cmd: str) -> None:
    command(cmd)
    t0 = time.time()
    for w in WAIT:
        time.sleep(max(0, t0 + w - time.time()))
        status(f"{cmd}+{w}s")


try:
    print("始め：")
    status("start")
    command("turnOff")
    time.sleep(3)
    status("start-off")
    for i in range(1, 4):
        print(f"--- {i} 回目")
        run("turnOn")
        run("turnOff")
finally:
    print("終わり：必ず切")
    command("turnOff")
    time.sleep(3)
    status("end")
    print(f"生の記録：{log.path}")
