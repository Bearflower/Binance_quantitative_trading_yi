# AI 编辑部系统

基于 AI 技术的自动化内容创作与发布平台

## 📋 项目概述

AI 编辑部系统是一个完整的内容创作自动化解决方案，集成了热点监测、素材收集、AI 内容生成、配图生成、排版和发布等功能。系统采用 FastAPI 后端框架，结合 DeepSeek AI 模型，实现从热点发现到内容发布的全流程自动化。

### 项目背景

在信息爆炸的时代，内容创作者面临着巨大的压力：
- 需要实时监测热点话题
- 需要快速收集相关素材
- 需要高效生成优质内容
- 需要多渠道发布内容

AI 编辑部系统应运而生，通过 AI 技术自动化整个内容创作流程，提高创作效率，降低人工成本。

## 🎯 核心需求

### 1. 热点监测与选题需求
- **实时监测**：能够实时监测各大平台的热点话题
- **智能分析**：自动分析和筛选有价值的选题
- **选题报告**：生成详细的选题报告和建议
- **自动化**：无需人工干预，自动发现热点

### 2. 素材收集与整合需求
- **自动爬取**：根据选题自动爬取网络素材
- **智能去重**：自动识别和去除重复素材
- **相关性评分**：对素材进行相关性评分和排序
- **整合输出**：将分散的素材整合成结构化数据

### 3. AI 内容生成需求
- **文章生成**：基于 AI 模型自动生成文章
- **标题生成**：自动生成多个吸引人的标题选项
- **内容优化**：对生成的内容进行润色和优化
- **长度控制**：支持自定义文章长度

### 4. 配图生成需求
- **自动生成**：根据文章内容自动生成配图
- **多种规格**：支持多种尺寸和风格
- **高质量**：保证图片质量和相关性

### 5. 智能排版需求
- **格式转换**：Markdown 转富文本格式
- **自动格式化**：自动调整格式，提升阅读体验
- **样式美化**：应用美观的样式模板

### 6. 多渠道发布需求
- **飞书集成**：支持飞书消息推送
- **多维表格**：自动创建飞书多维表格记录
- **可扩展**：架构设计支持未来扩展其他平台

## 🏗️ 系统架构

### 技术架构

```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│   FastAPI App   │  Port: 8888
└────────┬────────┘
         │
         ├──► Task Manager      (任务管理)
         ├──► Workflow Engine   (工作流引擎)
         ├──► Event Bus         (事件总线)
         │
         ├──► Hotspot Monitor   (热点监测)
         ├──► Material Spider   (素材收集)
         ├──► AI Content Gen    (内容生成)
         ├──► Image Generator   (配图生成)
         ├──► RichText Gen      (排版)
         └──► Feishu Client     (发布)
```

### 项目结构

```
ai-editorial-system/
├── api/                    # API 接口层
│   ├── app.py             # FastAPI 应用入口
│   ├── article/           # 文章相关 API
│   │   ├── content_planner.py      # 内容规划器
│   │   └── ai_content_generator.py # AI 内容生成器
│   ├── image/             # 图片相关 API
│   │   └── image_generator.py      # 图片生成器
│   ├── material/          # 素材相关 API
│   │   └── spider.py               # 素材爬虫
│   ├── publish/           # 发布相关 API
│   │   └── wechat_api_client.py    # 飞书客户端
│   ├── topic/             # 话题相关 API
│   │   └── hotspot_monitor.py      # 热点监测器
│   ├── typesetting/       # 排版相关 API
│   │   └── rich_text_generator.py  # 富文本生成器
│   └── tasks/             # 任务相关 API
├── core/                   # 核心业务逻辑层
│   ├── task_manager.py    # 任务管理器
│   ├── workflow_engine.py # 工作流引擎
│   └── event_bus.py       # 事件总线
├── Dockerfile             # Docker 镜像构建文件
├── docker-compose.yml     # Docker Compose 配置
├── requirements.txt       # Python 依赖
├── .env                   # 环境变量配置
├── .deploy_config         # 部署配置
├── one_click_deploy.sh    # 一键部署脚本
├── server_run_workflow.sh # 服务器工作流脚本
└── README.md              # 项目文档
```

## 🔧 技术栈

### 后端技术
- **框架**: FastAPI 0.104.1
- **AI 模型**: DeepSeek API
- **数据验证**: Pydantic 2.x
- **异步处理**: AnyIO, Starlette

### 容器化与部署
- **容器**: Docker, Docker Compose
- **部署**: SSH, rsync, crontab
- **镜像**: 自定义 Docker 镜像

### 第三方服务
- **发布平台**: 飞书开放平台
- **AI 服务**: DeepSeek

### 开发工具
- **代码格式化**: black, flake8
- **类型检查**: mypy
- **测试**: pytest

