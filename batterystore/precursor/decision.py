"""前兆判据。

单特征越限只记"疑似"，不动作——温升速率与电压变化必须互相印证才构成前兆，
这是把误报挡在动作之外的第一道闸门。双特征确认后的分级看三件事：速率是否
进入加速段、电压是否崩塌式跌落、按当前速率推算的保护线剩余窗口是否还容得
下一次降流观察。窗口容不下时直接切簇，不再把窗口浪费在观察上。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PrecursorLimits
from .features import WindowFeatures

LEVELS = ("none", "suspect", "watch", "isolate")


@dataclass(frozen=True)
class Assessment:
    """一次前兆判定结果。"""

    level: str
    reasons: tuple[str, ...]
    window_s: float | None
    features: WindowFeatures

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "window_s": round(self.window_s, 1) if self.window_s is not None else None,
            "features": self.features.as_dict(),
        }


def assess(
    features: WindowFeatures,
    *,
    max_temp_c: float,
    protection_temp_c: float,
    limits: PrecursorLimits,
) -> Assessment:
    """按温压配合关系把当前滑窗分级为 none / suspect / watch / isolate。"""

    if not features.sufficient:
        return Assessment("none", ("insufficient_samples",), None, features)

    rate_hit = features.temp_rate_c_per_min >= limits.rate_warn_c_per_min
    volt_hit = features.volt_drop_mv >= limits.volt_drop_mv
    window_s: float | None = None
    if features.temp_rate_c_per_min > 0.0:
        window_s = max(0.0, (float(protection_temp_c) - float(max_temp_c)) / (features.temp_rate_c_per_min / 60.0))

    if not (rate_hit and volt_hit):
        if rate_hit or volt_hit:
            reasons = tuple(name for name, hit in (("temp_rate", rate_hit), ("volt_drop", volt_hit)) if hit)
            return Assessment("suspect", reasons, window_s, features)
        return Assessment("none", (), window_s, features)

    reasons = ["temp_rate", "volt_drop"]
    if features.divergence:
        reasons.append("divergence")
    if features.temp_rate_c_per_min >= limits.rate_trip_c_per_min:
        return Assessment("isolate", tuple(reasons + ["rate_trip"]), window_s, features)
    if features.volt_drop_mv >= limits.volt_collapse_mv:
        return Assessment("isolate", tuple(reasons + ["voltage_collapse"]), window_s, features)
    if window_s is not None and window_s <= limits.observe_s + limits.settle_margin_s:
        return Assessment("isolate", tuple(reasons + ["window_exhausted"]), window_s, features)
    return Assessment("watch", tuple(reasons), window_s, features)
