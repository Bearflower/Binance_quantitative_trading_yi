FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY services/kline_service/requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt databases

# 复制共享模块（K 线服务自有共享模块，而非项目根目录 shared）
COPY services/kline_service/shared/ ./shared/

# 复制 K 线数据服务源代码
COPY services/kline_service/core/ ./core/
COPY services/kline_service/api/ ./api/
COPY services/kline_service/models/ ./models/
COPY services/kline_service/db/ ./db/
COPY services/kline_service/scripts/ ./scripts/
COPY services/kline_service/src/ ./src/

# 设置 PYTHONPATH
ENV PYTHONPATH=/app:/app/src

# 创建日志目录
RUN mkdir -p /app/logs

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/api/v1/health', timeout=5)" || exit 1

# 启动命令
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]