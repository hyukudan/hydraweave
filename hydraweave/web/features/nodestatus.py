"""One node status model for every page (sidebar, Nodes, Machine, Overview).

Two questions are answered separately, and never merged into one "online" flag:

- reachability: did this controller reach the node, and when was the last successful contact REALLY made
  (a cached answer never renews that time);
- telemetry freshness, per source (``doctor``, ``gpu``, ``host``): ``fresh``, ``stale`` since T, ``paused`` while
  a GPU slot holds the node, ``failed`` with the reason, ``never`` collected. A fresh source whose key checks came
  back unavailable is fresh but incomplete (``missing`` names each check and its reason).

``build`` is pure (the unit tests drive it); ``collect`` gathers its inputs from the portal process.
"""
from __future__ import annotations

import math

SOURCES = ("doctor", "gpu", "host")
LABELS = {"doctor": "Node checks", "gpu": "GPU counters", "host": "Host telemetry"}
#: The checks of each source without which the pages have nothing to show for it.
KEY_CHECKS = {"doctor": (), "gpu": ("amd", "ram"), "host": ("gpus", "memory", "cpu")}
CHECK_LABELS = {"os": "operating system", "amd": "GPU counters", "ram": "system memory", "gpus": "GPU counters",
                "memory": "system memory", "cpu": "CPU counters"}
FRESH_INTERVALS = 3          # a source is fresh while its last success is within 3 collection intervals
DEFAULT_INTERVAL_S = 30.0
UNPAIRED = ("pair this node first", "has not been paired", "not paired with", "unknown host key")
OLD_TOOL = ("did not answer this part", "unknown verb", "unknown node schema", "unexpected node answer",
            "unsupported verb")
