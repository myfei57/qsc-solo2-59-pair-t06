"""前兆处置编排。

顺序固定为「特征提取 → 分级判定 → 阶梯决策 → 动作执行 → 回读确认 → 落定档位」。
动作没确认就不换档；执行失败先反向撤销半截动作、恢复上一档的既有设置，再把
失败记进阶梯——任何时刻系统都停在一个已确认的档位上，不会悬在两档中间。
"""

from __future__ import annotations

from ..errors import InterlockError, StoreError


def evaluate_rack(control, pcs, bms, temp, rack, *, protection_temp_c: float, now: float) -> dict:
    """评估一簇的前兆并按阶梯执行处置，返回本轮报告。"""

    features = control.extract(rack.rack_id, now)
    assessment = control.assess(
        features,
        max_temp_c=rack.metrics().max_temperature_c,
        protection_temp_c=protection_temp_c,
    )
    ladder = control.ladder(rack.rack_id)
    target = ladder.consider(assessment, now)
    current = ladder.level

    steps: list[dict] = []
    transition: dict | None = None
    if target != current:
        transition = _transition(control, pcs, bms, temp, rack, ladder, target, now, steps)
    else:
        _maintain(control, pcs, bms, temp, rack, ladder, now, steps)
    return {
        "rack_id": rack.rack_id,
        "level": ladder.level,
        "assessment": assessment.as_dict(),
        "transition": transition,
        "steps": steps,
    }


def _transition(control, pcs, bms, temp, rack, ladder, target: str, now: float, steps: list[dict]) -> dict:
    if target == "watch":
        return _enter_watch(control, pcs, temp, ladder, now, steps)
    if target == "isolate":
        return _enter_isolate(control, pcs, bms, temp, rack, ladder, now, steps)
    return _exit_watch(control, pcs, temp, rack, ladder, now, steps)


def _enter_watch(control, pcs, temp, ladder, now: float, steps: list[dict]) -> dict:
    """进入观察档：下降流系数，回读确认后落定。"""

    ratio = control.limits.derate_ratio
    try:
        factor = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0, precursor=ratio)
        steps.append({"component": "pcs", "action": "derate", "detail": factor.as_dict()})
        if factor.sources.get("precursor") != ratio or factor.ratio > ratio + 1e-6:
            raise InterlockError("降流回读与下发不符", component="precursor")
        entry = ladder.commit("watch", now)
        return {"from": entry["from"], "to": "watch", "ok": True}
    except StoreError as error:
        _restore_derate(pcs, temp, steps)
        failure = ladder.rollback("watch", now, error.message)
        steps.append({"component": "precursor", "action": "rollback", "detail": failure})
        return {"from": failure["remain"], "to": "watch", "ok": False, "reason": error.message}


def _enter_isolate(control, pcs, bms, temp, rack, ladder, now: float, steps: list[dict]) -> dict:
    """进入保护档：咬合簇闩锁并跳闸，回读确认后落定。"""

    previous = ladder.level
    latched = False
    tripped = False
    try:
        bms.latch.engage(rack.rack_id, "precursor_isolate", now)
        latched = True
        steps.append({"component": "bms", "action": "latch", "detail": {"rack_id": rack.rack_id, "code": "precursor_isolate"}})
        trip = pcs.trip(f"precursor_isolate:{rack.rack_id}")
        tripped = True
        steps.append({"component": "pcs", "action": "trip", "detail": trip})
        if not pcs.cut.is_tripped() or not bms.latch.held(rack.rack_id, now):
            raise InterlockError("切簇回读与动作不符", component="precursor")
        entry = ladder.commit("isolate", now)
        return {"from": entry["from"], "to": "isolate", "ok": True}
    except StoreError as error:
        # 反向撤销半截动作：先恢复电气侧，再释放闩锁，最后恢复上一档设置。
        if tripped:
            steps.append({"component": "pcs", "action": "trip_undo", "detail": pcs.reset_trip()})
        if latched:
            bms.latch.release(rack.rack_id)
            steps.append({"component": "bms", "action": "unlatch", "detail": {"rack_id": rack.rack_id}})
        if previous == "watch":
            _restore_derate(pcs, temp, steps, precursor=control.limits.derate_ratio)
        failure = ladder.rollback("isolate", now, error.message)
        steps.append({"component": "precursor", "action": "rollback", "detail": failure})
        return {"from": failure["remain"], "to": "isolate", "ok": False, "reason": error.message}


def _exit_watch(control, pcs, temp, rack, ladder, now: float, steps: list[dict]) -> dict:
    """退出观察档：移除本簇的降流源，其余观察中的簇继续保留。"""

    remaining = control.station_derate_ratio(exclude=rack.rack_id)
    try:
        if remaining < 1.0:
            factor = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0, precursor=remaining)
            matched = factor.sources.get("precursor") == remaining
        else:
            factor = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0)
            matched = "precursor" not in factor.sources
        steps.append({"component": "pcs", "action": "derate", "detail": factor.as_dict()})
        if not matched:
            raise InterlockError("降额回读与下发不符", component="precursor")
        entry = ladder.commit("normal", now)
        return {"from": entry["from"], "to": "normal", "ok": True}
    except StoreError as error:
        # 撤不掉就停在观察档，降流继续保持，下一周期再试。
        failure = ladder.rollback("normal", now, error.message)
        steps.append({"component": "precursor", "action": "rollback", "detail": failure})
        return {"from": failure["remain"], "to": "normal", "ok": False, "reason": error.message}


def _restore_derate(pcs, temp, steps: list[dict], precursor: float | None = None) -> None:
    """把降额合成恢复到指定形态；恢复本身失败也要留痕，不再向外抛。"""

    try:
        if precursor is None:
            restored = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0)
        else:
            restored = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0, precursor=precursor)
        steps.append({"component": "pcs", "action": "derate_restore", "detail": restored.as_dict()})
    except StoreError as error:
        steps.append({"component": "pcs", "action": "derate_restore_failed", "detail": error.as_record()})


def _maintain(control, pcs, bms, temp, rack, ladder, now: float, steps: list[dict]) -> None:
    """档位不变时的保持性核对。"""

    if ladder.level == "watch":
        # 降流是观察档的立身之本：被其它编排覆盖了就补写回来；补写失败同样
        # 计入该档失败次数，达到上限后阶梯会按更严一档重新决策。
        if pcs.derate.sources.get("precursor") != control.limits.derate_ratio:
            ratio = control.limits.derate_ratio
            try:
                reapplied = pcs.apply_derate(temperature=temp.charge_limit_ratio(), grid=1.0, precursor=ratio)
                steps.append({"component": "pcs", "action": "derate_reapply", "detail": reapplied.as_dict()})
            except StoreError as error:
                failure = ladder.rollback("watch", now, error.message)
                steps.append({"component": "precursor", "action": "rollback", "detail": failure})
    elif ladder.level == "isolate":
        # 隔离档期间闩锁要保持咬合；PCS 被外力复位只留痕报警，不擅自再跳。
        if not bms.latch.held(rack.rack_id, now):
            bms.latch.engage(rack.rack_id, "precursor_isolate", now)
            steps.append({"component": "bms", "action": "relatch", "detail": {"rack_id": rack.rack_id}})
        if not pcs.cut.is_tripped():
            steps.append({"component": "precursor", "action": "isolate_broken", "detail": {"rack_id": rack.rack_id}})
