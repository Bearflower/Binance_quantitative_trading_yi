# ai-tuner Override Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ai-tuner override directory writable by container UID/GID 1000, preserve non-root runtime, and surface actionable diagnostics when permissions drift.

**Architecture:** A host-side shell helper owns directory provisioning and is called by both deployment entry points. `ConfigOperator` only verifies writability and reports ownership/mode context. Production deployment uploads only this fix's files, preventing unrelated dirty-worktree changes from entering the image.

**Tech Stack:** Python 3, pytest, Bash, Docker Compose, SSH/SCP

---

## File map

- Modify `ai_tuner/deploy/config_operator.py`: replace ineffective chmod with a writability guard.
- Modify `ai_tuner/tests/test_config_operator.py`: cover permission failure diagnostics.
- Create `scripts/prepare_ai_tuner_overrides.sh`: provision owner and mode on the host.
- Create `tests/scripts/test_prepare_ai_tuner_overrides.sh`: test provisioning behavior.
- Modify `one_click_deploy.sh`: invoke provisioning during full deployment.
- Modify `scripts/deploy_ai_tuner_local.sh`: invoke provisioning during ai-tuner-only deployment.
- Create `tests/scripts/test_ai_tuner_deploy_permission_hooks.py`: enforce hook ordering.

### Task 1: ConfigOperator permission diagnostics

**Files:**
- Modify: `ai_tuner/tests/test_config_operator.py`
- Modify: `ai_tuner/deploy/config_operator.py:15-18,175-188`

- [ ] **Step 1: Write the failing regression test**

```python
from types import SimpleNamespace


def test_unwritable_override_dir_reports_permission_context(
    config_operator, temp_override_dir
):
    config_path = os.path.join(temp_override_dir, "config.yaml")
    with open(config_path, "w", encoding="utf-8") as config_file:
        config_file.write("dummy: config\n")

    fake_stat = SimpleNamespace(st_uid=501, st_gid=20, st_mode=0o40755)
    with (
        patch("ai_tuner.deploy.config_operator.os.access", return_value=False),
        patch("ai_tuner.deploy.config_operator.os.stat", return_value=fake_stat),
        patch("ai_tuner.deploy.config_operator.os.geteuid", return_value=1000),
        patch("ai_tuner.deploy.config_operator.os.getegid", return_value=1000),
        patch("ai_tuner.deploy.config_operator.logger") as mock_logger,
    ):
        result = config_operator.apply_overrides(
            config_path, {"scoring.min_score": 0.75}
        )

    assert result is False
    mock_logger.error.assert_called_once_with(
        "覆盖层目录不可写",
        override_dir=os.path.join(temp_override_dir, "tuning_overrides"),
        process_uid=1000,
        process_gid=1000,
        directory_uid=501,
        directory_gid=20,
        directory_mode="0o755",
    )
    assert not os.path.exists(
        os.path.join(temp_override_dir, "tuning_overrides", ".active")
    )
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest ai_tuner/tests/test_config_operator.py::test_unwritable_override_dir_reports_permission_context -q`.

Expected: FAIL because current code ignores the permission failure and returns success.

- [ ] **Step 3: Implement the minimal guard**

```python
import stat

os.makedirs(override_dir, exist_ok=True)
if not os.access(override_dir, os.W_OK | os.X_OK):
    directory_stat = os.stat(override_dir)
    logger.error(
        "覆盖层目录不可写",
        override_dir=override_dir,
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        directory_uid=directory_stat.st_uid,
        directory_gid=directory_stat.st_gid,
        directory_mode=oct(stat.S_IMODE(directory_stat.st_mode)),
    )
    return False
```

- [ ] **Step 4: Verify GREEN**

