FROM ghcr.io/bearflower/trading-ai-tuner@sha256:dc7002b3a2db84a90cb9efe23d71e58df2874d9e8ddbea5ae9addeb5915b804f
COPY --chown=1000:1000 ai_tuner/allocation/activity_evidence.py /app/ai_tuner/allocation/
COPY --chown=1000:1000 ai_tuner/monitor/ordinary_execution_evidence.py ai_tuner/monitor/trial_risk_evidence.py ai_tuner/monitor/reduced_trial.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 VERSION /app/VERSION
