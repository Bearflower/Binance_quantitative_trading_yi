import hashlib
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class DedupEngine:
    def __init__(self):
        self.vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        self.similarity_threshold = 0.8
    
    def deduplicate(self, materials):
        """去重素材"""
        if not materials:
            return []
        
        # 计算每个素材的哈希值
        for material in materials:
            material['hash'] = self._calculate_hash(material['content'])
        
        # 去重：基于哈希值
        unique_materials = self._deduplicate_by_hash(materials)
        
        # 去重：基于内容相似度
        unique_materials = self._deduplicate_by_similarity(unique_materials)
        
        return unique_materials
    
    def _calculate_hash(self, content):
        """计算内容的哈希值"""
        # 清理内容
        cleaned_content = self._clean_content(content)
        # 计算哈希值
        return hashlib.md5(cleaned_content.encode()).hexdigest()
    
    def _clean_content(self, content):
        """清理内容"""
        # 转为小写
        content = content.lower()
        # 移除标点符号
        content = re.sub(r'[\W_]+', ' ', content)
        # 移除多余的空白字符
        content = re.sub(r'\s+', ' ', content)
        return content.strip()
    
    def _deduplicate_by_hash(self, materials):
        """基于哈希值去重"""
        seen_hashes = set()
        unique_materials = []
        
        for material in materials:
            if material['hash'] not in seen_hashes:
                seen_hashes.add(material['hash'])
                unique_materials.append(material)
        
        return unique_materials
    
    def _deduplicate_by_similarity(self, materials):
        """基于内容相似度去重"""
        if len(materials) <= 1:
            return materials
        
        # 提取内容
        contents = [material['content'] for material in materials]
        
        try:
            # 向量化内容
            X = self.vectorizer.fit_transform(contents)
            
            # 计算相似度矩阵
            similarity_matrix = cosine_similarity(X)
            
            # 标记重复的素材
            to_remove = set()
            for i in range(len(materials)):
                for j in range(i + 1, len(materials)):
                    if similarity_matrix[i][j] >= self.similarity_threshold:
                        # 保留较长的素材
                        if len(contents[i]) >= len(contents[j]):
                            to_remove.add(j)
                        else:
                            to_remove.add(i)
            
            # 移除重复的素材
            unique_materials = [material for i, material in enumerate(materials) if i not in to_remove]
            return unique_materials
        except Exception as e:
            print(f"Error in deduplicate_by_similarity: {e}")
            return materials