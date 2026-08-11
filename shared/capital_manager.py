"""
资金分配管理器
提供策略可用的分配资金上限，用于限制各策略的仓位大小。

数据来源：
- 各策略 config.yaml 中的 capital_limits 字段（由月度资金分配系统写入）
- 格式：
  ```yaml
  capital_limits:
    monthly_limit: 360.0       # 当月分配资金上限（USDT）
    allocated_ratio: 0.36      # 分配比例
    allocation_month: "2026-07" # 分配月份
    updated_at: "2026-07-31T23:55:00+08:00"  # 更新时间
  ```

用法：
    capital_mgr = CapitalManager("strategies/btc_eth/config.yaml")
    allocated = capital_mgr.get_allocated_capital()
    if allocated is not None:
        balance = allocated  # 使用分配金额
    else:
        balance = api_balance  # 回退到全账户余额
"""

import os
from typing import Optional

import structlog
import yaml

logger = structlog.get_logger()


class CapitalManager:
    """
    资金分配管理器

    从策略的 config.yaml 中读取 capital_limits 配置，
    提供策略可用的分配资金上限。

    每次调用都重新读取文件，确保获取最新分配金额。
    """

    def __init__(self, config_path: str):
        """
        初始化资金分配管理器

        Args:
            config_path: 策略配置文件路径（相对或绝对路径）
        """
        self.config_path = config_path

    def get_allocated_capital(self) -> Optional[float]:
        """
        读取分配资金上限

        Returns:
            float: 分配资金 USDT 金额
            None: 未配置 capital_limits，调用方应使用全账户余额
        """
        try:
            config = self._read_config()
            capital_limits = config.get("capital_limits")
            if not capital_limits or not isinstance(capital_limits, dict):
                return None

            monthly_limit = capital_limits.get("monthly_limit")
            if monthly_limit is None:
                return None

            return float(monthly_limit)
        except Exception as e:
            logger.warning(
                "读取分配资金失败，将使用全账户余额",
                config_path=self.config_path,
                error=str(e),
            )
            return None

    def get_allocated_ratio(self) -> Optional[float]:
        """
        读取分配比例

        Returns:
            float: 分配比例（如 0.36）
            None: 未配置 capital_limits
        """
        try:
            config = self._read_config()
            capital_limits = config.get("capital_limits")
            if not capital_limits or not isinstance(capital_limits, dict):
                return None

            ratio = capital_limits.get("allocated_ratio")
            if ratio is None:
                return None

            return float(ratio)
        except Exception as e:
            logger.warning(
                "读取分配比例失败",
                config_path=self.config_path,
                error=str(e),
            )
            return None

    def can_open_position(self, current_positions_value: float, new_position_value: float) -> bool:
        """
        检查是否可以开新仓（总仓位不超过分配金额）

        策略内部按自身逻辑计算每笔仓位大小，此方法仅检查总仓位上限。
        如果 current_positions_value + new_position_value > 分配金额，则拒绝开仓。

        Args:
            current_positions_value: 当前所有持仓总价值（USDT）
            new_position_value: 新仓价值（USDT）

        Returns:
            bool: True 表示可以开仓，False 表示总仓位超限
        """
        allocated = self.get_allocated_capital()
        if allocated is None:
            # 未配置分配，不限制
            return True

        total_after_opening = current_positions_value + new_position_value
        if total_after_opening > allocated:
            logger.warning(
                "总仓位超限，拒绝开仓",
                current=current_positions_value,
                new=new_position_value,
                total=total_after_opening,
                limit=allocated,
            )
            return False

        return True

    def is_allocated(self) -> bool:
        """
        capital_limits 是否已配置

        Returns:
            bool: True 表示已配置，False 表示未配置
        """
        return self.get_allocated_capital() is not None

    def _read_config(self) -> dict:
        """
        读取配置文件

        Returns:
            dict: 配置字典，读取失败返回空字典
        """
        # 尝试绝对路径
        if os.path.isabs(self.config_path):
            config_file = self.config_path
        else:
            # 相对路径：从项目根目录解析
            # 项目根目录为当前文件所在目录的上一级
            config_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                self.config_path,
            )

        if not os.path.exists(config_file):
            logger.warning("配置文件不存在", config_path=config_file)
            return {}

        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}