#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 OPGUSDT 评分记录
"""
import json

# 读取文件
with open('/root/short_selling_system/data/processed_symbols.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# 修复 OPGUSDT 的评分记录
# 实际评分历史:
# 1. 00:01 - attempt1 (第 1 次，丢失)
# 2. 00:03 - attempt2 (第 2 次)  
# 3. 00:34 - attempt3 (第 3 次)
# 4. 09:01 - 应该是第 4 次，但标记为第 1 次
# 5. 09:21 - 应该是第 5 次，但标记为第 1 次
# 6. 10:01 - 应该是第 6 次，但标记为第 1 次
# 7. 11:01 - 应该是第 7 次，但标记为第 1 次
# 8. 12:01 - 应该是第 8 次，但标记为第 1 次
# 9. 13:01 - 应该是第 9 次，但标记为第 1 次
# 10. 14:01 - 应该是第 10 次，但标记为第 1 次

# 统计实际评分文件数量
import subprocess
import glob

# 使用 glob 查找文件
files = glob.glob('/root/short_selling_system/logs/scoring_reports/2026-04-23/OPGUSDT_*_score_report.json')
actual_count = len(files)

print(f"发现 {actual_count} 个评分文件")

# 更新 OPGUSDT 的评分记录
if 'OPGUSDT' in data:
    data['OPGUSDT']['scoring_count'] = actual_count
    print(f"已更新 scoring_count = {actual_count}")
    
    # 保存修复后的文件
    with open('/root/short_selling_system/data/processed_symbols.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print("✅ 文件已保存")
else:
    print("❌ OPGUSDT 不在 processed_symbols.json 中")

# 验证修复结果
with open('/root/short_selling_system/data/processed_symbols.json', 'r', encoding='utf-8') as f:
    verify_data = json.load(f)

if 'OPGUSDT' in verify_data:
    print(f"\n验证结果:")
    print(f"  scoring_count: {verify_data['OPGUSDT']['scoring_count']}")
    print(f"  scoring_history 数量：{len(verify_data['OPGUSDT']['scoring_history'])}")
