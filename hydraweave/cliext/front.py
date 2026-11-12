"""``hydraweave front``: one OpenAI-compatible endpoint in front of many engines, routed by model id."""
import json
import os
import sys


def add(commands):
    parser = commands.add_parser("front", help="one OpenAI-compatible endpoint routed by model across engines")
    verbs = parser.add_subparsers(dest="front_command", required=True)
    for name, text in (("serve", "serve the front door"), ("models", "print the aggregated /v1/models")):
        verb = verbs.add_parser(name, help=text)
        verb.add_argument("--upstream", action="append", required=True, metavar="NAME=URL",
                          help="an OpenAI-compatible engine, e.g. node2=http://10.0.0.2:8080 (repeatable)")
        if name == "serve":
            verb.add_argument("--host", default="127.0.0.1")
            verb.add_argument("--port", type=int, default=18710)
            verb.add_argument("--token-env", default="HYDRAWEAVE_FRONT_TOKEN",
                              help="environment variable holding the bearer token clients must send")
            verb.add_argument("--no-auth", action="store_true", help="accept requests without a token")
    parser.set_defaults(ext_run=run)


def run(args):
    from hydraweave import frontdoor
    try:
        upstreams = [frontdoor.parse_upstream(text) for text in args.upstream]
        if args.front_command == "models":
            table = frontdoor.ModelTable(upstreams)
            print(json.dumps({**table.listing(), "upstream_errors": table.errors()}, indent=2))
            return 0
        token = None if args.no_auth else os.environ.get(args.token_env) or None
        if token is None and not args.no_auth and args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"hydraweave front: set {args.token_env} or pass --no-auth to listen on {args.host}",
                  file=sys.stderr)
            return 2
        server = frontdoor.make_server(upstreams, host=args.host, port=args.port, token=token)
    except ValueError as exc:
        print(f"hydraweave front: {exc}", file=sys.stderr)
        return 2
    print(f"hydraweave front listening on http://{args.host}:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
