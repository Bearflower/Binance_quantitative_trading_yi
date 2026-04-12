import requests
import json
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

class AIContentGenerator:
    def __init__(self):
        self.api_key = os.getenv('DEEPSEEK_API_KEY', '')
        self.base_url = 'https://api.deepseek.com/v1/chat/completions'
    
    def generate_article(self, topic, materials=None, length=1000):
        """生成文章"""
        if not self.api_key:
            raise ValueError("DeepSeek API密钥未设置")
        
        # 构建素材内容
        material_content = ""
        if materials:
            for material in materials:
                if material.get('content'):
                    material_content += material['content'] + '\n\n'
        
        # 构建prompt
        prompt = f"请根据以下话题和素材，生成一篇{length}字左右的文章。文章应该结构清晰，逻辑连贯，语言流畅，符合公众号的风格。\n\n话题：{topic}\n\n素材：{material_content}\n\n文章："
        
        # 调用DeepSeek API
        response = self._call_deepseek_api(prompt)
        
        return response
    
    def generate_title(self, content, num=3):
        """生成文章标题"""
        if not self.api_key:
            raise ValueError("DeepSeek API密钥未设置")
        
        # 构建prompt
        prompt = f"请根据以下文章内容，生成{num}个吸引人的标题。标题应该简洁明了，能够概括文章的主要内容，符合公众号的风格。\n\n文章内容：{content[:1000]}\n\n标题："
        
        # 调用DeepSeek API
        response = self._call_deepseek_api(prompt)
        
        # 解析标题
        titles = [title.strip() for title in response.split('\n') if title.strip()]
        
        return titles[:num]
    
    def optimize_content(self, content):
        """优化文章内容"""
        if not self.api_key:
            raise ValueError("DeepSeek API密钥未设置")
        
        # 构建prompt
        prompt = f"请优化以下文章内容，使其结构更清晰，逻辑更连贯，语言更流畅，符合公众号的风格。\n\n文章内容：{content}\n\n优化后的文章："
        
        # 调用DeepSeek API
        response = self._call_deepseek_api(prompt)
        
        return response
    
    def _call_deepseek_api(self, prompt):
        """调用DeepSeek API"""
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}'
        }
        
        data = {
            'model': 'deepseek-chat',
            'messages': [
                {
                    'role': 'user',
                    'content': prompt
                }
            ],
            'temperature': 0.7,
            'max_tokens': 2048
        }
        
        response = requests.post(self.base_url, headers=headers, data=json.dumps(data))
        
        if response.status_code != 200:
            raise Exception(f"DeepSeek API调用失败: {response.text}")
        
        result = response.json()
        
        if 'choices' not in result or not result['choices']:
            raise Exception("DeepSeek API返回结果格式错误")
        
        return result['choices'][0]['message']['content']

# 测试代码
if __name__ == '__main__':
    generator = AIContentGenerator()
    
    # 测试生成文章
    print("测试生成文章...")
    try:
        article = generator.generate_article(
            '人工智能的发展趋势',
            [
                {'content': '人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。'},
                {'content': 'AI在医疗、金融、教育等领域的应用越来越广泛，为这些行业带来了新的机遇和挑战。'}
            ],
            length=500
        )
        print(f"生成的文章：{article}")
        
        # 测试生成标题
        print("\n测试生成标题...")
        titles = generator.generate_title(article, num=3)
        print(f"生成的标题：{titles}")
        
        # 测试优化内容
        print("\n测试优化内容...")
        optimized = generator.optimize_content(article)
        print(f"优化后的文章：{optimized}")
    except Exception as e:
        print(f"测试失败: {e}")
