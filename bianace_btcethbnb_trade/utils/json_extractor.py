#!/usr/bin/env python3
"""
JSON 提取工具 - 从 DeepSeek 分析报告中提取交易建议
"""

import re
import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger('json_extractor')

class TradeRecommendExtractor:
    """从分析报告中提取 JSON 格式的交易建议"""
    
    def __init__(self):
        self.json_pattern = r'```json\s*([\s\S]*?)\s*```'
    
    def _fix_chinese_quotes(self, json_str: str) -> str:
        """
        修复 JSON 字符串中的中文引号问题
        
        AI 有时会返回包含中文引号（""）的 JSON，需要替换为英文引号（"）
        
        Args:
            json_str: 原始 JSON 字符串
            
        Returns:
            修复后的 JSON 字符串
        """
        logger.info("尝试修复中文引号问题...")
        
        # 中文引号映射表
        chinese_to_english_quotes = {
            '"': '"',  # 中文左双引号
            '"': '"',  # 中文右双引号
            ''': "'",  # 中文左单引号
            ''': "'",  # 中文右单引号
        }
        
        fixed_str = json_str
        
        # 替换中文引号
        for chinese_char, english_char in chinese_to_english_quotes.items():
            fixed_str = fixed_str.replace(chinese_char, english_char)
        
        # 检查是否有修复
        if fixed_str != json_str:
            chinese_double_quotes_count = json_str.count('"') + json_str.count('"')
            chinese_single_quotes_count = json_str.count(''') + json_str.count(''')
            logger.info(f"✅ 已修复中文引号：{chinese_double_quotes_count} 个中文双引号，{chinese_single_quotes_count} 个中文单引号")
        
        return fixed_str
    
    def extract_json(self, report_content: str) -> Optional[List[Dict[str, Any]]]:
        """
        从报告内容中提取 JSON 数据
        
        Args:
            report_content: DeepSeek 返回的完整报告文本
            
        Returns:
            提取的交易建议列表，如果提取失败则返回 None
        """
        logger.info(f"开始从报告中提取 JSON 数据，报告长度：{len(report_content)} 字符")
        
        # 查找所有 JSON 代码块
        matches = re.findall(self.json_pattern, report_content)
        
        if not matches:
            logger.warning("未找到 JSON 代码块，尝试使用备用解析方法")
            # 备用方案 1：尝试直接查找 JSON 数组或对象
            recommendations = self._extract_json_without_markers(report_content)
            if recommendations:
                logger.info(f"✅ 备用方案成功提取 {len(recommendations)} 条交易建议")
                return recommendations
            
            # 备用方案 2：尝试从结构化文本中提取
            logger.info("尝试从结构化文本中提取交易建议...")
            recommendations = self._extract_from_structured_text(report_content)
            if recommendations:
                logger.info(f"✅ 结构化文本提取成功 {len(recommendations)} 条交易建议")
                return recommendations
            
            logger.error("❌ 所有解析方法都失败")
            return None
        
        logger.info(f"找到 {len(matches)} 个 JSON 代码块")
        
        all_recommendations = []
        parse_errors = []
        
        # 尝试解析每个 JSON 代码块
        for i, json_str in enumerate(matches):
            try:
                data = json.loads(json_str.strip())
                
                # 检查是否是交易建议列表
                if isinstance(data, list):
                    # 是一个数组
                    if self._validate_trade_recommendations(data):
                        logger.info(f"✅ 第 {i+1} 个 JSON 代码块是有效的交易建议列表，包含 {len(data)} 条建议")
                        all_recommendations.extend(data)
                    else:
                        logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块验证失败")
                        # 记录验证失败的原因
                        self._log_validation_errors(data, i+1)
                elif isinstance(data, dict):
                    # 是单个交易建议对象
                    if self._validate_single_recommendation(data):
                        logger.info(f"✅ 第 {i+1} 个 JSON 代码块是有效的单个交易建议")
                        all_recommendations.append(data)
                    else:
                        logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块验证失败")
                        self._log_validation_errors([data], i+1)
                else:
                    logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块不是有效的交易建议")
                    
            except json.JSONDecodeError as e:
                # 尝试修复中文引号问题
                logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块解析失败，尝试修复中文引号...")
                fixed_json_str = self._fix_chinese_quotes(json_str)
                
                # 记录修复日志
                if fixed_json_str != json_str:
                    logger.info(f"✅ 已修复中文引号，尝试重新解析...")
                
                try:
                    data = json.loads(fixed_json_str.strip())
                    
                    # 检查是否是交易建议列表
                    if isinstance(data, list):
                        if self._validate_trade_recommendations(data):
                            logger.info(f"✅ 第 {i+1} 个 JSON 代码块（已修复）是有效的交易建议列表，包含 {len(data)} 条建议")
                            all_recommendations.extend(data)
                        else:
                            logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块（已修复）验证失败")
                            self._log_validation_errors(data, i+1)
                    elif isinstance(data, dict):
                        if self._validate_single_recommendation(data):
                            logger.info(f"✅ 第 {i+1} 个 JSON 代码块（已修复）是有效的单个交易建议")
                            all_recommendations.append(data)
                        else:
                            logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块（已修复）验证失败")
                            self._log_validation_errors([data], i+1)
                    else:
                        logger.warning(f"⚠️ 第 {i+1} 个 JSON 代码块（已修复）不是有效的交易建议")
                        
                except json.JSONDecodeError as e2:
                    error_msg = f"解析第 {i+1} 个 JSON 代码块失败（修复后仍然失败）：{str(e2)}"
                    logger.error(f"❌ {error_msg}")
                    parse_errors.append(f"代码块 {i+1}: {str(e)} -> 修复后：{str(e2)}")
                    logger.debug(f"第 {i+1} 个 JSON 代码块原始内容：{json_str[:500]}")
                    logger.debug(f"第 {i+1} 个 JSON 代码块修复后内容：{fixed_json_str[:500]}")
                    continue
        
        if all_recommendations:
            logger.info(f"✅ 成功提取 {len(all_recommendations)} 条交易建议")
            return all_recommendations
        else:
            if parse_errors:
                logger.error(f"❌ 所有 JSON 代码块都解析失败，共 {len(parse_errors)} 个错误：{'; '.join(parse_errors)}")
            logger.error("❌ 未能提取到有效的交易建议")
            return None
    
    def _log_validation_errors(self, recommendations: List[Dict[str, Any]], json_block_index: int):
        """
        记录验证失败的详细原因
        
        Args:
            recommendations: 交易建议列表
            json_block_index: JSON 代码块索引
        """
        for i, rec in enumerate(recommendations):
            logger.warning(f"  JSON 块{json_block_index}-建议{i+1} 验证失败详情:")
            
            # 检查必需字段
            basic_required_fields = [
                '币种', '开仓方向', '开仓推荐度', '信号等级',
                '止盈设置', '保证金', '实际杠杆', '风险占比', '通过检查清单', '备注'
            ]
            
            for field in basic_required_fields:
                if field not in rec:
                    logger.warning(f"    - 缺少必需字段：{field}")
            
            # 检查字段值有效性
            if '币种' in rec and not rec['币种']:
                logger.warning(f"    - 币种为空")
            
            if '开仓方向' in rec and rec['开仓方向'] not in ['多', '空', '观望']:
                logger.warning(f"    - 开仓方向无效：{rec['开仓方向']}")
            
            if '信号等级' in rec and rec['信号等级'] not in ['S', 'A', 'B', '无', 'A-', 'A+', 'B-', 'B+']:
                logger.warning(f"    - 信号等级无效：{rec['信号等级']}")
            
            if '开仓推荐度' in rec:
                score = rec['开仓推荐度']
                if not isinstance(score, (int, float)) or not (0 <= score <= 100):
                    logger.warning(f"    - 开仓推荐度无效：{score}")
            
            if '止盈设置' in rec and not isinstance(rec['止盈设置'], dict):
                logger.warning(f"    - 止盈设置格式错误：{rec['止盈设置']}")
    
    def _validate_trade_recommendations(self, recommendations: List[Dict[str, Any]]) -> bool:
        """验证交易建议列表"""
        for rec in recommendations:
            if not self._validate_single_recommendation(rec):
                return False
        return True
    
    def _extract_json_without_markers(self, report_content: str) -> Optional[List[Dict[str, Any]]]:
        """
        备用方案：当没有 ```json 标记时，尝试直接提取 JSON 内容
        
        Args:
            report_content: 报告内容
            
        Returns:
            提取的交易建议列表，如果提取失败则返回 None
        """
        logger.info("使用备用方案提取 JSON")
        
        # 方法 1: 尝试匹配 JSON 数组格式
        json_array_pattern = r'\[\s*\{[^}]*\}[^]]*\]'
        matches = re.findall(json_array_pattern, report_content)
        
        if matches:
            all_recommendations = []
            
            for i, json_str in enumerate(matches):
                try:
                    data = json.loads(json_str.strip())
                    
                    if isinstance(data, list):
                        if self._validate_trade_recommendations(data):
                            logger.info(f"备用方案成功解析第 {i+1} 个 JSON 数组，包含 {len(data)} 条建议")
                            all_recommendations.extend(data)
                    elif isinstance(data, dict):
                        if self._validate_single_recommendation(data):
                            logger.info(f"备用方案成功解析第 {i+1} 个 JSON 对象")
                            all_recommendations.append(data)
                            
                except json.JSONDecodeError as e:
                    logger.error(f"备用方案解析第 {i+1} 个 JSON 失败：{str(e)}")
                    continue
            
            if all_recommendations:
                return all_recommendations
        
        # 方法 2: 尝试从结构化文本中提取（当没有 JSON 时）
        logger.info("备用方案：尝试从结构化文本中提取交易建议")
        return self._extract_from_structured_text(report_content)
    
    def _extract_from_structured_text(self, report_content: str) -> Optional[List[Dict[str, Any]]]:
        """
        从结构化的文本报告中提取交易建议
        这是最后一种备用方案，当 JSON 解析完全失败时使用
        
        Args:
            report_content: 报告内容
            
        Returns:
            提取的交易建议列表，如果提取失败则返回 None
        """
        logger.info("开始从结构化文本中提取交易建议...")
        
        recommendations = []
        lines = report_content.split('\n')
        
        # 查找交易建议的起始标记
        current_rec = {}
        in_recommendation = False
        
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            
            # 检查是否是交易建议的开始（第三章或类似标记）
            if '### 3.' in line_stripped and ('BTC' in line_stripped or 'ETH' in line_stripped or 'BNB' in line_stripped):
                # 保存上一条建议
                if current_rec and self._validate_single_recommendation(current_rec):
                    recommendations.append(current_rec)
                    logger.info(f"✅ 从结构化文本中提取到一条建议：{current_rec.get('币种')}")
                current_rec = {}
                in_recommendation = True
                continue
            
            # 提取字段值（支持多种格式）
            if in_recommendation:
                self._extract_field_from_line(line_stripped, current_rec)
        
        # 添加最后一条建议
        if current_rec and self._validate_single_recommendation(current_rec):
            recommendations.append(current_rec)
            logger.info(f"✅ 从结构化文本中提取到一条建议：{current_rec.get('币种')}")
        
        if recommendations:
            logger.info(f"✅ 从结构化文本中成功提取 {len(recommendations)} 条建议")
            return recommendations
        else:
            logger.warning("❌ 从结构化文本中提取失败")
            return None
    
    def _extract_field_from_line(self, line: str, current_rec: Dict[str, Any]):
        """
        从单行文本中提取字段值
        
        Args:
            line: 输入行
            current_rec: 当前交易建议字典
        """
        line_stripped = line.strip()
        
        # 币种 - 支持多种格式
        if '币种' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                # 清理值并标准化币种格式
                symbol = value.replace('"', '').replace('*', '').strip()
                # 如果已经有 USDT 后缀，不再添加
                if not symbol.endswith('USDT'):
                    symbol = symbol + 'USDT'
                current_rec['币种'] = symbol
        
        # 开仓方向
        if '开仓方向' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                direction = value.replace('"', '').replace('*', '').strip()
                # 标准化方向值
                if '多' in direction or 'Long' in direction:
                    current_rec['开仓方向'] = '多'
                elif '空' in direction or 'Short' in direction:
                    current_rec['开仓方向'] = '空'
                elif '观望' in direction or '等待' in direction:
                    current_rec['开仓方向'] = '观望'
        
        # 开仓推荐度
        if '开仓推荐度' in line_stripped or '推荐度' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    score = int(value.replace('"', '').replace('*', '').replace('%', '').strip())
                    current_rec['开仓推荐度'] = score
                except:
                    pass
        
        # 信号等级
        if '信号等级' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                grade = value.replace('"', '').replace('*', '').strip()
                if grade in ['S', 'A', 'B', '无', 'A-', 'A+', 'B-', 'B+']:
                    current_rec['信号等级'] = grade
        
        # 开仓价
        if '开仓价' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    price = float(value.replace('"', '').replace('*', '').replace('U', '').replace(',', '').strip())
                    current_rec['开仓价'] = price
                except:
                    pass
        
        # 强平价
        if '强平价' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    price = float(value.replace('"', '').replace('*', '').replace('U', '').replace(',', '').strip())
                    current_rec['强平价'] = price
                except:
                    pass
        
        # 止损价
        if '止损价' in line_stripped or '止损' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    price = float(value.replace('"', '').replace('*', '').replace('U', '').replace(',', '').strip())
                    current_rec['止损价'] = price
                except:
                    pass
        
        # 止盈设置（简化处理）
        if '止盈' in line_stripped and '设置' in line_stripped or ('止盈' in line_stripped and ':' in line_stripped):
            current_rec['止盈设置'] = {
                "TP1": {"价格": 0, "仓位比例": "50%"},
                "TP2": {"价格": 0, "仓位比例": "30%"},
                "TP3": {"价格": 0, "仓位比例": "20%"}
            }
        
        # 保证金
        if '保证金' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    margin = int(value.replace('"', '').replace('*', '').replace('U', '').strip())
                    current_rec['保证金'] = margin
                except:
                    pass
        
        # 实际杠杆 - 支持"5 倍"、"5x"、"5"等格式
        if '实际杠杆' in line_stripped or ('杠杆' in line_stripped and '倍数' in line_stripped):
            value = self._extract_value_after_colon(line_stripped)
            if value:
                try:
                    # 移除"倍"、"x"、"X"等字符
                    leverage_str = value.replace('"', '').replace('*', '').replace('倍', '').replace('x', '').replace('X', '').strip()
                    leverage = int(leverage_str)
                    current_rec['实际杠杆'] = leverage
                except:
                    pass
        
        # 风险占比
        if '风险占比' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                current_rec['风险占比'] = value.replace('"', '').replace('*', '').strip()
        
        # 通过检查清单
        if '通过检查清单' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                val = value.strip().lower()
                current_rec['通过检查清单'] = val in ['true', '是', '√', '1']
        
        # 备注
        if '备注' in line_stripped:
            value = self._extract_value_after_colon(line_stripped)
            if value:
                current_rec['备注'] = value.replace('"', '').replace('*', '').strip()
    
    def _extract_value_after_colon(self, line: str) -> str:
        """
        提取冒号后的值，支持中英文冒号
        
        Args:
            line: 输入行
            
        Returns:
            提取的值，如果未找到则返回空字符串
        """
        # 支持中文冒号（：U+FF1A）和英文冒号（:）
        # 注意：中文冒号是全角字符，Unicode 为 0xFF1A
        if ':' not in line and ':' not in line and '\uff1a' not in line:
            return ''
        
        # 优先使用中文冒号分割，如果没有再用英文冒号
        if '\uff1a' in line:
            parts = line.split('\uff1a', 1)
        elif ':' in line:
            parts = line.split(':', 1)
        else:
            parts = line.split(':', 1)
            
        if len(parts) > 1:
            return parts[1].strip()
        return ''
    
    def _validate_single_recommendation(self, rec: Dict[str, Any]) -> bool:
        """验证单个交易建议"""
        # 基础必需字段（所有建议都必须有）
        basic_required_fields = [
            '币种', '开仓方向', '开仓推荐度', '信号等级',
            '止盈设置', '保证金', '实际杠杆', '风险占比', '通过检查清单', '备注'
        ]
        
        # 检查基础必需字段
        for field in basic_required_fields:
            if field not in rec:
                logger.warning(f"交易建议缺少必需字段：{field}")
                return False
        
        # 检查币种格式
        symbol = rec.get('币种', '')
        if not symbol or not isinstance(symbol, str):
            logger.warning(f"无效的币种：{symbol}")
            return False
        
        # 检查开仓方向
        direction = rec.get('开仓方向', '')
        if direction not in ['多', '空', '观望']:
            logger.warning(f"无效的开仓方向：{direction}")
            return False
        
        # 检查信号等级
        signal_grade = rec.get('信号等级', '')
        if signal_grade not in ['S', 'A', 'B', '无', 'A-', 'A+', 'B-', 'B+']:
            logger.warning(f"无效的信号等级：{signal_grade}")
            return False
        
        # 检查开仓推荐度
        recommendation_score = rec.get('开仓推荐度', 0)
        if not isinstance(recommendation_score, (int, float)) or not (0 <= recommendation_score <= 100):
            logger.warning(f"无效的开仓推荐度：{recommendation_score}")
            return False
        
        # 检查止盈设置
        take_profit = rec.get('止盈设置', {})
        if not isinstance(take_profit, dict):
            logger.warning(f"无效的止盈设置：{take_profit}")
            return False
        
        # 如果是观望信号或低等级信号，跳过价格字段验证
        if direction == '观望' or signal_grade in ['无', 'B', 'B-', 'B+']:
            logger.info(f"{symbol}: 观望或低等级信号，跳过价格字段验证")
            return True
        
        # 对于实际开仓信号，检查价格字段是否存在
        price_fields = ['开仓价', '强平价', '止损价']
        for field in price_fields:
            if field not in rec:
                logger.warning(f"{symbol}: 缺少必需字段 {field}")
                return False
        
        # 检查币种格式
        symbol = rec.get('币种', '')
        if not symbol or not isinstance(symbol, str):
            logger.warning(f"无效的币种：{symbol}")
            return False
        
        # 检查开仓方向
        direction = rec.get('开仓方向', '')
        if direction not in ['多', '空', '观望']:
            logger.warning(f"无效的开仓方向：{direction}")
            return False
        
        # 检查信号等级
        signal_grade = rec.get('信号等级', '')
        if signal_grade not in ['S', 'A', 'B', '无', 'A-', 'A+', 'B-', 'B+']:
            logger.warning(f"无效的信号等级：{signal_grade}")
            return False
        
        # 检查开仓推荐度
        recommendation_score = rec.get('开仓推荐度', 0)
        if not isinstance(recommendation_score, (int, float)) or not (0 <= recommendation_score <= 100):
            logger.warning(f"无效的开仓推荐度：{recommendation_score}")
            return False
        
        # 检查止盈设置
        take_profit = rec.get('止盈设置', {})
        if not isinstance(take_profit, dict):
            logger.warning(f"无效的止盈设置：{take_profit}")
            return False
        
        # 如果是观望信号，放宽其他字段的验证
        if direction == '观望' or signal_grade in ['无', 'B', 'B-', 'B+']:
            logger.info(f"{symbol}: 观望或低等级信号，跳过价格字段验证")
            return True
        
        # 对于实际开仓信号，严格验证价格字段
        price_fields = ['开仓价', '强平价', '止损价']
        for field in price_fields:
            value = rec.get(field)
            if value is None or value == 'N/A' or value == '':
                logger.warning(f"{symbol}: {field} 无效 (观望信号应使用观望方向)")
                return False
            if not isinstance(value, (int, float)):
                try:
                    float(value)
                except (ValueError, TypeError):
                    logger.warning(f"{symbol}: {field} 不是有效数字：{value}")
                    return False
        
        # 验证保证金（应该是整数）
        margin = rec.get('保证金')
        if margin is not None:
            if isinstance(margin, str):
                try:
                    margin_clean = margin.replace('U', '').strip()
                    int(margin_clean)
                except (ValueError, TypeError):
                    logger.warning(f"{symbol}: 保证金格式错误：{margin}")
                    return False
            elif not isinstance(margin, (int, float)):
                logger.warning(f"{symbol}: 保证金不是有效数字：{margin}")
                return False
        
        return True
    
    def extract_valid_signals(self, recommendations: List[Dict[str, Any]], 
                             min_score: int = 70,
                             allowed_grades: List[str] = None) -> List[Dict[str, Any]]:
        """
        提取有效的交易信号（根据推荐度和信号等级过滤）
        
        Args:
            recommendations: 交易建议列表
            min_score: 最小推荐度（默认 70）
            allowed_grades: 允许的信号等级（默认 ['S', 'A']）
            
        Returns:
            符合条件的交易建议列表
        """
        if allowed_grades is None:
            allowed_grades = ['S', 'A']
        
        valid_signals = []
        
        for rec in recommendations:
            # 排除观望
            if rec.get('开仓方向') == '观望':
                logger.info(f"{rec.get('币种')}：观望，跳过")
                continue
            
            # 检查信号等级 (支持 A-, A+ 等修饰符)
            signal_grade = rec.get('信号等级', '')
            grade_base = signal_grade.rstrip('+-')  # 去除修饰符
            if grade_base not in [g.rstrip('+-') for g in allowed_grades]:
                logger.info(f"{rec.get('币种')}：信号等级 {signal_grade} 不在允许范围内，跳过")
                continue
            
            # 检查推荐度
            recommendation_score = rec.get('开仓推荐度', 0)
            if recommendation_score < min_score:
                logger.info(f"{rec.get('币种')}：推荐度 {recommendation_score} < {min_score}，跳过")
                continue
            
            # 检查必需的价格信息
            if rec.get('开仓价') in ['N/A', None, '']:
                logger.warning(f"{rec.get('币种')}：开仓价无效，跳过")
                continue
            
            if rec.get('止损价') in ['N/A', None, '']:
                logger.warning(f"{rec.get('币种')}：止损价无效，跳过")
                continue
            
            valid_signals.append(rec)
            logger.info(f"{rec.get('币种')}：通过所有检查，添加到有效信号列表")
        
        logger.info(f"从 {len(recommendations)} 条建议中筛选出 {len(valid_signals)} 条有效信号")
        return valid_signals


def extract_trade_recommendations(report_file_path: str) -> Optional[List[Dict[str, Any]]]:
    """
    从报告文件中提取交易建议
    
    Args:
        report_file_path: 报告文件路径
        
    Returns:
        交易建议列表，如果提取失败则返回 None
    """
    try:
        with open(report_file_path, 'r', encoding='utf-8') as f:
            report_content = f.read()
        
        extractor = TradeRecommendExtractor()
        return extractor.extract_json(report_content)
        
    except Exception as e:
        logger.error(f"读取报告文件失败：{str(e)}")
        return None


if __name__ == '__main__':
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        report_path = sys.argv[1]
        recommendations = extract_trade_recommendations(report_path)
        
        if recommendations:
            print(f"成功提取 {len(recommendations)} 条交易建议:")
            for rec in recommendations:
                print(f"- {rec.get('币种')}: {rec.get('开仓方向')} (推荐度：{rec.get('开仓推荐度')}, 信号等级：{rec.get('信号等级')})")
        else:
            print("提取失败")
    else:
        print("用法：python json_extractor.py <报告文件路径>")
