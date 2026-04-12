FROM python:3.9-slim

WORKDIR /app

# 安装系统依赖和时区数据
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 设置时区为 Asia/Shanghai（北京时间）
ENV TZ=Asia/Shanghai
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    dpkg-reconfigure -f noninteractive tzdata

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 创建日志和数据目录，并设置权限
RUN mkdir -p logs data output && chmod -R 777 logs data output

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV PYTHONPATH=/app

# 以 root 用户运行（避免挂载卷权限问题）
# USER appuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# 默认命令：运行扫描
CMD ["python", "main.py", "--scan"]
