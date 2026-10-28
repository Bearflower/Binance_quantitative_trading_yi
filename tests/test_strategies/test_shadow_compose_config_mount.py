"""
静态守护测试：防止 mtcps-xau-shadow 服务通过 bind mount 遮蔽镜像内烘焙的 profile 配置。

背景（2026-10 crash-loop 事故）：
    docker-compose.yml 曾配置宿主目录 ./strategies/btc_eth/shadow/config 挂载到容器内
    /app/strategies/btc_eth/shadow/config:ro。服务器上该宿主目录为空（Docker 会为不存在
    的 bind 源自动创建空目录），空目录遮蔽了镜像通过
    `COPY strategies/btc_eth/ /app/strategies/btc_eth/` 烘焙进去的 xau_original.yaml /
    xau_aggressive.yaml，导致影子采集器启动时找不到 profile 配置、持续 crash-loop。

防护策略：
    镜像烘焙的 profile 配置才是单一可信源（git 跟踪 + Dockerfile COPY），
    运行期不应再用宿主 bind mount 覆盖——宿主缺失时 Docker 会创建空目录，
    造成比"文件缺失"更隐蔽的遮蔽故障。

    判定范围覆盖父目录与子目录两种有害挂载：
      - 宿主 ./strategies/btc_eth/shadow/config → 容器 /app/.../shadow/config:ro（原始事故）
      - 宿主 ./strategies/btc_eth/shadow      → 容器 /app/.../shadow:ro   （父目录遮蔽）
    两者都会遮蔽镜像内 COPY 烘焙的 config/xau_*.yaml，均应被拦截。
"""

from pathlib import Path

import pytest
import yaml


# 仓库根目录（本文件位于 tests/test_strategies/，向上三级）
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# 目标服务名
SERVICE_NAMES = ("mtcps-xau-shadow", "mtcps-xau-calibrated-shadow")

# 判定关键字：bind mount 的宿主侧和容器内路径同时命中此子串
# 即判定为"遮蔽镜像内烘焙 shadow 目录"的有害挂载。
# 覆盖 shadow 父目录挂载与 shadow/config 子目录挂载两种情形——
# 两者都会遮蔽镜像内 COPY 进来的 config/xau_*.yaml。
_SHADOW_PATH_HINT = "strategies/btc_eth/shadow"


def _parse_compose_service_volumes(compose_path: Path, service_name: str) -> list[str]:
    """解析 compose 文件，返回目标服务的 volumes 列表（纯字符串条目）。

    使用 PyYAML 解析，注释会被自动忽略，避免被注释中的示例挂载干扰。
    """
    with compose_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    services = data.get("services", {})
    if service_name not in services:
        pytest.fail(f"docker-compose.yml 中找不到服务 '{service_name}'，compose 结构可能已变更")

    volumes = services[service_name].get("volumes", [])
    return list(volumes)


@pytest.mark.parametrize("service_name", SERVICE_NAMES)
def test_shadow_compose_does_not_mask_image_config(service_name):
    """mtcps-xau-shadow 服务不应使用宿主 bind mount 遮蔽镜像内烘焙的 shadow 目录。

    覆盖两种有害挂载：
      (a) 宿主 .../shadow/config    → 容器 .../shadow/config:ro（原始 crash-loop 事故）
      (b) 宿主 .../shadow           → 容器 .../shadow:ro       （父目录遮蔽 config/ 子目录）

    危害链路相同：宿主目录缺失 → Docker 自动创建空目录 → 遮蔽镜像内 COPY 进来的
    xau_original.yaml / xau_aggressive.yaml → 影子采集器找不到 profile → crash-loop。
    """
    assert COMPOSE_FILE.exists(), f"找不到 docker-compose.yml: {COMPOSE_FILE}"

    volumes = _parse_compose_service_volumes(COMPOSE_FILE, service_name)

    offending_mounts: list[str] = []
    for mount in volumes:
        mount_str = str(mount)
        # 拆分宿主侧 / 容器内路径（格式: ./host/path:/container/path[:mode]）
        parts = mount_str.split(":")
        if len(parts) < 2:
            continue  # named volume（仅一个冒号），不涉及宿主 bind mount
        host_part = parts[0]
        container_part = parts[1]

        # 宿主侧与容器内路径同时命中 shadow 目录关键字 → 有害遮蔽
        # （覆盖 shadow 父目录挂载与 shadow/config 子目录挂载两种情形）
        if _SHADOW_PATH_HINT in host_part and _SHADOW_PATH_HINT in container_part:
            offending_mounts.append(mount_str)

    assert not offending_mounts, (
        f"{service_name} 检测到遮蔽镜像内烘焙 shadow 目录的 bind mount:\n"
        f"  {offending_mounts}\n"
        f"危害：宿主目录缺失时 Docker 会自动创建空目录，遮蔽镜像内 COPY 进来的\n"
        f"config/xau_original.yaml / xau_aggressive.yaml，导致影子采集器 crash-loop。\n"
        f"覆盖情形：(a) shadow/config 子目录挂载  (b) shadow 父目录挂载。\n"
        f"影子 profile 配置应随镜像烘焙（Dockerfile COPY strategies/btc_eth/），\n"
        f"运行期不再使用宿主 bind mount。"
    )
