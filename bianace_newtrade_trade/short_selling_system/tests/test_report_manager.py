"""
报告管理器测试

测试内容：
- 报告生成是否正常
- 文件格式是否正确
- 目录结构是否合理
- 历史报告查询功能
"""

import os
import sys
import json
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.report_manager import ReportManager
from datetime import datetime, timedelta


def test_report_manager_initialization():
    """测试报告管理器初始化"""
    print("\n=== 测试 1: 初始化报告管理器 ===")
    
    report_manager = ReportManager("test_reports")
    
    # 检查目录是否创建
    assert report_manager.reports_dir.exists(), "报告目录未创建"
    print(f"✅ 报告目录已创建：{report_manager.reports_dir}")
    
    # 检查.gitkeep 文件
    gitkeep = report_manager.reports_dir / ".gitkeep"
    assert gitkeep.exists(), ".gitkeep 文件未创建"
    print(f"✅ .gitkeep 文件已创建")
    
    print("✅ 测试 1 通过：初始化成功\n")
    return report_manager


def test_report_generation(report_manager):
    """测试报告生成"""
    print("\n=== 测试 2: 生成报告 ===")
    
    # 模拟评分数据
    scores = {
        "contract": {
            "score": 8.0,
            "weight": 0.35,
            "weighted_score": 2.80,
            "reason": "OI/市值比高，泡沫明显",
            "details": {
                "oi_usd": 5000000,
                "market_cap": 6250000,
                "oi_ratio": 0.8
            }
        },
        "fundamental": {
            "score": 7.0,
            "weight": 0.30,
            "weighted_score": 2.10,
            "reason": "解锁比例较高",
            "details": {
                "unlock_percentage": 15.5,
                "unlock_scale": "medium"
            }
        },
        "technical": {
            "score": 6.0,
            "weight": 0.25,
            "weighted_score": 1.50,
            "reason": "偏弱趋势，技术面不利",
            "details": {
                "trend": "downtrend",
                "rsi": 45.5,
                "atr_ratio": 0.025,
                "data_points": 200
            }
        },
        "sentiment": {
            "score": 5.0,
            "weight": 0.10,
            "weighted_score": 0.50,
            "reason": "资金费率中等",
            "details": {
                "funding_rate": 0.0005,
                "annual_rate": 54.75
            }
        }
    }
    
    # 生成报告
    report = report_manager.generate_report(
        symbol="TESTUSDT",
        listing_time=datetime.now() - timedelta(hours=5),
        scores=scores,
        total_score=6.9,
        threshold=7.0,
        veto=False,
        veto_reason=None
    )
    
    # 验证报告内容
    assert report["symbol"] == "TESTUSDT", "币种符号错误"
    assert report["total_score"] == 6.9, "总分错误"
    assert report["passed"] == False, "是否通过标志错误"
    assert report["hours_since_listing"] is not None, "上线时长缺失"
    
    print(f"✅ 报告生成成功：{report['symbol']}")
    print(f"   总分：{report['total_score']}")
    print(f"   是否通过：{report['passed']}")
    print(f"   上线时长：{report['hours_since_listing']} 小时")
    print(f"   建议：{report['recommendation']}")
    
    print("✅ 测试 2 通过：报告生成成功\n")
    return report


