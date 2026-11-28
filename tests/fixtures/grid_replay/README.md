# 网格历史回归样本

压缩文件解压后与需求文档的原始 20 分钟样本逐字节一致。原始 SHA-256、历史 Git 来源、行数与时间窗口见 `manifest.json`。恢复来源是仓库已有 Git blob，不是按预期结果生成的替代数据。

测试通过 `materialize_grid_sample()` 解压到 pytest 临时目录，并检查原始 SHA-256；不需要开发机根目录的遗留 JSON。压缩仅用于减小版本化资产体积，原审计断言和样本身份不变。

该片段已经参与参数选择，不能作为样本外表现证据。全库实验使用同目录的两份 CSV gzip。它们由原 SQLite 的只读一致性事务按主键排序导出，保留全部列与字符串精度（包括审计时间），没有筛行或合成价格。`database_manifest.json` 记录原始 DDL、列类型、索引、行数及 CSV/压缩文件 SHA-256；测试在临时目录恢复 SQLite，再运行原有精确结果断言。INTEGER 空值恢复为 SQL NULL；原表 TEXT 列均为 NOT NULL。

资产体积：原始 JSON gzip 2,769,920 字节；agg_trades CSV gzip 39,018,691 字节；price_samples_1s CSV gzip 2,129,248 字节。所有压缩文件使用 gzip mtime=0，可重复导出。
