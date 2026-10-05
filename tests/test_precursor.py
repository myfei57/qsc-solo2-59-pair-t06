"""前兆判定与处置升级路径。

覆盖四条主线：特征与强弱判定、防抖监视、档位与回退记录、以及与降额/保护
联动的整站行为（含动作失败退回上一档）。
"""

from __future__ import annotations

import pytest

from batterystore.config import StationConfig, config_to_mapping, default_config
from batterystore.errors import ConfigError, StoreError
from batterystore.precursor import EscalationLadder, PrecursorFeatures, PrecursorMonitor, extract_features, judge
from batterystore.telemetry import TrendBuffer


def _step(platform, clock, rack_id: str, temperature_c: float, dt: float = 5.0) -> dict:
    """推进时钟、写入簇温并跑一个控制周期。"""

    clock.advance(dt)
    platform.racks[rack_id].apply_temperature(temperature_c)
    return platform.cycle()


def _ramp(platform, clock, rack_id: str, start: float, step: float, cycles: int) -> dict:
    """按固定步长抬温跑若干周期，返回最后一个周期报告。"""

    report = {}
    for index in range(cycles):
        report = _step(platform, clock, rack_id, start + step * index)
    return report


def test_features_extract_window_rates():
    buffer = TrendBuffer(period_s=5.0)
    for index in range(5):
        ts = 100.0 + index * 5.0
        buffer.append("rk-01.temperature", ts, 30.0 + index * 0.1)
        buffer.append("rk-01.voltage", ts, 307.2 - index * 0.01)
        buffer.append("rk-01.delta_mv", ts, index * 10.0)
    features = extract_features(buffer, "rk-01", 60.0, 120.0)
    assert features.temp_rise_c_per_min == pytest.approx(1.2)
    assert features.voltage_drop_v == pytest.approx(0.04)
    assert features.delta_rise_mv == pytest.approx(40.0)
    assert features.samples == 5
    sparse = extract_features(buffer, "rk-02", 60.0, 120.0)
    assert sparse.temp_rise_c_per_min == 0.0
    assert sparse.samples == 0


def test_judge_levels(config):
    limits = config.precursor
    calm = PrecursorFeatures(0.5, 0.01, 10.0, 5, 20.0)
    assert judge(calm, limits)["level"] == "none"
    temp_only = PrecursorFeatures(1.5, 0.0, 0.0, 5, 20.0)
    assert judge(temp_only, limits)["level"] == "weak"
    volt_only = PrecursorFeatures(0.0, 0.06, 0.0, 5, 20.0)
    assert judge(volt_only, limits)["level"] == "weak"
    combined = judge(PrecursorFeatures(1.5, 0.06, 0.0, 5, 20.0), limits)
    assert combined["level"] == "strong"
    assert "temp_voltage_combined" in combined["reasons"]
    severe_temp = judge(PrecursorFeatures(3.5, 0.0, 0.0, 5, 20.0), limits)
    assert severe_temp["level"] == "strong"
    severe_delta = judge(PrecursorFeatures(0.0, 0.0, 90.0, 5, 20.0), limits)
    assert severe_delta["level"] == "strong"


def test_monitor_confirms_after_rounds_and_clears(config):
    monitor = PrecursorMonitor(config.precursor)
    assert monitor.evaluate("rk-01", "weak")["state"] == "pending"
    assert monitor.evaluate("rk-01", "weak")["state"] == "pending"
    raised = monitor.evaluate("rk-01", "weak")
    assert raised["state"] == "raised" and raised["level"] == "weak"
    upgraded = monitor.evaluate("rk-01", "strong")
    assert upgraded["state"] == "upgraded" and upgraded["level"] == "strong"
    for _ in range(config.precursor.clear_rounds - 1):
        assert monitor.evaluate("rk-01", "none")["state"] == "active"
    cleared = monitor.evaluate("rk-01", "none")
    assert cleared["state"] == "cleared" and cleared["previous"] == "strong"
    assert monitor.snapshot()["active"] == {}


def test_monitor_drops_single_round_blips(config):
    monitor = PrecursorMonitor(config.precursor)
    monitor.evaluate("rk-01", "weak")
    monitor.evaluate("rk-01", "weak")
    assert monitor.evaluate("rk-01", "none")["state"] == "normal"
    assert monitor.evaluate("rk-01", "weak")["state"] == "pending"
    assert monitor.snapshot()["pending_hits"] == {"rk-01": 1}


def test_ladder_records_confirm_and_fallback():
    ladder = EscalationLadder()
    assert ladder.level("rk-01") == "normal"
    entry = ladder.confirm("rk-01", "warning", 100.0, {"reasons": ["weak_precursor"]})
    assert entry["to"] == "warning"
    assert ladder.confirm("rk-01", "warning", 105.0) is None
    ladder.confirm("rk-01", "derate_watch", 110.0)
    assert ladder.time_in_level("rk-01", 140.0) == pytest.approx(30.0)
    fallback = ladder.fallback("rk-01", "isolated", 150.0, "isolate_failed")
    assert fallback["settled"] == "derate_watch"
    assert ladder.level("rk-01") == "derate_watch"
    snapshot = ladder.snapshot()
    assert snapshot["fallbacks"] == 1
    assert snapshot["levels"] == {"rk-01": "derate_watch"}
    with pytest.raises(ValueError):
        ladder.confirm("rk-01", "sideways", 160.0)


def test_config_rejects_inverted_precursor_thresholds():
    raw = config_to_mapping(default_config())
    raw["limits"]["precursor"]["temp_rise_warn_c_per_min"] = 9.0
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)
    raw = config_to_mapping(default_config())
    raw["limits"]["precursor"]["delta_rise_warn_mv"] = 200.0
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)


