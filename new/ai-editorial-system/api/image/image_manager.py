import sqlite3
import json
import os
import uuid
from datetime import datetime

class ImageManager:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
        self.images_dir = os.path.join(os.path.dirname(__file__), '..', 'images')
        os.makedirs(self.images_dir, exist_ok=True)
    
    def store_image(self, article_id, image_path):
        """存储图片"""
        image_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        
        # 生成唯一的文件名
        ext = os.path.splitext(image_path)[1]
        new_filename = f"{image_id}{ext}"
        new_path = os.path.join(self.images_dir, new_filename)
        
        # 复制图片到存储目录
        import shutil
        shutil.copy(image_path, new_path)
        
        # 保存到数据库
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO images (id, article_id, url, local_path, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                (image_id, article_id, new_filename, new_path, now, 'stored')
            )
            conn.commit()
            return image_id
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def get_image(self, image_id):
        """获取图片信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM images WHERE id = ?", (image_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            image = {
                'id': row[0],
                'article_id': row[1],
                'url': row[2],
                'local_path': row[3],
                'created_at': row[4],
                'status': row[5]
            }
            return image
        finally:
            conn.close()
    
    def get_images_by_article(self, article_id):
        """根据文章ID获取图片"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM images WHERE article_id = ?", (article_id,))
            images = []
            for row in cursor.fetchall():
                image = {
                    'id': row[0],
                    'article_id': row[1],
                    'url': row[2],
                    'local_path': row[3],
                    'created_at': row[4],
                    'status': row[5]
                }
                images.append(image)
            return images
        finally:
            conn.close()
    
    def update_image(self, image_id, updates):
        """更新图片信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 构建更新语句
            set_clause = []
            values = []
            for key, value in updates.items():
                if key in ['article_id', 'url', 'local_path', 'status']:
                    set_clause.append(f"{key} = ?")
                    values.append(value)
            
            if not set_clause:
                return False
            
            values.append(image_id)
            query = f"UPDATE images SET {', '.join(set_clause)} WHERE id = ?"
            
            cursor.execute(query, values)
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def delete_image(self, image_id):
        """删除图片"""
        # 获取图片信息
        image = self.get_image(image_id)
        if not image:
            return False
        
        # 删除文件
        if image.get('local_path') and os.path.exists(image['local_path']):
            try:
                os.remove(image['local_path'])
            except Exception as e:
                print(f"Error deleting image file: {e}")
        
        # 删除数据库记录
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("DELETE FROM images WHERE id = ?", (image_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()