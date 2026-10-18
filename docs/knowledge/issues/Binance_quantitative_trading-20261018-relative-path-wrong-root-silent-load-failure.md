---
id: Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure
类型: issue
项目: Binance_quantitative_trading
标题: 仓库根相对路径用错基准目录（ai_tuner/ 而非仓库根）+ 吞异常，致 manifest/策略配置静默加载失败
日期: 2026-10-18
标签: [配置, 路径口径, ai_tuner, 静默失效, 部署, 高优先级]
任务标识: ai_tuner_mvp_stage1_safety_readiness_2026-10-17
状态: 已解决
审查: 待审查
---

# 仓库根相对路径用错基准目录 + 吞异常，致 manifest/策略配置静默加载失败

## 现象

「目的驱动型 AI Tuner MVP」阶段一开发中，`ParameterManifest` 加载与策略生效配置读取「看起来没问题」，但实际：
- `_read_effective_config()` 读不到 `strategies/<s>/config.yaml`，返回 `{}`（空配置）而非报错；
- `WeeklyTuningJob` 拼 manifest 绝对路径时基准目录错误 + `except Exception` 吞掉 `FileNotFoundError`，静默返回 `""`；
- 下游 `validate_proposal()` 因 `get_path(effective_config, path)` 恒为 `None`，把「配置路径不存在」误判为参数不可用，**校验形同失效且无任何错误上抛**。

## 根因

同一仓库内出现**两个仓库根口径**，且用相对路径拼接时基准不一致：

- `ai_tuner/adapters/base_adapter.py`：`_load_manifest` 与 `_read_effective_config` 曾分别用不同基准。前者按 `ai_tuner/parameters/manifests/...` 推导，后者用 `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` 只上溯到 **`ai_tuner/` 目录**；而 `config_path`（如 `strategies/btc_eth_aggressive/config.yaml`）是**仓库根相对**路径 → 拼接后文件不存在 → 返回 `{}`。
- `ai_tuner/scheduler/weekly_job.py`（H2）：manifest 路径同为仓库根相对，却以错误基准拼接；且异常处理为裸 `except Exception`，把 `FileNotFoundError` 一并吞掉 → 静默 `""`。

两者同属「相对路径 + 错误基准 + 吞异常」族，表现为**静默失效**（无异常、无日志、错误结果被当正常结果继续流转）。

## 解决

统一为**唯一仓库根常量** `BaseAdapter._PROJECT_ROOT = Path(__file__).resolve().parents[2]`：
- H1（`base_adapter.py`）：`_load_manifest()` 与 `_read_effective_config()` **共用同一 `_PROJECT_ROOT`** 拼仓库根相对路径（替换 `os.path.dirname(dirname(...))`）。
- H2（`weekly_job.py`）：manifest/策略配置一律用同一 `_PROJECT_ROOT` 常量拼绝对路径；并把裸 `except Exception` **收窄为显式 `FileNotFoundError` 分支**，命中即 `logger.error("manifest 文件不存在（H2 路径拼错或文件缺失）")` 上抛/告警，不再静默返回 `""`。

## 验证

- 回归用例：`tests/test_ai_tuner_mvp/test_h_severity_regressions.py`（H1/H2 路径口径）、`tests/test_ai_tuner_mvp/test_parameter_manifest.py`、`tests/test_ai_tuner_mvp/test_manifest_validation.py`；阶段一累计 **219 用例通过**。
- 相关：`ai_tuner/Dockerfile` 补 `COPY strategies/btc_eth_aggressive/config.yaml`，确保容器内 `_read_effective_config()` 能读到真实配置（进程/容器两环境路径口径一致）。

## 适用范围与修订

- 模块：`ai_tuner/adapters/base_adapter.py`、`ai_tuner/scheduler/weekly_job.py`；MVP 候选 `btc_eth_aggressive`、`hrs`。
- 当前适用性：已修复。
- 替代条目：无。
- 日期与原因：2026-10-18 沉淀（阶段一收口）。

## 教训

- 一个仓库里**只允许一个仓库根口径**：用 `Path(__file__).resolve().parents[N]` 显式锚定，禁止 `os.path.dirname(dirname(...))` 之类「上溯 N 级」隐式推导拼仓库根相对路径。
- 读文件**失败必须可见**：禁止裸 `except Exception` 吞 `FileNotFoundError`；至少 `logger.error` + 让调用方拿到「缺失」信号，而非空值继续流转。
- 「返回空 dict/空串」是最危险的静默失效形态——空配置会让下游校验/决策**看似通过、实际失效**。
- （AI 归纳仅供参考）

## 关联

- 本地补充来源: [handoff](../../handoffs/)（不随 Git 同步）
- 持久证据: `ai_tuner/adapters/base_adapter.py`（`_PROJECT_ROOT`、`_load_manifest`、`_read_effective_config`）、`ai_tuner/scheduler/weekly_job.py`（H2 路径与异常收窄）、`tests/test_ai_tuner_mvp/test_h_severity_regressions.py`
- 必要证据摘要: 阶段一实装引入 `_PROJECT_ROOT` 统一口径后，manifest 校验的 `from` 值能对齐真实生效配置；219 用例通过。待验证项：无。
- 相关: [deploy-yml-init-scripts-unsynced](../issues/Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)（同为「部署看似执行、实际未按预期生效」型）
