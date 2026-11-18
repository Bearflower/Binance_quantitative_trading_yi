FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ /app/shared/
COPY strategies/btc_eth_aggressive/ /app/strategies/btc_eth_aggressive/
COPY VERSION /app/VERSION
ENV STRATEGY_NAME=btc_eth_aggressive PYTHONUNBUFFERED=1
CMD ["python", "-m", "strategies.btc_eth_aggressive.main"]
