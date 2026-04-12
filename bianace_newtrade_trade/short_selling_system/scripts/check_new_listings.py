from core.listing_detector import listing_detector
from datetime import datetime

# 检测最近 48 小时的币种
new_listings = listing_detector.detect_new_listings(hours=48)

print(f'检测到 {len(new_listings)} 个新上市合约:')
for listing in new_listings:
    symbol = listing['symbol']
    hours = listing['hours_since_listing']
    is_rescore = listing.get('is_rescore', False)
    print(f"- {symbol}: 上线{hours:.1f}小时，二次评分：{is_rescore}")
