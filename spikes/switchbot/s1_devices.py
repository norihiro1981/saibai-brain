# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""手順 1：機器の一覧を取り、ID と種類を画面に出す（ID は .env に人が入れる）。"""

from sb_client import get, log_writer

log = log_writer("s1_devices")
res = get("/devices")
log(res)

print(f"HTTP {res['http']}  statusCode={res['data'].get('statusCode')}  message={res['data'].get('message')}")
body = res["data"].get("body") or {}
for d in body.get("deviceList", []):
    print(
        f"{d.get('deviceType'):<20} id={d.get('deviceId')}  hub={d.get('hubDeviceId')}"
        f"  cloud={d.get('enableCloudService')}  keys={sorted(d)}"
    )
print(f"赤外線のリモコン：{len(body.get('infraredRemoteList', []))} 件")
print(f"生の記録：{log.path}")
