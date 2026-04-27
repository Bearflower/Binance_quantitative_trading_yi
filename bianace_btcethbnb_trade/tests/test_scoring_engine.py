#!/usr/bin/env python3
"""
测试评分引擎
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import MarketDataFetcher
from core.scoring import ScoringEngineV612
import logging

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def test_scoring():
    """测试评分引擎"""
    print("=" * 80)
    print("测试评分引擎")
    print("=" * 80)
    
    fetcher = MarketDataFetcher()
    scorer = ScoringEngineV612()
    
    try:
        print("\n开始获取行情数据...")
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        data = fetcher.fetch_market_data(symbols)
        
        if not data:
            print("\n❌ 获取数据失败：返回为空")
            return False
        
        print(f"\n✅ 成功获取 {len(data)} 个交易对的数据")
        
        for symbol, symbol_data in data.items():
            print(f"\n{'='*60}")
            print(f"测试 {symbol} 评分")
            print(f"{'='*60}")
            
            result = scorer.score(symbol, symbol_data)
            
            print(f"评分：{result['score']:.1f}")
            print(f"等级：{result.get('grade', '无')}")
            print(f"方向：{result.get('direction', '无')}")
            print(f"仓位比例：{result.get('position_ratio', 0.0):.1%}")
            
            if result.get('veto_reason'):
                print(f"否决原因：{result['veto_reason']}")
            
            # 检查市场状态
            market_state = scorer._check_market_state_v6(symbol_data)
            print(f"市场状态：{market_state}")
            
            # 检查数据完整性
            is_valid, confidence = scorer._check_data_integrity(symbol_data)
            print(f"数据完整性：{'有效' if is_valid else '无效'}, 置信度：{confidence:.2f}")
        
        print("\n" + "=" * 80)
        print("✅ 测试完成！")
        print("=" * 80)
        return True
        
    except Exception as e:
        import traceback
        print(f"\n❌ 测试失败：{e}")
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_scoring()
    sys.exit(0 if success else 1)
