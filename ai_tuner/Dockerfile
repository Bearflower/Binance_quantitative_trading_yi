# StratTuneAI 多策略AI调优系统 Dockerfile
FROM python:3.10-slim

# 时区与 Python 环境变量
ENV TZ=Asia/Shanghai \
    PYTHONPATH="/app:/app/ai_tuner" \
    PYTHONUNBUFFERED=1

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 安装系统依赖（asyncpg 编译需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY ai_tuner/requirements.txt /app/ai_tuner/
RUN pip install --no-cache-dir -r /app/ai_tuner/requirements.txt

# 复制应用代码
COPY ai_tuner/ /app/ai_tuner/
COPY shared/ /app/shared/

# 复制 VERSION 文件（部署时生成，用于版本验证）
COPY VERSION /app/VERSION

# 复制策略配置文件（适配器运行需要读取配置）
COPY strategies/btc_eth/config.yaml /app/strategies/btc_eth/config.yaml
COPY strategies/new_coin/config.yaml /app/strategies/new_coin/config.yaml
COPY strategies/grid/config.yaml /app/strategies/grid/config.yaml
COPY strategies/hrs/config.yaml /app/strategies/hrs/config.yaml

# 创建日志目录
RUN mkdir -p /app/logs

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8777/api/v1/health')" || exit 1

# 切换到非 root 用户
RUN useradd -m -u 1000 tuner && chown -R tuner:tuner /app
USER 1000:1000

# 启动命令
CMD ["python", "/app/ai_tuner/main.py"]