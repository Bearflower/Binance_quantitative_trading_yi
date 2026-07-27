with open("/root/trading_system/strategies/btc_eth/strategy.py", "r") as f:
    content = f.read()

# 替换止损和止盈的 stopPrice
old_sl = "stopPrice=str(float(signal['initial_stop_loss'])),"
new_sl = "stopPrice=f\"{float(signal['initial_stop_loss']):.2f}\","

old_tp = "stopPrice=str(float(signal['tp1_price'])),"
new_tp = "stopPrice=f\"{float(signal['tp1_price']):.2f}\","

count_sl = content.count(old_sl)
count_tp = content.count(old_tp)

content = content.replace(old_sl, new_sl)
content = content.replace(old_tp, new_tp)

print(f"SL replaced: {count_sl} occurrences")
print(f"TP replaced: {count_tp} occurrences")

with open("/root/trading_system/strategies/btc_eth/strategy.py", "w") as f:
    f.write(content)
print("Done")