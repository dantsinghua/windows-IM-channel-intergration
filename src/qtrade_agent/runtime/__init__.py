"""runtime 模块(02 §2.2.4):docker 编排、端口推导、卷目录、``_purge_ephemeral``、企点 ``ensure_root``(06 §2.9.5 唯一出处)。"""
from .backends import (AdbBackend, AdbCliBackend, ContainerBackend, ContainerInfo, ContainerSpec, DockerCliBackend, FakeAdb,
                       FakeContainers, docker_run_argv)
from .runtime import Runtime, RuntimeInfo, container_name, data_dir, port_plan

__all__ = ["AdbBackend", "AdbCliBackend", "ContainerBackend", "ContainerInfo", "ContainerSpec", "DockerCliBackend", "FakeAdb", "FakeContainers",
           "docker_run_argv", "Runtime", "RuntimeInfo", "container_name", "data_dir", "port_plan"]
