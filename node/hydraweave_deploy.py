"""Read-only engine deployment inspection on the engine's own node."""
import json
import subprocess

VERBS = {"deploy-check"}
MAX_JSON = 256 * 1024


def run(verb, payload):
    from hydraweave_lease import environment, marked_root, path, runtime_env
    result = {"schema": "hydraweave/node-v1", "verb": verb}
    try:
        root = marked_root(payload.get("root"))
        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise ValueError("deploy-check: expected engine argv")
        executable = path(argv[0]).resolve()
        if not executable.is_relative_to((root / "releases").resolve()) or executable.parent.name != "bin":
            raise ValueError("deploy-check requires an immutable release executable")
        env = environment(payload.get("env", {}), runtime_env(root, argv))
        # Never pass an unrecognised inspection flag to an old engine's launch parser.
        help_result = subprocess.run([argv[0], "--help"], env=env, capture_output=True, timeout=30)
        if b"--deploy-check" not in help_result.stdout + help_result.stderr:
            if "--deploy" in argv:
                raise ValueError("configured deployment spec requires engine --deploy-check support")
            result["supported"] = False
            return result
        command = list(argv)
        if "--deploy" not in command:
            defaults = payload.get("capacity_defaults", [])
            if not isinstance(defaults, list) or any(f not in ("--slots", "--capacity", "--context") for f in defaults):
                raise ValueError("invalid capacity defaults")
            for flag in defaults:
                if flag in command:
                    index = command.index(flag)
                    del command[index:index + 2]
            command.extend(("--deploy", "auto"))
        launch_argv = list(command)
        if "--deploy-check" not in command:
            command.append("--deploy-check")
        checked = subprocess.run(command, env=env, capture_output=True, timeout=120)
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
