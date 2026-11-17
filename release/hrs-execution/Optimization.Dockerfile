FROM ghcr.io/bearflower/trading-hrs@sha256:2cf4f020332b0df61656a3d926302893e2dba6cb617cdf2fc1deac9cc4d51433 AS hrs
ARG RELEASE_SHA
LABEL org.opencontainers.image.revision=$RELEASE_SHA
ENV HRS_RELEASE_SHA=$RELEASE_SHA
COPY strategies/hrs/ /app/strategies/hrs/
COPY shared/ /app/shared/
COPY ai_tuner/ /app/ai_tuner/
COPY scripts/collect_execution_facts.py scripts/export_execution_facts.py /app/scripts/
FROM ghcr.io/bearflower/trading-hrs@sha256:4fa94f38ebcedcaefc79a05520211130cfe3c47be9169be4822f69fd90bfa053 AS hub
ARG RELEASE_SHA
LABEL org.opencontainers.image.revision=$RELEASE_SHA
COPY services/market_cap/ /app/services/market_cap/
FROM hrs AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
COPY tests/ /app/tests/
COPY database/postgres/migrations/ /app/database/postgres/migrations/
COPY pytest.ini /app/pytest.ini
