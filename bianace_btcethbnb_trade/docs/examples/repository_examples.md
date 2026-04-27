# 数据仓库使用示例

本文档详细介绍如何使用数据仓库模式（Repository Pattern）进行数据库操作，包括 CRUD 操作、批量操作、事务管理等。

## 目录

1. [创建自定义仓库](#创建自定义仓库)
2. [CRUD 操作](#crud-操作)
3. [批量操作](#批量操作)
4. [事务管理](#事务管理)
5. [查询优化](#查询优化)

---

## 创建自定义仓库

### 示例 1：基本仓库类

```python
from models.repository import BaseRepository
from models.entities import Trade
from typing import List, Optional

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def find_by_symbol(self, symbol: str) -> List[Trade]:
        """根据交易对查询交易记录"""
        query = "SELECT * FROM trades WHERE symbol = %s ORDER BY open_time DESC"
        results = self.db._execute_query(query, (symbol,))
        return [Trade(**row) for row in results]
    
    def find_by_date_range(
        self,
        start_date: str,
        end_date: str
    ) -> List[Trade]:
        """根据日期范围查询交易记录"""
        query = """
            SELECT * FROM trades
            WHERE open_time >= %s AND open_time <= %s
            ORDER BY open_time DESC
        """
        results = self.db._execute_query(query, (start_date, end_date))
        return [Trade(**row) for row in results]

# 使用示例
repo = TradeRepository()
trades = repo.find_by_symbol('BTCUSDT')
print(f"找到 {len(trades)} 条交易记录")
```

### 示例 2：带缓存的仓库

```python
from models.repository import BaseRepository
from cachetools import TTLCache

class CachedTradeRepository(BaseRepository):
    """带缓存的交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
        # 创建 TTL 缓存（最多 1000 条，5 分钟过期）
        self.cache = TTLCache(maxsize=1000, ttl=300)
    
    def find_by_id(self, trade_id: int):
        """根据 ID 查询交易记录（带缓存）"""
        # 检查缓存
        cache_key = f"trade_{trade_id}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        # 查询数据库
        query = "SELECT * FROM trades WHERE id = %s"
        result = self.db._execute_one(query, (trade_id,))
        
        if result:
            # 存入缓存
            self.cache[cache_key] = result
            return result
        
        return None

# 使用示例
repo = CachedTradeRepository()
trade = repo.find_by_id(123)
print(f"交易记录: {trade}")
```

---

## CRUD 操作

### 示例 3：创建记录

```python
from models.repository import BaseRepository
from datetime import datetime

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def create_trade(self, trade_data: dict) -> int:
        """创建交易记录"""
        query = """
            INSERT INTO trades (
                symbol, direction, entry_price, stop_loss,
                quantity, margin, leverage, open_time, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        result = self.db._execute_one(
            query,
            (
                trade_data['symbol'],
                trade_data['direction'],
                trade_data['entry_price'],
                trade_data['stop_loss'],
                trade_data['quantity'],
                trade_data['margin'],
                trade_data['leverage'],
                datetime.now(),
                'OPEN'
            )
        )
        
        return result['id'] if result else None

# 使用示例
repo = TradeRepository()
trade_id = repo.create_trade({
    'symbol': 'BTCUSDT',
    'direction': 'LONG',
    'entry_price': 50000,
    'stop_loss': 48000,
    'quantity': 0.01,
    'margin': 10,
    'leverage': 5
})
print(f"创建交易记录: ID={trade_id}")
```

### 示例 4：读取记录

```python
from models.repository import BaseRepository

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def get_active_trades(self):
        """获取所有活跃交易"""
        query = "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY open_time DESC"
        results = self.db._execute_query(query)
        return results
    
    def get_trade_by_order_id(self, order_id: str):
        """根据订单 ID 查询交易"""
        query = "SELECT * FROM trades WHERE order_id = %s"
        result = self.db._execute_one(query, (order_id,))
        return result
    
    def count_trades_by_symbol(self, symbol: str) -> int:
        """统计交易对的数量"""
        query = "SELECT COUNT(*) as count FROM trades WHERE symbol = %s"
        result = self.db._execute_one(query, (symbol,))
        return result['count'] if result else 0

# 使用示例
repo = TradeRepository()

# 获取活跃交易
active_trades = repo.get_active_trades()
print(f"活跃交易数: {len(active_trades)}")

# 统计交易数量
count = repo.count_trades_by_symbol('BTCUSDT')
print(f"BTCUSDT 交易数: {count}")
```

### 示例 5：更新记录

```python
from models.repository import BaseRepository
from datetime import datetime

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def update_trade_status(self, trade_id: int, status: str, pnl: float = None):
        """更新交易状态"""
        if pnl is not None:
            query = """
                UPDATE trades
                SET status = %s, pnl = %s, close_time = %s
                WHERE id = %s
            """
            self.db._execute_query(query, (status, pnl, datetime.now(), trade_id))
        else:
            query = "UPDATE trades SET status = %s WHERE id = %s"
            self.db._execute_query(query, (status, trade_id))
    
    def update_stop_loss(self, trade_id: int, new_stop_loss: float):
        """更新止损价"""
        query = "UPDATE trades SET stop_loss = %s WHERE id = %s"
        self.db._execute_query(query, (new_stop_loss, trade_id))

# 使用示例
repo = TradeRepository()

# 更新交易状态
repo.update_trade_status(123, 'CLOSED', pnl=10.5)
print("交易状态已更新")

# 更新止损价
repo.update_stop_loss(123, 48500)
print("止损价已更新")
```

### 示例 6：删除记录

```python
from models.repository import BaseRepository

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def delete_trade(self, trade_id: int):
        """删除交易记录"""
        query = "DELETE FROM trades WHERE id = %s"
        self.db._execute_query(query, (trade_id,))
    
    def delete_old_trades(self, days: int = 30):
        """删除旧交易记录"""
        query = """
            DELETE FROM trades
            WHERE open_time < NOW() - INTERVAL '%s days'
        """
        self.db._execute_query(query, (days,))

# 使用示例
repo = TradeRepository()

# 删除单条记录
repo.delete_trade(123)
print("交易记录已删除")

# 删除 30 天前的记录
repo.delete_old_trades(30)
print("旧记录已清理")
```

---

## 批量操作

### 示例 7：批量插入

```python
from models.repository import BaseRepository
from datetime import datetime

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def batch_insert_trades(self, trades: list):
        """批量插入交易记录"""
        query = """
            INSERT INTO trades (
                symbol, direction, entry_price, quantity, open_time, status
            ) VALUES %s
        """
        
        # 准备数据
        values = [
            (
                trade['symbol'],
                trade['direction'],
                trade['entry_price'],
                trade['quantity'],
                datetime.now(),
                'OPEN'
            )
            for trade in trades
        ]
        
        # 批量插入
        from psycopg2.extras import execute_values
        execute_values(
            self.db._get_connection().cursor(),
            query,
            values
        )

# 使用示例
repo = TradeRepository()
trades = [
    {'symbol': 'BTCUSDT', 'direction': 'LONG', 'entry_price': 50000, 'quantity': 0.01},
    {'symbol': 'ETHUSDT', 'direction': 'LONG', 'entry_price': 3000, 'quantity': 0.1},
    {'symbol': 'BNBUSDT', 'direction': 'SHORT', 'entry_price': 300, 'quantity': 1.0}
]
repo.batch_insert_trades(trades)
print(f"批量插入 {len(trades)} 条记录")
```

### 示例 8：批量更新

```python
from models.repository import BaseRepository

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def batch_update_status(self, updates: list):
        """批量更新状态"""
        # updates 格式: [(trade_id, status), ...]
        query = """
            UPDATE trades
            SET status = data.status
            FROM (VALUES %s) AS data(id, status)
            WHERE trades.id = data.id
        """
        
        from psycopg2.extras import execute_values
        execute_values(
            self.db._get_connection().cursor(),
            query,
            updates
        )

# 使用示例
repo = TradeRepository()
updates = [
    (101, 'CLOSED'),
    (102, 'CLOSED'),
    (103, 'CANCELLED')
]
repo.batch_update_status(updates)
print(f"批量更新 {len(updates)} 条记录")
```

---

## 事务管理

### 示例 9：使用事务

```python
from models.repository import BaseRepository
from models.database import get_db_manager

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def transfer_position(self, from_id: int, to_id: int, quantity: float):
        """转移仓位（事务操作）"""
        db = get_db_manager()
        
        try:
            # 开始事务
            db._begin_transaction()
            
            # 1. 减少源仓位
            query1 = """
                UPDATE positions
                SET quantity = quantity - %s
                WHERE id = %s AND quantity >= %s
            """
            db._execute_query(query1, (quantity, from_id, quantity))
            
            # 2. 增加目标仓位
            query2 = """
                UPDATE positions
                SET quantity = quantity + %s
                WHERE id = %s
            """
            db._execute_query(query2, (quantity, to_id))
            
            # 3. 记录转移日志
            query3 = """
                INSERT INTO transfer_logs (from_id, to_id, quantity, timestamp)
                VALUES (%s, %s, %s, NOW())
            """
            db._execute_query(query3, (from_id, to_id, quantity))
            
            # 提交事务
            db._commit_transaction()
            print("仓位转移成功")
            
        except Exception as e:
            # 回滚事务
            db._rollback_transaction()
            print(f"仓位转移失败: {e}")
            raise

# 使用示例
repo = TradeRepository()
try:
    repo.transfer_position(1, 2, 0.005)
except Exception as e:
    print(f"操作失败: {e}")
```

### 示例 10：使用上下文管理器

```python
from models.database import get_db_manager

def execute_in_transaction(operations: list):
    """在事务中执行多个操作"""
    db = get_db_manager()
    
    with db.transaction():
        for operation in operations:
            query = operation['query']
            params = operation.get('params', ())
            db._execute_query(query, params)

# 使用示例
operations = [
    {
        'query': 'UPDATE accounts SET balance = balance - %s WHERE id = %s',
        'params': (100, 1)
    },
    {
        'query': 'UPDATE accounts SET balance = balance + %s WHERE id = %s',
        'params': (100, 2)
    },
    {
        'query': 'INSERT INTO transfer_logs (from_id, to_id, amount) VALUES (%s, %s, %s)',
        'params': (1, 2, 100)
    }
]

try:
    execute_in_transaction(operations)
    print("事务执行成功")
except Exception as e:
    print(f"事务执行失败: {e}")
```

---

## 查询优化

### 示例 11：使用索引

```python
from models.repository import BaseRepository

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def find_by_symbol_optimized(self, symbol: str):
        """使用索引查询（确保 symbol 列有索引）"""
        # 使用索引列查询
        query = """
            SELECT * FROM trades
            WHERE symbol = %s
            ORDER BY open_time DESC
            LIMIT 100
        """
        results = self.db._execute_query(query, (symbol,))
        return results
    
    def find_by_date_range_optimized(
        self,
        symbol: str,
        start_date: str,
        end_date: str
    ):
        """使用复合索引查询"""
        # 使用复合索引 (symbol, open_time)
        query = """
            SELECT * FROM trades
            WHERE symbol = %s
                AND open_time >= %s
                AND open_time <= %s
            ORDER BY open_time DESC
        """
        results = self.db._execute_query(query, (symbol, start_date, end_date))
        return results

# 使用示例
repo = TradeRepository()
trades = repo.find_by_symbol_optimized('BTCUSDT')
print(f"查询到 {len(trades)} 条记录")
```

### 示例 12：分页查询

```python
from models.repository import BaseRepository

class TradeRepository(BaseRepository):
    """交易数据仓库"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    def find_paginated(self, page: int = 1, page_size: int = 20):
        """分页查询"""
        offset = (page - 1) * page_size
        
        # 查询总数
        count_query = "SELECT COUNT(*) as total FROM trades"
        total = self.db._execute_one(count_query)['total']
        
        # 查询当前页数据
        query = """
            SELECT * FROM trades
            ORDER BY open_time DESC
            LIMIT %s OFFSET %s
        """
        results = self.db._execute_query(query, (page_size, offset))
        
        return {
            'data': results,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size
        }

# 使用示例
repo = TradeRepository()

# 获取第 1 页
page1 = repo.find_paginated(page=1, page_size=20)
print(f"第 1 页: {len(page1['data'])} 条记录，共 {page1['total_pages']} 页")

# 获取第 2 页
page2 = repo.find_paginated(page=2, page_size=20)
print(f"第 2 页: {len(page2['data'])} 条记录")
```

---

## 最佳实践

### 示例 13：完整的仓库实现

```python
from models.repository import BaseRepository
from models.entities import Trade
from typing import List, Optional, Dict, Any
from datetime import datetime
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

class CompleteTradeRepository(BaseRepository):
    """完整的交易数据仓库实现"""
    
    def __init__(self):
        super().__init__(table_name='trades')
    
    # ========== 查询方法 ==========
    
    def find_by_id(self, trade_id: int) -> Optional[Trade]:
        """根据 ID 查询"""
        query = "SELECT * FROM trades WHERE id = %s"
        result = self.db._execute_one(query, (trade_id,))
        return Trade(**result) if result else None
    
    def find_by_symbol(self, symbol: str, limit: int = 100) -> List[Trade]:
        """根据交易对查询"""
        query = """
            SELECT * FROM trades
            WHERE symbol = %s
            ORDER BY open_time DESC
            LIMIT %s
        """
        results = self.db._execute_query(query, (symbol, limit))
        return [Trade(**row) for row in results]
    
    def find_active_trades(self) -> List[Trade]:
        """查询活跃交易"""
        query = """
            SELECT * FROM trades
            WHERE status = 'OPEN'
            ORDER BY open_time DESC
        """
        results = self.db._execute_query(query)
        return [Trade(**row) for row in results]
    
    # ========== 创建方法 ==========
    
    def create(self, trade_data: Dict[str, Any]) -> int:
        """创建交易记录"""
        query = """
            INSERT INTO trades (
                symbol, direction, entry_price, stop_loss,
                take_profit, quantity, margin, leverage,
                open_time, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        result = self.db._execute_one(
            query,
            (
                trade_data['symbol'],
                trade_data['direction'],
                trade_data['entry_price'],
                trade_data.get('stop_loss'),
                trade_data.get('take_profit'),
                trade_data['quantity'],
                trade_data['margin'],
                trade_data['leverage'],
                datetime.now(),
                'OPEN'
            )
        )
        
        trade_id = result['id'] if result else None
        logger.info(f"创建交易记录: ID={trade_id}")
        return trade_id
    
    # ========== 更新方法 ==========
    
    def update_status(
        self,
        trade_id: int,
        status: str,
        pnl: Optional[Decimal] = None
    ):
        """更新交易状态"""
        if pnl is not None:
            query = """
                UPDATE trades
                SET status = %s, pnl = %s, close_time = %s
                WHERE id = %s
            """
            self.db._execute_query(
                query,
                (status, pnl, datetime.now(), trade_id)
            )
        else:
            query = "UPDATE trades SET status = %s WHERE id = %s"
            self.db._execute_query(query, (status, trade_id))
        
        logger.info(f"更新交易状态: ID={trade_id}, status={status}")
    
    # ========== 统计方法 ==========
    
    def count_by_symbol(self, symbol: str) -> int:
        """统计交易数量"""
        query = "SELECT COUNT(*) as count FROM trades WHERE symbol = %s"
        result = self.db._execute_one(query, (symbol,))
        return result['count'] if result else 0
    
    def calculate_total_pnl(self, symbol: str = None) -> Decimal:
        """计算总盈亏"""
        if symbol:
            query = "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE symbol = %s"
            result = self.db._execute_one(query, (symbol,))
        else:
            query = "SELECT COALESCE(SUM(pnl), 0) as total FROM trades"
            result = self.db._execute_one(query)
        
        return Decimal(str(result['total'])) if result else Decimal('0')

# 使用示例
repo = CompleteTradeRepository()

# 创建交易
trade_id = repo.create({
    'symbol': 'BTCUSDT',
    'direction': 'LONG',
    'entry_price': Decimal('50000'),
    'stop_loss': Decimal('48000'),
    'quantity': Decimal('0.01'),
    'margin': Decimal('10'),
    'leverage': 5
})

# 查询交易
trade = repo.find_by_id(trade_id)
print(f"交易记录: {trade}")

# 更新状态
repo.update_status(trade_id, 'CLOSED', pnl=Decimal('10.5'))

# 统计盈亏
total_pnl = repo.calculate_total_pnl('BTCUSDT')
print(f"总盈亏: {total_pnl}U")
```

---

## 注意事项

1. **SQL 注入**：始终使用参数化查询，避免 SQL 注入
2. **事务管理**：对多个相关操作使用事务
3. **索引优化**：为常用查询字段创建索引
4. **分页查询**：大数据集使用分页查询
5. **连接池**：使用连接池管理数据库连接
6. **错误处理**：妥善处理数据库错误

---

## 相关文档

- [服务基类使用示例](./service_base_examples.md)
- [异常处理使用示例](./exception_handling_examples.md)
- [监控告警使用示例](./monitoring_examples.md)
