FROM python:3.11-slim

ARG PIP_INDEX_URL=

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先 COPY requirements.txt 并 pip install — 这层最稳定，缓存命中率最高
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# 再 COPY 业务代码 — 代码改动只导致下面层失效
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/btc_eth/ /app/strategies/btc_eth/

# 复制版本标记文件
COPY VERSION /app/VERSION

# 设置环境变量
ENV STRATEGY_NAME=btc_eth
ENV PYTHONUNBUFFERED=1

# 运行策略
CMD ["python", "-m", "strategies.btc_eth.main"]
