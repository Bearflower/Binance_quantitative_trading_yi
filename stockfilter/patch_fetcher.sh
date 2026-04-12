#!/bin/bash
# 在容器内修改 fetcher.py

cat > /tmp/fix_fetcher.py << 'PYEOF'
import re

# 读取文件
with open('/app/data/fetcher.py', 'r') as f:
    content = f.read()

# 替换
old_code = "end_dt = datetime.now() - timedelta(days=1)"
new_code = "end_dt = datetime.now()  # 获取到今天的数据"

content = content.replace(old_code, new_code)

# 写回
with open('/app/data/fetcher.py', 'w') as f:
    f.write(content)

print("✅ 修改成功")
print("验证修改:")
with open('/app/data/fetcher.py', 'r') as f:
    for i, line in enumerate(f.readlines(), 1):
        if 'end_dt = datetime.now()' in line and 'timedelta' not in line:
            print(f"Line {i}: {line.strip()}")
PYEOF

python3 /tmp/fix_fetcher.py
