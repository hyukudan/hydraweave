"""Linux exec shim: the owned process group cannot outlive its controller.

Run as a file by LocalChild, before the target exec. The guard is created in
the target's new process group and keeps that group ID reserved until cleanup.
It watches pinned pidfds, never a reusable numeric parent PID. No application
imports or Python preexec_fn run in the multithreaded controller after fork.
"""
import json
import os
import select
import signal
import sys


def main():
    owner_fd, error_fd, control_fd = map(int, sys.argv[1:4])
    argv = sys.argv[4:]
    try:
        child_fd = os.pidfd_open(os.getpid())
        ready_read, ready_write = os.pipe()
        guard = os.fork()
        if guard == 0:
            os.close(error_fd)
            os.close(ready_read)
            # The controller drains the target's pipes; the guard must not
            # keep them open after the target exits.
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null, descriptor)
            if null > 2:
                os.close(null)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            poller = select.poll()
            poller.register(owner_fd, select.POLLIN)
            poller.register(child_fd, select.POLLIN)
            poller.register(control_fd, select.POLLIN)
            os.write(ready_write, b"1")
            os.close(ready_write)
            while True:
                for descriptor, _ in poller.poll():
                    command = os.read(control_fd, 1) if descriptor == control_fd else b"K"
                    # We are still a member, so this group ID cannot have
                    # been recycled, even if its leader has already died.
                    os.killpg(os.getpgrp(), signal.SIGTERM if command == b"T" else signal.SIGKILL)
            os._exit(127)
        os.close(ready_write)
        if os.read(ready_read, 1) != b"1":
            raise OSError("lifetime guard did not start")
        os.close(ready_read)
        os.close(control_fd)
        os.close(owner_fd)
        os.close(child_fd)
        os.set_inheritable(error_fd, False)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        os.execvpe(argv[0], argv, os.environ)
    except BaseException as error:
        data = json.dumps({"errno": getattr(error, "errno", None), "error": str(error)[:1500]}).encode()
        try:
            os.write(error_fd, data)
        except OSError:
            pass
        os._exit(127)


if __name__ == "__main__":
    main()
