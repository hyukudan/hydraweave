"""Read-only engine deployment inspection on the engine's own node."""
import json
import hashlib
import os
import selectors
import signal
import subprocess
import time

VERBS = {"deploy-check"}
MAX_JSON = 256 * 1024


def capture(argv, env, timeout):
    """Bound both output streams while reading them concurrently; reap on failure."""
    child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    streams = (child.stdout, child.stderr)
    data = {stream: bytearray() for stream in streams}
    complete = False
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(left, 0.2)):
                block = os.read(key.fileobj.fileno(), 64 * 1024)
                if not block:
                    selector.unregister(key.fileobj)
                else:
                    data[key.fileobj].extend(block)
                    if len(data[key.fileobj]) > MAX_JSON:
                        raise ValueError("deploy-check output exceeds 256 KiB")
        code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
        complete = True
        return subprocess.CompletedProcess(argv, code, bytes(data[child.stdout]), bytes(data[child.stderr]))
    finally:
        selector.close()
        if not complete:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)
        for stream in streams:
            stream.close()


def inventory_file(root, devices, topology):
    from hydraweave_lease import atomic, inside
    if topology != "tp2" or not isinstance(devices, list) or len(devices) != 2:
        raise ValueError("deployment inventory must describe the two measured pair devices")
    lines = ['schema = "strix-deploy-v1"', 'nodes = 2', 'topology = "tp2"']
    for device in devices:
        if not isinstance(device, dict) or type(device.get("unified")) is not bool:
            raise ValueError("deployment device memory kind is missing")
        lines.append("[[devices]]")
        for key in ("gfx_target", "source"):
            value = device.get(key)
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid measured device " + key)
            lines.append(key + " = " + json.dumps(value, ensure_ascii=False))
        for key in ("cu_count", "mem_total_bytes", "mem_free_bytes"):
            value = device.get(key)
            if type(value) is not int or not 0 <= value < 1 << 63:
                raise ValueError("invalid measured device " + key)
            lines.append(f"{key} = {value}")
        if not device["mem_total_bytes"] or device["mem_free_bytes"] > device["mem_total_bytes"]:
            raise ValueError("inconsistent measured device memory")
        lines.append("unified = " + str(device["unified"]).lower())
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    filename = inside(root, "run", "deployment-" + hashlib.sha256(raw).hexdigest() + ".toml")
    atomic(filename, raw)
    return str(filename)


def run(verb, payload):
    from hydraweave_lease import environment, marked_root, path, runtime_env
    result = {"schema": "hydraweave/node-v1", "verb": verb}
    try:
        root = marked_root(payload.get("root"))
        argv = payload.get("argv")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 4096
                or any(not isinstance(a, str) or "\0" in a for a in argv)):
            raise ValueError("deploy-check: expected engine argv")
        executable = path(argv[0]).resolve()
        if not executable.is_relative_to((root / "releases").resolve()) or executable.parent.name != "bin":
            raise ValueError("deploy-check requires an immutable release executable")
        env = environment(payload.get("env", {}), runtime_env(root, argv))
        # Never pass an unrecognised inspection flag to an old engine's launch parser.
        help_result = capture([argv[0], "--help"], env, 30)
        if b"--deploy-check" not in help_result.stdout + help_result.stderr:
            if "--deploy" in argv:
                raise ValueError("configured deployment spec requires engine --deploy-check support")
            result["supported"] = False
            return result
        command = list(argv)
        if "--deploy" not in command:
            command.extend(("--deploy", "auto"))
        if command.count("--deploy") != 1 or command.index("--deploy") == len(command) - 1:
            raise ValueError("deploy-check requires exactly one deployment spec")
        if payload.get("inventory") is not None:
            index = command.index("--deploy") + 1
            if command[index] != "auto":
                raise ValueError("measured inventory can only expand --deploy auto")
            command[index] = inventory_file(root, payload["inventory"], payload.get("topology"))
        launch_argv = list(command)
        if "--deploy-check" not in command:
            command.append("--deploy-check")
        checked = capture(command, env, 120)
        if len(checked.stdout) > MAX_JSON:
            raise ValueError("deploy-check JSON exceeds 256 KiB")
        try:
            plan = json.loads(checked.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("deploy-check did not return plan JSON") from exc
        if checked.returncode != 0 and not (isinstance(plan, dict) and plan.get("refused") is True):
            raise ValueError(f"deploy-check exited {checked.returncode}")
        result.update(supported=True, deployment=plan, argv=launch_argv)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    return result
