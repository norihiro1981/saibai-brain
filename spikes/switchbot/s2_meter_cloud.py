# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""手順 2：温湿度計をクラウド経由で読む。値に時刻が付いているかも見る。"""

import time
from datetime import datetime

from sb_client import device_id, get, log_writer

meter = device_id("SWITCHBOT_METER_ID", "MeterPlus")
log = log_writer("s2_meter_cloud")

for i in range(3):
    res = get(f"/devices/{meter}/status")
    log(res)
    b = res["data"].get("body") or {}
    rest = {k: v for k, v in b.items() if k not in ("deviceId", "hubDeviceId")}
    print(
        f"[{datetime.now():%H:%M:%S}] HTTP {res['http']} code={res['data'].get('statusCode')}"
        f" 往復={res['got'] - res['sent']:.2f}s  Date={res['server_date']}  {rest}"
    )
    if i < 2:
        time.sleep(20)

print(f"本体の鍵：{sorted(b)}")
print(f"生の記録：{log.path}")
