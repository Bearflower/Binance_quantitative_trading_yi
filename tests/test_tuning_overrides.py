"""
覆盖层机制幻觉测试
验证代码写完后实际行为是否符合预期

测试项：
1. 文件存在性验证
2. 合并逻辑验证
3. .active 指针验证
4. config_operator 写入验证
5. 回滚验证
6. 边界情况验证
"""
import os
import sys
import tempfile
import shutil
import unittest

# 添加项目根目录到 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from shared.config_loader import load_strategy_config, deep_merge, _read_active_version
from ai_tuner.deploy.config_operator import ConfigOperator


class TestTuningOverridesHallucination(unittest.TestCase):
    """幻觉测试：覆盖层机制"""

    @classmethod
    def setUpClass(cls):
        """创建临时测试目录"""
        cls.test_dir = tempfile.mkdtemp(prefix="tuning_test_")
        cls.strategy_dir = os.path.join(cls.test_dir, "test_strategy")
        cls.override_dir = os.path.join(cls.strategy_dir, "tuning_overrides")
        os.makedirs(cls.override_dir, exist_ok=True)

        # 创建基础配置文件
        cls.base_config = {
            "strategy": {
                "name": "test_strategy",
                "version": "1.0.0",
            },
            "scoring": {
                "min_score": 75,
                "weights": {
                    "trend_strength": 0.25,
                    "pattern_quality": 0.50,
                    "momentum_divergence": 0.25,
                },
            },
            "risk": {
                "stop_loss_atr_multiplier": 2.0,
                "max_daily_loss_usdt": 25,
            },
        }
        cls._write_yaml(os.path.join(cls.strategy_dir, "config.yaml"), cls.base_config)

    @classmethod
    def tearDownClass(cls):
        """清理测试目录"""
        shutil.rmtree(cls.test_dir)

    @classmethod
    def _write_yaml(cls, path, data):
        """写入 YAML 文件"""
        import yaml
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # ============================================================
    # 测试1：文件存在性验证
    # ============================================================

    def test_1_1_config_loader_exists(self):
        """shared/config_loader.py 存在且可导入"""
        try:
            from shared.config_loader import load_strategy_config, deep_merge
            self.assertTrue(callable(load_strategy_config))
            self.assertTrue(callable(deep_merge))
        except ImportError as e:
            self.fail(f"shared/config_loader.py 导入失败: {e}")

    def test_1_2_override_dir_created(self):
        """所有策略的 tuning_overrides/ 目录已创建"""
        strategies = ["btc_eth", "new_coin", "hrs", "grid"]
        for sid in strategies:
            override_dir = os.path.join(PROJECT_ROOT, "strategies", sid, "tuning_overrides")
            self.assertTrue(
                os.path.isdir(override_dir),
                f"{sid} 的 tuning_overrides/ 目录不存在",
            )

    # ============================================================
    # 测试2：合并逻辑验证
    # ============================================================

    def test_2_1_no_overrides_returns_base(self):
        """无覆盖层时，返回原始 config.yaml 内容"""
        # 确保没有 .active 文件
        active_path = os.path.join(self.override_dir, ".active")
        if os.path.exists(active_path):
            os.unlink(active_path)

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["strategy"]["name"], "test_strategy")
        self.assertEqual(config["scoring"]["min_score"], 75)
        self.assertEqual(config["scoring"]["weights"]["trend_strength"], 0.25)

    def test_2_2_overrides_merged_correctly(self):
        """有覆盖层时，覆盖层参数正确合并到基础配置"""
        # 创建覆盖层
        override_config = {
            "scoring": {
                "min_score": 80,
                "weights": {
                    "trend_strength": 0.35,
                },
            },
        }
        self._write_yaml(os.path.join(self.override_dir, "V20260811.yaml"), override_config)
        self._write_yaml(os.path.join(self.override_dir, ".active"), {"dummy": False})  # 先写个占位
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write("V20260811\n")

        config = load_strategy_config(self.strategy_dir)

        # 覆盖层参数优先
        self.assertEqual(config["scoring"]["min_score"], 80)
        self.assertEqual(config["scoring"]["weights"]["trend_strength"], 0.35)

        # 覆盖层不存在的参数，保留基础配置值
        self.assertEqual(config["scoring"]["weights"]["pattern_quality"], 0.50)
        self.assertEqual(config["scoring"]["weights"]["momentum_divergence"], 0.25)
        self.assertEqual(config["risk"]["stop_loss_atr_multiplier"], 2.0)

    def test_2_3_deep_merge_correctness(self):
        """deep_merge 函数逻辑正确"""
        base = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2, 3]}
        override = {"b": {"c": 99}, "f": 100}

        merged = deep_merge(base, override)

        # 覆盖层优先
        self.assertEqual(merged["a"], 1)
        self.assertEqual(merged["b"]["c"], 99)
        # 保留基础值
        self.assertEqual(merged["b"]["d"], 3)
        # 列表直接替换
        self.assertEqual(merged["e"], [1, 2, 3])
        # 新增键
        self.assertEqual(merged["f"], 100)

    def test_2_4_override_none_keeps_base(self):
        """覆盖层值为 None 时，保留基础配置值"""
        base = {"a": 1, "b": 2}
        override = {"a": None}
        merged = deep_merge(base, override)
        self.assertEqual(merged["a"], 1)
        self.assertEqual(merged["b"], 2)

    # ============================================================
    # 测试3：.active 指针验证
    # ============================================================

    def test_3_1_active_points_to_correct_version(self):
        """.active 指向 "V20260811" → 加载 tuning_overrides/V20260811.yaml"""
        # 写入 .active
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write("V20260811\n")

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 80)

    def test_3_2_active_empty_falls_back(self):
        """.active 内容为空 → 降级为基础配置"""
        # 清空 .active
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write("")

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 75)  # 基础值

    def test_3_3_active_missing_falls_back(self):
        """.active 文件不存在 → 降级为基础配置"""
        # 删除 .active
        active_path = os.path.join(self.override_dir, ".active")
        if os.path.exists(active_path):
            os.unlink(active_path)

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 75)  # 基础值

    def test_3_4_active_points_to_nonexistent_falls_back(self):
        """.active 指向不存在的文件 → 降级为基础配置"""
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write("V99999999\n")

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 75)  # 基础值

    # ============================================================
    # 测试4：config_operator 写入验证
    # ============================================================

    def test_4_1_apply_overrides_writes_to_override_dir(self):
        """apply_overrides() 写入 tuning_overrides/ 目录，而非 config.yaml"""
        operator = ConfigOperator()

        # 调用 apply_overrides
        config_path = os.path.join(self.strategy_dir, "config.yaml")
        adjustments = {"scoring.min_score": 85, "scoring.weights.trend_strength": 0.40}
        success = operator.apply_overrides(config_path, adjustments)

        self.assertTrue(success, "apply_overrides 返回失败")

        # 验证：config.yaml 未被修改
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            base_config = yaml.safe_load(f)
        self.assertEqual(base_config["scoring"]["min_score"], 75,
                         "config.yaml 不应被修改")

        # 验证：tuning_overrides/ 下有新文件
        override_files = [f for f in os.listdir(self.override_dir)
                          if f.startswith("V") and f.endswith(".yaml")]
        self.assertGreater(len(override_files), 0, "tuning_overrides/ 下应有版本文件")

        # 验证：.active 已更新
        with open(os.path.join(self.override_dir, ".active"), "r") as f:
            active_version = f.read().strip()
        self.assertTrue(active_version.startswith("V"), f".active 内容异常: {active_version}")

    def test_4_2_apply_overrides_generates_version_file(self):
        """apply_overrides 写入后自动生成 V{日期}.yaml 文件"""
        operator = ConfigOperator()
        config_path = os.path.join(self.strategy_dir, "config.yaml")
        adjustments = {"risk.stop_loss_atr_multiplier": 2.5}

        success = operator.apply_overrides(config_path, adjustments)
        self.assertTrue(success)

        # 验证版本文件存在
        with open(os.path.join(self.override_dir, ".active"), "r") as f:
            version = f.read().strip()
        version_path = os.path.join(self.override_dir, f"{version}.yaml")
        self.assertTrue(os.path.exists(version_path), f"版本文件 {version}.yaml 不存在")

    # ============================================================
    # 测试5：回滚验证
    # ============================================================

    def test_5_1_rollback_by_changing_active(self):
        """修改 .active 指向旧版本 → 读取旧版本"""
        # 先写入 .active 指向当前版本
        operator = ConfigOperator()
        config_path = os.path.join(self.strategy_dir, "config.yaml")

        # 生成一个版本并记录版本号
        operator.apply_overrides(config_path, {"scoring.min_score": 90})
        with open(os.path.join(self.override_dir, ".active"), "r") as f:
            first_version = f.read().strip()

        # 再生成第二个版本
        operator.apply_overrides(config_path, {"scoring.min_score": 95})
        with open(os.path.join(self.override_dir, ".active"), "r") as f:
            second_version = f.read().strip()

        # 回滚到第一个版本
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write(first_version)

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 90,
                         f"回滚到 {first_version} 后 min_score 应为 90, 实际为 {config['scoring']['min_score']}")

    def test_5_2_delete_active_falls_back(self):
        """删除 tuning_overrides/ 目录 → 降级为基础配置"""
        # 备份并删除 override_dir
        backup_dir = self.override_dir + "_backup"
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        shutil.copytree(self.override_dir, backup_dir)
        shutil.rmtree(self.override_dir)

        config = load_strategy_config(self.strategy_dir)
        self.assertEqual(config["scoring"]["min_score"], 75)  # 基础值

        # 恢复（确保父目录存在）
        if os.path.exists(self.override_dir):
            shutil.rmtree(self.override_dir)
        os.makedirs(self.strategy_dir, exist_ok=True)
        shutil.copytree(backup_dir, self.override_dir, dirs_exist_ok=True)

    # ============================================================
    # 测试6：边界情况验证
    # ============================================================

    def test_6_1_empty_override_file(self):
        """tuning_overrides/V{日期}.yaml 是空文件 → 不报错，返回基础配置"""
        # 写入空覆盖层
        self._write_yaml(os.path.join(self.override_dir, "V20260812.yaml"), {})
        with open(os.path.join(self.override_dir, ".active"), "w") as f:
            f.write("V20260812\n")

        config = load_strategy_config(self.strategy_dir)
        self.assertIsNotNone(config)
        self.assertEqual(config["scoring"]["min_score"], 75)

    def test_6_2_override_dir_not_exists(self):
        """tuning_overrides/ 目录不存在 → 不报错，返回基础配置"""
        config = load_strategy_config("/tmp/nonexistent_strategy")
        self.assertEqual(config, {})

    def test_6_3_base_config_not_exists(self):
        """config.yaml 不存在 → 返回空字典"""
        config = load_strategy_config("/tmp/empty_dir")
        self.assertEqual(config, {})

    def test_6_4_generate_version_same_day(self):
        """同一天多次调用 apply_overrides 生成不同版本号"""
        operator = ConfigOperator()
        config_path = os.path.join(self.strategy_dir, "config.yaml")

        # 第一次调用
        op1 = ConfigOperator()
        r1 = op1.apply_overrides(config_path, {"scoring.min_score": 80})
        self.assertTrue(r1)

        # 第二次调用（同一天，自动追加后缀）
        op2 = ConfigOperator()
        r2 = op2.apply_overrides(config_path, {"scoring.min_score": 85})
        self.assertTrue(r2)

        # 验证生成了两个不同的版本文件
        override_files = sorted([f for f in os.listdir(self.override_dir)
                                 if f.startswith("V") and f.endswith(".yaml")])
        self.assertGreaterEqual(len(override_files), 2,
                                f"应该有至少2个版本文件, 实际: {override_files}")


if __name__ == "__main__":
    unittest.main(verbosity=2)