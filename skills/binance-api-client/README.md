# 币安 API Skill 文档索引

## 📚 文档导航

### 核心文档

1. **[SKILL.md](SKILL.md)** - 技能主文档
   - 核心功能介绍
   - 完整使用示例
   - PM 账户说明
   - 常见问题 FAQ
   - **新增**: 实战经验引用

2. **[QUICK_START.md](QUICK_START.md)** - 快速开始指南
   - 5 分钟快速上手
   - 基础使用示例

### 实战经验 ⭐ 新增

3. **[TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md)** - 实战经验总结
   - 精度问题处理（3 个实际案例）
   - 频率限制控制（延迟策略）
   - 常见错误码速查
   - 最佳实践指南
   - 故障排查流程
   - **适合**: 深入理解和解决问题

4. **[QUICK_REFERENCE.md](QUICK_REFERENCE.md)** - 快速参考卡片
   - 精度要求速查表
   - 频率限制速查表
   - 错误码速查表
   - 代码片段速查
   - 监控检查清单
   - 最佳实践口诀
   - **适合**: 打印贴工位随时查阅

### 更新记录

5. **[SKILL_UPDATE_SUMMARY.md](SKILL_UPDATE_SUMMARY.md)** - 技能更新总结
   - v2.0.0: PM 账户适配和自动精度处理
   - v2.1.0: 智能精度验证和延迟控制
   - 详细更新内容和原因

6. **[STOP_LOSS_TAKE_PROFIT_GUIDE.md](STOP_LOSS_TAKE_PROFIT_GUIDE.md)** - 止损止盈指南
   - 条件单设置方法
   - 止损止盈策略

### PM 账户专用 ⭐ 重要

7. **[PM_ACCOUNT_API_GUIDE.md](PM_ACCOUNT_API_GUIDE.md)** - PM 账户 API 接口文档
   - **完整接口文档** (包含原始 `币安接口.md` 内容)
   - **PM 账户专用端点** (`/papi/v1/*` vs `/fapi/v1/*`)
   - **接口对比表** (传统账户 vs PM 账户)
   - **PM 账户特殊注意事项** (杠杆限制、持仓方向、保证金计算)
   - **实战调用流程** (开仓、延迟控制、精度处理)
   - **原始接口文档** (币安接口.md 整合版)
   - **适合**: 所有 PM 账户对接场景

## 🎯 快速查找

### 我要...

#### 学习如何使用币安 API
→ 查看 [SKILL.md](SKILL.md) 的"快速开始"章节

#### 解决精度问题
→ 查看 [TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md) 的"精度问题"章节
→ 或使用 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的"精度要求速查表"

#### 解决频率限制问题
→ 查看 [TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md) 的"频率限制"章节
→ 或使用 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的"频率限制速查表"

#### 查找错误码含义
→ 查看 [TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md) 的"常见错误码"章节
→ 或使用 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的"错误码速查表"

#### 快速查阅常用代码
→ 查看 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的"代码片段速查"

#### 了解 PM 账户
→ 查看 [SKILL.md](SKILL.md) 的"PM 账户（统一账户）重要说明"章节
→ **重点查看**: [PM_ACCOUNT_API_GUIDE.md](PM_ACCOUNT_API_GUIDE.md) (PM 账户专用接口文档)

#### 设置止损止盈
→ 查看 [STOP_LOSS_TAKE_PROFIT_GUIDE.md](STOP_LOSS_TAKE_PROFIT_GUIDE.md)

## 📊 文档对比

| 文档 | 用途 | 长度 | 适合人群 |
|------|------|------|---------|
| SKILL.md | 完整使用手册 | 长 | 所有用户 |
| QUICK_START.md | 快速入门 | 短 | 新手 |
| **PM_ACCOUNT_API_GUIDE.md** | **PM 账户接口文档** | **长** | **PM 账户对接 (重要)** |
| TRADING_EXPERIENCE.md | 实战经验 | 中长 | 开发者、问题解决 |
| QUICK_REFERENCE.md | 快速参考 | 短 | 所有人（建议打印） |
| SKILL_UPDATE_SUMMARY.md | 更新记录 | 长 | 维护者 |
| STOP_LOSS_TAKE_PROFIT_GUIDE.md | 专项指南 | 中 | 需要设置止损止盈的用户 |

## 🔥 热门主题

### 精度问题
- [TRADING_EXPERIENCE.md - 精度问题](TRADING_EXPERIENCE.md#精度问题)
- [QUICK_REFERENCE.md - 精度要求速查](QUICK_REFERENCE.md#精度要求速查)
- [SKILL.md - 精度处理工具示例](SKILL.md#示例-4 精度处理工具)

### 频率限制
- [TRADING_EXPERIENCE.md - 频率限制](TRADING_EXPERIENCE.md#频率限制)
- [QUICK_REFERENCE.md - 频率限制速查](QUICK_REFERENCE.md#频率限制速查)
- [QUICK_REFERENCE.md - 代码片段速查](QUICK_REFERENCE.md#2 添加延迟控制)

### 常见错误
- [TRADING_EXPERIENCE.md - 常见错误码](TRADING_EXPERIENCE.md#常见错误码)
- [QUICK_REFERENCE.md - 错误码速查](QUICK_REFERENCE.md#常见错误码速查)
- [SKILL.md - 常见问题](SKILL.md#常见问题)

### PM 账户
- **[PM_ACCOUNT_API_GUIDE.md](PM_ACCOUNT_API_GUIDE.md)** ⭐ (PM 账户专用接口文档)
- [SKILL.md - PM 账户重要说明](SKILL.md#pm 账户统一账户重要说明)
- [SKILL.md - 订单精度要求](SKILL.md#示例 -4 精度处理工具)
- [SKILL.md - 仓位方向要求](SKILL.md#仓位方向要求)

## 📝 版本信息

**当前版本**: 2.1.0  
**更新日期**: 2026-04-08  
**最新内容**: 
- ✅ 智能精度验证和修正
- ✅ 智能延迟控制
- ✅ 实战经验文档
- ✅ 快速参考卡片

## 💡 使用建议

1. **新手入门**：
   - 先看 [QUICK_START.md](QUICK_START.md)
   - 再看 [SKILL.md](SKILL.md) 的基础示例

2. **遇到问题**：
   - 先查 [QUICK_REFERENCE.md](QUICK_REFERENCE.md)（快速）
   - 再查 [TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md)（详细）

3. **日常开发**：
   - 打印 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 贴工位
   - 收藏 [SKILL.md](SKILL.md) 做手册

4. **代码审查**：
   - 参考 [TRADING_EXPERIENCE.md](TRADING_EXPERIENCE.md) 的最佳实践
   - 使用 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的检查清单

---

**维护者**: Trading System Team  
**最后更新**: 2026-04-08  
**反馈**: 如有问题或建议，请联系团队
