# Preserve the exact live AI Tuner image, including the independently deployed review fixes.
FROM ghcr.io/bearflower/trading-ai-tuner@sha256:b2539100642e046ec84490636938815d8a57f5d782ac0d25a1d03fca969b064c
COPY --chown=1000:1000 ai_tuner/allocation/activity_evidence.py /app/ai_tuner/allocation/activity_evidence.py
COPY --chown=1000:1000 ai_tuner/monitor/evidence_jobs.py ai_tuner/monitor/reduced_trial.py ai_tuner/monitor/trial_risk_evidence.py /app/ai_tuner/monitor/
COPY --chown=1000:1000 shared/accounting/pm_equity_bridge.py /app/shared/accounting/pm_equity_bridge.py
COPY --chown=1000:1000 VERSION /app/VERSION
