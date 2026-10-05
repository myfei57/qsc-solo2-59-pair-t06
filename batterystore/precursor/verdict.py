"""前兆强弱判定。

单轴越预警线只算弱前兆（先预警、可降流观察）；温升与电压同时越预警线，或
任一轴达重警线，才算强前兆（直接切簇）——温升加速伴随电压异常是内短路
簇的典型组合特征，单轴异动则可能是传感或工况扰动。判定只返回结论与理由，
动作由编排决定，与 BMS 保护判据同一套分工。
"""

from __future__ import annotations

from ..config import PrecursorLimits
from .features import PrecursorFeatures

NONE = "none"
WEAK = "weak"
STRONG = "strong"


def judge(features: PrecursorFeatures, limits: PrecursorLimits) -> dict:
    """按特征与阈值给出前兆等级与触发理由。"""

    temp_warn = features.temp_rise_c_per_min >= limits.temp_rise_warn_c_per_min
    temp_severe = features.temp_rise_c_per_min >= limits.temp_rise_trip_c_per_min
    volt_warn = (
        features.voltage_drop_v >= limits.voltage_drop_warn_v
        or features.delta_rise_mv >= limits.delta_rise_warn_mv
    )
    volt_severe = (
        features.voltage_drop_v >= limits.voltage_drop_trip_v
        or features.delta_rise_mv >= limits.delta_rise_trip_mv
    )
    reasons: list[str] = []
    if temp_warn:
        reasons.append("temp_rise_warn")
    if temp_severe:
        reasons.append("temp_rise_trip")
    if volt_warn:
        reasons.append("voltage_warn")
    if volt_severe:
        reasons.append("voltage_trip")
    if temp_severe or volt_severe or (temp_warn and volt_warn):
        level = STRONG
        if temp_warn and volt_warn and not (temp_severe or volt_severe):
            reasons.append("temp_voltage_combined")
    elif temp_warn or volt_warn:
        level = WEAK
    else:
        level = NONE
        reasons.clear()
    return {"level": level, "reasons": reasons}
