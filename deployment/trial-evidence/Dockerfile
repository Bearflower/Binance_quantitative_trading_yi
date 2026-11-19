FROM ghcr.io/bearflower/trading-ai-tuner@sha256:26b7197d9464e7d0e3778ff24fc61d3c62745cfef997c95a096c11a806bce38f
COPY --chown=1000:1000 ai_tuner/allocation/activity_evidence.py /app/ai_tuner/allocation/
COPY --chown=1000:1000 ai_tuner/monitor/hrs_closed_lot.py ai_tuner/monitor/protective_exit_evidence.py ai_tuner/monitor/reduced_trial.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 VERSION /app/VERSION