ACTIONS = {
    "unpaired": {"label": "Pair this node from this controller", "href": "#/devices"},
    "version": {"label": "Update the node tool", "href": "#/ops"},
    "unreachable": {"label": "Troubleshoot the connection", "href": "#/troubleshoot"},
    "failed": {"label": "Open troubleshooting", "href": "#/troubleshoot"},
    "partial": {"label": "Open troubleshooting", "href": "#/troubleshoot"},
    "never": {"label": "Check the collectors", "href": "#/ops"},
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _matches(text, patterns):
    text = str(text or "").lower()
    return any(pattern in text for pattern in patterns)


def missing_checks(source, value, ignore=()):
    """The key checks of one successful answer that are unavailable: [{check, label, why}]."""
    checks = (value or {}).get("checks") if isinstance(value, dict) else None
    if not isinstance(checks, dict):
        return []
    out = []
    for name in KEY_CHECKS.get(source, ()):
        if name in ignore:
            continue
        check = checks.get(name)
        if isinstance(check, dict) and check.get("status") == "ok" and check.get("value") is not None:
            continue
        why = (check.get("why") if isinstance(check, dict) else None) or "not reported by this node tool version"
        out.append({"check": name, "label": CHECK_LABELS.get(name, name), "why": str(why)[:300]})
    return out


def source_state(name, read, now, interval, paused=None, until=None, next_check_s=None, ignore=()):
    """The freshness of one source. ``read``: {ok_at, tried_at, error, value, never_why} (all optional)."""
    read = read or {}
    ok_at = read.get("ok_at") if _finite(read.get("ok_at")) else None
    tried_at = read.get("tried_at") if _finite(read.get("tried_at")) else None
    error = read.get("error")
    out = {"source": name, "label": LABELS.get(name, name), "state": "never", "at": ok_at, "since": None,
           "until": None, "next_check_s": None, "why": None, "missing": []}
    failed = bool(error) and (ok_at is None or tried_at is None or tried_at >= ok_at)
    fresh = ok_at is not None and not failed and now - ok_at <= FRESH_INTERVALS * interval
    if fresh:
        out["state"] = "fresh"
        out["missing"] = missing_checks(name, read.get("value"), ignore)
    elif paused:
        out.update(state="paused", why=str(paused), since=ok_at, until=until if _finite(until) else None,
                   next_check_s=next_check_s)
    elif failed:
        out.update(state="failed", why=str(error)[:300], since=tried_at)
    elif ok_at is not None:
        out.update(state="stale", since=ok_at,
                   why=read.get("stale_why") or f"no new sample for more than {int(FRESH_INTERVALS * interval)} s")
    else:
        out["why"] = read.get("never_why") or "not collected yet (first read pending)"
    return out


def build(*, now, reads, access="ssh", intervals=None, paused=None, turn_at=None, until=None, next_check_s=None,
          version=None, ignore=None, warnings=(), turn_error=None, turn_interval=None):
    """The status document of one node.

    reads: {source: {ok_at, tried_at, error, value, never_why}}; paused: the reason node reads are held (a GPU slot);
    turn_at: when the slot's holder was last seen on the node (a real contact); version: {deployed, expected}.
    turn_error: why the latest turn check failed (after turn_at); turn_interval: the cadence of that check. A
    holder seen once proves the node reachable only while the check keeps answering: a failed check, or no
    answer within FRESH_INTERVALS checks, ends it. The pause of the node's reads is reported regardless.
    """
    intervals = intervals or {}
    ignore = ignore or {}
    sources = {name: source_state(name, (reads or {}).get(name), now, intervals.get(name, DEFAULT_INTERVAL_S),
                                  paused=paused, until=until, next_check_s=next_check_s, ignore=ignore.get(name, ()))
               for name in SOURCES}
    rows = list(sources.values())
    oks = [row["at"] for row in rows if row["at"] is not None]
    last_sample = max(oks) if oks else None
    contacts = oks + ([turn_at] if paused and _finite(turn_at) else [])
    local = access == "local"
    turn_seen = bool(paused) and _finite(turn_at)
    turn_expired = turn_seen and _finite(turn_interval) and now - turn_at > FRESH_INTERVALS * turn_interval
    turn_live = turn_seen and not turn_error and not turn_expired
    last_contact = now if local else (max(contacts) if contacts else None)
    by = lambda state: [row for row in rows if row["state"] == state]      # noqa: E731
    fresh, failed, stale, never = by("fresh"), by("failed"), by("stale"), by("never")
    is_paused = bool(by("paused"))
    errors = [row["why"] for row in failed]
    deployed, expected = (version or {}).get("deployed"), (version or {}).get("expected")
    mismatch = bool(deployed and expected and deployed != expected)
    old_tool = any(_matches(why, OLD_TOOL) for why in errors)
    version_doc = None
    if version is not None:
        version_doc = {"deployed": deployed, "expected": expected,
                       "match": None if not (deployed and expected) else deployed == expected,
                       "why": (version or {}).get("why")}
    incomplete = [(row, miss) for row in fresh for miss in row["missing"]]

    if not local and not fresh and any(_matches(why, UNPAIRED) for why in errors):
        state, code, level = "unpaired", "unpaired", "info"
        summary, detail = "Not paired with this controller", errors[0]
    elif local or fresh or (is_paused and turn_live):
        state = "reachable"
        if mismatch or (old_tool and not local):
            code, level, summary = "version", "warn", "Node tool version differs: update"
            detail = (f"deployed {str(deployed)[:12]}, this controller expects {str(expected)[:12]}" if mismatch
                      else errors[0])
        elif is_paused:
            code, level, summary = "paused", "info", "Reachable - telemetry paused while the GPU runs a job"
            detail = str(paused)
        elif failed:
            code, level = "failed", "warn"
            summary, detail = f"Reachable - {failed[0]['label']} unreadable: {failed[0]['why']}", failed[0]["why"]
        elif incomplete:
            row, miss = incomplete[0]
            code, level = "partial", "warn"
            summary, detail = f"Reachable - {miss['label']} unreadable: {miss['why']}", miss["why"]
        elif warnings:
            first = warnings[0]
            text = str((first.get("message") or first.get("value") or first) if isinstance(first, dict) else first)[:300]
            code, level, summary, detail = "warning", "warn", f"Reachable - warning: {text}", text
        elif stale:
            code, level = "stale", "warn"
            summary, detail = f"Reachable - {stale[0]['label'].lower()} stale", stale[0]["why"]
        elif never:
            code, level = "never", "warn"
            summary, detail = f"Reachable - {never[0]['label'].lower()} never collected", never[0]["why"]
        else:
            code, level, summary, detail = "ok", "ok", "Reachable - telemetry current", None
    elif failed:
        state, code, level = "unreachable", "unreachable", "err"
        summary, detail = f"Unreachable: {errors[0]}", errors[0]
    elif is_paused and turn_error:
        state, code, level = "unreachable", "unreachable", "err"
        detail = str(turn_error)[:300]
        summary = f"Unreachable: the GPU turn check failed: {detail}"
    elif is_paused and turn_expired:
        state, code, level = "stale", "stale", "warn"
        summary = "Not contacted recently - telemetry was paused for a GPU job when the node last answered"
        detail = f"no GPU turn reading for more than {int(FRESH_INTERVALS * turn_interval)} s"
    elif is_paused:
        state, code, level = "unknown", "paused", "info"
        summary, detail = "Telemetry paused while the GPU runs a job - node not contacted yet", str(paused)
    elif stale:
        state, code, level = "stale", "stale", "warn"
        summary, detail = "Not contacted recently - showing the last answer", stale[0]["why"]
    else:
        state, code, level = "unknown", "never", "off"
        detail = next((row["why"] for row in never if row["why"]), "not collected yet")
        summary = f"No data yet: {detail}"
    return {"state": state, "code": code, "level": level,
            "reachable": True if state == "reachable" else None if state in ("unknown", "stale") else False,
            "last_contact": last_contact, "last_sample": last_sample, "summary": summary, "detail": detail,
            "action": ACTIONS.get(code), "sources": sources, "version": version_doc, "warnings": list(warnings),
            "paused": {"why": str(paused), "until": until if _finite(until) else None,
                       "next_check_s": next_check_s} if is_paused else None}


def health(model, warnings=()):
    """The one-word state older readers of the inventory use, derived from the model (never computed apart)."""
    state = model["state"]
    if state == "reachable":
        return "degraded" if warnings or model.get("warnings") else "online"
    return {"unpaired": "unpaired", "stale": "stale"}.get(state, "offline")


def source_why(model, source):
    """Why a field fed by one source has no value: the source's own reason, in words an operator can act on."""
    row = (model.get("sources") or {}).get(source) or {}
    state, why = row.get("state"), row.get("why")
    if state == "fresh":
        missing = row.get("missing") or []
        return "; ".join(f"{m['label']}: {m['why']}" for m in missing) or "not reported by the node"
    if state == "paused":
        return f"telemetry paused while the GPU runs a job ({why})" if why else "telemetry paused during a GPU slot"
    if state == "failed":
        return f"{row.get('label', source)} unreadable: {why}"
    if state == "stale":
        return f"{row.get('label', source)} stale: {why}"
    return why or "not collected yet"


def _version(api, machine):
    """{deployed, expected} of the node tool as the portal's own check recorded it; None when it does not apply."""
    if machine.access == "local":
        return None
    try:
        from hydraweave.web.features import ops
        known = ops.pref(api, ops.NAMESPACE, "node_tool", {}) or {}
        entry = known.get(machine.id)
        if not isinstance(entry, dict):
            return {"deployed": None, "expected": None, "why": "node tool version not checked yet"}
        if entry.get("error"):
            return {"deployed": None, "expected": None, "why": str(entry["error"])[:300]}
        return {"deployed": entry.get("deployed"), "expected": ops.expected_digest(), "why": None}
    except Exception as exc:        # the version check is advisory: its failure never hides the node's status
        return {"deployed": None, "expected": None, "why": f"{type(exc).__name__}: {exc}"[:300]}


def collect(api, machine, now, *, store=None, until=None, probe=None):
    """The status of one machine from what this portal process holds (no node call, no store write)."""
    from hydraweave import pollbudget, telemetry
    from hydraweave.web.features import machines as fleet

    inspections = getattr(api, "_inspections", None) or {}
    contacts = getattr(api, "_contacts", None) or {}
    reads = {}
    for verb in ("doctor", "gpu"):
        read = inspections.get((machine.id, verb))
        ok_at = contacts.get((machine.id, verb))
        reads[verb] = {"ok_at": ok_at, "tried_at": (read or {}).get("at"), "error": (read or {}).get("error"),
                       "value": (read or {}).get("value"),
                       "never_why": "not read yet (a node is read once per 30 s while a portal page is open)"}
    store = store if store is not None else telemetry.existing_store(api)
    entry = store.get(fleet.HOSTS, machine.id) if store is not None else None
    if store is None:
        reads["host"] = {"never_why": "telemetry store not open in this portal process (samplers disabled)"}
    elif not entry:
        reads["host"] = {"never_why": "no collection for this machine yet (samplers not running or first round pending)"}
    else:
        ok = bool(entry.get("ok"))
        reads["host"] = {"ok_at": entry.get("last_ok"), "tried_at": entry.get("ts") if ok else entry.get("failed_at"),
                         "error": None if ok else entry.get("error") or "last call failed",
                         "value": entry.get("host"), "stale_why": entry.get("skip_why")}
    paused = inspections.get((machine.id, "paused"))
    skip = (entry or {}).get("skip_why")
    if not paused and isinstance(skip, str) and skip.startswith("paused"):
        paused = skip
    turn_at = turn_error = None
    if machine.access != "local":
        try:
            reading = pollbudget.for_api(api).turn_reading(machine.id)
        except Exception:
            reading = None
        if reading is not None and reading[0] is not None:
            turn_at = reading[2]
            try:                    # the turn check's own latest outcome: a failure after the holder was seen
                from hydraweave.web.features import turns
                last = turns.latest(api, machine.id) or {}
                tried = last.get("fetched_at")
                if last.get("error") and _finite(tried) and tried >= turn_at:
                    turn_error = last["error"]
            except Exception:
                turn_error = None
            paused = paused or f"paused: a GPU turn holds {machine.id} ({reading[0]}); node reads wait for its end"
        elif paused and not (isinstance(skip, str) and skip.startswith("paused")):
            paused = None           # the slot ended: the next read is simply pending
    host_checks = ((entry or {}).get("host") or {}).get("checks") or {}
    nvidia = host_checks.get("nvidia")
    has_nvidia = isinstance(nvidia, dict) and nvidia.get("status") == "ok" and bool(nvidia.get("value"))
    windows = str(getattr(machine, "os", "") or "").lower().startswith("win")
    has_nvidia = has_nvidia or any(d.get("vendor") != "amd" for d in (probe or {}).get("devices") or [] if isinstance(d, dict))
    ignore = {"gpu": ("amd",) if windows or has_nvidia else (), "host": ("gpus",) if windows or has_nvidia else ()}
    interval = fleet.CONTROL_INTERVAL if machine.access == "local" else fleet.NODE_INTERVAL
    warnings = []
    doctor = reads["doctor"]
    if isinstance(doctor.get("value"), dict) and not doctor.get("error"):
        try:                        # the link warnings the node's own checks report (same reading as the inventory)
            from hydraweave.web import api as web_api
            warnings = list((web_api._usb4(doctor["value"].get("checks") or {}) or {}).get("warnings") or [])
        except Exception:
            warnings = []
    return build(now=now, reads=reads, access=machine.access,
                 intervals={"doctor": pollbudget.CYCLE_S, "gpu": pollbudget.CYCLE_S, "host": interval},
                 paused=paused, turn_at=turn_at, until=until, next_check_s=pollbudget.TURN_CHECK_S,
                 version=_version(api, machine), ignore=ignore, warnings=warnings,
                 turn_error=turn_error, turn_interval=pollbudget.TURN_CHECK_S)


def controller(api):
    """Which controller serves this page: each node's portal may be its own controller, with its own pairing
    credentials and its own telemetry store."""
    import socket
    doc = {"host": None, "config": None, "store": None, "why": {}}
    try:
        doc["host"] = socket.gethostname()
    except OSError as exc:
        doc["why"]["host"] = str(exc)
    try:
        from hydraweave import telemetry
        doc["store"] = "open" if telemetry.existing_store(api) is not None else "not open"
    except Exception as exc:
        doc["why"]["store"] = f"{type(exc).__name__}: {exc}"[:200]
    path = getattr(api, "config_path", None)
    doc["config"] = getattr(path, "name", None) or (str(path).replace("\\", "/").rsplit("/", 1)[-1] if path else None)
    if doc["config"] is None:
        doc["why"]["config"] = "configuration path not known to this process"
    return doc
