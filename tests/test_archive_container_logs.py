"""部署前保留 grid journald 当前容器日志。"""
import os
import subprocess
from pathlib import Path


def test_archive_includes_grid_journald_and_skips_other_journald(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fake = tmp_path / "docker"
    fake.write_text("""#!/bin/sh
if [ "$1" = ps ]; then
  printf 'trading_system-grid-realtime\\nother-service\\n'
elif [ "$1" = inspect ]; then
  if [ "$3" = '{{.Id}}' ]; then printf 'abcdef\\n';
  else printf 'journald\\n'; fi
elif [ "$1" = logs ]; then
  printf '2026-11-03T00:00:00Z started\\n2026-11-03T00:00:01Z running\\n'
fi
""")
    fake.chmod(0o755)
    archive = tmp_path / "archive"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(root / "scripts/archive_container_logs.sh"), str(archive)],
        env=env, text=True, capture_output=True, check=True)
    assert "containers=1" in result.stdout
    assert (archive / "trading_system-grid-realtime.log").exists()
    assert not (archive / "other-service.log").exists()
    manifest = (archive / "manifest.tsv").read_text()
    assert "trading_system-grid-realtime\tabcdef\t2026-11-03T00:00:00Z" in manifest
