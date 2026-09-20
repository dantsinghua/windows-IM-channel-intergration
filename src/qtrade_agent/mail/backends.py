"""IMAP/POP3/SMTP 的可注入后端 —— 规格:docs/06 §2.1(两条收信循环 + SMTP 发送)、§2.6.5(删除动作的协议差异)。

设计:协议(``Protocol``)定义动作面,``Fake*`` 供测试(不碰网络),``*LibBackend`` 是标准库真实现
(``imaplib``/``poplib``/``smtplib``;**开发容器里绝不实例化、绝不连任何真实邮箱**)。

异常分类是 §2.1.1 回落判据的**唯一依据**:
- ``MailConnectError``  = 连不上/不支持(connect_refused / connect_timeout / tls_handshake_failed / imap_not_enabled)⇒ 计入回落计数;
- ``MailAuthError``     = 认证失败(535 / ``NO [AUTHENTICATIONFAILED]`` / ``-ERR``)⇒ **不回落**(密码在 POP3 上一样错),发 ``MAIL_AUTH_FAILED``;
- ``MailTransientError``= 其它(网络断、IDLE 异常)⇒ 走 §5「网络断」分支 ``sleep(10)`` 重连,不计入回落计数。
SMTP 另分 ``SmtpPermanentError``(5xx:550/552/553 → 直接 DEAD,不重试)与 ``SmtpTemporaryError``(421/450/451/452 与网络错 → 退避)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class MailError(Exception):
    """邮件后端异常基类。"""


class MailConnectError(MailError):
    """连不上/服务商不支持(§2.1.1 回落判据的唯一命中项)。``kind`` ∈ connect_refused|connect_timeout|tls_handshake_failed|imap_not_enabled。"""

    def __init__(self, message: str, *, kind: str = "connect_refused"):
        super().__init__(message)
        self.kind = kind


class MailAuthError(MailError):
    """认证失败——**不是**回落条件(§2.1.1)。"""


class MailTransientError(MailError):
    """其它暂态错误:走 §5「网络断」分支重连,不计入回落计数。"""


class SmtpTemporaryError(MailError):
    """SMTP 4xx 临时码(421/450/451/452)与网络错误:按 ``backoff_s`` 退避(§2.1)。"""


class SmtpPermanentError(MailError):
    """SMTP 5xx 永久码(550 收件人不存在 / 552 超限 / 553):**不重试直接 DEAD**(§2.1)。"""


# ---------------------------------------------------------------------------- 协议
class ImapBackend(Protocol):
    def connect_login(self) -> None: ...
    def capabilities(self) -> set[str]: ...
    def select(self, folder: str, *, readonly: bool = True) -> tuple[int, int]: ...
    def search_uids(self, since_uid: int) -> list[int]: ...
    def fetch_size(self, uid: int) -> int: ...
    def fetch_headers(self, uid: int) -> bytes: ...
    def fetch_raw(self, uid: int) -> bytes: ...
    def create_folder(self, folder: str) -> None: ...
    def uid_move(self, uid: int, dest: str) -> Optional[int]: ...
    def uid_copy(self, uid: int, dest: str) -> Optional[int]: ...
    def store_deleted(self, uid: int) -> None: ...
    def uid_expunge(self, uids: list[int]) -> None: ...
    def expunge_folder(self) -> None: ...
    def search_deleted(self) -> list[int]: ...
    def quota(self) -> Optional[tuple[int, int]]: ...
    def idle_wait(self, timeout_s: float) -> bool: ...
    def logout(self) -> None: ...


class Pop3Backend(Protocol):
    def connect_login(self) -> None: ...
    def stat(self) -> tuple[int, int]: ...
    def uidl(self) -> list[tuple[int, str]]: ...
    def list_size(self, n: int) -> int: ...
    def top(self, n: int, lines: int = 0) -> bytes: ...
    def retr(self, n: int) -> bytes: ...
    def dele(self, n: int) -> None: ...
    def quit(self) -> bool: ...


class SmtpBackend(Protocol):
    def connect_login(self) -> None: ...
    def send(self, from_addr: str, to_addrs: list[str], msg_bytes: bytes) -> str: ...
    def quit(self) -> None: ...


def _headers_of(raw: bytes) -> bytes:
    """取原文的头部分(到第一个空行为止);``IMAP BODY.PEEK[HEADER]`` / ``POP3 TOP n 0`` 的等价物。"""
    for sep in (b"\r\n\r\n", b"\n\n"):
        head, found, _ = raw.partition(sep)
        if found:
            return head + sep
    return raw


# ---------------------------------------------------------------------------- 假实现(测试用;不碰网络)
@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    size: Optional[int] = None          # None = len(raw);用于造「大信」而不真的造 25 MB 字节
    flags: set[str] = field(default_factory=set)

    @property
    def size_bytes(self) -> int:
        return self.size if self.size is not None else len(self.raw)


class FakeImap:
    """假 IMAP 服务器 + 客户端(一体)。``folders`` 为 ``{夹名: [FakeMessage]}``,UID 全局递增。"""

    def __init__(self, *, caps: Optional[set[str]] = None, uidvalidity: int = 1,
                 quota_used: Optional[int] = None, quota_limit: Optional[int] = None):
        self.caps = set(caps if caps is not None else {"IMAP4REV1", "IDLE", "UIDPLUS", "MOVE", "QUOTA", "ID"})
        self.folders: dict[str, list[FakeMessage]] = {"INBOX": [], "Junk": []}
        self.uidvalidity = uidvalidity
        self.quota_used = quota_used
        self.quota_limit = quota_limit
        self.selected = "INBOX"
        self.logged_in = False
        self._next_uid = 1
        self.expunged: list[int] = []               # 真删过的 UID(验收断言用)
        self.moved: list[tuple[int, str]] = []      # (uid, dest)
        self.raise_on_connect: Optional[Exception] = None
        self.raise_on_fetch: Optional[Exception] = None
        self.idle_pending = False
        self.id_sent = False

    # ---- 造数据(测试侧)
    def add(self, raw: bytes, *, folder: str = "INBOX", size: Optional[int] = None) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self.folders.setdefault(folder, []).append(FakeMessage(uid, raw, size))
        return uid

    def find(self, uid: int, folder: Optional[str] = None) -> Optional[FakeMessage]:
        for m in self.folders.get(folder or self.selected, []):
            if m.uid == uid:
                return m
        return None

    def uids_in(self, folder: str) -> list[int]:
        return [m.uid for m in self.folders.get(folder, [])]

    # ---- 协议面
    def connect_login(self) -> None:
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.logged_in = True
        self.id_sent = True

    def capabilities(self) -> set[str]:
        return set(self.caps)

    def select(self, folder: str, *, readonly: bool = True) -> tuple[int, int]:
        self.folders.setdefault(folder, [])
        self.selected = folder
        return self.uidvalidity, len(self.folders[folder])

    def search_uids(self, since_uid: int) -> list[int]:
        msgs = self.folders.get(self.selected, [])
        if not msgs:
            return []
        # IMAP 规定 `n:*` 至少返回最大 UID —— 照真服务器把最后一封也塞回来,由调用方过滤 > last_uid
        got = [m.uid for m in msgs if m.uid >= since_uid]
        if not got:
            got = [msgs[-1].uid]
        return sorted(got)

    def fetch_size(self, uid: int) -> int:
        m = self.find(uid)
        if m is None:
            raise MailTransientError(f"uid {uid} 不存在")
        return m.size_bytes

    def fetch_headers(self, uid: int) -> bytes:
        """``BODY.PEEK[HEADER]``:只取头,**不是正文**——OVERSIZE 分支「只登记元数据」用(§2.1 / §5)。"""
        m = self.find(uid)
        if m is None:
            raise MailTransientError(f"uid {uid} 不存在")
        return _headers_of(m.raw)

    def fetch_raw(self, uid: int) -> bytes:
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        m = self.find(uid)
        if m is None:
            raise MailTransientError(f"uid {uid} 不存在")
        return m.raw

    def create_folder(self, folder: str) -> None:
        self.folders.setdefault(folder, [])

    def uid_move(self, uid: int, dest: str) -> Optional[int]:
        if "MOVE" not in self.caps:
            raise MailTransientError("服务器不支持 MOVE")
        m = self.find(uid)
        if m is None:
            return None
        self.folders[self.selected].remove(m)
        self.create_folder(dest)
        new_uid = self._next_uid
        self._next_uid += 1
        self.folders[dest].append(FakeMessage(new_uid, m.raw, m.size))
        self.moved.append((uid, dest))
        return new_uid

    def uid_copy(self, uid: int, dest: str) -> Optional[int]:
        m = self.find(uid)
        if m is None:
            return None
        self.create_folder(dest)
        new_uid = self._next_uid
        self._next_uid += 1
        self.folders[dest].append(FakeMessage(new_uid, m.raw, m.size))
        self.moved.append((uid, dest))
        return new_uid

    def store_deleted(self, uid: int) -> None:
        m = self.find(uid)
        if m is not None:
            m.flags.add("\\Deleted")

    def uid_expunge(self, uids: list[int]) -> None:
        if "UIDPLUS" not in self.caps:
            raise MailTransientError("服务器不支持 UIDPLUS")
        keep = []
        for m in self.folders.get(self.selected, []):
            if m.uid in uids and "\\Deleted" in m.flags:
                self.expunged.append(m.uid)
            else:
                keep.append(m)
        self.folders[self.selected] = keep

    def expunge_folder(self) -> None:
        keep = []
        for m in self.folders.get(self.selected, []):
            if "\\Deleted" in m.flags:
                self.expunged.append(m.uid)
            else:
                keep.append(m)
        self.folders[self.selected] = keep

    def search_deleted(self) -> list[int]:
        return [m.uid for m in self.folders.get(self.selected, []) if "\\Deleted" in m.flags]

    def quota(self) -> Optional[tuple[int, int]]:
        if "QUOTA" not in self.caps or self.quota_limit is None:
            return None
        return int(self.quota_used or 0), int(self.quota_limit)

    def idle_wait(self, timeout_s: float) -> bool:
        pending, self.idle_pending = self.idle_pending, False
        return pending

    def logout(self) -> None:
        self.logged_in = False


class FakePop3:
    """假 POP3 服务器 + 客户端。``DELE`` 只标记,``quit()`` 返回 ``+OK`` 时才真删(§2.1 表:`QUIT` 正常结束才生效)。"""

    def __init__(self):
        self.messages: list[tuple[str, bytes, Optional[int]]] = []   # (uidl, raw, size)
        self.logged_in = False
        self._marked: set[int] = set()
        self.deleted: list[str] = []                 # 真删掉的 uidl(验收断言用)
        self.raise_on_connect: Optional[Exception] = None
        self.quit_ok = True                          # False = QUIT 未 +OK ⇒ 服务器撤销全部 DELE

    # ---- 造数据
    def add(self, uidl: str, raw: bytes, *, size: Optional[int] = None) -> str:
        self.messages.append((uidl, raw, size))
        return uidl

    def uidls(self) -> list[str]:
        return [u for u, _, _ in self.messages]

    # ---- 协议面
    def connect_login(self) -> None:
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.logged_in = True
        self._marked = set()

    def stat(self) -> tuple[int, int]:
        return len(self.messages), sum((s if s is not None else len(r)) for _, r, s in self.messages)

    def uidl(self) -> list[tuple[int, str]]:
        return [(i + 1, u) for i, (u, _, _) in enumerate(self.messages)]

    def list_size(self, n: int) -> int:
        uidl, raw, size = self.messages[n - 1]
        return size if size is not None else len(raw)

    def top(self, n: int, lines: int = 0) -> bytes:
        raw = self.messages[n - 1][1]
        head, _, _ = raw.partition(b"\r\n\r\n")
        return head

    def retr(self, n: int) -> bytes:
        return self.messages[n - 1][1]

    def dele(self, n: int) -> None:
        self._marked.add(n)

    def quit(self) -> bool:
        if not self.quit_ok:
            self._marked = set()            # RSET 语义:断线/失败撤销全部 DELE
            self.logged_in = False
            return False
        keep = []
        for i, item in enumerate(self.messages, start=1):
            if i in self._marked:
                self.deleted.append(item[0])
            else:
                keep.append(item)
        self.messages = keep
        self._marked = set()
        self.logged_in = False
        return True


class FakeSmtp:
    """假 SMTP。``sent`` 存 ``(from, to[], msg_bytes)``;``fail_times``/``fail_exc`` 造失败。"""

    def __init__(self):
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.logged_in = False
        self.fail_times = 0
        self.fail_exc: Exception = SmtpTemporaryError("451 临时故障")
        self.raise_on_connect: Optional[Exception] = None

    def connect_login(self) -> None:
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.logged_in = True

    def send(self, from_addr: str, to_addrs: list[str], msg_bytes: bytes) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exc
        self.sent.append((from_addr, list(to_addrs), msg_bytes))
        return "250 OK"

    def quit(self) -> None:
        self.logged_in = False


# ---------------------------------------------------------------------------- 标准库真实现(只包协议动作,开发机不跑)
class ImapLibBackend:
    """``imaplib`` 实现。163/126/yeah 登录后必须发 ``ID``(§2.1 服务商怪癖),否则 ``EXAMINE`` 报 ``Unsafe Login``。"""

    def __init__(self, *, host: str, port: int, ssl: bool, user: str, password: str,
                 send_id: bool = True, timeout_s: int = 30):
        self.host, self.port, self.ssl = host, port, ssl
        self.user, self.password = user, password
        self.send_id, self.timeout_s = send_id, timeout_s
        self._c: Any = None
        self._selected = ""
        self._caps: set[str] = set()

    def connect_login(self) -> None:
        import imaplib
        import socket
        import ssl as ssl_mod
        try:
            if self.ssl:
                self._c = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout_s)
            else:
                self._c = imaplib.IMAP4(self.host, self.port, timeout=self.timeout_s)
                self._c.starttls()
        except (ConnectionRefusedError, socket.gaierror) as e:
            raise MailConnectError(str(e), kind="connect_refused") from e
        except (socket.timeout, TimeoutError) as e:
            raise MailConnectError(str(e), kind="connect_timeout") from e
        except ssl_mod.SSLError as e:
            raise MailConnectError(str(e), kind="tls_handshake_failed") from e
        except imaplib.IMAP4.error as e:
            raise MailConnectError(str(e), kind="imap_not_enabled") from e
        try:
            if self.send_id:
                self._c._simple_command("ID", '("name" "qtrade" "version" "1.0")')   # 163/126/yeah 必须
            self._c.login(self.user, self.password)
        except imaplib.IMAP4.error as e:
            raise MailAuthError(str(e)) from e
        self._caps = {c.decode().upper() for c in (self._c.capabilities or ())}

    def capabilities(self) -> set[str]:
        return set(self._caps)

    def select(self, folder: str, *, readonly: bool = True) -> tuple[int, int]:
        typ, data = self._c.select(f'"{folder}"', readonly=readonly)
        if typ != "OK":
            raise MailTransientError(f"SELECT {folder} 失败:{data!r}")
        self._selected = folder
        exists = int(data[0]) if data and data[0] else 0
        typ, uv = self._c.response("UIDVALIDITY")
        uidvalidity = int(uv[0]) if uv and uv[0] else 0
        return uidvalidity, exists

    def _uid(self, cmd: str, *args: str) -> list[bytes]:
        typ, data = self._c.uid(cmd, *args)
        if typ != "OK":
            raise MailTransientError(f"UID {cmd} 失败:{data!r}")
        return list(data or [])

    def search_uids(self, since_uid: int) -> list[int]:
        data = self._uid("SEARCH", None, f"{max(1, since_uid)}:*")   # type: ignore[arg-type]
        raw = (data[0] or b"").split() if data else []
        return sorted(int(x) for x in raw)

    def fetch_size(self, uid: int) -> int:
        data = self._uid("FETCH", str(uid), "(RFC822.SIZE)")
        for item in data:
            if isinstance(item, bytes) and b"RFC822.SIZE" in item:
                tail = item.split(b"RFC822.SIZE", 1)[1]
                digits = b"".join(ch.to_bytes(1, "big") for ch in tail if 48 <= ch <= 57)
                if digits:
                    return int(digits)
        raise MailTransientError(f"取不到 uid {uid} 的 RFC822.SIZE")

    def fetch_headers(self, uid: int) -> bytes:
        data = self._uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")    # 只取头,不取正文(OVERSIZE 分支)
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        raise MailTransientError(f"取不到 uid {uid} 的邮件头")

    def fetch_raw(self, uid: int) -> bytes:
        data = self._uid("FETCH", str(uid), "(BODY.PEEK[])")          # PEEK:不改已读态
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        raise MailTransientError(f"取不到 uid {uid} 的正文")

    def create_folder(self, folder: str) -> None:
        self._c.create(f'"{folder}"')

    def uid_move(self, uid: int, dest: str) -> Optional[int]:
        data = self._uid("MOVE", str(uid), f'"{dest}"')
        return _copyuid(data)

    def uid_copy(self, uid: int, dest: str) -> Optional[int]:
        data = self._uid("COPY", str(uid), f'"{dest}"')
        return _copyuid(data)

    def store_deleted(self, uid: int) -> None:
        self._uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")

    def uid_expunge(self, uids: list[int]) -> None:
        self._uid("EXPUNGE", ",".join(str(u) for u in uids))

    def expunge_folder(self) -> None:
        self._c.expunge()

    def search_deleted(self) -> list[int]:
        data = self._uid("SEARCH", None, "DELETED")                   # type: ignore[arg-type]
        raw = (data[0] or b"").split() if data else []
        return sorted(int(x) for x in raw)

    def quota(self) -> Optional[tuple[int, int]]:
        if "QUOTA" not in self._caps:
            return None
        typ, data = self._c.getquotaroot("INBOX")
        if typ != "OK":
            return None
        for item in (data[1] if len(data) > 1 else []) or []:
            text = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            if "STORAGE" in text:
                nums = [int(x) for x in text.replace("(", " ").replace(")", " ").split() if x.isdigit()]
                if len(nums) >= 2:
                    return nums[0] * 1024, nums[1] * 1024          # RFC 2087 单位是 KiB
        return None

    def idle_wait(self, timeout_s: float) -> bool:
        """``imaplib`` 无 IDLE 封装:按 §2.1「服务器不宣告 IDLE 就降级轮询」处理,恒返回 False。"""
        return False

    def logout(self) -> None:
        try:
            self._c.logout()
        except Exception:                                             # noqa: BLE001 —— 关连接失败不该影响主流程
            pass


def _copyuid(data: list[bytes]) -> Optional[int]:
    """从 ``COPYUID`` 响应里取新 UID(§2.6.5:移动后 UID 变化,``mail_inbox`` 记新夹与新 UID)。"""
    for item in data:
        text = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
        if "COPYUID" in text:
            parts = text.replace("]", " ").split()
            nums = [p for p in parts if p.isdigit()]
            if len(nums) >= 3:
                return int(nums[-1])
    return None


class PopLibBackend:
    """``poplib`` 实现。``DELE`` 只是标记,``QUIT`` 正常结束才生效(§2.1)。"""

    def __init__(self, *, host: str, port: int, ssl: bool, user: str, password: str, timeout_s: int = 30):
        self.host, self.port, self.ssl = host, port, ssl
        self.user, self.password = user, password
        self.timeout_s = timeout_s
        self._c: Any = None

    def connect_login(self) -> None:
        import poplib
        import socket
        import ssl as ssl_mod
        try:
            if self.ssl:
                self._c = poplib.POP3_SSL(self.host, self.port, timeout=self.timeout_s)
            else:
                self._c = poplib.POP3(self.host, self.port, timeout=self.timeout_s)
                self._c.stls()
        except (ConnectionRefusedError, socket.gaierror) as e:
            raise MailConnectError(str(e), kind="connect_refused") from e
        except (socket.timeout, TimeoutError) as e:
            raise MailConnectError(str(e), kind="connect_timeout") from e
        except ssl_mod.SSLError as e:
            raise MailConnectError(str(e), kind="tls_handshake_failed") from e
        try:
            self._c.user(self.user)
            self._c.pass_(self.password)
        except poplib.error_proto as e:
            raise MailAuthError(str(e)) from e

    def stat(self) -> tuple[int, int]:
        count, octets = self._c.stat()
        return int(count), int(octets)

    def uidl(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for line in self._c.uidl()[1]:
            text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
            parts = text.split()
            if len(parts) >= 2 and parts[0].isdigit():
                out.append((int(parts[0]), parts[1]))
        return out

    def list_size(self, n: int) -> int:
        text = self._c.list(n)
        text = text.decode(errors="replace") if isinstance(text, bytes) else str(text)
        parts = text.split()
        return int(parts[-1])

    def top(self, n: int, lines: int = 0) -> bytes:
        return b"\r\n".join(self._c.top(n, lines)[1])

    def retr(self, n: int) -> bytes:
        return b"\r\n".join(self._c.retr(n)[1])

    def dele(self, n: int) -> None:
        self._c.dele(n)

    def quit(self) -> bool:
        import poplib
        try:
            resp = self._c.quit()
        except poplib.error_proto:
            return False
        return bool(resp) and resp.startswith(b"+OK")


class SmtpLibBackend:
    """``smtplib`` 实现。``ssl=True`` → ``SMTP_SSL``(465);``ssl=False`` → ``SMTP`` + ``STARTTLS``(失败即失败,不降级明文)。"""

    def __init__(self, *, host: str, port: int, ssl: bool, user: str, password: str, timeout_s: int = 30):
        self.host, self.port, self.ssl = host, port, ssl
        self.user, self.password = user, password
        self.timeout_s = timeout_s
        self._c: Any = None

    def connect_login(self) -> None:
        import smtplib
        import socket
        try:
            if self.ssl:
                self._c = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout_s)
            else:
                self._c = smtplib.SMTP(self.host, self.port, timeout=self.timeout_s)
                self._c.starttls()
        except (ConnectionRefusedError, socket.gaierror, socket.timeout, TimeoutError, OSError) as e:
            raise SmtpTemporaryError(str(e)) from e
        try:
            if self.user:
                self._c.login(self.user, self.password)
        except smtplib.SMTPAuthenticationError as e:
            raise MailAuthError(str(e)) from e      # 535:按「环境故障」退避 + MAIL_AUTH_FAILED(§2.1)
        except smtplib.SMTPException as e:
            raise SmtpTemporaryError(str(e)) from e

    def send(self, from_addr: str, to_addrs: list[str], msg_bytes: bytes) -> str:
        import smtplib
        try:
            self._c.sendmail(from_addr, to_addrs, msg_bytes)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
            code = getattr(e, "smtp_code", None)
            if code is None and isinstance(getattr(e, "recipients", None), dict):
                code = next(iter(e.recipients.values()))[0] if e.recipients else None
            if code is not None and 500 <= int(code) < 600:
                raise SmtpPermanentError(str(e)) from e              # 5xx:不重试直接 DEAD
            raise SmtpTemporaryError(str(e)) from e
        except smtplib.SMTPException as e:
            raise SmtpTemporaryError(str(e)) from e
        return "250 OK"

    def quit(self) -> None:
        try:
            self._c.quit()
        except Exception:                                            # noqa: BLE001
            pass
