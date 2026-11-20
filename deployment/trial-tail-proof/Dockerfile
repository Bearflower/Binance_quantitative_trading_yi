FROM ghcr.io/bearflower/trading-ai-tuner@sha256:88d9f9d35075e86a9eea1200717f5a2baf89a58e0b59f686e0b56ba7a49360ee
COPY --chown=1000:1000 ai_tuner/monitor/evidence_jobs.py ai_tuner/monitor/hrs_closed_lot.py ai_tuner/monitor/protective_exit_evidence.py ai_tuner/monitor/ordinary_execution_evidence.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 shared/new_coin_recovery_evidence.py /app/shared/
COPY --chown=1000:1000 VERSION /app/VERSION
