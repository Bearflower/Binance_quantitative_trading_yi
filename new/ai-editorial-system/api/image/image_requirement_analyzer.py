import re
import jieba
from collections import Counter

class ImageRequirementAnalyzer:
    def __init__(self):
        pass
    
    def analyze_requirements(self, article_content):
        """分析文章的图片需求"""
        # 提取关键词
        keywords = self._extract_keywords(article_content)
        
        # 分析文章结构
        structure = self._analyze_structure(article_content)
        
        # 生成图片需求
        requirements = self._generate_requirements(keywords, structure)
        
        return requirements
    
    def _extract_keywords(self, content):
        """提取关键词"""
        # 使用 jieba 分词
        words = jieba.cut(content)
        
        # 过滤停用词
        stop_words = set(['的', '了', '和', '是', '在', '有', '我', '他', '她', '它', '这', '那', '你', '我', '他', '她', '它', '们'])
        filtered_words = [word for word in words if word not in stop_words and len(word) > 1]
        
        # 统计词频
        word_counts = Counter(filtered_words)
        
        # 取前10个高频词
        top_keywords = [word for word, _ in word_counts.most_common(10)]
        
        return top_keywords
    
    def _analyze_structure(self, content):
        """分析文章结构"""
        # 提取标题
        headings = re.findall(r'#+.+', content)
        
        # 提取段落
        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
        
        return {
            'headings': headings,
            'paragraph_count': len(paragraphs)
        }
    
    def _generate_requirements(self, keywords, structure):
        """生成图片需求"""
        requirements = []
        
        # 生成封面图需求
        cover_image_prompt = f"{keywords[0]}相关的封面图，美观，专业，符合公众号风格"
        requirements.append({
            'type': 'cover',
            'prompt': cover_image_prompt,
            'position': 'top'
        })
        
        # 为每个主要部分生成图片需求
        for i, keyword in enumerate(keywords[1:3]):  # 取前3个关键词
            section_image_prompt = f"{keyword}相关的图片，清晰，专业，与文章内容相关"
            requirements.append({
                'type': 'section',
                'prompt': section_image_prompt,
                'position': f'section_{i+1}'
            })
        
        return requirements