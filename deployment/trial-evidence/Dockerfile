# Reviewed successor preserves all installed runtime dependencies and permissions.
FROM ghcr.io/bearflower/trading-ai-tuner@sha256:e691e575b0fd9272ffa3ff691404d905fcd5e4d8d9d0929d5bf317fe3ee3cd7c
COPY --chown=1000:1000 ai_tuner/ /app/ai_tuner/
COPY --chown=1000:1000 shared/ /app/shared/
COPY --chown=1000:1000 VERSION /app/VERSION
