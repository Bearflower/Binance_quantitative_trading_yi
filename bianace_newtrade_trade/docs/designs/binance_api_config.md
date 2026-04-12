# 币安 API 配置说明

## 环境变量配置

在项目根目录的 `.env` 文件中配置币安 API 密钥：

```bash
# 币安 API 配置
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_SECRET_KEY=your_secret_key_here

# 飞书通知配置（可选）
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

## API 密钥获取步骤

### 1. 登录币安账户

访问 [币安官网](https://www.binance.com) 并登录

### 2. 创建 API 密钥

1. 点击右上角头像 → **API 管理**
2. 点击 **创建 API**
3. 输入 API 名称（如：trading_system）
4. 完成安全验证

### 3. 配置 API 权限

**重要：** 确保勾选以下权限：

- ✅ **读取** - 查询账户信息、订单状态
- ✅ **现货及杠杆交易** - 资金划转
- ✅ **合约交易** - 期货下单、撤销订单

### 4. 设置 IP 白名单（推荐）

为了安全，建议设置服务器 IP 白名单：

1. 在 API 管理页面找到 **限制编辑**
2. 选择 **仅受信任的 IP 访问**
3. 输入你的服务器公网 IP

### 5. 保存密钥

**重要提示：**
- API Secret 只会显示一次，请立即复制保存
- 不要将密钥提交到 Git 仓库
- 定期更换 API 密钥

## 权限说明

### 必需权限

| 权限 | 用途 |
|------|------|
| 读取 | 查询余额、持仓、订单状态 |
| 合约交易 | 开仓、平仓、设置止损止盈 |
| 现货交易 | 资金划转（可选） |

### 可选权限

| 权限 | 用途 |
|------|------|
| 杠杆交易 | 杠杆交易（如使用） |
| 提现 | 自动提现（不建议开启） |

## 安全建议

### 1. 使用子账户

建议创建子账户专门用于交易：

1. 主账户 → **子账户** → 创建子账户
2. 为子账户分配交易权限
3. 子账户 API 密钥独立管理

### 2. 设置提币白名单

即使 API 密钥泄露，也无法提币到其他地址：

1. 账户安全 → **提币地址管理**
2. 添加常用提币地址
3. 开启 **仅允许向白名单地址提币**

### 3. 限制 API 权限

遵循最小权限原则：

- ❌ 不要开启 **提现** 权限
- ✅ 只开启必要的交易权限
- ✅ 设置 IP 白名单限制

### 4. 定期轮换密钥

建议每 3-6 个月更换一次 API 密钥

## 测试配置

### 测试网络

币安提供测试网络，可用于开发测试：

```bash
# 测试网络配置
BINANCE_TESTNET=true
BINANCE_API_KEY=test_api_key
BINANCE_SECRET_KEY=test_secret_key
```

### 测试网络地址

- 现货测试网：https://testnet.binance.vision
- 合约测试网：https://testnet.binancefuture.com

## 常见问题

### Q: API 密钥无效？

A: 检查以下几点：
1. 密钥是否正确复制（无多余空格）
2. API 是否已启用
3. IP 白名单是否正确
4. 系统时间是否同步

### Q: 签名验证失败？

A: 可能原因：
1. 系统时间不同步（使用 NTP 同步）
2. Secret Key 配置错误
3. 请求参数顺序问题

### Q: 订单被拒绝？

A: 检查：
1. 账户余额是否充足
2. 是否超过持仓限制
3. 价格/数量精度是否正确
4. 杠杆倍数是否过高

### Q: 如何确认 API 配置成功？

A: 运行测试命令：

```bash
cd short_selling_system
python -c "from core.binance_trading_api import binance_trading_api; print(binance_trading_api.get_account_balance())"
```

如果返回余额信息，说明配置成功。

## 故障排查

### 1. 检查环境变量

```bash
# 查看环境变量是否加载
python -c "from config.settings import settings; print(settings.binance_api_key)"
```

### 2. 检查 API 连接

```bash
# 测试 API 连接
python -c "
from core.binance_trading_api import binance_trading_api
balance = binance_trading_api.get_account_balance()
print('API 连接成功!' if balance else 'API 连接失败!')
"
```

### 3. 查看日志

```bash
# 查看应用日志
tail -f logs/app.log | grep -i "binance"
```

## 相关文档

- [币安 API 官方文档](https://binance-docs.github.io/apidocs/futures/cn/)
- [使用指南](docs/binance_api_usage.md)
- [快速参考](docs/binance_api_quick_reference.md)
