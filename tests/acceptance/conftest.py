"""验收套件全局护栏:运行期间**禁止起任何宿主子进程**(白名单之外一律立即失败,并指出是哪条用例触发的)。

来由(`.omc/handoffs/e2e-rootfs-2.md` D-3):验收夹具曾构造真 ``AgentApp``、未注入假后端,后台 run 真的去执行
``adb -s 127.0.0.1:16001 …``。开发机上有登录着真实工作账号的设备 ⇒ 验收套件**绝不能**有任何机会碰真实
``adb`` / ``docker`` / ``wsl`` / ``powershell`` / ``hwclock`` / ``date -s`` 等宿主命令;用例结果也不得取决于宿主装没装这些命令。

设计:
- **白名单而非黑名单**:只放行「当前解释器」(``sys.executable`` 的真实路径,留给确有需要的 ``python -m …`` 子进程);
  其余可执行文件、以及一切 ``shell=True`` / ``os.system`` 一律拦。黑名单挡不住 ``hwclock``、``date -s`` 这类没想到的命令。
- **拦在最底层的咽喉点**:``subprocess.Popen._execute_child``(``subprocess.run/call/check_output``、
  ``asyncio.create_subprocess_exec/shell`` 最终都经过它,早先 ``from subprocess import Popen`` 拿到的引用也逃不掉)、
  ``os.system``、``os.posix_spawn[p]``、``os.execv[e]``(``execl*``/``execvp*`` 都落到这两个)、``os.spawnv*``、
  ``_posixsubprocess.fork_exec``。被拦的调用在**执行之前**抛 ``HostCommandBlocked``,命令本身从不落地。
- **整场常驻**:第一条用例开跑时装上、会话结束才卸,按「当前(或刚跑完的)用例是否属本目录」决定拦不拦 ——
  本目录用例留下的后台线程在两条用例之间起子进程也照拦;跑到其它目录的用例时一律放行(不影响开发者测试)。
- **被吞掉也照样红**:产品代码可能 ``except Exception`` 吞掉异常(后台 run 记成 failed 就完事)。所以每次拦截都记账,
  该用例所处阶段(setup/call/teardown)结束时账上有违规就 ``pytest.fail``,列出用例 nodeid、阶段、被拦的 argv 与调用栈;
  用例之间(无阶段归属)的违规在会话结束时列入汇总并把退出码置为失败。
"""
from __future__ import annotations

import inspect
import os
import shlex
import shutil
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

import pytest

_HERE = Path(__file__).resolve().parent


_ROOT_CONFTEST = _HERE.parent / "conftest.py"


def pytest_plugin_registered(plugin, plugin_name, manager):
    """🔴 撞名处理:`tests/` 与 `tests/acceptance/` 都没有 `__init__.py`,两个 conftest 的顶层模块名都是 ``conftest``;
    pytest 加载每个无包 conftest 前会 ``del sys.modules['conftest']``,于是本文件一加载,``sys.modules['conftest']`` 就变成本文件,
    之后被导入的 ``from conftest import Clock``(`tests/test_qq_*.py`、本目录 4 个 spec 文件)会落空。
    这里把 ``sys.modules['conftest']`` **钉回 pytest 已注册的那份根 conftest 模块对象**(同一对象,不是重新导入的副本)。
    本钩子是历史钩子:本文件注册时会为此前已注册的插件(含根 conftest)补调一次,之后每注册一个插件再调一次,
    所以不论收集顺序如何,``conftest`` 这个名字始终指向根那份。本文件自身仍以其路径名注册为插件,钩子照常生效。"""
    root = manager.get_plugin(str(_ROOT_CONFTEST))
    if root is None:
        root = next((m for m in manager.get_plugins()
                     if getattr(m, "__file__", None) and os.path.realpath(m.__file__) == str(_ROOT_CONFTEST)), None)
    if root is not None and sys.modules.get("conftest") is not root:
        sys.modules["conftest"] = root


class HostCommandBlocked(RuntimeError):
    """验收护栏:本用例试图起一个白名单之外的宿主子进程。"""


def _real(p: str) -> str:
    return os.path.realpath(p)


#: 可执行文件白名单(真实路径)。只有当前解释器 —— 给需要 ``python -m qtrade_agent.main --init-db`` 一类子进程的用例用。
ALLOWED_EXECUTABLES: frozenset[str] = frozenset({_real(sys.executable)})

