"""前兆判定、升级阶梯与失败回退。

覆盖四条主线：温压配合的分级判据、与降额/保护/消防对齐的升级阶梯、
动作失败时的回退与升档、以及现有保护始终优先。
"""

from __future__ import annotations

import pytest

from batterystore.config import StationConfig, config_to_mapping, default_config
from batterystore.errors import ConfigError, StoreError
from batterystore.precursor.decision import assess
from batterystore.precursor.features import WindowFeatures


def _sag(rack, delta_v: float) -> None:
    """让整簇电压同步偏移（不产生单体压差）。"""

    for cell in rack.cells():
        cell.update(voltage_v=cell.voltage_v + delta_v)


def _ramp(platform, clock, rack_id, *, start_c, step_c, volt_step=0.0, rounds, dt=5.0):
    """按固定步长推进温度与电压，逐周期评估，返回各周期报告。"""

    rack = platform.racks[rack_id]
    temp = start_c
    reports = []
    for _ in range(rounds):
        clock.advance(dt)
        temp += step_c
        rack.apply_temperature(temp)
        if volt_step:
            _sag(rack, volt_step)
        reports.append(platform.cycle(dt=dt))
    return reports


def _features(rate=0.0, drop=0.0, sufficient=True):
    return WindowFeatures(
        samples=8,
        span_s=60.0,
        temp_rate_c_per_min=rate,
        volt_drop_mv=drop,
        divergence=rate > 0.0 and drop > 0.0,
        sufficient=sufficient,
    )


def test_assess_requires_correlated_signals():
    limits = default_config().precursor
    protect = default_config().bms.protection_temp_c
    assert assess(_features(sufficient=False), max_temp_c=30.0, protection_temp_c=protect, limits=limits).level == "none"
    # 单特征只记疑似，不动作——这是把误报挡在动作之外的闸门。
    assert assess(_features(rate=1.0), max_temp_c=30.0, protection_temp_c=protect, limits=limits).level == "suspect"
    assert assess(_features(drop=40.0), max_temp_c=30.0, protection_temp_c=protect, limits=limits).level == "suspect"
    # 双特征互相印证且窗口充足才进入降流观察。
    watched = assess(_features(rate=1.0, drop=40.0), max_temp_c=30.0, protection_temp_c=protect, limits=limits)
    assert watched.level == "watch"
    assert watched.window_s > limits.observe_s + limits.settle_margin_s


def test_assess_isolates_when_window_gone():
    limits = default_config().precursor
    protect = default_config().bms.protection_temp_c
    # 速率进入加速段、电压崩塌、窗口容不下观察，三种都直切。
    fast = assess(_features(rate=3.0, drop=40.0), max_temp_c=30.0, protection_temp_c=protect, limits=limits)
    assert fast.level == "isolate" and "rate_trip" in fast.reasons
    collapsed = assess(_features(rate=1.0, drop=200.0), max_temp_c=30.0, protection_temp_c=protect, limits=limits)
    assert collapsed.level == "isolate" and "voltage_collapse" in collapsed.reasons
    exhausted = assess(_features(rate=1.0, drop=40.0), max_temp_c=45.0, protection_temp_c=protect, limits=limits)
    assert exhausted.level == "isolate" and "window_exhausted" in exhausted.reasons


def test_precursor_config_roundtrip_and_validation():
    config = default_config()
    assert StationConfig.from_mapping(config_to_mapping(config)).precursor == config.precursor
    raw = config_to_mapping(config)
    raw["limits"]["precursor"]["rate_warn_c_per_min"] = raw["limits"]["precursor"]["rate_trip_c_per_min"]
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)
    raw = config_to_mapping(config)
    raw["limits"]["precursor"]["recover_hold_s"] = raw["limits"]["precursor"]["observe_s"]
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)


def test_scan_reports_insufficient_samples(platform):
    report = platform.precursor_scan("rk-01")
    assert report["level"] == "normal"
    assert "insufficient_samples" in report["assessment"]["reasons"]


def test_single_feature_stays_suspect(platform, clock):
    reports = _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.1, rounds=14)
    marks = [report["precursor"]["rk-01"] for report in reports if report["precursor"].get("rk-01")]
    assert marks and all(mark["assessment"] in ("none", "suspect") for mark in marks)
    assert platform.precursor.ladder("rk-01").level == "normal"
    assert "precursor" not in platform.pcs.derate.sources


def test_watch_enters_after_confirm_rounds(platform, clock):
    reports = _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.1, volt_step=-8e-5, rounds=14)
    ladder = platform.precursor.ladder("rk-01")
    assert ladder.level == "watch"
    factor = platform.pcs.derate
    assert factor.sources.get("precursor") == platform.config.precursor.derate_ratio
    assert factor.ratio <= platform.config.precursor.derate_ratio + 1e-6
    # 首次达到 watch 判定的那一轮只计数不落定，确认轮次足够才动作。
    marks = [report["precursor"]["rk-01"] for report in reports if report["precursor"].get("rk-01", {}).get("assessment") == "watch"]
    assert marks[0]["level"] == "normal"
    assert marks[-1]["level"] == "watch"