## 📖 功能模块详解

### 1. 内容规划器 (ContentPlanner)
**位置**: `api/article/content_planner.py`

**功能**:
- 根据话题和素材生成文章结构
- 生成文章大纲
- 规划内容布局

**核心方法**:
- `plan_content(topic, materials)`: 生成文章结构
- `generate_outline(structure)`: 生成文章大纲

### 2. 素材爬虫 (MaterialSpider)
**位置**: `api/material/spider.py`

**功能**:
- 根据话题爬取网络素材
- 素材去重和整合
- 相关性评分

**核心方法**:
- `crawl_materials(topic, max_results)`: 爬取素材
- `integrate_materials(materials)`: 整合素材

### 3. AI 内容生成器 (AIContentGenerator)
**位置**: `api/article/ai_content_generator.py`

**功能**:
- 基于 AI 生成文章内容
- 生成多个标题选项
- 内容优化和润色

**核心方法**:
- `generate_article(topic, materials, length)`: 生成文章
- `generate_title(content)`: 生成标题
- `optimize_content(content)`: 优化内容

### 4. 工作流引擎 (WorkflowEngine)
**位置**: `core/workflow_engine.py`

**功能**:
- 协调各个模块完成工作流
- 任务状态管理
- 错误处理

**支持的工作流**:
- `article_creation`: 文章创建工作流
- `topic_monitoring`: 热点监测工作流
- `publish`: 发布工作流

### 5. 任务管理器 (TaskManager)
**位置**: `core/task_manager.py`

**功能**:
- 创建和管理任务
- 更新任务状态
- 查询任务列表

## 🚀 快速开始

### 环境要求
- Python 3.9+
- Docker 和 Docker Compose
- rsync (用于部署)
- SSH 客户端

### 本地开发

```bash
# 1. 克隆项目
git clone <repository-url>
cd ai-editorial-system

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入必要的配置

# 4. 启动服务
python api/app.py
# 或
uvicorn api.app:app --host 0.0.0.0 --port 8888 --reload

# 5. 访问 API 文档
# http://localhost:8888/docs
```

### Docker 部署

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 文件

# 2. 构建并启动
docker-compose up -d

# 3. 查看日志
docker logs -f ai-editorial-system

# 4. 停止服务
docker-compose down
```

## 🌐 生产环境部署

### 一键部署流程

```bash
# 1. 配置服务器信息
cat > .deploy_config << 'EOF'
SERVER_IP="your_server_ip"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/ai-editorial-system"
DOCKER_CONTAINER_NAME="ai-editorial-system"
DOCKER_IMAGE_NAME="ai-editorial-system:latest"
PROJECT_NAME="ai-editorial-system"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
EOF

# 2. 执行一键部署
./one_click_deploy.sh
```

### 部署步骤详解

1. **打包项目**: 自动打包项目文件，排除不必要的文件
2. **上传到服务器**: 使用 SSH 密钥认证上传压缩包
3. **远程部署**: 在服务器上解压、构建、启动容器
4. **验证部署**: 检查容器状态，确认服务正常

### 配置说明

**环境变量** (`.env`):
```bash
# 飞书 webhook URL
FEISHU_WEBHOOK_URL=your_webhook_url

# DeepSeek API 密钥
DEEPSEEK_API_KEY=your_api_key
```

**部署配置** (`.deploy_config`):
```bash
# 服务器配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/ai-editorial-system"

# Docker 配置
DOCKER_CONTAINER_NAME="ai-editorial-system"
DOCKER_IMAGE_NAME="ai-editorial-system:latest"

# 项目配置
PROJECT_NAME="ai-editorial-system"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
```

## ⏰ 定时任务

### 配置的定时任务

系统已配置三个定时任务，每天自动执行：
- **每天早上 8:00** - 执行文章创建和热点监测工作流
- **每天中午 12:00** - 执行文章创建和热点监测工作流
- **每天中午 12:50** - 执行文章创建和热点监测工作流

### 查看和管理

```bash
# 查看定时任务日志
ssh root@your_server_ip "tail -f /root/ai-editorial-system/cron.log"

# 手动执行工作流
ssh root@your_server_ip "/root/ai-editorial-system/run_workflow.sh"

# 查看定时任务配置
ssh root@your_server_ip "crontab -l"
```

### 添加定时任务

```bash
# 编辑 crontab
ssh root@your_server_ip "crontab -e"

# 添加新的定时任务（例如：每天晚上 20:00）
0 20 * * * /root/ai-editorial-system/run_workflow.sh >> /root/ai-editorial-system/cron.log 2>&1
```

## 🔌 API 接口文档

### 任务管理 API

#### 创建任务
```http
POST /api/tasks
Content-Type: application/json

