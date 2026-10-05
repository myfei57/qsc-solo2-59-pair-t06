"""前兆处置编排。

每个控制周期对每簇走一遍：取判定 → 定目标档位 → 执行并核对落地。处置方向
默认降流观察——前兆阶段置信度有限，直接切簇会让误报代价过高；满足下列任一
条件则跳过观察直接切簇：

* 强前兆（温升与电压双轴同越预警线，或任一轴达重警线）；
* 温度裕度不足（距保护线不到 ``temp_margin_c``，观察窗口已经不存在）；
* 该簇故障闩锁保持中，或保护判据已越线，或 PCS 已跳闸；
* 降流观察期满仍未确认恢复（观察被证伪）。

升档动作只有确认落地才记档位；落不了地就退回上一档的已确认动作集（切簇失败
退回降流观察、降流失败退回预警），绝不停在半升不升的中间态。降档（恢复）
同样要核对动作生效：释放降额失败就维持降额——往保守方向失败，不往危险
方向停。切簇后不再自动降档，与跳闸需人工复位同一口径。
"""

from __future__ import annotations

from ..errors import StoreError

_EPSILON = 1e-9


def _observe_applicable(guard, pcs, bms, rack, bms_limits) -> list[str]:
    """返回阻止降流观察的原因；为空表示可以降流观察。"""

    blocks: list[str] = []
    metrics = rack.metrics()
    if metrics.max_temperature_c >= bms_limits.protection_temp_c - guard.limits.temp_margin_c:
        blocks.append("temp_margin_short")
    if bms.latched(rack.rack_id):
        blocks.append("fault_latched")
    if pcs.cut.is_tripped():
        blocks.append("pcs_tripped")
    return blocks


def _target_level(guard, pcs, bms, rack, state: dict, bms_limits, protection_violations: list[str], now: float) -> tuple:
    """按防抖状态、保护越线与观察节拍定目标档位。"""

    current = guard.ladder.level(rack.rack_id)
    if current == "isolated":
        return "isolated", ["isolation_latched"]
    if protection_violations:
        return "isolated", ["protection_line_crossed"]
    if pcs.cut.is_tripped():
        return "isolated", ["pcs_tripped"]
    stage = state["state"]
    level = state.get("level")
    if stage in ("raised", "upgraded", "active") and level:
        if level == "strong":
            return "isolated", ["strong_precursor"]
        if current == "normal":
            return "warning", ["weak_precursor"]
        if current == "warning":
            blocks = _observe_applicable(guard, pcs, bms, rack, bms_limits)
            if blocks:
                return "isolated", blocks
            return "derate_watch", ["weak_precursor"]
        if guard.ladder.time_in_level(rack.rack_id, now) >= guard.limits.observe_s:
            return "isolated", ["observe_expired"]
        return "derate_watch", ["observe_in_progress"]
    if stage in ("normal", "cleared"):
        if current == "warning":
            return "normal", ["warning_cleared"]
        if current == "derate_watch" and guard.ladder.time_in_level(rack.rack_id, now) >= guard.limits.observe_s:
            return "normal", ["observed_clear"]
    return current, ["hold"]


def _apply_derate(pcs, derate_sources: dict, ratio: float | None) -> tuple:
    """写入或撤下前兆降额来源并重新合成，返回 (合成系数, 错误记录)。"""

    if ratio is None:
        derate_sources.pop("precursor", None)
    else:
        derate_sources["precursor"] = ratio
    try:
        return pcs.apply_derate(**derate_sources).ratio, None
    except StoreError as error:
        return None, error.as_record()


