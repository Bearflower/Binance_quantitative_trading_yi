#!/bin/bash
# SOLUSDT K线数据全面验证脚本

echo '========================================'
echo '     SOLUSDT K线数据全面验证'
echo '========================================'
echo

echo '--- 1. 数据库各周期数据量 ---'
docker exec common_service_postgres psql -U binance -d binance_data -c "
SELECT '1h' as period, COUNT(*) FROM kline_solusdt_1h
UNION ALL SELECT '4h', COUNT(*) FROM kline_solusdt_4h
UNION ALL SELECT '1d', COUNT(*) FROM kline_solusdt_1d
ORDER BY period;
"
echo

echo '--- 2. API接口验证(1h最新) ---'
curl -s 'http://localhost:8765/api/v1/klines/latest?symbol=SOLUSDT&interval=1h&limit=1' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'code={d[\"code\"]}') if d['code']!=0 else print(f'OK: {len(d[\"data\"])}条')"
echo

echo '--- 3. API接口验证(4h最新) ---'
curl -s 'http://localhost:8765/api/v1/klines/latest?symbol=SOLUSDT&interval=4h&limit=1' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'code={d[\"code\"]}') if d['code']!=0 else print(f'OK: {len(d[\"data\"])}条')"
echo

echo '--- 4. API接口验证(1d最新) ---'
curl -s 'http://localhost:8765/api/v1/klines/latest?symbol=SOLUSDT&interval=1d&limit=1' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'code={d[\"code\"]}') if d['code']!=0 else print(f'OK: {len(d[\"data\"])}条')"
echo

echo '--- 5. 采集任务状态 ---'
curl -s http://localhost:8765/api/v1/register/tasks/symbol/SOLUSDT | python3 -c "
import sys,json
d=json.load(sys.stdin)
for t in d['data']['tasks']:
    print(f'{t[\"task_id\"]}: cron={t[\"cron\"]}, next={t[\"next_run_time\"]}')
"
echo

echo '--- 6. 注册状态 ---'
docker exec common_service_postgres psql -U binance -d binance_data -c "SELECT symbol, intervals, status, priority, expires_at FROM registered_symbols ORDER BY symbol;"
echo

echo '========================================'
echo '     验证完成'
echo '========================================'