_lock = threading.Lock()
_state: dict[str, Any] = {"nodeid": None, "acceptance": False, "phase": None, "installed": None}
_violations: list[dict[str, Any]] = []


def _is_acceptance(item: pytest.Item) -> bool:
    p = Path(str(item.path)).resolve()
    return _HERE == p.parent or _HERE in p.parents


def _as_text(x: Any) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return os.fspath(x) if isinstance(x, os.PathLike) else str(x)


def _resolve(program: str, env: Any) -> Optional[str]:
    if not program:
        return None
    if os.sep in program:
        return _real(program)
    path = None
    try:
        path = (env if isinstance(env, Mapping) else os.environ).get("PATH")
    except Exception:
        path = os.environ.get("PATH")
    found = shutil.which(program, path=_as_text(path) if path is not None else None)
    return _real(found) if found else None


def _check(kind: str, argv: Any, *, executable: Any = None, shell: bool = False, env: Any = None) -> None:
    """放行或拦截。拦截 = 记账 + 在命令执行前抛 ``HostCommandBlocked``。"""
    with _lock:
        nodeid, acceptance, phase = _state["nodeid"], _state["acceptance"], _state["phase"]
    if not acceptance:
        return                                    # 当前跑的是其它目录的用例:不归本护栏管
    if isinstance(argv, (str, bytes, os.PathLike)):
        text = _as_text(argv)
        try:
            parts = shlex.split(text) if shell else [text]
        except ValueError:
            parts = [text]
    else:
        parts = [_as_text(a) for a in argv]
    program = _as_text(executable) if executable is not None and not shell else (parts[0] if parts else "")
    resolved = None if shell else _resolve(program, env)
    if not shell and resolved is not None and resolved in ALLOWED_EXECUTABLES:
        return
    rec = {"nodeid": nodeid, "phase": phase, "kind": kind, "argv": parts, "shell": shell, "resolved": resolved,
           "stack": "".join(traceback.format_stack(limit=14)[:-2])}
    with _lock:
        _violations.append(rec)
    raise HostCommandBlocked(
        f"[验收护栏] 用例 {nodeid}({phase or '用例之间/后台线程'})试图起宿主子进程 {kind}: {parts!r}"
        f"{'(shell=True)' if shell else ''};验收期间只放行当前解释器 {sorted(ALLOWED_EXECUTABLES)}")


def _install() -> list[tuple[Any, str, Any]]:
    import _posixsubprocess
    import subprocess

    saved: list[tuple[Any, str, Any]] = []

    def patch(obj: Any, name: str, new: Any) -> None:
        if hasattr(obj, name):
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, new)

    orig_exec_child = subprocess.Popen._execute_child
    sig = inspect.signature(orig_exec_child)          # 按形参名取 env/shell,不硬编码位置(各 CPython 版本位置不同)

    def _execute_child(self, *a, **kw):
        ba = sig.bind(self, *a, **kw)
        _check("subprocess.Popen", ba.arguments["args"], executable=ba.arguments.get("executable"),
               shell=bool(ba.arguments.get("shell", False)), env=ba.arguments.get("env"))
        return orig_exec_child(self, *a, **kw)

    def _system(command):
        _check("os.system", command, shell=True)
        return saved_system(command)          # 不可达:shell 一律拦;留着只为语义完整

    saved_system = os.system
    patch(subprocess.Popen, "_execute_child", _execute_child)
    patch(os, "system", _system)

    def _spawn_like(kind: str, orig: Any, *, path_pos: int, argv_pos: int, env_pos: Optional[int]):
        def f(*a, **kw):
            env = a[env_pos] if env_pos is not None and len(a) > env_pos else kw.get("env")
            argv = list(a[argv_pos]) if len(a) > argv_pos else []
            _check(kind, argv or [a[path_pos]], executable=a[path_pos], env=env)
            return orig(*a, **kw)
        return f

    for name in ("posix_spawn", "posix_spawnp"):             # (path, argv, env, …)
        if hasattr(os, name):
            patch(os, name, _spawn_like(f"os.{name}", getattr(os, name), path_pos=0, argv_pos=1, env_pos=2))
    for name, env_pos in (("execv", None), ("execve", 2)):    # (path, argv[, env])
        patch(os, name, _spawn_like(f"os.{name}", getattr(os, name), path_pos=0, argv_pos=1, env_pos=env_pos))
    for name, env_pos in (("spawnv", None), ("spawnve", 3), ("spawnvp", None), ("spawnvpe", 3)):   # (mode, file, args[, env])
        if hasattr(os, name):
            patch(os, name, _spawn_like(f"os.{name}", getattr(os, name), path_pos=1, argv_pos=2, env_pos=env_pos))

    orig_fork_exec = _posixsubprocess.fork_exec

    def fork_exec(args, executable_list, *rest, **kw):
        exe = executable_list[0] if executable_list else (args[0] if args else b"")
        _check("_posixsubprocess.fork_exec", list(args or [exe]), executable=exe)
        return orig_fork_exec(args, executable_list, *rest, **kw)

    patch(_posixsubprocess, "fork_exec", fork_exec)
    return saved


