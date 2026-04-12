from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.task_manager import TaskManager
from core.workflow_engine import WorkflowEngine
from core.event_bus import event_bus, EventTypes
from api.topic.hotspot_monitor import HotspotMonitor
from api.material.spider import MaterialSpider
from api.article.ai_content_generator import AIContentGenerator
from api.image.image_generator import ImageGenerator
from api.typesetting.rich_text_generator import RichTextGenerator
from api.publish.wechat_api_client import FeishuClient
import uvicorn

# 创建FastAPI应用
app = FastAPI(
    title="AI编辑部系统",
    description="基于AI技术的自动化内容创作与发布平台",
    version="1.0.0"
)

# 创建各个模块的实例
task_manager = TaskManager()
workflow_engine = WorkflowEngine()
hotspot_monitor = HotspotMonitor()
material_spider = MaterialSpider()
ai_content_generator = AIContentGenerator()
image_generator = ImageGenerator()
rich_text_generator = RichTextGenerator()
feishu_client = FeishuClient()

# 定义数据模型
class TaskCreate(BaseModel):
    name: str
    type: str
    parameters: dict = None

class WorkflowExecute(BaseModel):
    workflow_type: str
    parameters: dict = None

class ArticleCreate(BaseModel):
    topic: str
    materials: list = None
    length: int = 1000

class PublishRequest(BaseModel):
    article_id: int
    title: str
    content: str

# 任务管理API
@app.post("/api/tasks", response_model=dict)
def create_task(task: TaskCreate):
    """创建任务"""
    task_id = task_manager.create_task(task.name, task.type, task.parameters)
    # 发布任务创建事件
    event_bus.publish(EventTypes.TASK_CREATED, {
        'task_id': task_id,
        'task_name': task.name,
        'task_type': task.type
    })
    return {"task_id": task_id, "status": "created"}

@app.get("/api/tasks", response_model=list)
def get_tasks(status: str = None, type: str = None, limit: int = 100):
    """获取任务列表"""
    tasks = task_manager.get_tasks(status=status, task_type=type, limit=limit)
    return tasks

@app.get("/api/tasks/{task_id}", response_model=dict)
def get_task(task_id: str):
    """获取任务详情"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

@app.put("/api/tasks/{task_id}/status", response_model=dict)
def update_task_status(task_id: str, status: str):
    """更新任务状态"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task_manager.update_task_status(task_id, status)
    # 发布任务状态更新事件
    if status == 'in_progress':
        event_bus.publish(EventTypes.TASK_STARTED, {'task_id': task_id})
    elif status == 'completed':
        event_bus.publish(EventTypes.TASK_COMPLETED, {'task_id': task_id})
    elif status == 'failed':
        event_bus.publish(EventTypes.TASK_FAILED, {'task_id': task_id})
    return {"status": "updated"}

# 工作流API
@app.post("/api/workflows", response_model=dict)
def execute_workflow(workflow: WorkflowExecute):
    """执行工作流"""
    result = workflow_engine.execute_workflow(workflow.workflow_type, workflow.parameters)
    return result

# 热点监测API
@app.get("/api/topics/hotspots", response_model=list)
def get_hotspots():
    """获取热点话题"""
    hotspots = hotspot_monitor.scan_hotspots()
    return hotspots

@app.get("/api/topics/analyze", response_model=list)
def analyze_topics(topic_type: str = None):
    """分析热点话题"""
    hotspots = hotspot_monitor.scan_hotspots()
    analyzed_topics = hotspot_monitor.analyze_topics(hotspots, topic_type)
    return analyzed_topics

# 素材收集API
@app.get("/api/materials/crawl", response_model=list)
def crawl_materials(topic: str, max_results: int = 10):
    """爬取素材"""
    materials = material_spider.crawl_materials(topic, max_results)
    return materials

@app.get("/api/materials/integrate", response_model=dict)
def integrate_materials(topic: str, max_results: int = 10):
    """整合素材"""
    materials = material_spider.crawl_materials(topic, max_results)
    integrated = material_spider.integrate_materials(materials)
    return integrated

# 文章生成API
@app.post("/api/articles/generate", response_model=dict)
def generate_article(article: ArticleCreate):
    """生成文章"""
    content = ai_content_generator.generate_article(article.topic, article.materials, article.length)
    titles = ai_content_generator.generate_title(content)
    return {"content": content, "titles": titles}

@app.post("/api/articles/optimize", response_model=dict)
def optimize_article(content: str):
    """优化文章"""
    optimized = ai_content_generator.optimize_content(content)
    return {"content": optimized}

# 图片生成API
@app.post("/api/images/generate", response_model=dict)
def generate_image(prompt: str, size: str = "1024x1024", num: int = 1):
    """生成图片"""
    width, height = map(int, size.split('x'))
    images = image_generator.generate_image(prompt, (width, height), num)
    # 保存图片并返回路径
    image_paths = []
    for i, img in enumerate(images):
        path = f"images/generated/image_{i}.png"
        if image_generator.save_image(img, path):
            image_paths.append(path)
    return {"images": image_paths}

# 排版API
@app.post("/api/typesetting/convert", response_model=dict)
def convert_markdown(markdown: str):
    """转换Markdown为富文本"""
    rich_text = rich_text_generator.convert_markdown(markdown)
    return {"rich_text": rich_text}

# 发布API
@app.post("/api/publish/feishu", response_model=dict)
def publish_to_feishu(publish_data: PublishRequest):
    """发布到飞书"""
    try:
        # 发送飞书消息
        message_result = feishu_client.send_message(publish_data.title, publish_data.content)
        # 创建多维表格记录
        sheet_result = feishu_client.create_sheet_record("growth_articles", {
            "title": publish_data.title,
            "content": publish_data.content
        })
        return {"message_result": message_result, "sheet_result": sheet_result, "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 健康检查
@app.get("/health", response_model=dict)
def health_check():
    """健康检查"""
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8888, reload=True)
