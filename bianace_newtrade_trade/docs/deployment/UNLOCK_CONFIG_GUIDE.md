# 代币解锁配置指南

## 📝 配置说明

代币解锁数据需要**手动配置**，系统会从配置文件中读取并计算基本面评分。

---

## 🔧 配置步骤

### 步骤 1: 找到配置文件

```bash
# 配置文件路径
/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/config/unlock_config.json
```

### 步骤 2: 编辑配置文件

使用文本编辑器打开 `unlock_config.json`，添加币种信息：

```json
{
  "NEWUSDT": {
    "unlocks": [
      {
        "date": "2026-04-15",
        "percentage": 15.5,
        "target": "team",
        "description": "团队代币解锁"
      }
    ]
  }
}
```

### 步骤 3: 配置字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `date` | 字符串 | ✅ | 解锁日期，格式：`YYYY-MM-DD` |
| `percentage` | 数字 | ✅ | 解锁比例（%），如 `15.5` 表示 15.5% |
| `target` | 字符串 | ✅ | 解锁对象类型，可选值：<br>- `team`：团队/创始人<br>- `investor`：早期投资者/机构<br>- `ecosystem`：生态基金<br>- `mining`：挖矿释放 |
| `description` | 字符串 | ❌ | 解锁描述（可选） |

---

## 📊 评分规则

系统会根据未来 90 天内的解锁比例自动评分：

| 解锁比例 | 评分 | 说明 |
|---------|------|------|
| **> 20%** | 10 分 | 大额解锁，高风险，高优先级 |
| **10% - 20%** | 7 分 | 中等解锁，值得考虑 |
| **5% - 10%** | 3 分 | 小额解锁，影响较小 |
| **< 5%** | 0 分 | 几乎无影响 |

---

## 💡 示例配置

### 示例 1: 大额解锁（高风险）

```json
{
  "APTUSDT": {
    "unlocks": [
      {
        "date": "2026-04-20",
        "percentage": 22.0,
        "target": "investor",
        "description": "A16z 等机构大额解锁"
      }
    ]
  }
}
```

**评分结果**: 10 分（>20%）

---

### 示例 2: 多次解锁

```json
{
  "ROSEUSDT": {
    "unlocks": [
      {
        "date": "2026-05-01",
        "percentage": 8.0,
        "target": "investor",
        "description": "机构投资者解锁"
      },
      {
        "date": "2026-06-15",
        "percentage": 5.5,
        "target": "ecosystem",
        "description": "生态基金解锁"
      }
    ]
  }
}
```

**计算过程**: 8.0% + 5.5% = 13.5%  
**评分结果**: 7 分（10%-20%）

---

### 示例 3: 无解锁

```json
{
  "BTCUSDT": {}
}
```

**评分结果**: 0 分（无解锁）

---

## 🔍 如何获取解锁信息？

### 推荐数据源

1. **[TokenUnlocks](https://tokenunlocks.com/)**
   - 专业的代币解锁数据网站
   - 提供详细的解锁时间表

2. **[Dropstab](https://www.dropstab.com/)**
   - 加密货币数据平台
   - 包含解锁信息

3. **项目官方文档**
   - Tokenomics 页面
   - 白皮书

4. **[CryptoRank](https://cryptorank.io/)**
   - 提供解锁日历

---

## 📋 配置检查清单

在配置前，请确认：

- [ ] 币种是最近 7 天内上线的新币
- [ ] 从可靠数据源获取了解锁信息
- [ ] 解锁日期格式正确（YYYY-MM-DD）
- [ ] 解锁比例是百分比数值（如 15.5）
- [ ] 解锁对象类型正确（team/investor/ecosystem/mining）

---

## ⚠️ 注意事项

1. **及时更新**: 发现新币上线后，及时添加解锁信息
2. **数据准确性**: 确保解锁信息来自可靠来源
3. **删除过期**: 解锁事件过期后，可以从配置中删除
4. **不要配置过多**: 只关注真正的大额解锁（>10%）

---

## 🚀 快速开始

### 方法 1: 使用示例配置

```bash
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system/config

# 复制示例配置
cp unlock_config_sample.json unlock_config.json

# 编辑配置
vim unlock_config.json
```

### 方法 2: 从零开始

```bash
cd /Users/yl/vscode/bianace_newtrade_trade/short_selling_system/config

# 创建空配置
echo '{}' > unlock_config.json

# 编辑配置
vim unlock_config.json
```

---

## 📞 常见问题

### Q1: 不配置会怎样？
A: 基本面评分会是 0 分（默认），不会错过机会，但可能错过高优先级目标。

### Q2: 配置错误会怎样？
A: 系统会记录错误日志，该币种的基本面评分会是 0 分。

### Q3: 需要配置所有币种吗？
A: 不需要。只配置你关注的、有大额解锁的新币即可。

### Q4: 多久更新一次？
A: 建议每天检查一次，或者发现新币上线时立即添加。

---

## 📝 配置模板

```json
{
  "SYMBOLUSDT": {
    "unlocks": [
      {
        "date": "YYYY-MM-DD",
        "percentage": 0.0,
        "target": "team|investor|ecosystem|mining",
        "description": "解锁描述"
      }
    ]
  }
}
```

---

**最后更新**: 2026-03-11  
**版本**: v1.0
