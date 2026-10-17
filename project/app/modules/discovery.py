"""Read-only IPv4 discovery and evidence-based logical topology.

MIB-II ipAddrTable: https://www.rfc-editor.org/rfc/rfc1213.html#page-31
Interface subnet membership does not establish physical adjacency or device identity.
"""
from __future__ import annotations

import ipaddress

from app.collectors.snmp import SnmpClient, SnmpError, parse_oid
from app.core.db import Database, decode


IP_ADDR = "1.3.6.1.2.1.4.20.1.1"
IP_IF_INDEX = "1.3.6.1.2.1.4.20.1.2"
IP_MASK = "1.3.6.1.2.1.4.20.1.3"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"


def read_inventory(snmp: SnmpClient, database: Database, device_id: str, address: str,
                   sampled_at: str, interface_ids: set[str], limit: int = 64) -> dict:
    """Persist each attempt, including incomplete/unsupported tables, atomically."""
    identity = {}
    for field, oid in (("sys_name", SYS_NAME), ("sys_object_id", SYS_OBJECT_ID)):
        try:
            value = snmp.get(address, oid).value
            identity[field] = value[:512] if isinstance(value, str) else None
        except (SnmpError, OSError, ValueError):
            identity[field] = None
    rows, issues = [], []
    try:
        entries = snmp.walk(address, IP_ADDR, limit)
        if not entries:
            raise SnmpError("IPv4 address table is empty or unavailable")
        if len(entries) >= limit:
            issues.append("address walk reached configured limit")
        prefix = parse_oid(IP_ADDR)
        seen = set()
        for entry in entries:
            try:
                if entry.oid[:len(prefix)] != prefix:
                    raise ValueError("address OID is outside requested table")
                suffix = ".".join(map(str, entry.oid[len(prefix):]))
                if not isinstance(entry.value, str):
                    raise ValueError("address value is not an IPv4 address")
                ip = str(ipaddress.IPv4Address(entry.value))
                if ip != str(ipaddress.IPv4Address(suffix)) or ip in seen:
                    raise ValueError("address value/index mismatch or duplicate")
                seen.add(ip)
                index = snmp.get(address, f"{IP_IF_INDEX}.{suffix}").value
                if isinstance(index, bool) or not isinstance(index, int) or index <= 0:
                    raise ValueError("invalid address interface index")
                interface_id = f"{device_id}:{index}"
                if interface_id not in interface_ids:
                    raise ValueError("address references an unobserved interface")
                mask = snmp.get(address, f"{IP_MASK}.{suffix}").value
                if not isinstance(mask, str):
                    raise ValueError("invalid IPv4 netmask")
                # IPv4Network also accepts hostmasks; explicitly require a netmask.
                bits = int(ipaddress.IPv4Address(mask))
                inverse = bits ^ 0xFFFFFFFF
                if inverse & (inverse + 1):
                    raise ValueError("non-contiguous IPv4 netmask")
                network = ipaddress.IPv4Network(f"{ip}/{bits.bit_count()}", strict=False)
                rows.append({"interface_id": interface_id, "if_index": index,
                             "address": ip, "mask": mask, "network": str(network)})
            except (SnmpError, OSError, ValueError) as exc:
                issues.append(str(exc)[:160])
    except (SnmpError, OSError, ValueError) as exc:
        issues.append(str(exc)[:160])
    status = "ok" if rows and not issues else "partial" if rows else "unavailable"
    result = store_inventory(database, device_id, sampled_at, rows, status,
                             identity.get("sys_name"), identity.get("sys_object_id"), issues)
    return {**result, "addresses": rows}


