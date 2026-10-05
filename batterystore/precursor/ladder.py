"""升级阶梯。

前兆处置的档位与现有降额、保护、消防一一对应：watch 落在降额档（降流观察），
isolate 落在保护档（切簇隔离），suppression 就是消防联动本身。档位只升不降，
唯一的例外是 watch 观察干净后退回 normal；isolate 与 suppression 闩锁，必须
人工复位。

每次换档都是"先执行、后确认、再落定"：动作没确认前档位不动，执行失败退回
上一档已确认状态，绝不停在两档中间。失败按档计数，同一档失败达到上限就不
再尝试该档——降流观察建立不起来时，只能按更严的一档处置；切簇也失败时
挂起等人工，而不是反复空转。
"""

from __future__ import annotations

from ..config import PrecursorLimits
from .decision import Assessment

LADDER = ("normal", "watch", "isolate", "suppression")


class EscalationLadder:
    """一簇的处置档位状态机。"""

    def __init__(self, limits: PrecursorLimits) -> None:
        self._limits = limits
        self._level = "normal"
        self._pending = 0
        self._clean_since: float | None = None
        self._watch_since: float | None = None
        self._failures: dict[str, int] = {}
        self._manual_required = False
        self._history: list[dict] = []

    @property
    def level(self) -> str:
        """返回当前已确认落定的档位。"""

        return self._level

    @property
    def manual_required(self) -> bool:
        """是否已因动作反复失败而挂起，等待人工介入。"""

        return self._manual_required

    def consider(self, assessment: Assessment, now: float) -> str:
        """根据本轮判定给出目标档位（可能就是当前档）。"""

        if self._level in ("isolate", "suppression") or self._manual_required:
            return self._level
        desired = self._desired(assessment, float(now))
        if desired == "watch" and self._failures.get("watch", 0) >= self._limits.action_retries:
            # 降流观察反复建立不起来，观察手段不成立，按更严的一档处置。
            desired = "isolate"
        if desired == "isolate" and self._failures.get("isolate", 0) >= self._limits.action_retries:
            self._manual_required = True
            return self._level
        return desired

    def _desired(self, assessment: Assessment, now: float) -> str:
        if assessment.level == "isolate":
            return "isolate"
        if self._level == "watch":
            if assessment.level == "none":
                if self._clean_since is None:
                    self._clean_since = now
                if now - self._clean_since >= self._limits.recover_hold_s:
                    return "normal"
            else:
                self._clean_since = None
                if self._watch_since is not None and now - self._watch_since > self._limits.observe_s:
                    # 观察期耗尽仍未干净，观察失败，升级到切簇。
                    return "isolate"
            return "watch"
        if assessment.level == "watch":
            self._pending += 1
            if self._pending >= self._limits.confirm_rounds:
                return "watch"
            return "normal"
        if assessment.level == "none":
            self._pending = 0
        return "normal"

    def commit(self, level: str, now: float) -> dict:
        """动作确认后落定档位，返回换档记录。"""

        if level not in LADDER:
            raise ValueError(f"未知的处置档位: {level}")
        previous = self._level
        self._level = level
        if level == "watch":
            self._watch_since = float(now)
            self._pending = 0
            self._clean_since = None
        elif level == "normal":
            self._watch_since = None
            self._clean_since = None
            self._pending = 0
        entry = {"from": previous, "to": level, "at": float(now)}
        self._history.append(entry)
        return dict(entry)

    def rollback(self, level: str, now: float, reason: str) -> dict:
        """动作执行失败：档位不动，失败按档计数，等待下一轮重新决策。"""

        self._failures[level] = self._failures.get(level, 0) + 1
        entry = {
            "failed": level,
            "at": float(now),
            "reason": reason,
            "remain": self._level,
            "failures": self._failures[level],
        }
        self._history.append(entry)
        return dict(entry)

    def note_suppression(self, now: float) -> dict:
        """消防联动已动作，阶梯跟进到消防档并闩锁。"""

        return self.commit("suppression", now)

    def reset(self, now: float) -> dict:
        """人工复位：从闩锁档回到正常档，清空失败计数与挂起标记。"""

        self._failures.clear()
        self._manual_required = False
        return self.commit("normal", now)

    def snapshot(self) -> dict:
        """返回阶梯状态快照。"""

        return {
            "level": self._level,
            "pending": self._pending,
            "watch_since": self._watch_since,
            "failures": dict(self._failures),
            "manual_required": self._manual_required,
            "history": [dict(entry) for entry in self._history[-10:]],
        }