def _uninstall(saved: list[tuple[Any, str, Any]]) -> None:
    for obj, name, orig in reversed(saved):
        setattr(obj, name, orig)


# ------------------------------------------------------------------ 钩子
def _ensure_installed() -> None:
    with _lock:
        if _state["installed"] is None:
            _state["installed"] = _install()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    """(经 ``collector.ihook`` 按路径分派 ⇒ 只在收集本目录时进来)收集期导入用例模块时也在护栏内;违规 ⇒ 收集错误。"""
    _ensure_installed()
    with _lock:
        prev = (_state["nodeid"], _state["acceptance"], _state["phase"])
        _state["nodeid"], _state["acceptance"], _state["phase"] = f"<收集期> {collector.nodeid}", True, "collect"
        before = len(_violations)
    try:
        report = yield
    finally:
        with _lock:
            _state["nodeid"], _state["acceptance"], _state["phase"] = prev
            mine = _violations[before:]
            for v in mine:
                v["reported"] = True
    if mine and report.outcome != "failed":
        report.outcome = "failed"
        report.longrepr = "[验收护栏] 收集期试图起宿主子进程(已拦下、未执行):\n" + "\n".join(
            f"  - {v['kind']}: {v['argv']!r}" for v in mine)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """(全局钩子,每条用例都会进来)记下当前用例是否属本目录;第一次进来时装护栏。"""
    _ensure_installed()
    with _lock:
        _state["nodeid"], _state["acceptance"], _state["phase"] = item.nodeid, _is_acceptance(item), None
    try:
        return (yield)
    finally:
        with _lock:
            _state["phase"] = None               # 刚跑完的用例留下的后台线程,仍按它的归属判(本目录 ⇒ 继续拦)


def _phase(item: pytest.Item, phase: str):
    with _lock:
        _state["phase"] = phase
        before = len(_violations)
    try:
        yield
    finally:
        with _lock:
            _state["phase"] = None
            mine = [v for v in _violations[before:] if v["nodeid"] == item.nodeid]
            for v in mine:
                v["reported"] = True
    if mine:
        lines = [f"  - {v['kind']}: {v['argv']!r}{'(shell=True)' if v['shell'] else ''} → {v['resolved']}\n{v['stack']}"
                 for v in mine]
        pytest.fail(f"[验收护栏] {item.nodeid} 在 {phase} 阶段试图起 {len(mine)} 个宿主子进程(已拦下、未执行):\n"
                    + "\n".join(lines), pytrace=False)


def _wrap(item: pytest.Item, phase: str):
    gen = _phase(item, phase)
    next(gen)
    return gen


def _close(gen) -> None:
    try:
        next(gen)
    except StopIteration:
        pass


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    gen = _wrap(item, "setup")
    try:
        return (yield)
    finally:
        _close(gen)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    gen = _wrap(item, "call")
    try:
        return (yield)
    finally:
        _close(gen)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    gen = _wrap(item, "teardown")
    try:
        return (yield)
    finally:
        _close(gen)


def pytest_sessionfinish(session, exitstatus):
    stray = [v for v in _violations if not v.get("reported")]
    if stray and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    stray = [v for v in _violations if not v.get("reported")]
    if stray:
        terminalreporter.section("验收护栏:用例之间 / 后台线程里被拦下的宿主子进程", sep="=", red=True)
        for v in stray:
            terminalreporter.write_line(f"{v['nodeid']}: {v['kind']} {v['argv']!r}")


def pytest_unconfigure(config):
    with _lock:
        saved, _state["installed"] = _state["installed"], None
    if saved:
        _uninstall(saved)
