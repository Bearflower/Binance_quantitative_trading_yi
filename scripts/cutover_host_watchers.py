"""打包及安装账户切换涉及的宿主定时脚本。默认只核对，不启动作业。"""
import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile


HOST = Path("/root/trading_system")
# 原哈希来自 2026-11-25 正在运行的宿主定时任务及其依赖。
AI_SOURCES = {
    "btc_xau_inspection": "e5d27f5e33bee04209b02a4ef1827eb7183ab72a9a58eebba92da4e76faf1e52",
    "daily_digest": "704294a93814cecc2daac0903f771c45f686477ae44faf33da31651ae1e5c6e4",
    "digest_outbox": "372e0d90a76a66f96649b8c900b4edfa6b615f62e636385be37b4cfe26392cd9",
    "hrs_acceptance": "cefa53a70c40d74ca842626c00f9565a5eb84d8649eb1c1cf98edf2b9103bc7f",
    "hrs_observation": "2778ad9e3bc4c60d8c34812f9589bc8162f917c560b608a3831ef3a73c07bd08",
    "log_retention_check": "1135d85d08cee05ae8476935f50669e67971da02ed7e5205ff63de5f13ac4667",
    "mtpcs_entry_probe": "c33768fbfd04e8f2009305750c63130e818bec9a64ef47b41cc8527b12bd3c66",
    "mtpcs_observation": "adf1112cfb6c4aee2fbf36e8d3eda5302d8b370a699e15381737a54a7f768705",
    "mtpcs_order_archive": "718a33c4cf727508b36fb380d14ce08b8e4deb547efc107157116f951e58511d",
    "mtpcs_shadow_summary": "5468aeb6f60f3a5a80f38783d3a52e369cca5710b1d8db900db50ab7e86ca802",
}
MONTHLY = "scripts/maintain_monthly_facts_host.py"
MONTHLY_DEST = ".deploy_tmp/dashboard-monthly-facts-host.py"
MONTHLY_HASH = "08ee6638d06ae335e6b32e628097fcc44535a198140e3e668ce93e68d5ce6582"
AI_TIMERS = ("trading-hrs-acceptance", "trading-btc-xau-inspection", "trading-daily-digest",
             "trading-log-retention", "trading-grid-shadow")


def manifest(scope):
    files = {}
    if scope in ("ai", "all"):
        for name, previous in AI_SOURCES.items():
            path = f"ai_tuner/ops/{name}.py"
            files[path] = path, previous
    if scope in ("dashboard", "all"):
        files[MONTHLY] = MONTHLY_DEST, MONTHLY_HASH
    return files


def package(repo, output):
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(Path(__file__), "__main__.py")
        for source in manifest("all"):
            body = (repo / source).read_bytes()
            compile(body, source, "exec")
            archive.writestr(source, body)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def inspect(archive, root, scope):
    changes = []
    for source, (relative, prior) in manifest(scope).items():
        dest = root / relative
        if not dest.is_file() or dest.resolve() != dest:
            raise ValueError(f"宿主任务文件缺失或路径漂移 {relative}")
        desired, current = archive.read(source), dest.read_bytes()
        compile(desired, source, "exec")
        if current == desired:
            continue
        if digest(current) != prior:
            raise ValueError(f"宿主已有新修订，须重新核对 {relative} {digest(current)}")
        changes.append((dest, current, desired))
    return changes


@contextmanager
def quiesce(names):
    """保持计时器原启停状态，已在执行的任务完成后可安全重试。"""
    active = []
    for name in names:
        output = subprocess.check_output(["systemctl", "show", name + ".timer", "-p", "ActiveState",
                                          "-p", "LoadState"], text=True).splitlines()
        if "LoadState=loaded" not in output or not {"ActiveState=active", "ActiveState=inactive"}.intersection(output):
            raise RuntimeError(f"计时器状态未知 {name}")
        if "ActiveState=active" in output:
            active.append(name + ".timer")
    try:
        if active:
            subprocess.run(["systemctl", "stop", *active], check=True)
        for name in names:
            state = subprocess.check_output(["systemctl", "show", name + ".service", "-p",
                                             "ActiveState", "--value"], text=True).strip()
            if state not in ("inactive", "failed"):
                raise RuntimeError(f"宿主任务正在执行，请稍后重试 {name}")
        yield
    finally:
        if active:
            subprocess.run(["systemctl", "start", *active], check=True)


def replace(dest, body):
    attrs = dest.stat()
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(body)
            f.flush()
            if (os.geteuid(), os.getegid()) != (attrs.st_uid, attrs.st_gid):
                os.fchown(f.fileno(), attrs.st_uid, attrs.st_gid)
            os.fchmod(f.fileno(), stat.S_IMODE(attrs.st_mode))
            os.fsync(f.fileno())
            os.replace(tmp, dest)
            parent_fd = os.open(dest.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            tmp.unlink(missing_ok=True)


def install(archive, root, scope, apply):
    root = root.resolve(strict=True)
    changes = inspect(archive, root, scope)
    result = {"scope": scope, "changes": [str(p.relative_to(root)) for p, _, _ in changes], "applied": False}
    if not apply or not changes:
        return result
    names = ([] if scope == "dashboard" else list(AI_TIMERS))
    if scope != "ai":
        names.append("dashboard-monthly-facts")
    # 单次提交的所有文件预检通过后才暂停实际宿主计时器。
    lock = root / ".deploy_tmp/cutover-host-watchers.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        changes = inspect(archive, root, scope)
        if not changes:
            return result
        with quiesce(names) if root == HOST else nullcontext():
            applied = []
            try:
                for dest, current, desired in changes:
                    applied.append((dest, current))
                    replace(dest, desired)
                if inspect(archive, root, scope):
                    raise RuntimeError("宿主脚本安装后哈希不一致")
            except BaseException:
                for dest, current in reversed(applied):
                    replace(dest, current)
                raise
    result["applied"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, help="从此源码目录生成单文件发布制品")
    parser.add_argument("--root", type=Path, default=HOST)
    parser.add_argument("--scope", choices=("all", "ai", "dashboard"), default="all")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="只读核对，列出需要更新的脚本")
    action.add_argument("--apply", action="store_true", help="安装已核对的同一份制品")
    args = parser.parse_args()
    if args.package:
        if args.apply or args.check:
            parser.error("打包与宿主检查/安装必须分开")
        package(Path(__file__).resolve().parents[1], args.package)
        return
    with zipfile.ZipFile(sys.argv[0]) as archive:
        print(json.dumps(install(archive, args.root, args.scope, args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
