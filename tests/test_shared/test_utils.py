"""
测试工具函数
"""
import pytest
import asyncio
from shared.utils import retry_on_failure, setup_logging


class TestRetryOnFailure:
    """测试重试装饰器"""
    
    def test_valid_parameters(self):
        """测试有效参数"""
        # 不应该抛出异常
        @retry_on_failure(max_retries=3, delay=1.0, backoff=2.0)
        async def test_func():
            return "success"
        
        assert asyncio.run(test_func()) == "success"
    
    def test_invalid_max_retries_type(self):
        """测试无效的最大重试次数类型"""
        with pytest.raises(ValueError, match="最大重试次数必须是整数"):
            @retry_on_failure(max_retries=3.5)
            async def test_func():
                pass
    
    def test_invalid_max_retries_negative(self):
        """测试负数的最大重试次数"""
        with pytest.raises(ValueError, match="最大重试次数不能为负数"):
            @retry_on_failure(max_retries=-1)
            async def test_func():
                pass
    
    def test_invalid_delay_type(self):
        """测试无效的延迟时间类型"""
        with pytest.raises(ValueError, match="延迟时间必须是数字"):
            @retry_on_failure(delay="invalid")
            async def test_func():
                pass
    
    def test_invalid_delay_negative(self):
        """测试负数的延迟时间"""
        with pytest.raises(ValueError, match="延迟时间不能为负数"):
            @retry_on_failure(delay=-1.0)
            async def test_func():
                pass
    
    def test_invalid_backoff_type(self):
        """测试无效的退避系数类型"""
        with pytest.raises(ValueError, match="退避系数必须是数字"):
            @retry_on_failure(backoff="invalid")
            async def test_func():
                pass
    
    def test_invalid_backoff_less_than_one(self):
        """测试退避系数小于1"""
        with pytest.raises(ValueError, match="退避系数必须大于等于1"):
            @retry_on_failure(backoff=0.5)
            async def test_func():
                pass
    
    def test_invalid_exceptions_type(self):
        """测试无效的异常类型"""
        with pytest.raises(ValueError, match="异常类型必须是元组"):
            @retry_on_failure(exceptions=Exception)
            async def test_func():
                pass
    
    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
        """测试失败后重试成功"""
        call_count = 0
        
        @retry_on_failure(max_retries=3, delay=0.1, backoff=1.0)
        async def test_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary error")
            return "success"
        
        result = await test_func()
        assert result == "success"
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_retry_max_retries_exceeded(self):
        """测试超过最大重试次数"""
        call_count = 0
        
        @retry_on_failure(max_retries=2, delay=0.1, backoff=1.0)
        async def test_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("Permanent error")
        
        with pytest.raises(ValueError, match="Permanent error"):
            await test_func()
        
        assert call_count == 3  # 初始调用 + 2次重试


class TestSetupLogging:
    """测试日志配置"""
    
    def test_valid_parameters_json(self):
        """测试有效参数 - JSON格式"""
        # 不应该抛出异常
        setup_logging(level="INFO", format="json")
    
    def test_valid_parameters_text(self):
        """测试有效参数 - 文本格式"""
        # 不应该抛出异常
        setup_logging(level="DEBUG", format="text")
    
    def test_valid_level_case_insensitive(self):
        """测试日志级别大小写不敏感"""
        # 不应该抛出异常
        setup_logging(level="info", format="json")
        setup_logging(level="Warning", format="json")
        setup_logging(level="ERROR", format="json")
    
    def test_invalid_level_type(self):
        """测试无效的日志级别类型"""
        with pytest.raises(ValueError, match="日志级别必须是字符串"):
            setup_logging(level=123, format="json")
    
    def test_invalid_level_value(self):
        """测试无效的日志级别值"""
        with pytest.raises(ValueError, match="无效的日志级别"):
            setup_logging(level="INVALID", format="json")
    
    def test_invalid_format_type(self):
        """测试无效的日志格式类型"""
        with pytest.raises(ValueError, match="日志格式必须是字符串"):
            setup_logging(level="INFO", format=123)
    
    def test_invalid_format_value(self):
        """测试无效的日志格式值"""
        with pytest.raises(ValueError, match="无效的日志格式"):
            setup_logging(level="INFO", format="invalid")
