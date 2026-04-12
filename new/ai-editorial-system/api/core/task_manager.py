import sqlite3
import json
from datetime import datetime
import uuid
import os

class TaskManager:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
    
    def create_task(self, name, task_type, parameters=None):
        """创建新任务"""
        task_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        status = 'created'
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO tasks (id, type, status, created_at, updated_at, params) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, task_type, status, now, now, json.dumps(parameters))
            )
            conn.commit()
            return task_id
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def get_task(self, task_id):
        """获取任务信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            task = {
                'id': row[0],
                'type': row[1],
                'status': row[2],
                'created_at': row[3],
                'updated_at': row[4],
                'params': json.loads(row[5]) if row[5] else {},
                'result': json.loads(row[6]) if row[6] else {},
                'error': row[7]
            }
            return task
        finally:
            conn.close()
    
    def update_task_status(self, task_id, status, result=None, error=None):
        """更新任务状态"""
        now = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, result = ?, error = ? WHERE id = ?",
                (status, now, json.dumps(result) if result else None, error, task_id)
            )
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def cancel_task(self, task_id):
        """取消任务"""
        return self.update_task_status(task_id, 'cancelled')
    
    def get_tasks(self, status=None):
        """获取任务列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if status:
                cursor.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status,))
            else:
                cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
            
            tasks = []
            for row in cursor.fetchall():
                task = {
                    'id': row[0],
                    'type': row[1],
                    'status': row[2],
                    'created_at': row[3],
                    'updated_at': row[4],
                    'params': json.loads(row[5]) if row[5] else {},
                    'result': json.loads(row[6]) if row[6] else {},
                    'error': row[7]
                }
                tasks.append(task)
            return tasks
        finally:
            conn.close()
    
    def update_task_result(self, task_id, result):
        """更新任务结果"""
        now = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result), now, task_id)
            )
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()