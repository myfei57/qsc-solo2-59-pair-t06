"""处置档位记录。

档位只记"已确认落地"的处置：编排试图升档但动作失败时，档位留在原处并记
一次回退，审计看到的就是现场真实所处的档，而不是"想去哪档"。档位序列与
既有处置链对齐——预警（新增）→ 降流观察（降额）→ 切簇隔离（保护），消防
联动仍由既有路径独立推进，切簇后不再自动降档，与跳闸需人工复位同一口径。
"""

from __future__ import annotations

import threading

ORDER = ("normal", "warning", "derate_watch", "isolated")


class EscalationLadder:
    """簇级处置档位与回退记录。"""

    def __init__(self) -> None:
        self._levels: dict[str, str] = {}
        self._entered: dict[str, float] = {}
        self._history: list[dict] = []
        self._fallbacks = 0
        self._lock = threading.RLock()

    def level(self, rack_id: str) -> str:
        """返回某簇当前已确认落地的档位。"""

        with self._lock:
            return self._levels.get(rack_id, "normal")

    def time_in_level(self, rack_id: str, now: float) -> float:
        """返回在当前档位已停留的秒数。"""

        with self._lock:
            return float(now) - self._entered.get(rack_id, float(now))

    def confirm(self, rack_id: str, level: str, now: float, detail: dict | None = None) -> dict | None:
        """确认一次档位落地；档位未变时不重复记录。"""

        if level not in ORDER:
            raise ValueError(f"未知的处置档位: {level}")
        with self._lock:
            previous = self._levels.get(rack_id, "normal")
            if previous == level:
                return None
            self._levels[rack_id] = level
            self._entered[rack_id] = float(now)
            entry = {
                "rack_id": rack_id,
                "from": previous,
                "to": level,
                "ts": round(float(now), 3),
                "detail": dict(detail or {}),
            }
            self._history.append(entry)
            return dict(entry)

    def fallback(self, rack_id: str, attempted: str, now: float, reason: str) -> dict:
        """记录一次升档失败：档位保持原处，记回退原因。"""

        with self._lock:
            self._fallbacks += 1
            entry = {
                "rack_id": rack_id,
                "attempted": attempted,
                "settled": self._levels.get(rack_id, "normal"),
                "reason": reason,
                "ts": round(float(now), 3),
            }
            self._history.append(entry)
            return dict(entry)

    def snapshot(self) -> dict:
        """返回档位现状与最近流水。"""

        with self._lock:
            return {
                "levels": {rack_id: self._levels.get(rack_id, "normal") for rack_id in sorted(self._levels)},
                "fallbacks": self._fallbacks,
                "history": [dict(entry) for entry in self._history[-20:]],
            }
