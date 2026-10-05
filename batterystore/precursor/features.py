"""前兆特征提取。

温升速率与电压变化从趋势缓冲的同一个滑窗里取：速率用最小二乘斜率，抵抗
单点采样抖动；电压跌落取窗口跨度内的净变化。两个特征共用时间基准，
"温度在升而电压在落"的背离关系才有可比性。样本不足时宁可判数据不够，
也不拿半截窗口凑数。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PrecursorLimits
from ..telemetry import TrendBuffer


@dataclass(frozen=True)
class WindowFeatures:
    """一个滑窗内提取出的前兆特征。"""

    samples: int
    span_s: float
    temp_rate_c_per_min: float
    volt_drop_mv: float
    divergence: bool
    sufficient: bool

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "span_s": round(self.span_s, 1),
            "temp_rate_c_per_min": round(self.temp_rate_c_per_min, 3),
            "volt_drop_mv": round(self.volt_drop_mv, 2),
            "divergence": self.divergence,
            "sufficient": self.sufficient,
        }


def _slope_per_s(points: list[tuple[float, float]]) -> float:
    """对 (ts, value) 序列做最小二乘拟合，返回每秒斜率。"""

    count = len(points)
    if count < 2:
        return 0.0
    t0 = points[0][0]
    xs = [ts - t0 for ts, _ in points]
    ys = [value for _, value in points]
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0.0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def extract_features(trend: TrendBuffer, rack_id: str, limits: PrecursorLimits, now: float) -> WindowFeatures:
    """从趋势缓冲提取一簇当前滑窗的前兆特征。"""

    since = float(now) - limits.window_s
    temps = [(sample.ts, sample.value) for sample in trend.series(f"{rack_id}.temperature", limit=512) if sample.ts >= since]
    volts = [(sample.ts, sample.value) for sample in trend.series(f"{rack_id}.voltage", limit=512) if sample.ts >= since]
    samples = min(len(temps), len(volts))
    if samples < limits.min_samples:
        return WindowFeatures(samples, 0.0, 0.0, 0.0, False, False)
    temps = temps[-samples:]
    volts = volts[-samples:]
    span_s = max(0.0, temps[-1][0] - temps[0][0])
    temp_slope = _slope_per_s(temps)
    volt_slope = _slope_per_s(volts)
    return WindowFeatures(
        samples=samples,
        span_s=span_s,
        temp_rate_c_per_min=temp_slope * 60.0,
        volt_drop_mv=-volt_slope * span_s * 1000.0,
        divergence=temp_slope > 0.0 and volt_slope < 0.0,
        sufficient=True,
    )
