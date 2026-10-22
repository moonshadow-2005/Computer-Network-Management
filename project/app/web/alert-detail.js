'use strict';

const $ = id => document.getElementById(id);
const route = window.location.pathname.match(/^\/(events|risks)\/([1-9]\d*)\/?$/);
const kind = route?.[1];
const recordId = route?.[2];
const isEvent = kind === 'events';
const label = isEvent ? '故障告警' : '安全风险';
const pageSize = 20;
let record = null;
let offset = 0;
let detailRequest = 0;
let evidenceRequest = 0;
let acknowledging = false;

const names = {
  ok:'正常',up:'开启',down:'关闭',testing:'测试中',timeout:'超时',unavailable:'未知',
  active:'持续中', recovered:'已恢复', open:'待处理', acknowledged:'已确认', resolved:'已解除',
  warning:'警告', live:'真实采集', sample:'演示样例', document:'课程文档', configured:'手动配置',
  ping_unreachable:'主机连续不可达', interface_oper_down:'接口运行状态异常',
  snmp_common_default_read_access:'默认 SNMP 凭据可读管理信息',
  possible_telnet_service_tcp_23:'TCP/23 开放，需核实明文管理服务',
};
const fieldNames = {
  summary:'证据摘要', basis:'判断依据', recommendation:'建议操作',
  common_default_credential:'命中常见默认凭据规则', observed_from:'观测位置',
  other_network_exposure_tested:'已测试其他网络的暴露情况', vulnerability_confirmed:'已确认存在漏洞',
  attack_source_identified:'已识别攻击来源', check_id:'端口检查编号', port:'端口', observed_state:'观测状态',
  metric_id:'性能样本编号', interface_id:'关联接口', threshold_pct:'利用率阈值（%）',
  in_util_pct:'入方向利用率（%）', out_util_pct:'出方向利用率（%）',
  consecutive_metric_ids:'连续满足规则的性能样本编号',
};

function show(value) {
  if (value === null || value === undefined || value === '') return '未记录';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}
