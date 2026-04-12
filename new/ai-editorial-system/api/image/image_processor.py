from PIL import Image
import os

class ImageProcessor:
    def __init__(self):
        pass
    
    def process_image(self, image_path, output_path=None, size=None, format='PNG'):
        """处理图片"""
        try:
            # 打开图片
            img = Image.open(image_path)
            
            # 调整尺寸
            if size:
                img = self._resize_image(img, size)
            
            # 转换格式
            if format != img.format:
                img = self._convert_format(img, format)
            
            # 优化图片
            img = self._optimize_image(img)
            
            # 保存图片
            if output_path:
                img.save(output_path, optimize=True, quality=90)
            else:
                img.save(image_path, optimize=True, quality=90)
            
            return output_path or image_path
        except Exception as e:
            print(f"Error processing image: {e}")
            return None
    
    def _resize_image(self, img, size):
        """调整图片尺寸"""
        # 保持宽高比
        img.thumbnail(size, Image.Resampling.LANCZOS)
        return img
    
    def _convert_format(self, img, format):
        """转换图片格式"""
        return img.convert('RGB') if format == 'JPEG' else img
    
    def _optimize_image(self, img):
        """优化图片"""
        # 简单的优化，实际项目中可能需要更复杂的优化
        return img
    
    def crop_image(self, image_path, output_path, box):
        """裁剪图片"""
        try:
            img = Image.open(image_path)
            cropped_img = img.crop(box)
            cropped_img.save(output_path, optimize=True, quality=90)
            return output_path
        except Exception as e:
            print(f"Error cropping image: {e}")
            return None
    
    def rotate_image(self, image_path, output_path, angle):
        """旋转图片"""
        try:
            img = Image.open(image_path)
            rotated_img = img.rotate(angle, expand=True)
            rotated_img.save(output_path, optimize=True, quality=90)
            return output_path
        except Exception as e:
            print(f"Error rotating image: {e}")
            return None
    
    def watermark_image(self, image_path, output_path, watermark_text):
        """添加水印"""
        try:
            from PIL import ImageDraw, ImageFont
            
            img = Image.open(image_path)
            draw = ImageDraw.Draw(img)
            
            # 尝试使用系统字体
            try:
                font = ImageFont.truetype('Arial', 20)
            except:
                font = ImageFont.load_default()
            
            # 计算水印位置
            text_width, text_height = draw.textsize(watermark_text, font=font)
            x = img.width - text_width - 10
            y = img.height - text_height - 10
            
            # 添加水印
            draw.text((x, y), watermark_text, fill=(255, 255, 255, 128), font=font)
            
            img.save(output_path, optimize=True, quality=90)
            return output_path
        except Exception as e:
            print(f"Error adding watermark: {e}")
            return None