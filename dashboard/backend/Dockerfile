FROM python:3.11-slim

ARG PIP_INDEX_URL=

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制项目依赖
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# 复制整个项目
COPY . .

# 设置工作目录
WORKDIR /app/dashboard/backend

EXPOSE 8767

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8767/api/health || exit 1

# 生产入口用 main_docker：main.py 为遗留演示代码（api/routes.py 为占位会抛 NotImplementedError），uvicorn main:app 会误命中它导致接口 500
# main_docker 注册 routes_docker（生产真实数据服务 DataService），health 接口返回结构符合 HealthResponse 模型
CMD ["python", "-m", "uvicorn", "main_docker:app", "--host", "0.0.0.0", "--port", "8767"]