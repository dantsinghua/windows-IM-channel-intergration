"""Serialize every shared SQLite operation with its owner transaction lock."""
import sqlite3
import threading

class LockedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        with self.connection.guard: return super().execute(*args, **kwargs)
    def executemany(self, *args, **kwargs):
        with self.connection.guard: return super().executemany(*args, **kwargs)
    def executescript(self, *args, **kwargs):
        with self.connection.guard: return super().executescript(*args, **kwargs)
    def fetchone(self):
        with self.connection.guard: return super().fetchone()
    def fetchall(self):
        with self.connection.guard: return super().fetchall()
    def fetchmany(self, size=None):
        with self.connection.guard:
            return super().fetchmany() if size is None else super().fetchmany(size)
    def __iter__(self): return self
    def __next__(self):
        with self.connection.guard: return super().__next__()
    def close(self):
        with self.connection.guard: return super().close()

class LockedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard=threading.RLock()
    def cursor(self, factory=LockedCursor):
        with self.guard: return super().cursor(factory)
    def execute(self, *args, **kwargs):
        with self.guard: return self.cursor().execute(*args, **kwargs)
    def executemany(self, *args, **kwargs):
        with self.guard: return self.cursor().executemany(*args, **kwargs)
    def executescript(self, *args, **kwargs):
        with self.guard: return self.cursor().executescript(*args, **kwargs)
    def commit(self):
        with self.guard: return super().commit()
    def rollback(self):
        with self.guard: return super().rollback()
    def close(self):
        with self.guard: return super().close()
    def backup(self, *args, **kwargs):
        with self.guard: return super().backup(*args, **kwargs)
