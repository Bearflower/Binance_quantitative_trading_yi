from .task_manager import TaskManager

class WorkflowEngine:
    def __init__(self):
        self.task_manager = TaskManager()
    
    def execute_workflow(self, workflow_type, params):
        """执行工作流"""
        # 创建工作流任务
        task_id = self.task_manager.create_task(f"{workflow_type}工作流", workflow_type, params)
        
        # 根据工作流类型执行不同的流程
        if workflow_type == 'full':
            self._execute_full_workflow(task_id, params)
        elif workflow_type == 'topic':
            self._execute_topic_workflow(task_id, params)
        elif workflow_type == 'article':
            self._execute_article_workflow(task_id, params)
        elif workflow_type == 'publish':
            self._execute_publish_workflow(task_id, params)
        else:
            self.task_manager.update_task_status(
                task_id, 'failed', error='Unknown workflow type'
            )
        
        return {
            'workflow_task_id': task_id,
            'status': 'completed',
            'result': {'message': 'Workflow executed successfully'}
        }
    
    def _execute_full_workflow(self, task_id, params):
        """执行完整工作流：选题 → 素材 → 文章 → 配图 → 排版 → 发布"""
        try:
            # 更新任务状态为处理中
            self.task_manager.update_task_status(task_id, 'processing')
            
            # 1. 选题
            print(f"执行选题步骤 for task {task_id}")
            # 这里会调用选题模块
            
            # 2. 素材搜集
            print(f"执行素材搜集步骤 for task {task_id}")
            # 这里会调用素材搜集模块
            
            # 3. 文章生成
            print(f"执行文章生成步骤 for task {task_id}")
            # 这里会调用文章生成模块
            
            # 4. 配图生成
            print(f"执行配图生成步骤 for task {task_id}")
            # 这里会调用配图生成模块
            
            # 5. 排版处理
            print(f"执行排版处理步骤 for task {task_id}")
            # 这里会调用排版模块
            
            # 6. 发布
            print(f"执行发布步骤 for task {task_id}")
            # 这里会调用发布模块
            
            # 更新任务状态为完成
            self.task_manager.update_task_status(
                task_id, 'completed', result={'message': 'Full workflow executed successfully'}
            )
        except Exception as e:
            self.task_manager.update_task_status(
                task_id, 'failed', error=str(e)
            )
    
    def _execute_topic_workflow(self, task_id, params):
        """执行选题工作流"""
        try:
            self.task_manager.update_task_status(task_id, 'processing')
            
            # 执行选题相关操作
            print(f"执行选题工作流 for task {task_id}")
            # 这里会调用选题模块
            
            self.task_manager.update_task_status(
                task_id, 'completed', result={'message': 'Topic workflow executed successfully'}
            )
        except Exception as e:
            self.task_manager.update_task_status(
                task_id, 'failed', error=str(e)
            )
    
    def _execute_article_workflow(self, task_id, params):
        """执行文章生成工作流"""
        try:
            self.task_manager.update_task_status(task_id, 'processing')
            
            # 执行文章生成相关操作
            print(f"执行文章生成工作流 for task {task_id}")
            # 这里会调用文章生成模块
            
            self.task_manager.update_task_status(
                task_id, 'completed', result={'message': 'Article workflow executed successfully'}
            )
        except Exception as e:
            self.task_manager.update_task_status(
                task_id, 'failed', error=str(e)
            )
    
    def _execute_publish_workflow(self, task_id, params):
        """执行发布工作流"""
        try:
            self.task_manager.update_task_status(task_id, 'processing')
            
            # 执行发布相关操作
            print(f"执行发布工作流 for task {task_id}")
            # 这里会调用发布模块
            
            self.task_manager.update_task_status(
                task_id, 'completed', result={'message': 'Publish workflow executed successfully'}
            )
        except Exception as e:
            self.task_manager.update_task_status(
                task_id, 'failed', error=str(e)
            )