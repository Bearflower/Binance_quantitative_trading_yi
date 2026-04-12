import sqlite3
import json
import os
from datetime import datetime

class TopicAnalyzer:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', 'app.db')
    
    def analyze_topic(self, topic):
        """分析话题"""
        # 计算热度分数
        heat_score = self._calculate_heat_score(topic)
        
        # 计算相关性分数
        relevance_score = self._calculate_relevance_score(topic)
        
        return {
            'title': topic['title'],
            'heat': heat_score,
            'relevance': relevance_score,
            'platform': topic.get('platform', 'unknown')
        }
    
    def _calculate_heat_score(self, topic):
        """计算热度分数"""
        # 基础热度分数
        base_heat = topic.get('heat', 0)
        
        # 时间衰减因子（越新的话题热度越高）
        time_factor = 1.0
        
        return int(base_heat * time_factor)
    
    def _calculate_relevance_score(self, topic):
        """计算相关性分数"""
        # 这里可以根据历史数据和用户兴趣计算相关性
        # 目前使用默认值
        return 80  # 默认相关性分数
    
    def analyze_topics(self, topics):
        """分析多个话题"""
        analyzed_topics = []
        for topic in topics:
            analyzed_topic = self.analyze_topic(topic)
            analyzed_topics.append(analyzed_topic)
        
        # 按热度排序
        analyzed_topics.sort(key=lambda x: x['heat'], reverse=True)
        
        return analyzed_topics