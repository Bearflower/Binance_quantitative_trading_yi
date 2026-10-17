---
id: Binance_quantitative_trading-20261015-income-history-silent-empty-non-unified
类型: issue
项目: Binance_quantitative_trading
标题: get_income_history 非统一账户静默返回 []，调用方无法区分「真无收入」与「被拦截」
日期: 2026-10-15
标签: [bug, 静默失效, binance_api, 统一账户, income, 数据采集]
任务标识: c1_d1_evidence_contract_2026-10-15
状态: 待复查
审查: 待审查
---

# get_income_history 非统一账户静默返回 []，调用方无法区分「真无收入」与「被拦截」

## 现象
在非统一账户模式（`use_unified_account=False`）下调用 `BinanceClient.get_income_history`，方法**不发任何 HTTP 请求、直接返回空列表 `[]`，仅打一条 warning**。于 D1 只读采集脚本（`scripts/export_execution_facts.py` 的 `export_income`）而言，这一空结果与「该账户在该时间窗内确实没有任何收入流水」**在返回值层面完全同形**，脚本无从区分二者；其 while 循环把空数组判为 `page_complete=TRUE` 正常收尾、不抛异常，**采集结果可能被静默丢弃而看似成功**。

## 根因
`shared/binance_api.py` 的 `get_income_history`（约 L1536-1577）开头存在模式守卫：
```python
if not self.use_unified_account:
    logger.warning("收入查询仅支持统一账户模式")
    return []
```
即 `income` 查询仅支持 PMC（统一账户）的 `/papi/v1/um/income` 端点；非统一账户走该分支，用 **空列表 + warning** 表达「不可用」。这是**静默降级**：失败信号（warning 日志）与合法空结果（`[]`）在**返回值通道**上不可区分，调用方若只看返回值就会误判为「无数据」。

## 解决
本轮**未修改该行为**（旁路只读、零行为变更定位）。规避方式为**调用方契约**：
- `ExecutionFactExporter` docstring（`scripts/export_execution_facts.py`）显式以「关于 api_client 的统一账户告警」小节告知：调用方应确保传入的 `api_client` 已启用统一账户（`use_unified_account=True`），否则 income 采集可能被静默丢弃，且数据完整性取决于调用方；脚本自身不抛异常。
- CLI 工厂 `_build_client_from_env` 注明 `use_unified_account` 由 `BinanceClient` 默认值决定（项目默认 `True`），并提示传非统一账户客户端会被静默返回 `[]`。

## 验证
- 单元测试 `tests/test_accounting/test_fact_ingestion_gaps.py::test_api_income_history_non_unified_returns_empty_without_request` 直接固化该行为：非统一账户时**不发请求**、返回 `[]`。
- 同文件 `test_api_income_history_forwards_params_and_non_list_fallback` 覆盖统一账户路径的参数透传与非列表响应回退空列表。
- 本轮 C1/D1 全部测试：`python3 -m pytest tests/test_shared/test_decision_evidence.py tests/test_accounting/ -q` → **84 passed**（本地 2026-10-15）。

## 适用范围与修订
`shared/binance_api.py` 的 `get_income_history`；`scripts/export_execution_facts.py`（`ExecutionFactExporter` / `_build_client_from_env`）。**状态为「待复查」而非「已解决」**：代码行为未改，仅为规避性告知；真正的修复（如在返回值/异常通道上显式暴露「不可用」信号，或让采集侧 fail-closed）尚未实施。无替代条目。

## 教训
「守卫分支返回空集合 + 打日志」是一种静默降级，**在返回值通道上与合法空结果同形**，任何按返回值判定的自动化（采集、对账、统计）都会误判为「无数据」。需要区分「无数据」与「不可用」时，应当**在返回值或异常上显式携带可用性信号**（或 fail-closed），而不是只靠日志。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: 本轮任务（`c1_d1_evidence_contract_2026-10-15`）交付物，handoff 见 `docs/handoffs/`（不随 Git 同步）
- 持久证据: `shared/binance_api.py`（`get_income_history`，约 L1558-1560 守卫）、`scripts/export_execution_facts.py`（`ExecutionFactExporter` docstring、`_build_client_from_env`）、`tests/test_accounting/test_fact_ingestion_gaps.py`（`test_api_income_history_non_unified_returns_empty_without_request`）
- 必要证据摘要: 非统一账户 → 不发请求、返回 `[]` + warning；采集脚本把空数组判为 `page_complete=TRUE`；C1/D1 测试 84 passed；SQL 未 apply、CLI 未组装真实 sink
- 相关: [deploy-yml-init-scripts-unsynced](./Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)（同为「静默不生效」族，已在其中补记本轮 `migrations/` 不随部署生效的边界场景）
