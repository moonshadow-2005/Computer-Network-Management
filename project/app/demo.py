"""Populate a separate, explicitly labelled demonstration database.

Run ``python3 -m app.demo``. This never writes to the live database path.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import ROOT
from app.core.db import CURRENT_SCHEMA_VERSION, Database, encode


# Samples are reproducible and read-only through the demo launcher. Keep one
# file per schema version instead of migrating old samples on filesystems that
# cannot enforce the permissions required for migration backups (e.g. DrvFS).
# Previous sample databases are left intact.
DEMO_DB = ROOT / "data" / f"demo-v{CURRENT_SCHEMA_VERSION}.sqlite3"
LOCAL_TEST_DB = ROOT / "data" / f"local-test-v{CURRENT_SCHEMA_VERSION}.sqlite3"


def load_demo(db_path: Path | None = None) -> bool:
    database = Database(db_path if db_path is not None else DEMO_DB)
    database.initialize()
    with database.connect() as db:
        if db.execute("SELECT 1 FROM metadata WHERE key='demo_loaded'").fetchone():
            return False
        for device_id, label, address, role in (
            ("sample-router", "构造示例：路由对象", "192.0.2.10", "sample"),
            ("sample-host-1", "构造示例：测试主机 A", "192.0.2.11", "sample"),
            ("sample-host-2", "构造示例：测试主机 B", "192.0.2.12", "sample"),
        ):
            db.execute(
                """INSERT INTO devices(device_id,label,address,role,identity_status,data_source,note)
                   VALUES (?,?,?,?,?,?,?)""",
                (device_id, label, address, role, "sample", "sample", "构造数据，不是实验网实测"),
            )
        db.execute(
            """INSERT INTO interfaces(interface_id,device_id,if_index,description,admin_status,
               oper_status,reference_speed_bps,query_status,sampled_at,data_source)
               VALUES ('sample-router:1','sample-router',1,'sample-ens3','up','up',100000000,
                       'ok','2026-10-13T09:00:10Z','sample')"""
        )
        db.execute(
            """INSERT INTO routes(route_id,device_id,destination,mask,next_hop,interface_id,
               sampled_at,data_source) VALUES ('sample-route-1','sample-router','192.0.2.0',
               '255.255.255.0','0.0.0.0','sample-router:1','2026-10-13T09:00:10Z','sample')"""
        )
        initial = {"interfaces": {"sample-router:1": {"description": "sample-ens2",
                     "admin_status": "up", "oper_status": "up", "reference_speed_bps": 100000000}},
                   "routes": {}}
        changed = {"interfaces": {"sample-router:1": {"description": "sample-ens3",
                     "admin_status": "up", "oper_status": "up", "reference_speed_bps": 100000000}},
                   "routes": {"sample-route-1": {"destination": "192.0.2.0", "mask": "255.255.255.0",
                                                "next_hop": "0.0.0.0", "interface_id": "sample-router:1"}}}
        for sampled_at, content in (("2026-10-13T08:59:00Z", initial),
                                    ("2026-10-13T09:00:10Z", changed)):
            db.execute("INSERT INTO config_snapshots(device_id,sampled_at,content,data_source) VALUES (?,?,?,'sample')",
                       ("sample-router", sampled_at, encode(content)))
        ping_ids = []
        for second, received, rtt in ((0, 2, 1.5), (1, 0, None), (2, 0, None),
                                      (3, 0, None), (4, 2, 1.2)):
            ping_ids.append(db.execute(
                """INSERT INTO ping_samples(device_id,target,sampled_at,query_status,
                   packets_sent,packets_received,rtt_ms,loss_pct,error,data_source)
                   VALUES ('sample-host-1','192.0.2.11',?,'ok',2,?,?,?,NULL,'sample')""",
                (f"2026-10-13T09:00:0{second}Z", received, rtt,
                 100.0 if received == 0 else 0.0),
            ).lastrowid)
        db.execute(
            """INSERT INTO events(device_id,interface_id,type,state,first_seen,last_seen,
               recovered_at,evidence_sample_ids,data_source)
               VALUES ('sample-host-1',NULL,'ping_unreachable','recovered',
                       '2026-10-13T09:00:03Z','2026-10-13T09:00:04Z',
                       '2026-10-13T09:00:04Z',?,'sample')""",
            (encode(ping_ids[1:]),),
        )
        counter_ids = []
        for sampled_at, inbound, outbound in (
            ("2026-10-13T09:00:00Z", "9007199254740993", "9007199254741993"),
            ("2026-10-13T09:00:10Z", "9007199254741118", "9007199254742243"),
        ):
            counter_ids.append(db.execute(
                """INSERT INTO counter_samples(interface_id,sampled_at,in_octets,out_octets,
                   sys_uptime_ticks,discontinuity_ticks,query_status,invalid_reason,data_source)
                   VALUES ('sample-router:1',?,?,?,'1000','0','ok',NULL,'sample')""",
                (sampled_at, inbound, outbound),
            ).lastrowid)
        db.execute(
            """INSERT INTO metric_samples(interface_id,sampled_at,interval_start,
               in_delta_bytes,out_delta_bytes,in_rate_bps,out_rate_bps,in_util_pct,
               out_util_pct,valid,invalid_reason,source_sample_ids,data_source)
               VALUES ('sample-router:1','2026-10-13T09:00:00Z',NULL,NULL,NULL,NULL,NULL,
                       NULL,NULL,0,'first_or_previous_invalid_sample',?,'sample')""",
            (encode([counter_ids[0]]),),
        )
        db.execute(
            """INSERT INTO metric_samples(interface_id,sampled_at,interval_start,
               in_delta_bytes,out_delta_bytes,in_rate_bps,out_rate_bps,in_util_pct,
               out_util_pct,valid,invalid_reason,source_sample_ids,data_source)
               VALUES ('sample-router:1','2026-10-13T09:00:10Z','2026-10-13T09:00:00Z',
                       '125','250',100,200,0.0001,0.0002,1,NULL,?,'sample')""",
            (encode(counter_ids),),
        )
        db.execute("""INSERT INTO service_baselines(device_id,port,allowed,note,data_source)
                   VALUES ('sample-host-1',5201,0,'样例：不应开放','sample')""")
        check_id = db.execute(
            """INSERT INTO service_checks(device_id,port,observed_at,state,error,data_source)
               VALUES ('sample-host-1',5201,'2026-10-13T09:00:05Z','open',NULL,'sample')"""
        ).lastrowid
        db.execute(
            """INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,
               resolved_at,evidence,data_source)
               VALUES ('sample-host-1','unexpected_open_tcp_5201','warning','open',
                       '2026-10-13T09:00:05Z','2026-10-13T09:00:05Z',NULL,?,'sample')""",
            (encode({"check_id": check_id, "port": 5201, "basis": "构造服务基线"}),),
        )
        db.execute(
            """INSERT INTO tasks(task_id,type,task_state,client_device_id,server_device_id,
               params,created_at,started_at,finished_at,result,error,data_source)
               VALUES ('sample-iperf-1','iperf3','succeeded','sample-host-1','sample-host-2',
               ?,'2026-10-13T09:00:00Z','2026-10-13T09:00:01Z',
               '2026-10-13T09:00:03Z',?,NULL,'sample')""",
            (encode({"duration_s": 2, "target_bps": 10000000, "protocol": "tcp"}),
             encode({"received_bps": 9200000, "received_bytes": 2300000,
                     "note": "构造样例，不是本次实测"})),
        )
        db.execute("INSERT INTO metadata(key,value) VALUES ('demo_loaded','v1')")
    return True


if __name__ == "__main__":
    changed = load_demo()
    print(f"演示数据库：{DEMO_DB}；{'已装载构造样例' if changed else '已存在，未重复装载'}")
