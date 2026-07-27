"""
新币做空策略 V4.1 功能测试和回归测试脚本

测试范围：
1. 单元测试：ScoringEngine（5个测试用例）
2. 配置一致性测试
3. 回归测试：回测引擎可运行性

V4.1 核心变更：
- 入场阈值提高：entry_threshold 5.0→7.0，min_total_score 6.0→7.0，min_three_tops_score 2.0→3.0
- 最低K线数延长：14→18
- 降级模式约束：降级模式下技术分必须≥7，情绪分最高6分
- 降级模式情绪分门槛：年化费率<50%不计分（删除了mild分支）
- 消除硬编码：10处硬编码改为配置读取
"""
import sys
import os
import yaml
import tempfile
from typing import Dict, Any, List, Tuple

# 添加项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'strategies', 'new_coin'))

from scoring_engine import ScoringEngine, ScoringResult


# ============================================================
# 测试结果统计
# ============================================================
class TestResult:
    """测试结果收集器"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.details: List[Dict[str, Any]] = []

    def record(self, case_name: str, passed: bool, expected: Any = None, actual: Any = None, error: str = None):
        status = "通过" if passed else "失败"
        self.details.append({
            'case': case_name,
            'status': status,
            'expected': expected,
            'actual': actual,
            'error': error
        })
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        # 实时输出
        marker = "[PASS]" if passed else "[FAIL]"
        print(f"  {marker} {case_name}")
        if not passed:
            if expected is not None or actual is not None:
                print(f"         期望: {expected}")
                print(f"         实际: {actual}")
            if error:
                print(f"         错误: {error}")

    def summary(self) -> str:
        total = self.passed + self.failed
        rate = (self.passed / total * 100) if total > 0 else 0
        return f"总计: {total} 用例, 通过: {self.passed}, 失败: {self.failed}, 通过率: {rate:.1f}%"


# ============================================================
# 辅助函数
# ============================================================
def load_config() -> Dict[str, Any]:
    """加载策略配置文件"""
    config_path = os.path.join(PROJECT_ROOT, 'strategies', 'new_coin', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def make_score_result(
    total_score: float,
    technical_score: float = 0.0,
    sentiment_score: float = 0.0,
    veto: bool = False,
    veto_reason: str = None
) -> ScoringResult:
    """构造评分结果对象（用于测试 should_entry）"""
    return ScoringResult(
        symbol="TESTUSDT",
        total_score=total_score,
        contract_score=0.0,
        technical_score=technical_score,
        sentiment_score=sentiment_score,
        veto=veto,
        veto_reason=veto_reason,
        details={}
    )


# ============================================================
# 测试用例1：入场阈值提高
# ============================================================
def test_case_1_entry_threshold(engine: ScoringEngine, result: TestResult):
    """测试用例1：入场阈值提高（entry_threshold=7.0, min_total_score=7.0, min_three_tops_score=3.0）"""
    print("\n" + "=" * 70)
    print("测试用例1：入场阈值提高")
    print("=" * 70)

    # 1.1 总分=6.5（< 7.0），技术分=8，三次冲顶=4 → 不应入场
    sr = make_score_result(total_score=6.5, technical_score=8.0)
    actual = engine.should_entry(sr, three_tops_score=4.0, total_technical_score=8.0)
    result.record(
        "1.1 总分6.5(<7.0)技术分8冲顶4 → 不应入场",
        passed=(actual is False),
        expected=False,
        actual=actual
    )

    # 1.2 总分=7.5（≥ 7.0），技术分=8，三次冲顶=4 → 应入场
    sr = make_score_result(total_score=7.5, technical_score=8.0)
    actual = engine.should_entry(sr, three_tops_score=4.0, total_technical_score=8.0)
    result.record(
        "1.2 总分7.5(>=7.0)技术分8冲顶4 → 应入场",
        passed=(actual is True),
        expected=True,
        actual=actual
    )

    # 1.3 总分=7.5，技术分=6（< 7.0），三次冲顶=4 → 不应入场
    sr = make_score_result(total_score=7.5, technical_score=6.0)
    actual = engine.should_entry(sr, three_tops_score=4.0, total_technical_score=6.0)
    result.record(
        "1.3 总分7.5技术分6(<7.0)冲顶4 → 不应入场",
        passed=(actual is False),
        expected=False,
        actual=actual
    )

    # 1.4 总分=7.5，技术分=8，三次冲顶=2（< 3.0）→ 不应入场
    sr = make_score_result(total_score=7.5, technical_score=8.0)
    actual = engine.should_entry(sr, three_tops_score=2.0, total_technical_score=8.0)
    result.record(
        "1.4 总分7.5技术分8冲顶2(<3.0) → 不应入场",
        passed=(actual is False),
        expected=False,
        actual=actual
    )


# ============================================================
# 测试用例2：降级模式约束
# ============================================================
def test_case_2_degraded_mode_constraint(engine: ScoringEngine, result: TestResult):
    """测试用例2：降级模式约束（min_technical_score=7.0）"""
    print("\n" + "=" * 70)
    print("测试用例2：降级模式约束")
    print("=" * 70)

    # 2.1 降级模式，总分=7.5，技术分=6（< 7.0）→ 不应入场
    sr = make_score_result(total_score=7.5, technical_score=6.0)
    actual = engine.should_entry(
        sr, three_tops_score=4.0, total_technical_score=6.0, sentiment_degraded=True
    )
    result.record(
        "2.1 降级模式总分7.5技术分6(<7.0) → 不应入场",
        passed=(actual is False),
        expected=False,
        actual=actual
    )

    # 2.2 降级模式，总分=7.5，技术分=8（≥ 7.0）→ 应入场
    sr = make_score_result(total_score=7.5, technical_score=8.0)
    actual = engine.should_entry(
        sr, three_tops_score=4.0, total_technical_score=8.0, sentiment_degraded=True
    )
    result.record(
        "2.2 降级模式总分7.5技术分8(>=7.0) → 应入场",
        passed=(actual is True),
        expected=True,
        actual=actual
    )

    # 2.3 非降级模式，总分=7.5，技术分=7（≥ 7.0）→ 应入场
    sr = make_score_result(total_score=7.5, technical_score=7.0)
    actual = engine.should_entry(
        sr, three_tops_score=4.0, total_technical_score=7.0, sentiment_degraded=False
    )
    result.record(
        "2.3 非降级模式总分7.5技术分7(>=7.0) → 应入场",
        passed=(actual is True),
        expected=True,
        actual=actual
    )


# ============================================================
# 测试用例3：情绪分降级模式上限截断
# ============================================================
def test_case_3_sentiment_cap(engine: ScoringEngine, result: TestResult):
    """测试用例3：情绪分降级模式上限截断（max_sentiment_score=6.0）"""
    print("\n" + "=" * 70)
    print("测试用例3：情绪分降级模式上限截断")
    print("=" * 70)

    # 3.1 降级模式，资金费率极高（年化>100%），OI变化率=0 → 情绪分应≤6.0
    # funding_rate=0.001 → 年化 = 0.001 * 3 * 365 * 100 = 109.5% > 100% → fr_score=5.0
    # 降级模式：5.0 * 2 = 10.0，截断为 6.0
    score, reason = engine.calculate_sentiment_score(
        funding_rate=0.001, oi_change_rate=0.0, sentiment_degraded=True
    )
    result.record(
        "3.1 降级模式年化>100% → 情绪分截断为6.0",
        passed=(score <= 6.0 and abs(score - 6.0) < 0.001),
        expected="<=6.0 (期望6.0)",
        actual=score,
        error=None if score <= 6.0 else f"reason={reason}"
    )

    # 3.2 降级模式，资金费率年化60%（≥50%）→ 情绪分 = 3.0*2 = 6.0（不截断）
    # 60% = funding_rate * 3 * 365 * 100 → funding_rate = 60/(3*365*100) ≈ 0.000547945
    funding_rate_60 = 60.0 / (3 * 365 * 100)
    score, reason = engine.calculate_sentiment_score(
        funding_rate=funding_rate_60, oi_change_rate=0.0, sentiment_degraded=True
    )
    result.record(
        "3.2 降级模式年化60%(>=50%) → 情绪分=6.0(不截断)",
        passed=(abs(score - 6.0) < 0.001),
        expected=6.0,
        actual=score,
        error=None if abs(score - 6.0) < 0.001 else f"reason={reason}"
    )

    # 3.3 降级模式，资金费率年化40%（<50%）→ 情绪分 = 0.0（不计分）
    # 40% = funding_rate * 3 * 365 * 100 → funding_rate = 40/(3*365*100) ≈ 0.000365297
    funding_rate_40 = 40.0 / (3 * 365 * 100)
    score, reason = engine.calculate_sentiment_score(
        funding_rate=funding_rate_40, oi_change_rate=0.0, sentiment_degraded=True
    )
    result.record(
        "3.3 降级模式年化40%(<50%) → 情绪分=0.0(不计分)",
        passed=(abs(score - 0.0) < 0.001),
        expected=0.0,
        actual=score,
        error=None if abs(score - 0.0) < 0.001 else f"reason={reason}"
    )


# ============================================================
# 测试用例4：年化费率计算
# ============================================================
def test_case_4_annualized_rate(engine: ScoringEngine, result: TestResult):
    """测试用例4：年化费率计算（公式：funding_rate * 3 * 365 * 100）"""
    print("\n" + "=" * 70)
    print("测试用例4：年化费率计算")
    print("=" * 70)

    # 4.1 funding_rate=0.0001 → 年化费率 = 10.95%
    actual = engine._calc_annualized_rate(0.0001)
    expected = 10.95
    result.record(
        "4.1 funding_rate=0.0001 → 年化=10.95%",
        passed=(abs(actual - expected) < 0.001),
        expected=expected,
        actual=actual
    )

    # 4.2 funding_rate=0.001 → 年化费率 = 109.5%
    actual = engine._calc_annualized_rate(0.001)
    expected = 109.5
    result.record(
        "4.2 funding_rate=0.001 → 年化=109.5%",
        passed=(abs(actual - expected) < 0.001),
        expected=expected,
        actual=actual
    )


# ============================================================
# 测试用例5：一票否决阈值从配置读取
# ============================================================
def test_case_5_veto_threshold(engine: ScoringEngine, result: TestResult):
    """测试用例5：一票否决阈值从配置读取（veto=0.5）"""
    print("\n" + "=" * 70)
    print("测试用例5：一票否决阈值从配置读取")
    print("=" * 70)

    # 5.1 OI/总交易量比率=0.6（> 0.5）→ 应否决
    # 注意：listing_hours 必须 <= 48 才能进入 OI 比率检查
    veto, reason = engine.check_veto(listing_hours=10.0, oi_volume_ratio=0.6)
    result.record(
        "5.1 OI比率=0.6(>0.5) → 应否决",
        passed=(veto is True),
        expected=True,
        actual=veto,
        error=None if veto else f"reason={reason}"
    )

    # 5.2 OI/总交易量比率=0.3（≤ 0.5）→ 不应否决
    veto, reason = engine.check_veto(listing_hours=10.0, oi_volume_ratio=0.3)
    result.record(
        "5.2 OI比率=0.3(<=0.5) → 不应否决",
        passed=(veto is False),
        expected=False,
        actual=veto,
        error=None if not veto else f"reason={reason}"
    )


# ============================================================
# 配置一致性测试
# ============================================================
def test_config_consistency(config: Dict[str, Any], engine: ScoringEngine, result: TestResult):
    """配置一致性测试：验证 config.yaml 参数与代码读取一致"""
    print("\n" + "=" * 70)
    print("配置一致性测试")
    print("=" * 70)

    # entry_threshold = 7.0
    expected = 7.0
    actual = config.get('scoring', {}).get('entry_threshold')
    actual_engine = engine.entry_threshold
    result.record(
        "config.yaml entry_threshold = 7.0",
        passed=(actual == expected and actual_engine == expected),
        expected=expected,
        actual=f"config={actual}, engine={actual_engine}"
    )

    # min_total_score = 7.0
    expected = 7.0
    actual = config.get('scoring', {}).get('technical', {}).get('min_total_score')
    result.record(
        "config.yaml min_total_score = 7.0",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # min_three_tops_score = 3.0
    expected = 3.0
    actual = config.get('scoring', {}).get('technical', {}).get('min_three_tops_score')
    result.record(
        "config.yaml min_three_tops_score = 3.0",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # min_klines_for_analysis = 18
    expected = 18
    actual = config.get('kline', {}).get('min_klines_for_analysis')
    result.record(
        "config.yaml min_klines_for_analysis = 18",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # degraded_mode.min_technical_score = 7.0
    expected = 7.0
    actual = config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {}).get('min_technical_score')
    result.record(
        "config.yaml degraded_mode.min_technical_score = 7.0",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # degraded_mode.max_sentiment_score = 6.0
    expected = 6.0
    actual = config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {}).get('max_sentiment_score')
    result.record(
        "config.yaml degraded_mode.max_sentiment_score = 6.0",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # 额外验证：kline.limit = 18
    expected = 18
    actual = config.get('kline', {}).get('limit')
    result.record(
        "config.yaml kline.limit = 18 (V4.1从14提高)",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )

    # 额外验证：funding_rate greed 阈值 = 50
    expected = 50
    actual = config.get('scoring', {}).get('sentiment', {}).get('funding_rate', {}).get('thresholds', {}).get('greed')
    result.record(
        "config.yaml funding_rate.greed 阈值 = 50 (V4.1门槛提高)",
        passed=(actual == expected),
        expected=expected,
        actual=actual
    )


# ============================================================
# 回归测试：回测引擎可运行性
# ============================================================
def test_backtest_engine_runnable(result: TestResult):
    """回归测试：验证回测引擎能正常初始化和加载配置"""
    print("\n" + "=" * 70)
    print("回归测试：回测引擎可运行性")
    print("=" * 70)

    try:
        import yaml
        config_path = os.path.join(PROJECT_ROOT, 'strategies', 'new_coin', 'config.yaml')
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        config['backtest'] = {
            'initial_balance': 500,
            'commission_rate': 0.0004,
            'slippage_rate': 0.0001,
            'leverage': 2,
            'start_date': '2026-05-01',
            'end_date': '2026-06-23',
            'data_dir': 'backtest/new_coin/data',
            'mode': 'bar_by_bar'
        }

        temp_path = tempfile.mktemp(suffix='.yaml')
        with open(temp_path, 'w') as f:
            yaml.dump(config, f, allow_unicode=True)

        # 导入回测引擎
        from backtest.new_coin.backtest_engine import BacktestEngine
        engine = BacktestEngine(temp_path)

        # 验证初始化成功
        result.record(
            "回测引擎初始化成功",
            passed=True,
            expected="BacktestEngine 实例创建成功",
            actual="成功"
        )

        # 验证 entry_threshold
        expected = 7.0
        actual = engine.scoring_engine.entry_threshold
        result.record(
            "回测引擎 entry_threshold = 7.0",
            passed=(actual == expected),
            expected=expected,
            actual=actual
        )

        # 验证 min_klines 配置
        expected = 18
        actual = engine.config.get('kline', {}).get('min_klines_for_analysis')
        result.record(
            "回测引擎 min_klines_for_analysis = 18",
            passed=(actual == expected),
            expected=expected,
            actual=actual
        )

        # 验证降级模式配置已加载
        degraded_config = engine.config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
        result.record(
            "回测引擎降级模式配置已加载 (min_technical_score)",
            passed=('min_technical_score' in degraded_config),
            expected="存在 min_technical_score 字段",
            actual=f"min_technical_score={degraded_config.get('min_technical_score')}"
        )

        result.record(
            "回测引擎降级模式配置已加载 (max_sentiment_score)",
            passed=('max_sentiment_score' in degraded_config),
            expected="存在 max_sentiment_score 字段",
            actual=f"max_sentiment_score={degraded_config.get('max_sentiment_score')}"
        )

        # 清理临时文件
        os.remove(temp_path)

    except Exception as e:
        result.record(
            "回测引擎初始化",
            passed=False,
            expected="无异常",
            actual=str(e),
            error=repr(e)
        )


# ============================================================
# 主测试入口
# ============================================================
def main():
    print("=" * 70)
    print("新币做空策略 V4.1 功能测试和回归测试")
    print("=" * 70)

    # 加载配置
    config = load_config()
    engine = ScoringEngine(config)

    # 输出 V4.1 关键参数
    print(f"\n[配置参数]")
    print(f"  entry_threshold = {engine.entry_threshold}")
    print(f"  min_total_score = {config.get('scoring', {}).get('technical', {}).get('min_total_score')}")
    print(f"  min_three_tops_score = {config.get('scoring', {}).get('technical', {}).get('min_three_tops_score')}")
    print(f"  min_klines_for_analysis = {config.get('kline', {}).get('min_klines_for_analysis')}")
    degraded = config.get('scoring', {}).get('sentiment', {}).get('degraded_mode', {})
    print(f"  degraded_mode.min_technical_score = {degraded.get('min_technical_score')}")
    print(f"  degraded_mode.max_sentiment_score = {degraded.get('max_sentiment_score')}")

    # 执行测试
    result = TestResult()

    # 单元测试
    test_case_1_entry_threshold(engine, result)
    test_case_2_degraded_mode_constraint(engine, result)
    test_case_3_sentiment_cap(engine, result)
    test_case_4_annualized_rate(engine, result)
    test_case_5_veto_threshold(engine, result)

    # 配置一致性测试
    test_config_consistency(config, engine, result)

    # 回归测试
    test_backtest_engine_runnable(result)

    # 输出汇总
    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    print(result.summary())
    print()

    # 输出失败用例详情
    failed_cases = [d for d in result.details if d['status'] == '失败']
    if failed_cases:
        print("=" * 70)
        print("失败用例详情")
        print("=" * 70)
        for fc in failed_cases:
            print(f"\n  用例: {fc['case']}")
            print(f"  期望: {fc['expected']}")
            print(f"  实际: {fc['actual']}")
            if fc['error']:
                print(f"  错误: {fc['error']}")
    else:
        print("所有测试用例全部通过！")

    print("\n" + "=" * 70)
    print("测试完成")
    print("=" * 70)

    return 0 if result.failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
