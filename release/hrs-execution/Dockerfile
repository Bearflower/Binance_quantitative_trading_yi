FROM ghcr.io/bearflower/trading-hrs@sha256:a064155bdb85b273ad0417b7ae26d7d73742b4a854be9348161f7e4b167cfa41 AS runtime
COPY strategies/hrs/executor.py strategies/hrs/strategy.py strategies/hrs/timeout_recovery.py /app/strategies/hrs/
FROM runtime AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
COPY strategies/hrs/tests /app/strategies/hrs/tests
COPY pytest.ini /app/pytest.ini
