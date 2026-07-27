"""
测试币安API封装
"""
import pytest
from decimal import Decimal
from shared.binance_api import BinanceClient, BinanceAPIError


class TestBinanceClientInitialization:
    """测试币安客户端初始化"""
    
    @pytest.mark.asyncio
    async def test_valid_initialization(self):
        """测试有效初始化"""
        client = BinanceClient(
            api_key="test_api_key_12345",
            api_secret="test_api_secret_67890",
            testnet=True
        )
        
        # 验证脱敏后的属性
        # test_api_key_12345 (18字符) -> test + 10个* + 2345
        assert client.api_key == "test**********2345"
        # test_api_secret_67890 (21字符) -> test + 13个* + 7890
        assert client.api_secret == "test*************7890"
        assert client.testnet is True
        assert client.base_url == "https://testnet.binancefuture.com"
        
        await client.close()
    
    @pytest.mark.asyncio
    async def test_invalid_api_key_empty(self):
        """测试空API密钥"""
        with pytest.raises(ValueError, match="API密钥不能为空"):
            BinanceClient(
                api_key="",
                api_secret="test_secret"
            )
    
    @pytest.mark.asyncio
    async def test_invalid_api_key_whitespace(self):
        """测试仅包含空白的API密钥"""
        with pytest.raises(ValueError, match="API密钥不能为空"):
            BinanceClient(
                api_key="   ",
                api_secret="test_secret"
            )
    
    @pytest.mark.asyncio
    async def test_invalid_api_secret_empty(self):
        """测试空API密钥"""
        with pytest.raises(ValueError, match="API密钥不能为空"):
            BinanceClient(
                api_key="test_key",
                api_secret=""
            )
    
    @pytest.mark.asyncio
    async def test_production_url(self):
        """测试生产环境URL"""
        client = BinanceClient(
            api_key="test_api_key",
            api_secret="test_api_secret",
            testnet=False
        )
        
        assert client.base_url == "https://fapi.binance.com"
        
        await client.close()
    
    @pytest.mark.asyncio
    async def test_custom_base_url(self):
        """测试自定义URL"""
        client = BinanceClient(
            api_key="test_api_key",
            api_secret="test_api_secret",
            base_url="https://custom.binance.com"
        )
        
        assert client.base_url == "https://custom.binance.com"
        
        await client.close()


class TestPlaceOrderValidation:
    """测试下单参数验证"""
    
    @pytest.fixture
    async def client(self):
        """创建测试客户端"""
        client = BinanceClient(
            api_key="test_api_key_12345",
            api_secret="test_api_secret_67890",
            testnet=True
        )
        yield client
        await client.close()
    
    @pytest.mark.asyncio
    async def test_invalid_symbol_empty(self, client):
        """测试空交易对"""
        with pytest.raises(ValueError, match="交易对不能为空"):
            await client.place_order(
                symbol="",
                side="BUY",
                quantity=Decimal("0.001")
            )
    
    @pytest.mark.asyncio
    async def test_invalid_side(self, client):
        """测试无效订单方向"""
        with pytest.raises(ValueError, match="无效的订单方向"):
            await client.place_order(
                symbol="BTCUSDT",
                side="INVALID",
                quantity=Decimal("0.001")
            )
    
    @pytest.mark.asyncio
    async def test_invalid_quantity_zero(self, client):
        """测试数量为0"""
        with pytest.raises(ValueError, match="数量必须大于0"):
            await client.place_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0")
            )
    
    @pytest.mark.asyncio
    async def test_invalid_quantity_negative(self, client):
        """测试数量为负数"""
        with pytest.raises(ValueError, match="数量必须大于0"):
            await client.place_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("-0.001")
            )
    
    @pytest.mark.asyncio
    async def test_invalid_order_type(self, client):
        """测试无效订单类型"""
        with pytest.raises(ValueError, match="无效的订单类型"):
            await client.place_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                order_type="INVALID"
            )
    
    @pytest.mark.asyncio
    async def test_limit_order_without_price(self, client):
        """测试限价单缺少价格"""
        with pytest.raises(ValueError, match="限价单必须提供价格"):
            await client.place_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                order_type="LIMIT"
            )
    
    @pytest.mark.asyncio
    async def test_invalid_price_zero(self, client):
        """测试价格为0"""
        with pytest.raises(ValueError, match="价格必须大于0"):
            await client.place_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                price=Decimal("0"),
                order_type="LIMIT"
            )


class TestCancelOrderValidation:
    """测试撤销订单参数验证"""
    
    @pytest.fixture
    async def client(self):
        """创建测试客户端"""
        client = BinanceClient(
            api_key="test_api_key_12345",
            api_secret="test_api_secret_67890",
            testnet=True
        )
        yield client
        await client.close()
    
    @pytest.mark.asyncio
    async def test_invalid_symbol_empty(self, client):
        """测试空交易对"""
        with pytest.raises(ValueError, match="交易对不能为空"):
            await client.cancel_order(symbol="")
    
    @pytest.mark.asyncio
    async def test_missing_order_id(self, client):
        """测试缺少订单ID"""
        with pytest.raises(ValueError, match="必须提供 orderId 或 clientOrderId"):
            await client.cancel_order(symbol="BTCUSDT")
    
    @pytest.mark.asyncio
    async def test_invalid_order_id_empty(self, client):
        """测试空订单ID"""
        # 空字符串会被认为是 False，所以会触发 "必须提供 orderId 或 clientOrderId" 的错误
        with pytest.raises(ValueError, match="必须提供 orderId 或 clientOrderId"):
            await client.cancel_order(
                symbol="BTCUSDT",
                order_id=""
            )


class TestAPIKeyMasking:
    """测试API密钥脱敏"""
    
    def test_short_key_masking(self):
        """测试短密钥脱敏"""
        client = BinanceClient.__new__(BinanceClient)
        client._api_key = "short"
        
        # 短密钥应该全部用*代替
        assert client.api_key == "*****"
    
    def test_long_key_masking(self):
        """测试长密钥脱敏"""
        client = BinanceClient.__new__(BinanceClient)
        client._api_key = "abcdefghijklmnopqrstuvwxyz"
        
        # 长密钥应该显示前4位和后4位
        # abcdefghijklmnopqrstuvwxyz (26字符) -> abcd + 18个* + wxyz
        assert client.api_key == "abcd******************wxyz"
    
    def test_medium_key_masking(self):
        """测试中等长度密钥脱敏"""
        client = BinanceClient.__new__(BinanceClient)
        client._api_key = "12345678"
        
        # 刚好8位，应该全部用*代替
        assert client.api_key == "********"
