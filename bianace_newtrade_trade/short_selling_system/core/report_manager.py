"""
报告管理器模块

负责：
- 生成详细评分报告
- 保存为 JSON 和 Markdown 格式
- 管理报告目录结构
- 提供历史报告查询功能
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

from utils.logger import logger


class ReportManager:
    """报告管理器"""
    
    def __init__(self, reports_dir: str = "reports"):
        """
        初始化报告管理器
        
        Args:
            reports_dir: 报告保存目录
        """
        self.reports_dir = Path(reports_dir)
        self._ensure_reports_dir()
        
        logger.info(f"✅ 报告管理器初始化完成 (目录：{self.reports_dir})")
    
    def _ensure_reports_dir(self):
        """确保报告目录存在"""
        if not self.reports_dir.exists():
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"📂 创建报告目录：{self.reports_dir}")
        
        # 创建 .gitkeep 文件（保持目录在 git 中）
        gitkeep = self.reports_dir / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
    
    def _get_symbol_dir(self, symbol: str) -> Path:
        """
        获取币种的报告目录
        
        Args:
            symbol: 币种符号
            
        Returns:
            币种报告目录路径
        """
        symbol_dir = self.reports_dir / symbol
        if not symbol_dir.exists():
            symbol_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"📂 创建币种报告目录：{symbol_dir}")
        return symbol_dir
    
    def generate_report(
        self,
        symbol: str,
        listing_time: Optional[datetime],
        scores: Dict[str, Any],
        total_score: float,
        threshold: float,
        veto: bool = False,
        veto_reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        生成评分报告
        
        Args:
            symbol: 币种符号
            listing_time: 上市时间
            scores: 各维度评分详情
            total_score: 综合评分
            threshold: 开仓阈值
            veto: 是否触发一票否决
            veto_reason: 一票否决原因
            
        Returns:
            报告字典
        """
        # 计算上线时长
        hours_since_listing = None
        if listing_time:
            hours_since_listing = (datetime.now() - listing_time).total_seconds() / 3600
        
        # 构建报告
        report = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "listing_time": listing_time.isoformat() if listing_time else None,
            "hours_since_listing": round(hours_since_listing, 1) if hours_since_listing else None,
            "scores": scores,
            "total_score": round(total_score, 2),
            "threshold": threshold,
            "passed": total_score >= threshold and not veto,
            "veto": veto,
            "veto_reason": veto_reason,
            "recommendation": self._generate_recommendation(
                total_score, threshold, veto, veto_reason
            )
        }
        
        return report
    
    def _generate_recommendation(
        self,
        total_score: float,
        threshold: float,
        veto: bool,
        veto_reason: Optional[str]
    ) -> str:
        """
        生成建议
        
        Args:
            total_score: 综合评分
            threshold: 开仓阈值
            veto: 是否触发一票否决
            veto_reason: 一票否决原因
            
        Returns:
            建议文本
        """
        if veto:
            return f"不建议做空（一票否决：{veto_reason}）"
        elif total_score >= threshold:
            return "建议做空（达到开仓条件）"
        else:
            return f"不建议做空（评分不足：{total_score:.2f} < {threshold}）"
    
    def save_report(
        self,
        report: Dict[str, Any],
        symbol: Optional[str] = None,
        timestamp: Optional[str] = None
    ) -> str:
        """
        保存报告
        
        Args:
            report: 报告字典
            symbol: 币种符号（可选，从报告中获取）
            timestamp: 时间戳（可选，从报告中获取）
            
        Returns:
            保存的文件路径
        """
        symbol = symbol or report.get("symbol")
        timestamp = timestamp or report.get("timestamp")
        
        if not symbol:
            logger.error("❌ 无法保存报告：缺少 symbol")
            return ""
        
        # 生成文件名
        if timestamp:
            # 将 ISO 格式转换为文件名友好格式
            file_timestamp = timestamp.replace(":", "-").replace(".", "-")[:19]
        else:
            file_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # 获取币种目录
        symbol_dir = self._get_symbol_dir(symbol)
        
        # 保存 JSON 格式
        json_file = symbol_dir / f"{file_timestamp}_report.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.debug(f"📄 保存 JSON 报告：{json_file}")
        
        # 保存 Markdown 格式
        md_file = symbol_dir / f"{file_timestamp}_report.md"
        self._save_markdown_report(report, md_file)
        logger.debug(f"📄 保存 Markdown 报告：{md_file}")
        
        # 保存最新报告（覆盖）
        latest_json = symbol_dir / "latest_report.json"
        with open(latest_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        latest_md = symbol_dir / "latest_report.md"
        self._save_markdown_report(report, latest_md)
        
        logger.info(f"✅ 报告已保存：{symbol} ({file_timestamp})")
        
        return str(json_file)
    
    def _save_markdown_report(
        self,
        report: Dict[str, Any],
        file_path: Path
    ):
        """
        保存 Markdown 格式报告
        
        Args:
            report: 报告字典
            file_path: 文件路径
        """
        symbol = report.get("symbol", "Unknown")
        
        # 格式化时间
        timestamp = report.get("timestamp", "")
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp)
                timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass
        
        listing_time = report.get("listing_time", "")
        if listing_time:
            try:
                dt = datetime.fromisoformat(listing_time)
                listing_time = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass
        
        # 生成 Markdown 内容
        md_content = f"""# 新币评分报告 - {symbol}

## 基本信息
- **币种**: {symbol}
- **分析时间**: {timestamp}
- **上市时间**: {listing_time}
- **上线时长**: {report.get('hours_since_listing', 'N/A')} 小时

## 综合评分
- **总分**: {report.get('total_score', 0):.2f}/10.0
- **开仓阈值**: {report.get('threshold', 7.0)}
- **是否通过**: {"✅ 是" if report.get('passed') else "❌ 否"}
- **一票否决**: {"✅ 是" if report.get('veto') else "❌ 否"}
- **建议**: {report.get('recommendation', 'N/A')}

## 评分详情

### 1. 合约数据评分 (35%)
- **评分**: {self._get_score(report, 'contract'):.1f}/10.0
- **加权得分**: {self._get_weighted_score(report, 'contract'):.2f}
- **原因**: {self._get_reason(report, 'contract')}
- **详细数据**:
  - OI (持仓量): {self._get_detail(report, 'contract', 'oi_usd')}
  - 市值：{self._get_detail(report, 'contract', 'market_cap')}
  - OI/市值比：{self._get_detail(report, 'contract', 'oi_ratio')}

### 2. 基本面评分 (30%)
- **评分**: {self._get_score(report, 'fundamental'):.1f}/10.0
- **加权得分**: {self._get_weighted_score(report, 'fundamental'):.2f}
- **原因**: {self._get_reason(report, 'fundamental')}
- **详细数据**:
  - 解锁比例：{self._get_detail(report, 'fundamental', 'unlock_percentage')}%
  - 解锁规模：{self._get_detail(report, 'fundamental', 'unlock_scale')}

### 3. 技术面评分 (25%)
- **评分**: {self._get_score(report, 'technical'):.1f}/10.0
- **加权得分**: {self._get_weighted_score(report, 'technical'):.2f}
- **原因**: {self._get_reason(report, 'technical')}
- **详细数据**:
  - 趋势：{self._get_detail(report, 'technical', 'trend')}
  - RSI(14): {self._get_detail(report, 'technical', 'rsi')}
  - ATR 比率：{self._get_detail(report, 'technical', 'atr_ratio')}
  - K 线数据量：{self._get_detail(report, 'technical', 'data_points')} 条

### 4. 情绪面评分 (10%)
- **评分**: {self._get_score(report, 'sentiment'):.1f}/10.0
- **加权得分**: {self._get_weighted_score(report, 'sentiment'):.2f}
- **原因**: {self._get_reason(report, 'sentiment')}
- **详细数据**:
  - 资金费率：{self._get_detail(report, 'sentiment', 'funding_rate')}
  - 年化费率：{self._get_detail(report, 'sentiment', 'annual_rate')}

## 历史报告
- [查看该币种的历史报告](./)

---
*报告由币安新币精准做空系统自动生成*
"""
        
        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(md_content)
    
    def _get_score(self, report: Dict[str, Any], dimension: str) -> float:
        """获取某维度评分"""
        scores = report.get("scores", {})
        return scores.get(dimension, {}).get("score", 0.0)
    
    def _get_weighted_score(self, report: Dict[str, Any], dimension: str) -> float:
        """获取某维度加权得分"""
        scores = report.get("scores", {})
        return scores.get(dimension, {}).get("weighted_score", 0.0)
    
    def _get_reason(self, report: Dict[str, Any], dimension: str) -> str:
        """获取某维度评分原因"""
        scores = report.get("scores", {})
        return scores.get(dimension, {}).get("reason", "N/A")
    
    def _get_detail(self, report: Dict[str, Any], dimension: str, key: str) -> str:
        """获取某维度详细数据"""
        scores = report.get("scores", {})
        details = scores.get(dimension, {}).get("details", {})
        value = details.get(key)
        if value is None:
            return "N/A"
        elif isinstance(value, float):
            return f"{value:.4f}" if value < 1 else f"{value:.2f}"
        else:
            return str(value)
    
    def get_history(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取币种历史报告
        
        Args:
            symbol: 币种符号
            limit: 返回数量限制
            
        Returns:
            历史报告列表
        """
        symbol_dir = self.reports_dir / symbol
        
        if not symbol_dir.exists():
            return []
        
        # 获取所有 JSON 报告文件（排除 latest_report.json）
        report_files = [
            f for f in symbol_dir.glob("*_report.json")
            if f.name != "latest_report.json"
        ]
        
        # 按文件名排序（包含时间戳）
        report_files.sort(key=lambda x: x.name, reverse=True)
        
        # 读取报告
        reports = []
        for file in report_files[:limit]:
            try:
                with open(file, "r", encoding="utf-8") as f:
                    report = json.load(f)
                    reports.append(report)
            except Exception as e:
                logger.error(f"❌ 读取报告失败：{file}, 错误：{e}")
        
        return reports
    
    def analyze_trend(self, symbol: str) -> Dict[str, Any]:
        """
        分析币种评分趋势
        
        Args:
            symbol: 币种符号
            
        Returns:
            趋势分析结果
        """
        history = self.get_history(symbol, limit=20)
        
        if not history:
            return {"error": "无历史数据"}
        
        # 计算平均分
        avg_score = sum(r.get("total_score", 0) for r in history) / len(history)
        
        # 计算通过率
        passed_count = sum(1 for r in history if r.get("passed"))
        pass_rate = passed_count / len(history) * 100
        
        # 计算各维度平均分
        dimensions = ["contract", "fundamental", "technical", "sentiment"]
        dim_avg = {}
        for dim in dimensions:
            scores = [
                r.get("scores", {}).get(dim, {}).get("score", 0)
                for r in history
            ]
            if scores:
                dim_avg[dim] = sum(scores) / len(scores)
            else:
                dim_avg[dim] = 0
        
        return {
            "symbol": symbol,
            "report_count": len(history),
            "avg_score": round(avg_score, 2),
            "pass_rate": round(pass_rate, 2),
            "dimension_avg": dim_avg,
            "latest_score": history[0].get("total_score", 0),
            "trend": "improving" if history[0].get("total_score", 0) > avg_score else "declining"
        }


# 全局报告管理器实例
report_manager = ReportManager()
