import subprocess
import os
import tempfile

class RichTextGenerator:
    def __init__(self):
        pass
    
    def convert_markdown(self, markdown_content):
        """将Markdown转换为富文本"""
        try:
            # 创建临时文件
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
                f.write(markdown_content)
                temp_file = f.name
            
            # 使用wenyan-cli转换
            output = subprocess.check_output(
                ['wenyan', 'convert', temp_file],
                universal_newlines=True
            )
            
            # 删除临时文件
            os.unlink(temp_file)
            
            return output
        except Exception as e:
            print(f"转换Markdown时出错: {e}")
            # 返回原始内容
            return markdown_content
    
    def beautify_layout(self, rich_content):
        """美化排版"""
        try:
            # 添加基本的排版样式
            beautified = rich_content
            
            # 添加标题样式
            beautified = beautified.replace('<h1>', '<h1 style="font-size: 24px; font-weight: bold; margin-top: 20px; margin-bottom: 10px;">')
            beautified = beautified.replace('<h2>', '<h2 style="font-size: 20px; font-weight: bold; margin-top: 16px; margin-bottom: 8px;">')
            beautified = beautified.replace('<h3>', '<h3 style="font-size: 18px; font-weight: bold; margin-top: 14px; margin-bottom: 6px;">')
            
            # 添加段落样式
            beautified = beautified.replace('<p>', '<p style="font-size: 16px; line-height: 1.6; margin-bottom: 12px;">')
            
            # 添加图片样式
            beautified = beautified.replace('<img ', '<img style="max-width: 100%; height: auto; margin: 10px 0;" ')
            
            # 添加代码块样式
            beautified = beautified.replace('<code>', '<code style="font-family: monospace; background-color: #f5f5f5; padding: 2px 4px; border-radius: 3px;">')
            beautified = beautified.replace('<pre>', '<pre style="font-family: monospace; background-color: #f5f5f5; padding: 10px; border-radius: 5px; overflow-x: auto;">')
            
            return beautified
        except Exception as e:
            print(f"美化排版时出错: {e}")
            return rich_content
    
    def process_images(self, rich_content, images):
        """处理文章中的图片"""
        try:

            # 暂时返回原始内容
            return rich_content
        except Exception as e:
            print(f"处理图片时出错: {e}")
            return rich_content
    
    def generate_rich_text(self, markdown_content, images=None):
        """生成富文本"""
        # 转换Markdown为富文本
        rich_content = self.convert_markdown(markdown_content)
        
        # 美化排版
        beautified_content = self.beautify_layout(rich_content)
        
        # 处理图片
        if images:
            final_content = self.process_images(beautified_content, images)
        else:
            final_content = beautified_content
        
        return final_content

# 测试代码
if __name__ == '__main__':
    generator = RichTextGenerator()
    
    # 测试Markdown转换
    print("测试Markdown转换...")
    markdown = """# 人工智能的发展趋势

人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。

## 技术突破

近年来，大语言模型如GPT系列的出现，使得AI在自然语言处理方面取得了重大突破。

## 行业应用

在医疗领域，AI可以帮助医生诊断疾病、预测患者风险、优化治疗方案。

![人工智能技术](https://example.com/image.jpg)
"""
    
    rich_text = generator.generate_rich_text(markdown)
    print(f"生成的富文本：{rich_text}")
