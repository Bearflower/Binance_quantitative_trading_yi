#!/usr/bin/env python3
"""
报告生成器
生成 JSON 和 Markdown 格式的回测报告
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict


class ReportGenerator:
    """报告生成器"""
    
    def __init__(self, report: Dict, analysis: Dict):
        self.report = report
        self.analysis = analysis
    
    def save_json_report(self, output_path: str):
        """保存 JSON 格式报告"""
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        full_report = {
            'generated_at': datetime.now().isoformat(),
            'report': self.report,
            'analysis': self.analysis
        }
        
        with open(output, 'w', encoding='utf-8') as f:
            json.dump(full_report, f, ensure_ascii=False, indent=2, default=str)
        
        return str(output)
    
    def generate_markdown_report(self) -> str:
        """生成 Markdown 格式报告"""
        summary = self.report.get('summary', {})
        assessment = self.analysis.get('performance_assessment', {})
        recommendations = self.analysis.get('recommendations', [])
        
        md = []
        md.append("# 做空策略回测报告\n")
        md.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        md.append("## 📊 基础统计\n")
        md.append(f"- **总交易数**: {summary.get('total_trades', 0)} 笔")
        md.append(f"- **盈利交易**: {summary.get('winning_trades', 0)} 笔")
        md.append(f"- **亏损交易**: {summary.get('losing_trades', 0)} 笔")
        md.append(f"- **手续费总额**: {summary.get('total_fees', 0):.2f} USDT\n")
        
        md.append("## 💰 盈利能力\n")
        md.append(f"- **初始资金**: {summary.get('initial_capital', 0):.0f} USDT")
        md.append(f"- **最终资金**: {summary.get('final_capital', 0):.2f} USDT")
        md.append(f"- **总盈亏**: {summary.get('total_pnl', 0):.2f} USDT")
        md.append(f"- **总收益率**: {summary.get('total_return', 0):.1%}\n")
        
        md.append("## 📈 稳定性指标\n")
        win_rate_analysis = self.analysis.get('win_rate_analysis', {})
        pl_ratio_analysis = self.analysis.get('profit_loss_ratio_analysis', {})
        
        md.append(f"- **胜率**: {summary.get('win_rate', 0):.1%} ({win_rate_analysis.get('assessment', 'N/A')})")
        md.append(f"- **盈亏比**: {summary.get('profit_loss_ratio', 0):.2f} ({pl_ratio_analysis.get('assessment', 'N/A')})\n")
        
        md.append("## 🏆 综合评估\n")
        md.append(f"**{assessment.get('overall', 'N/A')}**\n")
        md.append(f"- 胜率评级：{assessment.get('win_rate', 'N/A')}")
        md.append(f"- 盈亏比评级：{assessment.get('profit_loss_ratio', 'N/A')}")
        md.append(f"- 收益率评级：{assessment.get('returns', 'N/A')}")
        md.append(f"- 综合评分：{assessment.get('score', 0):.1f}/5.0\n")
        
        grade_analysis = self.analysis.get('grade_analysis', {})
        if grade_analysis:
            md.append("## 📊 按信号等级统计\n")
            md.append("| 等级 | 交易数 | 胜率 | 总盈亏 | 贡献度 |")
            md.append("|------|--------|------|--------|--------|")
            for grade in ['S', 'A', 'B', 'N/A']:
                if grade in grade_analysis:
                    stats = grade_analysis[grade]
                    md.append(f"| {grade} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.1%} | {stats.get('total_pnl', 0):.2f}U | {stats.get('contribution', 0):.1%} |")
            md.append("")
        
        exit_analysis = self.analysis.get('exit_reason_analysis', {})
        if exit_analysis:
            md.append("## 🚪 出场原因分析\n")
            md.append("| 出场原因 | 次数 | 占比 |")
            md.append("|----------|------|------|")
            for reason, stats in exit_analysis.items():
                reason_cn = {
                    'STOP_LOSS': '止损',
                    'TAKE_PROFIT_1': '第一止盈',
                    'TAKE_PROFIT_2': '第二止盈',
                    'TIME_STOP': '时间止损'
                }.get(reason, reason)
                md.append(f"| {reason_cn} | {stats.get('count', 0)} | {stats.get('percentage', 0):.1%} |")
            md.append("")
        
        symbol_analysis = self.analysis.get('symbol_analysis', {})
        if symbol_analysis:
            md.append("## 🪙 按币种统计\n")
            md.append("| 币种 | 交易数 | 胜率 | 总盈亏 | 平均盈亏 |")
            md.append("|------|--------|------|--------|----------|")
            
            sorted_symbols = sorted(
                symbol_analysis.items(),
                key=lambda x: x[1].get('total_pnl', 0),
                reverse=True
            )
            
            for symbol, stats in sorted_symbols[:10]:
                md.append(f"| {symbol} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.1%} | {stats.get('total_pnl', 0):.2f}U | {stats.get('avg_pnl', 0):.2f}U |")
            md.append("")
        
        md.append("## 💡 优化建议\n")
        for rec in recommendations:
            md.append(f"- {rec}")
        md.append("")
        
        if self.report.get('trades'):
            md.append("## 📝 交易样本 (前 20)\n")
            md.append("| # | 币种 | 方向 | 入场价 | 出场价 | 盈亏 | 出场原因 |")
            md.append("|---|------|------|--------|--------|------|----------|")
            
            for i, trade in enumerate(self.report['trades'][:20], 1):
                md.append(f"| {i} | {trade['symbol']} | {trade['direction']} | {trade['entry_price']:.2f} | {trade['exit_price']:.2f} | {trade['pnl']:.2f}U | {trade['exit_reason']} |")
            md.append("")
        
        md.append("---\n")
        md.append("*本报告由做空策略回测系统自动生成*")
        
        return "\n".join(md)
    
    def save_markdown_report(self, output_path: str):
        """保存 Markdown 格式报告"""
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        md_content = self.generate_markdown_report()
        
        with open(output, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        return str(output)
    
    def generate_all(self, json_path: str, md_path: str) -> Dict:
        """生成所有格式的报告"""
        json_result = self.save_json_report(json_path)
        md_result = self.save_markdown_report(md_path)
        
        return {
            'json_report': json_result,
            'markdown_report': md_result
        }
