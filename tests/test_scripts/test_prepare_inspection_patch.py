import subprocess
from pathlib import Path
import pytest
from scripts.prepare_inspection_patch import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'docs/reviews/server-inspection-20261128/server_check.before.sh'

def test_patch_connects_both_checks_and_preserves_existing_script(tmp_path):
    source=SOURCE.read_text(); result=patch(source)
    assert '    check_hrs_acceptance_source\n' in result
    assert '    check_archived_task_failures\n' in result
    assert result.count('    send_feishu_message "$CHECK_RESULT" "$SSH_CHECK_STATUS"') == 1
    target=tmp_path/'server_check.sh';target.write_text(result)
    subprocess.run(['bash','-n',str(target)],check=True)
    with pytest.raises(ValueError): patch(result)

def test_changed_entrypoint_cannot_be_silently_patched():
    with pytest.raises(ValueError): patch('main(){ check_docker_logs; }')
