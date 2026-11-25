import hashlib
import io
from pathlib import Path
import zipfile

import pytest

from scripts import cutover_host_watchers as installer


def test_pack_contains_only_reviewed_host_sources(tmp_path):
    target = tmp_path / "watchers.pyz"
    installer.package(Path(__file__).resolve().parents[2], target)
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {"__main__.py", *installer.manifest("all")}
        assert len(installer.manifest("all")) == 11
        assert installer.manifest("dashboard") == {
            installer.MONTHLY: (".deploy_tmp/dashboard-monthly-facts-host.py", installer.MONTHLY_HASH)
        }


def test_apply_and_retry_are_atomic_per_file_and_preserve_unrelated_data(tmp_path, monkeypatch):
    entries = [(name, ("old=yes\n#"+name).encode(), ("new=yes\n#"+name).encode()) for name in ('a', 'b')]
    originals = {name: hashlib.sha256(old).hexdigest() for name, old, _ in entries}
    monkeypatch.setattr(installer, "AI_SOURCES", originals)
    (tmp_path/".deploy_tmp").mkdir()
    ops = tmp_path / "ai_tuner/ops"
    ops.mkdir(parents=True)
    separate = tmp_path / ".env"
    separate.write_bytes(b"existing")
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        for name, old, new in entries:
            target = ops/f"{name}.py"
            target.write_bytes(old)
            target.chmod(0o640)
            archive.writestr(f"ai_tuner/ops/{name}.py", new)
    with zipfile.ZipFile(memory) as archive:
        assert len(installer.install(archive, tmp_path, "ai", False)["changes"]) == 2
        swap = installer.replace
        def fail(dest, body):
            if dest.name == "b.py" and body.startswith(b"new"):
                raise OSError("write")
            swap(dest, body)
        with monkeypatch.context() as local:
            local.setattr(installer, "replace", fail)
            with pytest.raises(OSError):
                installer.install(archive, tmp_path, "ai", True)
        assert all((ops/f"{name}.py").read_bytes() == old for name, old, _ in entries)
        assert installer.install(archive, tmp_path, "ai", True)["applied"]
        assert not installer.install(archive, tmp_path, "ai", True)["changes"]
        assert all((ops/f"{name}.py").stat().st_mode & 0o777 == 0o640 for name, _, _ in entries)
        (ops/"a.py").write_bytes(b"changed on host")
        with pytest.raises(ValueError, match="新修订"):
            installer.install(archive, tmp_path, "ai", True)
    assert separate.read_bytes() == b"existing"
