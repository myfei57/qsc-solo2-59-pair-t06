"""前兆判定组件。

把特征提取、分级判据与升级阶梯收在一个入口后面：控制周期对每簇问一次
"该在哪一档"，动作由编排层执行并回执。判据本身不碰执行器，与现有
BMS／PCS 保护判据一样保持"判定与动作分离"。
"""

from __future__ import annotations

from ..config import PrecursorLimits
from ..telemetry import TrendBuffer
from .decision import Assessment, assess
from .features import WindowFeatures, extract_features
from .ladder import EscalationLadder


class PrecursorControl:
    """簇级前兆判定与升级阶梯。"""

    def __init__(self, limits: PrecursorLimits, trend: TrendBuffer) -> None:
        self._limits = limits
        self._trend = trend
        self._ladders: dict[str, EscalationLadder] = {}

    @property
    def limits(self) -> PrecursorLimits:
        return self._limits

    def ladder(self, rack_id: str) -> EscalationLadder:
        """取一簇的升级阶梯，首次访问时建立。"""

        ladder = self._ladders.get(rack_id)
        if ladder is None:
            ladder = EscalationLadder(self._limits)
            self._ladders[rack_id] = ladder
        return ladder

    def extract(self, rack_id: str, now: float) -> WindowFeatures:
        """提取一簇当前滑窗的前兆特征。"""

        return extract_features(self._trend, rack_id, self._limits, now)

    def assess(self, features: WindowFeatures, *, max_temp_c: float, protection_temp_c: float) -> Assessment:
        """对一组滑窗特征做分级判定。"""

        return assess(
            features,
            max_temp_c=max_temp_c,
            protection_temp_c=protection_temp_c,
            limits=self._limits,
        )

    def station_derate_ratio(self, exclude: str | None = None) -> float:
        """站内其余仍处于观察档的簇要求的最低降流系数（没有则 1.0）。"""

        ratios = [
            self._limits.derate_ratio
            for rack_id, ladder in self._ladders.items()
            if rack_id != exclude and ladder.level == "watch"
        ]
        return min(ratios) if ratios else 1.0

    def snapshot(self) -> dict:
        """返回前兆判定状态快照。"""

        return {
            "limits": {
                "rate_warn_c_per_min": self._limits.rate_warn_c_per_min,
                "rate_trip_c_per_min": self._limits.rate_trip_c_per_min,
                "volt_drop_mv": self._limits.volt_drop_mv,
                "volt_collapse_mv": self._limits.volt_collapse_mv,
                "observe_s": self._limits.observe_s,
                "derate_ratio": self._limits.derate_ratio,
            },
            "racks": {rack_id: ladder.snapshot() for rack_id, ladder in sorted(self._ladders.items())},
        }
