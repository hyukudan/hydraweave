"""One OpenAI-compatible front door for many engines, keyed by the request's ``model``.

Each upstream is a chat-completions-compatible server (any backend, any node, any accelerator vendor). The front
door asks every upstream for its /v1/models, serves the union on its own /v1/models, and forwards each POST under
/v1/ to an upstream that lists the requested model. Responses (including server-sent event streams) are passed
through byte for byte. An unknown model gets an OpenAI-style 404 instead of a guess."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import http.client
import itertools
import json
import logging
import math
import socket
import threading
import time
from urllib.parse import unquote, urlsplit

LOG = logging.getLogger("hydraweave.frontdoor")
MAX_BODY = 32 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authenticate",
               "proxy-authorization", "content-length", "host", "authorization"}


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    key: str | None = field(default=None, repr=False)

    def parts(self):
        split = urlsplit(self.url)
        if (split.scheme not in ("http", "https") or not split.hostname or split.username is not None
                or split.password is not None or split.query or split.fragment):
            raise ValueError(f"upstream {self.name}: url must be http(s)://host[:port]")
        return split.scheme, split.hostname, split.port or (443 if split.scheme == "https" else 80), \
            split.path.rstrip("/")

    def connection(self, timeout):
        scheme, host, port, _ = self.parts()
        kind = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        return kind(host, port, timeout=timeout)

    def headers(self, extra=None):
        headers = dict(extra or {})
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers


def parse_upstream(text):
    """``name=http://host:port`` (or a bare URL, named after its host:port)."""
    name, sep, url = text.partition("=")
    if not sep:
        name, url = "", text
    upstream = Upstream(name or "", url.strip())
    _, host, port, _ = upstream.parts()
    return Upstream(name.strip() or f"{host}:{port}", upstream.url)


def error_document(message, *, code=None, param=None, kind="invalid_request_error"):
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def fetch_models(upstream, timeout=5.0):
    """The model entries an upstream lists, or raise OSError/ValueError."""
    connection = upstream.connection(timeout)
    try:
        connection.request("GET", upstream.parts()[3] + "/v1/models", headers=upstream.headers())
        response = connection.getresponse()
        body = response.read(MAX_BODY + 1)
        if response.status != 200:
            raise ValueError(f"/v1/models returned HTTP {response.status}")
        if len(body) > MAX_BODY:
            raise ValueError("/v1/models exceeds the catalog size limit")
        if response.length not in (None, 0):
            raise ValueError("/v1/models response ended early")
    finally:
        connection.close()
    document = json.loads(body)
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("/v1/models is not an OpenAI list")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]


class ModelTable:
    """model id -> upstreams that serve it, refreshed at most every ``ttl`` seconds."""

    def __init__(self, upstreams, *, ttl=10.0, timeout=5.0, fetch=fetch_models, clock=time.monotonic):
        if not upstreams:
            raise ValueError("the front door needs at least one upstream")
        names = [upstream.name for upstream in upstreams]
        if len(set(names)) != len(names):
            raise ValueError("upstream names must be unique")
        if not all(math.isfinite(value) and value > 0 for value in (ttl, timeout)):
            raise ValueError("catalog ttl and timeout must be positive finite numbers")
        self.upstreams, self.ttl, self.timeout = tuple(upstreams), ttl, timeout
        self.min_refresh = min(1.0, ttl)
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stamp = None
        self._routes, self._entries, self._errors, self._turns = {}, [], {}, {}

    def refresh(self, force=False):
        interval = self.min_refresh if force else self.ttl
        with self._lock:
            if self._stamp is not None and self._clock() - self._stamp < interval:
                return
        # Only one caller probes at a time. Warm readers keep using the last atomic snapshot; cold readers
        # wait for its first publication. Never hold the route lock across a network operation.
        if not self._refresh_lock.acquire(blocking=False):
            with self._lock:
                if self._stamp is not None:
                    return
            self._refresh_lock.acquire()
        try:
            with self._lock:
                if self._stamp is not None and self._clock() - self._stamp < interval:
                    return
            routes, entries, errors = {}, [], {}
            with ThreadPoolExecutor(max_workers=min(8, len(self.upstreams)),
                                    thread_name_prefix="front-catalog") as pool:
                probes = [(upstream, pool.submit(self._fetch, upstream, self.timeout))
                          for upstream in self.upstreams]
                # Apply results in configuration order, regardless of which probe completes first.
                for upstream, probe in probes:
                    try:
                        rows = probe.result()
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        errors[upstream.name] = str(exc) or type(exc).__name__
                        LOG.warning("upstream %s unavailable: %s", upstream.name, errors[upstream.name])
                        continue
                    seen = set()
                    for row in rows:
                        if row["id"] in seen:
                            continue
                        seen.add(row["id"])
                        if row["id"] not in routes:
                            entry = {key: value for key, value in row.items() if key != "owned_by"}
                            entry.setdefault("object", "model")
                            entries.append(dict(entry, owned_by=upstream.name))
                        routes.setdefault(row["id"], []).append(upstream)
            with self._lock:
                turns = {model: self._turns[model] if self._routes.get(model) == targets
                         else itertools.cycle(targets) for model, targets in routes.items()}
                self._routes, self._entries, self._errors, self._turns = routes, entries, errors, turns
                self._stamp = self._clock()
        finally:
            self._refresh_lock.release()

    def listing(self):
        self.refresh()
        with self._lock:
            return {"object": "list", "data": [dict(entry) for entry in self._entries]}

    def errors(self):
        with self._lock:
            return dict(self._errors)

    def pick(self, model):
        """An upstream serving ``model`` (round robin across replicas), or None."""
        self.refresh()
        with self._lock:
            turns = self._turns.get(model)
            if turns is not None:
                return next(turns)
        # An unknown id may be a model that just came up; re-ask the engines, at most once a second.
        with self._lock:
            recent = self._stamp is not None and self._clock() - self._stamp < self.min_refresh
        if not recent:
            self.refresh(force=True)
        with self._lock:
            turns = self._turns.get(model)
            return next(turns) if turns is not None else None


