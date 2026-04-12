import hashlib
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class PlagiarismChecker:
    def __init__(self):
        self.vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        self.similarity_threshold = 0.7
    
    def check_plagiarism(self, content, materials):
        """检查抄袭"""
        if not materials:
            return {'is_plagiarized': False, 'score': 0, 'sources': []}
        
        # 清理内容
        cleaned_content = self._clean_content(content)
        
        # 检查与素材的相似度
        similarities = []
        for material in materials:
            material_content = material.get('content', '')
            if material_content:
                similarity = self._calculate_similarity(cleaned_content, material_content)
                if similarity >= self.similarity_threshold:
                    similarities.append({
                        'material_id': material.get('id'),
                        'title': material.get('title'),
                        'similarity': similarity
                    })
        
        # 计算总体抄袭评分
        if similarities:
            avg_similarity = sum(s['similarity'] for s in similarities) / len(similarities)
            is_plagiarized = avg_similarity >= self.similarity_threshold
        else:
            avg_similarity = 0
            is_plagiarized = False
        
        return {
            'is_plagiarized': is_plagiarized,
            'score': avg_similarity,
            'sources': similarities
        }
    
    def _clean_content(self, content):
        """清理内容"""
        # 转为小写
        content = content.lower()
        # 移除标点符号
        content = re.sub(r'[\W_]+', ' ', content)
        # 移除多余的空白字符
        content = re.sub(r'\s+', ' ', content)
        return content.strip()
    
    def _calculate_similarity(self, text1, text2):
        """计算文本相似度"""
        try:
            # 向量化文本
            X = self.vectorizer.fit_transform([text1, text2])
            # 计算余弦相似度
            similarity = cosine_similarity(X[0], X[1])[0][0]
            return similarity
        except Exception as e:
            print(f"Error calculating similarity: {e}")
            return 0
    
    def generate_unique_content(self, content, materials):
        """生成原创内容"""
        # 检查抄袭
        plagiarism_result = self.check_plagiarism(content, materials)
        
        if not plagiarism_result['is_plagiarized']:
            return content
        
        # 如果有抄袭，尝试改写内容
        # 这里使用简单的方法，实际项目中可能需要更复杂的改写算法
        # 例如使用同义词替换、句子重组等
        
        # 简单的改写示例
        rewritten_content = content
        
        # 替换一些常见词汇
        replacements = {
            '非常': '十分',
            '很': '相当',
            '重要': '关键',
            '需要': '必须',
            '但是': '然而',
            '所以': '因此',
            '因为': '由于',
            '如果': '假如',
            '能够': '可以',
            '可能': '或许'
        }
        
        for old, new in replacements.items():
            rewritten_content = rewritten_content.replace(old, new)
        
        return rewritten_content