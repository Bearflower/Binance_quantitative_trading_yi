import sqlite3
import os
from datetime import datetime

# 获取数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), '../data/ai_editorial_system.db')

# 确保数据目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 连接数据库
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# 创建任务表
cursor.execute('''
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    parameters TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# 创建选题表
cursor.execute('''
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    heat REAL,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# 创建素材表
cursor.execute('''
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER,
    type TEXT NOT NULL,
    content TEXT,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (topic_id) REFERENCES topics (id)
)
''')

# 创建文章表
cursor.execute('''
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER,
    title TEXT NOT NULL,
    content TEXT,
    rich_content TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (topic_id) REFERENCES topics (id)
)
''')

# 创建图片表
cursor.execute('''
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    url TEXT,
    path TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (article_id) REFERENCES articles (id)
)
''')

# 创建配置表
cursor.execute('''
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# 插入默认配置
default_configs = [

    ('minimax_api_key', 'your_minimax_api_key', 'MiniMax API密钥'),
    ('system_name', 'AI编辑部系统', '系统名称'),
    ('system_version', '1.0.0', '系统版本'),
]

for key, value, description in default_configs:
    cursor.execute('''
    INSERT OR REPLACE INTO configs (key, value, description, updated_at) 
    VALUES (?, ?, ?, ?)
    ''', (key, value, description, datetime.now()))

# 提交事务
conn.commit()

# 关闭连接
conn.close()

print(f"数据库初始化成功！数据库文件路径：{DB_PATH}")
