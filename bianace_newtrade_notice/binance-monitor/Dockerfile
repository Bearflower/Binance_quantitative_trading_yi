# 使用Ubuntu 22.04作为基础镜像
FROM ubuntu:22.04

# 设置环境变量
ENV DEBIAN_FRONTEND=noninteractive

# 更新系统并安装必要的依赖
RUN apt-get update &&     apt-get install -y python3 python3-pip python3-venv &&     apt-get clean &&     rm -rf /var/lib/apt/lists/*

# 创建工作目录
WORKDIR /app

# 复制项目文件
COPY sd.py .

# 安装Python依赖
RUN pip3 install --no-cache-dir requests

# 创建logs目录
RUN mkdir -p logs

# 设置环境变量，用于配置飞书webhook
ENV FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/9804fe72-cd95-48a4-9fc2-368907210451

# 运行监控程序
CMD ["python3", "sd.py"]
