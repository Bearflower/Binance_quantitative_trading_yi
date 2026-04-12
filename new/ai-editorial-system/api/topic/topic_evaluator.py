import sqlite3
import json
import os

class TopicEvaluator:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
    
    def evaluate_topic(self, topic_id):
        """评估选题"""
        # 获取选题信息
        topic = self._get_topic(topic_id)
        if not topic:
            return None
        
        # 评估可行性
        feasibility = self._evaluate_feasibility(topic)
        
        # 评估价值
        value = self._evaluate_value(topic)
        
        # 计算综合评分
        score = (feasibility + value) / 2
        
        return {
            'topic_id': topic_id,
            'feasibility': feasibility,
            'value': value,
            'score': score,
            'suggestion': self._generate_suggestion(score)
        }
    
    def _get_topic(self, topic_id):
        """获取选题信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM topics WHERE id = ?", (topic_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            topic = {
                'id': row[0],
                'title': row[1],
                'description': row[2],
                '热度': row[3],
                'relevance': row[4],
                'created_at': row[5],
                'status': row[6]
            }
            return topic
        finally:
            conn.close()
    
    def _evaluate_feasibility(self, topic):
        """评估可行性"""
        # 基于热度和相关性评估可行性
        heat = topic.get('热度', 0)
        relevance = topic.get('relevance', 0)
        
        # 热度越高，可行性越高
        heat_score = min(heat / 10000 * 50, 50)
        
        # 相关性越高，可行性越高
        relevance_score = relevance * 0.5
        
        return heat_score + relevance_score
    
    def _evaluate_value(self, topic):
        """评估价值"""
        # 基于热度和相关性评估价值
        heat = topic.get('热度', 0)
        relevance = topic.get('relevance', 0)
        
        # 热度越高，价值越高
        heat_score = min(heat / 10000 * 50, 50)
        
        # 相关性越高，价值越高
        relevance_score = relevance * 0.5
        
        return heat_score + relevance_score
    
    def _generate_suggestion(self, score):
        """生成建议"""
        if score >= 80:
            return "强烈推荐：该选题热度高，相关性强，非常适合作为文章主题"
        elif score >= 60:
            return "推荐：该选题有一定热度和相关性，可以作为文章主题"
        elif score >= 40:
            return "谨慎推荐：该选题热度或相关性一般，需要进一步分析"
        else:
            return "不推荐：该选题热度和相关性较低，建议考虑其他选题"
    
    def evaluate_topics(self, topic_ids):
        """评估多个选题"""
        evaluations = []
        for topic_id in topic_ids:
            evaluation = self.evaluate_topic(topic_id)
            if evaluation:
                evaluations.append(evaluation)
        
        # 按综合评分排序
        evaluations.sort(key=lambda x: x['score'], reverse=True)
        
        return evaluations