import tempfile
import os
import subprocess

class PreviewGenerator:
    def __init__(self):
        pass
    
    def generate_preview(self, content, format='html'):
        """生成预览"""
        if format == 'html':
            return self._generate_html_preview(content)
        elif format == 'pdf':
            return self._generate_pdf_preview(content)
        else:
            return None
    
    def _generate_html_preview(self, content):
        """生成HTML预览"""
        # 创建临时HTML文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(content)
            temp_file = f.name
        
        # 打开浏览器预览
        try:
            subprocess.run(['open', temp_file])
        except Exception as e:
            print(f"Error opening preview: {e}")
        
        return temp_file
    
    def _generate_pdf_preview(self, content):
        """生成PDF预览"""
        # 创建临时HTML文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(content)
            html_file = f.name
        
        # 生成PDF文件
        pdf_file = html_file.replace('.html', '.pdf')
        
        try:
            # 使用wkhtmltopdf生成PDF
            # 注意：需要安装wkhtmltopdf
            subprocess.run(['wkhtmltopdf', html_file, pdf_file])
            
            # 打开PDF文件
            subprocess.run(['open', pdf_file])
        except Exception as e:
            print(f"Error generating PDF preview: {e}")
            return None
        finally:
            # 清理临时文件
            if os.path.exists(html_file):
                os.unlink(html_file)
        
        return pdf_file
    
    def generate_mobile_preview(self, content):
        """生成移动端预览"""
        # 创建响应式HTML
        responsive_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                    font-family: PingFang SC, Microsoft YaHei, sans-serif;
                }
            </style>
        </head>
        <body>
            {content}
        </body>
        </html>
        """
        
        return self._generate_html_preview(responsive_content)
    
    def cleanup_preview(self, preview_file):
        """清理预览文件"""
        if preview_file and os.path.exists(preview_file):
            try:
                os.unlink(preview_file)
                return True
            except Exception as e:
                print(f"Error cleaning up preview file: {e}")
                return False
        return False