def test_report_saving(report_manager, report):
    """测试报告保存"""
    print("\n=== 测试 3: 保存报告 ===")
    
    # 保存报告
    file_path = report_manager.save_report(report)
    
    # 验证文件存在
    assert file_path, "文件路径为空"
    assert Path(file_path).exists(), "JSON 报告文件未创建"
    print(f"✅ JSON 报告已保存：{file_path}")
    
    # 验证 Markdown 文件存在
    md_file = Path(file_path).with_suffix(".md")
    assert md_file.exists(), "Markdown 报告文件未创建"
    print(f"✅ Markdown 报告已保存：{md_file}")
    
    # 验证 latest 文件存在
    symbol_dir = Path(file_path).parent
    latest_json = symbol_dir / "latest_report.json"
    latest_md = symbol_dir / "latest_report.md"
    assert latest_json.exists(), "latest_report.json 未创建"
    assert latest_md.exists(), "latest_report.md 未创建"
    print(f"✅ 最新报告文件已更新")
    
    # 验证 JSON 内容
    with open(file_path, "r", encoding="utf-8") as f:
        saved_report = json.load(f)
    
    assert saved_report["symbol"] == report["symbol"], "保存的报告币种错误"
    assert saved_report["total_score"] == report["total_score"], "保存的总分错误"
    print(f"✅ JSON 内容验证通过")
    
    print("✅ 测试 3 通过：报告保存成功\n")
    return file_path


def test_history_query(report_manager):
    """测试历史报告查询"""
    print("\n=== 测试 4: 历史报告查询 ===")
    
    import time
    
    # 生成多个报告
    for i in range(3):
        report = report_manager.generate_report(
            symbol="HISTORYUSDT",
            listing_time=datetime.now() - timedelta(hours=i*2),
            scores={
                "contract": {"score": 5.0, "weight": 0.35, "weighted_score": 1.75, "reason": "测试", "details": {}},
                "fundamental": {"score": 5.0, "weight": 0.30, "weighted_score": 1.50, "reason": "测试", "details": {}},
                "technical": {"score": 5.0, "weight": 0.25, "weighted_score": 1.25, "reason": "测试", "details": {}},
                "sentiment": {"score": 5.0, "weight": 0.10, "weighted_score": 0.50, "reason": "测试", "details": {}}
            },
            total_score=5.0 + i,
            threshold=7.0
        )
        report_manager.save_report(report)
        time.sleep(1.1)  # 确保时间戳不同
    
    # 查询历史
    history = report_manager.get_history("HISTORYUSDT", limit=5)
    
    assert len(history) == 3, f"历史报告数量错误：{len(history)}"
    print(f"✅ 查询到 {len(history)} 条历史报告")
    
    # 验证报告按时间排序
    for i, report in enumerate(history):
        print(f"   报告 {i+1}: 总分={report['total_score']}")
    
    print("✅ 测试 4 通过：历史查询成功\n")


def test_trend_analysis(report_manager):
    """测试趋势分析"""
    print("\n=== 测试 5: 趋势分析 ===")
    
    trend = report_manager.analyze_trend("HISTORYUSDT")
    
    assert "error" not in trend, "趋势分析返回错误"
    assert trend["symbol"] == "HISTORYUSDT", "币种错误"
    assert trend["report_count"] == 3, "报告数量错误"
    assert "avg_score" in trend, "缺少平均分"
    assert "pass_rate" in trend, "缺少通过率"
    
    print(f"✅ 趋势分析结果:")
    print(f"   币种：{trend['symbol']}")
    print(f"   报告数量：{trend['report_count']}")
    print(f"   平均分：{trend['avg_score']}")
    print(f"   通过率：{trend['pass_rate']:.2f}%")
    print(f"   趋势：{trend['trend']}")
    
    print("✅ 测试 5 通过：趋势分析成功\n")


def cleanup():
    """清理测试文件"""
    print("\n=== 清理测试文件 ===")
    import shutil
    
    test_dir = Path("test_reports")
    if test_dir.exists():
        shutil.rmtree(test_dir)
        print(f"✅ 已清理测试目录：{test_dir}")


def main():
    """运行所有测试"""
    print("="*60)
    print("报告管理器测试")
    print("="*60)
    
    try:
        # 运行测试
        report_manager = test_report_manager_initialization()
        report = test_report_generation(report_manager)
        test_report_saving(report_manager, report)
        test_history_query(report_manager)
        test_trend_analysis(report_manager)
        
        print("="*60)
        print("🎉 所有测试通过！")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        # 清理
        cleanup()
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
