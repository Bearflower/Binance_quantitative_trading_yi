"""归档两份已核验的重复历史CSV，并清理无引用的可再下载浏览器二进制。

默认只读；prepare仅创建可恢复归档，apply要求已验证本地副本SHA。
不接收任意待删除目录，不递归强删，不修改数据库备份或journal。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile

SOURCE_ROOTS = (Path('/root/trading_system/backtest/cash_carry/data_2y'),
                Path('/root/trading_system/cash_carry/data_2y'))
CACHE_ROOTS = tuple(Path('/root/.cache/ms-playwright') / name for name in
                   ('chromium-1208', 'chromium_headless_shell-1208', 'ffmpeg-1011'))
PACKAGE_ROOT = Path('/root/.npm/_npx/e41f203b7505f1fb/node_modules/playwright-core')
DEFAULT_DESTINATION = Path('/root/trading_system/.archives/server-inspection-20261128')


def digest_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def checked_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != name:
        raise ValueError('文件清单含不安全路径')
    return path


def snapshot_tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('源目录缺失、含符号链接或不是真实绝对路径: ' + str(root))
    files, directories = {}, []
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs[:]:
            path = Path(base) / name
            if path.is_symlink():
                names.append(name); dirs.remove(name)
        for name in names:
            path = Path(base) / name
            relative = str(path.relative_to(root)); checked_relative(relative)
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode):
                row = {'kind': 'symlink', 'target': os.readlink(path)}
            elif stat.S_ISREG(before.st_mode):
                row = {'kind': 'file', 'bytes': before.st_size, 'sha256': digest_file(path)}
            else:
                raise ValueError('源目录包含特殊文件: ' + relative)
            after = path.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('读取期间源文件变化: ' + relative)
            row.update(inode=before.st_ino, mtime_ns=before.st_mtime_ns, mode=stat.S_IMODE(before.st_mode))
            files[relative] = row
        directories.extend(str((Path(base)/name).relative_to(root)) for name in dirs)
    return {'files': files, 'directories': sorted(directories)}


def _under(path, roots):
    return any(path == str(root) or path.startswith(str(root) + '/') for root in roots)


def check_references(roots):
    """检查所有容器挂载、进程及定时启动配置；不输出进程凭据。"""
    ids = subprocess.check_output(['docker', 'ps', '-aq'], text=True).split()
    containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids], text=True)) if ids else []
    for container in containers:
        for mount in container['Mounts']:
            if _under(mount['Source'], roots) or any(_under(str(root), [Path(mount['Source'])]) for root in roots):
                raise ValueError('目录仍被容器挂载: ' + container['Name'])
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            targets = []
            for path in [proc/'cwd', proc/'exe', *list((proc/'fd').iterdir())]:
                try:
                    targets.append(os.readlink(path))
                except (FileNotFoundError, ProcessLookupError):
                    continue
            if any(_under(target.removesuffix(' (deleted)'), roots) for target in targets):
                raise ValueError('目录仍被进程引用: PID ' + proc.name)
            for key in ('maps', 'cmdline'):
                text = (proc/key).read_text(errors='replace')
                if any(str(root) in text for root in roots):
                    raise ValueError('目录仍被进程映射或参数引用: PID ' + proc.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
    for folder in ('/etc/systemd/system', '/etc/cron.d', '/var/spool/cron'):
        for path in Path(folder).rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text(errors='replace')
            if any(str(root) in text for root in roots) or 'ms-playwright' in text:
                raise ValueError('目录仍被定时配置引用: ' + str(path))


def review_sources(review):
    expected = review.get('collections', [])
    if review.get('identical_content') is not True or {r['root'] for r in expected} != {str(p) for p in SOURCE_ROOTS}:
        raise ValueError('审核清单不是两份指定历史CSV')
    snapshots = []
    for root in SOURCE_ROOTS:
        snapshot = snapshot_tree(root)
        rows = next(r['files'] for r in expected if r['root'] == str(root))
        actual = snapshot['files']
        if set(actual) != {r['path'] for r in rows}:
            raise ValueError('历史CSV文件集合已变化')
        for row in rows:
            value = actual[row['path']]
            if value['kind'] != 'file' or any(value[k] != row[k] for k in ('bytes', 'sha256')):
                raise ValueError('历史CSV内容与已审核清单不同: ' + row['path'])
        snapshots.append({'root': str(root), 'snapshot': snapshot})
    canonical = lambda item: {name: (r['bytes'], r['sha256']) for name, r in item['snapshot']['files'].items()}
    if canonical(snapshots[0]) != canonical(snapshots[1]):
        raise ValueError('两份CSV并不完全相同')
    return snapshots


def verify_archive(path, snapshot):
    seen = set()
    with tarfile.open(path, 'r:gz') as stream:
        for member in stream:
            if member.isdir():
                if member.name != 'data_2y':
                    checked_relative(member.name)
                    if not member.name.startswith('data_2y/'):
                        raise ValueError('归档目录越界')
                continue
            if not member.isfile() or not member.name.startswith('data_2y/'):
                raise ValueError('归档含未批准的非普通文件')
            relative = member.name[len('data_2y/'):]; checked_relative(relative)
            if relative not in snapshot['files'] or relative in seen:
                raise ValueError('归档文件集合不符')
            result = hashlib.sha256()
            source = stream.extractfile(member)
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                result.update(chunk)
            row = snapshot['files'][relative]
            if member.size != row['bytes'] or result.hexdigest() != row['sha256']:
                raise ValueError('归档成员内容校验失败: ' + relative)
            seen.add(relative)
    if seen != set(snapshot['files']):
        raise ValueError('归档缺少文件')


def prepare(review, destination):
    destination = Path(destination)
    if destination.exists() or any(_under(str(destination), [r]) for r in (*SOURCE_ROOTS, *CACHE_ROOTS)):
        raise ValueError('归档目录必须为源目录之外的全新路径')
    check_references((*SOURCE_ROOTS, *CACHE_ROOTS))
    sources = review_sources(review)
    caches = [{'root': str(root), 'snapshot': snapshot_tree(root)} for root in CACHE_ROOTS]
    package = json.loads((PACKAGE_ROOT/'package.json').read_text())
    if package['name'] != 'playwright-core' or package['version'] != '1.58.1':
        raise ValueError('浏览器包版本已变化，必须重新核验')
    browsers = json.loads((PACKAGE_ROOT/'browsers.json').read_text())
    expected = {'chromium': '1208', 'chromium-headless-shell': '1208', 'ffmpeg': '1011'}
    revisions = {row['name']: str(row['revision']) for row in browsers['browsers']}
    if any(revisions.get(name) != revision for name, revision in expected.items()):
        raise ValueError('浏览器修订已变化，不能复用缓存删除范围')
    destination.mkdir(parents=True, mode=0o700)
    target = destination/'cash-carry-data_2y.tar.gz'
    with tarfile.open(target, 'w:gz', compresslevel=6) as stream:
        stream.add(SOURCE_ROOTS[0], arcname='data_2y')
    os.chmod(target, 0o600)
    verify_archive(target, sources[0]['snapshot'])
    manifest = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'archive_sha256': digest_file(target),
                'archive_bytes': target.stat().st_size, 'sources': sources, 'caches': caches,
                'package': {'root': str(PACKAGE_ROOT), 'name': package['name'], 'version': package['version']},
                'browsers': browsers}
    (destination/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    (destination/'browsers.json').write_text(json.dumps(browsers, indent=2)+'\n')
    (destination/'RESTORE.md').write_text(
        '# 历史数据与浏览器恢复\n\n归档须先与 manifest.json 的 archive_sha256 校验。\n\n'
        '每个原目录按需 mkdir -p，再执行 tar -xzf cash-carry-data_2y.tar.gz --strip-components=1 -C 原目录。'
        '源文件逐项SHA见 manifest.json 的 sources；两个原目录内容完全相同。\n\n'
        '浏览器包、.links、browsers.json 均保留；通过原包CLI恢复相同修订：\n\n'
        f'node {PACKAGE_ROOT}/cli.js install chromium\n\n'
        '版本 playwright-core 1.58.1，Chromium/headless 1208、FFmpeg 1011。\n')
    return manifest


def apply(destination, local_copy_sha256):
    destination = Path(destination)
    manifest = json.loads((destination/'manifest.json').read_text())
    target = destination/'cash-carry-data_2y.tar.gz'
    if local_copy_sha256 != manifest['archive_sha256'] or digest_file(target) != local_copy_sha256:
        raise ValueError('本地副本或服务器归档SHA不符')
    roots = (*SOURCE_ROOTS, *CACHE_ROOTS)
    entries = manifest['sources'] + manifest['caches']
    if {r['root'] for r in entries} != {str(r) for r in roots} or len(entries) != len(roots):
        raise ValueError('删除范围不是指定历史数据与浏览器缓存')
    package = json.loads((PACKAGE_ROOT/'package.json').read_text())
    if package['name'] != manifest['package']['name'] or package['version'] != manifest['package']['version']:
        raise ValueError('归档后浏览器包发生变化')
    if json.loads((PACKAGE_ROOT/'browsers.json').read_text()) != manifest['browsers']:
        raise ValueError('归档后浏览器修订发生变化')
    verify_archive(target, manifest['sources'][0]['snapshot'])
    check_references(roots)
    for row in entries:
        if snapshot_tree(Path(row['root'])) != row['snapshot']:
            raise ValueError('归档后源目录变化，拒绝删除: ' + row['root'])
    # 所有前置检查通过后，再逐文件删除；未知新增文件会令rmdir失败，不强删目录。
    removed = {'removed_source_files': 0, 'removed_cache_files': 0}
    for row in entries:
        root = Path(row['root'])
        for relative, original in row['snapshot']['files'].items():
            checked_relative(relative)
            path = root/relative
            if path.parent.resolve() != path.parent:
                raise ValueError('删除路径父目录出现符号链接')
            current = path.lstat()
            if (current.st_ino, current.st_mtime_ns) != (original['inode'], original['mtime_ns']):
                raise ValueError('删除前文件已变化: ' + relative)
            path.unlink()
            removed['removed_source_files' if root in SOURCE_ROOTS else 'removed_cache_files'] += 1
        for relative in sorted(row['snapshot']['directories'], key=lambda value: len(PurePosixPath(value).parts), reverse=True):
            checked_relative(relative); (root/relative).rmdir()
        root.rmdir()
        (root.parent/(root.name+'.ARCHIVED.txt')).write_text('归档与恢复说明: '+str(destination/'RESTORE.md')+'\n')
    result = dict(removed, applied_at=datetime.now(timezone.utc).isoformat(), archive_sha256=local_copy_sha256)
    (destination/'applied.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['dry-run', 'prepare', 'apply'], default='dry-run')
    parser.add_argument('--review', type=Path)
    parser.add_argument('--destination', type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument('--verified-local-copy-sha256')
    args = parser.parse_args()
    if args.mode in ('dry-run', 'prepare') and args.review is None:
        parser.error('--review 必填')
    if args.mode == 'apply' and not args.verified_local_copy_sha256:
        parser.error('apply 需要先核验本地副本并提供 --verified-local-copy-sha256')
    if args.mode == 'dry-run':
        check_references((*SOURCE_ROOTS, *CACHE_ROOTS))
        sources = review_sources(json.loads(args.review.read_text()))
        caches = [snapshot_tree(root) for root in CACHE_ROOTS]
        result = {'source_files': sum(len(row['snapshot']['files']) for row in sources),
                  'source_bytes': sum(f['bytes'] for row in sources for f in row['snapshot']['files'].values()),
                  'cache_files': sum(len(row['files']) for row in caches),
                  'cache_bytes': sum(f.get('bytes', 0) for row in caches for f in row['files'].values()),
                  'ready_for_prepare': True}
    else:
        lock = Path('/root/trading_system/.ops/disk-archive.lock')
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = (prepare(json.loads(args.review.read_text()), args.destination) if args.mode == 'prepare'
                      else apply(args.destination, args.verified_local_copy_sha256))
    print(json.dumps({k: v for k, v in result.items() if k not in ('sources','caches','browsers')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
