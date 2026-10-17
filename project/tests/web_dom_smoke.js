const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class ElementStub {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.handlers = {};
    this._text = '';
    this.value = '';
    this.style = {setProperty() {}};
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; this._text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
}

function countNodes(root, tag) {
  let count = 0;
  const visit = node => {
    if (node.tag === tag) count += 1;
    for (const child of node.children || []) visit(child);
  };
  visit(root);
  return count;
}

function findNodes(root, tag) {
  const found = [];
  const visit = node => {
    if (node.tag === tag) found.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(root);
  return found;
}

async function main() {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new ElementStub(id));
      return elements.get(id);
    },
    createElement(tag) { return new ElementStub(tag); },
    createElementNS(_namespace, tag) { return new ElementStub(tag); },
    createTextNode(text) { const node = new ElementStub('#text'); node.textContent = text; return node; },
  };
  const devices = [
    {device_id:'r-0-1', address:'192.168.0.1', role:'router-candidate', data_source:'document'},
    {device_id:'host-1', address:'192.168.1.1', role:'test-host', data_source:'document'},
    {device_id:'sample-router', address:'192.0.2.10', role:'sample', data_source:'sample'},
    {device_id:'sample-host-1', address:'192.0.2.11', role:'sample', data_source:'sample'},
  ];
  const pings = [0, 1, 2, 3].map(index => ({
    sample_id:index+1, device_id:'host-1', target:'192.168.1.1',
    sampled_at:`2026-10-13T10:00:${String(index*10).padStart(2, '0')}Z`,
    query_status:index === 2 ? 'timeout' : 'ok', rtt_ms:index === 2 ? 999 : index+1,
    loss_pct:index === 2 ? 0 : index*10, data_source:'sample',
  }));
  const metrics = [0, 1, 2, 3].map(index => ({
    metric_id:index+1, interface_id:'r-0-1:2',
    sampled_at:`2026-10-13T10:00:${String(index*10).padStart(2, '0')}Z`,
    in_rate_bps:index === 2 ? 999 : 100+index*10,
    out_rate_bps:index === 2 ? 999 : 200+index*10,
    in_util_pct:null, out_util_pct:null, valid:index !== 2,
    invalid_reason:index === 2 ? 'sample_gap' : null, data_source:'sample',
  }));
  const taskId = 'task-live-12345678';
  const state = {failPing:false, failMetrics:false, failConfigDiff:false, failTaskCancel:true, failCollect:false,
    failHealth:false, failAuth:false,
    health:{live_enabled:true,service_probe_enabled:true,iperf3_enabled:true},
    baselineItems:[{device_id:'host-1',port:22,allowed:true,note:'course SSH service',data_source:'configured'}],
    failRoutes:false, deferNextRoutes:false, routeDeferredStarted:false, routeGate:null, trafficRequest:null,
    configDiffRequests:[], faultFilters:[], faultAcks:[], securityRequests:[], performanceRequests:[], taskCancelRequests:[],
    collectRequests:[],
    taskItems:[{task_id:taskId,type:'iperf3',task_state:'running',client_device_id:'host-1',server_device_id:'host-2',
      result:null,finished_at:null,error:null,data_source:'live'}], faultAcked:false};
  const faultEvent = {event_id:42,device_id:'host-1',type:'ping_unreachable',state:'active',
    first_seen:'2026-10-13T10:00:00Z',last_seen:'2026-10-13T10:00:20Z',recovered_at:null,
    evidence_sample_ids:[1,2,3],data_source:'sample'};
  const reply = data => ({ok:true, status:200, json:async () => ({status:'ok', data})});
  const empty = {items:[], total:0};
  const fetch = async (url, options = {}) => {
    const path = new URL(url, 'http://local').pathname;
    if (path.endsWith('/health')) {
      if (state.failHealth) throw new Error('fixture health failure');
      return reply(state.health);
    }
    if (options.method === 'POST' && state.failAuth) {
      return {ok:false, status:401, json:async () => ({status:'error', error:{message:'invalid admin token'}})};
    }
    if (path.endsWith('/overview')) return reply({counts:{devices:2, interfaces:1, active_events:0, open_risks:0}});
    if (path.endsWith('/devices')) return reply({items:devices});
    if (path.endsWith('/interfaces')) return reply({items:[{
      interface_id:'r-0-1:2', device_id:'r-0-1', description:'eth0', admin_status:'up', oper_status:'up',
      reference_speed_bps:1000000, sampled_at:'2026-10-13T10:00:30Z', query_status:'ok', data_source:'sample',
    }]});
    if (path.endsWith('/routes')) {
      if (state.deferNextRoutes) {
        state.deferNextRoutes = false;
        state.routeDeferredStarted = true;
        return state.routeGate;
      }
      if (state.failRoutes) return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture route failure'}})};
      return reply({items:[]});
    }
    if (path.endsWith('/topology')) return reply({nodes:[
      {node_id:'r-0-1',address:'192.168.0.1',role:'router-candidate'},
      {node_id:'subnet-1',label:'192.168.1.0/24',role:'subnet'}],
      edges:[{source:'r-0-1',target:'subnet-1',kind:'ip_subnet',interface_id:'r-0-1:2',address:'192.168.1.254',
        mask:'255.255.255.0',basis:'SNMP address table',sampled_at:'2026-10-13T10:00:00Z',data_source:'sample'}],
      identity_candidates:[],note:'sample'});
    if (path.endsWith('/interface-addresses')) return reply({items:[{interface_id:'r-0-1:2',
      address:'192.168.1.254',mask:'255.255.255.0',network:'192.168.1.0/24',sampled_at:'2026-10-13T10:00:00Z',data_source:'sample'}]});
    if (path.endsWith('/diagnostics')) return reply({items:[{device_id:'host-1',stale:true,data_source:'sample',
      findings:[{summary:'本次收到 ICMP 响应',next_steps:['检查历史与来源'],data_source:'sample'}]}]});
    if (path.endsWith('/traffic/pairs')) return reply({items:[{client_device_id:'host-1',server_device_id:'host-2',
      received_bytes:'2300000',task_count:1,task_ids:['sample-task'],data_source:'sample'}],note:'仅统计成功 iperf3 测试'});
    if (path.endsWith('/config-snapshots')) {
      const deviceId = new URL(url, 'http://local').searchParams.get('device_id');
      const items = [
        {snapshot_id:1, device_id:'r-0-1', sampled_at:'2026-10-13T10:00:00Z',
          content:{interfaces:{'r-0-1:2':{description:'eth0-old'}}, routes:{}}, data_source:'sample'},
        {snapshot_id:2, device_id:'r-0-1', sampled_at:'2026-10-13T10:01:00Z',
          content:{interfaces:{'r-0-1:2':{description:'eth0'}}, routes:{}}, data_source:'sample'},
        {snapshot_id:3, device_id:'sample-router', sampled_at:'2026-10-13T10:02:00Z',
          content:{interfaces:{'sample-router:1':{description:'sample-old'}}, routes:{}}, data_source:'sample'},
        {snapshot_id:4, device_id:'sample-router', sampled_at:'2026-10-13T10:03:00Z',
          content:{interfaces:{'sample-router:1':{description:'sample-new'}},
            routes:{'sample-route-1':{destination:'192.0.2.0'}}}, data_source:'sample'},
      ];
      return reply({items:deviceId ? items.filter(item => item.device_id === deviceId) : items});
    }
    if (path.endsWith('/config-diff')) {
      const query = new URL(url, 'http://local').searchParams;
      const request = {before_id:query.get('before_id'), after_id:query.get('after_id')};
      state.configDiffRequests.push(request);
      if (state.failConfigDiff) {
        return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture diff failure'}})};
      }
      const isSample = request.before_id === '3';
      const changes = isSample ? [
        {kind:'interfaces', id:'sample-router:1', change:'changed',
          before:{description:'sample-old'}, after:{description:'sample-new'}},
        {kind:'routes', id:'sample-route-1', change:'added', before:null, after:{destination:'192.0.2.0'}},
      ] : [{kind:'interfaces', id:'r-0-1:2', change:'changed',
        before:{description:'eth0-old'}, after:{description:'eth0'}}];
      return reply({device_id:isSample ? 'sample-router' : 'r-0-1', before_id:Number(request.before_id), after_id:Number(request.after_id),
        data_source:'sample', changes});
    }
    if (path.endsWith('/service-baselines')) {
      if (options.method === 'POST') {
        const request = {path, method:options.method, token:options.headers?.['X-Admin-Token'], body:JSON.parse(options.body)};
        state.securityRequests.push(request);
        state.baselineItems = state.baselineItems.filter(item => item.device_id !== request.body.device_id || item.port !== request.body.port);
        state.baselineItems.push({...request.body,data_source:'configured'});
        return reply({...request.body, data_source:'configured'});
      }
      return reply({items:state.baselineItems,total:state.baselineItems.length});
    }
    if (path.endsWith('/security/probe')) {
      const request = {path, method:options.method, token:options.headers?.['X-Admin-Token'], body:JSON.parse(options.body)};
      state.securityRequests.push(request);
      return reply({state:'unavailable', error:'fixture timeout'});
    }
    if (path.endsWith('/tasks/iperf3')) {
      const request = {path, method:options.method, token:options.headers?.['X-Admin-Token'], body:JSON.parse(options.body)};
      state.performanceRequests.push(request);
      return reply({task_id:'task-sample', task_state:'queued'});
    }
    if (path.endsWith('/collect')) {
      state.collectRequests.push({path, method:options.method, token:options.headers?.['X-Admin-Token'], body:options.body});
      if (state.failCollect) {
        state.failCollect = false;
        return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture collect failure'}})};
      }
      return reply({run_id:7, state:'completed'});
    }
    if (path.startsWith('/api/v1/tasks/') && path.endsWith('/cancel')) {
      state.taskCancelRequests.push({path, method:options.method, token:options.headers?.['X-Admin-Token']});
      if (state.failTaskCancel) {
        state.failTaskCancel = false;
        return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture cancel failure'}})};
      }
      state.taskItems = state.taskItems.map(task => task.task_id === taskId ? {...task, task_state:'cancelled'} : task);
      return reply(state.taskItems[0]);
    }
    if (path === '/api/v1/tasks') return reply({items:state.taskItems, total:state.taskItems.length});
    if (path.startsWith('/api/v1/risks/') && path.endsWith('/acknowledge')) {
      state.securityRequests.push({path, method:options.method, token:options.headers?.['X-Admin-Token']});
      return reply({risk_id:5, state:'acknowledged'});
    }
    if (path.endsWith('/service-checks')) return reply({items:[
      {check_id:2, device_id:'host-1', port:22, observed_at:'2026-10-13T10:01:00Z',
        state:'open', error:null, data_source:'live'},
      {check_id:1, device_id:'host-1', port:22, observed_at:'2026-10-13T10:00:00Z',
        state:'unavailable', error:'TCP connect did not confirm port state', data_source:'sample'},
    ]});
    if (path.endsWith('/ping-samples')) {
      const query = new URL(url, 'http://local').searchParams;
      state.faultFilters.push({kind:'ping', device_id:query.get('device_id')});
      if (state.failPing && query.has('device_id')) {
        return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture failure'}})};
      }
      return reply({items:pings});
    }
    if (path.startsWith('/api/v1/events/') && path.endsWith('/acknowledge')) {
      state.faultAcks.push({path, method:options.method, token:options.headers?.['X-Admin-Token']});
      state.faultAcked = true;
      return reply({...faultEvent, acknowledged_at:'2026-10-13T10:01:00Z'});
    }
    if (path.endsWith('/events')) {
      const query = new URL(url, 'http://local').searchParams;
      state.faultFilters.push({kind:'events', device_id:query.get('device_id')});
      return reply({items:[{...faultEvent,
        acknowledged_at:state.faultAcked ? '2026-10-13T10:01:00Z' : null}], total:1});
    }
    if (path.endsWith('/metrics')) {
      if (state.failMetrics) return {ok:false, status:503, json:async () => ({status:'error', error:{message:'fixture failure'}})};
      return reply({items:metrics});
    }
    if (path.endsWith('/traffic')) {
      const query = new URL(url, 'http://local').searchParams;
      state.trafficRequest = Object.fromEntries(query.entries());
      return reply({interface_id:'r-0-1:2', observation_point:'r-0-1:2', from:query.get('from'), to:query.get('to'),
        direction:query.get('direction'), in_bytes:null, out_bytes:'400', coverage_pct:100, complete:true,
        invalid_intervals:[], data_source:'sample', note:'sample fixture'});
    }
    return reply(empty);
  };

  const context = {document, Node:ElementStub, fetch, URL, URLSearchParams};
  vm.runInNewContext(fs.readFileSync('app/web/app.js', 'utf8'), context);
  await new Promise(resolve => setTimeout(resolve, 20));

  const success = {
    pingLines:countNodes(elements.get('ping-rtt-chart'), 'polyline') + countNodes(elements.get('ping-loss-chart'), 'polyline'),
    pingPoints:countNodes(elements.get('ping-rtt-chart'), 'circle') + countNodes(elements.get('ping-loss-chart'), 'circle'),
    rateLines:countNodes(elements.get('metric-rate-chart'), 'polyline'),
    ratePoints:countNodes(elements.get('metric-rate-chart'), 'circle'),
  };
  assert.deepEqual(success, {pingLines:2, pingPoints:6, rateLines:2, ratePoints:6});
  assert.match(elements.get('metric-trend-summary').textContent, /无效 1 条/);
  assert.match(elements.get('ping-trend-summary').textContent, /来源 sample/);
  assert.ok(elements.get('ping-trend-device').children.some(option => option.value === 'sample-host-1'));
  assert.ok(!elements.get('ping-trend-device').children.some(option => option.value === 'sample-router'));
  assert.match(elements.get('baselines').textContent, /host-1.*22.*是.*course SSH service.*configured/);
  assert.match(elements.get('service-checks').textContent, /开放/);
  assert.match(elements.get('service-checks').textContent, /live/);
  assert.match(elements.get('service-checks').textContent, /未知（未能确认）/);
  assert.match(elements.get('service-checks').textContent, /TCP connect did not confirm port state/);
  assert.match(elements.get('interface-addresses').textContent, /192.168.1.254.*255.255.255.0.*sample/);
  assert.equal(countNodes(elements.get('topology-diagram'),'svg'),1);
  assert.match(elements.get('topology-summary').textContent,/1 条采样子网关系/);
  assert.match(elements.get('diagnostics').textContent,/历史样本.*ICMP/);

  const beforeSnapshot = document.getElementById('config-diff-before');
  const afterSnapshot = document.getElementById('config-diff-after');
  const diffSubmit = document.getElementById('config-diff-submit');
  document.getElementById('config-diff-device').value = 'r-0-1';
  assert.equal(beforeSnapshot.disabled, false);
  assert.equal(afterSnapshot.disabled, false);
  assert.equal(diffSubmit.disabled, false);
  await elements.get('config-diff-form').handlers.submit({preventDefault() {}});
  assert.deepEqual(state.configDiffRequests, [{before_id:'1', after_id:'2'}]);
  assert.match(elements.get('config-diff-summary').textContent, /r-0-1：快照 #1 → #2，1 处变化；来源 sample/);
  assert.match(elements.get('config-diff-changes').textContent, /接口[\s\S]*变化[\s\S]*eth0-old[\s\S]*eth0/);

  const configDevice = elements.get('config-diff-device');
  assert.ok(configDevice.children.some(option => option.value === 'sample-router'));
  configDevice.value = 'sample-router';
  await configDevice.handlers.change({target:{value:'sample-router'}});
  assert.equal(elements.get('config-diff-submit').disabled, false);
  await elements.get('config-diff-form').handlers.submit({preventDefault() {}});
  assert.deepEqual(state.configDiffRequests[1], {before_id:'3', after_id:'4'});
  assert.match(elements.get('config-diff-summary').textContent, /sample-router：快照 #3 → #4，2 处变化；来源 sample/);
  assert.match(elements.get('config-diff-changes').textContent, /接口[\s\S]*路由/);

  configDevice.value = 'r-0-1';
  await configDevice.handlers.change({target:{value:'r-0-1'}});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(elements.get('config-diff-submit').disabled, false);

  beforeSnapshot.value = '2';
  elements.get('config-diff-before').handlers.change({target:{value:'2'}});
  assert.equal(beforeSnapshot.value, '1');
  assert.equal(afterSnapshot.value, '2');
  assert.equal(diffSubmit.disabled, false);
  afterSnapshot.value = '2';
  beforeSnapshot.value = '2';
  await elements.get('config-diff-form').handlers.submit({preventDefault() {}});
  assert.match(elements.get('config-diff-note').textContent, /请选择两个不同的快照/);
  assert.equal(state.configDiffRequests.length, 2);

  beforeSnapshot.value = '1';
  elements.get('config-diff-before').handlers.change({target:{value:'1'}});
  afterSnapshot.value = '2';
  elements.get('config-diff-after').handlers.change({target:{value:'2'}});
  state.failConfigDiff = true;
  await elements.get('config-diff-form').handlers.submit({preventDefault() {}});
  assert.match(elements.get('config-diff-summary').textContent, /比较失败：fixture diff failure/);
  assert.match(elements.get('config-diff-changes').textContent, /暂无记录/);
  assert.equal(diffSubmit.disabled, false);

  document.getElementById('fault-device').value = 'host-1';
  elements.get('fault-device').handlers.change({target:{value:'host-1'}});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(state.faultFilters.some(item => item.kind === 'events' && item.device_id === 'host-1'));
  assert.ok(state.faultFilters.some(item => item.kind === 'ping' && item.device_id === 'host-1'));
  const initialAckButton = findNodes(elements.get('events'), 'button').find(node => node.textContent === '确认告警');
  assert.ok(initialAckButton);
  await initialAckButton.handlers.click();
  assert.equal(state.faultAcks.length, 0);
  assert.match(elements.get('fault-filter-summary').textContent, /页面底部.*管理令牌/);
  assert.equal(initialAckButton.disabled, false);
  document.getElementById('admin-token').value = 'test-token';
  await initialAckButton.handlers.click();
  assert.deepEqual(state.faultAcks, [{path:'/api/v1/events/42/acknowledge',method:'POST',token:'test-token'}]);
  const acknowledgedButton = findNodes(elements.get('events'), 'button').find(node => node.textContent === '已确认');
  assert.ok(acknowledgedButton && acknowledgedButton.disabled);

  document.getElementById('traffic-interface').value = 'r-0-1:2';
  document.getElementById('traffic-direction').value = 'out';
  document.getElementById('traffic-from').value = '2026-10-13T18:00';
  document.getElementById('traffic-to').value = '2026-10-13T18:00:10';
  await elements.get('traffic-form').handlers.submit({preventDefault() {}});
  assert.equal(state.trafficRequest.direction, 'out');
  assert.match(elements.get('traffic-csv').href, /direction=out/);
  assert.match(elements.get('traffic-result').textContent, /观测接口：r-0-1:2[\s\S]*方向：仅出方向[\s\S]*400 byte/);
  assert.match(elements.get('pair-traffic').textContent,/host-1 → host-2.*2300000.*sample/);
  assert.match(elements.get('pair-traffic-csv').href,/traffic\/pairs\/export.csv/);

  document.getElementById('baseline-device').value = 'host-1';
  document.getElementById('baseline-port').value = '22';
  elements.get('baseline-port').handlers.change();
  assert.equal(elements.get('baseline-allowed').value, 'true');
  assert.equal(elements.get('baseline-note').value, 'course SSH service');
  assert.match(elements.get('baseline-current').textContent, /已载入 host-1:22 的基线/);
  document.getElementById('baseline-port').value = '5201';
  elements.get('baseline-port').handlers.change();
  assert.equal(elements.get('baseline-allowed').value, 'false');
  assert.equal(elements.get('baseline-note').value, '');
  document.getElementById('baseline-note').value = 'planned test service';
  document.getElementById('baseline-allowed').value = 'false';
  await elements.get('baseline-form').handlers.submit({preventDefault() {}});
  assert.deepEqual(state.securityRequests[0], {path:'/api/v1/service-baselines', method:'POST', token:'test-token',
    body:{device_id:'host-1', port:5201, allowed:false, note:'planned test service'}});
  assert.match(elements.get('security-result').textContent, /基线已保存/);
  assert.match(elements.get('baseline-current').textContent, /已载入 host-1:5201 的基线/);
  assert.equal(elements.get('baseline-note').value, 'planned test service');

  await elements.get('probe-service').handlers.click();
  assert.deepEqual(state.securityRequests[1], {path:'/api/v1/security/probe', method:'POST', token:'test-token',
    body:{device_id:'host-1', port:5201}});
  assert.match(elements.get('security-result').textContent, /未知（连接未能确认端口状态）/);
  assert.match(elements.get('security-result').textContent, /不可达不表示端口已关闭/);

  document.getElementById('risk-id').value = '0';
  await elements.get('ack-risk').handlers.click();
  assert.match(elements.get('security-result').textContent, /请填写有效的风险编号/);
  assert.equal(state.securityRequests.length, 2);
  document.getElementById('risk-id').value = '5';
  await elements.get('ack-risk').handlers.click();
  assert.deepEqual(state.securityRequests[2], {path:'/api/v1/risks/5/acknowledge', method:'POST', token:'test-token'});
  assert.match(elements.get('security-result').textContent, /风险 #5：acknowledged/);

  document.getElementById('iperf-client').value = 'host-1';
  document.getElementById('iperf-server').value = 'host-2';
  await elements.get('iperf-form').handlers.submit({preventDefault() {}});
  assert.deepEqual(state.performanceRequests, [{path:'/api/v1/tasks/iperf3', method:'POST', token:'test-token',
    body:{client_device_id:'host-1', server_device_id:'host-2', duration_s:2, target_bps:10000000}}]);
  assert.match(elements.get('iperf-result').textContent, /任务 task-sample：queued/);

  const cancelTaskButton = findNodes(elements.get('tasks'), 'button').find(node => node.textContent === '取消任务');
  assert.ok(cancelTaskButton);
  await cancelTaskButton.handlers.click();
  assert.match(elements.get('iperf-result').textContent, /取消任务失败：fixture cancel failure/);
  assert.equal(cancelTaskButton.disabled, false);
  await cancelTaskButton.handlers.click();
  assert.deepEqual(state.taskCancelRequests, [
    {path:`/api/v1/tasks/${taskId}/cancel`, method:'POST', token:'test-token'},
    {path:`/api/v1/tasks/${taskId}/cancel`, method:'POST', token:'test-token'},
  ]);
  assert.match(elements.get('iperf-result').textContent, /任务 task-liv 返回状态：cancelled/);
  assert.equal(findNodes(elements.get('tasks'), 'button').filter(node => node.textContent === '取消任务').length, 0);

  const collectButton = elements.get('collect');
  state.failCollect = true;
  await collectButton.handlers.click();
  assert.deepEqual(state.collectRequests[0], {path:'/api/v1/collect', method:'POST', token:'test-token', body:'{}'});
  assert.match(elements.get('operation-result').textContent, /采集失败：fixture collect failure/);
  assert.equal(collectButton.disabled, false);
  await collectButton.handlers.click();
  assert.deepEqual(state.collectRequests[1], {path:'/api/v1/collect', method:'POST', token:'test-token', body:'{}'});
  assert.match(elements.get('operation-result').textContent, /采集 #7：completed/);
  assert.equal(collectButton.disabled, false);

  state.failPing = true;
  state.failMetrics = true;
  elements.get('ping-trend-device').handlers.change({target:{value:'host-1'}});
  elements.get('metric-trend-interface').handlers.change({target:{value:'r-0-1:2'}});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.match(elements.get('ping-trend-summary').textContent, /ping 历史读取失败/);
  assert.match(elements.get('metric-trend-summary').textContent, /接口速率历史读取失败/);

  state.failRoutes = true;
  await elements.get('refresh').handlers.click();
  assert.match(elements.get('routes').textContent, /读取失败：fixture route failure/);
  assert.match(elements.get('devices').textContent, /192\.168\.0\.1/);
  assert.match(elements.get('interfaces').textContent, /r-0-1:2/);
  assert.match(elements.get('notice').textContent, /部分数据读取失败：路由/);
  assert.equal(elements.get('refresh').disabled, false);

  let resolveDelayedRoutes;
  state.failRoutes = false;
  state.deferNextRoutes = true;
  state.routeGate = new Promise(resolve => { resolveDelayedRoutes = resolve; });
  const delayedRefresh = elements.get('refresh').handlers.click();
  assert.equal(state.routeDeferredStarted, true);
  state.failRoutes = true;
  await elements.get('refresh').handlers.click();
  assert.match(elements.get('routes').textContent, /读取失败：fixture route failure/);
  resolveDelayedRoutes(reply({items:[{device_id:'stale', destination:'198.51.100.0', mask:'255.255.255.0',
    next_hop:'198.51.100.1', interface_id:'r-0-1:2', sampled_at:'2026-10-13T10:02:00Z', data_source:'sample'}]}));
  await delayedRefresh;
  assert.match(elements.get('routes').textContent, /读取失败：fixture route failure/);
  assert.match(elements.get('notice').textContent, /部分数据读取失败：路由/);
  assert.equal(elements.get('refresh').disabled, false);
  state.failRoutes = false;
  state.failAuth = true;
  await elements.get('baseline-form').handlers.submit({preventDefault() {}});
  assert.match(elements.get('security-result').textContent, /管理令牌不正确.*NMS_API_TOKEN/);
  state.failAuth = false;

  // Each feature follows its own server-side switch.
  state.health = {live_enabled:true,service_probe_enabled:false,iperf3_enabled:false};
  await elements.get('refresh').handlers.click();
  assert.equal(elements.get('collect').disabled, false);
  assert.equal(elements.get('iperf-start').disabled, true);
  assert.equal(elements.get('probe-service').disabled, true);

  // Local mode must explain and block real network actions, including form submits.
  state.health = {live_enabled:false,service_probe_enabled:false,iperf3_enabled:false};
  await elements.get('refresh').handlers.click();
  assert.equal(elements.get('collect').disabled, true);
  assert.match(elements.get('iperf-availability').textContent, /未启用.*iperf3.*预置样例/);
  const beforeBlocked = [state.collectRequests.length, state.performanceRequests.length, state.securityRequests.length];
  await elements.get('collect').handlers.click();
  await elements.get('iperf-form').handlers.submit({preventDefault() {}});
  await elements.get('probe-service').handlers.click();
  assert.deepEqual([state.collectRequests.length, state.performanceRequests.length, state.securityRequests.length], beforeBlocked);
  assert.match(elements.get('iperf-result').textContent, /当前未启用/);

  state.failHealth = true;
  await elements.get('refresh').handlers.click();
  assert.equal(elements.get('iperf-start').disabled, true);
  assert.equal(elements.get('collect').disabled, true);
  assert.equal(elements.get('probe-service').disabled, true);
  assert.match(elements.get('iperf-availability').textContent, /无法确认.*刷新数据/);
  console.log('前端 DOM 冒烟通过：管理交互、功能表单、分区错误处理与并发刷新保护均正确。');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
