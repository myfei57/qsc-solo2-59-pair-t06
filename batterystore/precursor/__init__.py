"""前兆判定：特征提取、强弱判定、防抖监视与处置档位。"""

from __future__ import annotations

from .features import PrecursorFeatures, extract_features
from .guard import PrecursorGuard
from .ladder import EscalationLadder
from .monitor import PrecursorMonitor
from .snapshot import PrecursorSnapshot
from .verdict import judge

__all__ = [
    "EscalationLadder",
    "PrecursorFeatures",
    "PrecursorGuard",
    "PrecursorMonitor",
    "PrecursorSnapshot",
    "extract_features",
    "judge",
]
