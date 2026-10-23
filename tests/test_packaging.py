"""打包自检:**真打一个 wheel**,开包断言运行期要读的数据文件都在里面。

为什么要有这条:`pyproject.toml` 的 `[tool.setuptools.package-data]` 是**逐条声明**的白名单 ——
新增一个数据文件却忘了声明,源码树跑测试全绿、装进发行版却读到不存在的目录(企点 profile 漏一次 =
登录与发送全挂)。源码树的测试**结构上看不见**这类问题,只有真打包才看得见。

🔴 **必须在干净的临时树里打包**:`setuptools` 的 `build/lib/` 是增量缓存,上一次打包拷进去的文件会被
下一次直接复用 —— 在源码树里打,哪怕把 `package-data` 的声明删掉,漏掉的文件仍会从缓存里"进包",
本条测试就成了安慰剂(2026-09-21 实测踩到:删掉 profiles 声明后测试照样全绿)。故只把
`pyproject.toml` + `src/` 复制到 tmp 再打,顺带也不在源码树留 `egg-info`。

🔴 不联网:用 `--no-build-isolation`(本机已有 setuptools/wheel,不去 PyPI 拉构建后端)+ `--no-deps`。
`~/.venvs/qtrade` 是 uv 建的、没有 `pip` 模块,故自动挑一个带 pip + setuptools 的解释器。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

#: 运行期必须能读到的数据文件(相对 `src/`)。新增一类就往这里加一条。
MUST_HAVE = ["qtrade_agent/store/schema_agent.sql",
             "qtrade_agent/adapters/qidian/profiles/default.yaml"]

#: 只给人看、不参与运行的,允许不进包。
DOC_SUFFIXES = (".md",)

#: 构建副产物,不是源码树的数据文件(`--no-build-isolation` 会在 `src/` 下生成 egg-info)。
BUILD_ARTIFACT_DIRS = (".egg-info", ".dist-info")


def _packager() -> str:
    """挑一个能打包的解释器:先当前解释器,再 `/usr/bin/python3`。都不行 ⇒ 跳过并说清原因。"""
    for exe in (sys.executable, "/usr/bin/python3", "python3"):
        if not exe:
            continue
        r = subprocess.run([exe, "-c", "import pip, setuptools"], capture_output=True)
        if r.returncode == 0:
            return exe
    pytest.skip("本机没有带 pip + setuptools 的解释器,打不了 wheel(装好后本条自动恢复)")


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> list[str]:
    exe = _packager()
    work = tmp_path_factory.mktemp("src")           # 干净树:不带 build/ 缓存、不带 egg-info
    out = tmp_path_factory.mktemp("wheel")
    shutil.copy2(REPO / "pyproject.toml", work / "pyproject.toml")
    shutil.copytree(SRC, work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    r = subprocess.run([exe, "-m", "pip", "wheel", str(work), "-w", str(out), "--no-deps", "--no-build-isolation", "-q"],
                       capture_output=True, text=True, cwd=str(work), env=env, timeout=600)
    assert r.returncode == 0, f"pip wheel 失败 rc={r.returncode}\nstdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}"
    wheels = glob.glob(str(out / "qtrade_agent-*.whl"))
    assert len(wheels) == 1, f"期望正好一个 wheel,实得 {wheels}"
    with zipfile.ZipFile(wheels[0]) as z:
        return z.namelist()


def test_必读数据文件都在wheel里(wheel_names):
    missing = [p for p in MUST_HAVE if p not in wheel_names]
    assert not missing, f"这些运行期数据文件没打进 wheel(去 pyproject.toml 的 package-data 补声明):{missing}"


def test_能力目录十六个json都在wheel里(wheel_names):
    in_wheel = sorted(n for n in wheel_names if n.startswith("qtrade_agent/capabilities/") and n.endswith(".json"))
    on_disk = sorted("qtrade_agent/capabilities/" + p.name for p in (SRC / "qtrade_agent/capabilities").glob("*.json"))
    assert in_wheel == on_disk
    assert len(in_wheel) == 16, f"能力目录应有 16 个 json(02 §3.10),实得 {len(in_wheel)}"


def test_源码树里任何非py数据文件都不许漏进包(wheel_names):
    """通用守卫:以后**新增**任何数据文件、忘了写 package-data,这条先红。"""
    missing = []
    for path in SRC.rglob("*"):
        if not path.is_file() or path.suffix == ".py" or path.suffix in DOC_SUFFIXES:
            continue
        if "__pycache__" in path.parts or any(part.endswith(BUILD_ARTIFACT_DIRS) for part in path.parts):
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel not in wheel_names:
            missing.append(rel)
    assert not missing, f"源码树里有数据文件没进 wheel:{missing}"


def test_企点profile装包后仍能按包内资源定位():
    """`ui.load_profiles()` 缺省走 `importlib.resources`,不拼 `__file__` 旁边的相对路径。"""
    import inspect

    from qtrade_agent.adapters.qidian import ui
    assert "resources.files" in inspect.getsource(ui.profiles_root)
    assert "default" in ui.load_profiles()
