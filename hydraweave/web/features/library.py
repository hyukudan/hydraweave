"""Model library (F36): model units per machine.

GET library -> hydraweave/ui-library-v1: every machine with its model roots (machine ``model_roots``, default
    ``<lab_dir>/models``; config assets; prefs ``library/roots``), its last scan (units with ``format``, ``quant``,
    ``size_bytes`` from the node and ``engine`` {family, presets, basis, assets} mapped here), verification results and library jobs kept from earlier releases, and the
    RTX big-job ledger guard state. It reads the portal store only: no node call, no file named by the client,
    nothing created. Capabilities describe managed verification and configured direct SSH copies.
POST library/roots {machine, roots} (audited): the extra model roots of one machine.
POST library/ledger-accept {} (audited): accept the current RTX big-job ledger as the guard's baseline.
Jobs: library/scan {machine?} (bounded parallel read-only inventory), library/verify and library/copy
    (admitted, owned, cancellable model-I/O jobs). Copies require a fresh plan and confirmation.
    The CLI family ``hydraweave models`` runs the same functions.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading
import time

from hydraweave import downloads, library_jobs, telemetry


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
SCAN_STALE_S = 600
COPY = {"available": True, "why": "Direct copies require Linux peers, a configured peer_ssh route and a pinned destination host key."}
VERIFY = {"available": True, "why": "Owned jobs verify complete manifests while the node's model-I/O gate is held."}
_scans = {}
_scan_lock = threading.Lock()


def _arguments(arguments, required, optional=()):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    extra = set(arguments) - set(required) - set(optional)
    missing = set(required) - set(arguments)
    if extra or missing:
        raise ValueError("unexpected arguments " + str(sorted(extra)) + "; missing arguments " + str(sorted(missing)))
    for key, value in arguments.items():
        if key != "roots" and (not isinstance(value, str) or not value or len(value) > 1024):
            raise ValueError(key + " must be a nonempty string")
    return arguments


def _machine(cluster, value):
    if not ID.fullmatch(value) or value not in cluster.machines:
        raise LookupError("unknown machine " + value)
    return value


def _ledger(api, store):
    return downloads.ledger_guard(api.config_path, store)


def _engines(cluster, machine, inventory):
    """Each unit's engine family against the current configuration (also for inventories kept by older scans)."""
    if not isinstance(inventory, dict):
        return
    for root in inventory.get("roots") or []:
        for unit in (root.get("units") or []) if isinstance(root, dict) else []:
            if isinstance(unit, dict):
                unit.update(downloads.engine_of(cluster, machine, root.get("path"), unit))


def overview(api, query):
    if query:
        raise ValueError("library takes no query arguments")
    store = telemetry.existing_store(api)
    cluster = api.cluster()
    roots = downloads.library_roots(cluster, store)
    why = {}
    if store is None:
        why["inventory"] = "the portal store is not open in this process: scans and results are not kept"
    verify = {}
    jobs = []
    if store is not None:
        for key, ts, value in store.items("library/verify", limit=500):
            verify[key] = value
        jobs = [value for _, _, value in store.items("library/jobs", limit=30)]
    machines = []
    for machine in cluster.machines.values():
        inventory = store.get("library/inventory", machine.id) if store is not None else None
        _engines(cluster, machine, inventory)
        row = {"id": machine.id, "os": machine.os, "access": machine.access, "lab_mode": machine.lab_mode,
               "roots": roots.get(machine.id, []), "inventory": inventory, "why": {}}
        row["copy_destinations"] = [mid for mid in machine.peer_ssh if mid in cluster.machines
                                    and cluster.machines[mid].os == "linux" and cluster.machines[mid].access == "ssh"]
        row["verify_available"] = machine.os == "linux" or machine.access == "local"
        if not row["roots"]:
            row["why"]["roots"] = "no model roots: the machine has no model_roots or lab_dir, none of the " \
                                  "configured assets is on it and prefs library/roots has none"
        elif inventory is None:
            row["why"]["inventory"] = "not scanned yet" if store is not None else why["inventory"]
        machines.append(row)
    with _scan_lock:
        scanning = sorted(mid for mid, since in _scans.items() if time.time() - since < SCAN_STALE_S)
    return api.document("library", machines=machines, verify=verify, jobs=jobs, scanning=scanning,
                        ledger=_ledger(api, store).describe(), why=why, copy=COPY, verify_job=VERIFY)


# --- synchronous POSTs -------------------------------------------------------------------------------------------

