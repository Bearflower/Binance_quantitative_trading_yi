#!/usr/bin/env python3
"""
DeepSeek API client for analyzing Binance data
"""

import os
import base64
import requests
import json
from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_API_BASE, DEEPSEEK_MODEL
import logging

logger = logging.getLogger('deepseek_client')

def encode_image(image_path):
    """
    Encode image to base64 (for backward compatibility)
    """
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding image: {str(e)}")
        return None

def send_multiple_screenshots_to_deepseek(screenshot_paths, document_content, currency="COMPREHENSIVE", prompt=None, api_data=None):
    """
    Send Binance data to DeepSeek API for analysis
    
    Args:
        screenshot_paths: 截图路径列表（兼容用，实际使用 api_data）
        document_content: 500U 交易规则文档内容（config/traderule.txt）
        currency: 币种标识
        prompt: 分析提示词
        api_data: API 数据字典
    """
    if not DEEPSEEK_API_KEY:
        logger.error("DeepSeek API key not found")
        return "Error: DeepSeek API key not configured"
    
    # Prepare messages
    messages = []
    
    # Add system prompt with 500U account information
    system_prompt = """You are a professional cryptocurrency trading analyst specializing in 500U micro-account trading.

**Current Account Status (500U Phase 1)**:
- Total Capital: 500U
- Single Position Margin: Fixed 30U (6% of total capital)
- Maximum Concurrent Positions: 2
- Reserve Capital: ≥400U (80%)
- Allowed Signal Grades: S + A only (No B grade)

**Analysis Requirements**:
- Strictly follow the provided 500Utrade_rule_v3.0.md rules
- All trading recommendations must comply with 500U phase 1 specifications
- Calculate position sizes, stop-loss, and take-profit based on 30U margin per position
- Use 7-8x leverage for S-grade signals, 5-6x for A-grade signals

Analyze the provided Binance data and provide detailed trading insights following the 500U rules."""
    messages.append({"role": "system", "content": system_prompt})
    
    # Add document content (500U trading rules)
    if document_content:
        messages.append({
            "role": "user", 
            "content": f"【500U 交易规则 - 必须严格遵守】\n{document_content}"
        })
        logger.info(f"Added config/traderule.txt (500U rules) content, length: {len(document_content)} characters")
    
    # Add API data (preferred method)
    if api_data:
        # 进一步简化 API 数据，只发送最核心的关键信息
        simplified_data = {}
        for symbol, data in api_data.items():
            # 极度简化指标数据，只保留最核心的值
            simplified_indicators = {}
            indicators = data.get('indicators', {})
            
            # 只保留时间戳
            if 'timestamp' in indicators:
                simplified_indicators['timestamp'] = indicators['timestamp']
            
            # 保留所有重要的时间周期和指标
            for timeframe in ['1d', '4h', '1h', '15m']:  # 保留日线、4 小时、1 小时和 15 分钟线
                if timeframe in indicators:
                    tf_data = indicators[timeframe]
                    simplified_tf = {}
                    
                    # 保留核心价格指标
                    if 'prices' in tf_data and len(tf_data['prices']) > 0:
                        simplified_tf['price'] = tf_data['prices'][-1]
                    if 'ema21' in tf_data and len(tf_data['ema21']) > 0:
                        simplified_tf['ema21'] = tf_data['ema21'][-1]
                    if 'atr14' in tf_data and len(tf_data['atr14']) > 0:
                        simplified_tf['atr14'] = tf_data['atr14'][-1]
                    if 'rsi' in tf_data and len(tf_data['rsi']) > 0:
                        simplified_tf['rsi'] = tf_data['rsi'][-1]
                    
                    simplified_indicators[timeframe] = simplified_tf
            
            # 只保留资金费率
            if 'funding_rate' in indicators:
                simplified_indicators['funding_rate'] = indicators['funding_rate']
            
            simplified_data[symbol] = {
                "lastPrice": data.get('lastPrice'),
                "priceChangePercent": data.get('priceChangePercent'),
                "indicators": simplified_indicators
            }
        
        api_data_str = json.dumps(simplified_data, indent=2, ensure_ascii=False)
        logger.info(f"Simplified API data length: {len(api_data_str)} characters")
        
        # 将提示词放在开头，强调格式要求
        if prompt:
            # 提示词在前，API 数据在后，确保 DeepSeek 首先看到格式要求
            content = f"{prompt}\n\n---\n\n以下是 Binance API 实时数据，请基于上述要求进行分析：\n\n{api_data_str}"
        else:
            # 使用默认提示词作为备选
            default_prompt = "请分析 BTCUSDT、ETHUSDT、BNBUSDT 三个交易对，并提供详细的交易建议，包括：开仓方向、推荐度 (0-100)、强平价、止损价、分批止盈价及对应仓位比例、开仓占用总资金比例。请确保报告完整，不要截断。"
            content = f"{default_prompt}\n\n---\n\nBinance API data for {currency}:\n{api_data_str}"
        logger.info(f"Final content length: {len(content)} characters")
        
        messages.append({"role": "user", "content": content})
    elif screenshot_paths:
        # Fallback to screenshot method (for backward compatibility)
        image_contents = []
        
        for path in screenshot_paths:
            if os.path.exists(path):
                if path.endswith('.json'):
                    # Handle JSON data files
                    try:
                        with open(path, 'r') as f:
                            data = json.load(f)
                        data_str = json.dumps(data, indent=2, ensure_ascii=False)
                        image_contents.append(f"Data file {os.path.basename(path)}:\n{data_str}")
                    except Exception as e:
                        logger.error(f"Error reading JSON file: {str(e)}")
                else:
                    # Handle image files - just add a placeholder since we can't send actual images
                    image_contents.append(f"[Image file: {os.path.basename(path)}]\n")
        
        if image_contents:
            content = f"Binance trading data for {currency}:\n"
            for item in image_contents:
                content += f"{item}\n\n"
            
            content += f"\n{prompt or 'Please analyze these screenshots and provide trading insights.'}"
            messages.append({"role": "user", "content": content})
        else:
            logger.error("No valid files found for analysis")
            return "Error: No valid files for analysis"
    else:
        logger.error("No data provided for analysis")
        return "Error: No data provided for analysis"
    
    # Prepare API request
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 8000,  # DeepSeek API max_tokens range: [1, 8192]
        "timeout": 300
    }
    
    # 记录完整的请求内容
    logger.info(f"Sending request to DeepSeek API with payload:")
    logger.info(f"Model: {DEEPSEEK_MODEL}")
    logger.info(f"Messages count: {len(messages)}")
    for i, msg in enumerate(messages):
        logger.info(f"Message {i+1} - Role: {msg.get('role', 'unknown')}")
        content_preview = str(msg.get('content', ''))[:200] + "..." if len(str(msg.get('content', ''))) > 200 else str(msg.get('content', ''))
        logger.info(f"Message {i+1} - Content preview: {content_preview}")
    
    try:
        logger.info("Sending request to DeepSeek API...")
        response = requests.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers=headers,
            json=payload,
            timeout=240
        )
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"API response received, choices count: {len(result.get('choices', []))}")
            
            # 不再保存原始响应为 json 文件，只保留 txt 报告
            
            if 'choices' in result and len(result['choices']) > 0:
                content = result['choices'][0]['message']['content']
                logger.info(f"Response content length: {len(content)} characters")
                logger.info(f"Response content preview: {content[:500]}...")
                
                # 检查响应是否被截断
                if len(content) < 1000:
                    logger.warning("Response content is unusually short, may be truncated")
                
                # 检查响应是否包含截断标记
                truncation_markers = ['...', 'truncated', 'cut off', 'incomplete']
                if any(marker in content.lower() for marker in truncation_markers):
                    logger.warning("Response may be truncated, contains truncation markers")
                
                # 检查是否所有交易对都有完整分析
                required_pairs = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
                missing_pairs = []
                for pair in required_pairs:
                    if pair not in content:
                        missing_pairs.append(pair)
                
                if missing_pairs:
                    logger.warning(f"Response may be incomplete, missing analysis for: {', '.join(missing_pairs)}")
                
                return content
            else:
                logger.error(f"Invalid response from DeepSeek API: {result}")
                return f"Error: Invalid response from API: {result}"
        else:
            logger.error(f"DeepSeek API error: {response.status_code} - {response.text}")
            return f"Error: API request failed with status {response.status_code}"
            
    except Exception as e:
        logger.error(f"Error calling DeepSeek API: {str(e)}", exc_info=True)
        return f"Error: {str(e)}"

def save_response(response, filepath):
    """
    Save the analysis response to a file
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(response)
        
        return filepath
    except Exception as e:
        logger.error(f"Error saving response: {str(e)}")
        return None
