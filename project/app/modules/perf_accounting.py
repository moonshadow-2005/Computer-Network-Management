from __future__ import annotations

from datetime import datetime

from app.core.db import Database, decode, encode


UINT64_MAX = 2**64 - 1
MAX_GAP_S = 60.0


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) -
            datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def record_counter(
    database: Database, interface_id: str, in_octets: int | None,
    out_octets: int | None, sampled_at: str, sys_uptime_ticks: int | None,
    discontinuity_ticks: int | None = None, data_source: str = "live",
    query_status: str = "ok", reference_speed_bps: int | None = None,
) -> dict:
    """Store raw uint64 counters and derive a rate only from two sound samples."""
    for value in (in_octets, out_octets):
        if value is not None and not 0 <= value <= UINT64_MAX:
            raise ValueError("counter must be an unsigned 64-bit integer")
    with database.connect() as db:
        previous = db.execute(
            """SELECT * FROM counter_samples WHERE interface_id=?
               ORDER BY sampled_at DESC, sample_id DESC LIMIT 1""", (interface_id,),
        ).fetchone()
        cursor = db.execute(
            """INSERT INTO counter_samples
               (interface_id,sampled_at,in_octets,out_octets,sys_uptime_ticks,
                discontinuity_ticks,query_status,invalid_reason,data_source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (interface_id, sampled_at, str(in_octets) if in_octets is not None else None,
             str(out_octets) if out_octets is not None else None,
             str(sys_uptime_ticks) if sys_uptime_ticks is not None else None,
             str(discontinuity_ticks) if discontinuity_ticks is not None else None,
             query_status, None if query_status == "ok" else "query_failed", data_source),
        )
        sample_id = cursor.lastrowid
        reason = None
        seconds = _seconds(previous["sampled_at"], sampled_at) if previous else 0.0
        if query_status != "ok" or in_octets is None or out_octets is None:
            reason = "query_failed"
        elif previous is None or previous["query_status"] != "ok" or previous["in_octets"] is None or previous["out_octets"] is None:
            reason = "first_or_previous_invalid_sample"
        elif previous["data_source"] != data_source:
            reason = "source_changed"
        elif seconds <= 0:
            reason = "non_increasing_time"
        elif seconds > MAX_GAP_S:
            reason = "sample_gap"
        elif (sys_uptime_ticks is not None and previous["sys_uptime_ticks"] is not None
              and sys_uptime_ticks < int(previous["sys_uptime_ticks"])):
            reason = "agent_restarted"
        elif (discontinuity_ticks is not None and previous["discontinuity_ticks"] is not None
              and discontinuity_ticks != int(previous["discontinuity_ticks"])):
            reason = "counter_discontinuity"
        elif in_octets < int(previous["in_octets"]) or out_octets < int(previous["out_octets"]):
            reason = "counter_reset_or_wrap"
        in_delta = in_octets - int(previous["in_octets"]) if reason is None else None
        out_delta = out_octets - int(previous["out_octets"]) if reason is None else None
        in_rate = 8 * in_delta / seconds if in_delta is not None else None
        out_rate = 8 * out_delta / seconds if out_delta is not None else None
        speed = reference_speed_bps if reference_speed_bps and reference_speed_bps > 0 else None
        evidence = [previous["sample_id"], sample_id] if previous else [sample_id]
        cursor = db.execute(
            """INSERT INTO metric_samples
               (interface_id,sampled_at,interval_start,in_delta_bytes,out_delta_bytes,
                in_rate_bps,out_rate_bps,in_util_pct,out_util_pct,valid,
                invalid_reason,source_sample_ids,data_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (interface_id, sampled_at, previous["sampled_at"] if previous else None,
             str(in_delta) if in_delta is not None else None,
             str(out_delta) if out_delta is not None else None,
             in_rate, out_rate, 100 * in_rate / speed if speed and in_rate is not None else None,
             100 * out_rate / speed if speed and out_rate is not None else None,
             int(reason is None), reason, encode(evidence), data_source),
        )
        return {"sample_id": sample_id, "metric_id": cursor.lastrowid,
                "valid": reason is None, "invalid_reason": reason,
                "in_rate_bps": in_rate, "out_rate_bps": out_rate}


def traffic_report(database: Database, interface_id: str, start: str, end: str) -> dict:
    window_s = _seconds(start, end)
    if window_s <= 0:
        raise ValueError("from must precede to")
    rows = database.rows(
        """SELECT * FROM metric_samples WHERE interface_id=? AND sampled_at>=?
           AND sampled_at<=? ORDER BY sampled_at, metric_id""",
        (interface_id, start, end),
    )
    included = [x for x in rows if x["valid"] and x["interval_start"] >= start]
    covered = sum(_seconds(x["interval_start"], x["sampled_at"]) for x in included)
    invalid = []
    for row in rows:
        if row in included or _seconds(start, row["sampled_at"]) <= 0:
            continue
        interval_start = row["interval_start"]
        if interval_start is not None and _seconds(interval_start, end) <= 0:
            continue
        invalid.append({
            "metric_id": row["metric_id"],
            "reason": row["invalid_reason"] or "crosses_window_boundary",
        })
    first_start = included[0]["interval_start"] if included else None
    last_end = included[-1]["sampled_at"] if included else None
    complete = bool(included and first_start == start and last_end == end and
                    abs(covered - window_s) < 0.001 and not invalid)
    sources = {x["data_source"] for x in included}
    if not sources:
        interface = database.row("SELECT data_source FROM interfaces WHERE interface_id=?", (interface_id,))
        sources = {interface["data_source"]} if interface else set()
    source_sample_ids = []
    seen_source_sample_ids = set()
    for metric in included:
        raw_ids = decode(metric["source_sample_ids"], [])
        if not isinstance(raw_ids, list):
            continue
        for sample_id in raw_ids:
            is_sample_id = isinstance(sample_id, int) and not isinstance(sample_id, bool)
            if is_sample_id and sample_id not in seen_source_sample_ids:
                seen_source_sample_ids.add(sample_id)
                source_sample_ids.append(sample_id)
    return {
        "interface_id": interface_id, "observation_point": interface_id,
        "data_source": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "none",
        "from": start, "to": end,
        "in_bytes": str(sum(int(x["in_delta_bytes"]) for x in included)) if included else None,
        "out_bytes": str(sum(int(x["out_delta_bytes"]) for x in included)) if included else None,
        "coverage_pct": round(min(100.0, 100.0 * covered / window_s), 2),
        "complete": complete, "invalid_intervals": invalid,
        "source_sample_ids": source_sample_ids,
        "metric_sample_ids": [x["metric_id"] for x in included],
        "note": "仅合计完全位于窗口内的有效采样区间；缺口和边界不外推。",
    }
