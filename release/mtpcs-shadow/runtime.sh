# Shared Docker runtime spec. The caller provides the frozen bundle, ledger directory and probe.
network=trading-network-v2
url=http://trading_system-kline:8000/api/v1

common=(--network "$network" --user 65534:65534 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 128
        --cpus 0.5 --memory 512m --env PYTHONDONTWRITEBYTECODE=1 --env OPENBLAS_NUM_THREADS=1
        --env OMP_NUM_THREADS=1
        --mount "type=bind,src=$bundle/entry_study.py,dst=/app/strategies/btc_eth/shadow/entry_study.py,readonly"
        --mount "type=bind,src=$bundle/entry_ledger.py,dst=/app/strategies/btc_eth/shadow/entry_ledger.py,readonly"
        --mount "type=bind,src=$bundle/btc_eth_aggressive,dst=/app/strategies/btc_eth_aggressive,readonly"
        --mount "type=bind,src=$bundle/shared,dst=/app/shared,readonly"
        --mount "type=bind,src=$bundle,dst=/research,readonly"
        --mount "type=bind,src=$data,dst=/ledger"
        --mount "type=bind,src=$probe,dst=/opt/mtpcs-shadow-health.py,readonly")
command=(python -m strategies.btc_eth.shadow.entry_ledger --kline-url "$url"
         --original-config /research/btc_eth.json --aggressive-config /research/btc_eth_aggressive.json)
health=(
  --health-cmd 'python /opt/mtpcs-shadow-health.py /ledger/entries.sqlite3'
  --health-interval 30s --health-timeout 5s --health-retries 3 --health-start-period 30s
)
