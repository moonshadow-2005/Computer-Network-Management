const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.handlers = {}; this.value = ''; this._text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync('app/web/alert-detail.js', 'utf8');

function page(path) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const state = {calls:[], failEvidence:false, failDetail:false, failAuth:false, confirmed:false};
  const ok = data => ({ok:true, status:200, json:async () => ({status:'ok',data})});
  const error = (status, message) => ({ok:false, status, json:async () => ({status:'error',error:{message}})});
  const fetch = async (url, options = {}) => {
    state.calls.push({url, options});
    if (options.method === 'POST') {
      if (state.failAuth) return error(401, 'invalid admin token');
      state.confirmed = true;
      return ok({});
    }
    if (url.includes('/evidence?')) {
      if (state.failEvidence) return error(503, 'fixture evidence failure');
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
      return ok({kind:'ping_unreachable', offset, total:45, limit:20,
        items:Array.from({length:Math.min(20,45-offset)}, (_, index) => ({sample_id:45-offset-index,
          sample_available:offset > 0 || index !== 0, note:'原始样本缺失，保留编号',
          sampled_at:'2026-10-20T08:00:00Z',target:'192.168.1.1',packets_sent:2,packets_received:0,
          loss_pct:100,rtt_ms:null,query_status:'ok',data_source:'sample'}))});
    }
    if (state.failDetail) return error(404, 'event not found');
    return ok({device_id:'host-1',type:'ping_unreachable',rule_id:'sample-rule',state:state.confirmed ? 'acknowledged' : 'open',
      first_seen:'2026-10-20T08:00:00Z',last_seen:'2026-10-20T08:00:00Z',data_source:'sample',
      acknowledged_at:state.confirmed ? '2026-10-20T08:01:00Z' : null,evidence_count:45,
      evidence:{summary:'<script>原样显示，不执行</script>',vulnerability_confirmed:false,basis:'样例依据'}});
  };
  vm.runInNewContext(source, {window:{location:{pathname:path}}, document:{getElementById:get,
    createElement:tag => new Element(tag)}, fetch});
  return {get,state};
}

async function main() {
  const {get,state} = page('/events/42');
  await settle();
  assert.equal(get('detail-title').textContent, '故障告警 #42');
  assert.equal(get('back-home').href, '/#fault');
  assert.equal(get('detail-overview').hidden, false);
  assert.match(get('evidence-position').textContent, /1–20 \/ 45/);
  assert.match(get('event-evidence').textContent, /原始样本缺失/);
  assert.equal(get('evidence-prev').disabled, true);
  assert.equal(get('evidence-next').disabled, false);
  await get('evidence-next').handlers.click();
  assert.match(get('evidence-position').textContent, /21–40 \/ 45/);
  await get('evidence-next').handlers.click();
  assert.match(get('evidence-position').textContent, /41–45 \/ 45/);
  assert.equal(get('evidence-next').disabled, true);
  await get('evidence-prev').handlers.click();
  assert.match(get('evidence-position').textContent, /21–40 \/ 45/);
  assert.ok(state.calls.every(call => !call.options.method)); // Opening and paging do not mutate anything.
  await get('detail-ack-form').handlers.submit({preventDefault() {}});
  assert.match(get('detail-action-result').textContent, /请先填写管理令牌/);
  assert.ok(state.calls.every(call => !call.options.method));
  get('detail-token').value = 'test-token';
  state.failAuth = true;
  await get('detail-ack-form').handlers.submit({preventDefault() {}});
  assert.match(get('detail-action-result').textContent, /管理令牌不正确/);
  assert.equal(get('detail-ack').disabled, false);
  state.failAuth = false;
  await get('detail-ack-form').handlers.submit({preventDefault() {}});
  assert.match(get('detail-action-result').textContent, /确认操作已保存/);
  assert.equal(get('detail-ack').disabled, true);
  assert.equal(get('detail-token').value, '');
  assert.deepEqual(state.calls.filter(call => call.options.method).map(call => call.url),
    ['/api/v1/events/42/acknowledge','/api/v1/events/42/acknowledge']);
  state.failEvidence = true;
  await get('evidence-next').handlers.click();
  assert.match(get('event-evidence').textContent, /证据读取失败/);
  assert.equal(get('detail-raw').textContent, '');
  assert.equal(get('evidence-next').disabled, true);
  state.failEvidence = false;
  await get('detail-refresh').handlers.click();
  assert.match(get('evidence-position').textContent, /1–20 \/ 45/);
  state.failDetail = true;
  await get('detail-refresh').handlers.click();
  assert.equal(get('detail-overview').hidden, true);
  assert.equal(get('detail-ack').disabled, true);
  assert.match(get('detail-notice').textContent, /记录不存在/);

  const risk = page('/risks/5');
  await settle();
  assert.equal(risk.get('detail-title').textContent, '安全风险 #5');
  assert.equal(risk.get('back-home').href, '/#security');
  assert.match(risk.get('risk-evidence').textContent, /<script>原样显示，不执行<\/script>/);
  assert.match(risk.get('risk-evidence').textContent, /已确认存在漏洞否/);
  assert.equal(risk.state.calls.length, 1);
  assert.ok(!risk.get('risk-evidence').children.some(node => node.tag === 'script'));
  risk.get('detail-token').value = 'test-token';
  await risk.get('detail-ack-form').handlers.submit({preventDefault() {}});
  assert.equal(risk.state.calls.find(call => call.options.method).url, '/api/v1/risks/5/acknowledge');
  assert.equal(risk.get('detail-ack').disabled, true);

  const invalid = page('/events/not-an-id');
  await settle();
  assert.match(invalid.get('detail-notice').textContent, /地址无效/);
  assert.equal(invalid.state.calls.length, 0);
  console.log('告警详情 DOM 检查通过：分页、只读访问、确认鉴权、缺失证据和失败重试。');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
