"""前兆特征提取。

温升速率、簇电压跌落与压差劣化都从趋势缓冲的滑窗里取，控制台展示的趋势与
判定用的是同一份采样，不会出现"图上看着没事、判定却报警"的两套口径。窗口
内不足两个样本时各特征取零——刚启动的簇没有历史可判，按正常处理而不是报
缺数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..telemetry import Sample, TrendBuffer


@dataclass(frozen=True)
class PrecursorFeatures:
    """一簇在一个滑窗上的前兆特征。"""

    temp_rise_c_per_min: float
    voltage_drop_v: float
    delta_rise_mv: float
    samples: int
    span_s: float

    def as_dict(self) -> dict:
        return {
            "temp_rise_c_per_min": round(self.temp_rise_c_per_min, 3),
            "voltage_drop_v": round(self.voltage_drop_v, 4),
            "delta_rise_mv": round(self.delta_rise_mv, 2),
            "samples": self.samples,
            "span_s": round(self.span_s, 1),
        }


def _window(buffer: TrendBuffer, signal: str, window_s: float, now: float) -> list[Sample]:
    """取信号在滑窗内的样本序列。"""

    return [sample for sample in buffer.series(signal, limit=512) if now - sample.ts <= window_s]


def _span_s(samples: list[Sample]) -> float:
    """样本序列覆盖的秒数。"""

    if len(samples) < 2:
        return 0.0
    return samples[-1].ts - samples[0].ts


def extract_features(buffer: TrendBuffer, rack_id: str, window_s: float, now: float) -> PrecursorFeatures:
    """从趋势缓冲提取一簇的滑窗特征。

    温升速率按窗口首尾差折算成每分钟摄氏度；电压跌落与压差劣化取窗口内的
    总量（首减末 / 末减首），只计劣化方向，恢复方向不记前兆。
    """

    temperatures = _window(buffer, f"{rack_id}.temperature", window_s, now)
    voltages = _window(buffer, f"{rack_id}.voltage", window_s, now)
    deltas = _window(buffer, f"{rack_id}.delta_mv", window_s, now)
    temp_span = _span_s(temperatures)
    temp_rise = 0.0
    if temp_span > 0.0:
        temp_rise = (temperatures[-1].value - temperatures[0].value) / temp_span * 60.0
    voltage_drop = 0.0
    if len(voltages) >= 2:
        voltage_drop = max(0.0, voltages[0].value - voltages[-1].value)
    delta_rise = 0.0
    if len(deltas) >= 2:
        delta_rise = max(0.0, deltas[-1].value - deltas[0].value)
    return PrecursorFeatures(
        temp_rise_c_per_min=temp_rise,
        voltage_drop_v=voltage_drop,
        delta_rise_mv=delta_rise,
        samples=min(len(temperatures), len(voltages), len(deltas)),
        span_s=temp_span,
    )