def store_inventory(database: Database, device_id: str, sampled_at: str, addresses: list[dict],
                    status: str = "ok", sys_name: str | None = None,
                    sys_object_id: str | None = None, issues: list[str] | None = None,
                    data_source: str = "live") -> dict:
    from app.core.db import encode
    with database.connect() as db:
        discovery_id = db.execute(
            """INSERT INTO network_discoveries
               (device_id,sampled_at,query_status,sys_name,sys_object_id,issues,data_source)
               VALUES (?,?,?,?,?,?,?)""",
            (device_id, sampled_at, status, sys_name, sys_object_id, encode(issues or []), data_source),
        ).lastrowid
        for item in addresses:
            db.execute(
                """INSERT INTO interface_addresses
                   (discovery_id,device_id,interface_id,if_index,address,mask,network,sampled_at,data_source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (discovery_id, device_id, item["interface_id"], item["if_index"], item["address"],
                 item["mask"], item["network"], sampled_at, data_source),
            )
    return {"discovery_id": discovery_id, "query_status": status,
            "address_count": len(addresses), "issues": issues or []}


def latest_discoveries(database: Database) -> list[dict]:
    rows = database.rows("""SELECT d.* FROM network_discoveries d
        WHERE discovery_id=(SELECT discovery_id FROM network_discoveries n
          WHERE n.device_id=d.device_id ORDER BY sampled_at DESC,discovery_id DESC LIMIT 1)
        ORDER BY device_id""")
    for row in rows:
        row["issues"] = decode(row["issues"], [])
    return rows


def topology(database: Database) -> dict:
    devices = database.rows("SELECT * FROM devices ORDER BY device_id")
    nodes = [{"node_id": d["device_id"], **d} for d in devices]
    networks = {f"192.168.{n}.0/24": f"subnet-{n}" for n in (1, 2, 3)}
    edges = []
    for router, subnet in (("r-0-1", 1), ("r-0-2", 1), ("r-0-5", 2), ("r-0-8", 3)):
        edges.append({"edge_id": f"doc-{router}-{subnet}", "source": router,
                      "target": f"subnet-{subnet}", "kind": "document", "confirmed": False,
                      "basis": "课程说明逻辑连接关系", "data_source": "document"})
    for host, subnet in (("host-1", 1), ("host-2", 2), ("host-3", 3)):
        edges.append({"edge_id": f"doc-{host}-{subnet}", "source": host,
                      "target": f"subnet-{subnet}", "kind": "document", "confirmed": False,
                      "basis": "课程说明地址所属子网", "data_source": "document"})
    discoveries = latest_discoveries(database)
    observed_addresses = []
    for discovery in discoveries:
        # A failed new attempt must not silently resurrect an older successful table.
        if discovery["query_status"] != "ok":
            continue
        addresses = database.rows("SELECT * FROM interface_addresses WHERE discovery_id=?",
                                  (discovery["discovery_id"],))
        observed_addresses.extend(addresses)
        for item in addresses:
            net = ipaddress.IPv4Network(item["network"])
            if net.is_loopback or net.prefixlen == 0 or ipaddress.IPv4Address(item["address"]).is_unspecified:
                continue
            node_id = networks.setdefault(item["network"], f"network-{item['network']}")
            edges.append({"edge_id": f"address-{item['address_id']}", "source": item["device_id"],
                          "target": node_id, "kind": "ip_subnet", "confirmed": True,
                          "physical_link_confirmed": False, "interface_id": item["interface_id"],
                          "address": item["address"], "mask": item["mask"],
                          "sampled_at": item["sampled_at"], "address_id": item["address_id"],
                          "discovery_id": item["discovery_id"], "basis": "SNMP ipAddrTable 的接口/IP/掩码",
                          "data_source": item["data_source"]})
    observed_pairs = {(e["source"], e["target"]) for e in edges if e["kind"] == "ip_subnet"}
    edges = [e for e in edges if e["kind"] != "document" or (e["source"], e["target"]) not in observed_pairs]
    for network, node_id in networks.items():
        related = [e for e in edges if e["target"] == node_id and e["kind"] == "ip_subnet"]
        sources = {e["data_source"] for e in related}
        nodes.append({"node_id": node_id, "label": network, "role": "subnet",
                      "data_source": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "document"})
    # Multiple managed addresses can be aliases of one SNMP agent. Show evidence,
    # never merge identifiers automatically merely because sysName matches.
    groups = {}
    for d in discoveries:
        ips = frozenset(x["address"] for x in observed_addresses if x["discovery_id"] == d["discovery_id"])
        if d["query_status"] == "ok" and d["sys_name"] and ips:
            groups.setdefault((d["sys_name"], d["sys_object_id"], ips), []).append(d["device_id"])
    candidates = [{"device_ids": ids, "basis": "相同 sysName/sysObjectID 和完整 IP 地址集合",
                   "confirmed": False} for ids in groups.values() if len(ids) > 1]
    return {"nodes": nodes, "edges": edges, "discoveries": discoveries,
            "identity_candidates": candidates,
            "observed_relation_count": sum(e["kind"] == "ip_subnet" for e in edges),
            "note": "实测关系表示采样时的接口子网归属；文档关系单独标记。均不证明物理直连，候选地址不自动合并。"}


def traffic_attribution(database: Database, interface_id: str, start: str, end: str) -> dict:
    interface = database.row("SELECT device_id FROM interfaces WHERE interface_id=?", (interface_id,))
    device_id = interface["device_id"] if interface else None
    result = {"device_id": device_id, "interface_id": interface_id,
              "granularity": "interface", "exact_host_pair": False, "networks": [],
              "address_ids": [], "mapping_status": "unavailable",
              "in_direction": "相邻网络 → 观测设备", "out_direction": "观测设备 → 相邻网络",
              "note": "接口总量包含经过该口的所有业务，不能分配给某个主机对。"}
    if not device_id:
        return result
    previous = database.row("""SELECT * FROM network_discoveries WHERE device_id=? AND sampled_at<=?
        ORDER BY sampled_at DESC,discovery_id DESC LIMIT 1""", (device_id, start))
    if not previous or previous["query_status"] != "ok":
        return result
    from datetime import datetime
    age = (datetime.fromisoformat(start.replace('Z','+00:00')) -
           datetime.fromisoformat(previous["sampled_at"].replace('Z','+00:00'))).total_seconds()
    if age > 300:
        result["mapping_status"] = "stale"
        result["note"] += " 窗口起点前的映射已超过 5 分钟，未用于本次归属。"
        return result
    initial = database.rows("SELECT * FROM interface_addresses WHERE discovery_id=? AND interface_id=?",
                            (previous["discovery_id"], interface_id))
    if not initial:
        return result
    identity = {(x["address"], x["mask"]) for x in initial}
    subsequent = database.rows("""SELECT * FROM network_discoveries WHERE device_id=?
        AND sampled_at>? AND sampled_at<=? ORDER BY sampled_at,discovery_id""", (device_id, start, end))
    for discovery in subsequent:
        rows = database.rows("SELECT address,mask FROM interface_addresses WHERE discovery_id=? AND interface_id=?",
                             (discovery["discovery_id"], interface_id))
        if discovery["query_status"] != "ok" or {(x["address"], x["mask"]) for x in rows} != identity:
            result["mapping_status"] = "changed_or_incomplete"
            result["note"] += " 统计窗口内地址映射变化或读取不完整，未给整段流量标注单一子网。"
            return result
    result.update({"granularity": "interface_subnet", "mapping_status": "observed",
                   "networks": sorted({x["network"] for x in initial}),
                   "address_ids": [x["address_id"] for x in initial],
                   "mapping_sampled_at": previous["sampled_at"],
                   "mapping_data_source": previous["data_source"]})
    result["note"] += " 子网映射依据窗口起点及窗口内已观测配置，未采样期间的变化无法排除。"
    return result
