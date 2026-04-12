class EventBus:
    def __init__(self):
        self._subscribers = {}
    
    def subscribe(self, event_type, callback):
        """订阅事件"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
    
    def unsubscribe(self, event_type, callback):
        """取消订阅"""
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(callback)
    
    def publish(self, event_type, data):
        """发布事件"""
        if event_type in self._subscribers:
            for callback in self._subscribers[event_type]:
                try:
                    callback(data)
                except Exception as e:
                    print(f"Error in event handler: {e}")
    
    def get_subscribers(self, event_type):
        """获取事件的订阅者"""
        return self._subscribers.get(event_type, [])

# 全局事件总线实例
event_bus = EventBus()

# 事件类型常量
class EventTypes:
    # 系统事件
    SYSTEM_STARTED = 'system_started'
    SYSTEM_STOPPED = 'system_stopped'
    
    # 任务事件
    TASK_CREATED = 'task_created'
    TASK_STARTED = 'task_started'
    TASK_COMPLETED = 'task_completed'
    TASK_FAILED = 'task_failed'
    
    # 选题事件
    TOPIC_CREATED = 'topic_created'
    TOPIC_UPDATED = 'topic_updated'
    
    # 文章事件
    ARTICLE_CREATED = 'article_created'
    ARTICLE_UPDATED = 'article_updated'
    ARTICLE_PUBLISHED = 'article_published'
    
    # 工作流事件
    WORKFLOW_STARTED = 'workflow_started'
    WORKFLOW_COMPLETED = 'workflow_completed'
    WORKFLOW_FAILED = 'workflow_failed'