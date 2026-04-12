import re

class ContentOptimizer:
    def __init__(self):
        pass
    
    def optimize_article(self, content):
        """优化文章内容"""
        # 清理内容
        content = self._clean_content(content)
        
        # 优化标题
        content = self._optimize_headings(content)
        
        # 优化段落
        content = self._optimize_paragraphs(content)
        
        # 优化句子
        content = self._optimize_sentences(content)
        
        return content
    
    def _clean_content(self, content):
        """清理内容"""
        # 移除多余的空白字符
        content = re.sub(r'\s+', ' ', content)
        # 移除首尾空白
        content = content.strip()
        return content
    
    def _optimize_headings(self, content):
        """优化标题"""
        # 确保标题格式正确
        lines = content.split('\n')
        optimized_lines = []
        
        for line in lines:
            # 检查是否是标题
            if line.startswith('#'):
                # 确保标题后有空格
                match = re.match(r'(#+)([^\s])', line)
                if match:
                    hashes = match.group(1)
                    text = match.group(2)
                    line = f"{hashes} {text}"
            optimized_lines.append(line)
        
        return '\n'.join(optimized_lines)
    
    def _optimize_paragraphs(self, content):
        """优化段落"""
        # 确保段落之间有空白行
        # 确保每个段落开头没有多余的空格
        lines = content.split('\n')
        optimized_lines = []
        
        for line in lines:
            # 跳过空行
            if not line.strip():
                optimized_lines.append('')
                continue
            # 移除行首空格
            line = line.lstrip()
            optimized_lines.append(line)
        
        return '\n'.join(optimized_lines)
    
    def _optimize_sentences(self, content):
        """优化句子"""
        # 确保句子结尾有标点符号
        # 确保句子开头大写
        sentences = re.split(r'(?<=[。！？.!?])\s*', content)
        optimized_sentences = []
        
        for sentence in sentences:
            if sentence.strip():
                # 确保句子开头大写
                sentence = sentence[0].upper() + sentence[1:] if sentence else sentence
                # 确保句子结尾有标点符号
                if not re.search(r'[。！？.!?]$', sentence):
                    sentence += '。'
                optimized_sentences.append(sentence)
        
        return ' '.join(optimized_sentences)
    
    def check_readability(self, content):
        """检查文章可读性"""
        # 计算字数
        word_count = len(content)
        
        # 计算句子数
        sentences = re.split(r'[。！？.!?]', content)
        sentence_count = len([s for s in sentences if s.strip()])
        
        # 计算段落数
        paragraphs = content.split('\n\n')
        paragraph_count = len([p for p in paragraphs if p.strip()])
        
        # 计算平均句子长度
        avg_sentence_length = word_count / sentence_count if sentence_count > 0 else 0
        
        return {
            'word_count': word_count,
            'sentence_count': sentence_count,
            'paragraph_count': paragraph_count,
            'avg_sentence_length': avg_sentence_length,
            'readability_score': self._calculate_readability_score(avg_sentence_length, word_count)
        }
    
    def _calculate_readability_score(self, avg_sentence_length, word_count):
        """计算可读性评分"""
        # 简单的可读性评分算法
        # 分数越高，可读性越好
        score = 100
        
        # 句子越长，可读性越差
        if avg_sentence_length > 30:
            score -= (avg_sentence_length - 30) * 2
        
        # 文章越长，可读性要求越高
        if word_count > 2000:
            score -= 10
        
        return max(0, min(100, score))