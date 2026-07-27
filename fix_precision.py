with open("/root/trading_system/strategies/btc_eth/strategy.py", "r") as f:
    content = f.read()

# 替换止损和止盈的 stopPrice，添加精度处理
old_sl = "stopPrice=str(float(signal['initial_stop_loss'])),"
new_sl = "stopPrice=str(round(float(signal['initial_stop_loss']), 2)),"

old_tp = "stopPrice=str(float(signal['tp1_price'])),"
new_tp = "stopPrice=str(round(float(signal['tp1_price']), 2)),"

content = content.replace(old_sl, new_sl)
content = content.replace(old_tp, new_tp)

print(f"SL fixed: {old_sl in content} -> {new_sl in content}")
print(f"TP fixed: {old_tp in content} -> {new_tp in content}")

with open("/root/trading_system/strategies/btc_eth/strategy.py", "w") as f:
    f.write(content)
print("Done")