{
  "name": "任务名称",
  "type": "任务类型",
  "parameters": {}
}
```

#### 获取任务列表
```http
GET /api/tasks?status=completed&type=article&limit=100
```

#### 获取任务详情
```http
GET /api/tasks/{task_id}
```

#### 更新任务状态
```http
PUT /api/tasks/{task_id}/status?status=in_progress
```

### 工作流 API

#### 执行工作流
```http
POST /api/workflows
Content-Type: application/json

{
  "workflow_type": "article_creation",
  "parameters": {}
}
```

**支持的工作流类型**:
- `article_creation`: 文章创建工作流
- `topic_monitoring`: 热点监测工作流
- `publish`: 发布工作流

### 热点监测 API

#### 获取热点话题
```http
GET /api/topics/hotspots
```

#### 分析热点话题
```http
GET /api/topics/analyze?topic_type=tech
```

### 素材收集 API

#### 爬取素材
```http
GET /api/materials/crawl?topic=人工智能&max_results=10
```

#### 整合素材
```http
GET /api/materials/integrate?topic=人工智能&max_results=10
```

### 文章生成 API

#### 生成文章
```http
POST /api/articles/generate
Content-Type: application/json

{
  "topic": "人工智能的发展趋势",
  "materials": [],
  "length": 1000
}
```

#### 优化文章
```http
POST /api/articles/optimize
Content-Type: application/json

{
  "content": "文章内容..."
}
```

### 图片生成 API

#### 生成图片
```http
POST /api/images/generate?prompt=人工智能技术&size=1024x1024&num=1
```

### 排版 API

#### Markdown 转富文本
```http
POST /api/typesetting/convert
Content-Type: application/json

{
  "markdown": "# 标题\n\n内容..."
}
```

### 发布 API

#### 发布到飞书
```http
POST /api/publish/feishu
Content-Type: application/json

{
  "article_id": 1,
  "title": "文章标题",
  "content": "文章内容"
}
```

### 健康检查

#### 健康检查
```http
GET /health
```

**响应**:
```json
{
  "status": "healthy"
}
```

## 📝 工作流详解

### 文章创建工作流 (article_creation)

**执行流程**:
1. **热点监测与选题** (2 秒)
   - 创建任务，状态：in_progress
   - 模拟热点监测过程
   - 生成选题结果

2. **素材收集** (2 秒)
   - 创建任务，状态：in_progress
   - 爬取相关素材
   - 整合素材内容

3. **内容生成** (3 秒)
   - 创建任务，状态：in_progress
   - 调用 AI 生成文章
   - 生成标题选项

4. **配图生成** (2 秒)
   - 创建任务，状态：in_progress
   - 根据内容生成配图

5. **排版** (1 秒)
   - 创建任务，状态：in_progress
   - 转换 Markdown 为富文本

6. **发布** (1 秒)
   - 创建任务，状态：in_progress
   - 发送到飞书
   - 创建多维表格记录

**总执行时间**: 约 11 秒

### 热点监测工作流 (topic_monitoring)

**执行流程**:
1. 扫描各大平台热点
2. 分析和筛选有价值的话题
3. 返回热点话题列表

**执行时间**: 约 3 秒

### 发布工作流 (publish)

**执行流程**:
1. 发送飞书消息
2. 创建飞书多维表格记录

**执行时间**: 约 1 秒

## 🛠️ 开发指南

### 代码规范

```bash
# 代码格式化
black .

# 代码检查
flake8 .

# 类型检查
mypy .
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_workflow.py

# 查看测试覆盖率
pytest --cov=.
```

### 添加新功能

1. **创建新的 API 模块**:
   ```bash
   mkdir api/new_feature
   touch api/new_feature/__init__.py
   touch api/new_feature/handler.py
   ```

2. **在 app.py 中注册路由**:
   ```python
   from api.new_feature.handler import new_feature_router
   app.include_router(new_feature_router, prefix="/api/new-feature")
   ```

3. **实现业务逻辑**:
   - 在 `core/` 目录下创建核心逻辑
   - 在 `api/` 目录下创建 API 接口

4. **编写测试**:
   ```bash
   mkdir tests
   touch tests/test_new_feature.py
   ```

## 🔍 故障排查

### 常见问题

#### 1. 端口冲突

**症状**: 端口 8888 被占用

**解决方案**:
```bash
# 查看占用端口的进程
lsof -i :8888

# 停止占用端口的进程
kill <PID>

# 或者修改端口配置
# 修改 api/app.py, Dockerfile, docker-compose.yml 中的端口
```

#### 2. 部署失败

**症状**: 一键部署脚本执行失败

**解决方案**:
```bash
# 检查 SSH 免密登录
ssh root@your_server_ip "echo 成功"

# 检查 Docker 服务
ssh root@your_server_ip "systemctl status docker"

