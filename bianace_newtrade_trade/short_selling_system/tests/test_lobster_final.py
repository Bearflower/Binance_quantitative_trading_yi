from core.contract_scorer import contract_scorer
from core.technical_analyzer import technical_analyzer
from core.unlock_manager import UnlockDataManager
from core.binance_client import binance_client

symbol = 'LOBSTERUSDT'

print(f"\n{'='*60}")
print(f"检查 {symbol} 的评分")
print(f"{'='*60}\n")

# 1. 检查币种状态
print("1️⃣  检查币种状态...")
info = binance_client.get_exchange_info()
symbols = info.get('symbols', [])

lobster_info = None
for s in symbols:
    if s.get('symbol') == symbol:
        lobster_info = s
        break

if lobster_info:
    print(f"   ✅ {symbol} 是有效的交易对")
    print(f"   状态：{lobster_info.get('status')}")
    onboard = lobster_info.get('onboardDate', 0)
    if onboard:
        from datetime import datetime
        listing_time = datetime.fromtimestamp(onboard / 1000)
        print(f"   上市时间：{listing_time}")
        
        # 计算上线时长
        hours_since_listing = (datetime.now() - listing_time).total_seconds() / 3600
        print(f"   上线时长：{hours_since_listing:.1f} 小时")
        
        # 2. 合约数据评分
        print(f"\n2️⃣  合约数据评分")
        contract_score, contract_reason = contract_scorer.calculate_contract_score(symbol)
        print(f"   评分：{contract_score:.2f}/10.0")
        print(f"   原因：{contract_reason}")
        
        # 3. 技术面评分
        print(f"\n3️⃣  技术面评分")
        technical_score = technical_analyzer.calculate_technical_score(symbol)
        print(f"   评分：{technical_score:.2f}/10.0")
        
        # 4. 基本面评分
        print(f"\n4️⃣  基本面评分")
        unlock_manager = UnlockDataManager(auto_fetch=True)
        if symbol not in unlock_manager.unlock_data:
            print(f"   自动添加 {symbol}...")
            unlock_manager.auto_add_symbol(symbol)
        fundamental_score = unlock_manager.score_fundamental(symbol, days=90)
        print(f"   评分：{fundamental_score:.2f}/10.0")
        
        # 5. 情绪面评分
        print(f"\n5️⃣  情绪面评分")
        try:
            funding_rate = binance_client.get_funding_rate(symbol)
            if funding_rate:
                annual_rate = funding_rate * 3 * 365 * 100
                print(f"   资金费率：{funding_rate:.6f} (年化 {annual_rate:.2f}%)")
                if annual_rate > 100:
                    sentiment_score = 10.0
                elif annual_rate > 50:
                    sentiment_score = 7.0
                elif annual_rate > 20:
                    sentiment_score = 5.0
                else:
                    sentiment_score = 3.0
                print(f"   评分：{sentiment_score:.2f}/10.0")
            else:
                sentiment_score = 5.0
                print(f"   无法获取资金费率，使用默认评分：{sentiment_score:.2f}/10.0")
        except Exception as e:
            sentiment_score = 5.0
            print(f"   获取失败，使用默认评分：{sentiment_score:.2f}/10.0")
        
        # 6. 综合评分
        print(f"\n{'='*60}")
        print("📊 综合评分")
        print(f"{'='*60}")
        weights = {
            'contract': 0.35,
            'fundamental': 0.30,
            'technical': 0.25,
            'sentiment': 0.10
        }
        total_score = (
            contract_score * weights['contract'] +
            fundamental_score * weights['fundamental'] +
            technical_score * weights['technical'] +
            sentiment_score * weights['sentiment']
        )
        print(f"   合约数据：{contract_score:.2f} × {weights['contract']:.2f} = {contract_score * weights['contract']:.2f}")
        print(f"   基本面：{fundamental_score:.2f} × {weights['fundamental']:.2f} = {fundamental_score * weights['fundamental']:.2f}")
        print(f"   技术面：{technical_score:.2f} × {weights['technical']:.2f} = {technical_score * weights['technical']:.2f}")
        print(f"   情绪面：{sentiment_score:.2f} × {weights['sentiment']:.2f} = {sentiment_score * weights['sentiment']:.2f}")
        print(f"\n   综合评分：{total_score:.2f}/10.0")
        print(f"   开仓阈值：7.0")
        if total_score >= 7.0:
            print(f"   ✅ 达到开仓条件！")
        else:
            print(f"   ❌ 未达到开仓条件")
        print(f"{'='*60}\n")
