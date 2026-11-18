# Preserve the exact live AI Tuner image, including the independently deployed review fixes.
FROM ghcr.io/bearflower/trading-ai-tuner@sha256:97cb758159ae41089a5e133a5d2cbf8686d3ca9a66e5db9f88ee63494735f5ff
COPY --chown=1000:1000 ai_tuner/allocation/activity_evidence.py /app/ai_tuner/allocation/activity_evidence.py
COPY --chown=1000:1000 ai_tuner/monitor/evidence_jobs.py ai_tuner/monitor/reduced_trial.py ai_tuner/monitor/trial_risk_evidence.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 VERSION /app/VERSION
