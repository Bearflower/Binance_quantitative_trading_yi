FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY kline_data_service/requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt databases

# 复制共享模块
COPY src/shared/ ./src/shared/

# 复制 K 线数据服务源代码
COPY kline_data_service/ ./kline_data_service/

# 设置 PYTHONPATH
ENV PYTHONPATH=/app/src:/app/kline_data_service

# 创建日志目录
RUN mkdir -p /app/logs

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/api/v1/health', timeout=5)" || exit 1

# 启动命令
CMD ["uvicorn", "kline_data_service.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