def dispose_precursor(guard, pcs, bms, rack, buffer, bms_limits, *, derate_sources: dict, protection_violations: list[str], now: float) -> dict:
    """对一簇执行一轮前兆处置，返回带步骤与违约项的报告。"""

    evaluation = guard.evaluate(rack.rack_id, buffer, now)
    target, reasons = _target_level(
        guard, pcs, bms, rack, evaluation["state"], bms_limits, protection_violations, now
    )
    current = guard.ladder.level(rack.rack_id)
    steps: list[dict] = [
        {
            "component": "precursor",
            "action": "evaluate",
            "detail": {**evaluation["features"], "verdict": evaluation["verdict"]["level"], "state": evaluation["state"]["state"]},
        }
    ]
    violations: list[str] = []
    settled = current

    if target == current:
        if current == "derate_watch":
            # 观察档每周期重申降额：其它来源重算系数时不允许把前兆系数冲掉。
            _, error = _apply_derate(pcs, derate_sources, guard.limits.observe_derate_ratio)
            if error is not None:
                violations.append("derate_reassert_failed")
                steps.append({"component": "pcs", "action": "derate_rejected", "detail": error})
        steps.append({"component": "precursor", "action": "hold", "detail": {"level": current, "reasons": reasons}})
    elif target == "warning":
        entry = guard.ladder.confirm(rack.rack_id, "warning", now, {"reasons": reasons})
        settled = "warning"
        steps.append({"component": "precursor", "action": "warn", "detail": {"entry": entry, "reasons": reasons}})
    elif target == "derate_watch":
        ratio, error = _apply_derate(pcs, derate_sources, guard.limits.observe_derate_ratio)
        if error is not None:
            steps.append({"component": "pcs", "action": "derate_rejected", "detail": error})
        if ratio is not None and ratio <= guard.limits.observe_derate_ratio + _EPSILON:
            entry = guard.ladder.confirm(rack.rack_id, "derate_watch", now, {"reasons": reasons, "ratio": ratio})
            settled = "derate_watch"
            steps.append({"component": "pcs", "action": "derate", "detail": {"ratio": ratio, "entry": entry}})
        else:
            _apply_derate(pcs, derate_sources, None)
            fallback = guard.ladder.fallback(rack.rack_id, "derate_watch", now, "derate_apply_failed")
            violations.append("derate_apply_failed")
            steps.append({"component": "precursor", "action": "fallback", "detail": fallback})
    elif target == "isolated":
        try:
            if not pcs.cut.is_tripped():
                pcs.trip(f"precursor:{rack.rack_id}")
                bms.report_fault(rack.rack_id, "precursor_isolate")
            landed = pcs.cut.is_tripped()
        except StoreError as error:
            landed = False
            steps.append({"component": "pcs", "action": "isolate_rejected", "detail": error.as_record()})
        if landed:
            derate_sources.pop("precursor", None)
            entry = guard.ladder.confirm(rack.rack_id, "isolated", now, {"reasons": reasons})
            settled = "isolated"
            steps.append({"component": "pcs", "action": "isolate", "detail": {"tripped": True, "entry": entry}})
        else:
            # 退回上一档：把当前已确认档位的动作集重新执行一遍。
            if current == "derate_watch":
                _apply_derate(pcs, derate_sources, guard.limits.observe_derate_ratio)
            fallback = guard.ladder.fallback(rack.rack_id, "isolated", now, "isolate_failed")
            violations.append("isolate_failed")
            steps.append({"component": "precursor", "action": "fallback", "detail": fallback})
    elif target == "normal":
        _, error = _apply_derate(pcs, derate_sources, None)
        if error is not None:
            steps.append({"component": "pcs", "action": "release_rejected", "detail": error})
        if error is None and "precursor" not in derate_sources:
            entry = guard.ladder.confirm(rack.rack_id, "normal", now, {"reasons": reasons})
            settled = "normal"
            steps.append({"component": "precursor", "action": "release", "detail": {"entry": entry, "reasons": reasons}})
        else:
            # 降额释放失败就维持降额：往保守方向失败，不停在中间态。
            _apply_derate(pcs, derate_sources, guard.limits.observe_derate_ratio)
            fallback = guard.ladder.fallback(rack.rack_id, "normal", now, "derate_release_failed")
            violations.append("derate_release_failed")
            steps.append({"component": "precursor", "action": "fallback", "detail": fallback})

    return {
        "rack_id": rack.rack_id,
        "from": current,
        "to": settled,
        "target": target,
        "reasons": reasons,
        "evaluation": evaluation,
        "steps": steps,
        "violations": violations,
        "ok": not violations,
    }
