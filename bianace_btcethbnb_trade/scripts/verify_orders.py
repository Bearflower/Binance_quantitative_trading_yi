#!/bin/bash

# ============================================
# 验证订单和数据库记录
# ============================================

SERVER_IP="43.156.242.184"

echo "============================================="
echo "验证订单和数据库记录"
echo "============================================="
echo ""

# 步骤 1: 查询数据库统计
echo "📊 步骤 1/3: 查询数据库统计..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker exec binance-trade-analyzer python3 << 'EOF'
from models.database import get_db_manager
db = get_db_manager()

# 查询每日统计
print('=== 每日执行统计 ===')
query = '''
    SELECT stat_date, signals_count, executed_count, win_count, loss_count
    FROM daily_execution_stats
    ORDER BY stat_date DESC
    LIMIT 7
'''
result = db._execute_query(query)
for row in result:
    total = row['win_count'] + row['loss_count']
    win_rate = (row['win_count'] / row['executed_count'] * 100) if row['executed_count'] > 0 else 0
    print(f\"{row['stat_date']}: 信号={row['signals_count']}, 执行={row['executed_count']}, \"
          f\"盈利={row['win_count']}, 亏损={row['loss_count']}, 胜率={win_rate:.1f}%\")

print()

# 查询交易记录
print('=== 最近交易记录 ===')
query = '''
    SELECT symbol, direction, open_time, status, order_id, entry_price, quantity
    FROM trade_records
    ORDER BY open_time DESC
    LIMIT 10
'''
result = db._execute_query(query)
for row in result:
    print(f\"{row['symbol']} {row['direction']}: {row['status']} \"
          f\"订单 ID={row['order_id']} 价格={row['entry_price']} 数量={row['quantity']} \"
          f\"时间={row['open_time']}\")
EOF
"

echo ""

# 步骤 2: 查询实际订单
echo "📋 步骤 2/3: 查询币安实际订单..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker exec binance-trade-analyzer python3 << 'EOF'
from utils.binance_trade_api import BinanceTradeAPI
api = BinanceTradeAPI()

# 获取所有交易对的订单
for symbol in ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']:
    try:
        import requests
        url = f'{api.base_url}/papi/v1/um/order'
        params = {
            'symbol': symbol,
            'limit': 5,
            'timestamp': int(__import__('time').time() * 1000)
        }
        params['signature'] = api._generate_signature(__import__('urllib.parse').urlencode(params))
        headers = api._get_headers()
        r = requests.get(url, params=params, headers=headers)
        orders = r.json()
        
        if isinstance(orders, list) and len(orders) > 0:
            print(f'\n{symbol} 最近订单:')
            for order in orders[:3]:
                print(f\"  {order['orderId']}: {order['side']} {order['type']} \"
                      f\"{order['executedQty']} @ {order['avgPrice']} \"
                      f\"状态：{order['status']}\")
        else:
            print(f'\n{symbol}: 无订单')
    except Exception as e:
        print(f'\n{symbol}: 查询失败 - {e}')
EOF
"

echo ""

# 步骤 3: 查看当前持仓
echo "💼 步骤 3/3: 查看当前持仓..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker exec binance-trade-analyzer python3 << 'EOF'
from utils.binance_trade_api import BinanceTradeAPI
api = BinanceTradeAPI()

import requests
url = f'{api.base_url}/papi/v1/account'
params = {'timestamp': int(__import__('time').time() * 1000)}
params['signature'] = api._generate_signature(__import__('urllib.parse').urlencode(params))
headers = api._get_headers()
r = requests.get(url, params=params, headers=headers)
account = r.json()

print('=== 账户信息 ===')
print(f\"可用余额：{account.get('availableBalance', 'N/A')} USDT\")
print(f\"总余额：{account.get('totalWalletBalance', 'N/A')} USDT\")
print()

print('=== 当前持仓 ===')
has_position = False
for pos in account.get('positions', []):
    if float(pos.get('positionAmt', 0)) != 0:
        has_position = True
        pnl = float(pos.get('unRealizedProfit', 0))
        print(f\"{pos['symbol']}: {pos['positionAmt']} \"
              f\"入场价：{pos['entryPrice']} 标记价：{pos['markPrice']} \"
              f\"未实现盈亏：{pnl:.2f} USDT\")

if not has_position:
    print('当前无持仓')
EOF
"

echo ""
echo "============================================="
echo "验证完成！"
echo "============================================="
