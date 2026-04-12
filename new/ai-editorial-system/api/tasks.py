from api.celery_config import app
from core.workflow_engine import WorkflowEngine
from core.event_bus import event_bus, EventTypes

# 创建工作流引擎实例
workflow_engine = WorkflowEngine()

@app.task
def monitor_hotspots():
    """监测热点话题"""
    try:
        # 发布工作流开始事件
        event_bus.publish(EventTypes.WORKFLOW_STARTED, {
            'workflow_type': 'topic_monitoring',
            'message': '开始监测热点话题'
        })
        
        # 执行热点监测工作流
        result = workflow_engine.execute_workflow('topic_monitoring')
        
        # 发布工作流完成事件
        event_bus.publish(EventTypes.WORKFLOW_COMPLETED, {
            'workflow_type': 'topic_monitoring',
            'result': result
        })
        
        return result
    except Exception as e:
        # 发布工作流失败事件
        event_bus.publish(EventTypes.WORKFLOW_FAILED, {
            'workflow_type': 'topic_monitoring',
            'error': str(e)
        })
        raise

@app.task
def generate_article():
    """生成文章"""
    try:
        # 发布工作流开始事件
        event_bus.publish(EventTypes.WORKFLOW_STARTED, {
            'workflow_type': 'article_creation',
            'message': '开始生成文章'
        })
        
        # 执行文章创建工作流
        result = workflow_engine.execute_workflow('article_creation')
        
        # 发布工作流完成事件
        event_bus.publish(EventTypes.WORKFLOW_COMPLETED, {
            'workflow_type': 'article_creation',
            'result': result
        })
        
        return result
    except Exception as e:
        # 发布工作流失败事件
        event_bus.publish(EventTypes.WORKFLOW_FAILED, {
            'workflow_type': 'article_creation',
            'error': str(e)
        })
        raise

@app.task
def publish_article(article_id):
    """发布文章"""
    try:
        # 发布工作流开始事件
        event_bus.publish(EventTypes.WORKFLOW_STARTED, {
            'workflow_type': 'publish',
            'message': f'开始发布文章 {article_id}',
            'article_id': article_id
        })
        
        # 执行发布工作流
        result = workflow_engine.execute_workflow('publish', {
            'article_id': article_id
        })
        
        # 发布文章发布事件
        event_bus.publish(EventTypes.ARTICLE_PUBLISHED, {
            'article_id': article_id,
            'publish_result': result
        })
        
        # 发布工作流完成事件
        event_bus.publish(EventTypes.WORKFLOW_COMPLETED, {
            'workflow_type': 'publish',
            'result': result
        })
        
        return result
    except Exception as e:
        # 发布工作流失败事件
        event_bus.publish(EventTypes.WORKFLOW_FAILED, {
            'workflow_type': 'publish',
            'error': str(e)
        })
        raise
