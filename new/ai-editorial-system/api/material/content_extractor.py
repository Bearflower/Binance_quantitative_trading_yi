from bs4 import BeautifulSoup
import re

class ContentExtractor:
    def __init__(self):
        pass
    
    def extract_content(self, html, url):
        """提取网页内容"""
        soup = BeautifulSoup(html, 'html.parser')
        
        # 提取标题
        title = self._extract_title(soup)
        
        # 提取正文
        content = self._extract_main_content(soup)
        
        # 提取摘要
        summary = self._extract_summary(content)
        
        # 提取图片链接
        images = self._extract_images(soup, url)
        
        return {
            'title': title,
            'content': content,
            'summary': summary,
            'images': images
        }
    
    def _extract_title(self, soup):
        """提取标题"""
        # 尝试从h1标签提取
        h1 = soup.find('h1')
        if h1:
            return h1.get_text().strip()
        
        # 尝试从title标签提取
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text().strip()
        
        return 'Untitled'
    
    def _extract_main_content(self, soup):
        """提取正文内容"""
        # 移除脚本和样式
        for script in soup(['script', 'style']):
            script.decompose()
        
        # 尝试从常见的内容容器提取
        content_containers = ['article', 'main', 'content', 'div[class*=content]', 'div[class*=article]']
        for selector in content_containers:
            container = soup.select_one(selector)
            if container:
                text = container.get_text(separator='\n', strip=True)
                if text:
                    return text
        
        # 如果没有找到特定容器，提取所有文本
        text = soup.get_text(separator='\n', strip=True)
        return text
    
    def _extract_summary(self, content, max_length=200):
        """提取摘要"""
        # 简单地截取前N个字符作为摘要
        if len(content) > max_length:
            return content[:max_length] + '...'
        return content
    
    def _extract_images(self, soup, base_url):
        """提取图片链接"""
        images = []
        for img in soup.find_all('img', src=True):
            src = img['src']
            # 处理相对链接
            if src.startswith('/'):
                src = base_url + src
            elif not src.startswith('http'):
                continue
            images.append(src)
        return images
    
    def clean_content(self, content):
        """清理内容"""
        # 移除多余的空白字符
        content = re.sub(r'\s+', ' ', content)
        # 移除特殊字符
        content = re.sub(r'[\x00-\x1f\x7f]', '', content)
        return content.strip()