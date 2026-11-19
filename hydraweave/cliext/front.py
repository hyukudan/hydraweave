"""``hydraweave front``: one OpenAI-compatible endpoint in front of many engines, routed by model id."""
import json
import os
from dataclasses import replace
from pathlib import Path
import sys


def add(commands):
    parser = commands.add_parser("front", help="one OpenAI-compatible endpoint routed by model across engines")
    verbs = parser.add_subparsers(dest="front_command", required=True)
    for name, text in (("serve", "serve the front door"), ("models", "print the aggregated /v1/models")):
        verb = verbs.add_parser(name, help=text)
        verb.add_argument("--upstream", action="append", required=True, metavar="NAME=URL",
                          help="an OpenAI-compatible engine, e.g. node2=http://10.0.0.2:8080 (repeatable)")
        verb.add_argument("--upstream-key-env", action="append", default=[], metavar="NAME=ENV",
                          help="read this upstream's bearer key from an environment variable (repeatable)")
        verb.add_argument("--upstream-key-file", action="append", default=[], metavar="NAME=PATH",
                          help="read this upstream's bearer key from a UTF-8 file (repeatable)")
        if name == "serve":
            verb.add_argument("--host", default="127.0.0.1")
            verb.add_argument("--port", type=int, default=18710)
            verb.add_argument("--token-env", default="HYDRAWEAVE_FRONT_TOKEN",
                              help="environment variable holding the bearer token clients must send")
            verb.add_argument("--no-auth", action="store_true", help="accept requests without a token")
    parser.set_defaults(ext_run=run)


def _key(value, source):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError(f"{source}: expected a nonempty bearer key without whitespace (at most 8192 bytes)")
    return value


def _upstreams(args, frontdoor):
    upstreams = [frontdoor.parse_upstream(text) for text in args.upstream]
    names, keys = {upstream.name for upstream in upstreams}, {}
    for kind, bindings in (("env", getattr(args, "upstream_key_env", [])),
                           ("file", getattr(args, "upstream_key_file", []))):
        for binding in bindings:
            name, sep, reference = binding.partition("=")
            if not sep or not name or not reference:
                raise ValueError(f"--upstream-key-{kind} expects NAME={'ENV' if kind == 'env' else 'PATH'}")
            if name not in names:
                raise ValueError(f"key references unknown upstream {name}")
            if name in keys:
                raise ValueError(f"more than one key supplied for upstream {name}")
            if kind == "env":
                value = os.environ.get(reference)
            else:
                try:
                    with Path(reference).expanduser().open("rb") as handle:
                        raw = handle.read(8195)
                        if len(raw) > 8194:
                            raise ValueError(f"key file for upstream {name} exceeds 8194 bytes")
                        value = raw.decode("utf-8").rstrip("\r\n")
                except (OSError, UnicodeError):
                    raise ValueError(f"cannot read the key file for upstream {name}") from None
            keys[name] = _key(value, f"upstream {name}")
    return [replace(upstream, key=keys.get(upstream.name)) for upstream in upstreams]


def run(args):
    from hydraweave import frontdoor
    try:
        upstreams = _upstreams(args, frontdoor)
        if args.front_command == "models":
            table = frontdoor.ModelTable(upstreams)
            print(json.dumps({**table.listing(), "upstream_errors": table.errors()}, indent=2))
            return 0
        token = None if args.no_auth else os.environ.get(args.token_env) or None
        if token is not None:
            _key(token, "front token")
        if token is None and not args.no_auth and args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"hydraweave front: set {args.token_env} or pass --no-auth to listen on {args.host}",
                  file=sys.stderr)
            return 2
        server = frontdoor.make_server(upstreams, host=args.host, port=args.port, token=token)
    except (ValueError, OSError) as exc:
        print(f"hydraweave front: {exc}", file=sys.stderr)
        return 2
    host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"hydraweave front listening on http://{host}:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
