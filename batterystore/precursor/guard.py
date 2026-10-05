"""前兆判定组件。

``PrecursorGuard`` 把特征提取、强弱判定、防抖监视与处置档位收在一个入口后面：
上层每个控制周期只问一件事——这簇现在的前兆状态是什么；档位的确认为编排
留出落点，判定与动作分开，与 BMS「判据归判据、动作归编排」同一套分工。
"""

from __future__ import annotations

from ..config import PrecursorLimits
from ..telemetry import TrendBuffer
from .features import extract_features
from .ladder import EscalationLadder
from .monitor import PrecursorMonitor
from .snapshot import PrecursorSnapshot
from .verdict import judge


class PrecursorGuard:
    """前兆监视与处置档位组件。"""

    def __init__(self, limits: PrecursorLimits) -> None:
        self._limits = limits
        self._monitor = PrecursorMonitor(limits)
        self._ladder = EscalationLadder()

    @property
    def limits(self) -> PrecursorLimits:
        return self._limits

    @property
    def monitor(self) -> PrecursorMonitor:
        return self._monitor

    @property
    def ladder(self) -> EscalationLadder:
        return self._ladder

    def evaluate(self, rack_id: str, buffer: TrendBuffer, now: float) -> dict:
        """提取一簇的滑窗特征并登记一轮判定，返回特征、结论与防抖状态。"""

        features = extract_features(buffer, rack_id, self._limits.window_s, now)
        verdict = judge(features, self._limits)
        state = self._monitor.evaluate(rack_id, verdict["level"])
        return {
            "rack_id": rack_id,
            "features": features.as_dict(),
            "verdict": verdict,
            "state": state,
        }

    def snapshot(self) -> PrecursorSnapshot:
        """返回前兆组件状态快照。"""

        return PrecursorSnapshot(monitor=self._monitor.snapshot(), ladder=self._ladder.snapshot())
