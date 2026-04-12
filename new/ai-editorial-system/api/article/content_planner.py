class ContentPlanner:
    def __init__(self):
        pass
    
    def plan_content(self, topic, materials):
        """规划文章内容结构"""
        # 分析选题和素材
        analysis = self._analyze_topic_and_materials(topic, materials)
        
        # 生成文章结构
        structure = self._generate_structure(analysis)
        
        return structure
    
    def _analyze_topic_and_materials(self, topic, materials):
        """分析选题和素材"""
        # 提取关键信息
        key_points = []
        for material in materials:
            # 提取素材中的关键信息
            if material.get('content'):
                # 简单地提取前几个句子作为关键点
                sentences = material['content'].split('\n')
                for sentence in sentences[:3]:  # 取前3句
                    if sentence.strip():
                        key_points.append(sentence.strip())
        
        # 去重
        key_points = list(set(key_points))
        
        return {
            'topic': topic,
            'key_points': key_points,
            'material_count': len(materials)
        }
    
    def _generate_structure(self, analysis):
        """生成文章结构"""
        # 基于分析结果生成文章结构
        structure = {
            'title': analysis['topic']['title'],
            'sections': [
                {
                    'title': '引言',
                    'content': f"介绍{analysis['topic']['title']}的背景和重要性"
                },
                {
                    'title': '核心内容',
                    'subsections': []
                },
                {
                    'title': '分析与见解',
                    'content': '基于素材分析，提供独特的见解'
                },
                {
                    'title': '结论',
                    'content': '总结文章要点，提出建议或展望'
                }
            ]
        }
        
        # 根据关键信息生成核心内容的子部分
        for i, key_point in enumerate(analysis['key_points'][:3]):  # 取前3个关键点
            structure['sections'][1]['subsections'].append({
                'title': f'关键点{i+1}',
                'content': key_point
            })
        
        return structure
    
    def generate_outline(self, structure):
        """生成文章大纲"""
        outline = f"# {structure['title']}\n\n"
        
        for section in structure['sections']:
            outline += f"## {section['title']}\n"
            if section.get('content'):
                outline += f"{section['content']}\n\n"
            if section.get('subsections'):
                for subsection in section['subsections']:
                    outline += f"### {subsection['title']}\n"
                    if subsection.get('content'):
                        outline += f"{subsection['content']}\n\n"
        
        return outline