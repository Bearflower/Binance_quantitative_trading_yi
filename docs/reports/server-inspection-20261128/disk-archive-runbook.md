# 已核验磁盘归档执行单

仅限本次固定路径；本子任务未执行生产写入。执行者先审核脚本和测试，再运行。服务器数据库备份、journal、其他项目镜像与业务缓存全部保留。

## 事实

- `/root/trading_system/backtest/cash_carry/data_2y` 与 `/root/trading_system/cash_carry/data_2y`：各1431文件、810,678,178字节，所有相对路径和文件SHA-256逐项完全一致；原始审核清单为 `historical-csv-review.json`。
- 完整tar+gzip6只读流实测181,052,829字节。远端留单份归档后，两份原目录合计释放约1,440,303,527字节（元数据/新建gzip时间会造成少量差异）。
- 可再下载缓存仅限 `/root/.cache/ms-playwright/chromium-1208`、`chromium_headless_shell-1208`、`ffmpeg-1011`，合计约622MiB。唯一 `.links` 指向 `/root/.npm/_npx/e41f203b7505f1fb/node_modules/playwright-core`，版本1.58.1；浏览器revision为1208、1208、1011。包、`.links`和browsers.json保留。
- 只读时未见以上目录的Docker挂载、进程exe/cwd/fd引用或systemd/cron配置；执行脚本再次检查，并额外检查maps/cmdline。
- `/root/.cache/pip` 已为空，不执行无收益的清理。系统dnf缓存约92MiB，亦不需要处理。
- 观测磁盘：Used52,999,573,504/Total64,356,331,520，精确82.35%。CSV合并归档加622MiB可再下载二进制清理，预计降约79.1%；实际以结束后的df为准。

## 脚本保护

`scripts/ops/archive_inspection_data.py` 默认dry-run，不接收任意删除目录。prepare只生成新归档；apply要求先确认本地副本SHA，并重新校验服务器归档、tar所有成员内容、完整源/缓存快照和使用引用。没有 `shutil.rmtree`；仅按清单逐文件unlink，逐文件核对inode/mtime，目录仅rmdir。未知新增文件、内容变化、包版本变化、引用存在均拒绝。源目录旁保留 `.ARCHIVED.txt` 恢复入口。

使用 `/root/trading_system/.ops/disk-archive.lock` 串行执行prepare/apply；部署操作另外由执行者串行安排，不能因这个锁误认为所有外部进程均停止。

## 命令顺序

1. 上传本次脚本和 `historical-csv-review.json` 到专用暂存目录，例如 `/root/trading_system/.deploy_tmp/server-inspection-20261128-disk/`。不要覆盖已有发布文件。以下在服务器运行：

```bash
nice -n 15 python3 /root/trading_system/.deploy_tmp/server-inspection-20261128-disk/archive_inspection_data.py \
  --mode dry-run \
  --review /root/trading_system/.deploy_tmp/server-inspection-20261128-disk/historical-csv-review.json
```

2. dry-run通过后，创建归档（全新目录，若已存在则拒绝，避免覆盖归档）：

```bash
nice -n 15 python3 /root/trading_system/.deploy_tmp/server-inspection-20261128-disk/archive_inspection_data.py \
  --mode prepare \
  --review /root/trading_system/.deploy_tmp/server-inspection-20261128-disk/historical-csv-review.json
```

产物目录 `/root/trading_system/.archives/server-inspection-20261128/`：`cash-carry-data_2y.tar.gz`、`manifest.json`、`browsers.json`、`RESTORE.md`。先核对输出的archive SHA与大小。

3. 将四个文件复制到本机长期目录，例如 `/Users/yl/.codex/archives/server-inspection-20261128/`；该目录不入Git。必须对实际本机文件运行以下内容校验，而不是只取服务器报告的SHA作为本地证明（在修复worktree执行）：

```python
import json
from pathlib import Path
from scripts.ops.archive_inspection_data import digest_file, verify_archive
p = Path('/Users/yl/.codex/archives/server-inspection-20261128')
m = json.loads((p/'manifest.json').read_text())
f = p/'cash-carry-data_2y.tar.gz'
assert digest_file(f) == m['archive_sha256']
verify_archive(f, m['sources'][0]['snapshot'])
print(m['archive_sha256'])
```

4. 使用上一步本机实际核验通过的SHA运行服务器apply：

```bash
nice -n 15 python3 /root/trading_system/.deploy_tmp/server-inspection-20261128-disk/archive_inspection_data.py \
  --mode apply --verified-local-copy-sha256 <本机核验通过的64位SHA>
```

5. 核对 `applied.json`、远端归档仍可读、五个固定目录的 `.ARCHIVED.txt` 恢复入口，以及 `df -B1 --output=size,used,avail,pcent /`。容器状态和关键服务业务检查必须另行完成。不要把期望节省量当实际释放量。

## 恢复

CSV保留源文件逐项SHA，先校验归档SHA后，对需要恢复的原路径mkdir，再 `tar -xzf cash-carry-data_2y.tar.gz --strip-components=1 -C 原路径`，最后逐项SHA复核。

浏览器使用保留的原版本CLI重下相同revision：

```bash
node /root/.npm/_npx/e41f203b7505f1fb/node_modules/playwright-core/cli.js install chromium
```

如果package或下载源已变化，先按保存的browsers.json与版本1.58.1核对，不能直接假设新版本revision一致。

## 测试

`tests/test_scripts/test_archive_inspection_data.py` 的7项测试验证prepare不删源、本地副本SHA门禁、源或缓存变化拒绝、损坏归档拒绝、越界/符号根拒绝、tar成员逐内容验证、引用失败阻断。连同inventory与巡检来源检查独立跑61项通过。
