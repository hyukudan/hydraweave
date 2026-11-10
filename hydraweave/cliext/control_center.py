"""Headless access to the same setup and component operations as the portal."""

import json
from pathlib import Path

from hydraweave.web.api import API, JobFailed
from .ops import Context, _audited


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return 0


def setup_command(args):
    from hydraweave.web.features import setup
    api = API(args.config)
    if args.operation == "status":
        return _print(setup.overview(api, {}))
    if args.operation == "init":
        return _print(_audited(api, "setup/initialize", {}, lambda: setup.initialize(api, {})))
    _, version = setup.ops._read_config(api)
    arguments = {"version": version, "machine": args.machine}
    if args.port is not None:
        arguments["port"] = args.port
    return _print(_audited(api, "setup/router/save", arguments, lambda: setup.router_save(api, arguments)))


def components_command(args):
    from hydraweave.web.features import components
    api = API(args.config)
    if args.operation == "status":
        return _print(components.overview(api, {}))
    if args.operation == "plan":
        return _print(components.plan(api, {"profile": args.profile} if args.profile else {}))
    arguments = {"component": args.component} if args.component else {}
    if args.operation == "download":
        arguments["commit"] = args.commit
        validated = components.download_validate(api, arguments)
        work, timeout = components.download, 1900
    else:
        validated = components.check_validate(api, arguments)
        work, timeout = components.check, 240
    context = Context(timeout)
    try:
        return _print(_audited(api, "components/" + args.operation, validated,
                               lambda: work(api, validated, context)))
    except KeyboardInterrupt:
        context.reason = "cancelled by operator"
        return 130
    except JobFailed as exc:
        # A check records its per-component failures, then exits like other CLI errors.
        raise RuntimeError(str(exc)) from None


def add(commands):
    from hydraweave.cli import config_path
    setup = commands.add_parser("setup", help="create a workspace and inspect controller/node placement")
    operations = setup.add_subparsers(dest="operation", required=True)
    for operation in ("status", "init", "router"):
        parser = operations.add_parser(operation)
        parser.add_argument("--config", type=Path, default=config_path())
        if operation == "router":
            parser.add_argument("machine", help="local or the id of a configured Linux SSH node")
            parser.add_argument("--port", type=int)
        parser.set_defaults(ext_run=setup_command)
    component = commands.add_parser("components", help="inspect, check and download pinned component sources")
    operations = component.add_subparsers(dest="operation", required=True)
    for operation in ("status", "check", "download", "plan"):
        parser = operations.add_parser(operation)
        parser.add_argument("--config", type=Path, default=config_path())
        if operation == "check":
            parser.add_argument("--component")
        elif operation == "download":
            parser.add_argument("component")
            parser.add_argument("commit", help="full SHA from the profile pins or the latest explicit check")
        elif operation == "plan":
            parser.add_argument("profile", nargs="?")
        parser.set_defaults(ext_run=components_command)
