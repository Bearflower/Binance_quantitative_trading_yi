import requests
from bs4 import BeautifulSoup
import time
import random

class HotspotMonitor:
    def __init__(self):
        self.sources = [
            {'name': '百度热搜', 'url': 'https://top.baidu.com/board?tab=realtime'},
            {'name': '微博热搜', 'url': 'https://s.weibo.com/top/summary'},
            {'name': '知乎热榜', 'url': 'https://www.zhihu.com/hot'},
        ]
    
    def scan_hotspots(self):
        """扫描热点话题"""
        hotspots = []
        
        for source in self.sources:
            try:
                print(f"正在扫描 {source['name']}...")
                source_hotspots = self._scan_source(source)
                hotspots.extend(source_hotspots)
                # 避免请求过于频繁
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                print(f"扫描 {source['name']} 时出错: {e}")
        
        # 去重
        unique_hotspots = self._deduplicate_hotspots(hotspots)
        
        # 排序，按热度降序
        sorted_hotspots = sorted(unique_hotspots, key=lambda x: x['heat'], reverse=True)
        
        return sorted_hotspots
    
    def _scan_source(self, source):
        """扫描单个来源的热点话题"""
        hotspots = []
        
        if source['name'] == '百度热搜':
            hotspots = self._scan_baidu(source['url'])
        elif source['name'] == '微博热搜':
            hotspots = self._scan_weibo(source['url'])
        elif source['name'] == '知乎热榜':
            hotspots = self._scan_zhihu(source['url'])
        
        return hotspots
    
    def _scan_baidu(self, url):
        """扫描百度热搜"""
        hotspots = []
        
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到热搜列表
        hot_list = soup.find_all('div', class_='c-single-text-ellipsis')
        
        for i, item in enumerate(hot_list):
            title = item.text.strip()
            # 热度值根据排名计算，排名越靠前热度越高
            heat = 1.0 - (i / len(hot_list))
            
            hotspots.append({
                'title': title,
                'heat': heat,
                'source': '百度热搜',
                'rank': i + 1
            })
        
        return hotspots
    
    def _scan_weibo(self, url):
        """扫描微博热搜"""
        hotspots = []
        
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到热搜列表
        hot_list = soup.find_all('tr', class_='')
        
        for i, item in enumerate(hot_list):
            title_elem = item.find('td', class_='td-02')
            if title_elem:
                title = title_elem.text.strip()
                # 热度值根据排名计算，排名越靠前热度越高
                heat = 1.0 - (i / len(hot_list))
                
                hotspots.append({
                    'title': title,
                    'heat': heat,
                    'source': '微博热搜',
                    'rank': i + 1
                })
        
        return hotspots
    
    def _scan_zhihu(self, url):
        """扫描知乎热榜"""
        hotspots = []
        
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找到热榜列表
        hot_list = soup.find_all('div', class_='HotList-item')
        
        for i, item in enumerate(hot_list):
            title_elem = item.find('a', class_='HotList-itemTitle')
            if title_elem:
                title = title_elem.text.strip()
                # 热度值根据排名计算，排名越靠前热度越高
                heat = 1.0 - (i / len(hot_list))
                
                hotspots.append({
                    'title': title,
                    'heat': heat,
                    'source': '知乎热榜',
                    'rank': i + 1
                })
        
        return hotspots
    
    def _deduplicate_hotspots(self, hotspots):
        """去重热点话题"""
        seen_titles = set()
        unique_hotspots = []
        
        for hotspot in hotspots:
            title = hotspot['title']
            if title not in seen_titles:
                seen_titles.add(title)
                unique_hotspots.append(hotspot)
        
        return unique_hotspots
    
    def analyze_topics(self, hotspots, topic_type=None):
        """分析热点话题，选择适合的选题"""
        # 过滤掉不适合的话题
        filtered_hotspots = self._filter_hotspots(hotspots, topic_type)
        
        # 对过滤后的话题进行排序
        sorted_hotspots = sorted(filtered_hotspots, key=lambda x: x['heat'], reverse=True)
        
        # 选择前10个话题作为候选选题
        candidate_topics = sorted_hotspots[:10]
        
        return candidate_topics
    
    def _filter_hotspots(self, hotspots, topic_type):
        """过滤热点话题"""
        filtered = []
        
        for hotspot in hotspots:
            # 过滤掉过于简短的话题
            if len(hotspot['title']) < 4:
                continue
            
            # 过滤出娱乐或书籍引申的个人成长或心灵方面的思考的话题
            if self._is_entertainment_topic(hotspot['title']) or \
               self._is_book_topic(hotspot['title']) or \
               self._is_growth_topic(hotspot['title']):
                filtered.append(hotspot)
        
        return filtered
    
    def _is_entertainment_topic(self, title):
        """判断是否为娱乐话题"""
        entertainment_keywords = ['娱乐', '明星', '电影', '电视剧', '综艺', '音乐', '演唱会', '八卦', '绯闻', '娱乐圈']
        return any(keyword in title for keyword in entertainment_keywords)
    
    def _is_book_topic(self, title):
        """判断是否为书籍话题"""
        book_keywords = ['书籍', '读书', '阅读', '小说', '作家', '文学', '出版社', '书店', '书单', '读后感']
        return any(keyword in title for keyword in book_keywords)
    
    def _is_growth_topic(self, title):
        """判断是否为个人成长话题"""
        growth_keywords = ['成长', '心灵', '思考', '人生', '感悟', '自我提升', '心理健康', '情绪管理', '人际关系', '职业发展']
        return any(keyword in title for keyword in growth_keywords)

# 测试代码
if __name__ == '__main__':
    monitor = HotspotMonitor()
    
    # 扫描热点话题
    print("开始扫描热点话题...")
    hotspots = monitor.scan_hotspots()
    
    print(f"共扫描到 {len(hotspots)} 个热点话题")
    print("前10个热点话题：")
    for i, hotspot in enumerate(hotspots[:10]):
        print(f"{i+1}. {hotspot['title']} (热度: {hotspot['heat']:.2f}, 来源: {hotspot['source']})")
    
    # 分析科技类话题
    print("\n分析科技类话题...")
    tech_topics = monitor.analyze_topics(hotspots, 'tech')
    print(f"共找到 {len(tech_topics)} 个科技类话题")
    for i, topic in enumerate(tech_topics):
        print(f"{i+1}. {topic['title']} (热度: {topic['heat']:.2f}, 来源: {topic['source']})")
