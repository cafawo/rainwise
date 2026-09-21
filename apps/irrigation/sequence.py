"""Pure, bounded ordering and timing shared by Smart plans and reservations."""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_FLOOR


RELAY_MAX_SECONDS = 3276
# Even unusual historical timezone changes cannot justify an unbounded planner.
# Ordinary site-local days are 23–25 hours; allow date-line repeats as well.
MAX_LOCAL_DAY_SECONDS = 48 * 3600


def _available_seconds(value):
    if (
        isinstance(value, bool) or not math.isfinite(value)
        or not 0 <= value <= MAX_LOCAL_DAY_SECONDS
    ):
        raise ValueError("The sequence must fit within the remaining local day.")
    return value


def target_seconds(target_mm, rate, available_seconds=86400):
    """Floor a finite water target before allocating any pulse records."""
    _available_seconds(available_seconds)
    if (
        not math.isfinite(target_mm) or target_mm < 0
        or not math.isfinite(rate) or rate <= 0
    ):
        raise ValueError("The target must be finite and the watering rate positive.")
    seconds = (Decimal(str(target_mm)) * 3600 / Decimal(str(rate))).to_integral_value(
        rounding=ROUND_FLOOR
    )
    if seconds > Decimal(str(available_seconds)):
        raise ValueError(
            "The watering target cannot fit before local midnight; "
            "reduce the coverage window or peak demand, or check the measured rate."
        )
    return int(seconds)


def plan_sequence(
    members, *, smart=True, available_seconds=86400,
    controller_interval_seconds=60, command_allowance_seconds=0,
):
    """Plan ordered rounds, with each repeated valve resting its prior duration.

    Member dictionaries contain valve_id, order, total_seconds and
    run_cap_seconds. Returned offsets describe ideal elapsed watering/rest
    time; scheduling allowance is reported separately. Bounds are checked before
    constructing the pulse list, including for tiny rates and one-second caps.
    """
    _available_seconds(available_seconds)
    interval = controller_interval_seconds
    command = command_allowance_seconds
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("Controller interval must be a positive whole number.")
    if isinstance(command, bool) or not math.isfinite(command) or command < 0:
        raise ValueError("Command allowance must be finite and nonnegative.")
    members = list(members)
    watering = pulse_count = active_members = 0
    valve_ids = set()
    orders = set()
    for member in members:
        total = member["total_seconds"]
        cap = member["run_cap_seconds"]
        order = member["order"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("Watering totals must be nonnegative whole seconds.")
        if (
            isinstance(cap, bool) or not isinstance(cap, int)
            or not 1 <= cap <= RELAY_MAX_SECONDS
        ):
            raise ValueError("Run time before a break must be 1–3276 whole seconds.")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ValueError("Valve order must be a nonnegative whole number.")
        if member["valve_id"] in valve_ids or order in orders:
            raise ValueError("Sequence valves and their order must be unique.")
        valve_ids.add(member["valve_id"])
        orders.add(order)
        if total > available_seconds or watering + total > available_seconds:
            raise ValueError("Peak watering cannot fit before local midnight.")
        if not smart and total > cap:
            raise ValueError("Fixed watering must fit in one bounded run per valve.")
        watering += total
        if total:
            active_members += 1
            pulse_count += (total + cap - 1) // cap if smart else 1
    repeat_count = pulse_count - active_members
    allowance = (
        (pulse_count + repeat_count + 1) * interval + pulse_count * command
        if pulse_count else 0
    )
    if watering + allowance > available_seconds:
        raise ValueError(
            "Watering and scheduling allowance cannot fit before local midnight; "
            "increase the run time before a break or move the start earlier."
        )
    remaining = {member["valve_id"]: member["total_seconds"] for member in members}
    ready_at = {}
    elapsed = breaks = 0
    pulses = []
    pass_number = 0
    ordered = sorted(members, key=lambda member: member["order"])
    while len(pulses) < pulse_count:
        pass_number += 1
        for member in ordered:
            valve_id = member["valve_id"]
            if not remaining[valve_id]:
                continue
            duration = min(remaining[valve_id], member["run_cap_seconds"])
            rest = max(0, ready_at.get(valve_id, 0) - elapsed) if smart else 0
            start = elapsed + rest
            end = start + duration
            if end + allowance > available_seconds:
                raise ValueError(
                    "Watering, breaks and scheduling allowance cannot fit "
                    "before local midnight; move the start earlier or reduce "
                    "the coverage window or peak demand."
                )
            pulses.append({
                "valve_id": valve_id,
                "order": member["order"],
                "pass_number": pass_number,
                "duration_seconds": duration,
                "start_seconds": start,
                "end_seconds": end,
                "break_before_seconds": rest,
            })
            remaining[valve_id] -= duration
            ready_at[valve_id] = end + duration
            elapsed = end
            breaks += rest
    return {
        "pulses": pulses,
        "watering_seconds": watering,
        "break_seconds": breaks,
        "elapsed_seconds": elapsed,
        "pulse_count": pulse_count,
        "repeat_count": repeat_count,
        "scheduling_allowance_seconds": allowance,
        "reserved_seconds": elapsed + allowance,
    }
