class EventBus:
    def __init__(self):
        self._handlers = {}
    
    def subscribe(self, event_type, handler):
        """订阅事件"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
    
    def unsubscribe(self, event_type, handler):
        """取消订阅事件"""
        if event_type in self._handlers:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)
    
    def publish(self, event_type, data=None):
        """发布事件"""
        if event_type in self._handlers:
            for handler in self._handlers[event_type]:
                try:
                    handler(data)
                except Exception as e:
                    print(f"处理事件 {event_type} 时出错: {e}")
    
    def get_handlers(self, event_type):
        """获取事件的所有处理器"""
        return self._handlers.get(event_type, [])

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

# 测试代码
if __name__ == '__main__':
    # 定义事件处理器
    def handle_task_created(data):
        print(f"任务创建事件: {data}")
    
    def handle_task_completed(data):
        print(f"任务完成事件: {data}")
    
    def handle_article_published(data):
        print(f"文章发布事件: {data}")
    
    # 订阅事件
    event_bus.subscribe(EventTypes.TASK_CREATED, handle_task_created)
    event_bus.subscribe(EventTypes.TASK_COMPLETED, handle_task_completed)
    event_bus.subscribe(EventTypes.ARTICLE_PUBLISHED, handle_article_published)
    
    # 发布事件
    print("发布任务创建事件...")
    event_bus.publish(EventTypes.TASK_CREATED, {'task_id': 1, 'task_name': '测试任务'})
    
    print("发布任务完成事件...")
    event_bus.publish(EventTypes.TASK_COMPLETED, {'task_id': 1, 'result': 'success'})
    
    print("发布文章发布事件...")
    event_bus.publish(EventTypes.ARTICLE_PUBLISHED, {'article_id': 1, 'title': '测试文章'})
    
    # 取消订阅
    event_bus.unsubscribe(EventTypes.TASK_CREATED, handle_task_created)
    
    # 再次发布任务创建事件（应该不会触发处理器）
    print("再次发布任务创建事件...")
    event_bus.publish(EventTypes.TASK_CREATED, {'task_id': 2, 'task_name': '测试任务2'})
