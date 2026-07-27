FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/btc_eth/ /app/strategies/btc_eth/

# 复制版本标记文件
COPY VERSION /app/VERSION

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 设置环境变量
ENV STRATEGY_NAME=btc_eth
ENV PYTHONUNBUFFERED=1

# 运行策略
CMD ["python", "-m", "strategies.btc_eth.main"]
