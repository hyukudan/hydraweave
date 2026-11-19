"""``hydraweave models``: the model library from the command line (the same functions as the portal's library page).

    hydraweave models list [--machine M] [--refresh]       model units per machine (cached scan, or scan now)
    hydraweave models verify M ROOT NAME                  owned verification, waits and cancels on Ctrl+C
    hydraweave models copy-plan M ROOT NAME DEST DEST_ROOT
    hydraweave models copy M ROOT NAME DEST DEST_ROOT --confirm CODE
    hydraweave models roots M [--set DIR ...] [--clear]
    hydraweave models ledger-accept                         accept the current RTX big-job ledger as the baseline
"""

import json
from pathlib import Path
import sys
import time


def _store(create=False):
    from hydraweave import cli
    from hydraweave.telemetry.store import Store
    path = cli.config_path().parent / "telemetry.sqlite3"
    if not create and not path.exists():
        return None
    return Store(str(path))


def _cluster():
    from hydraweave import cli
    return cli._cluster(cli.config_path())


def _print(value):
    print(json.dumps(value, indent=2, default=str))


def run(args):
    from hydraweave import cli, downloads
    operation = args.models_operation
    if operation in ("verify", "copy-plan", "copy"):
        return _model_job(args)
    if operation == "registry":
        from hydraweave.backends import model_registry
        _print(model_registry(user_dir=cli.config_path().parent / "models"))
        return 0
    cluster = _cluster()
    if operation == "list":
        store = _store(create=args.refresh)
        try:
            machines = [args.machine] if args.machine else list(cluster.machines)
            result = {}
            for mid in machines:
                if args.refresh:
                    inventory = downloads.inventory(cluster, mid, store)
                    if store is not None and not inventory.get("busy"):
                        store.put("library/inventory", mid, inventory)
                else:
                    inventory = store.get("library/inventory", mid) if store is not None else None
                    if inventory is None:
                        inventory = {"machine": mid, "why": "not scanned yet: run with --refresh"}
                result[mid] = inventory
            _print(result)
            return 0
        finally:
            if store is not None:
                store.close()
    if operation == "roots":
        store = _store(create=bool(args.set) or args.clear)
        try:
            if args.set or args.clear:
                _print(downloads.set_roots(cluster, store, args.machine, [] if args.clear else args.set))
            else:
                _print({"machine": args.machine, "roots": downloads.library_roots(cluster, store).get(args.machine)})
            return 0
        finally:
            if store is not None:
                store.close()
    store = _store()
    try:
        ledger = downloads.ledger_guard(cli.config_path(), store)
        if operation == "ledger-accept":
            _print(ledger.accept())
            return 0
        raise ValueError("unknown models operation")
    except (LookupError, PermissionError) as exc:
        print(f"hydraweave: {str(exc).strip(chr(39))}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def _model_job(args):
    from hydraweave import cli
    from hydraweave.web.api import API
    from hydraweave.web.features import library
    api = API(cli.config_path())
    arguments = {key: getattr(args, key) for key in ("machine", "model_root", "name")}
    if args.models_operation != "verify":
        arguments.update(destination=args.destination, destination_root=args.destination_root)
    if args.models_operation == "copy-plan":
        _print(library.copy_plan(api, arguments))
        return 0
    if args.models_operation == "copy":
        arguments["confirm"] = args.confirm
    job = None
    try:
        job = api.mutate("library/" + args.models_operation, arguments)
        identifier = job["id"]
        progress = None
        while True:
            api.renew_jobs({"jobs": [identifier]})
            row = api.read("jobs/" + identifier)
            if row.get("progress") != progress:
                progress = row.get("progress")
                if progress:
                    print("hydraweave: " + str(progress), file=sys.stderr)
            if row["state"] != "running":
                _print(row)
                return 0 if row["state"] == "succeeded" else 1
            time.sleep(.25)
    except KeyboardInterrupt:
        if job:
            api.mutate("cancel", {"job": job["id"]})
        return 130
    finally:
        api.close_features()


def add(commands):
    models = commands.add_parser("models", help="model library: inventory")
    operations = models.add_subparsers(dest="models_operation", required=True)
    listing = operations.add_parser("list", help="model units per machine (the last scan, or --refresh)")
    listing.add_argument("--machine")
    listing.add_argument("--refresh", action="store_true", help="scan now (read-only on every machine)")
    operations.add_parser("registry", help="list declared model metadata")
    verify = operations.add_parser("verify", help="verify a model manifest as an owned cancellable job")
    verify.add_argument("machine")
    verify.add_argument("model_root")
    verify.add_argument("name")
    for operation in ("copy-plan", "copy"):
        copy = operations.add_parser(operation, help="review or run a direct, verified node-to-node copy")
        for key in ("machine", "model_root", "name", "destination", "destination_root"):
            copy.add_argument(key)
        if operation == "copy":
            copy.add_argument("--confirm", required=True)
    roots = operations.add_parser("roots", help="show or set a machine's extra model roots")
    roots.add_argument("machine")
    roots.add_argument("--set", nargs="+", metavar="DIR")
    roots.add_argument("--clear", action="store_true")
    operations.add_parser("ledger-accept", help="accept the current RTX big-job ledger as the guard's baseline")
    models.set_defaults(ext_run=run)
