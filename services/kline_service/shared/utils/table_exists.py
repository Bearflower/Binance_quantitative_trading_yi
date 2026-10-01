"""K 线表存在性判断（kline-service 内唯一实现）。

历史问题：读路径 / 采集路径 / 启动自检各自内联了一段
``information_schema.tables`` + 硬编码 ``table_schema = 'public'`` 的存在性查询，
而 kline 表实际落在 search_path 命中的 schema（生产为 ``btc_eth``），
导致判断恒为 False，使「表已存在即放行」等分支永不命中。

本模块用 ``to_regclass(:table_name) IS NOT NULL`` 判断存在性：该函数尊重
连接会话的 ``search_path``，对不存在对象返回 NULL，且表名以**绑定参数**传入
（不拼接 SQL，避免注入）。

约定：查询异常**不吞**，直接上抛，由调用方决定 fail-closed 行为
（读路径 → 503 拒绝；采集/自检 → 记 warning 并放弃该表）。
"""


async def table_exists(conn, table_name: str) -> bool:
    """判断表是否存在（尊重 search_path；参数化查询）。

    Args:
        conn: 具备 ``fetch_val(sql, params)`` 的数据库连接
        table_name: 已通过 TABLE_NAME_PATTERN 校验的表名

    Returns:
        bool: 表存在返回 True，否则 False

    Raises:
        Exception: 查询本身失败时原样上抛，交由调用方 fail-closed 处理
    """
    return bool(
        await conn.fetch_val(
            "SELECT to_regclass(:table_name) IS NOT NULL",
            {"table_name": table_name},
        )
    )
