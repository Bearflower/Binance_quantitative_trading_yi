FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY services/kline_monitor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制监控脚本
COPY services/kline_monitor/src/ ./src/

# 创建数据目录（用于持久化状态文件）
RUN mkdir -p /app/data

# 设置 PYTHONPATH
ENV PYTHONPATH=/app

# 健康检查（检查监控服务自身是否存活）
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import os; os.kill(os.getppid(), 0)" || exit 1

# 启动监控
CMD ["python", "-u", "src/monitor.py"]