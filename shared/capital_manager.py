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
        return self._get_nested_float(
            "capital_limits", "monthly_limit",
            log_msg="读取分配资金失败，将使用全账户余额",
        )

    def get_account_ratio_cap(self) -> Optional[float]:
        """
        动态读取总持仓保证金占账户权益的比例阈值

        读取根级 position_sizing.total.account_ratio_cap（每月由 AI 资金分配自动更新），
        每次调用重新读取配置文件，确保获取最新值（禁止调用方硬编码）。

        Returns:
            float: 比例阈值（如 0.3 表示总持仓保证金 ≤ 账户权益 30%）
            None: 未配置 position_sizing.total.account_ratio_cap，调用方不做限制
        """
        return self._get_nested_float(
            "position_sizing", "total", "account_ratio_cap",
            log_msg="读取总持仓保证金比例阈值失败，调用方不做限制",
        )

    def get_total_margin_limit(self) -> Optional[float]:
        """
        动态读取总持仓保证金上限

        优先取「月度资金分配」金额 capital_limits.monthly_limit（每月由 AI 更新，动态变化）；
        未配置时回退到静态兜底 trading.total_position_margin_limit。
        每次调用都重新读取配置文件，确保获取最新值（禁止调用方硬编码）。

        Returns:
            float: 总持仓保证金上限（USDT）
            None: 未配置任何来源，调用方不做限制
        """
        # 1) 优先：月度分配金额（动态，随 AI 月度资金分配更新）
        monthly = self._get_nested_float(
            "capital_limits", "monthly_limit",
            log_msg="读取总持仓保证金上限失败",
        )
        if monthly is not None:
            return monthly

        # 2) 回退：静态兜底阈值（未开展月度分配时使用）
        return self._get_nested_float(
            "trading", "total_position_margin_limit",
            log_msg="读取总持仓保证金上限失败",
        )

    def get_allocated_ratio(self) -> Optional[float]:
        """
        读取分配比例

        Returns:
            float: 分配比例（如 0.36）
            None: 未配置 capital_limits
        """
        return self._get_nested_float(
            "capital_limits", "allocated_ratio",
            log_msg="读取分配比例失败",
        )

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

    def _get_nested_float(self, *keys: str, log_msg: str) -> Optional[float]:
        """
        按嵌套路径读取配置中的浮点值（任一节点缺失返回 None）

        供各读取方法复用，避免重复 try/except 与节点遍历模板。

        Args:
            keys: 配置嵌套键路径，如 ("strategy", "risk", "total_margin_ratio_limit")
            log_msg: 读取失败时的日志消息（中文）

        Returns:
            float: 读取到的数值；未配置或读取异常返回 None
        """
        try:
            config = self._read_config()
            node = config
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return None
                node = node[key]
            if node is None:
                return None
            return float(node)
        except Exception as e:
            logger.warning(log_msg, config_path=self.config_path, error=str(e))
            return None

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