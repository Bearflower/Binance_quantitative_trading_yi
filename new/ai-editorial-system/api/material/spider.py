import requests
from bs4 import BeautifulSoup
import time
import random
import urllib.parse

class MaterialSpider:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
    def crawl_materials(self, topic, max_results=10):
        """根据话题爬取素材"""
        materials = []
        
        # 构建搜索查询
        query = urllib.parse.quote(topic)
        
        # 从多个搜索引擎和新闻网站爬取素材
        sources = [
            {'name': '百度搜索', 'url': f'https://www.baidu.com/s?wd={query}'},
            {'name': '360搜索', 'url': f'https://www.so.com/s?q={query}'},
            {'name': '搜狗搜索', 'url': f'https://www.sogou.com/web?query={query}'},
        ]
        
        for source in sources:
            try:
                print(f"正在从 {source['name']} 爬取素材...")
                source_materials = self._crawl_source(source, topic, max_results // len(sources))
                materials.extend(source_materials)
                # 避免请求过于频繁
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                print(f"从 {source['name']} 爬取素材时出错: {e}")
        
        # 去重
        unique_materials = self._deduplicate_materials(materials)
        
        # 排序，按相关性降序
        sorted_materials = sorted(unique_materials, key=lambda x: x['relevance'], reverse=True)
        
        # 限制结果数量
        return sorted_materials[:max_results]
    
    def _crawl_source(self, source, topic, max_results):
        """从单个来源爬取素材"""
        materials = []
        
        if source['name'] == '百度搜索':
            materials = self._crawl_baidu(source['url'], topic, max_results)
        elif source['name'] == '360搜索':
            materials = self._crawl_so(source['url'], topic, max_results)
        elif source['name'] == '搜狗搜索':
            materials = self._crawl_sogou(source['url'], topic, max_results)
        
        return materials
    
    def _crawl_baidu(self, url, topic, max_results):
        """从百度搜索爬取素材"""
        materials = []
        
        response = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到搜索结果
        result_list = soup.find_all('div', class_='result')
        
        for i, item in enumerate(result_list):
            if i >= max_results:
                break
            
            title_elem = item.find('h3')
            if not title_elem:
                continue
            
            title = title_elem.text.strip()
            
            link_elem = title_elem.find('a')
            if not link_elem:
                continue
            
            link = link_elem.get('href')
            
            summary_elem = item.find('div', class_='c-abstract')
            summary = summary_elem.text.strip() if summary_elem else ''
            
            # 计算相关性（简单的关键词匹配）
            relevance = self._calculate_relevance(title + ' ' + summary, topic)
            
            materials.append({
                'title': title,
                'url': link,
                'content': summary,
                'source': '百度搜索',
                'relevance': relevance
            })
        
        return materials
    
    def _crawl_so(self, url, topic, max_results):
        """从360搜索爬取素材"""
        materials = []
        
        response = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到搜索结果
        result_list = soup.find_all('li', class_='res-list')
        
        for i, item in enumerate(result_list):
            if i >= max_results:
                break
            
            title_elem = item.find('h3')
            if not title_elem:
                continue
            
            title = title_elem.text.strip()
            
            link_elem = title_elem.find('a')
            if not link_elem:
                continue
            
            link = link_elem.get('href')
            
            summary_elem = item.find('p', class_='res-desc')
            summary = summary_elem.text.strip() if summary_elem else ''
            
            # 计算相关性（简单的关键词匹配）
            relevance = self._calculate_relevance(title + ' ' + summary, topic)
            
            materials.append({
                'title': title,
                'url': link,
                'content': summary,
                'source': '360搜索',
                'relevance': relevance
            })
        
        return materials
    
    def _crawl_sogou(self, url, topic, max_results):
        """从搜狗搜索爬取素材"""
        materials = []
        
        response = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到搜索结果
        result_list = soup.find_all('div', class_='vrwrap')
        
        for i, item in enumerate(result_list):
            if i >= max_results:
                break
            
            title_elem = item.find('h3')
            if not title_elem:
                continue
            
            title = title_elem.text.strip()
            
            link_elem = title_elem.find('a')
            if not link_elem:
                continue
            
            link = link_elem.get('href')
            
            summary_elem = item.find('p', class_='str-text')
            summary = summary_elem.text.strip() if summary_elem else ''
            
            # 计算相关性（简单的关键词匹配）
            relevance = self._calculate_relevance(title + ' ' + summary, topic)
            
            materials.append({
                'title': title,
                'url': link,
                'content': summary,
                'source': '搜狗搜索',
                'relevance': relevance
            })
        
        return materials
    
    def _deduplicate_materials(self, materials):
        """去重素材"""
        seen_urls = set()
        unique_materials = []
        
        for material in materials:
            url = material['url']
            if url not in seen_urls:
                seen_urls.add(url)
                unique_materials.append(material)
        
        return unique_materials
    
    def _calculate_relevance(self, text, topic):
        """计算文本与话题的相关性"""
        # 简单的关键词匹配算法
        topic_words = topic.split()
        text_lower = text.lower()
        
        matched_words = 0
        for word in topic_words:
            if word.lower() in text_lower:
                matched_words += 1
        
        if not topic_words:
            return 0.0
        
        return matched_words / len(topic_words)
    
    def parse_material(self, url):
        """解析素材内容"""
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 提取标题
            title_elem = soup.find('h1')
            title = title_elem.text.strip() if title_elem else ''
            
            # 提取正文
            content_elem = soup.find('div', class_='article-content')
            if not content_elem:
                content_elem = soup.find('div', class_='content')
            if not content_elem:
                content_elem = soup.find('article')
            
            content = ''
            if content_elem:
                paragraphs = content_elem.find_all('p')
                content = '\n'.join([p.text.strip() for p in paragraphs])
            
            return {
                'title': title,
                'content': content
            }
        except Exception as e:
            print(f"解析素材 {url} 时出错: {e}")
            return {
                'title': '',
                'content': ''
            }
    
    def integrate_materials(self, materials):
        """整合素材"""
        integrated = {
            'title': '',
            'content': '',
            'sources': []
        }
        
        # 选择相关性最高的素材作为标题来源
        if materials:
            top_material = max(materials, key=lambda x: x['relevance'])
            integrated['title'] = top_material['title']
        
        # 整合内容
        for material in materials:
            if material['content']:
                integrated['content'] += material['content'] + '\n\n'
            integrated['sources'].append({
                'title': material['title'],
                'url': material['url'],
                'source': material['source']
            })
        
        # 去除多余的空行
        integrated['content'] = integrated['content'].strip()
        
        return integrated

# 测试代码
if __name__ == '__main__':
    spider = MaterialSpider()
    
    # 爬取素材
    print("开始爬取素材...")
    materials = spider.crawl_materials('人工智能的发展趋势', max_results=5)
    
    print(f"共爬取到 {len(materials)} 个素材")
    for i, material in enumerate(materials):
        print(f"{i+1}. {material['title']} (相关性: {material['relevance']:.2f}, 来源: {material['source']})")
    
    # 解析素材
    if materials:
        print("\n解析第一个素材...")
        parsed = spider.parse_material(materials[0]['url'])
        print(f"标题: {parsed['title']}")
        print(f"内容: {parsed['content'][:200]}...")
    
    # 整合素材
    print("\n整合素材...")
    integrated = spider.integrate_materials(materials)
    print(f"整合后的标题: {integrated['title']}")
    print(f"整合后的内容: {integrated['content'][:200]}...")
    print(f"素材来源数量: {len(integrated['sources'])}")
