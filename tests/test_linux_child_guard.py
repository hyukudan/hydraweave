"""Linux owner lifetime is enforced for real forked children and open listeners."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

try:
    import pytest
except ImportError:  # unittest discovery before installation of the optional test extra
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hydraweave import procs

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "pidfd_open"), reason="Linux pidfd guard")
ROOT = Path(__file__).resolve().parents[1]


def wait_file(path):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text())
        time.sleep(0.02)
    raise AssertionError("fixture did not become ready")


@pytest.mark.parametrize("leader_exits", [False, True])
def test_controller_or_leader_crash_kills_all_group_members(tmp_path, leader_exits):
    marker = tmp_path / "processes.json"
    child_code = (
        "import os,json,signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)"
    )
    parent_code = ("import os,time; from hydraweave.procs import LocalChild; "
                   f"child=LocalChild([{sys.executable!r},'-c',{child_code!r}],dict(os.environ),{str(tmp_path)!r},'fixture').start(); "
                   "time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", parent_code], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    descriptors = []
    try:
        pids = wait_file(marker)
        descriptors = [os.pidfd_open(pid) for pid in pids]
        if leader_exits:
            signal.pidfd_send_signal(descriptors[0], signal.SIGKILL)
        else:
            parent.kill()
            parent.wait(timeout=5)
        poller = select.poll()
        for descriptor in descriptors:
            poller.register(descriptor, select.POLLIN)
        pending = set(descriptors)
        end = time.monotonic() + 5
        while pending and time.monotonic() < end:
            for descriptor, _ in poller.poll(100):
                pending.discard(descriptor)
                poller.unregister(descriptor)
        assert not pending, "owned descendants survived their lifetime owner"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        parent.stderr.close()
        for descriptor in descriptors:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(descriptor)


def test_spawning_thread_can_finish_without_killing_a_live_process_owner(tmp_path):
    import threading
    children = []
    worker = threading.Thread(target=lambda: children.append(procs.LocalChild(
        [sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), tmp_path, "fixture").start()))
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    try:
        time.sleep(0.05)
        assert children[0].alive()  # PR_SET_PDEATHSIG alone would kill on creator-thread exit.
    finally:
        children[0].stop(0.1)
