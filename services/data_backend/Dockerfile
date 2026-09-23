FROM python:3.11-slim

ARG PIP_INDEX_URL=

WORKDIR /app

# 安装系统依赖（asyncpg 需要编译；tzdata 提供 zoneinfo 时区数据，确保北京时间调度生效）
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制项目依赖并安装（根 requirements.txt 含 APScheduler/structlog/asyncpg 等）
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# 复制数据后台所需模块（仅业务必需，不含前端/API 层）
COPY shared/ ./shared/
COPY dashboard/backend/core/ ./dashboard/backend/core/
COPY dashboard/backend/config/ ./dashboard/backend/config/
COPY dashboard/backend/services/ ./dashboard/backend/services/
COPY services/data_backend/src/ ./src/

# 版本标记文件（部署防幻觉验证用）
COPY VERSION ./VERSION

ENV APP_ROOT=/app

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

CMD ["python", "src/main.py"]
