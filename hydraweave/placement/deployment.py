"""Use the engine's measured per-rank admission, for UMA and discrete devices."""
from ..deployment import parse_plan, uint
from ..vramplan import Plan, PlanError, check_peak


def plan(receipt, *, rank=0):
    try:
        uint(rank, "requested rank")
        receipt = parse_plan(receipt)
        row = next(r for r in receipt["ranks"] if r["rank"] == rank)
        footprint = row["footprint"]
        weights = uint(footprint["weights"], "footprint.weights")
        staging = uint(footprint["staging"], "footprint.staging")
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise PlanError(f"engine deployment: {exc}") from exc
    fixed = row["required_bytes"] - weights - staging
    if fixed < 0:
        raise PlanError("engine deployment: footprint exceeds required bytes")
    chosen = receipt["chosen"]
    check_peak(receipt.get("model", "engine-deployment"), row["required_bytes"], row["free_bytes"], row["margin_bytes"])
    return Plan(engine=receipt.get("model", "engine-deployment"), cap_bytes=row["free_bytes"],
                margin_bytes=row["margin_bytes"], fixed_bytes=fixed, stream_bytes=staging,
                resident=[], streamed=[], resident_bytes=weights, pinned_bytes=0, disk_bytes=0,
                peak_bytes=row["required_bytes"], benefit=0, flags={}, fixed=[("engine fixed owners", fixed)],
                notes=[f"engine chose {chosen['topology']}, context {chosen['context']}, slots {chosen['slots']}",
                       *receipt.get("reasons", [])], source={"deployment": receipt, "rank": rank})
