"""aggtrade_collector 测试通用配置：把仓库根加入 sys.path（命名空间包 services.*）。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