def test_weak_precursor_derates_then_recovers(platform, clock):
    _ramp(platform, clock, "rk-01", 30.0, 0.1, 3)
    assert platform.guard.ladder.level("rk-01") == "normal"
    report = _step(platform, clock, "rk-01", 30.3)
    assert report["precursor"]["rk-01"]["level"] == "warning"
    assert platform.pcs_snapshot()["derate_ratio"] == 1.0
    report = _step(platform, clock, "rk-01", 30.4)
    assert report["precursor"]["rk-01"]["level"] == "derate_watch"
    assert platform.pcs_snapshot()["derate_ratio"] == pytest.approx(platform.config.precursor.observe_derate_ratio)
    for _ in range(6):
        report = _step(platform, clock, "rk-01", 30.4)
    assert platform.guard.ladder.level("rk-01") == "derate_watch"
    clock.advance(platform.config.precursor.observe_s + 1.0)
    report = _step(platform, clock, "rk-01", 30.4)
    assert report["precursor"]["rk-01"]["level"] == "normal"
    assert platform.pcs_snapshot()["derate_ratio"] == 1.0


def test_strong_precursor_isolates_immediately(platform, clock):
    report = _ramp(platform, clock, "rk-01", 30.0, 0.3, 4)
    assert report["precursor"]["rk-01"]["level"] == "isolated"
    assert report["trips"] == []
    assert platform.pcs.cut.is_tripped() is True
    assert platform.guard.ladder.level("rk-01") == "isolated"
    assert platform.bms.faults.counter("rk-01") == 1


def test_voltage_axis_precursor_derates(platform, clock):
    _step(platform, clock, "rk-01", 30.0)
    platform.disturb_cell(rack_id="rk-01", cell_index=0, delta_v=-0.06)
    _step(platform, clock, "rk-01", 30.0)
    _step(platform, clock, "rk-01", 30.0)
    report = _step(platform, clock, "rk-01", 30.0)
    assert report["precursor"]["rk-01"]["level"] == "warning"
    report = _step(platform, clock, "rk-01", 30.0)
    assert report["precursor"]["rk-01"]["level"] == "derate_watch"
    assert platform.pcs.cut.is_tripped() is False


def test_temp_margin_short_skips_observation(platform, clock):
    _ramp(platform, clock, "rk-01", 51.0, 0.1, 4)
    assert platform.guard.ladder.level("rk-01") == "warning"
    report = _step(platform, clock, "rk-01", 51.4)
    assert report["precursor"]["rk-01"]["level"] == "isolated"
    assert platform.pcs.cut.is_tripped() is True


def test_observe_expiry_isolates(platform, clock):
    _ramp(platform, clock, "rk-01", 30.0, 0.1, 5)
    assert platform.guard.ladder.level("rk-01") == "derate_watch"
    clock.advance(platform.config.precursor.observe_s + 1.0)
    report = _step(platform, clock, "rk-01", 30.5)
    assert report["precursor"]["rk-01"]["level"] == "isolated"
    assert platform.pcs.cut.is_tripped() is True


def test_isolate_failure_falls_back_to_previous_level(platform, clock, monkeypatch):
    def rejected(reason: str) -> dict:
        raise StoreError("跳闸回路拒动", component="pcs")

    monkeypatch.setattr(platform.pcs, "trip", rejected)
    report = _ramp(platform, clock, "rk-01", 30.0, 0.3, 4)
    assert report["precursor"]["rk-01"]["violations"] == ["isolate_failed"]
    assert platform.guard.ladder.level("rk-01") == "normal"
    assert platform.guard.snapshot().as_dict()["ladder"]["fallbacks"] == 1
    assert platform.pcs.cut.is_tripped() is False
    monkeypatch.undo()
    report = _step(platform, clock, "rk-01", 31.2)
    assert report["precursor"]["rk-01"]["level"] == "isolated"
    assert platform.pcs.cut.is_tripped() is True


def test_derate_failure_stays_at_warning(platform, clock, monkeypatch):
    def rejected(**ratios: float) -> dict:
        raise StoreError("降额下发被拒", component="pcs")

    _ramp(platform, clock, "rk-01", 30.0, 0.1, 4)
    assert platform.guard.ladder.level("rk-01") == "warning"
    monkeypatch.setattr(platform.pcs, "apply_derate", rejected)
    report = _step(platform, clock, "rk-01", 30.4)
    assert report["precursor"]["rk-01"]["violations"] == ["derate_apply_failed"]
    assert platform.guard.ladder.level("rk-01") == "warning"
    assert platform.pcs_snapshot()["derate_ratio"] == 1.0


def test_release_precursor_clears_state(platform, clock):
    _ramp(platform, clock, "rk-01", 30.0, 0.3, 4)
    assert platform.guard.ladder.level("rk-01") == "isolated"
    platform.release_fire()
    released = platform.release_precursor("rk-01")
    assert released["was_active"] is True
    assert platform.guard.ladder.level("rk-01") == "normal"
    report = _step(platform, clock, "rk-01", 30.9)
    assert report["precursor"]["rk-01"]["level"] == "normal"
    assert platform.pcs.cut.is_tripped() is False


def test_normal_cycles_stay_quiet(platform, clock):
    report = _ramp(platform, clock, "rk-01", 30.0, 0.0, 4)
    assert report["precursor"]["rk-01"]["level"] == "normal"
    assert platform.guard.snapshot().as_dict()["monitor"]["active"] == {}
    assert platform.pcs_snapshot()["derate_ratio"] == 1.0
