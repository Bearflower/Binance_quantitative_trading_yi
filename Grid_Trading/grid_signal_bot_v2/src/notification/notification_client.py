"""
推送服务客户端
对接通用推送服务 REST API（飞书）
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)


class NotificationClient:
    """推送服务客户端"""
    
    def __init__(
        self,
        base_url: str,
        project: str = "grid",
        timeout: int = 10
    ):
        """
        初始化推送服务客户端
        
        Args:
            base_url: 推送服务基础 URL
            project: 项目标识
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.project = project
        self.timeout = timeout
        self.session = requests.Session()
        
        logger.info(f"推送服务客户端初始化完成：{self.base_url}, 项目={self.project}")
    
    def send_message(
        self,
        message: str,
        msg_type: str = "text",
        level: str = "info"
    ) -> bool:
        """
        发送消息
        
        Args:
            message: 消息内容
            msg_type: 消息类型（text, markdown, card）
            level: 消息级别（info, warning, error）
            
        Returns:
            是否发送成功
        """
        try:
            url = f"{self.base_url}/api/v1/send"
            data = {
                "project": self.project,
                "message": message,
                "type": msg_type,
                "level": level
            }
            
            response = self.session.post(
                url,
                json=data,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 0:
                logger.info(f"消息发送成功：{message[:50]}...")
                return True
            else:
                logger.error(f"消息发送失败：{result.get('message')}")
                return False
                
        except RequestException as e:
            logger.error(f"请求推送服务失败：{e}")
            return False
    
    def send_grid_signal(
        self,
        market_state: str,
        current_price: float,
        atr: float,
        adx: float,
        grid_params: Dict[str, Any],
        position_validation: Dict[str, Any]
    ) -> bool:
        """
        发送网格信号推送
        
        Args:
            market_state: 市场状态
            current_price: 当前价格
            atr: ATR 值
            adx: ADX 值
            grid_params: 网格参数
            position_validation: 仓位验证结果
            
        Returns:
            是否发送成功
        """
        # 构建推送消息
        message = self._build_grid_signal_message(
            market_state,
            current_price,
            atr,
            adx,
            grid_params,
            position_validation
        )
        
        # 发送消息
        return self.send_message(
            message=message,
            msg_type="markdown",
            level="info" if market_state != "strong_trend" else "warning"
        )
    
    def _build_grid_signal_message(
        self,
        market_state: str,
        current_price: float,
        atr: float,
        adx: float,
        grid_params: Dict[str, Any],
        position_validation: Dict[str, Any]
    ) -> str:
        """
        构建网格信号推送消息
        
        Args:
            market_state: 市场状态
            current_price: 当前价格
            atr: ATR 值
            adx: ADX 值
            grid_params: 网格参数
            position_validation: 仓位验证结果
            
        Returns:
            格式化的推送消息
        """
        # 状态映射
        state_map = {
            "ranging": "震荡市场",
            "uptrend": "上升趋势",
            "downtrend": "下降趋势",
            "strong_trend": "强趋势暂停"
        }
        
        state_name = state_map.get(market_state, market_state)
        
        # 构建消息
        message = f"""【网格信号灯】{state_name}

📊 当前市场数据
- 价格: {current_price:,.2f} USDT
- ATR(14): {atr:,.2f}
- ADX(14): {adx:.2f}
- 每格利润率: {grid_params.get('profit_rate', 0)*100:.2f}%

📐 建议网格参数
- 网格模式: {grid_params.get('grid_type', 'arithmetic')}
- 价格区间: {grid_params['lower_price']:,.2f} - {grid_params['upper_price']:,.2f} USDT
- 网格数量: {grid_params['grid_count']} 格
- 网格间距: {grid_params.get('grid_spacing', 'N/A')}

🎯 止盈止损
- 终止最低价: {grid_params['terminate_lower_price']:,.2f} USDT
- 终止最高价: {grid_params['terminate_upper_price']:,.2f} USDT"""

        # 添加上移/下移功能
        if market_state == "uptrend" and grid_params.get('stop_upper_price'):
            message += f"\n\n📈 上移功能（启用）\n- 停止上移价格: {grid_params['stop_upper_price']:,.2f} USDT"
        
        if market_state == "downtrend" and grid_params.get('stop_lower_price'):
            message += f"\n\n📉 下移功能（启用）\n- 停止下移价格: {grid_params['stop_lower_price']:,.2f} USDT"
        
        # 添加资金可行性提醒
        message += f"\n\n💰 资金可行性提醒"
        if position_validation.get('is_valid'):
            message += f"\n✅ 每格合约数量：{position_validation['qty_per_grid']:.2f} 张（≥1 张）"
        else:
            message += f"\n⚠️ 当前价格较高，默认配置无法满足每格最小 1 张"
            message += f"\n请根据以下公式自行调整："
            message += f"\n- 最小总保证金 ≈ 网格数量 × 当前价格 / 杠杆"
            if position_validation.get('suggested_margin'):
                message += f"\n- 建议保证金：{position_validation['suggested_margin']:,.0f} USDT"
        
        # 添加操作指令
        if market_state == "strong_trend":
            message += f"\n\n💡 操作指令：\n1. 登录币安APP → 永续合约 → 策略交易 → 运行中\n2. 终止当前网格（如有）\n3. 等待市场趋势减弱后再创建新网格"
        else:
            message += f"\n\n💡 操作指令：\n1. 登录币安APP → 永续合约 → 策略交易 → 运行中，终止当前网格（如有）\n2. 点击\"创建网格\" → 合约网格\n3. 填入以上价格区间、网格数量、网格模式\n4. 设置杠杆（建议10x）、总投入金额（根据您的资金能力）\n5. 高级设置中，启用\"上移/下移\"并填入停止价格（如适用），设置止盈止损价格\n6. 确认创建前请检查每格下单数量≥1张"
        
        # 添加时间戳
        message += f"\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        return message
    
    def send_error_alert(
        self,
        error_type: str,
        error_message: str,
        details: Optional[str] = None
    ) -> bool:
        """
        发送错误报警
        
        Args:
            error_type: 错误类型
            error_message: 错误消息
            details: 详细信息
            
        Returns:
            是否发送成功
        """
        message = f"""❌ 系统错误

**错误类型**: {error_type}

**错误消息**: {error_message}"""
        
        if details:
            message += f"\n\n**详细信息**: {details}"
        
        message += f"\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(
            message=message,
            msg_type="markdown",
            level="error"
        )
    
    def health_check(self) -> bool:
        """
        健康检查
        
        Returns:
            服务是否健康
        """
        try:
            url = f"{self.base_url}/health"
            response = self.session.get(url, timeout=5)
            response.raise_for_status()
            
            result = response.json()
            is_healthy = result.get("code") == 0
            
            if is_healthy:
                logger.info("推送服务健康检查通过")
            else:
                logger.warning("推送服务健康检查失败")
            
            return is_healthy
            
        except Exception as e:
            logger.error(f"推送服务健康检查失败：{e}")
            return False
    
    def close(self):
        """关闭会话"""
        self.session.close()
        logger.info("推送服务客户端会话已关闭")


# 使用示例
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s'
    )
    
    # 初始化客户端
    notification_url = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8766")
    client = NotificationClient(notification_url, project="grid")
    
    # 健康检查
    if client.health_check():
        print("✅ 推送服务健康")
    
    # 发送测试消息
    test_message = """📊 测试消息

这是一条测试消息，用于验证推送服务是否正常工作。

⏰ 2026-04-23 22:00:00"""
    
    success = client.send_message(test_message, msg_type="markdown")
    if success:
        print("✅ 测试消息发送成功")
    
    # 关闭客户端
    client.close()
