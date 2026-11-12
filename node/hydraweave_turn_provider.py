"""Small built-in FIFO GPU turn provider. Python 3.10+, standard library only."""
try:
    import fcntl
except ImportError:  # The node's lease path is POSIX-only; keep module imports portable for tests.
    fcntl = None
import os
from pathlib import Path
import time
import uuid


class TurnProvider:
    def acquire(self, label, kind="single", peers=()):
        raise NotImplementedError

    def holder(self):
        raise NotImplementedError

    def release(self, ticket):
        raise NotImplementedError


class BuiltinProvider(TurnProvider):
    def __init__(self, root):
        self.directory = Path(root) / "run" / "turns"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lockfile = self.directory / "lock"
        self.holderfile = self.directory / "holder"

    def _lock(self):
        if fcntl is None:
            raise OSError("built-in GPU turns require POSIX flock")
        stream = self.lockfile.open("a+")
        fcntl.flock(stream, fcntl.LOCK_EX)
        return stream

    def holder(self):
        try:
            label, pid, since = self.holderfile.read_text().split()
            pid = int(pid)
            try:
                os.kill(pid, 0)
            except OSError:
                return None
            return {"label": label, "since": float(since), "pid": pid}
        except (OSError, ValueError):
            return None

    @staticmethod
    def _waiting(ticket):
        """Drop tickets whose waiter died so one crash cannot block the queue."""
        try:
            os.kill(int(ticket.read_text().split()[0]), 0)
            return True
        except (OSError, ValueError, IndexError):
            ticket.unlink(missing_ok=True)
            return False

    def acquire(self, label, kind="single", peers=()):
        if kind != "single":
            raise ValueError("the built-in turn provider handles single-node turns only")
        ticket = self.directory / (f"{time.time_ns():020d}-{uuid.uuid4().hex}")
        ticket.write_text(f"{os.getpid()}\n")
        while True:
            lock = self._lock()
            try:
                current = self.holder()
                queue = [p for p in sorted(self.directory.iterdir()) if p.name[:1].isdigit() and self._waiting(p)]
                if current is None and queue and queue[0] == ticket:
                    ticket.unlink(missing_ok=True)
                    self.holderfile.write_text(f"{label} {os.getpid()} {time.time()}\n")
                    return label
            finally:
                lock.close()
            time.sleep(.05)

    def release(self, ticket):
        lock = self._lock()
        try:
            current = self.holder()
            if current and current["label"] == ticket and current["pid"] == os.getpid():
                self.holderfile.unlink(missing_ok=True)
        finally:
            lock.close()


class LabScriptsProvider(TurnProvider):
    def __init__(self, lab_dir):
        self.lab_dir = Path(lab_dir)

    def holder(self):
        words = (self.lab_dir / ".gpu-turn" / "holder").read_text().split()
        return {"label": words[0], "pid": 0, "since": 0} if words else None

    def acquire(self, label, kind="single", peers=()):
        raise NotImplementedError("lab turns are acquired by the configured scripts")

    def release(self, ticket):
        return None
