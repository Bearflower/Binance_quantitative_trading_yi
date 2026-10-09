# grid V2.5.4 aggTrades 常驻落库器（轨道B，计划 §0.1 B8 部署件）
# 仅采集公共行情并落本地 SQLite，不调用任何交易接口
FROM python:3.11-slim

ARG PIP_INDEX_URL=

WORKDIR /app

# 先装依赖（最稳定层，缓存命中率最高）
COPY services/aggtrade_collector/requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# 再复制落库器源码（含包内 config.yaml 与 schema.sql）
COPY services/aggtrade_collector/ /app/services/aggtrade_collector/

# 版本标记（部署五层验证需容器内 /app/VERSION 核对 DEPLOY_ID / GIT_SHA）
COPY VERSION /app/VERSION

# 基准目录：配置内相对路径（data/aggtrades/...）解析到 /app/data，由 compose 命名卷持久化
ENV PYTHONPATH=/app
ENV AGGTRADE_BASE_DIR=/app
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

# 常驻落库：启动即引导回溯 + 增量补齐 + 按周期滚动清理
CMD ["python3", "-m", "services.aggtrade_collector.cli", "serve"]

# 健康检查（与库内其余常驻策略容器一致）
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"