Run `python -m pytest ai_tuner/tests/test_config_operator.py -q`.

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ai_tuner/deploy/config_operator.py ai_tuner/tests/test_config_operator.py
git commit -m "fix: report unwritable tuning override directories"
```

### Task 2: Host-side permission provisioning

**Files:**
- Create: `scripts/prepare_ai_tuner_overrides.sh`
- Create: `tests/scripts/test_prepare_ai_tuner_overrides.sh`

- [ ] **Step 1: Write the failing shell test**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/strategies/alpha" "$TEST_ROOT/strategies/beta"
: > "$TEST_ROOT/strategies/alpha/config.yaml"
: > "$TEST_ROOT/strategies/beta/config.yaml"

AI_TUNER_UID="$(id -u)" AI_TUNER_GID="$(id -g)" \
  bash "$REPO_ROOT/scripts/prepare_ai_tuner_overrides.sh" "$TEST_ROOT"

mode_of() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }
for strategy in alpha beta; do
  override_dir="$TEST_ROOT/strategies/$strategy/tuning_overrides"
  test -d "$override_dir"
  test "$(mode_of "$override_dir")" = "775"
done
```

- [ ] **Step 2: Verify RED**

Run `bash tests/scripts/test_prepare_ai_tuner_overrides.sh`.

Expected: FAIL because the helper does not exist.

- [ ] **Step 3: Implement the helper**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="${1:-$DEFAULT_PROJECT_ROOT}"
TUNER_UID="${AI_TUNER_UID:-1000}"
TUNER_GID="${AI_TUNER_GID:-1000}"
prepared=0

