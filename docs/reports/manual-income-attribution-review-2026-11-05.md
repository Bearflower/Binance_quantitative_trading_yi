# 未归属收入人工复核与入库

2026-11-05 17:19（北京时间）的 2026-11 快照含 127 条未归属交易所收入事件，其中 58 条带候选策略线索。该数字是截至导出时点的快照，不代表整月最终总量。Excel 和对应源 JSON 保存在本机 `data/review_exports/`，权限仅供本地复核；这些账户明细不提交到 Git。

## 人工填写

打开 `data/review_exports/outputs/strategy-attribution-review-20261105/未归属收入人工复核-2026-11.xlsx` 的“逐笔复核”页，仅填写黄色的 L–O 列：

- L「审核决定」：`确认`、`跳过`，或留空待查。
- M「确认策略」：仅在确认时填写 `hrs`、`btc_eth`、`btc_eth_aggressive`、`new_coin`、`grid` 之一；不必与建议策略相同。
- N「审核人」：填写可识别的姓名或账号。
- O「证据备注」：至少 8 字，写明可回查的订单、条件单、策略日志或资金费持仓依据。单靠同币种、时间相近或建议列不足以确认。

其余列是交易所事实和核对线索，保持原样。长流水号、成交号、订单号均以文本保存，避免 Excel 精度损失。资金费须核实对应持仓的策略归属；没有足够证据就留空或跳过。

## 导入流程

填完后在本地生成待入库批次（此步不连接数据库）：

```bash
python3 -m scripts.prepare_review_import \
  --workbook data/review_exports/outputs/strategy-attribution-review-20261105/未归属收入人工复核-2026-11.xlsx \
  --source data/review_exports/unattributed-review-source-2026-11.json \
  --output data/review_exports/reviewed-import-2026-11.json
```

程序逐列核验导出源与 Excel，拒绝变更事实字段、被 Excel 改成数字的长 ID、无效策略、缺审核人或缺证据的确认行。输出文件仅含已确认行，模式为 `0600`。导入程序默认只读检查生产库事实与已有人工归属；必须显式加 `--apply` 才在一个事务中写入 `trading.reviewed_income_attribution`，已有相同记录会跳过，冲突则整批回滚。导入前先看 dry-run 的 `would_insert`、`already_present` 和错误明细；实际执行由维护人员在具备数据库环境变量的服务器容器中运行：

```bash
python3 -m scripts.apply_reviewed_attribution --batch /path/to/reviewed-import-2026-11.json
python3 -m scripts.apply_reviewed_attribution --batch /path/to/reviewed-import-2026-11.json --apply
```

批次 JSON 含账户明细及人工结论，只在受控主机私下传递，不提交 Git。当前表格尚无人工确认，因此没有执行 `--apply`。

## 验收

入库后重新运行 `scripts.report_economic_ledger` 的对应月份并启用 `--use-reviewed-overrides`。逐笔检查确认行均从未归属清单消失且归属到确认策略；交易所收入三类行数、账户合计金额和游标覆盖须与入库前一致；核验人工归属表中的 `reviewed_by`、`evidence`、`reviewed_at`。仍未归属的行继续显示为未知，不能凑成五策略净收益或开启基于不完整数据的自动资金分配。新增交易所事件需要下一次快照和复核。
