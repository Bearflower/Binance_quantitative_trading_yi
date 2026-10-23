"""AI Tuner 阶段二：双策略确定性回放与资格考试内核。

本包只在开发机（本地离线）执行回放，运行期不连 Postgres、不在容器内触发。
契约见 docs/superpowers/plans/2026-10-22-ai-tuner-stage2-replay-contracts.md。
"""

from __future__ import annotations

# 回放引擎版本：任何影响撮合/事件顺序/序列化口径的改动都必须提升此版本，
# 并写入 run_manifest.json 便于复审追溯。
ENGINE_VERSION = "stage2-replay-1.0.0"

# 费用模型版本：任何影响手续费/滑点/资金费公式的改动都必须提升此版本。
COST_MODEL_VERSION = "decimal-fee-slip-funding-1.0.0"

__all__ = ["ENGINE_VERSION", "COST_MODEL_VERSION"]
