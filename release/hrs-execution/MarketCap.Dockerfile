FROM ghcr.io/bearflower/trading-hrs@sha256:c63f36faef67ab3a9d685a1126ef46bb7de985586021d0467284f8f690ba9c13 AS hrs
COPY strategies/hrs/main.py strategies/hrs/strategy.py strategies/hrs/market_data.py strategies/hrs/candidate_pool.py /app/strategies/hrs/
COPY shared/condition_orders.py shared/config_loader.py shared/trade_logger.py shared/market_cap_client.py /app/shared/
FROM ghcr.io/bearflower/trading-ai-tuner@sha256:ec831eabd3d386c4121278753e77c8edfdd7ce52080ef40735f1d0a2a795a2b9 AS snapshot
COPY shared/market_cap_client.py /app/shared/
COPY ai_tuner/snapshots/ /app/ai_tuner/snapshots/
FROM ghcr.io/bearflower/trading-hrs@sha256:c63f36faef67ab3a9d685a1126ef46bb7de985586021d0467284f8f690ba9c13 AS hub
COPY services/market_cap/ /app/services/market_cap/
CMD ["python", "-m", "services.market_cap.main"]
FROM hrs AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
COPY services/market_cap/ /app/services/market_cap/
COPY ai_tuner/snapshots/ /app/ai_tuner/snapshots/
COPY strategies/hrs/tests/ /app/strategies/hrs/tests/
COPY tests/test_shared/test_market_cap_hub.py /app/tests/test_shared/test_market_cap_hub.py
COPY tests/test_ai_tuner_mvp/ /app/tests/test_ai_tuner_mvp/
COPY pytest.ini /app/pytest.ini
