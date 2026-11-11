"""Additional operations on the existing core nodes CLI family."""
import getpass
import json
import time

from hydraweave import bundles, onboarding, provisioning


def add(commands, default_config):
    for name in ("discover", "adopt", "provision"):
        parser = commands.add_parser(name)
        parser.add_argument("--config", type=type(default_config), default=default_config)
        parser.add_argument("--wait", type=float, default=3)
        if name != "discover":
            parser.add_argument("id")
        if name == "adopt":
            parser.add_argument("--code", help="console pairing code; omitted: read privately")
            parser.add_argument("--no-provision", action="store_true", help="adopt without installing assigned profiles")
        if name == "provision":
            parser.add_argument("profile")
            parser.add_argument("--source", action="append", default=[])
            parser.add_argument("--plan", action="store_true")
        parser.set_defaults(ext_run=run)


def run(args):
    from hydraweave.cli import _cluster
    if args.operation == "provision":
        cluster = _cluster(args.config)
        if args.plan:
            result = provisioning.plan(cluster, args.id, args.profile)
        else:
            result = provisioning.execute(cluster, args.config, args.id, args.profile, report=print,
                                           sources=bundles.parse_all(args.source))
    else:
        controller = onboarding.Controller(args.config).start()
        try:
            time.sleep(max(0, min(args.wait, 30)))
            result = (controller.inventory() if args.operation == "discover" else
                      controller.adopt(args.id, args.code or getpass.getpass("Node pairing code: ")))
        finally:
            controller.close()
        if args.operation == "adopt" and not args.no_provision:
            cluster = _cluster(args.config)
            mid = result["adopted"]
            result["provisioning"] = [provisioning.execute(cluster, args.config, mid, profile, report=print)
                                      for profile, row in cluster.profiles.items()
                                      if mid in row.get("nodes", [row.get("rank0"), row.get("rank1")])]
    print(json.dumps(result, indent=2))
    return 0
