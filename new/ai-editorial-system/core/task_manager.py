import sqlite3
import json
from datetime import datetime
import os

# 获取数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), '../data/ai_editorial_system.db')

class TaskManager:
    def __init__(self):
        self.db_path = DB_PATH
    
    def create_task(self, name, task_type, parameters=None):
        """创建新任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        params_json = json.dumps(parameters) if parameters else None
        
        cursor.execute('''
        INSERT INTO tasks (name, type, status, parameters, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, task_type, 'pending', params_json, datetime.now(), datetime.now()))
        
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return task_id
    
    def get_task(self, task_id):
        """获取任务详情"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT id, name, type, status, parameters, result, created_at, updated_at
        FROM tasks
        WHERE id = ?
        ''', (task_id,))
        
        task = cursor.fetchone()
        conn.close()
        
        if not task:
            return None
        
        return {
            'id': task[0],
            'name': task[1],
            'type': task[2],
            'status': task[3],
            'parameters': json.loads(task[4]) if task[4] else None,
            'result': json.loads(task[5]) if task[5] else None,
            'created_at': task[6],
            'updated_at': task[7]
        }
    
    def get_tasks(self, status=None, task_type=None, limit=100):
        """获取任务列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        query = 'SELECT id, name, type, status, parameters, result, created_at, updated_at FROM tasks'
        conditions = []
        params = []
        
        if status:
            conditions.append('status = ?')
            params.append(status)
        
        if task_type:
            conditions.append('type = ?')
            params.append(task_type)
        
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        
        query += ' ORDER BY created_at DESC LIMIT ?'
        params.append(limit)
        
        cursor.execute(query, params)
        tasks = cursor.fetchall()
        conn.close()
        
        result = []
        for task in tasks:
            result.append({
                'id': task[0],
                'name': task[1],
                'type': task[2],
                'status': task[3],
                'parameters': json.loads(task[4]) if task[4] else None,
                'result': json.loads(task[5]) if task[5] else None,
                'created_at': task[6],
                'updated_at': task[7]
            })
        
        return result
    
    def update_task_status(self, task_id, status):
        """更新任务状态"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
        UPDATE tasks
        SET status = ?, updated_at = ?
        WHERE id = ?
        ''', (status, datetime.now(), task_id))
        
        conn.commit()
        conn.close()
    
    def update_task_result(self, task_id, result):
        """更新任务结果"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        result_json = json.dumps(result)
        
        cursor.execute('''
        UPDATE tasks
        SET result = ?, updated_at = ?
        WHERE id = ?
        ''', (result_json, datetime.now(), task_id))
        
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id):
        """删除任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        
        conn.commit()
        conn.close()

# 测试代码
if __name__ == '__main__':
    task_manager = TaskManager()
    
    # 创建任务
    task_id = task_manager.create_task('测试任务', 'test', {'param1': 'value1', 'param2': 'value2'})
    print(f"创建任务成功，任务ID: {task_id}")
    
    # 获取任务详情
    task = task_manager.get_task(task_id)
    print(f"任务详情: {task}")
    
    # 更新任务状态
    task_manager.update_task_status(task_id, 'in_progress')
    print("更新任务状态为 'in_progress'")
    
    # 更新任务结果
    task_manager.update_task_result(task_id, {'result': 'success', 'data': 'test data'})
    print("更新任务结果")
    
    # 再次获取任务详情
    task = task_manager.get_task(task_id)
    print(f"更新后的任务详情: {task}")
    
    # 获取任务列表
    tasks = task_manager.get_tasks()
    print(f"任务列表: {tasks}")
