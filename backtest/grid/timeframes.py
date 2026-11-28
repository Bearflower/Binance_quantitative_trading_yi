"""将历史 K 线开盘时间转换为可用于决策的收盘时间。"""
import pandas as pd


def bar_close_time(timestamp, interval: str) -> pd.Timestamp:
    """CSV 的 timestamp 为开盘时间；月线按日历月计算。"""
    opened_at = pd.to_datetime(timestamp, utc=True)
    count, unit = int(interval[:-1]), interval[-1]
    if count <= 0:
        raise ValueError("K 线周期必须为正数")
    if unit == "M":
        return opened_at + pd.DateOffset(months=count)
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if unit not in units:
        raise ValueError(f"不支持的 K 线周期：{interval}")
    return opened_at + pd.Timedelta(**{units[unit]: count})