def test_watch_recovers_after_sustained_clean(platform, clock):
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.1, volt_step=-8e-5, rounds=14)
    assert platform.precursor.ladder("rk-01").level == "watch"
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.0, rounds=90)
    assert platform.precursor.ladder("rk-01").level == "normal"
    assert "precursor" not in platform.pcs.derate.sources


def test_watch_derate_is_reapplied_if_overwritten(platform, clock):
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.1, volt_step=-8e-5, rounds=14)
    assert "precursor" in platform.pcs.derate.sources
    # 其它编排整写降额合成时会覆盖前兆源，下一周期必须补写回来。
    platform.sample_temperature(rack_id="rk-01", temperature_c=30.0)
    assert "precursor" not in platform.pcs.derate.sources
    clock.advance(5.0)
    platform.cycle()
    assert platform.pcs.derate.sources.get("precursor") == platform.config.precursor.derate_ratio


def test_observe_timeout_escalates_to_isolate(platform, clock):
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.05, volt_step=-8e-5, rounds=135)
    ladder = platform.precursor.ladder("rk-01")
    assert ladder.level == "isolate"
    assert platform.pcs.cut.is_tripped()
    assert platform.bms.latched("rk-01")


def test_rate_trip_isolates_without_observing(platform, clock):
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.2, volt_step=-8e-5, rounds=12)
    ladder = platform.precursor.ladder("rk-01")
    assert ladder.level == "isolate"
    assert platform.pcs.cut.is_tripped()
    assert platform.bms.latched("rk-01")
    # 直切不经过观察档。
    assert all(entry.get("to") != "watch" for entry in ladder.snapshot()["history"])


def test_window_exhaustion_isolates_without_observing(platform, clock):
    reports = _ramp(platform, clock, "rk-01", start_c=44.0, step_c=0.1, volt_step=-8e-5, rounds=12)
    assert platform.precursor.ladder("rk-01").level == "isolate"
    marks = [report["precursor"]["rk-01"] for report in reports if report["precursor"].get("rk-01")]
    assert any("window_exhausted" in mark["reasons"] for mark in marks)


def test_isolate_latches_until_manual_reset(platform, clock):
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.2, volt_step=-8e-5, rounds=12)
    ladder = platform.precursor.ladder("rk-01")
    assert ladder.level == "isolate"
    # 数据转好也不会自动退出闩锁档。
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.0, rounds=5)
    assert ladder.level == "isolate"
    platform.precursor_reset("rk-01")
    assert ladder.level == "normal"


def test_derate_failure_rolls_back_then_escalates(platform, clock, monkeypatch):
    original = platform.pcs.apply_derate

    def flaky(**ratios):
        if "precursor" in ratios:
            raise StoreError("注入的降流失败", component="pcs")
        return original(**ratios)

    monkeypatch.setattr(platform.pcs, "apply_derate", flaky)
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.1, volt_step=-8e-5, rounds=14)
    ladder = platform.precursor.ladder("rk-01")
    # 降流反复建立不起来：档位停在已确认态，达到重试上限后按更严一档处置。
    assert ladder.snapshot()["failures"].get("watch") == platform.config.precursor.action_retries
    assert ladder.level == "isolate"
    assert platform.pcs.cut.is_tripped()


def test_isolate_failure_rolls_back_and_suspends(platform, clock, monkeypatch):
    def broken_trip(reason):
        raise StoreError("注入的跳闸失败", component="pcs")

    monkeypatch.setattr(platform.pcs, "trip", broken_trip)
    _ramp(platform, clock, "rk-01", start_c=30.0, step_c=0.2, volt_step=-8e-5, rounds=14)
    ladder = platform.precursor.ladder("rk-01")
    # 切簇失败：退回上一档确认态，闩锁不留半截，达到上限后挂起等人工。
    assert ladder.level == "normal"
    assert ladder.manual_required is True
    assert platform.pcs.cut.is_tripped() is False
    assert platform.bms.latched("rk-01") is False
    platform.precursor_reset("rk-01")
    assert ladder.manual_required is False


def test_existing_protection_trips_before_precursor(platform, clock):
    rack = platform.racks["rk-01"]
    clock.advance(5.0)
    rack.apply_temperature(platform.config.bms.protection_temp_c + 1.0)
    report = platform.cycle()
    assert report["trips"] == ["rk-01:temperature_protection"]
    assert platform.pcs.cut.is_tripped()
    assert any(reason.startswith("protection:rk-01") for reason in platform.pcs.cut.reasons())
    assert platform.precursor.ladder("rk-01").level == "normal"


def test_suppression_marks_ladder(platform, clock):
    clock.advance(5.0)
    platform.cycle()
    report = platform.fire_drill(zone="Z1", smoke_ppm=1500.0, temperature_c=30.0)
    assert report["ok"] is True
    assert platform.precursor.ladder("rk-01").level == "suppression"
    assert platform.precursor.ladder("rk-01").snapshot()["level"] == "suppression"
