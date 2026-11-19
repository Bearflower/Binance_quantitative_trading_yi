FROM ghcr.io/bearflower/trading-hrs@sha256:2cf4f020332b0df61656a3d926302893e2dba6cb617cdf2fc1deac9cc4d51433 AS hrs
ARG RELEASE_SHA
ARG CI_RUN_NUMBER
ARG CI_RUN_ATTEMPT
LABEL org.opencontainers.image.revision=$RELEASE_SHA
ENV HRS_RELEASE_SHA=$RELEASE_SHA
RUN test -n "$CI_RUN_NUMBER" && test -n "$CI_RUN_ATTEMPT" && \
    printf 'GIT_SHA=%s\nDEPLOY_ID=%08X\nCI_RUN_NUMBER=%s\nCI_RUN_ATTEMPT=%s\n' "$RELEASE_SHA" "$(( (CI_RUN_NUMBER << 8) | CI_RUN_ATTEMPT ))" "$CI_RUN_NUMBER" "$CI_RUN_ATTEMPT" > /app/VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY strategies/hrs/ /app/strategies/hrs/
COPY shared/ /app/shared/
COPY ai_tuner/ /app/ai_tuner/
COPY scripts/collect_execution_facts.py scripts/export_execution_facts.py /app/scripts/
FROM ghcr.io/bearflower/trading-hrs@sha256:4fa94f38ebcedcaefc79a05520211130cfe3c47be9169be4822f69fd90bfa053 AS hub
ARG RELEASE_SHA
ARG CI_RUN_NUMBER
ARG CI_RUN_ATTEMPT
LABEL org.opencontainers.image.revision=$RELEASE_SHA
RUN test -n "$CI_RUN_NUMBER" && test -n "$CI_RUN_ATTEMPT" && \
    printf 'GIT_SHA=%s\nDEPLOY_ID=%08X\nCI_RUN_NUMBER=%s\nCI_RUN_ATTEMPT=%s\n' "$RELEASE_SHA" "$(( (CI_RUN_NUMBER << 8) | CI_RUN_ATTEMPT ))" "$CI_RUN_NUMBER" "$CI_RUN_ATTEMPT" > /app/VERSION
COPY services/market_cap/ /app/services/market_cap/
FROM hrs AS test
# Base images may retain tests removed from the current tracked checkout.
RUN rm -rf /app/tests /app/strategies/hrs/tests
COPY strategies/hrs/tests/ /app/strategies/hrs/tests/
RUN pip install --no-cache-dir pytest pytest-asyncio APScheduler==3.10.4
COPY services/market_cap/ /app/services/market_cap/
COPY tests/ /app/tests/
COPY release/hrs-execution/deploy_optimization.py /app/release/hrs-execution/deploy_optimization.py
COPY release/hrs-execution/activate_minimum_budget.py /app/release/hrs-execution/activate_minimum_budget.py
COPY database/postgres/migrations/ /app/database/postgres/migrations/
COPY database/postgres/init-scripts/ /app/database/postgres/init-scripts/
COPY .gitignore /app/.gitignore
RUN git init /app
COPY pytest.ini /app/pytest.ini