function named(value) { return names[value] || show(value); }
function time(value) { return value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) : '未记录'; }
function fields(root, entries) {
  root.replaceChildren();
  for (const [key, value] of entries) {
    const box = document.createElement('div');
    const term = document.createElement('dt'); term.textContent = key;
    const description = document.createElement('dd'); description.textContent = show(value);
    if (show(value).length > 90) box.className = 'wide';
    box.append(term, description); root.append(box);
  }
}
async function request(path, options = {}) {
  if (globalThis.NmsAuth) options = await globalThis.NmsAuth.prepare(options);
  const response = await fetch('/api/v1/' + path, options);
  const body = await response.json();
  if (!response.ok || body.status !== 'ok') {
    if (response.status === 404) throw new Error('这条记录不存在，可能编号有误或已被移除。');
    if (response.status === 401) throw new Error(globalThis.NmsAuth?.local
      ? '本机保存的授权已失效，请更新本机加密凭据并重新打开连接。'
      : '管理令牌不正确，请填写 DEV 服务的管理令牌。');
    if (response.status === 503 && body.error?.message === 'write operations require NMS_API_TOKEN') {
      throw new Error('当前服务未配置管理令牌，暂时不能确认记录。');
    }
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  }
  return body.data;
}
function updateAck() {
  const confirmed = record && (isEvent ? Boolean(record.acknowledged_at) : record.state !== 'open');
  $('detail-ack').disabled = !record || confirmed || acknowledging;
  $('detail-ack').textContent = !record ? '确认告警' : confirmed
    ? (record.state === 'resolved' ? '风险已解除' : '已确认') : `确认${label}`;
}
function renderRecord(item) {
  document.title = `${label} #${recordId} · 第 2 组网络管理`;
  $('detail-title').textContent = `${label} #${recordId}`;
  $('detail-subtitle').textContent = `${item.device_id} · ${named(item.type || item.rule_id)}`;
  const entries = [
    ['记录编号', `#${recordId}`], ['设备', item.device_id], ['数据来源', named(item.data_source)],
    [isEvent ? '告警类型' : '风险规则', named(item.type || item.rule_id)],
    ['当前状态', named(item.state)], ['首次发现', time(item.first_seen)], ['最近观测', time(item.last_seen)],
    [isEvent ? '恢复时间' : '解除时间', time(item.recovered_at || item.resolved_at)],
  ];
  if (isEvent) entries.push(['接口', item.interface_id], ['当前接口名称', item.interface_context?.description],
    ['接口信息采样时间', time(item.interface_context?.sampled_at)], ['业务影响', item.impact_note || '需结合实际业务确认'], ['关联证据数', item.evidence_count],
    ['确认情况', item.acknowledged_at ? '已确认' : '待确认'], ['确认时间', time(item.acknowledged_at)]);
  else entries.push(['风险级别', named(item.level)],
    ['确认情况', item.state === 'acknowledged' ? '已确认（未单独记录确认时间）' : named(item.state)]);
  fields($('detail-fields'), entries);
  for (const id of ['detail-overview','detail-evidence-panel','detail-manage']) $(id).hidden = false;
  $('detail-notice').textContent = isEvent
    ? '证据是关联的历史观测，不会在打开详情时重新探测设备；确认不会将故障标记为恢复。'
    : '此页显示这条风险最近一次保存的证据；风险提示不等于已经确认漏洞或攻击。';
  if (!isEvent) {
    $('risk-evidence').hidden = false;
    fields($('risk-evidence'), Object.entries(item.evidence || {}).map(([key, value]) => [fieldNames[key] || key, value]));
    $('evidence-help').textContent = '按字段展示全部已保存证据；原始数据可在下方展开。';
    $('detail-raw').textContent = JSON.stringify(item, null, 2);
  }
  updateAck();
}
function evidenceTable(data) {
  const root = $('event-evidence'); root.replaceChildren();
  if (!data.items.length) {
    const empty = document.createElement('p'); empty.className = 'empty';
    empty.textContent = '此页暂无关联证据。'; root.append(empty); return;
  }
  const ping = data.kind === 'ping_unreachable';
  const headers = ping ? ['编号','观测时间','目标','发包 / 收包','丢包率','RTT','查询结果','来源']
    : ['编号','观测时间','接口','管理状态','运行状态','查询结果','来源'];
  const table = document.createElement('table'), head = document.createElement('thead');
  const header = document.createElement('tr');
  for (const name of headers) { const cell = document.createElement('th'); cell.textContent = name; header.append(cell); }
  head.append(header); table.append(head);
  const body = document.createElement('tbody');
  for (const sample of data.items) {
    const row = document.createElement('tr');
    if (!sample.sample_available) {
      const id = document.createElement('td'); id.textContent = show(sample.sample_id);
      const note = document.createElement('td'); note.colSpan = headers.length - 1;
      note.textContent = sample.note; row.append(id, note);
    } else {
      const values = ping ? [sample.sample_id, time(sample.sampled_at), sample.target,
        `${show(sample.packets_sent)} / ${show(sample.packets_received)}`,
        sample.loss_pct == null ? '未知' : `${sample.loss_pct}%`,
        sample.rtt_ms == null ? '未知' : `${sample.rtt_ms} ms`, named(sample.query_status), named(sample.data_source)]
        : [sample.sample_id, time(sample.sampled_at), sample.interface_id,
          named(sample.admin_status), named(sample.oper_status), named(sample.query_status), named(sample.data_source)];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = show(value); row.append(cell); }
    }
    body.append(row);
  }
  table.append(body); root.append(table);
}
async function loadEvidence(targetOffset = 0) {
  const requestId = ++evidenceRequest;
  $('evidence-prev').disabled = true; $('evidence-next').disabled = true;
  $('evidence-position').textContent = '正在读取证据…';
  $('evidence-pager').hidden = false;
  try {
    const data = await request(`${kind}/${recordId}/evidence?limit=${pageSize}&offset=${targetOffset}`);
    if (requestId !== evidenceRequest) return;
    offset = data.offset;
    evidenceTable(data);
    $('evidence-help').textContent = `共 ${data.total} 条关联证据，按最近记录在前排列，每页 ${pageSize} 条；刷新详情可查看新增证据。`;
    $('evidence-position').textContent = data.total ? `${data.items.length ? data.offset + 1 : 0}–${data.offset + data.items.length} / ${data.total} 条` : '0 条';
    $('evidence-prev').disabled = offset === 0;
    $('evidence-next').disabled = offset + data.items.length >= data.total;
    $('detail-raw').textContent = JSON.stringify({record, evidence_page:data}, null, 2);
  } catch (error) {
    if (requestId !== evidenceRequest) return;
    $('event-evidence').textContent = `证据读取失败：${error.message}`;
    $('detail-raw').textContent = '';
    $('evidence-position').textContent = '请点击“刷新详情”重试';
  }
}
async function loadDetail() {
  const requestId = ++detailRequest;
  ++evidenceRequest;
  record = null; updateAck();
  $('detail-refresh').disabled = true;
  $('detail-notice').textContent = '正在读取告警信息…';
  for (const id of ['detail-overview','detail-evidence-panel','detail-manage']) $(id).hidden = true;
  try {
    const item = await request(`${kind}/${recordId}`);
    if (requestId !== detailRequest) return;
    record = item;
    renderRecord(item);
    if (isEvent) await loadEvidence(0);
  } catch (error) {
    if (requestId !== detailRequest) return;
    $('detail-notice').textContent = `读取失败：${error.message}`;
  } finally {
    if (requestId === detailRequest) $('detail-refresh').disabled = false;
  }
}

$('detail-refresh').addEventListener('click', () => loadDetail());
$('evidence-prev').addEventListener('click', () => loadEvidence(Math.max(0, offset - pageSize)));
$('evidence-next').addEventListener('click', () => loadEvidence(offset + pageSize));
$('detail-ack-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('detail-ack').disabled || acknowledging || !record) return;
  const token = $('detail-token').value.trim();
  const localAuth = globalThis.NmsAuth ? await globalThis.NmsAuth.ready : false;
  if (!token && !localAuth) {
    $('detail-action-result').textContent = globalThis.NmsAuth?.available
      ? '本机授权暂不可用，请检查连接或更新本机加密凭据。'
      : '请先填写管理令牌。查看证据无需令牌。';
    return;
  }
  acknowledging = true; updateAck();
  $('detail-action-result').textContent = '正在确认…';
  try {
    await request(`${kind}/${recordId}/acknowledge`, {method:'POST', headers:{'X-Admin-Token':token}});
    $('detail-token').value = '';
    await loadDetail();
    $('detail-action-result').textContent = '确认操作已保存，未改变自动恢复或解除状态。';
  } catch (error) {
    $('detail-action-result').textContent = `确认失败：${error.message}`;
  } finally {
    acknowledging = false; updateAck();
  }
});
if (route) {
  $('back-home').href = isEvent ? '/#fault' : '/#security';
  loadDetail();
} else {
  $('detail-refresh').disabled = true;
  $('detail-notice').textContent = '告警地址无效，请返回主页选择一条记录。';
}
