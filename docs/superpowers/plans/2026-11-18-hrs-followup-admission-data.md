# HRS Follow-up Admission and Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** 修复策略/账户仓位混用、预期风控告警及真实K线缺口，同步回放并诚实核对剩余数据验收。

**Architecture:** HRS正向归属pure helper与同锁typed snapshot；独立K线真实补取helper；root负责strategy/capture接线，replay域负责版本化历史与反事实对齐。账户风险约束保持。

**Tech Stack:** Python3.11/3.12, Decimal, PostgreSQL, pytest, Docker/GHCR, append-only decision capture.

## Task 1: HRS独立槽位证明

- [x] `strategies/hrs/entry_occupancy.py` + `shared/entry_risk_policy.py`：先添加红例，证明BNB/SOL外仓不占HRS三个槽位，未知owner拒绝；旧tuple语义不变。
- [x] 真实PG测试最后槽位并发、claims/parent hash/gate时效以及全账户保证金不遗漏；命令：`HRS_TEST_POSTGRES_DSN="$HRS_TEST_POSTGRES_DSN" /tmp/hrs-test-venv/bin/python -m pytest tests/test_strategies/test_hrs_entry_occupancy.py -q`。
- [x] 增加锁内reason_code/context；提交owned源码和测试，独立复核。

## Task 2: 真实K线连续性

- [x] `strategies/hrs/kline_repair.py`、`market_data.py`及专属测试：先复现五个标的实际缺小时和不完整4h合成；不改shared服务及core连续性门槛。
- [x] 有限GET补真实bar，保留原始span/哈希与receipt；冲突、未闭合、缺失、预算耗尽均拒绝。复核真实来源，无生产写入。
- [x] 完整1h组成4h；测试失败不替换有效缓存、无假bar/短后缀绕过。

## Task 3: 实盘接线与日志

- [x] `strategy.py` typed snapshot/两库存/PM状态；`decision_context.py`明确runtime scope和真实inputs；先添加 `test_hrs_admission_wiring.py` 红例。
- [x] EntryDenied记录INFO业务事件、reason_code/context；unknown异常保留错误；预留释放/交易HTTP路径不改。
- [x] standard/LV评分使用修复数组并留证，持仓保护不依赖新开仓数据修复。

## Task 4: 回放和数据审计

- [x] `ai_tuner/hrs_evidence_export.py` / adapter / DataPortal按raw envelope复验v2证明；虚拟HRS槽位+全账户margin，v1不重解释。
- [x] 真实PG→archive→seal→engine→CLI与MTPC禁HRS import回归；完整事前窗口/身份/成本条件不放宽。
- [x] 确认真实午夜原点缺口、8–9月反事实、SL/TP优选及持续样本条件；不能形成的事实明确阻断，不篡改账本。

## Task 5: 独立审查、CI、部署和六点汇报

- [x] root逐文件OCR+领域交叉审查；确认所有已发现缺陷修复并重测。
- [x] 候选CI独立分支使用现有已注册workflow构建，产品deploy.yml不被替换；不可变镜像/新源清单/实际配置及仓位预检通过后部署。
- [ ] 核对权限、源闭包、拒单上下文、实际连续K线/市值/新自然扫描、原保护单不变。
- [ ] 对仓位及告警、K线、日初权益、SL/TP、8–9月和24小时/五个自然闭合六项逐项报告实际状态，未得数据不得标全部完成。
