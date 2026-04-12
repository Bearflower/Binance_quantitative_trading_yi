"""
pytest 配置文件
"""

import pytest
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def sample_klines():
    """示例 K 线数据"""
    import pandas as pd
    from datetime import datetime, timedelta
    
    n = 100
    dates = pd.date_range(datetime.now() - timedelta(hours=n), periods=n, freq='1h')
    
    return pd.DataFrame({
        'open': [100 + i * 0.1 for i in range(n)],
        'high': [102 + i * 0.1 for i in range(n)],
        'low': [98 + i * 0.1 for i in range(n)],
        'close': [100 + i * 0.1 for i in range(n)],
        'volume': [1000] * n
    }, index=dates)


@pytest.fixture
def config_loader():
    """配置加载器"""
    from src.utils.config_loader import ConfigLoader
    loader = ConfigLoader("config/config.yaml.template")
    loader.load()
    return loader