for config_path in "$PROJECT_ROOT"/strategies/*/config.yaml; do
  test -e "$config_path" || continue
  override_dir="${config_path%/config.yaml}/tuning_overrides"
  install -d -m 0775 "$override_dir"
  chown "$TUNER_UID:$TUNER_GID" "$override_dir"
  chmod 0775 "$override_dir"
  echo "prepared $override_dir owner=$TUNER_UID:$TUNER_GID mode=0775"
  prepared=$((prepared + 1))
done

if [ "$prepared" -eq 0 ]; then
  echo "no strategy config.yaml files found under $PROJECT_ROOT/strategies" >&2
  exit 1
fi
```

- [ ] **Step 4: Verify GREEN and syntax**

Run `bash tests/scripts/test_prepare_ai_tuner_overrides.sh` and `bash -n scripts/prepare_ai_tuner_overrides.sh`.

Expected: both exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare_ai_tuner_overrides.sh tests/scripts/test_prepare_ai_tuner_overrides.sh
git commit -m "fix: provision ai-tuner override directory ownership"
```

### Task 3: Deployment hooks

**Files:**
- Modify: `one_click_deploy.sh:60-65`
- Modify: `scripts/deploy_ai_tuner_local.sh:29-32,150-189`
- Create: `tests/scripts/test_ai_tuner_deploy_permission_hooks.py`

- [ ] **Step 1: Write failing ordering tests**

```python
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_full_deploy_prepares_overrides_before_container_replacement():
    script = (REPO_ROOT / "one_click_deploy.sh").read_text(encoding="utf-8")
    assert script.index("prepare_ai_tuner_overrides.sh") < script.index("停止旧容器")


def test_ai_tuner_deploy_prepares_overrides_before_branch_selection():
    script = (REPO_ROOT / "scripts/deploy_ai_tuner_local.sh").read_text(
        encoding="utf-8"
    )
    assert script.index("prepare_ai_tuner_overrides.sh") < script.index(
        "路径 A: 本地构建"
    )
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/scripts/test_ai_tuner_deploy_permission_hooks.py -q`.

Expected: both tests FAIL because the hooks are absent.

- [ ] **Step 3: Update full deployment**

After package extraction and before builds or container replacement, add:

```bash
chmod +x scripts/prepare_ai_tuner_overrides.sh
bash scripts/prepare_ai_tuner_overrides.sh "$SERVER_PROJECT_PATH"
```

Do not add `|| true`; provisioning failure must stop deployment.

- [ ] **Step 4: Update ai-tuner-only deployment**

Before branch selection, upload and execute the helper:

```bash
REMOTE_PERMISSION_HELPER="/tmp/prepare_ai_tuner_overrides_${TIMESTAMP}.sh"
$SCP_CMD "$PROJECT_ROOT/scripts/prepare_ai_tuner_overrides.sh" \
  "${SERVER_USER}@${SERVER_IP}:${REMOTE_PERMISSION_HELPER}"
$SSH_CMD "bash '${REMOTE_PERMISSION_HELPER}' '${SERVER_PROJECT_PATH}' && rm -f '${REMOTE_PERMISSION_HELPER}'"
```

Include `scripts/prepare_ai_tuner_overrides.sh` in the server-build source archive.

- [ ] **Step 5: Verify GREEN and syntax**

Run:

```bash
python -m pytest tests/scripts/test_ai_tuner_deploy_permission_hooks.py -q
bash -n one_click_deploy.sh
bash -n scripts/deploy_ai_tuner_local.sh
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add one_click_deploy.sh scripts/deploy_ai_tuner_local.sh tests/scripts/test_ai_tuner_deploy_permission_hooks.py
git commit -m "fix: prepare ai-tuner permissions during deployment"
```

### Task 4: Local regression verification

**Files:** Verify only.

- [ ] **Step 1: Run focused suites**

```bash
python -m pytest ai_tuner/tests/test_config_operator.py tests/scripts/test_ai_tuner_deploy_permission_hooks.py -q
bash tests/scripts/test_prepare_ai_tuner_overrides.sh
```

Expected: all tests PASS.

- [ ] **Step 2: Run static and diff checks**

```bash
bash -n scripts/prepare_ai_tuner_overrides.sh one_click_deploy.sh scripts/deploy_ai_tuner_local.sh
git diff --check HEAD~3..HEAD
```

Expected: exit 0 and no whitespace errors.

### Task 5: Production repair and deployment

**Files:** Upload only `ai_tuner/deploy/config_operator.py` and `scripts/prepare_ai_tuner_overrides.sh`.

- [ ] **Step 1: Capture pre-change state**

Record container/image IDs, `VERSION`, operator SHA-256, and `stat` output for every override directory. Expected state includes the observed `501:20/0755` mismatch.

- [ ] **Step 2: Create rollback artifacts**

Copy the production operator to a timestamped backup and tag the running image `ai-tuner:pre-override-permissions-<timestamp>`. Do not delete images, YAML files, or `.active` pointers.

- [ ] **Step 3: Upload and hash-verify only the fix files**

SCP the operator and helper to temporary server paths, compare local/remote SHA-256, then install the operator at `/root/trading_system/ai_tuner/deploy/config_operator.py`.

- [ ] **Step 4: Repair permissions**

Run `bash /tmp/prepare_ai_tuner_overrides_<timestamp>.sh /root/trading_system`.

Expected: every strategy override directory is `1000:1000/0775`; production YAML and `.active` contents remain unchanged.

- [ ] **Step 5: Execute a real write/rename/delete probe**

Use `docker exec ai-tuner sh -c` to create, rename, and delete `.permission_probe` inside every override directory. Expected: exit 0 and no probe remains.

- [ ] **Step 6: Rebuild and recreate only ai-tuner**

Run `docker compose build --no-cache ai-tuner` and `docker compose up -d ai-tuner --no-deps --force-recreate` from `/root/trading_system`. Do not restart strategy or database containers.

- [ ] **Step 7: Verify deployment and behavior**

Confirm image ID equality, healthy state, port 8777 health response, and operator SHA-256 equality. In `/tmp` inside the container, call `ConfigOperator.apply_overrides()` and confirm version YAML plus `.active` creation; never target a production strategy directory.

- [ ] **Step 8: Review logs**

Inspect logs since the new container start. Expected: no new `Permission denied`, `应用覆盖层异常`, traceback, or health-check failure.

### Task 6: Final handoff

**Files:** Verify only.

- [ ] **Step 1: Report evidence**

Report commits, test counts, production owner/mode, image/container IDs, hash match, health result, atomic-write probe, and log review window.

- [ ] **Step 2: Report rollback coordinates**

Provide the timestamped operator backup path and pre-fix image tag used for rollback.
