"""Reconnect existing QQ transports without starting a login or a container."""
from __future__ import annotations
import asyncio
import time
from ..base import Account


class ExistingQQRecovery:
    def __init__(self, store, adapter, *, busy=lambda aid: False, clock=time.monotonic):
        self.store = store
        self.adapter = adapter
        self.busy = busy
        self.clock = clock
        self.verified = set()
        self.errors = {}
        self._attempts = {}
        self._next = {}
        self._blocked = set()
        self._cursor = 0

    @staticmethod
    def eligible(row):
        return bool(row and row.get("channel") == "qq" and row.get("enabled")
                    and not row.get("deleted_ms") and row.get("desired_state") == "running"
                    and row.get("state") in ("running", "degraded") and row.get("self_uid"))

    def identity(self, row):
        return row["id"], str(row["self_uid"]), self.store.native_identity(row["id"]).get("epoch", 0)

    def current(self, identity):
        row = self.store.get_account_full(identity[0])
        return self.eligible(row) and not self.busy(identity[0]) and self.identity(row) == identity

    async def tick(self):
        rows = [r for r in self.store.list_accounts(channel="qq") if self.eligible(r)]
        ids = {r["id"] for r in rows}
        self.verified.intersection_update(ids)
        self.errors = {k: v for k, v in self.errors.items() if k in ids}
        self._attempts = {k: v for k, v in self._attempts.items() if k[0] in ids}
        self._next = {k: v for k, v in self._next.items() if k[0] in ids}
        self._blocked = {k for k in self._blocked if k[0] in ids}
        if not rows:
            return
        row = rows[self._cursor % len(rows)]
        self._cursor += 1
        aid = row["id"]
        identity = self.identity(row)
        if self.busy(aid) or identity in self._blocked or self.clock() < self._next.get(identity, 0):
            return
        account = Account(id=aid, channel="qq", state=row["state"], self_uid=str(row["self_uid"]))
        session = self.adapter.session_of(aid)
        if session is not None:
            account.extra["_qq_session_generation"] = session.generation
        if session is None and self._attempts.get(identity, 0) >= 10:
            self.errors[aid] = "QQ_REATTACH_RETRY_EXHAUSTED"
            return
        try:
            async with asyncio.timeout(10):
                if session is None:
                    await self.adapter.start(account)
                    session = self.adapter.session_of(aid)
                owned_generation = account.extra.get("_qq_session_generation")
                if session is not None and owned_generation is not session.generation:
                    return
                if not self.current(identity):
                    await self.adapter.stop(account, graceful=True)
                    self.verified.discard(aid)
                    return
                if session is None or not session.client.connected:
                    raise ConnectionError("QQ_TRANSPORT_OFFLINE")
                status = await session.client.call_action("get_status")
                info = await session.client.call_action("get_login_info")
                if not self.current(identity) or self.adapter.session_of(aid) is not session:
                    await self.adapter.stop(account, graceful=True)
                    self.verified.discard(aid)
                    return
                uid = info.get("user_id") if isinstance(info, dict) else None
                if not isinstance(status, dict) or status.get("online") is not True:
                    raise ConnectionError("NATIVE_LOGIN_REQUIRED")
                if isinstance(uid, bool) or not isinstance(uid, (str, int)) or str(uid) != identity[1]:
                    self._blocked.add(identity)
                    self.errors[aid] = "NATIVE_ACCOUNT_CHANGED"
                    self.verified.discard(aid)
                    await self.adapter.stop(account, graceful=True)
                    return
                self.verified.add(aid)
                self.errors.pop(aid, None)
                self._attempts.pop(identity, None)
                self._next[identity] = self.clock() + 10
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # OneBot owns its existing transport reconnect loop. Do not stop it
            # merely because the backend was not ready during startup.
            self.verified.discard(aid)
            attempts = self._attempts.get(identity, 0) + 1
            self._attempts[identity] = attempts
            self._next[identity] = self.clock() + min(60, 2 ** min(attempts, 6))
            self.errors[aid] = str(exc) if isinstance(exc, ConnectionError) else "QQ_REATTACH_UNAVAILABLE"
