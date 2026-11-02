# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak", "httpx", "python-dotenv"]
# ///
"""手順 3：温湿度計の BLE の発信を Air で直接読み、クラウドの値と並べて記録する。

解き方は公開の資料（OpenWonderLabs/SwitchBotAPI-BLE の devicetypes/meter.md）の
Service data の表に従う：Byte3 の下 4 bit＝温度の小数、Byte4 の bit7＝正負・下 7 bit＝温度の整数、
Byte5 の下 7 bit＝湿度。macOS では MAC が見えないので、製造者データに入っている MAC で機器を見分ける。

使い方：uv run --script s3_ble_meter.py [秒]（既定 600 秒）
"""

import asyncio
import sys
import time
from datetime import datetime

from bleak import BleakScanner

from sb_client import device_id, get, log_writer

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 600
CLOUD_EVERY = 60

meter = device_id("SWITCHBOT_METER_ID", "MeterPlus").upper()
mac = bytes.fromhex(meter)
log = log_writer("s3_ble_meter")


def decode_service(sd: bytes) -> dict | None:
    if len(sd) < 6:
        return None
    temp = (sd[4] & 0x7F) + (sd[3] & 0x0F) / 10
    if not sd[4] & 0x80:
        temp = -temp
    return {
        "dev_type": chr(sd[0] & 0x7F),
        "battery": sd[2] & 0x7F,
        "temperature": round(temp, 1),
        "humidity": sd[5] & 0x7F,
        "fahrenheit": bool(sd[5] & 0x80),
    }


ble: list[dict] = []
cloud: list[dict] = []
others: set[str] = set()


def on_adv(device, adv) -> None:
    now = time.time()
    mfr = {cid: bytes(v) for cid, v in adv.manufacturer_data.items()}
    is_meter = any(v[:6] == mac for v in mfr.values())
    sd = {u: bytes(v) for u, v in adv.service_data.items()}
    if not is_meter:
        # 見分けが外れた時に後から調べられるよう、SwitchBot らしい発信は生のまま残す
        if any(("fd3d" in u or "0d00" in u or "cba20d00" in u) for u in sd) or {0x0059, 0x0969} & set(mfr):
            if device.address not in others:
                log({
                    "t": now,
                    "kind": "other",
                    "addr": device.address,
                    "name": adv.local_name,
                    "rssi": adv.rssi,
                    "uuids": adv.service_uuids,
                    "mfr": {f"0x{cid:04x}": v.hex() for cid, v in mfr.items()},
                    "service": {u: v.hex() for u, v in sd.items()},
                })
            others.add(device.address)
        return
    rec = {
        "t": now,
        "kind": "ble",
        "rssi": adv.rssi,
        "mfr": {f"0x{cid:04x}": v.hex() for cid, v in mfr.items()},
        "service": {u: v.hex() for u, v in sd.items()},
        "decoded": None,
    }
    for v in sd.values():
        if d := decode_service(v):
            rec["decoded"] = d
            break
    ble.append(rec)
    log(rec)


async def poll_cloud(stop: float) -> None:
    while time.time() < stop:
        res = get(f"/devices/{meter}/status")
        b = res["data"].get("body") or {}
        rec = {
            "t": res["got"],
            "kind": "cloud",
            "http": res["http"],
            "temperature": b.get("temperature"),
            "humidity": b.get("humidity"),
        }
        cloud.append(rec)
        log(rec)
        print(f"[{datetime.now():%H:%M:%S}] クラウド {rec['temperature']}℃ {rec['humidity']}%  BLE 受信 {len(ble)} 回")
        await asyncio.sleep(CLOUD_EVERY)


def changes(rows: list[dict], key) -> list[tuple[float, tuple]]:
    out, last = [], None
    for r in rows:
        v = key(r)
        if v is not None and v != last:
            out.append((r["t"], v))
            last = v
    return out


def summarize() -> None:
    print("\n==== まとめ ====")
    print(f"BLE で温湿度計を拾った回数：{len(ble)}（{SECONDS} 秒）")
    if len(ble) >= 2:
        gaps = sorted(b["t"] - a["t"] for a, b in zip(ble, ble[1:]))
        n = len(gaps)
        print(f"受信の間隔：最小 {gaps[0]:.1f}s・中央 {gaps[n // 2]:.1f}s・最大 {gaps[-1]:.1f}s")
    dec = [b for b in ble if b["decoded"]]
    print(f"Service data を解けた回数：{len(dec)}")
    if dec:
        print(f"例：{dec[-1]['decoded']}  service={dec[-1]['service']}  mfr={dec[-1]['mfr']}")
    bc = changes(dec, lambda r: (r["decoded"]["temperature"], r["decoded"]["humidity"]))
    cc = changes(cloud, lambda r: (r["temperature"], r["humidity"]))
    t0 = ble[0]["t"] if ble else time.time()
    print("BLE の値が変わった時：", [(round(t - t0), v) for t, v in bc])
    print("クラウドの値が変わった時：", [(round(t - t0), v) for t, v in cc])
    # 同じ時刻の BLE とクラウドの差
    diffs = []
    for c in cloud:
        before = [b for b in dec if b["t"] <= c["t"]]
        if before and c["temperature"] is not None:
            d = before[-1]["decoded"]
            diffs.append((round(c["temperature"] - d["temperature"], 1), c["humidity"] - d["humidity"]))
    print("クラウド−BLE（その時の最新どうし・温度, 湿度）：", diffs)
    # クラウドの各変化が、同じ値を BLE で初めて見た時から何秒遅れたか
    lags = []
    for t, v in cc[1:]:
        seen = [bt for bt, bv in bc if bv == v and bt <= t]
        if seen:
            lags.append(round(t - seen[-1]))
    print("クラウドの遅れ（秒・クラウドの読みが 60 秒おきなので最大 60 秒の粗さ）：", lags)
    print(f"ほかの SwitchBot の発信：{len(others)} 台")
    print(f"生の記録：{log.path}")


async def main() -> None:
    stop = time.time() + SECONDS
    scanner = BleakScanner(detection_callback=on_adv, scanning_mode="active")
    await scanner.start()
    try:
        await poll_cloud(stop)
    finally:
        await scanner.stop()
    summarize()


asyncio.run(main())
