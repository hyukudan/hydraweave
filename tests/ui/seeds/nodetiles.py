"""Memory series for the Nodes trend tiles and the Machine memory charts: two hours of GPU memory by driver domain
(VRAM carve-out + GTT on the unified-memory nodes) and of system memory, one sample every 30 s."""
import math

GIB = 1024 ** 3


def seed(store, now):
    for name in ("node_gpu_mem_used_bytes", "node_gpu_mem_total_bytes", "node_mem_available_bytes", "node_mem_total_bytes"):
        store.describe(name, "gauge", unit="bytes", interval=30)
    batches = []
    t = now - 2 * 3600
    while t < now:
        for index, machine in enumerate(("halo-1", "halo-2", "workstation")):
            phase = t / 900.0 + index
            total = (128 if machine != "workstation" else 64) * GIB
            points = [("node_mem_total_bytes", {"machine": machine}, total),
                      ("node_mem_available_bytes", {"machine": machine}, total * (0.55 + 0.2 * math.sin(phase)))]
            if machine != "workstation":
                labels = {"machine": machine, "device": "card0"}
                points += [("node_gpu_mem_total_bytes", {**labels, "domain": "vram"}, 0.5 * GIB),
                           ("node_gpu_mem_used_bytes", {**labels, "domain": "vram"}, 0.3 * GIB),
                           ("node_gpu_mem_total_bytes", {**labels, "domain": "gtt"}, 108 * GIB),
                           ("node_gpu_mem_used_bytes", {**labels, "domain": "gtt"}, (40 + 25 * math.sin(phase)) * GIB)]
            batches.append((points, t, 30))
        t += 30

    def insert(pending):
        for points, ts, step in batches:
            store._insert(pending, store._rows(points), ts, step)
    store._atomic(insert)
