import os
import requests
import base64
import json
from PIL import Image
from io import BytesIO

class ImageGenerator:
    def __init__(self, api_url='http://localhost:7860'):
        self.api_url = api_url
    
    def generate_image(self, prompt, size=(1024, 1024), num=1):
        """生成图像"""
        try:
            # 构建请求数据
            data = {
                "prompt": prompt,
                "width": size[0],
                "height": size[1],
                "n_samples": num
            }
            
            # 调用Stable Diffusion API
            response = requests.post(
                f"{self.api_url}/sdapi/v1/txt2img",
                json=data
            )
            
            if response.status_code != 200:
                raise Exception(f"Stable Diffusion API调用失败: {response.text}")
            
            result = response.json()
            
            if 'images' not in result:
                raise Exception("Stable Diffusion API返回结果格式错误")
            
            # 处理生成的图像
            images = []
            for i, img_data in enumerate(result['images']):
                # 解码base64图像数据
                img_bytes = base64.b64decode(img_data)
                img = Image.open(BytesIO(img_bytes))
                images.append(img)
            
            return images
        except Exception as e:
            print(f"生成图像时出错: {e}")
            # 返回默认图像
            return [self._get_default_image()]
    
    def optimize_image(self, image, size=(800, 600)):
        """优化图像"""
        try:
            # 调整图像大小
            resized_image = image.resize(size, Image.LANCZOS)
            
            # 转换为RGB模式
            if resized_image.mode != 'RGB':
                resized_image = resized_image.convert('RGB')
            
            return resized_image
        except Exception as e:
            print(f"优化图像时出错: {e}")
            return image
    
    def save_image(self, image, path):
        """保存图像"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(path), exist_ok=True)
            
            # 保存图像
            image.save(path)
            return True
        except Exception as e:
            print(f"保存图像时出错: {e}")
            return False
    
    def generate_article_images(self, article_title, article_content, num=1):
        """生成文章配图"""
        # 提取文章关键词
        keywords = self._extract_keywords(article_title + ' ' + article_content)
        
        # 构建prompt
        prompt = f"{article_title}, {', '.join(keywords)}, 高质量, 清晰, 专业, 适合公众号配图"
        
        # 生成图像
        images = self.generate_image(prompt, num=num)
        
        # 优化图像
        optimized_images = [self.optimize_image(img) for img in images]
        
        return optimized_images
    
    def _extract_keywords(self, text, num=5):
        """提取关键词"""
        # 简单的关键词提取（实际项目中可以使用更复杂的NLP方法）
        words = text.split()
        # 过滤掉常见虚词
        stop_words = {'的', '了', '和', '与', '或', '是', '在', '有', '为', '以', '我们', '你', '他', '她', '它', '这', '那', '并', '但', '而', '如果', '因为', '所以', '虽然', '但是'}
        filtered_words = [word for word in words if word not in stop_words and len(word) > 1]
        
        # 简单的词频统计
        word_freq = {}
        for word in filtered_words:
            word_freq[word] = word_freq.get(word, 0) + 1
        
        # 按词频排序，取前num个
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        keywords = [word for word, _ in sorted_words[:num]]
        
        return keywords
    
    def _get_default_image(self):
        """获取默认图像"""
        # 创建一个简单的默认图像
        img = Image.new('RGB', (1024, 1024), color='white')
        return img

# 测试代码
if __name__ == '__main__':
    generator = ImageGenerator()
    
    # 测试生成图像
    print("测试生成图像...")
    try:
        images = generator.generate_image(
            "人工智能技术，未来科技，机器人，高科技，蓝色调",
            size=(1024, 1024),
            num=1
        )
        print(f"生成了 {len(images)} 张图像")
        
        # 保存图像
        for i, img in enumerate(images):
            save_path = f"test_image_{i}.png"
            if generator.save_image(img, save_path):
                print(f"图像已保存到: {save_path}")
            else:
                print("图像保存失败")
        
        # 测试生成文章配图
        print("\n测试生成文章配图...")
        article_title = "人工智能的发展趋势"
        article_content = "人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。AI在医疗、金融、教育等领域的应用越来越广泛，为这些行业带来了新的机遇和挑战。"
        article_images = generator.generate_article_images(article_title, article_content, num=1)
        print(f"生成了 {len(article_images)} 张文章配图")
        
        # 保存文章配图
        for i, img in enumerate(article_images):
            save_path = f"article_image_{i}.png"
            if generator.save_image(img, save_path):
                print(f"文章配图已保存到: {save_path}")
            else:
                print("文章配图保存失败")
    except Exception as e:
        print(f"测试失败: {e}")
