"""
本地影子回放器（XAUUSDT MTCPS 影子仿真第三阶段）。

全部离线运行，不连生产 PostgreSQL；信号已由采集器冻结，
不重算 score/direction；全程 Decimal 运算保证确定性。
"""

ENGINE_VERSION = "replay-v1.0.0"
