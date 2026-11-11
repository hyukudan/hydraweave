"""Listen addresses for the portal and the metrics exporter.

Both bind 127.0.0.1 by default. An operator may choose any interface address, or 0.0.0.0 / :: for every interface,
like ComfyUI's --listen, so that a headless machine can serve its portal on the LAN. The portal protects LAN writes
with an access token; --no-auth and the metrics exporter print LAN_WARNING. The Host check refuses foreign names, so a
web page on another site cannot rebind its name to the portal.
"""
import ipaddress
import socket


LAN_WARNING = ("WARNING: {what} listens on {listen} without authentication: "
               "anyone who can reach this address on the network can control the cluster.")


def address(value, where="--listen"):
    """A bind address: an IPv4 or IPv6 literal (optionally in brackets); 0.0.0.0 and :: mean every interface."""
    if not isinstance(value, str) or not value or len(value) > 64 or "%" in value:
        raise ValueError(f"{where}: expected an IP address such as 127.0.0.1 or 0.0.0.0")
    text = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"{where}: expected an IP address such as 127.0.0.1 or 0.0.0.0") from None
    if ip.is_multicast:
        raise ValueError(f"{where}: a multicast address cannot be listened on")
    return str(ip)


def listen(value, where="--listen", default_port=None):
    """ADDR:PORT, [IPv6]:PORT, or a bare ADDR when default_port is given. Returns {"address", "port"}."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected ADDR:PORT")
    host, port = value, None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError(f"{where}: expected [IPv6]:PORT")
        host, rest = value[:end + 1], value[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"{where}: expected [IPv6]:PORT")
            port = rest[1:]
    elif value.count(":") == 1:
        host, port = value.split(":")
    if port is None:
        if default_port is None:
            raise ValueError(f"{where}: expected ADDR:PORT")
        number = default_port
    else:
        if not port.isascii() or not port.isdecimal():
            raise ValueError(f"{where}: the port must be a number")
        number = int(port)
    if not 0 <= number <= 65535:
        raise ValueError(f"{where}: the port must be in 0..65535")
    return {"address": address(host, where), "port": number}


def is_loopback(value):
    return ipaddress.ip_address(value).is_loopback


def family(value):
    return socket.AF_INET6 if ipaddress.ip_address(value).version == 6 else socket.AF_INET


def browse_address(value):
    """The address a browser on this machine uses to reach a server bound to `value`."""
    ip = ipaddress.ip_address(value)
    if ip.is_unspecified:
        return "::1" if ip.version == 6 else "127.0.0.1"
    return str(ip)


def url_host(value, port):
    return f"[{value}]:{port}" if ":" in value else f"{value}:{port}"


def _local_names():
    names = set()
    for get in (socket.gethostname, socket.getfqdn):
        try:
            name = get().strip().lower().rstrip(".")
        except OSError:
            continue
        if name:
            names.update({name, name.split(".")[0], name.split(".")[0] + ".local"})
    return names


def host_allowed(header, bound, port, names=None):
    """Whether a Host header may reach a server bound to `bound`:`port`.

    Loopback binds accept only 127.0.0.1 and localhost (the historical rule). Other binds also accept the bound address
    itself, any IP literal when bound to every interface, and this machine's own host names.
    """
    if not isinstance(header, str) or not header or len(header) > 300:
        return False
    if header.startswith("["):
        end = header.find("]")
        if end < 0 or header[end + 1:end + 2] != ":":
            return False
        name, given = header[1:end], header[end + 2:]
    else:
        if header.count(":") != 1:
            return False
        name, given = header.split(":")
    if not given.isascii() or not given.isdecimal() or int(given) != port:
        return False
    name = name.lower()
    if is_loopback(bound):
        return name in {"127.0.0.1", "localhost"}
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or name in (_local_names() if names is None else names)
    return ip.is_loopback or ipaddress.ip_address(bound).is_unspecified or str(ip) == bound
