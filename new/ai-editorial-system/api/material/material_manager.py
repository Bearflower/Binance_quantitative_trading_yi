import sqlite3
import json
import os
import uuid
from datetime import datetime

class MaterialManager:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
    
    def store_material(self, topic_id, material):
        """存储素材"""
        material_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO materials (id, topic_id, type, title, content, url, created_at, relevance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (material_id, topic_id, material.get('type', 'article'), material.get('title', ''), 
                 material.get('content', ''), material.get('url', ''), now, material.get('relevance', 80))
            )
            conn.commit()
            return material_id
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def store_materials(self, topic_id, materials):
        """批量存储素材"""
        material_ids = []
        for material in materials:
            material_id = self.store_material(topic_id, material)
            material_ids.append(material_id)
        return material_ids
    
    def get_materials_by_topic(self, topic_id):
        """根据选题ID获取素材"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM materials WHERE topic_id = ? ORDER BY created_at DESC", (topic_id,))
            materials = []
            for row in cursor.fetchall():
                material = {
                    'id': row[0],
                    'topic_id': row[1],
                    'type': row[2],
                    'title': row[3],
                    'content': row[4],
                    'url': row[5],
                    'created_at': row[6],
                    'relevance': row[7]
                }
                materials.append(material)
            return materials
        finally:
            conn.close()
    
    def get_material(self, material_id):
        """获取单个素材"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM materials WHERE id = ?", (material_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            material = {
                'id': row[0],
                'topic_id': row[1],
                'type': row[2],
                'title': row[3],
                'content': row[4],
                'url': row[5],
                'created_at': row[6],
                'relevance': row[7]
            }
            return material
        finally:
            conn.close()
    
    def update_material(self, material_id, updates):
        """更新素材"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 构建更新语句
            set_clause = []
            values = []
            for key, value in updates.items():
                if key in ['type', 'title', 'content', 'url', 'relevance']:
                    set_clause.append(f"{key} = ?")
                    values.append(value)
            
            if not set_clause:
                return False
            
            values.append(material_id)
            query = f"UPDATE materials SET {', '.join(set_clause)} WHERE id = ?"
            
            cursor.execute(query, values)
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def delete_material(self, material_id):
        """删除素材"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("DELETE FROM materials WHERE id = ?", (material_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()