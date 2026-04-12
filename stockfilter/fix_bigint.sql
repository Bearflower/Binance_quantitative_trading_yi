-- 修复 bigint out of range 错误
-- 将 volume 和 amount 字段改为更大的类型

-- 1. 修改 volume 为 NUMERIC(20,0) 可以存储更大的整数
ALTER TABLE klines ALTER COLUMN volume TYPE NUMERIC(20,0);

-- 2. amount 已经是 NUMERIC(20,2)，应该足够，但可以增加精度
ALTER TABLE klines ALTER COLUMN amount TYPE NUMERIC(30,2);

-- 3. 验证修改结果
\d klines

-- 4. 测试插入大数据
INSERT INTO klines (code, date, open, high, low, close, volume, amount)
VALUES ('TEST', '2026-04-09', 100.00, 110.00, 90.00, 105.00, 99999999999999999999, 9999999999999999999999.99)
ON CONFLICT (code, date) DO NOTHING;

-- 5. 验证插入成功
SELECT * FROM klines WHERE code = 'TEST';

-- 6. 删除测试数据
DELETE FROM klines WHERE code = 'TEST';
