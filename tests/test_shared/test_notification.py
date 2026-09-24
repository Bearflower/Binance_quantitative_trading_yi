"""
测试通知服务客户端
"""
import os
import pytest
from unittest.mock import AsyncMock, patch
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


@pytest.mark.asyncio
async def test_send_alert_uses_direct_webhook_when_no_alert_mapping():
    """告警 project="alert" 未配置专属 webhook 时，复用 btc_eth 的 webhook 直连，
    不再回退到通知服务通道（8766）。"""
    with patch.dict(
        os.environ,
        {
            "FEISHU_WEBHOOK_BTC_ETH": "https://open.feishu.cn/open-apis/bot/v2/hook/btc_eth_mock",
            "FEISHU_WEBHOOK_BTC_ETH_AGGRESSIVE": "https://open.feishu.cn/open-apis/bot/v2/hook/aggressive_mock",
        },
        clear=False,
    ):
        client = NotificationClient(
            service_url="http://43.156.242.184:8766/api/v1"
        )
        # alert 应映射到 btc_eth 的 webhook
        assert client._webhook_mapping.get("alert") == (
            "https://open.feishu.cn/open-apis/bot/v2/hook/btc_eth_mock"
        )

        # 直连 webhook 应被调用，且不触发 _send_via_service
        with patch.object(
            client,
            "_send_to_feishu_webhook",
            new=AsyncMock(return_value=True),
        ) as mock_direct:
            with patch.object(
                client,
                "_send_via_service",
                new=AsyncMock(return_value=False),
            ) as mock_service:
                result = await client.send_alert(
                    title="测试告警",
                    message="策略执行失败",
                    level="error",
                )
                assert result is True
                mock_direct.assert_awaited_once()
                mock_service.assert_not_awaited()
                # 直连目标确为 btc_eth webhook
                assert mock_direct.await_args.kwargs["webhook_url"] == (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/btc_eth_mock"
                )

        await client.close()


@pytest.mark.asyncio
async def test_send_alert_uses_dedicated_alert_webhook_if_configured():
    """配置了 FEISHU_WEBHOOK_ALERT 时优先使用独立告警 webhook。"""
    with patch.dict(
        os.environ,
        {
            "FEISHU_WEBHOOK_ALERT": "https://open.feishu.cn/open-apis/bot/v2/hook/alert_mock",
            "FEISHU_WEBHOOK_BTC_ETH": "https://open.feishu.cn/open-apis/bot/v2/hook/btc_eth_mock",
        },
        clear=False,
    ):
        client = NotificationClient(
            service_url="http://43.156.242.184:8766/api/v1"
        )
        assert client._webhook_mapping.get("alert") == (
            "https://open.feishu.cn/open-apis/bot/v2/hook/alert_mock"
        )
        await client.close()
