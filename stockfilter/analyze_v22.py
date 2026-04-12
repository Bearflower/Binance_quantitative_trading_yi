import json
from collections import Counter

with open('backtest_results/backtest_v22_20260410_090401.json', 'r') as f:
    signals = json.load(f)

print(f'总信号数：{len(signals)}')
print()

# 统计退出原因
exit_reasons = Counter([s['exit_reason'] for s in signals])
print('退出原因统计:')
for reason, count in exit_reasons.items():
    print(f'  {reason}: {count} 个')

print()

# 统计持有天数
hold_days_list = [s['hold_days'] for s in signals]
avg_hold_days = sum(hold_days_list) / len(hold_days_list)
print(f'平均持有天数：{avg_hold_days:.1f} 天')

print()

# 查看盈利的信号
profitable = [s for s in signals if s['profit_pct'] > 0]
print(f'盈利信号：{len(profitable)} 个 ({len(profitable)/len(signals)*100:.1f}%)')

# 查看亏损的信号
losing = [s for s in signals if s['profit_pct'] <= 0]
print(f'亏损信号：{len(losing)} 个 ({len(losing)/len(signals)*100:.1f}%)')

print()

# 分析亏损原因
losing_trailing = [s for s in losing if s['exit_reason'] == '移动止盈']
losing_hard = [s for s in losing if s['exit_reason'] == '硬止损']
losing_other = [s for s in losing if s['exit_reason'] not in ['移动止盈', '硬止损']]

print(f'亏损 - 移动止盈：{len(losing_trailing)} 个')
print(f'亏损 - 硬止损：{len(losing_hard)} 个')
print(f'亏损 - 其他：{len(losing_other)} 个')
