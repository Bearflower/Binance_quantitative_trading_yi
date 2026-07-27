"""
测试数据库管理
"""
import pytest
from shared.database import DatabaseManager, SQLInjectionError


class TestDatabaseManagerInitialization:
    """测试数据库管理器初始化"""
    
    def test_valid_initialization(self):
        """测试有效初始化"""
        db = DatabaseManager(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="test_password"
        )
        
        assert db.host == "localhost"
        assert db.port == 5432
        assert db.database == "test_db"
        assert db.user == "test_user"
        # 验证脱敏后的密码
        # test_password (13字符) -> te + 9个* + rd
        assert db.password == "te*********rd"
        assert db.min_pool_size == 5
        assert db.max_pool_size == 20
    
    def test_custom_pool_size(self):
        """测试自定义连接池大小"""
        db = DatabaseManager(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="test_password",
            min_pool_size=10,
            max_pool_size=30
        )
        
        assert db.min_pool_size == 10
        assert db.max_pool_size == 30


class TestPasswordMasking:
    """测试密码脱敏"""
    
    def test_short_password_masking(self):
        """测试短密码脱敏"""
        db = DatabaseManager.__new__(DatabaseManager)
        db._password = "abc"
        
        # 短密码应该全部用*代替
        assert db.password == "***"
    
    def test_medium_password_masking(self):
        """测试中等长度密码脱敏"""
        db = DatabaseManager.__new__(DatabaseManager)
        db._password = "abcd"
        
        # 刚好4位，应该全部用*代替
        assert db.password == "****"
    
    def test_long_password_masking(self):
        """测试长密码脱敏"""
        db = DatabaseManager.__new__(DatabaseManager)
        db._password = "password123"
        
        # 长密码应该显示前2位和后2位
        # password123 (11字符) -> pa + 7个* + 23
        assert db.password == "pa*******23"


class TestSQLInjectionProtection:
    """测试SQL注入防护"""
    
    @pytest.fixture
    def db(self):
        """创建测试数据库管理器"""
        return DatabaseManager(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="test_password"
        )
    
    def test_drop_statement(self, db):
        """测试DROP语句检测"""
        with pytest.raises(SQLInjectionError, match="检测到危险的SQL操作"):
            db._validate_sql("DROP TABLE users;")
    
    def test_truncate_statement(self, db):
        """测试TRUNCATE语句检测"""
        with pytest.raises(SQLInjectionError, match="检测到危险的SQL操作"):
            db._validate_sql("TRUNCATE TABLE users;")
    
    def test_alter_statement(self, db):
        """测试ALTER语句检测"""
        with pytest.raises(SQLInjectionError, match="检测到危险的SQL操作"):
            db._validate_sql("ALTER TABLE users ADD COLUMN test INT;")
    
    def test_create_statement(self, db):
        """测试CREATE语句检测"""
        with pytest.raises(SQLInjectionError, match="检测到危险的SQL操作"):
            db._validate_sql("CREATE TABLE test (id INT);")
    
    def test_comment_injection(self, db):
        """测试注释注入检测"""
        with pytest.raises(SQLInjectionError, match="检测到SQL注释注入风险"):
            db._validate_sql("SELECT * FROM users -- comment")
    
    def test_union_injection(self, db):
        """测试UNION注入检测"""
        with pytest.raises(SQLInjectionError, match="检测到UNION注入风险"):
            db._validate_sql("SELECT * FROM users UNION SELECT * FROM admin")
    
    def test_multiple_statements(self, db):
        """测试多语句执行检测"""
        with pytest.raises(SQLInjectionError, match="禁止执行多条SQL语句"):
            db._validate_sql("SELECT * FROM users; DELETE FROM users;")
    
    def test_valid_select_statement(self, db):
        """测试有效的SELECT语句"""
        # 不应该抛出异常
        db._validate_sql("SELECT * FROM users WHERE id = $1")
    
    def test_valid_insert_statement(self, db):
        """测试有效的INSERT语句"""
        # 不应该抛出异常
        db._validate_sql("INSERT INTO users (name, email) VALUES ($1, $2)")
    
    def test_valid_update_statement(self, db):
        """测试有效的UPDATE语句"""
        # 不应该抛出异常
        db._validate_sql("UPDATE users SET name = $1 WHERE id = $2")
    
    def test_valid_delete_statement(self, db):
        """测试有效的DELETE语句"""
        # 不应该抛出异常
        db._validate_sql("DELETE FROM users WHERE id = $1")


class TestTransactionValidation:
    """测试事务参数验证"""
    
    @pytest.fixture
    def db(self):
        """创建测试数据库管理器"""
        return DatabaseManager(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="test_password"
        )
    
    @pytest.mark.asyncio
    async def test_empty_queries_list(self, db):
        """测试空查询列表"""
        with pytest.raises(ValueError, match="查询列表不能为空"):
            await db.execute_transaction([])