def _hop_headers(headers):
    """Connection options name additional fields that must not cross this HTTP hop."""
    return HOP_HEADERS | {option.strip().lower() for key, value in headers
                          if key.lower() == "connection" for option in value.split(",")}


def _handler(table, token, timeout):
    class Front(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hydraweave-front"

        def setup(self):
            super().setup()
            self.connection.settimeout(min(30.0, timeout))

        def log_message(self, fmt, *args):
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status, document):
            if status >= 400:
                # Rejected requests may still have an unread body; it cannot become the next request.
                self.close_connection = True
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if token is None:
                return True
            values = self.headers.get_all("Authorization", [])
            if len(values) == 1 and hmac.compare_digest(values[0].encode(), f"Bearer {token}".encode()):
                return True
            self._send(401, error_document("missing or wrong bearer token", code="invalid_api_key",
                                           kind="authentication_error"))
            return False

        def do_GET(self):
            if not self._authorized():
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or (lengths and lengths != ["0"]):
                return self._send(400, error_document("GET requests must not have a body"))
            path = self.path.split("?", 1)[0]
            if path == "/v1/models":
                return self._send(200, table.listing())
            if path.startswith("/v1/models/"):
                model = unquote(path[len("/v1/models/"):])
                for entry in table.listing()["data"]:
                    if entry["id"] == model:
                        return self._send(200, entry)
                return self._send(404, error_document(f"The model '{model}' does not exist", code="model_not_found",
                                                      param="model"))
            if path == "/health":
                return self._send(200, {"status": "ok", "upstream_errors": table.errors()})
            self._send(404, error_document(f"unknown path {path}", code="not_found"))

        def do_POST(self):
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if not path.startswith("/v1/"):
                return self._send(404, error_document(f"unknown path {path}", code="not_found"))
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) > 1:
                return self._send(400, error_document("ambiguous or unsupported request body framing"))
            if not lengths:
                return self._send(411, error_document("Content-Length is required"))
            length = -1
            if lengths[0].isascii() and lengths[0].isdecimal():
                length = int(lengths[0]) if len(lengths[0]) <= 20 else MAX_BODY + 1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._send(413 if length > MAX_BODY else 400, error_document("bad request body size"))
            try:
                body = self.rfile.read(length)
            except (OSError, TimeoutError):
                return self._send(408, error_document("request body read timed out"))
            if len(body) != length:
                return self._send(400, error_document("request body ended early"))
            try:
                request = json.loads(body or b"null")
            except ValueError:
                return self._send(400, error_document("request body is not JSON"))
            model = request.get("model") if isinstance(request, dict) else None
            if not isinstance(model, str) or not model:
                return self._send(400, error_document("you must provide a model parameter", param="model"))
            upstream = table.pick(model)
            if upstream is None:
                return self._send(404, error_document(f"The model '{model}' does not exist", code="model_not_found",
                                                      param="model"))
            self._forward(upstream, self.path, body)

        def _forward(self, upstream, path, body):
            blocked = _hop_headers(self.headers.items())
            headers = {key: value for key, value in self.headers.items() if key.lower() not in blocked}
            headers["Content-Length"] = str(len(body))
            connection = upstream.connection(timeout)
            try:
                try:
                    connection.request("POST", upstream.parts()[3] + path, body=body, headers=upstream.headers(headers))
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    return self._send(502, error_document(f"upstream {upstream.name} failed: {exc}",
                                                          code="upstream_unavailable", kind="api_error"))
                self.send_response(response.status)
                blocked = _hop_headers(response.getheaders())
                for key, value in response.getheaders():
                    if key.lower() not in blocked:
                        self.send_header(key, value)
                if response.status in (204, 304):
                    self.end_headers()
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            if response.length not in (None, 0):
                                raise http.client.IncompleteRead(b"", response.length)
                            break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (OSError, http.client.HTTPException) as exc:
                    LOG.warning("stream from %s ended early: %s", upstream.name, exc)
                    self.close_connection = True
            finally:
                connection.close()

    return Front


def make_server(upstreams, *, host="127.0.0.1", port=18710, token=None, ttl=10.0, timeout=600.0, fetch=fetch_models):
    """A ready-to-serve front door (call ``serve_forever``); ``port=0`` picks a free port."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("upstream timeout must be a positive finite number")
    table = ModelTable(upstreams, ttl=ttl, fetch=fetch)
    server_type = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((host, port), _handler(table, token, timeout))
    server.daemon_threads = True
    server.table = table
    return server
