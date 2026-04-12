#!/usr/bin/env python3
"""
v6.13 动态仓位调整回测模拟器

使用 v6.12 的历史交易数据，模拟 v6.13 的动态仓位调整逻辑

回测逻辑:
1. 加载 v6.12 回测报告中的交易记录
2. 假设初始资金 500U
3. 对每笔交易，根据当时的可用余额动态调整仓位
4. 计算调整后的盈亏
5. 更新可用余额
6. 对比 v6.12（固定仓位）vs v6.13（动态仓位）的表现

使用方式:
    python3 scripts/backtest_v613.py
"""

import json
import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, Any, List

# 导入 v6.13 动态仓位调整器
import sys
sys.path.append('/Users/yl/vscode/bianace_btcethbnb_trade')
from services.position_adjuster import PositionAdjuster

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v613_backtest')


class V613Backtester:
    """v6.13 回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        """
        初始化回测器
        
        Args:
            initial_capital: 初始资金，默认 500U
        """
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        logger.info("=" * 80)
        logger.info("v6.13 动态仓位调整回测器初始化完成")
        logger.info("=" * 80)
        logger.info(f"初始资金：{initial_capital}U")
        logger.info(f"安全垫比例：{self.position_adjuster.safety_ratio}")
        logger.info(f"最小保证金：{self.position_adjuster.min_position_margin}U")
        logger.info("=" * 80)
    
    def load_backtest_data(self, filepath: str) -> Dict[str, Any]:
        """
        加载 v6.12 回测数据
        
        Args:
            filepath: 回测报告 JSON 文件路径
        
        Returns:
            回测报告数据
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        logger.info(f"加载回测数据：{filepath}")
        logger.info(f"总交易数：{data['summary']['total_trades']}")
        logger.info(f"胜率：{data['summary']['win_rate']}")
        logger.info(f"总盈亏：{data['summary']['total_pnl']}U")
        
        return data
    
    def simulate_v612(self, trades: List[Dict[str, Any]], 
                     fixed_margin: Decimal = Decimal('14')) -> Dict[str, Any]:
        """
        模拟 v6.12 固定仓位策略
        
        Args:
            trades: 交易记录列表
            fixed_margin: 固定保证金（每笔交易）
        
        Returns:
            回测结果
        """
        logger.info("\n" + "=" * 80)
        logger.info("模拟 v6.12 固定仓位策略")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        
        for i, trade in enumerate(trades):
            # v6.12: 固定仓位，不调整
            required_margin = fixed_margin
            
            # 检查资金是否充足
            if current_capital < required_margin:
                logger.warning(f"交易 {i+1}: 资金不足 ({current_capital}U < {required_margin}U)，跳过")
                continue
            
            # 计算盈亏（使用原始盈亏比例）
            original_pnl = Decimal(trade['pnl'])
            original_margin = Decimal('14')  # 假设 v6.12 使用 14U 保证金
            pnl_rate = original_pnl / original_margin
            
            # 应用盈亏比例到固定保证金
            actual_pnl = required_margin * pnl_rate
            fee = abs(actual_pnl) * Decimal('0.0004') * 2  # 开平仓手续费（万分之四）
            
            # 更新资金
            current_capital += actual_pnl - fee
            total_pnl += actual_pnl
            total_fees += fee
            
            # 统计
            if actual_pnl > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            
            # 更新峰值和回撤
            if current_capital > peak_capital:
                peak_capital = current_capital
            drawdown = (peak_capital - current_capital) / peak_capital
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            logger.info(f"交易 {i+1}: {trade['symbol']} {trade['direction']} "
                       f"盈亏：{actual_pnl:.2f}U, 余额：{current_capital:.2f}U")
            
            trade_details.append({
                'symbol': trade['symbol'],
                'direction': trade['direction'],
                'margin': float(required_margin),
                'pnl': float(actual_pnl),
                'fee': float(fee),
                'balance': float(current_capital),
                'adjusted': False
            })
        
        # 生成回测报告
        return {
            'strategy': 'v6.12 固定仓位',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': winning_trades + losing_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'trade_details': trade_details
        }
    
    def simulate_v613(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        模拟 v6.13 动态仓位调整策略
        
        Args:
            trades: 交易记录列表
        
        Returns:
            回测结果
        """
        logger.info("\n" + "=" * 80)
        logger.info("模拟 v6.13 动态仓位调整策略")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        skipped_trades = 0
        adjusted_trades = 0
        
        for i, trade in enumerate(trades):
            # v6.13: 动态仓位调整
            base_margin = Decimal('14')  # 基础保证金
            
            # 构建仓位参数
            position_params = {
                'symbol': trade['symbol'],
                'margin': base_margin,
                'quantity': Decimal('1'),  # 假设基础数量
                'notional_value': base_margin * Decimal('5'),  # 假设 5 倍杠杆
                'leverage': 5
            }
            
            # 执行动态仓位调整
            adjusted_position = self.position_adjuster.adjust_position(
                position_params, 
                current_capital
            )
            
            if adjusted_position is None:
                # 资金不足且无法调整（低于最小阈值）
                logger.warning(f"交易 {i+1}: 资金严重不足，跳过")
                skipped_trades += 1
                continue
            
            # 获取调整后的保证金
            adj_info = adjusted_position.get('adjustment_info', {})
            required_margin = adjusted_position['margin']
            
            if adj_info.get('adjusted'):
                adjusted_trades += 1
                logger.info(f"交易 {i+1}: 触发动态调仓 {base_margin}U → {required_margin}U "
                           f"({adj_info['adjustment_ratio']:.0%})")
            else:
                logger.info(f"交易 {i+1}: 资金充足，不调整 ({required_margin}U)")
            
            # 计算盈亏（按调整比例缩放）
            original_pnl = Decimal(trade['pnl'])
            original_margin = Decimal('14')
            pnl_rate = original_pnl / original_margin
            
            # 应用盈亏比例到调整后的保证金
            actual_pnl = required_margin * pnl_rate
            fee = abs(actual_pnl) * Decimal('0.0004') * 2  # 开平仓手续费
            
            # 更新资金
            current_capital += actual_pnl - fee
            total_pnl += actual_pnl
            total_fees += fee
            
            # 统计
            if actual_pnl > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            
            # 更新峰值和回撤
            if current_capital > peak_capital:
                peak_capital = current_capital
            drawdown = (peak_capital - current_capital) / peak_capital
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            logger.info(f"  盈亏：{actual_pnl:.2f}U, 余额：{current_capital:.2f}U")
            
            trade_details.append({
                'symbol': trade['symbol'],
                'direction': trade['direction'],
                'margin': float(required_margin),
                'pnl': float(actual_pnl),
                'fee': float(fee),
                'balance': float(current_capital),
                'adjusted': adj_info.get('adjusted', False),
                'adjustment_ratio': adj_info.get('adjustment_ratio', 1.0)
            })
        
        # 生成回测报告
        return {
            'strategy': 'v6.13 动态仓位',
            'initial_capital': float(self.initial_capital),
            'final_capital': float(current_capital),
            'total_trades': winning_trades + losing_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
            'skipped_trades': skipped_trades,
            'adjusted_trades': adjusted_trades,
            'total_pnl': float(total_pnl),
            'total_fees': float(total_fees),
            'total_return': float((current_capital - self.initial_capital) / self.initial_capital),
            'max_drawdown': float(max_drawdown),
            'trade_details': trade_details
        }
    
    def compare_strategies(self, v612_result: Dict[str, Any], 
                          v613_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        对比 v6.12 和 v6.13 的策略表现
        
        Args:
            v612_result: v6.12 回测结果
            v613_result: v6.13 回测结果
        
        Returns:
            对比报告
        """
        logger.info("\n" + "=" * 80)
        logger.info("策略对比报告")
        logger.info("=" * 80)
        
        # 计算改善指标
        pnl_improvement = v613_result['total_pnl'] - v612_result['total_pnl']
        win_rate_improvement = v613_result['win_rate'] - v612_result['win_rate']
        drawdown_improvement = v612_result['max_drawdown'] - v613_result['max_drawdown']
        return_improvement = v613_result['total_return'] - v612_result['total_return']
        
        # 交易次数变化
        trade_count_change = v613_result['total_trades'] - v612_result['total_trades']
        
        comparison = {
            'v612': v612_result,
            'v613': v613_result,
            'improvements': {
                'pnl_improvement': float(pnl_improvement),
                'win_rate_improvement': float(win_rate_improvement),
                'drawdown_improvement': float(drawdown_improvement),
                'return_improvement': float(return_improvement),
                'trade_count_change': trade_count_change
            },
            'summary': self._generate_summary(v612_result, v613_result, pnl_improvement)
        }
        
        # 打印对比报告
        self._print_comparison_report(comparison)
        
        return comparison
    
    def _generate_summary(self, v612: Dict[str, Any], v613: Dict[str, Any], 
                         pnl_improvement: Decimal) -> str:
        """生成总结文字"""
        if pnl_improvement > 0:
            conclusion = "✅ v6.13 表现更优"
        elif pnl_improvement < 0:
            conclusion = "⚠️ v6.12 表现更优"
        else:
            conclusion = "➖ 两者表现相当"
        
        # 分析原因
        if v613['adjusted_trades'] > 0:
            reason = f"v6.13 通过动态调整 {v613['adjusted_trades']} 笔交易，"
            if v613['skipped_trades'] > 0:
                reason += f"跳过 {v613['skipped_trades']} 笔资金不足的交易，"
            reason += "充分利用了可用资金。"
        else:
            reason = "资金充足，v6.13 未触发动态调整。"
        
        return f"{conclusion}\n\n{reason}"
    
    def _print_comparison_report(self, comparison: Dict[str, Any]):
        """打印对比报告"""
        v612 = comparison['v612']
        v613 = comparison['v613']
        improvements = comparison['improvements']
        
        print("\n" + "=" * 80)
        print("📊 v6.12 vs v6.13 策略对比报告")
        print("=" * 80)
        
        print(f"\n{'指标':<20} {'v6.12 固定仓位':<20} {'v6.13 动态仓位':<20} {'改善':<20}")
        print("-" * 80)
        
        # 格式化输出
        metrics = [
            ('总交易数 (笔)', 'total_trades', '{:.0f}', '{:.0f}'),
            ('胜率 (%)', 'win_rate', '{:.1%}', '{:.1%}'),
            ('总盈亏 (U)', 'total_pnl', '{:.2f}', '{:.2f}'),
            ('总收益率 (%)', 'total_return', '{:.1%}', '{:.1%}'),
            ('最大回撤 (%)', 'max_drawdown', '{:.1%}', '{:.1%}'),
            ('总手续费 (U)', 'total_fees', '{:.2f}', '{:.2f}'),
        ]
        
        for name, key, fmt_v612, fmt_v613 in metrics:
            v612_val = v612.get(key, 0)
            v613_val = v613.get(key, 0)
            
            if key == 'total_trades':
                improvement = f"{v613_val - v612_val:+.0f}"
            elif key in ['win_rate', 'total_return', 'max_drawdown']:
                improvement = f"{v613_val - v612_val:+.1%}"
            else:
                improvement = f"{v613_val - v612_val:+.2f}"
            
            print(f"{name:<20} {fmt_v612.format(v612_val):<20} {fmt_v613.format(v613_val):<20} {improvement:<20}")
        
        # v6.13 特有指标
        print(f"{'调整后交易数 (笔)':<20} {'-':<20} {v613['adjusted_trades']:<20} {'-':<20}")
        print(f"{'跳过交易数 (笔)':<20} {'-':<20} {v613['skipped_trades']:<20} {'-':<20}")
        
        print("\n" + "-" * 80)
        print("📝 总结:")
        print(comparison['summary'])
        print("=" * 80)
    
    def save_report(self, comparison: Dict[str, Any], filepath: str):
        """
        保存回测报告
        
        Args:
            comparison: 对比报告数据
            filepath: 保存路径
        """
        # 转换 Decimal 为 float（用于 JSON 序列化）
        def decimal_to_float(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: decimal_to_float(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_to_float(item) for item in obj]
            return obj
        
        report = {
            'backtest_date': datetime.now().isoformat(),
            'initial_capital': float(self.initial_capital),
            'v612_result': decimal_to_float(comparison['v612']),
            'v613_result': decimal_to_float(comparison['v613']),
            'improvements': decimal_to_float(comparison['improvements']),
            'summary': comparison['summary']
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{filepath}")


def main():
    """主函数"""
    logger.info("开始 v6.13 回测")
    
    # 1. 初始化回测器
    backtester = V613Backtester(initial_capital=Decimal('500'))
    
    # 2. 加载 v6.12 回测数据
    backtest_file = 'data/backtest_report_v5_5_full.json'
    backtest_data = backtester.load_backtest_data(backtest_file)
    trades = backtest_data['trades']
    
    # 3. 模拟 v6.12 固定仓位
    v612_result = backtester.simulate_v612(trades, fixed_margin=Decimal('14'))
    
    # 4. 模拟 v6.13 动态仓位
    v613_result = backtester.simulate_v613(trades)
    
    # 5. 对比分析
    comparison = backtester.compare_strategies(v612_result, v613_result)
    
    # 6. 保存报告
    report_file = f"data/backtest_v613_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backtester.save_report(comparison, report_file)
    
    # 7. 生成 Markdown 报告
    markdown_report = generate_markdown_report(comparison, report_file)
    print(markdown_report)
    
    logger.info("回测完成")


def generate_markdown_report(comparison: Dict[str, Any], report_file: str) -> str:
    """生成 Markdown 格式的回测报告"""
    v612 = comparison['v612']
    v613 = comparison['v613']
    improvements = comparison['improvements']
    
    report = f"""# v6.13 动态仓位调整回测报告

**回测日期**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**数据来源**: {report_file}  
**初始资金**: 500U

---

## 📊 核心指标对比

| 指标 | v6.12 固定仓位 | v6.13 动态仓位 | 改善 |
|------|---------------|---------------|------|
| **总交易数** | {v612['total_trades']} 笔 | {v613['total_trades']} 笔 | {improvements['trade_count_change']:+.0f} |
| **胜率** | {v612['win_rate']:.1%} | {v613['win_rate']:.1%} | {improvements['win_rate_improvement']:+.1%} |
| **总盈亏** | {v612['total_pnl']:.2f}U | {v613['total_pnl']:.2f}U | {improvements['pnl_improvement']:+.2f}U |
| **总收益率** | {v612['total_return']:.1%} | {v613['total_return']:.1%} | {improvements['return_improvement']:+.1%} |
| **最大回撤** | {v612['max_drawdown']:.1%} | {v613['max_drawdown']:.1%} | {improvements['drawdown_improvement']:+.1%} |
| **总手续费** | {v612['total_fees']:.2f}U | {v613['total_fees']:.2f}U | {v613['total_fees'] - v612['total_fees']:+.2f}U |

### v6.13 特有指标

- **调整后交易数**: {v613['adjusted_trades']} 笔
- **跳过交易数**: {v613['skipped_trades']} 笔

---

## 📈 详细交易记录

### v6.12 固定仓位

| # | 币种 | 方向 | 保证金 | 盈亏 | 余额 |
|---|------|------|--------|------|------|
"""
    
    for i, trade in enumerate(v612['trade_details']):
        report += f"| {i+1} | {trade['symbol']} | {trade['direction']} | {trade['margin']:.2f}U | {trade['pnl']:+.2f}U | {trade['balance']:.2f}U |\n"
    
    report += f"""
### v6.13 动态仓位

| # | 币种 | 方向 | 保证金 | 调整 | 盈亏 | 余额 |
|---|------|------|--------|------|------|------|
"""
    
    for i, trade in enumerate(v613['trade_details']):
        adjusted = "✅" if trade['adjusted'] else "-"
        report += f"| {i+1} | {trade['symbol']} | {trade['direction']} | {trade['margin']:.2f}U | {adjusted} | {trade['pnl']:+.2f}U | {trade['balance']:.2f}U |\n"
    
    report += f"""
---

## 📝 总结

{comparison['summary']}

---

## 💡 分析建议

### v6.13 的优势

1. **资金利用率高**: 保留 20% 安全垫，充分利用 80% 可用资金
2. **不错过机会**: 资金不足时自动降仓，而不是直接跳过
3. **风险控制**: 最小保证金阈值（5U）避免过小仓位

### 适用场景

- ✅ **200-500U 账户**: 资金紧张时自动降仓，提高交易机会
- ✅ **波动市场**: 保留安全垫应对极端行情
- ⚠️ **1000U+ 账户**: 资金充足，v6.12 已足够

### 参数优化建议

根据回测结果，可以考虑：

1. **调整安全垫比例**: 
   - 当前：80%
   - 建议：保守型 70%，激进型 90%

2. **调整最小保证金**:
   - 当前：5U
   - 建议：大账户可提高到 8-10U

---

## 📋 回测说明

### 回测假设

1. 初始资金固定为 500U（不考虑充值/提现）
2. 使用 v6.12 历史交易记录的盈亏比例
3. 手续费按万分之四计算（开仓 + 平仓）
4. 不考虑滑点和网络延迟

### 局限性

1. ⚠️ 假设所有信号都能执行（实盘可能有延迟）
2. ⚠️ 使用收盘价计算（实盘使用市价单）
3. ⚠️ 不考虑充值行为（实盘可能追加保证金）

### 下一步

1. 部署 v6.13 到生产环境，收集实盘数据
2. 对比理论回测 vs 实盘表现的差异
3. 根据实盘数据优化参数配置

---

*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    return report


if __name__ == '__main__':
    main()
