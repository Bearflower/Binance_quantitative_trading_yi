FROM ghcr.io/bearflower/trading-ai-tuner@sha256:3b6b1a6b07724c9b65ad0983f32c84fbde285ee6f5dd267bcca489f8b930aad0
COPY --chown=1000:1000 ai_tuner/monitor/reduced_trial.py ai_tuner/monitor/um_margin_evidence.py ai_tuner/monitor/protective_exit_evidence.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 ai_tuner/allocation/activity_evidence.py /app/ai_tuner/allocation/
COPY --chown=1000:1000 VERSION /app/VERSION
