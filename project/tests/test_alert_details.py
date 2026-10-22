from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.db import Database, encode
from app.main import create_app


@pytest.mark.parametrize("kind", ["ping_unreachable", "interface_oper_down"])
def test_alert_details_preserve_and_page_evidence(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("NMS_API_TOKEN", "detail-test-token")
    database = Database(tmp_path / "details.sqlite3")
    database.initialize()
    now = "2026-10-20T08:00:00Z"
    with database.connect() as db:
        for interface in (1, 2):
            db.execute("""INSERT INTO interfaces(interface_id,device_id,if_index,query_status,data_source)
                          VALUES (?,'r-0-1',?,'ok','sample')""", (f"r-0-1:{interface}", interface))
        ids = []
        for _ in range(43):
            if kind == "ping_unreachable":
                sample = db.execute("""INSERT INTO ping_samples(device_id,target,sampled_at,query_status,
                    packets_sent,packets_received,loss_pct,data_source)
                    VALUES ('host-1','192.168.1.1',?,'ok',2,0,100,'sample')""", (now,))
            else:
                sample = db.execute("""INSERT INTO interface_status_samples(interface_id,sampled_at,
                    admin_status,oper_status,query_status,data_source)
                    VALUES ('r-0-1:1',?,'up','down','ok','sample')""", (now,))
            ids.append(sample.lastrowid)
        # Referenced samples belonging to another target must never be shown as this alert's measurements.
        if kind == "ping_unreachable":
            foreign = db.execute("""INSERT INTO ping_samples(device_id,target,sampled_at,query_status,data_source)
                                    VALUES ('host-2','192.168.2.1',?,'ok','sample')""", (now,)).lastrowid
        else:
            foreign = db.execute("""INSERT INTO interface_status_samples(interface_id,sampled_at,query_status,data_source)
                                    VALUES ('r-0-1:2',?,'ok','sample')""", (now,)).lastrowid
        ids += [foreign, 999999]
        event_id = db.execute("""INSERT INTO events(device_id,interface_id,type,state,first_seen,last_seen,
            evidence_sample_ids,data_source) VALUES (?, ?, ?,'active',?,?,?,'sample')""",
            ("host-1" if kind == "ping_unreachable" else "r-0-1",
             None if kind == "ping_unreachable" else "r-0-1:1", kind, now, now, encode(ids))).lastrowid
        evidence = {"summary": "需核实 <script>alert(1)</script>", "vulnerability_confirmed": False,
                    "basis": "原始证据" * 100}
        risk_id = db.execute("""INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,evidence,data_source)
            VALUES ('r-0-1','sample-risk','warning','open',?,?,?,'sample')""",
            (now, now, encode(evidence))).lastrowid

    async def verify():
        app = create_app(replace(Settings(), db_path=database.path, enable_live=False, poll_interval_s=0))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                full = (await client.get("/api/v1/events")).json()["data"]["items"][0]
                assert full["evidence_sample_ids"] == ids  # Existing API callers retain full references.
                preview = (await client.get("/api/v1/events?evidence_limit=3")).json()["data"]["items"][0]
                assert preview["evidence_count"] == 45 and preview["evidence_sample_ids"] == ids[:3]
                assert preview["evidence_truncated"] is True
                detail = (await client.get(f"/api/v1/events/{event_id}")).json()["data"]
                assert detail == preview
                assert detail["state"] == "active" and detail["acknowledged_at"] is None
                pages = [(await client.get(f"/api/v1/events/{event_id}/evidence?offset={offset}")).json()["data"]
                         for offset in (0, 20, 40)]
                assert [len(page["items"]) for page in pages] == [20, 20, 5]
                assert [item["sample_id"] for page in pages for item in page["items"]] == list(reversed(ids))
                assert all(page["total"] == 45 and page["kind"] == kind for page in pages)
                assert all(not row["sample_available"] and "query_status" not in row for row in pages[0]["items"][:2])
                assert pages[0]["items"][2]["query_status"] == "ok"
                assert (await client.get(f"/api/v1/events/{event_id}/evidence?offset=99")).json()["data"]["items"] == []
                for path in (f"/events/{event_id}", f"/risks/{risk_id}"):
                    page = await client.get(path)
                    assert page.status_code == 200 and "/static/alert-detail.js" in page.text
                    assert '<script>alert(1)</script>' not in page.text
                risk = (await client.get(f"/api/v1/risks/{risk_id}")).json()["data"]
                assert risk["evidence"] == evidence and risk["state"] == "open"
                for path in ("/api/v1/events/99999", "/api/v1/events/99999/evidence", "/api/v1/risks/99999",
                             "/events/99999", "/risks/99999"):
                    assert (await client.get(path)).status_code == 404
                for query in ("limit=101", "offset=-1", "limit=0"):
                    assert (await client.get(f"/api/v1/events/{event_id}/evidence?{query}")).status_code == 422
                assert (await client.get("/api/v1/events?evidence_limit=51")).status_code == 422
                assert (await client.post(f"/api/v1/events/{event_id}/acknowledge")).status_code == 401
                assert (await client.get("/static/alert-detail.js")).status_code == 200
                # Merely reading a page or evidence leaves the alert history and lifecycle unchanged.
                after = database.row("SELECT * FROM events WHERE event_id=?", (event_id,))
                assert after["evidence_sample_ids"] == encode(ids) and after["acknowledged_at"] is None

    asyncio.run(verify())