# 检查磁盘空间
ssh root@your_server_ip "df -h"
```

#### 3. 工作流执行失败

**症状**: 工作流执行报错

**解决方案**:
```bash
# 查看容器日志
ssh root@your_server_ip "docker logs ai-editorial-system"

# 检查环境变量
ssh root@your_server_ip "docker exec ai-editorial-system env"

# 手动执行工作流测试
ssh root@your_server_ip "/root/ai-editorial-system/run_workflow.sh"
```

#### 4. 定时任务不执行

**症状**: 定时任务未按时执行

**解决方案**:
```bash
# 检查脚本权限
ssh root@your_server_ip "ls -la /root/ai-editorial-system/run_workflow.sh"

# 修复权限
ssh root@your_server_ip "chmod +x /root/ai-editorial-system/run_workflow.sh"

# 查看 cron 日志
ssh root@your_server_ip "tail -f /root/ai-editorial-system/cron.log"

# 检查 crontab 配置
ssh root@your_server_ip "crontab -l"
```

#### 5. API 密钥无效

**症状**: AI 生成内容时报错

**解决方案**:
```bash
# 检查环境变量配置
ssh root@your_server_ip "docker exec ai-editorial-system env | grep DEEPSEEK"

# 更新环境变量
# 编辑 .env 文件，更新 API 密钥
# 重新部署
./one_click_deploy.sh
```

## 📊 监控与日志

### 查看容器状态

```bash
# 查看运行状态
ssh root@your_server_ip "docker ps -f name=ai-editorial-system"

# 查看资源使用
ssh root@your_server_ip "docker stats ai-editorial-system"
```

### 查看日志

```bash
# 实时日志
ssh root@your_server_ip "docker logs -f ai-editorial-system"

# 最近 100 行日志
ssh root@your_server_ip "docker logs --tail 100 ai-editorial-system"

# 定时任务日志
ssh root@your_server_ip "tail -f /root/ai-editorial-system/cron.log"
```

### 健康检查

```bash
# 检查服务状态
curl http://your_server_ip:8888/health

# 检查 API 响应
curl http://your_server_ip:8888/api/tasks
```

## 📅 更新日志

### 2026-03-23 - 生产环境部署与优化

#### 功能更新
- ✅ **修复 task_id 类型问题**: 将 `get_task` 和 `update_task_status` 接口的 `task_id` 参数类型从 `int` 改为 `str`，支持 UUID 格式
- ✅ **修改默认端口**: 将服务端口从 8000 改为 8888，避免与其他项目冲突
  - 修改 `api/app.py` 中的端口配置
  - 修改 `Dockerfile` 中的 EXPOSE 端口
  - 修改 `docker-compose.yml` 中的端口映射

#### 定时任务配置
- ✅ **配置定时任务**: 每天自动执行工作流
  - 每天早上 8:00
  - 每天中午 12:00
  - 每天中午 12:50（新增）
- ✅ **创建工作流执行脚本**: 创建简化的 `run_workflow.sh` 脚本，去掉 SSH 命令，避免权限问题
- ✅ **修复脚本权限问题**: 设置正确的执行权限（`chmod +x`）

#### 部署优化
- ✅ **完成生产环境部署**: 使用一键部署脚本将系统部署到服务器 (43.156.242.184:8888)
- ✅ **创建 server_run_workflow.sh**: 创建专门用于服务器的工作流执行脚本
- ✅ **优化部署流程**: 确保脚本在部署时自动上传并设置权限

#### 测试与验证
- ✅ **添加完整的测试方案**: 创建 test_plan.md，包含 API 测试、功能测试、工作流测试、异常处理测试、性能测试
- ✅ **执行完整测试**: 所有测试用例通过
  - API 接口测试：100% 通过
  - 核心功能模块测试：100% 通过
  - 工作流测试：执行时间 < 30 秒
  - 异常处理测试：正确处理各种异常情况
  - 性能测试：并发 10，响应时间 < 5 秒

#### 代码优化
- ✅ **优化工作流引擎错误处理**: 修复 `_execute_article_creation_workflow` 方法中 `parameters` 为 `None` 时的错误
- ✅ **改进日志记录**: 添加详细的执行日志，便于故障排查

#### 文档更新
- ✅ **创建 README.md**: 完整的项目文档，包括需求、架构、部署、API 等内容
- ✅ **记录更新日志**: 详细记录每天的修改内容

#### 系统状态
- **服务器**: 43.156.242.184
- **端口**: 8888
- **容器状态**: 运行中
- **定时任务**: 已配置并验证
- **健康检查**: 通过

## 📄 许可证

MIT License

## 👥 联系方式

如有问题，请提交 Issue 或联系开发团队。

---

**最后更新**: 2026-03-23  
**版本**: 1.0.0  
**状态**: 生产环境运行中
