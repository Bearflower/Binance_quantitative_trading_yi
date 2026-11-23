FROM ghcr.io/bearflower/trading-hrs@sha256:01ca2e088df0ea3481d318242c385de775fe3ca70fc3c7acf64a195a5fe3c6d9
ARG RELEASE_SHA
ARG CI_RUN_NUMBER
ARG CI_RUN_ATTEMPT
LABEL org.opencontainers.image.revision=$RELEASE_SHA
RUN printf 'GIT_SHA=%s\nDEPLOY_ID=%08X\nCI_RUN_NUMBER=%s\nCI_RUN_ATTEMPT=%s\n' "$RELEASE_SHA" "$(( (CI_RUN_NUMBER << 8) | CI_RUN_ATTEMPT ))" "$CI_RUN_NUMBER" "$CI_RUN_ATTEMPT" > /app/VERSION
COPY strategies/hrs/acceptance_report.py /app/strategies/hrs/acceptance_report.py
RUN python -m py_compile /app/strategies/hrs/acceptance_report.py
