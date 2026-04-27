from core.technical_analyzer import technical_analyzer

symbol = 'COPPERUSDT'

print(f"\n测试 {symbol} 的技术面分析...\n")

# 1. 获取 K 线数据
print("1️⃣  获取 K 线数据...")
klines = technical_analyzer.get_klines(symbol, interval='4h', limit=200)
if klines:
    print(f"   ✅ 成功获取 {len(klines)} 条 K 线数据")
    print(f"   当前价格：{klines[-1]['close']}")
else:
    print(f"   ❌ 获取失败")
    exit(1)

# 2. 计算 EMA
print("\n2️⃣  计算 EMA...")
ema21 = technical_analyzer.calculate_ema(klines, 21)
ema50 = technical_analyzer.calculate_ema(klines, 50)
ema200 = technical_analyzer.calculate_ema(klines, 200)
print(f"   EMA21: {ema21}")
print(f"   EMA50: {ema50}")
print(f"   EMA200: {ema200}")

# 3. 计算 RSI
print("\n3️⃣  计算 RSI...")
rsi = technical_analyzer.calculate_rsi(klines, 14)
print(f"   RSI(14): {rsi}")

# 4. 计算 ATR
print("\n4️⃣  计算 ATR...")
atr = technical_analyzer.calculate_atr(klines, 14)
print(f"   ATR(14): {atr}")
if atr:
    atr_ratio = atr / klines[-1]['close']
    print(f"   ATR/价格比率：{atr_ratio:.4f}")

# 5. 分析趋势
print("\n5️⃣  分析趋势...")
trend = technical_analyzer.analyze_trend(klines)
print(f"   趋势：{trend}")

# 6. 计算技术面评分
print("\n6️⃣  计算技术面评分...")
score = technical_analyzer.calculate_technical_score(symbol)
print(f"   技术面评分：{score:.2f}/10.0")
