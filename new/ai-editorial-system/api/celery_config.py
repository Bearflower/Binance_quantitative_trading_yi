from celery import Celery
from celery.schedules import crontab
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 创建 Celery 实例
app = Celery(
    'ai_editorial_system',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    include=['api.tasks']
)

# 配置 Celery
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Asia/Shanghai',
    enable_utc=True,
    beat_schedule={
        # 每天早上10点执行文章生成任务
        'generate-article-every-day': {
            'task': 'api.tasks.generate_article',
            'schedule': crontab(hour=10, minute=0),  # 每天早上10点
            'args': (),
        },
    },
)

if __name__ == '__main__':
    app.start()
