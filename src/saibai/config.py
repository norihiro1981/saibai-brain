"""線の値の設定ファイル（config/saibai.toml）を読む。数はコードに散らさず、ここから引く。"""

import os
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@cache
def load() -> dict:
    path = Path(os.environ.get("SAIBAI_CONFIG", ROOT / "config" / "saibai.toml"))
    with path.open("rb") as f:
        return tomllib.load(f)
