"""飽差（VPD）。葉の温度は気温と同じと置いた、空気の VPD。"""

import math


def vpd_kpa(temp_c: float, rh_pct: float) -> float:
    # 飽和水蒸気圧 e°(T)=0.6108·exp[17.27T/(T+237.3)] kPa（FAO 56 の式 11）・VPD=e°(T)·(1−RH/100)
    es = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
    return round(es * (1 - rh_pct / 100), 3)
