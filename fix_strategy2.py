with open("/root/trading_system/strategies/btc_eth/strategy.py", "r") as f:
    content = f.read()

# 修复被sed破坏的 close_quantity=position.current_quantity,
# sed 把 "quantity=position.current_quantity," 从 "close_quantity=position.current_quantity," 中也删掉了
content = content.replace(
    "                    close_\n                    close_reason=\"CHANDLIER\",",
    "                    close_quantity=position.current_quantity,\n                    close_reason=\"CHANDLIER\","
)

with open("/root/trading_system/strategies/btc_eth/strategy.py", "w") as f:
    f.write(content)

print("Fixes applied")

# Check remaining issues
import re
with open("/root/trading_system/strategies/btc_eth/strategy.py", "r") as f:
    for i, line in enumerate(f, 1):
        if line.strip() == "close_":
            print(f"WARNING line {i}: still has 'close_'")