import asyncio
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.db import Database, decode
from app.main import create_app
from app.modules.diagnostics import diagnose
from app.modules.discovery import store_inventory
from app.modules.fault_config import record_ping
from app.modules.security import record_service_check, record_snmp_access_risk
from app.modules.iperf_tasks import IperfTaskManager, TaskUnavailable


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path/'diagnostics.sqlite3')
    database.initialize()
    return database


def test_snmp_default_read_risk_is_deduplicated_and_other_credentials_do_not_prove_revocation(database):
    stamp='2026-10-17T10:00:00Z'
    risk_id=record_snmp_access_risk(database, 'r-0-1', stamp, True)
    assert risk_id is not None
    assert record_snmp_access_risk(database, 'r-0-1', stamp, True)==risk_id
    risk=database.row('SELECT * FROM risks WHERE risk_id=?',(risk_id,))
    evidence=decode(risk['evidence'])
    assert evidence['common_default_credential'] and not evidence['vulnerability_confirmed']
    assert not evidence['other_network_exposure_tested']
    assert 'public' not in risk['evidence'] and 'private' not in risk['evidence']
    with database.connect() as db:
        db.execute("UPDATE risks SET state='acknowledged' WHERE risk_id=?",(risk_id,))
    record_snmp_access_risk(database,'r-0-1','2026-10-17T10:00:10Z',True)
    assert database.row('SELECT state FROM risks WHERE risk_id=?',(risk_id,))['state']=='acknowledged'
    record_snmp_access_risk(database,'r-0-1','2026-10-17T10:00:20Z',False)
    assert database.row('SELECT state FROM risks WHERE risk_id=?',(risk_id,))['state']=='acknowledged'


def test_iperf_limits_targets_to_provisioned_ssh_endpoints(database):
    settings=replace(Settings(),enable_live=True,enable_iperf3=True,iperf_allowed_devices=('host-1','host-2'))
    with pytest.raises(TaskUnavailable,match='not configured'):
        IperfTaskManager(database,settings).create('host-1','host-3')
    assert database.row('SELECT COUNT(*) AS n FROM tasks')['n']==0


def test_telnet_port_risk_is_a_hint_and_unknown_does_not_resolve(database):
    with database.connect() as db:
        db.execute("INSERT INTO service_baselines(device_id,port,allowed,note) VALUES ('host-1',23,1,'test')")
    record_service_check(database,'host-1',23,'open')
    risk=database.row("SELECT * FROM risks WHERE rule_id='possible_telnet_service_tcp_23'")
    assert risk['state']=='open' and not decode(risk['evidence'])['vulnerability_confirmed']
    record_service_check(database,'host-1',23,'unavailable','timeout')
    assert database.row('SELECT state FROM risks WHERE risk_id=?',(risk['risk_id'],))['state']=='open'
    record_service_check(database,'host-1',23,'closed')
    assert database.row('SELECT state FROM risks WHERE risk_id=?',(risk['risk_id'],))['state']=='resolved'


def test_diagnosis_correlates_only_nearby_same_source_interface_evidence(database):
    stamp='2026-10-17T10:00:00Z'
    with database.connect() as db:
        db.execute("""INSERT INTO interfaces(interface_id,device_id,if_index,admin_status,oper_status,
                      query_status,sampled_at,data_source) VALUES ('r-0-1:2','r-0-1',2,'up','down','ok',?,'live')""",(stamp,))
    store_inventory(database,'r-0-1',stamp,[{'interface_id':'r-0-1:2','if_index':2,
        'address':'192.168.1.254','mask':'255.255.255.0','network':'192.168.1.0/24'}])
    failed={'query_status':'ok','packets_sent':2,'packets_received':0,'rtt_ms':None,'loss_pct':100.0,'error':None}
    record_ping(database,'host-1','192.168.1.1',stamp,failed)
    finding=diagnose(database,'host-1')['items'][0]['findings'][0]
    assert finding['code']=='ping_unreachable' and not finding['root_cause_confirmed']
    assert finding['related_down_interfaces'][0]['interface_id']=='r-0-1:2'
    # Measurements of another origin are never presented as correlated live evidence.
    record_ping(database,'host-1','192.168.1.1','2026-10-17T10:00:01Z',failed,data_source='sample')
    finding=diagnose(database,'host-1')['items'][0]['findings'][0]
    assert finding['related_down_interfaces']==[]
    unknown={**failed,'query_status':'timeout','loss_pct':None,'error':'timeout'}
    record_ping(database,'host-1','192.168.1.1','2026-10-17T10:00:02Z',unknown)
    assert diagnose(database,'host-1')['items'][0]['findings'][0]['code']=='measurement_unknown'


def test_diagnostics_api_marks_old_samples_and_handles_empty_unknown_devices(database):
    record_ping(database,'host-1','192.168.1.1','2020-01-01T00:00:00Z',
        {'query_status':'ok','packets_sent':2,'packets_received':2,'rtt_ms':1.0,'loss_pct':0,'error':None})
    async def verify():
        app=create_app(replace(Settings(),db_path=database.path))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                result=(await client.get('/api/v1/diagnostics',params={'device_id':'host-1'})).json()['data']['items'][0]
                assert result['stale'] and result['findings'][0]['code']=='ping_replied'
                empty=(await client.get('/api/v1/diagnostics',params={'device_id':'dev'})).json()['data']['items'][0]
                assert empty['stale'] and empty['findings']==[]
                assert (await client.get('/api/v1/diagnostics',params={'device_id':'missing'})).status_code==404
    asyncio.run(verify())
