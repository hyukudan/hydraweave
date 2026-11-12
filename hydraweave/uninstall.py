"""Conservative removal of user units and explicitly marked directories.

The removal engine is stdlib-only so the controller can send it over the pinned
SSH transport without installing or bootstrapping anything on a node.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


MARKERS = (".hydraweave-root", ".hydraweave-assets")
FIXED_UNITS = {"hydraweave-node.service", "hydraweave-portal.service", "hydraweave-ui.service"}


def plain(path: Path, home: Path) -> bool:
    """Reject indirection and any root that contains the user's home."""
    path = Path(path).expanduser().absolute()
    if ".." in path.parts or path == Path(path.anchor) or home.absolute().is_relative_to(path):
        return False
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            return False
    return path.resolve() == path


def marked(path: Path, home: Path) -> bool:
    return plain(path, home) and path.is_dir() and any(
        (path / name).is_file() and not (path / name).is_symlink() for name in MARKERS)


def size(path: Path) -> int:
    """Logical bytes, without following directory links or junctions."""
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        base = Path(directory)
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()
                   and not getattr(base / name, "is_junction", lambda: False)()]
        total += sum((base / name).stat().st_size for name in files if not (base / name).is_symlink())
    return total


def model_directories(roots, home: Path) -> list[Path]:
    """Recognize models even inside a marked install root; never traverse links."""
    found = []
    for root in roots:
        root = Path(root).expanduser().absolute()
        if not plain(root, home):
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            if (base.name in {"models", "hydraweave-models"} or MARKERS[1] in files
                    or any(name.endswith((".gguf", ".safetensors")) for name in files)):
                found.append(base)
                # Still find marked asset children beneath an unmarked model container.
            dirs[:] = [name for name in dirs if plain(base / name, home)]
    return found


def owned_units(directories, home: Path, *, node_only=False):
    result = {}
    for directory in directories:
        directory = Path(directory)
        if not plain(directory, home):
            continue
        for path in sorted(directory.glob("*.service")):
            if not plain(path, home) or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            fixed = path.name in ({"hydraweave-node.service"} if node_only else FIXED_UNITS)
            generated = (not node_only and re.fullmatch(r"(?:hl|hydraweave)-[a-z0-9-]{1,40}\.service", path.name)
                         and ("hydraweave" in text or "hyperloom" in text))
            if fixed or generated:
                result.setdefault(path.name, path)
    return result


def remove(*, roots=(), configs=(), models=(), unit_dirs=(), home=None,
           keep_models=True, purge_config=False, dry_run=True, runner=None,
           node_only=False, user_unit_dirs=None) -> dict:
    """Plan or execute with injected filesystem roots and command runner.

    All services must stop and disable successfully before any directory is
    removed. Vendor units are disabled for this user but remain package-owned.
    """
    home = Path.home() if home is None else Path(home)
    runner = subprocess.run if runner is None else runner
    roots = [Path(path).expanduser().absolute() for path in roots]
    configs = [Path(path).expanduser().absolute() for path in configs]
    models = [Path(path).expanduser().absolute() for path in models]
    models = list(dict.fromkeys([*models, *model_directories([*roots, *models], home)]))
    units = owned_units(unit_dirs, home, node_only=node_only)
    writable_units = {Path(path) for path in (unit_dirs if user_unit_dirs is None else user_unit_dirs)}
    actions = [{"action": "stop-disable", "unit": name, "path": str(path)} for name, path in units.items()]
    protected = ([] if not keep_models else models) + ([] if purge_config else configs)
    candidates = list(dict.fromkeys([*roots, *models, *configs]))
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        reason = None
        if any(path == item or item.is_relative_to(path) or path.is_relative_to(item) for item in protected):
            reason = "models kept" if any(path == item or item.is_relative_to(path) or path.is_relative_to(item)
                                               for item in models) and keep_models else "config kept"
        elif not marked(path, home):
            reason = "unmarked or indirect path"
        actions.append({"action": "keep" if reason else "remove", "path": str(path),
                        "bytes": size(path) if plain(path, home) else 0, "reason": reason})
    if not dry_run:
        for name in units:
            for operation in ("stop", "disable"):
                runner(["systemctl", "--user", operation, name], check=True)
        for path in units.values():
            if path.parent in writable_units:
                path.unlink()
        if units:
            runner(["systemctl", "--user", "daemon-reload"], check=True)
        for row in actions:
            path = Path(row["path"])
            if row["action"] == "remove" and path.exists():
                if not marked(path, home):
                    raise ValueError(f"ownership marker changed: {path}")
                shutil.rmtree(path)
    return {"dry_run": dry_run, "actions": actions}


def local_options(*, nodes=False, config=None) -> dict:
    from . import paths
    home = Path.home()
    configs = list(dict.fromkeys([paths.config_dir(), paths.legacy_config_dir(),
                                 *([Path(config).parent] if config else [])]))
    roots = [paths.state_dir(), paths.state_dir() / "run", paths.data_dir(),
             paths.data_dir() / "run", paths.legacy_config_dir() / "run", home / ".hydraweave-boot"]
    if nodes:
        roots.append(home / "hydraweave")
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    units = (base if base.is_absolute() else home / ".config") / "systemd/user"
    return {"home": home, "roots": roots, "configs": configs,
            "models": [paths.data_dir() / "models", home / "hydraweave-models"],
            "unit_dirs": [units, home / ".config/systemd/user", Path("/usr/lib/systemd/user"), Path("/lib/systemd/user")],
            "user_unit_dirs": [units, home / ".config/systemd/user"]}


def command(args, *, remover=None, remote_factory=None, confirm=None) -> int:
    """CLI orchestration; confirmation follows a concrete local and remote plan."""
    remover = remove if remover is None else remover
    confirm = input if confirm is None else confirm
    from . import config, remote
    remote_factory = remote.Remote if remote_factory is None else remote_factory
    options = local_options(nodes=args.nodes, config=args.config)
    clients = []
    if args.nodes and args.config.is_file():
        cluster = config.load(args.config)
        for machine in cluster.machines.values():
            if machine.access != "ssh":
                if machine.os != "windows":
                    raise ValueError(f"cannot uninstall node {machine.id}: SSH access required")
                continue
            roots = config.model_roots_of(machine)
            if remote.is_self(machine):
                options["roots"].append(Path(machine.install_root))
                options["models"].extend(Path(path) for path in roots)
            else:
                clients.append((machine.id, remote_factory(machine, args.config.parent / "known_hosts"),
                                {"root": machine.install_root, "models": roots, "keep_models": args.keep_models}))
    flags = {"keep_models": args.keep_models, "purge_config": args.purge_config}
    plan = {"local": remover(**options, **flags, dry_run=True), "nodes": {}}
    for name, client, payload in clients:
        plan["nodes"][name] = client.remove_install(payload, dry_run=True)
    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return 0
    if not args.yes and confirm("Apply this uninstall plan? [y/N] ").strip().lower() not in {"y", "yes"}:
        return 0
    # Keep controller config available if a remote removal fails.
    for name, client, payload in clients:
        print(json.dumps({name: client.remove_install(payload, dry_run=False)}, indent=2))
    print(json.dumps(remover(**options, **flags, dry_run=False), indent=2))
    return 0