def roots(api, arguments):
    a = _arguments(arguments, ("machine", "roots"))
    cluster = api.cluster()
    if not isinstance(a["roots"], list) or any(not isinstance(value, str) for value in a["roots"]):
        raise ValueError("roots must be a list of absolute directories")
    return downloads.set_roots(cluster, telemetry.open_store(api), _machine(cluster, a["machine"]), a["roots"])


def ledger_accept(api, arguments):
    _arguments(arguments, ())
    store = telemetry.open_store(api)
    state = _ledger(api, store).accept()
    return {"accepted_at": state["accepted_at"], "size": state["size"]}


# --- jobs ----------------------------------------------------------------------------------------------------

def scan_validate(api, arguments):
    a = _arguments(arguments, (), ("machine",))
    cluster = api.cluster()
    machines = [_machine(cluster, a["machine"])] if "machine" in a else list(cluster.machines)
    with _scan_lock:
        now = time.time()
        busy = [mid for mid in machines if now - _scans.get(mid, 0) < SCAN_STALE_S]
        if busy:
            raise PermissionError("a scan of " + ", ".join(busy) + " is already running")
        for mid in machines:
            _scans[mid] = now
    return {"machines": machines}


def scan_rollback(api, arguments):
    """The scan was validated but not started (refused or not audited): its machines are free again."""
    with _scan_lock:
        for mid in arguments.get("machines") or []:
            _scans.pop(mid, None)


def scan(api, arguments, ctx):
    machines = arguments["machines"]
    try:
        cluster = api.cluster()
        store = telemetry.open_store(api)
        result = {}
        def one(mid):
            try:
                inventory = downloads.inventory(cluster, mid, store)
            except Exception as exc:          # one machine failing must not hide the others
                inventory = {"machine": mid, "scanned_at": time.time(), "roots": [], "error": str(exc)}
            return mid, inventory
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(machines)))) as pool:
            pending = {pool.submit(one, mid) for mid in machines if not ctx.cancelled()}
            for future in as_completed(pending):
                mid, inventory = future.result()
                ctx("scanned " + mid)
                if inventory.get("busy"):
                    result[mid] = "busy (another scan is running on the node; the previous result is kept)"
                    continue
                store.put("library/inventory", mid, inventory)
                result[mid] = "error: " + inventory["error"] if inventory.get("error") else "scanned"
        return {"machines": result}
    finally:
        with _scan_lock:
            for mid in machines:
                _scans.pop(mid, None)


def _job_arguments(api, arguments, copying=False):
    required = ("machine", "model_root", "name") + (("destination", "destination_root", "confirm") if copying else ())
    a = dict(_arguments(arguments, required))
    cluster, store = api.cluster(), telemetry.existing_store(api)
    library_jobs.selection(cluster, store, a["machine"], a["model_root"], a["name"])
    mids = {a["machine"]}
    if copying:
        library_jobs.selection(cluster, store, a["destination"], a["destination_root"], a["name"])
        if not re.fullmatch(r"[0-9a-f]{16}", a["confirm"]):
            raise ValueError("review the copy plan and send its confirmation code")
        mids.add(a["destination"])
    a["resources"] = sorted(mids)
    a["configuration"] = library_jobs.identity(cluster, mids)
    return a


def verify_validate(api, arguments):
    return _job_arguments(api, arguments)


def copy_validate(api, arguments):
    return _job_arguments(api, arguments, copying=True)


def copy_plan(api, arguments):
    _arguments(arguments, ("machine", "model_root", "name", "destination", "destination_root"))
    return library_jobs.copy_plan(api.cluster(), telemetry.existing_store(api), arguments)


def _run(api, arguments, ctx, kind):
    cluster = api.cluster()
    if library_jobs.identity(cluster, arguments["resources"]) != arguments["configuration"]:
        raise ValueError("machine configuration changed while the job was queued")
    return library_jobs.execute(cluster, telemetry.open_store(api), api.config_path, arguments, ctx, kind)


def verify(api, arguments, ctx):
    return _run(api, arguments, ctx, "verify")


def copy(api, arguments, ctx):
    return _run(api, arguments, ctx, "copy")


def register(reg):
    reg.get("library", overview)
    reg.post("library/roots", roots, audited=True)
    reg.post("library/ledger-accept", ledger_accept, audited=True)
    reg.job("library/scan", scan, validate=scan_validate, deadline_s=600, rollback=scan_rollback)
    reg.post("library/copy-plan", copy_plan)
    reg.job("library/verify", verify, validate=verify_validate, admission="bench",
            resources=lambda api, a: set(a["resources"]), deadline_s=86400)
    reg.job("library/copy", copy, validate=copy_validate, admission="bench",
            resources=lambda api, a: set(a["resources"]), deadline_s=86400)
