FROM python:3.11-slim

WORKDIR /app

# 安装编译工具（pandas/numpy 需要）
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY strategies/hrs/requirements.txt /app/strategies/hrs/requirements.txt
RUN pip install --no-cache-dir -r /app/strategies/hrs/requirements.txt

# 复制项目代码
COPY . /app/

# 设置环境变量
ENV PYTHONPATH=/app

# 运行策略
CMD ["python", "-m", "strategies.hrs.main"]