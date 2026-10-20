---
id: Binance_quantitative_trading-20261019-shadow-replay-manifest-top-hash-verify-incomplete
类型: issue
项目: Binance_quantitative_trading
标题: 回放快照顶层哈希（manifest.sha256）验证不完整——仅逐文件比对，未重算组合哈希可被同步篡改绕过
日期: 2026-10-19
标签: [bug, 回放, XAU影子, 数据完整性, fail-closed, 快照哈希]
任务标识: xau-mtcps-shadow-pilot_2026-10-19
状态: 已解决
审查: 待审查
---

# 回放快照顶层哈希（manifest.sha256）验证不完整——仅逐文件比对，未重算组合哈希可被同步篡改绕过

## 现象

XAU 影子本地回放引擎的 `snapshot.verify_snapshot` 号称「校验文件 SHA-256 未被篡改」，但只逐文件比对 `manifest["files"][k]["sha256"]` 与磁盘实际哈希；顶层组合哈希 `manifest["sha256"]` 从未被重算，仅被当作写死的元数据。于是「看起来校验通过」的快照仍可能被篡改：只要攻击者/误操作把三文件的哈希字段与顶层哈希**同步**改写（或只改嵌套、顶层留旧值），逐文件校验与顶层值之间不再有独立约束，篡改可被静默放行。此缺陷由第三轮复审打回（P1#2）。

## 根因

`build_snapshot` 与 `verify_snapshot` 对「顶层哈希」的两端定义/校验各自实现、口径未共用：写入侧用某组合口径算 `manifest["sha256"]`，校验侧完全不重算该组合、只查逐文件哈希。**完整性根（root-of-trust）缺少闭环**——顶层哈希既不与三个文件内容构成可验证的确定性映射，也就无法作为「三个文件哈希本身是否被整体替换」的冗余校验。属「写时算、读时不验」型完整性缺陷（与「静默回退默认」族同源于缺显式断言的校验缺口）。

## 解决

`backtest/btc_eth/replay/snapshot.py`：

- 新增共享纯函数 `_compute_manifest_sha256(files)`，canonical 组合：
  `json.dumps({"klines_1m":..,"funding_rates":..,"exchange_info":..}, sort_keys=True, separators=(",",":"))` 的 utf-8 SHA-256。
- `build_snapshot` 写 `manifest["sha256"] = _compute_manifest_sha256(files)`（`files` 为三文件内容哈希键值）。
- `verify_snapshot` 改为**两步 fail-closed**：① 逐文件比对 `files.*.sha256`（缺失 → `{"file":key,"error":"missing"}`；不一致 → mismatch）；② **重算** `actual_top = _compute_manifest_sha256(manifest["files"])` 与 `expected_top = manifest["sha256"]` 比对，不一致 → `files_ok=False` 且 mismatches **追加 `{"file":"__manifest_sha256__", "expected":.., "actual":..}`**。
- 写侧与读侧**共用同一 helper**，杜绝「组合口径漂移」。

## 验证

- 针对性用例：`tests/test_shadow_replay/test_round4_snapshot_report.py`（6 用例，含**顶层哈希篡改 → `files_ok=False`** 的 fail-closed 断言）。
- 夹具同步：`test_e2e_real_chain.py` 顶层哈希改用共享 helper、`test_replay_determinism.py` 夹具同步 canonical，确保既有用例与新口径一致。
- 本轮 `tests/test_shadow_replay` **167 passed**（163→167）；**本轮测试计数与审查「缺口已闭合」以工程师/审查复测为准（文档侧未独立重跑）**。

## 适用范围与修订

- 模块：`backtest/btc_eth/replay/snapshot.py`（本地离线回放器，不部署、不连生产 DB）；对应用例 `tests/test_shadow_replay/{test_round4_snapshot_report,test_e2e_real_chain,test_replay_determinism}.py`。
- 当前适用性：本分支已修复、**未部署**（分支 `xau-mtcps-shadow-pilot`，部署仍暂停）。
- 替代条目：无。
- 日期与原因：2026-10-19 沉淀（第三轮复审打回后修复轮，2026-10-20 闭环）。

## 教训

- 「完整性校验」必须**闭环到根**：写入侧与校验侧对同一不可变对象的哈希**必须共用同一确定性 helper**，且校验侧要**重算**根哈希后比对，不能只比对叶子、把根哈希当摆设。
- 只逐文件比对会留下「叶子与根被同步篡改」的绕过面；fail-closed 必须覆盖根层（不一致即判失败，不静默放行）。
- 校验逻辑写侧/读侧分开实现是口径漂移的温床——凡是「写时计算、读时校验」的派生值，都应收敛到单一纯函数。AI 归纳，仅供参考。

## 关联

- 本地补充来源: [handoff](../../handoffs/xau-mtcps-shadow-pilot_2026-10-19.md)（不随 Git 同步）
- 持久证据: [2026-10-19-xau-replay-fix-design.md](../../superpowers/plans/2026-10-19-xau-replay-fix-design.md) §13.2；`backtest/btc_eth/replay/snapshot.py`（`_compute_manifest_sha256`、`build_snapshot`、`verify_snapshot`）；`tests/test_shadow_replay/test_round4_snapshot_report.py`
- 必要证据摘要: `_compute_manifest_sha256` 基于 `manifest["files"]` 三键 canonical `json.dumps(sort_keys=True, separators=(",",":"))` utf-8 SHA-256；`verify_snapshot` 逐文件校验后重算顶层并比对，不一致 → `files_ok=False` + `{"file":"__manifest_sha256__",...}`。
- 相关: [shadow-replay-flat-trailing-key-silent-default](./Binance_quantitative_trading-20261019-shadow-replay-flat-trailing-key-silent-default.md)（同属 XAU 回放器「缺显式校验断言」缺陷）；[relative-path-wrong-root-silent-load-failure](./Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure.md)（同为「静默放行/静默失效」型）
