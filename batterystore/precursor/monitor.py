"""前兆防抖监视。

前兆判定每个控制周期都会触发一轮，单轮越线不足以打断生产：弱/强前兆都要
连续 ``confirm_rounds`` 轮越线才确认预警，预警要连续 ``clear_rounds`` 轮
恢复正常才解除——与故障登记的确认轮次同一套防抖口径。已确认期间出现强前兆
立即升级，不再重新计轮：升级不额外打断生产，拖延才损失处置窗口。
"""

from __future__ import annotations

import threading

from ..config import PrecursorLimits
from .verdict import NONE, STRONG


class PrecursorMonitor:
    """带确认与解除轮次的簇级前兆监视器。"""

    def __init__(self, limits: PrecursorLimits) -> None:
        self._limits = limits
        self._hits: dict[str, int] = {}
        self._clears: dict[str, int] = {}
        self._active: dict[str, str] = {}
        self._lock = threading.RLock()

    def evaluate(self, rack_id: str, verdict_level: str) -> dict:
        """登记一轮判定，返回防抖后的预警状态。"""

        with self._lock:
            if verdict_level == NONE:
                self._hits[rack_id] = 0
                if rack_id not in self._active:
                    self._clears[rack_id] = 0
                    return {"rack_id": rack_id, "state": "normal", "level": None}
                clears = self._clears.get(rack_id, 0) + 1
                self._clears[rack_id] = clears
                if clears >= self._limits.clear_rounds:
                    previous = self._active.pop(rack_id)
                    self._clears[rack_id] = 0
                    return {
                        "rack_id": rack_id,
                        "state": "cleared",
                        "level": None,
                        "previous": previous,
                        "rounds": clears,
                    }
                return {
                    "rack_id": rack_id,
                    "state": "active",
                    "level": self._active[rack_id],
                    "clearing": clears,
                    "required": self._limits.clear_rounds,
                }
            self._clears[rack_id] = 0
            current = self._active.get(rack_id)
            if current is None:
                hits = self._hits.get(rack_id, 0) + 1
                self._hits[rack_id] = hits
                if hits >= self._limits.confirm_rounds:
                    self._active[rack_id] = verdict_level
                    self._hits[rack_id] = 0
                    return {"rack_id": rack_id, "state": "raised", "level": verdict_level, "rounds": hits}
                return {
                    "rack_id": rack_id,
                    "state": "pending",
                    "level": None,
                    "hits": hits,
                    "required": self._limits.confirm_rounds,
                }
            if verdict_level == STRONG and current != STRONG:
                self._active[rack_id] = STRONG
                return {"rack_id": rack_id, "state": "upgraded", "level": STRONG, "previous": current}
            return {"rack_id": rack_id, "state": "active", "level": current}

    def reset(self, rack_id: str) -> bool:
        """清掉某簇的预警与计轮状态（人工解除用），返回清除前是否在预警中。"""

        with self._lock:
            self._hits.pop(rack_id, None)
            self._clears.pop(rack_id, None)
            return self._active.pop(rack_id, None) is not None

    def snapshot(self) -> dict:
        """返回监视器状态，供快照与审计读取。"""

        with self._lock:
            return {
                "active": dict(self._active),
                "pending_hits": {rack_id: hits for rack_id, hits in sorted(self._hits.items()) if hits > 0},
                "clearing": {rack_id: clears for rack_id, clears in sorted(self._clears.items()) if clears > 0},
            }
