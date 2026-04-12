# 技能更新总结 - 止损止盈条件单经验

> 📅 2026-03-23 更新

## 🎯 更新目标

将今天调试止损止盈条件单接口的经验总结并加入到 `binance-api-client` 技能中，方便后续复用。

## 📝 更新内容

### 1. 主技能文档 (SKILL.md)

**新增章节**：
- ✅ **示例 7：条件单设置（止损止盈）** - 完整代码示例
- ✅ **止损止盈最佳实践** - 详细说明和常见错误
- ✅ **调试经验总结** - 实际问题解决过程记录

**关键内容**：
```python
# ❌ 错误做法
api.place_stop_market_order(...)  # Invalid orderType

# ✅ 正确做法
api.place_pm_conditional_order(
    symbol="ETHUSDT",
    side="SELL",
    position_side="BOTH",
    strategy_type="STOP_MARKET",
    quantity=position_qty,
    stop_price=Decimal('2095.0'),
    reduce_only=True
)
```

**版本升级**: v2.0.0 → v2.1.0

### 2. 快速使用指南 (QUICK_START.md)

**更新章节**：
- ✅ **示例 5：设置止损止盈** - 重写为条件单接口
- ✅ 添加重要提示和错误示例对比

**版本升级**: v1.0 → v1.1

### 3. 新增独立指南 (STOP_LOSS_TAKE_PROFIT_GUIDE.md)

**完整指南包含**：
- ✅ 重要提示和常见错误
- ✅ 关键参数详细说明
- ✅ 接口对比表格
- ✅ 完整示例代码（分批止盈）
- ✅ 实际验证结果
- ✅ 参考文档链接

## 🔑 核心知识点

### 1. 接口选择

| 接口 | 路径 | 用途 | 是否用于止损止盈 |
|------|------|------|----------------|
| `place_um_order` | `/papi/v1/um/order` | 普通订单 | ❌ 否 |
| `place_market_order` | `/papi/v1/um/order` | 市价单 | ❌ 否 |
| `place_pm_conditional_order` | `/papi/v1/um/conditional/order` | 条件单 | ✅ 是 |

### 2. 关键参数

- **strategy_type**: `"STOP_MARKET"` 或 `"TAKE_PROFIT_MARKET"`
- **quantity**: 必须指定（通过 `get_position_risk()` 获取）
- **reduce_only**: 必须为 `True`
- **side**: 多单平仓用 `SELL`，空单平仓用 `BUY`

### 3. 完整流程

```python
# 1. 开仓
order = api.place_market_order(...)

# 2. 获取持仓
positions = api.get_position_risk(symbol)
position_qty = abs(Decimal(positions[0]['positionAmt']))

# 3. 设置止损
api.place_pm_conditional_order(
    strategy_type="STOP_MARKET",
    quantity=position_qty,
    stop_price=stop_price,
    reduce_only=True
)

# 4. 设置止盈（可分批）
api.place_pm_conditional_order(
    strategy_type="TAKE_PROFIT_MARKET",
    quantity=tp_qty,
    stop_price=tp_price,
    reduce_only=True
)
```

## ✅ 验证结果

实际测试成功，所有止损止盈单都已正确设置：

```
2026-03-23 23:29:16 - 止损设置成功，策略 ID=81786402
2026-03-23 23:29:17 - 止盈 1 设置成功，策略 ID=81786403
2026-03-23 23:29:17 - 止盈 2 设置成功，策略 ID=81786404
2026-03-23 23:29:18 - 止盈 3 设置成功，策略 ID=81786405
```

## 📚 文档结构

```
.trae/skills/binance-api-client/
├── SKILL.md                          # ✅ 已更新 (v2.1.0)
├── QUICK_START.md                    # ✅ 已更新 (v1.1)
├── STOP_LOSS_TAKE_PROFIT_GUIDE.md    # ✅ 新增
└── package/
    ├── README.md                     # ✅ 已更新
    └── ...
```

## 🎓 学习要点

### 问题排查过程
1. **发现问题**: `Invalid orderType` 错误
2. **分析原因**: 使用了错误的接口路径
3. **查找文档**: 币安官方文档 - UM 条件单下单
4. **定位方法**: `place_pm_conditional_order`
5. **验证修复**: 手动测试成功

### 经验教训
- ✅ 仔细阅读 API 文档，区分不同接口
- ✅ 条件单必须使用专门的接口
- ✅ `reduce_only` 参数至关重要
- ✅ 必须先获取持仓数量才能设置平仓单

## 🚀 使用建议

1. **开发阶段**: 使用测试网验证逻辑
2. **生产环境**: 严格检查参数，特别是 `reduce_only`
3. **代码审查**: 确保使用正确的接口
4. **文档参考**: 遇到问题先查看 `STOP_LOSS_TAKE_PROFIT_GUIDE.md`

## 📖 参考链接

- [币安官方文档 - UM 条件单下单](https://binance-docs.github.io/apidocs/portfolio_margin/cn/#um-6b9c3e0a)
- 接口路径：`POST /papi/v1/um/conditional/order`

---

**更新者**: Binance API Client Skill  
**更新日期**: 2026-03-23  
**文档版本**: v2.1.0 / v1.1 / v1.0 (新指南)
