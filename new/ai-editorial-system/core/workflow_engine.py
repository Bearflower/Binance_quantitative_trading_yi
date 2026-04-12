import time
from core.task_manager import TaskManager
from api.publish.wechat_api_client import FeishuClient

class WorkflowEngine:
    def __init__(self):
        self.task_manager = TaskManager()
        self.feishu_client = FeishuClient()
    
    def execute_workflow(self, workflow_type, parameters=None):
        """执行工作流程"""
        # 创建工作流任务
        workflow_task_id = self.task_manager.create_task(
            f"{workflow_type}工作流", 
            f"workflow_{workflow_type}",
            parameters
        )
        
        # 更新任务状态为执行中
        self.task_manager.update_task_status(workflow_task_id, 'in_progress')
        
        try:
            if workflow_type == 'article_creation':
                result = self._execute_article_creation_workflow(parameters)
            elif workflow_type == 'topic_monitoring':
                result = self._execute_topic_monitoring_workflow(parameters)
            elif workflow_type == 'publish':
                result = self._execute_publish_workflow(parameters)
            else:
                raise ValueError(f"未知的工作流类型: {workflow_type}")
            
            # 更新任务状态为完成
            self.task_manager.update_task_status(workflow_task_id, 'completed')
            self.task_manager.update_task_result(workflow_task_id, result)
            
            return {
                'workflow_task_id': workflow_task_id,
                'status': 'completed',
                'result': result
            }
        except Exception as e:
            # 更新任务状态为失败
            self.task_manager.update_task_status(workflow_task_id, 'failed')
            self.task_manager.update_task_result(workflow_task_id, {
                'error': str(e)
            })
            
            return {
                'workflow_task_id': workflow_task_id,
                'status': 'failed',
                'error': str(e)
            }
    
    def _execute_article_creation_workflow(self, parameters):
        """执行文章创建工作流程"""
        # 1. 热点监测与选题
        print("开始热点监测与选题...")
        topic_task_id = self.task_manager.create_task(
            "热点监测与选题",
            "topic_monitoring",
            parameters.get('topic_parameters', {})
        )
        self.task_manager.update_task_status(topic_task_id, 'in_progress')
        
        # 模拟热点监测与选题过程
        time.sleep(2)
        
        # 模拟选题结果
        topic_result = {
            'topic_id': 1,
            'title': '人工智能的发展趋势',
            'description': '探讨人工智能技术的最新发展趋势和应用场景',
            'heat': 0.9,
            'source': '热点监测'
        }
        
        self.task_manager.update_task_status(topic_task_id, 'completed')
        self.task_manager.update_task_result(topic_task_id, topic_result)
        
        # 2. 素材收集
        print("开始素材收集...")
        material_task_id = self.task_manager.create_task(
            "素材收集",
            "material_collection",
            {
                'topic_id': topic_result['topic_id'],
                'topic_title': topic_result['title']
            }
        )
        self.task_manager.update_task_status(material_task_id, 'in_progress')
        
        # 模拟素材收集过程
        time.sleep(3)
        
        # 模拟素材收集结果
        material_result = {
            'materials': [
                {
                    'id': 1,
                    'type': 'text',
                    'content': '人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。',
                    'source': '科技新闻'
                },
                {
                    'id': 2,
                    'type': 'text',
                    'content': 'AI在医疗、金融、教育等领域的应用越来越广泛，为这些行业带来了新的机遇和挑战。',
                    'source': '行业报告'
                }
            ]
        }
        
        self.task_manager.update_task_status(material_task_id, 'completed')
        self.task_manager.update_task_result(material_task_id, material_result)
        
        # 3. 内容生成
        print("开始内容生成...")
        content_task_id = self.task_manager.create_task(
            "内容生成",
            "content_generation",
            {
                'topic_id': topic_result['topic_id'],
                'topic_title': topic_result['title'],
                'materials': material_result['materials']
            }
        )
        self.task_manager.update_task_status(content_task_id, 'in_progress')
        
        # 模拟内容生成过程
        time.sleep(5)
        
        # 模拟内容生成结果
        content_result = {
            'article_id': 1,
            'title': '人工智能的发展趋势：从技术突破到行业应用',
            'content': '# 人工智能的发展趋势\n\n人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。AI在医疗、金融、教育等领域的应用越来越广泛，为这些行业带来了新的机遇和挑战。\n\n## 技术突破\n\n近年来，大语言模型如GPT系列的出现，使得AI在自然语言处理方面取得了重大突破。这些模型能够理解和生成人类语言，完成各种任务，如文本生成、翻译、问答等。\n\n## 行业应用\n\n在医疗领域，AI可以帮助医生诊断疾病、预测患者风险、优化治疗方案。在金融领域，AI可以用于 fraud detection、风险评估、自动化交易等。在教育领域，AI可以提供个性化学习体验、自动批改作业、智能辅导等。\n\n## 未来展望\n\n未来，人工智能技术将继续发展，在更多领域得到应用。同时，我们也需要关注AI带来的伦理和社会问题，确保AI的发展符合人类的利益。',
            'status': 'generated'
        }
        
        self.task_manager.update_task_status(content_task_id, 'completed')
        self.task_manager.update_task_result(content_task_id, content_result)
        
        # 4. 配图生成
        print("开始配图生成...")
        image_task_id = self.task_manager.create_task(
            "配图生成",
            "image_generation",
            {
                'article_id': content_result['article_id'],
                'article_title': content_result['title'],
                'article_content': content_result['content']
            }
        )
        self.task_manager.update_task_status(image_task_id, 'in_progress')
        
        # 模拟配图生成过程
        time.sleep(4)
        
        # 模拟配图生成结果
        image_result = {
            'images': [
                {
                    'id': 1,
                    'url': 'https://example.com/image1.jpg',
                    'path': '/path/to/image1.jpg',
                    'description': '人工智能技术示意图'
                }
            ]
        }
        
        self.task_manager.update_task_status(image_task_id, 'completed')
        self.task_manager.update_task_result(image_task_id, image_result)
        
        # 5. 排版
        print("开始排版...")
        layout_task_id = self.task_manager.create_task(
            "排版",
            "layout",
            {
                'article_id': content_result['article_id'],
                'article_content': content_result['content'],
                'images': image_result['images']
            }
        )
        self.task_manager.update_task_status(layout_task_id, 'in_progress')
        
        # 模拟排版过程
        time.sleep(2)
        
        # 模拟排版结果
        layout_result = {
            'article_id': content_result['article_id'],
            'rich_content': '<h1>人工智能的发展趋势：从技术突破到行业应用</h1><p>人工智能技术在过去几年取得了显著进展，特别是在大语言模型和计算机视觉领域。AI在医疗、金融、教育等领域的应用越来越广泛，为这些行业带来了新的机遇和挑战。</p><h2>技术突破</h2><p>近年来，大语言模型如GPT系列的出现，使得AI在自然语言处理方面取得了重大突破。这些模型能够理解和生成人类语言，完成各种任务，如文本生成、翻译、问答等。</p><h2>行业应用</h2><p>在医疗领域，AI可以帮助医生诊断疾病、预测患者风险、优化治疗方案。在金融领域，AI可以用于 fraud detection、风险评估、自动化交易等。在教育领域，AI可以提供个性化学习体验、自动批改作业、智能辅导等。</p><h2>未来展望</h2><p>未来，人工智能技术将继续发展，在更多领域得到应用。同时，我们也需要关注AI带来的伦理和社会问题，确保AI的发展符合人类的利益。</p><img src="https://example.com/image1.jpg" alt="人工智能技术示意图">',
            'status': 'formatted'
        }
        
        self.task_manager.update_task_status(layout_task_id, 'completed')
        self.task_manager.update_task_result(layout_task_id, layout_result)
        
        # 6. 发布到飞书
        print("开始发布到飞书...")
        publish_task_id = self.task_manager.create_task(
            "发布到飞书",
            "publish",
            {
                'article_id': content_result['article_id'],
                'article_title': content_result['title'],
                'rich_content': layout_result['rich_content'],
                'images': image_result['images']
            }
        )
        self.task_manager.update_task_status(publish_task_id, 'in_progress')
        
        # 发送飞书消息
        time.sleep(2)
        try:
            message_title = f"【AI编辑部】新文章生成完成"
            message_content = f"标题：{content_result['title']}\n\n内容：{content_result['content'][:200]}..."
            self.feishu_client.send_message(message_title, message_content)
            print("飞书消息发送成功")
        except Exception as e:
            print(f"飞书消息发送失败: {e}")
        
        # 创建飞书文档存储完整内容
        time.sleep(2)
        doc_result = None
        try:
            doc_title = content_result['title']
            doc_content = layout_result['rich_content']
            doc_result = self.feishu_client.create_doc(doc_title, doc_content)
            print("飞书文档创建成功")
        except Exception as e:
            print(f"飞书文档创建失败: {e}")
        
        # 在飞书多维表格中存储文档链接和基本信息
        time.sleep(2)
        try:
            sheet_id = "growth_articles"
            record = {
                "title": content_result['title'],
                "doc_url": doc_result['data']['doc_url'] if doc_result else "",
                "doc_id": doc_result['data']['doc_id'] if doc_result else "",
                "summary": content_result['content'][:100] + "...",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.feishu_client.create_sheet_record(sheet_id, record)
            print("飞书多维表格记录创建成功")
        except Exception as e:
            print(f"飞书多维表格记录创建失败: {e}")
        
        # 模拟发布结果
        publish_result = {
            'publish_id': 1,
            'status': 'published',
            'platform': 'feishu',
            'message_sent': True,
            'sheet_record_created': True
        }
        
        self.task_manager.update_task_status(publish_task_id, 'completed')
        self.task_manager.update_task_result(publish_task_id, publish_result)
        
        # 返回工作流结果
        return {
            'topic': topic_result,
            'materials': material_result,
            'content': content_result,
            'images': image_result,
            'layout': layout_result,
            'publish': publish_result
        }
    
    def _execute_topic_monitoring_workflow(self, parameters):
        """执行热点监测工作流程"""
        # 模拟热点监测过程
        time.sleep(3)
        
        # 模拟热点监测结果
        return {
            'hotspots': [
                {
                    'title': '人工智能的发展趋势',
                    'heat': 0.9,
                    'source': '科技新闻'
                },
                {
                    'title': '新能源汽车市场分析',
                    'heat': 0.8,
                    'source': '财经新闻'
                },
                {
                    'title': '健康生活方式',
                    'heat': 0.7,
                    'source': '健康杂志'
                }
            ]
        }
    
    def _execute_publish_workflow(self, parameters):
        """执行发布工作流程"""
        # 模拟发布过程
        time.sleep(3)
        
        # 发送飞书消息
        try:
            message_title = f"【AI编辑部】文章发布完成"
            message_content = f"文章ID：{parameters.get('article_id', '未知')}\n标题：{parameters.get('title', '未知')}"
            self.feishu_client.send_message(message_title, message_content)
            print("飞书消息发送成功")
        except Exception as e:
            print(f"飞书消息发送失败: {e}")
        
        # 模拟发布结果
        return {
            'publish_id': 1,
            'status': 'published',
            'platform': 'feishu',
            'message_sent': True
        }

# 测试代码
if __name__ == '__main__':
    workflow_engine = WorkflowEngine()
    
    # 测试文章创建工作流
    print("测试文章创建工作流...")
    result = workflow_engine.execute_workflow('article_creation')
    print(f"工作流执行结果: {result}")
    
    # 测试热点监测工作流
    print("\n测试热点监测工作流...")
    result = workflow_engine.execute_workflow('topic_monitoring')
    print(f"工作流执行结果: {result}")
    
    # 测试发布工作流
    print("\n测试发布工作流...")
    result = workflow_engine.execute_workflow('publish')
    print(f"工作流执行结果: {result}")
