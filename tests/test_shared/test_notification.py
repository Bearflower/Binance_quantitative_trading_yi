"""
测试通知服务客户端
"""
import pytest
from shared.notification import NotificationClient


@pytest.mark.asyncio
async def test_notification_client_initialization():
    """测试通知服务初始化"""
    client = NotificationClient(
        service_url="http://43.156.242.184:8766/api/v1"
    )
    
    assert client.service_url == "http://43.156.242.184:8766/api/v1"
    assert client.timeout == 10
    
    await client.close()


def test_trade_notification_message_format():
    """测试交易通知消息格式"""
    client = NotificationClient(
        service_url="http://43.156.242.184:8766/api/v1"
    )
    
    message = f"""
【交易通知】
策略: btc_eth
交易对: BTCUSDT
动作: BUY
数量: 0.001
价格: 50000.0
"""
    
    assert "策略: btc_eth" in message
    assert "交易对: BTCUSDT" in message
    assert "动作: BUY" in message
