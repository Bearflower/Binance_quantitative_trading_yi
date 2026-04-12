class StyleApplier:
    def __init__(self):
        # 预定义的样式模板
        self.templates = {
            'default': {
                'font_family': 'PingFang SC, Microsoft YaHei, sans-serif',
                'font_size': '16px',
                'line_height': '1.6',
                'color': '#333333',
                'heading1': {
                    'font_size': '24px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '20px'
                },
                'heading2': {
                    'font_size': '20px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '16px'
                },
                'heading3': {
                    'font_size': '18px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '12px'
                },
                'paragraph': {
                    'margin_bottom': '16px'
                },
                'image': {
                    'max_width': '100%',
                    'margin': '20px 0'
                },
                'code': {
                    'background_color': '#f5f5f5',
                    'padding': '2px 4px',
                    'border-radius': '4px',
                    'font_family': 'Consolas, Monaco, monospace'
                },
                'blockquote': {
                    'border_left': '4px solid #ddd',
                    'padding_left': '16px',
                    'color': '#666',
                    'margin': '20px 0'
                }
            },
            'simple': {
                'font_family': 'PingFang SC, Microsoft YaHei, sans-serif',
                'font_size': '15px',
                'line_height': '1.5',
                'color': '#333333',
                'heading1': {
                    'font_size': '22px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '16px'
                },
                'heading2': {
                    'font_size': '18px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '12px'
                },
                'heading3': {
                    'font_size': '16px',
                    'font_weight': 'bold',
                    'color': '#000000',
                    'margin_bottom': '10px'
                },
                'paragraph': {
                    'margin_bottom': '14px'
                },
                'image': {
                    'max_width': '100%',
                    'margin': '16px 0'
                },
                'code': {
                    'background_color': '#f5f5f5',
                    'padding': '2px 4px',
                    'border-radius': '4px',
                    'font_family': 'Consolas, Monaco, monospace'
                },
                'blockquote': {
                    'border_left': '4px solid #ddd',
                    'padding_left': '12px',
                    'color': '#666',
                    'margin': '16px 0'
                }
            }
        }
    
    def apply_style(self, html_content, template_name='default'):
        """应用样式"""
        if template_name not in self.templates:
            template_name = 'default'
        
        template = self.templates[template_name]
        
        # 生成样式CSS
        css = self._generate_css(template)
        
        # 包装HTML内容
        styled_content = f"""
        <style>
        {css}
        </style>
        {html_content}
        """
        
        return styled_content
    
    def _generate_css(self, template):
        """生成CSS样式"""
        css = f"""
        body {{
            font-family: {template['font_family']};
            font-size: {template['font_size']};
            line-height: {template['line_height']};
            color: {template['color']};
        }}
        
        h1 {{
            font-size: {template['heading1']['font_size']};
            font-weight: {template['heading1']['font_weight']};
            color: {template['heading1']['color']};
            margin-bottom: {template['heading1']['margin_bottom']};
        }}
        
        h2 {{
            font-size: {template['heading2']['font_size']};
            font-weight: {template['heading2']['font_weight']};
            color: {template['heading2']['color']};
            margin-bottom: {template['heading2']['margin_bottom']};
        }}
        
        h3 {{
            font-size: {template['heading3']['font_size']};
            font-weight: {template['heading3']['font_weight']};
            color: {template['heading3']['color']};
            margin-bottom: {template['heading3']['margin_bottom']};
        }}
        
        p {{
            margin-bottom: {template['paragraph']['margin_bottom']};
        }}
        
        img {{
            max-width: {template['image']['max_width']};
            margin: {template['image']['margin']};
        }}
        
        code {{
            background-color: {template['code']['background_color']};
            padding: {template['code']['padding']};
            border-radius: {template['code']['border-radius']};
            font-family: {template['code']['font_family']};
        }}
        
        blockquote {{
            border-left: {template['blockquote']['border_left']};
            padding-left: {template['blockquote']['padding_left']};
            color: {template['blockquote']['color']};
            margin: {template['blockquote']['margin']};
        }}
        """
        
        return css
    
    def create_custom_template(self, template_name, styles):
        """创建自定义模板"""
        self.templates[template_name] = styles
        return True
    
    def list_templates(self):
        """列出所有可用模板"""
        return list(self.templates.keys())