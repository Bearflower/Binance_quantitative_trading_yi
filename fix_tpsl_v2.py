with open("/root/trading_system/strategies/btc_eth/strategy.py", "r") as f:
    content = f.read()

# 替换止损和止盈单：用 quantity 替代 closePosition
old_sl_block = """            try:
                sl_order = await self.binance.place_order(
                    symbol=signal['symbol'],
                    side=close_side,
                    
                    order_type="STOP_MARKET",
                    stopPrice=f"{float(signal['initial_stop_loss']):.2f}",
                    closePosition="true"
                )"""

new_sl_block = """            try:
                sl_order = await self.binance.place_order(
                    symbol=signal['symbol'],
                    side=close_side,
                    quantity=position.current_quantity,
                    order_type="STOP_MARKET",
                    stopPrice=f"{float(signal['initial_stop_loss']):.2f}"
                )"""

old_tp_block = """            try:
                tp_order = await self.binance.place_order(
                    symbol=signal['symbol'],
                    side=close_side,
                    
                    order_type="TAKE_PROFIT_MARKET",
                    stopPrice=f"{float(signal['tp1_price']):.2f}",
                    closePosition="true"
                )"""

new_tp_block = """            try:
                tp_order = await self.binance.place_order(
                    symbol=signal['symbol'],
                    side=close_side,
                    quantity=position.current_quantity,
                    order_type="TAKE_PROFIT_MARKET",
                    stopPrice=f"{float(signal['tp1_price']):.2f}"
                )"""

content = content.replace(old_sl_block, new_sl_block)
content = content.replace(old_tp_block, new_tp_block)

with open("/root/trading_system/strategies/btc_eth/strategy.py", "w") as f:
    f.write(content)

print("Replaced TP/SL to use quantity instead of closePosition")