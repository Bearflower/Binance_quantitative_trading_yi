import markdown
from markdown.extensions import fenced_code, tables

class MarkdownParser:
    def __init__(self):
        # 配置Markdown扩展
        self.extensions = [
            fenced_code.FencedCodeExtension(),
            tables.TableExtension()
        ]
    
    def parse(self, markdown_content):
        """解析Markdown内容"""
        try:
            # 解析Markdown为HTML
            html = markdown.markdown(markdown_content, extensions=self.extensions)
            return html
        except Exception as e:
            print(f"Error parsing Markdown: {e}")
            return markdown_content
    
    def extract_headings(self, markdown_content):
        """提取Markdown中的标题"""
        import re
        # 匹配标题
        headings = re.findall(r'(#+)(.+?)$', markdown_content, re.MULTILINE)
        return [(len(heading[0]), heading[1].strip()) for heading in headings]
    
    def extract_images(self, markdown_content):
        """提取Markdown中的图片"""
        import re
        # 匹配图片
        images = re.findall(r'!\[(.*?)\]\((.*?)\)', markdown_content)
        return [{'alt': alt, 'src': src} for alt, src in images]
    
    def extract_links(self, markdown_content):
        """提取Markdown中的链接"""
        import re
        # 匹配链接
        links = re.findall(r'\[(.*?)\]\((.*?)\)', markdown_content)
        return [{'text': text, 'url': url} for text, url in links]
    
    def convert_to_plain_text(self, markdown_content):
        """将Markdown转换为纯文本"""
        import re
        # 移除Markdown标记
        # 移除标题
        text = re.sub(r'#+\s+', '', markdown_content)
        # 移除粗体和斜体
        text = re.sub(r'\*\*(.*?)\*\*|\*(.*?)\*', r'\1\2', text)
        # 移除链接
        text = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', text)
        # 移除图片
        text = re.sub(r'!\[(.*?)\]\((.*?)\)', '', text)
        # 移除代码块
        text = re.sub(r'```[\s\S]*?```', '', text)
        # 移除行内代码
        text = re.sub(r'`(.*?)`', r'\1', text)
        # 移除引用
        text = re.sub(r'>\s+', '', text)
        return text.strip()