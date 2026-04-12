import sqlite3
import json
import os
import uuid
from datetime import datetime

class TopicGenerator:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
    
    def generate_topics(self, analyzed_topics, count=5):
        """生成选题建议"""
        # 取前N个热度最高的话题
        top_topics = analyzed_topics[:count]
        
        generated_topics = []
        for topic in top_topics:
            # 生成选题建议
            topic_suggestion = self._generate_topic_suggestion(topic)
            # 保存到数据库
            topic_id = self._save_topic(topic_suggestion)
            topic_suggestion['id'] = topic_id
            generated_topics.append(topic_suggestion)
        
        return generated_topics
    
    def _generate_topic_suggestion(self, topic):
        """生成单个选题建议"""
        # 基于话题生成选题建议
        title = topic['title']
        description = f"基于{topic['platform']}平台的热点话题：{title}"
        
        return {
            'title': title,
            'description': description,
            '热度': topic['heat'],
            'relevance': topic['relevance'],
            'status': 'pending'
        }
    
    def _save_topic(self, topic):
        """保存选题到数据库"""
        topic_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO topics (id, title, description, 热度, relevance, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (topic_id, topic['title'], topic['description'], topic['热度'], topic['relevance'], now, topic['status'])
            )
            conn.commit()
            return topic_id
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()