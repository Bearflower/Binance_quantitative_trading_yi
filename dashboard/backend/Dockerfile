FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制项目依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制整个项目
COPY . .

# 设置工作目录
WORKDIR /app/dashboard/backend

EXPOSE 8767

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8767/api/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8767"]