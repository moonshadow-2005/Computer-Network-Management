from __future__ import annotations

from app.core.db import Database, decode, encode, utc_now


def acknowledge_event(database: Database, event_id: int) -> dict | None:
    """Record operator acknowledgement without changing active/recovered lifecycle."""
    with database.connect() as db:
        found = db.execute("SELECT event_id FROM events WHERE event_id=?", (event_id,)).fetchone()
        if found is None:
            return None
        db.execute(
            "UPDATE events SET acknowledged_at=? WHERE event_id=? AND acknowledged_at IS NULL",
            (utc_now(), event_id),
        )
    return database.row("SELECT * FROM events WHERE event_id=?", (event_id,))


def record_ping(database: Database, device_id: str, target: str, sampled_at: str,
                measurement: dict, threshold: int = 3, data_source: str = "live") -> dict:
    with database.connect() as db:
        cursor = db.execute(
            """INSERT INTO ping_samples
               (device_id,target,sampled_at,query_status,packets_sent,packets_received,
                rtt_ms,loss_pct,error,data_source) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (device_id, target, sampled_at, measurement["query_status"],
             measurement["packets_sent"], measurement["packets_received"],
             measurement["rtt_ms"], measurement["loss_pct"], measurement["error"],
             data_source),
        )
        sample_id = cursor.lastrowid
        state = db.execute("SELECT * FROM fault_state WHERE device_id=?", (device_id,)).fetchone()
        failures = state["failures"] if state else 0
        active = state["active_event_id"] if state else None
        event_id = active
        # A failed command or unparseable reply is unknown, not proof of outage.
        if measurement["query_status"] == "ok" and measurement["loss_pct"] is not None:
            if measurement["loss_pct"] == 100:
                failures += 1
                if active:
                    existing = db.execute("SELECT evidence_sample_ids FROM events WHERE event_id=?", (active,)).fetchone()
                    evidence = list(decode(existing[0], [])) + [sample_id]
                    db.execute("UPDATE events SET last_seen=?,evidence_sample_ids=? WHERE event_id=?",
                               (sampled_at, encode(evidence), active))
                elif failures >= threshold:
                    event_id = db.execute(
                        """INSERT INTO events
                           (device_id,interface_id,type,state,first_seen,last_seen,
                            recovered_at,evidence_sample_ids,data_source)
                           VALUES (?,NULL,'ping_unreachable','active',?,?,NULL,?,?)""",
                        (device_id, sampled_at, sampled_at, encode([sample_id]), data_source),
                    ).lastrowid
            else:
                failures = 0
                if active:
                    db.execute(
                        """UPDATE events SET state='recovered',last_seen=?,recovered_at=?,
                           evidence_sample_ids=? WHERE event_id=?""",
                        (sampled_at, sampled_at,
                         encode(list(decode(db.execute("SELECT evidence_sample_ids FROM events WHERE event_id=?",
                                                       (active,)).fetchone()[0], [])) + [sample_id]), active),
                    )
                event_id = None
        db.execute(
            """INSERT INTO fault_state(device_id,failures,active_event_id)
               VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET
               failures=excluded.failures,active_event_id=excluded.active_event_id""",
            (device_id, failures, event_id),
        )
        return {"sample_id": sample_id, "failure_streak": failures,
                "active_event_id": event_id}


def record_interface_status(database: Database, interface_id: str, device_id: str,
                            sampled_at: str, admin_status: str | None,
                            oper_status: str | None, query_status: str,
                            threshold: int = 3, data_source: str = "live") -> dict:
    """Only an administratively up / operationally down interface is a fault."""
    with database.connect() as db:
        sample_id = db.execute(
            """INSERT INTO interface_status_samples
               (interface_id,sampled_at,admin_status,oper_status,query_status,data_source)
               VALUES (?,?,?,?,?,?)""",
            (interface_id, sampled_at, admin_status, oper_status, query_status, data_source),
        ).lastrowid
        state = db.execute("SELECT failures,active_event_id FROM interface_fault_state WHERE interface_id=?",
                           (interface_id,)).fetchone()
        failures = state["failures"] if state else 0
        active = state["active_event_id"] if state else None
        event_id = active
        if query_status == "ok" and admin_status is not None and oper_status is not None:
            if admin_status == "up" and oper_status == "down":
                failures += 1
                if active:
                    evidence = list(decode(db.execute("SELECT evidence_sample_ids FROM events WHERE event_id=?",
                                                      (active,)).fetchone()[0], [])) + [sample_id]
                    db.execute("UPDATE events SET last_seen=?,evidence_sample_ids=? WHERE event_id=?",
                               (sampled_at, encode(evidence), active))
                elif failures >= threshold:
                    event_id = db.execute(
                        """INSERT INTO events
                           (device_id,interface_id,type,state,first_seen,last_seen,
                            recovered_at,evidence_sample_ids,data_source)
                           VALUES (?,?, 'interface_oper_down','active',?,?,NULL,?,?)""",
                        (device_id, interface_id, sampled_at, sampled_at, encode([sample_id]), data_source),
                    ).lastrowid
            else:
                failures = 0
                if active:
                    evidence = list(decode(db.execute("SELECT evidence_sample_ids FROM events WHERE event_id=?",
                                                      (active,)).fetchone()[0], [])) + [sample_id]
                    db.execute("""UPDATE events SET state='recovered',last_seen=?,recovered_at=?,
                               evidence_sample_ids=? WHERE event_id=?""",
                               (sampled_at, sampled_at, encode(evidence), active))
                event_id = None
        db.execute(
            """INSERT INTO interface_fault_state(interface_id,failures,active_event_id)
               VALUES (?,?,?) ON CONFLICT(interface_id) DO UPDATE SET
               failures=excluded.failures,active_event_id=excluded.active_event_id""",
            (interface_id, failures, event_id),
        )
        return {"sample_id": sample_id, "failure_streak": failures,
                "active_event_id": event_id}


def save_snapshot(database: Database, device_id: str, sampled_at: str,
                  interfaces: list[dict], routes: list[dict],
                  data_source: str = "live", addresses: list[dict] | None = None) -> int:
    content = {
        "interfaces": {x["interface_id"]: {
            "description": x.get("description"), "admin_status": x.get("admin_status"),
            "oper_status": x.get("oper_status"), "reference_speed_bps": x.get("reference_speed_bps"),
        } for x in interfaces},
        "routes": {x["route_id"]: {
            "destination": x.get("destination"), "mask": x.get("mask"),
            "next_hop": x.get("next_hop"), "interface_id": x.get("interface_id"),
        } for x in routes},
    }
    content["addresses"] = ({f"{x['interface_id']}@{x['address']}": {
        k: x[k] for k in ("interface_id", "address", "mask", "network")
    } for x in addresses} if addresses is not None else None)
    with database.connect() as db:
        previous = db.execute(
            "SELECT snapshot_id,content FROM config_snapshots WHERE device_id=? ORDER BY snapshot_id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        if previous and decode(previous["content"]) == content:
            return previous["snapshot_id"]
        return db.execute(
            "INSERT INTO config_snapshots(device_id,sampled_at,content,data_source) VALUES (?,?,?,?)",
            (device_id, sampled_at, encode(content), data_source),
        ).lastrowid


def diff_snapshots(database: Database, before_id: int, after_id: int) -> dict:
    rows = database.rows("SELECT * FROM config_snapshots WHERE snapshot_id IN (?,?)", (before_id, after_id))
    by_id = {x["snapshot_id"]: x for x in rows}
    if before_id not in by_id or after_id not in by_id:
        raise LookupError("snapshot not found")
    if by_id[before_id]["device_id"] != by_id[after_id]["device_id"]:
        raise ValueError("snapshots belong to different devices")
    before = decode(by_id[before_id]["content"])
    after = decode(by_id[after_id]["content"])
    changes = []
    incomplete_sections = []
    for kind in ("interfaces", "routes", "addresses"):
        old, new = before.get(kind), after.get(kind)
        if old is None or new is None:
            incomplete_sections.append(kind)
            continue
        for key in sorted(old.keys() | new.keys()):
            if key not in old:
                changes.append({"kind": kind, "id": key, "change": "added", "before": None, "after": new[key]})
            elif key not in new:
                changes.append({"kind": kind, "id": key, "change": "removed", "before": old[key], "after": None})
            elif old[key] != new[key]:
                changes.append({"kind": kind, "id": key, "change": "changed", "before": old[key], "after": new[key]})
    sources = {by_id[before_id]["data_source"], by_id[after_id]["data_source"]}
    return {"device_id": by_id[before_id]["device_id"], "before_id": before_id,
            "after_id": after_id, "changes": changes,
            "data_source": next(iter(sources)) if len(sources) == 1 else "mixed",
            "incomplete_sections": incomplete_sections}
