"""前兆组件快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrecursorSnapshot:
    """前兆监视与处置档位某一时刻的状态。"""

    monitor: dict
    ladder: dict

    def as_dict(self) -> dict:
        return {
            "monitor": dict(self.monitor),
            "ladder": dict(self.ladder),
        }
