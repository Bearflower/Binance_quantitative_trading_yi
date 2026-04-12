from ..celery_config import celery
from ..core.task_manager import TaskManager
from ..core.workflow_engine import WorkflowEngine

# 创建实例
task_manager = TaskManager()
workflow_engine = WorkflowEngine()

@celery.task
def execute_workflow_task(workflow_type, params):
    """执行工作流任务"""
    return workflow_engine.execute_workflow(workflow_type, params)

@celery.task
def process_topic_task(topic_id):
    """处理选题任务"""
    # 这里会调用选题模块的相关功能
    print(f"Processing topic: {topic_id}")
    return f"Topic {topic_id} processed"

@celery.task
def process_material_task(topic_id):
    """处理素材搜集任务"""
    # 这里会调用素材搜集模块的相关功能
    print(f"Processing materials for topic: {topic_id}")
    return f"Materials for topic {topic_id} processed"

@celery.task
def generate_article_task(topic_id, materials):
    """生成文章任务"""
    # 这里会调用文章生成模块的相关功能
    print(f"Generating article for topic: {topic_id}")
    return f"Article for topic {topic_id} generated"

@celery.task
def generate_image_task(article_id, prompt):
    """生成图片任务"""
    # 这里会调用配图生成模块的相关功能
    print(f"Generating image for article: {article_id}")
    return f"Image for article {article_id} generated"

@celery.task
def publish_article_task(article_id):
    """发布文章任务"""
    # 这里会调用发布模块的相关功能
    print(f"Publishing article: {article_id}")
    return f"Article {article_id} published"