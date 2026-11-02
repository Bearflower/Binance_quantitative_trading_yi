# 入场记录对账证据清单（2026-10-30）

2026-10-27 至 10-28 的逐单审计已完成。生产更正前像及审计明细保留在本地 `docs/reports/`；此仓库为公开仓库，因此版本管理中只保存不可逆的 SHA-256 校验值与行数，不发布逐笔交易明细。原始文件未经本次改写。

| 本地证据文件 | 记录数 | SHA-256 |
|---|---:|---|
| `open-entry-reconciliation-2026-10-27-preimage.jsonl` | 310 | `68c65252e67fa261cfc596d0f7fdf9aaee0a5725e908a54f1e7f39637ed77119` |
| `open-entry-reconciliation-2026-10-27-audit.jsonl` | 315 | `5cdec04bb937b911fd6564393eaea600be95241900f8a09894a95e5d104fc0fd` |
| `open-entry-reconciliation-2026-10-27-final-preimage.jsonl` | 203 | `fe5b66ad2d854bb9efc3f44fb2d4452127427b438c66bd6f4a6bf9f0a002eee1` |
| `open-entry-reconciliation-2026-10-27-final-audit.jsonl` | 318 | `7536baab0d268c8af1989db64eba9e55385607e13836ed803ec06ae608d4dccb` |
| `open-entry-reconciliation-2026-10-27-updated.json` | summary | `1ae06c2e00b133a67ab80b5575d585e2c9a4bc44237886c8977c5931608a53ec` |
| `active-symbol-reconciliation-2026-10-27-final-preimage.jsonl` | 108 | `452b1169ec6417fdbfa42d6cfff18463bfaf4dd5d132b1fbed3ff5220b8d40dc` |
| `active-symbol-reconciliation-2026-10-27-updated.json` | summary | `629081dcce7c2b4aa4f5de52b4401832388a319edf671b5a2c3d2ecf9d062408` |
| `new-ghost-entries-2026-10-28-preimage.jsonl` | 2 | `df4ab55b5b6673776c4fde4df8c1fa9f6580f39bab2e7e50a2c85c69d166d287` |
| `bnb-conditions-2026-10-28-preimage.jsonl` | 4 | `cc26000049e6ea89509525a155cd309d915d2329bc76d0a978a835a689c88a14` |

处理摘要：2026-10-27 两批条件关闭 311 条历史未终结入场记录；10-28 另经逐单复核关闭 2 条空仓残留，并按交易所终态更正 4 张 BNB 保护单。本次归档不再次写入生产数据库。代码补丁 `413fa252` 已在三策略生产容器验证；后续真实平仓仍需继续观察。

完整操作叙事保留在本地 `open-entry-reconciliation-2026-10-27.md`，供具备本机工作区访问权的审计人员结合上表哈希核验。
