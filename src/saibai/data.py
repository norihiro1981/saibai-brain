"""記録の受け口：data/*.csv を共通の形で読む。

- 時刻は JST の aware な datetime（pandas では tz=Asia/Tokyo）。
- 環境の記録は縦に長い形（1 行＝1 台の 1 回の読み）のまま、機器 ID ごとに引ける形で返す。
- data/ は読むだけ（書き換えない）。
"""

from pathlib import Path

import pandas as pd

TZ = "Asia/Tokyo"
METRICS = ["temp_c", "rh_pct", "vpd_kpa", "co2_ppm", "soil_raw"]
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _to_jst(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="ISO8601", utc=True).dt.tz_convert(TZ)


def load_readings(path: Path | str) -> pd.DataFrame:
    """環境の記録。列＝timestamp（JST aware）・device_id・各値（無い値は NaN）。時刻順。"""
    df = pd.read_csv(path, dtype={"device_id": "string"})
    df["timestamp"] = _to_jst(df["timestamp"])
    for c in METRICS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["timestamp", "device_id"], kind="stable").reset_index(drop=True)


def by_device(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """機器 ID ごとに、時刻を索引にした表（その機器に無い値の列は落とす）。"""
    out = {}
    for dev, g in df.groupby("device_id", sort=True):
        g = g.set_index("timestamp").drop(columns=["device_id"])
        out[str(dev)] = g.dropna(axis=1, how="all")
    return out


def load_actions(path: Path | str) -> pd.DataFrame:
    """作業の記録。start_jst・end_jst は JST aware（終わりの無い作業は NaT）。"""
    df = pd.read_csv(path, dtype="string")
    df["start_jst"] = _to_jst(df["start_jst"])
    df["end_jst"] = _to_jst(df["end_jst"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df.sort_values("start_jst", kind="stable").reset_index(drop=True)


def season_files(season: str) -> tuple[Path, Path]:
    """'summer' か 'autumn' の、環境の記録と作業の記録の道。"""
    env = next(DATA_DIR.glob(f"{season}_*.csv"))
    span = env.stem.split("_", 1)[1]
    return env, DATA_DIR / f"actions_{span}.csv"
