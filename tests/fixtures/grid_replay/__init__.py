"""恢复原历史样本字节，不重新生成或替换审计指纹。"""
import gzip
import hashlib
from pathlib import Path

SAMPLE_NAME = 'ethusdt_aggtrades_20261007_0950_1010.json'
SAMPLE_SHA256 = '63e375fb1cb7586c03c303682cab6fde389c94165cbbb67735f05064a04ec2fc'


def materialize_grid_sample(directory: Path) -> Path:
    """将版本化压缩资产解压到测试临时目录，并核验原始 SHA-256。"""
    archive = Path(__file__).with_name(SAMPLE_NAME + '.gz')
    raw = gzip.decompress(archive.read_bytes())
    if hashlib.sha256(raw).hexdigest() != SAMPLE_SHA256:
        raise ValueError('历史网格样本解压后的 SHA-256 与原证据不一致')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SAMPLE_NAME
    path.write_bytes(raw)
    return path


def materialize_grid_database(directory: Path) -> Path:
    """从逐列无损 CSV 恢复临时 SQLite，校验两层摘要、类型及原行数。"""
    import csv
    import json
    import sqlite3

    assets = Path(__file__).parent
    manifest = json.loads((assets / 'database_manifest.json').read_text())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'ethusdt_aggtrades.sqlite'
    # 调用者用 session fixture 缓存；已有文件不作为已验证的证据复用。
    if path.exists():
        raise FileExistsError(path)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute('PRAGMA journal_mode=OFF')
        connection.execute('PRAGMA synchronous=OFF')
        for table, metadata in manifest['tables'].items():
            archive = assets / (table + '.csv.gz')
            zipped = hashlib.sha256()
            with archive.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    zipped.update(block)
            if zipped.hexdigest() != metadata['gzip_sha256']:
                raise ValueError('历史数据库压缩资产 SHA-256 不一致: ' + table)
            connection.execute(metadata['schema'])
            columns = metadata['columns']
            query = 'INSERT INTO "' + table + '" VALUES (' + ','.join('?' for _ in columns) + ')'
            digest = hashlib.sha256()
            count = 0
            def checked_lines(stream):
                for line in stream:
                    digest.update(line.encode('utf-8'))
                    yield line
            with gzip.open(archive, 'rt', encoding='utf-8', newline='') as stream:
                rows = csv.reader(checked_lines(stream))
                if next(rows) != [column['name'] for column in columns]:
                    raise ValueError('历史数据库列顺序不一致: ' + table)
                batch = []
                for row in rows:
                    if len(row) != len(columns):
                        raise ValueError('历史数据库字段数不一致: ' + table)
                    batch.append(tuple(None if value == '' and column['type'] == 'INTEGER'
                                       else int(value) if column['type'] == 'INTEGER' else value
                                       for value, column in zip(row, columns)))
                    count += 1
                    if len(batch) == 10000:
                        connection.executemany(query, batch)
                        batch.clear()
                if batch:
                    connection.executemany(query, batch)
            if count != metadata['rows'] or digest.hexdigest() != metadata['csv_sha256']:
                raise ValueError('历史数据库行数或原文 SHA-256 不一致: ' + table)
        for query in manifest['indexes']:
            connection.execute(query)
        connection.commit()
    except BaseException:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    return path
