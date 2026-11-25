"""将已审核的 systemd 收益归属任务绑定到经典 HRS 镜像。暂停后出错保持暂停。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path("/root/trading_system")
UNITS = Path("/etc/systemd/system")
SERVICE = "income-attribution.service"
TIMER = "income-attribution.timer"
OVERRIDE = UNITS / (SERVICE + ".d") / "99-classic-account-image.conf"
RELEASE = ROOT / ".releases/income-attribution/013582c0c86a2d1827bfef1d5fe68e294b98c403"
WRAPPER = RELEASE / "scripts/run_income_attribution.sh"
EXPECTED = {UNITS / SERVICE: "40dda09d31752f056b415746409f26adb8109113aac9a384fc4c3a0d1a5ca0d4",
            UNITS / TIMER: "0c163bfe2837d8c28736807aa4386d9af3fc33b8cc3879da3715388b00d6e36a",
            WRAPPER: "46635271b8fdb7d5ecce847dc206c11c2bfbec011782464eeaa78e13304400f1"}
IMAGE = r"ghcr\.io/bearflower/trading-hrs@sha256:[0-9a-f]{64}"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def setting(unit, name):
    return run("systemctl", "show", unit, "-p", name, "--value")


def verify_units():
    for path, expected in EXPECTED.items():
        if path.resolve() != path or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"宿主已审核归属任务发生变化 {path}")
    for unit in (TIMER, SERVICE):
        allowed = {"", str(OVERRIDE)} if unit == SERVICE else {""}
        if setting(unit, "FragmentPath") != str(UNITS / unit) or setting(unit, "DropInPaths") not in allowed:
            raise ValueError(f"宿主归属任务路径或覆盖发生变化 {unit}")
    if OVERRIDE.exists() and (OVERRIDE.resolve() != OVERRIDE or not re.fullmatch(
            r"\[Service\]\nEnvironment=ATTRIBUTION_IMAGE="+IMAGE+r"\n", OVERRIDE.read_text())):
        raise ValueError("归属任务的镜像覆盖不属于切换流程")


def write_atomic(path, text):
    path.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    Path(file.name).chmod(0o644)
    os.replace(file.name, path)


def paused_state(folder):
    verify_units()
    marker = folder / "income-timer-previous-state"
    if not marker.exists():
        state = setting(TIMER, "ActiveState")
        if state not in {"active", "inactive"}:
            raise ValueError("归属定时器正在切换状态")
        write_atomic(marker, state)
    run("systemctl", "stop", TIMER)
    if setting(SERVICE, "ActiveState") not in {"inactive", "failed"}:
        raise ValueError("已有归属核对正在运行，等待完成后重试")
    return marker.read_text()


def resume(folder, revision):
    state = paused_state(folder)
    image_id = run("docker", "inspect", "-f", "{{.Image}}", "trading_system-hrs")
    refs = json.loads(run("docker", "image", "inspect", "-f", "{{json .RepoDigests}}", image_id))
    exact = [ref for ref in refs if re.fullmatch(IMAGE, ref)]
    built = run("docker", "exec", "trading_system-hrs", "printenv", "HRS_IMAGE_REVISION")
    if len(exact) != 1 or not re.fullmatch(r"[0-9a-f]{40}", revision) or built != revision:
        raise ValueError("运行中 HRS 的实际构建版本或镜像摘要未核对")
    image = exact[0]
    shadow = folder / "income-attribution-shadow.json"
    env = dict(os.environ, ATTRIBUTION_IMAGE=image, ATTRIBUTION_OUTPUT=str(shadow))
    subprocess.run(["/bin/sh", str(WRAPPER), "--current-month"], env=env, check=True)
    proof = json.loads(shadow.read_text())
    account = run("docker", "exec", "trading_system-hrs", "printenv", "TRADE_ACCOUNT_ID")
    cutover = int(run("docker", "exec", "trading_system-hrs", "printenv", "BINANCE_CLASSIC_CUTOVER_MS"))
    if (proof.get("mode") != "shadow" or proof.get("account_mode") != "classic"
            or proof.get("account_id") != account or proof.get("cutover_ms") != cutover
            or proof.get("window_start_ms", -1) < cutover):
        raise ValueError("归属预演的账户、模式或时间范围与 HRS 不符")
    write_atomic(OVERRIDE, f"[Service]\nEnvironment=ATTRIBUTION_IMAGE={image}\n")
    run("systemctl", "daemon-reload")
    verify_units()
    if f"ATTRIBUTION_IMAGE={image}" not in setting(SERVICE, "Environment").split():
        raise ValueError("systemd 未加载已核对的 HRS 镜像摘要")
    if state == "active":
        run("systemctl", "start", TIMER)
        if setting(TIMER, "ActiveState") != "active":
            raise RuntimeError("归属定时器恢复失败")
    elif state != "inactive":
        raise ValueError("归属定时器原状态无效")


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("action", choices=("pause", "resume"))
    cli.add_argument("--sha", required=True)
    args = cli.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.sha):
        cli.error("需要完整 Git 版本")
    folder = ROOT / ".deploy_tmp" / ("account-mode-" + args.sha)
    if args.action == "pause":
        paused_state(folder)
    else:
        resume(folder, args.sha)
