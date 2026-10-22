const $ = (id) => document.getElementById(id);
const base = '/api/v1';
let cachedSnapshots = [];
let snapshotRequestId = 0;
let snapshotLoadError = '';
let refreshRequestId = 0;
let pingTrendRequestId = 0;
let metricTrendRequestId = 0;
let faultHistoryRequestId = 0;
let cachedBaselines = [];
let baselineInitialized = false;
let currentHealth = null;
let currentInterfaces = [];
const tablePages = new Map();
const labels = {live:'真实采集',sample:'演示样例',document:'课程文档',configured:'手动配置',mixed:'混合来源',none:'无来源',
  active:'持续中',recovered:'已恢复',open:'待处理',acknowledged:'已确认',resolved:'已解除',ok:'正常',partial:'不完整',
  failed:'失败',error:'错误',timeout:'超时',unavailable:'未知',pending:'等待中',running:'执行中',succeeded:'成功',cancelled:'已取消',
  candidate:'候选身份','router-candidate':'路由器候选','test-host':'测试主机',development:'开发主机',up:'开启',down:'关闭',testing:'测试中',warning:'警告',
  snmp_common_default_read_access:'默认 SNMP 凭据可读',possible_telnet_service_tcp_23:'TCP/23 潜在明文管理入口',
  unexpected_service_open:'预期关闭的服务开放',expected_service_closed:'预期开放的服务关闭',sustained_high_utilization:'连续高利用率',observed:'地址表观测',unknown:'未知',ambiguous:'归属不唯一',
  first_or_previous_invalid_sample:'首次或前值无效',sample_gap:'采样间隔过长',query_failed:'查询失败',source_changed:'来源改变',
  non_increasing_time:'采样时间未递增',agent_restarted:'采集代理重启',counter_discontinuity:'计数器不连续',counter_reset_or_wrap:'计数器复位或回绕'};
function named(value) {
  if (String(value).startsWith('unexpected_open_tcp_')) return `预期关闭的 TCP/${String(value).slice(20)} 开放`;
  if (String(value).startsWith('high_utilization_')) return `接口持续高利用率：${String(value).slice(17)}`;
  return labels[value] || show(value);
}
function preferredObservation(items) {
  const score = x => (x.data_source === 'live' ? 8 : 0) + (x.oper_status === 'up' ? 4 : 0)
    - (/^(lo$|virbr|docker|veth|br-|tun|tap)/i.test(x.description || '') ? 100 : 0);
  return items.slice().sort((a,b) => score(b)-score(a))[0]?.interface_id;
}

const networkActions = {
  collect: {button:'collect', note:'collect-availability', label:'真实采集',
    help:'本地模式展示已有样例；采集新网络数据需要连接并启用实验环境。'},
  probe: {button:'probe-service', note:'probe-availability', label:'真实端口检查', flag:'service_probe_enabled',
    help:'本地模式可以保存基线；实际检查端口需要连接并启用实验环境。'},
  iperf: {button:'iperf-start', note:'iperf-availability', label:'真实 iperf3 测试', flag:'iperf3_enabled',
    help:'本地模式只展示预置样例结果；真实测试需要实验网连接和两端 SSH 配置。'},
};

function networkActionReason(name) {
  const action = networkActions[name];
  if (!currentHealth) return '无法确认功能是否已启用，请刷新数据后重试。';
  if (!currentHealth.live_enabled || (action.flag && !currentHealth[action.flag])) {
    return `当前未启用${action.label}。${action.help}`;
  }
  return '';
}

function updateNetworkControls(health) {
  currentHealth = health;
  if (health?.iperf3_allowed_devices) {
    for (const id of ['iperf-client','iperf-server']) {
      const select = $(id);
      for (const option of select.children) option.disabled = !health.iperf3_allowed_devices.includes(option.value);
      if (!health.iperf3_allowed_devices.includes(select.value)) {
        select.value = health.iperf3_allowed_devices[id === 'iperf-client' ? 0 : 1] || '';
      }
    }
  }
  for (const [name, action] of Object.entries(networkActions)) {
    const reason = networkActionReason(name);
    $(action.button).disabled = Boolean(reason);
    $(action.button).setAttribute('title', reason);
    $(action.note).textContent = reason || `${action.label}已启用；${globalThis.NmsAuth?.local ? '本机管理授权已就绪。' : '操作前请填写管理令牌。'}`;
  }
}

async function api(path, options = {}) {
  if (globalThis.NmsAuth) options = await globalThis.NmsAuth.prepare(options);
  if (options.method?.toUpperCase() === 'POST' && !options.headers?.['X-Admin-Token']?.trim()
      && options.headers?.['X-NMS-Local-Auth'] !== '1') {
    throw new Error('请先在导航“运行 → 管理令牌”填写启动服务时设置的令牌。');
  }
  const response = await fetch(base + path, options);
  const body = await response.json();
  if (!response.ok || body.status !== 'ok') {
    if (response.status === 401) {
      if (globalThis.NmsAuth?.local) throw new Error('本机保存的授权已失效，请更新本机加密凭据并重新打开连接。');
      throw new Error('管理令牌不正确，请在“运行与采集”填写与启动服务时 NMS_API_TOKEN 相同的值；刷新或重新打开页面后需要重新填写。');
    }
    if (response.status === 503 && body.error?.message === 'write operations require NMS_API_TOKEN') {
      throw new Error('当前服务未启用管理写入。本地交互测试请使用 local 模式并设置管理令牌。');
    }
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  }
  return body.data;
}
function localTime(value) { return value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) : '未知'; }
function show(value) { return value === null || value === undefined || value === '' ? '未知' : String(value); }
function badge(value) {
  const span = document.createElement('span'); span.className = 'badge ' + String(value || '').replace(/[^a-z]/g, '');
  span.textContent = named(value); span.setAttribute('title', show(value)); return span;
}
function alertSummary(text) {
  const span = document.createElement('span');
  span.className = 'alert-summary';
  const value = String(text || '暂无证据摘要');
  span.textContent = value.length > 90 ? value.slice(0, 89) + '…' : value;
  return span;
}
function alertLink(kind, id) {
  const link = document.createElement('a');
  link.href = `/${kind}/${encodeURIComponent(id)}`;
  link.textContent = '查看详情';
  link.setAttribute('aria-label', `查看${kind === 'events' ? '故障告警' : '安全风险'} #${id} 详情`);
  return link;
}
function riskSummary(item) {
  const evidence = item.evidence || {};
  if (evidence.summary) return evidence.summary;
  if (evidence.interface_id) return `接口 ${evidence.interface_id} 利用率达到 ${evidence.threshold_pct ?? '配置的'}% 阈值`;
  if (evidence.port) return `TCP/${evidence.port} 观测状态：${evidence.observed_state || '未知'}；依据服务基线检查`;
  return evidence.basis || '规则触发记录；完整证据见详情';
}
function table(id, headers, rows) {
  const root = $(id); root.replaceChildren();
  if (!rows.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = '暂无记录'; root.append(p); return; }
  const t = document.createElement('table'), head = document.createElement('thead'), tr = document.createElement('tr');
  for (const h of headers) { const th = document.createElement('th'); th.textContent = h; tr.append(th); } head.append(tr); t.append(head);
  const body = document.createElement('tbody');
  const size = 10;
  const page = Math.min(tablePages.get(id) || 0, Math.max(0, Math.ceil(rows.length / size) - 1));
  tablePages.set(id, page);
  for (const cells of rows.slice(page * size, (page + 1) * size)) {
    const row = document.createElement('tr');
    for (const value of cells) { const td = document.createElement('td'); td.append(value instanceof Node ? value : document.createTextNode(show(value))); row.append(td); }
    body.append(row);
  }
  t.append(body); root.append(t);
  if (rows.length > size) {
    const pager = document.createElement('div'); pager.className = 'pager';
    const prev = document.createElement('button'), next = document.createElement('button'), info = document.createElement('span');
    prev.textContent = '上一页'; next.textContent = '下一页';
    prev.disabled = page === 0; next.disabled = (page + 1) * size >= rows.length;
    info.textContent = `${page * size + 1}–${Math.min((page + 1) * size, rows.length)} / 已载入 ${rows.length} 条`;
    for (const [button, step] of [[prev,-1],[next,1]]) button.addEventListener('click', () => {
      tablePages.set(id, page + step); table(id, headers, rows);
    });
    pager.append(prev,info,next); root.append(pager);
  }
}
function failed(id, error) { $(id).textContent = `读取失败：${error.message}`; }
function syncBaselineForm() {
  const deviceId = $('baseline-device').value;
  const port = Number($('baseline-port').value);
  const saved = cachedBaselines.find(item => item.device_id === deviceId && item.port === port);
  $('baseline-allowed').value = saved ? String(Boolean(saved.allowed)) : 'false';
  $('baseline-note').value = saved?.note || '';
  $('baseline-current').textContent = saved
    ? `已载入 ${deviceId}:${port} 的基线：预期${saved.allowed ? '开放' : '关闭'}。`
    : `${deviceId}:${port || '未选端口'} 尚无已保存基线；请核对预期状态后保存。`;
}
function renderResource(result, name, id, headers, rows, failures) {
  if (result.status === 'rejected') {
    failures.push(name);
    failed(id, result.reason);
    return {items:[]};
  }
  table(id, headers, rows(result.value.items));
  if (result.value.total > result.value.items.length) {
    const note = document.createElement('p'); note.className = 'footer-note';
    note.textContent = `本次载入 ${result.value.items.length} / ${result.value.total} 条；完整记录可通过分页 API 查询。`;
    $(id).append(note);
  }
  return result.value;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svgNode(name, attributes = {}, text = '') {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text) node.textContent = text;
  return node;
}
function renderTopology(topology) {
  const observed = topology.edges.filter(edge => edge.kind === 'ip_subnet');
  const documents = topology.edges.filter(edge => edge.data_source === 'document');
  $('topology-summary').textContent = `${topology.nodes.length} 个节点；${observed.length} 条采样子网关系，${documents.length} 条文档关系。${topology.note}`;
  table('topology-edges', ['对象', '子网', '接口/IP', '依据', '采样时间', '来源'], topology.edges.map(edge => [
    edge.source, topology.nodes.find(node => node.node_id === edge.target)?.label || edge.target,
    edge.interface_id ? `${edge.interface_id} · ${edge.address} / ${edge.mask}` : '未实测',
    edge.basis, localTime(edge.sampled_at), badge(edge.data_source)
  ]));
  $('identity-candidates').textContent = topology.identity_candidates?.length
    ? '可能指向同一 SNMP 代理的候选地址：' + topology.identity_candidates.map(group => group.device_ids.join('、')).join('；') + '。尚未归并物理设备。'
    : '设备身份按候选地址保留；当前没有充分依据归并物理设备。';
  const root = $('topology-diagram'); root.replaceChildren();
  if (!topology.nodes.length) return;
  const devices = topology.nodes.filter(node => node.role !== 'subnet');
  const subnets = topology.nodes.filter(node => node.role === 'subnet');
  const height = Math.max(220, Math.max(devices.length, subnets.length) * 54 + 55);
  const svg = svgNode('svg', {viewBox:`0 0 960 ${height}`, role:'img', 'aria-label':'设备与子网关系图'});
  svg.append(svgNode('title', {}, '实线为采样得到的子网归属，虚线为课程文档关系；不表示物理直连'));
  const positions = new Map();
  devices.forEach((node, index) => positions.set(node.node_id, {x:20, y:35 + index*54, width:250}));
  subnets.forEach((node, index) => positions.set(node.node_id, {x:650, y:35 + index*54, width:280}));
  for (const edge of topology.edges) {
    const source = positions.get(edge.source), target = positions.get(edge.target);
    if (!source || !target) continue;
    const line = svgNode('line', {x1:source.x+source.width, y1:source.y+16, x2:target.x, y2:target.y+16,
      stroke:edge.data_source === 'live' ? '#087c78' : edge.data_source === 'sample' ? '#7950b8' : '#8797a5',
      'stroke-width':edge.kind === 'ip_subnet' ? 2.5 : 1.5,
      'stroke-dasharray':edge.kind === 'ip_subnet' ? 'none' : '6 5'});
    line.append(svgNode('title', {}, `${edge.source} → ${edge.target} · ${edge.basis} · ${edge.data_source} · ${localTime(edge.sampled_at)}`));
    svg.append(line);
  }
  for (const node of topology.nodes) {
    const p = positions.get(node.node_id);
    if (!p) continue;
    svg.append(svgNode('rect', {x:p.x, y:p.y, width:p.width, height:34, rx:6, fill:'#edf5f8', stroke:'#b2cbd5'}));
    svg.append(svgNode('text', {x:p.x+10, y:p.y+22, fill:'#173b50', 'font-size':14},
      node.role === 'subnet' ? node.label : `${node.node_id} · ${node.address || ''}`));
  }
  root.append(svg);
}
function formatAxisValue(value, unit) {
  if (unit === 'bit/s') {
    if (value >= 1e9) return `${(value / 1e9).toFixed(2)} Gbit/s`;
    if (value >= 1e6) return `${(value / 1e6).toFixed(2)} Mbit/s`;
    if (value >= 1e3) return `${(value / 1e3).toFixed(2)} kbit/s`;
    return `${value.toFixed(0)} bit/s`;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`;
}
function chartTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '未知';
}
function renderLineChart(containerId, samples, series) {
  const root = $(containerId);
  root.replaceChildren();
  const values = series.flatMap(item => samples
    .filter(sample => !item.isValid || item.isValid(sample))
    .map(sample => sample[item.field])
    .filter(value => typeof value === 'number' && Number.isFinite(value)));
  if (!values.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '所选时间范围内没有可绘制的有效样本；无效采样请查看下方明细。';
    root.append(empty);
    return;
  }

  const width = 720, height = 270, left = 96, right = 18, top = 18, bottom = 46;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const maxValue = Math.max(0, ...values);
  const scaleMax = maxValue > 0 ? maxValue * 1.08 : 1;
  const sampleTimes = samples.map(sample => Date.parse(sample.sampled_at));
  const validTimes = sampleTimes.filter(Number.isFinite);
  const firstTime = validTimes[0], lastTime = validTimes[validTimes.length - 1];
  const timeSpan = lastTime - firstTime;
  const xAt = index => {
    if (Number.isFinite(sampleTimes[index]) && timeSpan > 0) {
      return left + plotWidth * (sampleTimes[index] - firstTime) / timeSpan;
    }
    return samples.length < 2 ? left + plotWidth / 2 : left + plotWidth * index / (samples.length - 1);
  };
  const yAt = value => top + plotHeight * (1 - value / scaleMax);
  const titleId = `${containerId}-title`;
  const svg = svgNode('svg', {viewBox:`0 0 ${width} ${height}`, role:'img', 'aria-labelledby':titleId});
  svg.append(svgNode('title', {id:titleId}, `${series.map(item => item.label).join('、')}趋势`));
  svg.append(svgNode('desc', {}, `按采样时间排列，共 ${samples.length} 条样本；没有连线的区段表示该时刻没有有效值。`));

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = scaleMax * (1 - tick / 4), y = yAt(value);
    svg.append(svgNode('line', {x1:left, y1:y, x2:width-right, y2:y, class:'chart-gridline'}));
    svg.append(svgNode('text', {x:left-10, y:y+4, 'text-anchor':'end', class:'chart-axis'}, formatAxisValue(value, series[0].unit)));
  }
  svg.append(svgNode('line', {x1:left, y1:top, x2:left, y2:top+plotHeight, class:'chart-axis-line'}));
  svg.append(svgNode('line', {x1:left, y1:top+plotHeight, x2:width-right, y2:top+plotHeight, class:'chart-axis-line'}));
  if (samples.length) {
    svg.append(svgNode('text', {x:left, y:height-12, 'text-anchor':'start', class:'chart-axis'}, chartTime(samples[0].sampled_at)));
    svg.append(svgNode('text', {x:width-right, y:height-12, 'text-anchor':'end', class:'chart-axis'}, chartTime(samples[samples.length-1].sampled_at)));
  }

  for (const item of series) {
    let segment = [];
    const drawSegment = () => {
      if (segment.length > 1) {
        svg.append(svgNode('polyline', {points:segment.map(point => `${point.x},${point.y}`).join(' '),
          fill:'none', stroke:item.color, 'stroke-width':2.5, 'stroke-linecap':'round', 'stroke-linejoin':'round'}));
      }
      for (const point of segment) {
        const circle = svgNode('circle', {cx:point.x, cy:point.y, r:3.5, fill:item.color});
        circle.append(svgNode('title', {}, `${chartTime(point.sample.sampled_at)} · ${item.label} ${formatAxisValue(point.value, item.unit)}`));
        svg.append(circle);
      }
      segment = [];
    };
    samples.forEach((sample, index) => {
      const value = sample[item.field];
      if ((!item.isValid || item.isValid(sample)) && typeof value === 'number' && Number.isFinite(value) && value >= 0) {
        segment.push({x:xAt(index), y:yAt(value), value, sample});
      } else drawSegment();
    });
    drawSegment();
  }

  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  for (const item of series) {
    const entry = document.createElement('span');
    entry.className = 'chart-legend-item';
    entry.style.setProperty('--series-color', item.color);
    entry.textContent = item.label;
    legend.append(entry);
  }
  root.append(legend, svg);
}

function sourceSummary(samples) {
  const sources = [...new Set(samples.map(item => item.data_source).filter(Boolean))];
  return sources.length ? sources.map(named).join('、') : '无来源';
}
function sampleRange(samples) {
  return samples.length
    ? `${localTime(samples[0].sampled_at)} 至 ${localTime(samples[samples.length-1].sampled_at)}`
    : '暂无样本';
}
function formatRate(value) {
  return value === null || value === undefined ? '未知' : `${formatAxisValue(value, 'bit/s')}`;
}

async function loadPingTrend(deviceId) {
  const requestId = ++pingTrendRequestId;
  const summary = $('ping-trend-summary');
  if (!deviceId) {
    summary.textContent = '没有可选择的测试主机。';
    renderLineChart('ping-rtt-chart', [], [{field:'rtt_ms',label:'RTT',unit:'ms',color:'#087f8c'}]);
    renderLineChart('ping-loss-chart', [], [{field:'loss_pct',label:'丢包率',unit:'%',color:'#c6533d'}]);
    table('ping-trend-history', ['目标', '时间', 'RTT', '丢包', '查询状态', '来源'], []);
    return;
  }
  summary.textContent = '正在读取该主机最近 120 条 ping 样本…';
  try {
    const params = new URLSearchParams({device_id:deviceId, limit:'120'});
    const result = await api('/ping-samples?' + params);
    if (requestId !== pingTrendRequestId || $('ping-trend-device').value !== deviceId) return;
    const samples = result.items.slice().sort((a, b) => Date.parse(a.sampled_at) - Date.parse(b.sampled_at) || a.sample_id - b.sample_id);
    const rttCount = samples.filter(item => item.query_status === 'ok' && typeof item.rtt_ms === 'number' && Number.isFinite(item.rtt_ms)).length;
    const lossCount = samples.filter(item => item.query_status === 'ok' && typeof item.loss_pct === 'number' && Number.isFinite(item.loss_pct)).length;
    summary.textContent = `${deviceId}：${samples.length} 条样本，RTT 有效 ${rttCount} 条、丢包率有效 ${lossCount} 条；${sampleRange(samples)}；来源 ${sourceSummary(samples)}。无效值不参与趋势线。`;
    renderLineChart('ping-rtt-chart', samples, [{field:'rtt_ms',label:'RTT',unit:'ms',color:'#087f8c',isValid:item=>item.query_status==='ok'}]);
    renderLineChart('ping-loss-chart', samples, [{field:'loss_pct',label:'丢包率',unit:'%',color:'#c6533d',isValid:item=>item.query_status==='ok'}]);
    table('ping-trend-history', ['目标', '时间', 'RTT', '丢包', '查询状态', '来源'], samples.slice(-30).reverse().map(item => [
      item.target, localTime(item.sampled_at), item.query_status !== 'ok' || item.rtt_ms === null ? '未知' : `${item.rtt_ms.toFixed(2)} ms`,
      item.query_status !== 'ok' || item.loss_pct === null ? '未知' : `${item.loss_pct}%`, badge(item.query_status), badge(item.data_source)
    ]));
  } catch (error) {
    if (requestId !== pingTrendRequestId) return;
    summary.textContent = `ping 历史读取失败：${error.message}`;
    renderLineChart('ping-rtt-chart', [], [{field:'rtt_ms',label:'RTT',unit:'ms',color:'#087f8c'}]);
    renderLineChart('ping-loss-chart', [], [{field:'loss_pct',label:'丢包率',unit:'%',color:'#c6533d'}]);
    table('ping-trend-history', ['目标', '时间', 'RTT', '丢包', '查询状态', '来源'], []);
  }
}

async function loadMetricTrend(interfaceId) {
  const requestId = ++metricTrendRequestId;
  const summary = $('metric-trend-summary');
  if (!interfaceId) {
    summary.textContent = '没有可选择的接口。';
    renderLineChart('metric-rate-chart', [], [
      {field:'in_rate_bps',label:'入方向',unit:'bit/s',color:'#087f8c'},
      {field:'out_rate_bps',label:'出方向',unit:'bit/s',color:'#c27a16'}
    ]);
    table('metrics', ['接口', '时间', '入速率', '出速率', '利用率', '有效性', '来源'], []);
    return;
  }
  summary.textContent = '正在读取该接口最近 120 条速率样本…';
  try {
    const params = new URLSearchParams({interface_id:interfaceId, limit:'120'});
    const result = await api('/metrics?' + params);
    if (requestId !== metricTrendRequestId || $('metric-trend-interface').value !== interfaceId) return;
    const samples = result.items.slice().sort((a, b) => Date.parse(a.sampled_at) - Date.parse(b.sampled_at) || a.metric_id - b.metric_id);
    const valid = samples.filter(item => item.valid).length;
    const invalid = samples.length - valid;
    summary.textContent = `${interfaceId}：${samples.length} 条样本，有效 ${valid} 条、无效 ${invalid} 条；${sampleRange(samples)}；来源 ${sourceSummary(samples)}。无效区间不会连入趋势线。`;
    renderLineChart('metric-rate-chart', samples, [
      {field:'in_rate_bps',label:'入方向',unit:'bit/s',color:'#087f8c',isValid:item=>item.valid},
      {field:'out_rate_bps',label:'出方向',unit:'bit/s',color:'#c27a16',isValid:item=>item.valid}
    ]);
    table('metrics', ['接口', '时间', '入速率', '出速率', '入/出利用率', '有效性', '来源'], samples.slice(-30).reverse().map(item => [
      item.interface_id, localTime(item.sampled_at), formatRate(item.valid ? item.in_rate_bps : null), formatRate(item.valid ? item.out_rate_bps : null),
      `${!item.valid || item.in_util_pct === null ? '未知' : item.in_util_pct.toFixed(2) + '%'} / ${!item.valid || item.out_util_pct === null ? '未知' : item.out_util_pct.toFixed(2) + '%'}`,
      item.valid ? badge('ok') : named(item.invalid_reason), badge(item.data_source)
    ]));
  } catch (error) {
    if (requestId !== metricTrendRequestId) return;
    summary.textContent = `接口速率历史读取失败：${error.message}`;
    renderLineChart('metric-rate-chart', [], [
      {field:'in_rate_bps',label:'入方向',unit:'bit/s',color:'#087f8c'},
      {field:'out_rate_bps',label:'出方向',unit:'bit/s',color:'#c27a16'}
    ]);
    table('metrics', ['接口', '时间', '入速率', '出速率', '利用率', '有效性', '来源'], []);
  }
}

async function loadFaultHistory(deviceId) {
  const requestId = ++faultHistoryRequestId;
  const summary = $('fault-filter-summary');
  summary.textContent = '正在读取所选设备最近 30 条故障与连通性历史…';
  const params = new URLSearchParams({limit:'30', evidence_limit:'3'});
  if (deviceId) params.set('device_id', deviceId);
  const [eventResult, pingResult, diagnosticResult] = await Promise.allSettled([
    api('/events?' + params), api('/ping-samples?' + params),
    api('/diagnostics?' + (deviceId ? new URLSearchParams({device_id:deviceId}) : ''))
  ]);
  if (requestId !== faultHistoryRequestId || $('fault-device').value !== deviceId) return;

  if (eventResult.status === 'fulfilled') {
    const events = eventResult.value.items;
    table('events', ['编号', '设备 / 接口', '类型', '状态', '确认', '最近', '证据摘要', '来源', '操作'], events.map(item => {
      const acknowledge = document.createElement('button');
      acknowledge.type = 'button';
      acknowledge.disabled = Boolean(item.acknowledged_at);
      acknowledge.textContent = item.acknowledged_at ? '已确认' : '确认告警';
      acknowledge.setAttribute('aria-label', `确认故障事件 #${item.event_id}`);
      acknowledge.addEventListener('click', async () => {
        acknowledge.disabled = true;
        summary.textContent = `正在确认故障事件 #${item.event_id}…`;
        try {
          await api(`/events/${item.event_id}/acknowledge`, {
            method:'POST', headers:{'X-Admin-Token':$('admin-token').value},
          });
          await loadFaultHistory(deviceId);
        } catch (error) {
          acknowledge.disabled = false;
          summary.textContent = `确认故障事件 #${item.event_id} 失败：${error.message}`;
        }
      });
      const actions = document.createElement('div'); actions.className = 'alert-actions';
      actions.append(alertLink('events', item.event_id), acknowledge);
      const ids = item.evidence_sample_ids || [];
      const count = item.evidence_count ?? ids.length;
      const preview = count ? `共 ${count} 条；编号 ${ids.slice(0, 3).join('、')}${count > 3 ? '…' : ''}` : '暂无关联样本';
      const types = {ping_unreachable:'主机连续不可达', interface_oper_down:'接口运行状态异常'};
      return [item.event_id, item.interface_id ? `${item.interface_id} · ${item.interface_context?.description || '接口名称未取得'}` : item.device_id,
        (types[item.type] || item.type) + (item.interface_id ? '（业务影响待确认）' : ''), badge(item.state),
        badge(item.acknowledged_at ? '已确认' : '待确认'), localTime(item.last_seen),
        alertSummary(preview), badge(item.data_source), actions];
    }));
  } else failed('events', eventResult.reason);

  if (pingResult.status === 'fulfilled') {
    const samples = pingResult.value.items;
    table('ping', ['设备', '时间', 'RTT', '丢包', '查询', '来源'], samples.map(item => [
      item.device_id, localTime(item.sampled_at), item.rtt_ms === null ? '未知' : `${item.rtt_ms.toFixed(2)} ms`,
      item.loss_pct === null ? '未知' : `${item.loss_pct}%`, badge(item.query_status), badge(item.data_source)
    ]));
  } else failed('ping', pingResult.reason);

  if (diagnosticResult.status === 'fulfilled') {
    table('diagnostics', ['对象', '观测时效', '诊断', '依据', '建议', '来源'],
      diagnosticResult.value.items.flatMap(device => device.findings?.length ? device.findings.map(item => [
        device.device_id, device.stale ? '历史样本（超过 5 分钟）' : localTime(device.sampled_at),
        item.summary, [item.ping_sample_id && `连通性样本 #${item.ping_sample_id}`, item.interface_id && `接口 ${item.interface_id}`,
          item.related_down_interfaces?.length && `关联异常接口 ${item.related_down_interfaces.join('、')}`].filter(Boolean).join('；') || '见最近观测', item.next_steps.join('；'), badge(item.data_source)
      ]) : [[device.device_id, '未取得诊断样本', '未知', '—', '执行一轮采集后查看', badge(device.data_source)]]));
  } else failed('diagnostics', diagnosticResult.reason);

  const label = deviceId || '所有设备';
  const eventCount = eventResult.status === 'fulfilled' ? `${eventResult.value.total} 条告警` : '告警读取失败';
  const pingCount = pingResult.status === 'fulfilled' ? `${pingResult.value.total} 条连通性样本` : '连通性样本读取失败';
  summary.textContent = `${label}：${eventCount}，${pingCount}；各显示最近最多 30 条。查询失败保持独立状态，不会显示成无记录。`;
}

function setOptions(select, entries, selectedValue, labelFor) {
  select.replaceChildren();
  for (const entry of entries) {
    const option = document.createElement('option');
    option.value = String(entry.value);
    option.textContent = labelFor(entry);
    select.append(option);
  }
  if (entries.some(entry => String(entry.value) === String(selectedValue))) select.value = String(selectedValue);
  else if (entries.length) select.value = String(entries[0].value);
}

function updateSnapshotDiffControls() {
  const beforeSelect = $('config-diff-before');
  const afterSelect = $('config-diff-after');
  const entries = cachedSnapshots.slice().sort((a, b) => a.snapshot_id - b.snapshot_id).map(item => ({
    value: item.snapshot_id,
    label: `#${item.snapshot_id} · ${localTime(item.sampled_at)} · ${named(item.data_source)}`
  }));
  const oldBefore = beforeSelect.value;
  const oldAfter = afterSelect.value;
  setOptions(beforeSelect, entries, oldBefore, item => item.label);
  setOptions(afterSelect, entries, oldAfter, item => item.label);
  if (entries.length > 1 && (!oldBefore || !oldAfter || beforeSelect.value === afterSelect.value)) {
    beforeSelect.value = String(entries[0].value);
    afterSelect.value = String(entries[entries.length - 1].value);
  }

  const hasPair = entries.length > 1 && beforeSelect.value !== afterSelect.value;
  beforeSelect.disabled = entries.length < 2;
  afterSelect.disabled = entries.length < 2;
  $('config-diff-submit').disabled = !hasPair;
  $('config-diff-note').textContent = snapshotLoadError
    ? `快照读取失败：${snapshotLoadError}`
    : !entries.length
    ? '当前没有可比较的配置快照。'
    : entries.length < 2
      ? '此设备只有一个配置快照；需要两个不同快照才能比较。'
      : hasPair ? '选择同一设备的两个不同快照后可查看接口与路由的新增、删除和变化。'
        : '请选择两个不同的快照。';
}

async function loadSnapshotChoices(deviceId) {
  const requestId = ++snapshotRequestId;
  cachedSnapshots = [];
  snapshotLoadError = '';
  $('config-diff-before').disabled = true;
  $('config-diff-after').disabled = true;
  $('config-diff-submit').disabled = true;
  $('config-diff-note').textContent = '正在读取该设备的配置快照…';
  $('config-diff-summary').textContent = '';
  $('config-diff-changes').replaceChildren();
  if (!deviceId) {
    updateSnapshotDiffControls();
    return;
  }
  try {
    const params = new URLSearchParams({device_id: deviceId, limit: '500'});
    const result = await api('/config-snapshots?' + params);
    if (requestId !== snapshotRequestId || $('config-diff-device').value !== deviceId) return;
    cachedSnapshots = result.items;
  } catch (error) {
    if (requestId !== snapshotRequestId) return;
    snapshotLoadError = error.message;
  }
  if (requestId === snapshotRequestId) updateSnapshotDiffControls();
}

async function refresh(background = false) {
  background = background === true;
  const requestId = ++refreshRequestId;
  const isCurrent = () => requestId === refreshRequestId;
  $('refresh').disabled = true;
  try {
    const [healthResult, overviewResult, devicesResult, interfacesResult, routesResult, baselinesResult,
      checksResult, risksResult, runsResult, snapshotsResult, topologyResult, tasksResult,
      addressesResult, discoveriesResult] = await Promise.allSettled([
      api('/health'), api('/overview'), api('/devices'), api('/interfaces?limit=500'), api('/routes?limit=500'),
      api('/service-baselines'), api('/service-checks?limit=30'), api('/risks'),
      api('/collector-runs?limit=10'), api('/config-snapshots?limit=10'), api('/topology'), api('/tasks?limit=10'),
      api('/interface-addresses?latest=true&limit=500'), api('/network-discoveries?latest=true&limit=500')
    ]);
    if (!isCurrent()) return;
    const failures = [];
    let liveEnabled = false;
    if (healthResult.status === 'fulfilled') {
      liveEnabled = healthResult.value.live_enabled;
      $('health').textContent = liveEnabled ? '实时采集已启用' : '实时采集未启用';
      updateNetworkControls(healthResult.value);
    } else {
      updateNetworkControls(null);
      failures.push('运行状态');
      failed('health', healthResult.reason);
    }
    const ordinaryNotice = liveEnabled
      ? '实时采集已启用。请结合每条记录的来源、时间和查询状态判断结果。'
      : '当前仅显示课程文档候选对象和已有历史。启用实时采集后才会出现新的网络样本。';
    if (overviewResult.status === 'fulfilled') {
      const counts = overviewResult.value.counts;
      $('count-devices').textContent = counts.devices;
      $('count-interfaces').textContent = counts.interfaces;
      $('count-events').textContent = counts.active_events;
      $('count-risks').textContent = counts.open_risks;
      $('risk-count-note').textContent = `待处理风险 ${counts.open_risks} 条；未解除风险（含已确认）${counts.unresolved_risks ?? '未知'} 条。确认仅表示已知悉。`;
      const run = overviewResult.value.latest_collector_run;
      $('latest-collection').textContent = run
        ? `最近采集开始：${localTime(run.started_at)}；完成：${localTime(run.finished_at)}；状态：${named(run.state)}。页面刷新仅重新读取已有数据。`
        : '尚无采集轮次；请查看数据来源，样例和课程清单不等于实时测量。';
    } else {
      failures.push('概览');
      for (const id of ['count-devices', 'count-interfaces', 'count-events', 'count-risks']) $(id).textContent = '未知';
    }
    const devices = renderResource(devicesResult, '设备', 'devices', ['标识', '地址', '角色', '身份', '来源'],
      items => items.map(x => [x.device_id, x.address, named(x.role), named(x.identity_status), badge(x.data_source)]), failures);
    const interfaces = renderResource(interfacesResult, '接口', 'interfaces', ['接口', '描述', '管理/运行', '参考速率', '采样时间', '状态', '来源'],
      items => items.map(x => [x.interface_id, x.description,
      `${named(x.admin_status)} / ${named(x.oper_status)}`, x.reference_speed_bps ? `${x.reference_speed_bps} bit/s` : '未知',
      localTime(x.sampled_at), badge(x.query_status), badge(x.data_source)]), failures);
    currentInterfaces = interfaces.items;
    renderResource(routesResult, '路由', 'routes', ['对象', '目的/掩码', '下一跳', '接口', '采样时间', '来源'],
      items => items.map(x => [x.device_id, `${x.destination} / ${show(x.mask)}`, x.next_hop, x.interface_id,
        localTime(x.sampled_at), badge(x.data_source)]), failures);
    renderResource(addressesResult, '接口 IP', 'interface-addresses', ['接口', 'IP', '掩码', '子网', '采样时间', '来源'],
      items => items.map(x => [x.interface_id, x.address, x.mask, x.network, localTime(x.sampled_at), badge(x.data_source)]), failures);
    renderResource(discoveriesResult, '地址发现', 'network-discoveries', ['对象', 'sysName', '地址表状态', '采样时间', '说明', '来源'],
      items => items.map(x => [x.device_id, x.sys_name, badge(x.query_status), localTime(x.sampled_at),
        x.issues.join('；') || '地址表完整读取', badge(x.data_source)]), failures);
    renderResource(tasksResult, '任务', 'tasks', ['任务', '端点', '状态', '接收速率', '完成时间', '错误', '来源', '操作'], items => items.map(x => {
      let action = '—';
      const canCancel = x.type === 'iperf3' && ['pending', 'running'].includes(x.task_state);
      if (canCancel) {
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.textContent = '取消任务';
        cancel.setAttribute('aria-label', `取消 iperf3 任务 ${x.task_id}`);
        cancel.addEventListener('click', async () => {
          cancel.disabled = true;
          $('iperf-result').textContent = `正在取消任务 ${x.task_id.slice(0,8)}…`;
          try {
            const task = await api(`/tasks/${encodeURIComponent(x.task_id)}/cancel`, {
              method:'POST', headers:{'X-Admin-Token':$('admin-token').value},
            });
            $('iperf-result').textContent = `任务 ${x.task_id.slice(0,8)} 返回状态：${named(task.task_state)}。`;
            await refresh();
          } catch (error) {
            cancel.disabled = false;
            $('iperf-result').textContent = `取消任务失败：${error.message}`;
          }
        });
        action = cancel;
      }
      return [x.task_id.slice(0,8), `${x.client_device_id} → ${x.server_device_id}`, badge(x.task_state),
        x.result?.received_bps === null || x.result?.received_bps === undefined ? '未知' : `${x.result.received_bps.toFixed(0)} bit/s`,
        localTime(x.finished_at), x.error?.message || '—', badge(x.data_source), action];
    }), failures);
    renderResource(baselinesResult, '服务基线', 'baselines', ['设备', '端口', '允许', '说明', '来源'],
      items => items.map(x => [x.device_id, x.port, x.allowed ? '是' : '否', x.note, badge(x.data_source)]), failures);
    if (baselinesResult.status === 'fulfilled') {
      cachedBaselines = baselinesResult.value.items;
      if (!baselineInitialized) {
        syncBaselineForm();
        baselineInitialized = true;
      }
    } else {
      $('baseline-current').textContent = '基线读取失败；保存前请核对当前表单。';
    }
    renderResource(checksResult, '端口检查', 'service-checks', ['设备', '端口', '观测时间', '状态', '结果说明', '来源'], items => items.map(x => {
      const labels = {open:'开放',closed:'关闭（连接被拒绝）',unavailable:'未知（未能确认）'};
      const state = badge(x.state); state.textContent = labels[x.state] || show(x.state);
      return [x.device_id, x.port, localTime(x.observed_at), state, x.error || '—', badge(x.data_source)];
    }), failures);
    renderResource(risksResult, '风险', 'risks', ['编号', '设备', '规则', '级别', '状态', '最近', '证据摘要', '来源', '详情'],
      items => items.map(x => [x.risk_id, x.device_id, named(x.rule_id),
        named(x.level), badge(x.state), localTime(x.last_seen), alertSummary(riskSummary(x)), badge(x.data_source),
        alertLink('risks', x.risk_id)]), failures);
    renderResource(runsResult, '采集记录', 'runs', ['开始', '完成', '状态', '详情'], items => items.map(x => [localTime(x.started_at), localTime(x.finished_at),
      badge(x.state), x.details.map(y => `${y.device_id || '系统'}: ${named(y.status)}`).join('；')]), failures);
    const snapshots = renderResource(snapshotsResult, '配置快照', 'snapshots', ['编号', '对象', '时间', '接口数', '路由数', '来源'],
      items => items.map(x => [x.snapshot_id, x.device_id,
        localTime(x.sampled_at), Object.keys(x.content.interfaces).length, Object.keys(x.content.routes).length, badge(x.data_source)]), failures);
    const deviceSelect = $('config-diff-device');
    const previousDevice = deviceSelect.value;
    const snapshotDeviceIds = new Set(snapshots.items.map(item => item.device_id));
    const configDevices = devices.items.filter(x =>
      x.role === 'router-candidate' || snapshotDeviceIds.has(x.device_id));
    const preferredDevice = previousDevice ||
      configDevices.find(device => snapshots.items.some(item => item.device_id === device.device_id))?.device_id ||
      configDevices[0]?.device_id || '';
    setOptions(deviceSelect, configDevices.map(item => ({value: item.device_id,
      label: `${item.device_id} · ${item.address}`})), preferredDevice, item => item.label);
    if (!background || !cachedSnapshots.length) await loadSnapshotChoices(deviceSelect.value);
    if (!isCurrent()) return;
    const faultSelect = $('fault-device');
    const previousFaultDevice = faultSelect.value;
    const faultOptions = [{value:'',label:'所有设备'}, ...devices.items.map(item => ({
      value:item.device_id, label:`${item.device_id} · ${item.address}`}))];
    setOptions(faultSelect, faultOptions, previousFaultDevice, item => item.label);
    await loadFaultHistory(faultSelect.value);
    if (!isCurrent()) return;
    const pingDevices = devices.items.filter(item => item.role === 'test-host' ||
      (item.role === 'sample' && item.device_id.startsWith('sample-host-')));
    const pingSelect = $('ping-trend-device');
    const previousPingDevice = pingSelect.value;
    setOptions(pingSelect, pingDevices.map(item => ({value:item.device_id,
      label:`${item.device_id} · ${item.address}`})), previousPingDevice || pingDevices[0]?.device_id, item => item.label);
    const metricSelect = $('metric-trend-interface');
    const previousInterface = metricSelect.value;
    const preferredInterface = previousInterface || preferredObservation(interfaces.items);
    setOptions(metricSelect, interfaces.items.map(item => ({value:item.interface_id,
      label:`${item.interface_id} · ${item.description || '未命名接口'} · ${named(item.data_source)}`})), preferredInterface, item => item.label);
    await Promise.all([loadPingTrend(pingSelect.value), loadMetricTrend(metricSelect.value)]);
    if (!isCurrent()) return;
    if (topologyResult.status === 'fulfilled') {
      renderTopology(topologyResult.value);
    } else {
      failures.push('逻辑拓扑');
      failed('topology-summary', topologyResult.reason);
      $('topology-diagram').replaceChildren();
      $('topology-edges').replaceChildren();
    }
    const select = $('traffic-interface'), current = select.value; select.replaceChildren();
    if (!interfaces.items.length) {
      const option = document.createElement('option'); option.value = '';
      option.textContent = interfacesResult.status === 'rejected' ? '接口读取失败，请刷新重试' : '暂无接口样本';
      select.append(option);
    }
    for (const x of interfaces.items) { const option = document.createElement('option'); option.value = x.interface_id; option.textContent = `${x.interface_id} · ${x.description || '未命名接口'}`; select.append(option); }
    select.value = interfaces.items.some(x => x.interface_id === current) ? current : preferredObservation(interfaces.items) || '';
    $('view-updated').textContent = `视图更新：${localTime(new Date().toISOString())}（北京时间）${failures.length ? ' · 部分失败' : ''}`;
    $('notice').textContent = failures.length
      ? `部分数据读取失败：${failures.join('、')}。成功区块仍已更新，请检查对应提示后重试。`
      : ordinaryNotice;
  } catch (error) {
    if (isCurrent()) { $('notice').textContent = `读取失败：${error.message}`; $('view-updated').textContent = '本次刷新失败，请重试；已显示的记录可能是历史数据。'; }
  } finally {
    if (isCurrent()) $('refresh').disabled = false;
  }
}

$('config-diff-device').addEventListener('change', event => loadSnapshotChoices(event.target.value));
$('fault-device').addEventListener('change', event => loadFaultHistory(event.target.value));
$('ping-trend-device').addEventListener('change', event => loadPingTrend(event.target.value));
$('metric-trend-interface').addEventListener('change', event => loadMetricTrend(event.target.value));
$('config-diff-before').addEventListener('change', updateSnapshotDiffControls);
$('config-diff-after').addEventListener('change', updateSnapshotDiffControls);
$('config-diff-form').addEventListener('submit', async event => {
  event.preventDefault();
  const beforeId = $('config-diff-before').value;
  const afterId = $('config-diff-after').value;
  if (!beforeId || !afterId || beforeId === afterId) {
    $('config-diff-note').textContent = '请选择两个不同的快照。';
    return;
  }
  $('config-diff-submit').disabled = true;
  $('config-diff-summary').textContent = '正在比较配置快照…';
  $('config-diff-changes').replaceChildren();
  try {
    const params = new URLSearchParams({before_id: beforeId, after_id: afterId});
    const result = await api('/config-diff?' + params);
    $('config-diff-summary').textContent = result.changes.length
      ? `${result.device_id}：快照 #${result.before_id} → #${result.after_id}，${result.changes.length} 处变化；来源 ${named(result.data_source)}。`
      : `${result.device_id}：快照 #${result.before_id} → #${result.after_id}，没有配置变化；来源 ${named(result.data_source)}。`;
    if (result.incomplete_sections?.length) $('config-diff-summary').textContent += ` 未完整采样的部分（${result.incomplete_sections.join('、')}）未比较。`;
    table('config-diff-changes', ['类别', '标识', '变化', '变更前', '变更后'], result.changes.map(item => [
      {interfaces:'接口',routes:'路由',addresses:'IP/掩码'}[item.kind] || item.kind, item.id,
      {added: '新增', removed: '删除', changed: '变化'}[item.change] || item.change,
      item.before === null ? '—' : JSON.stringify(item.before),
      item.after === null ? '—' : JSON.stringify(item.after)
    ]));
  } catch (error) {
    $('config-diff-summary').textContent = `比较失败：${error.message}`;
    table('config-diff-changes', [], []);
  } finally {
    updateSnapshotDiffControls();
  }
});

$('traffic-form').addEventListener('submit', async event => {
  event.preventDefault(); $('traffic-csv').hidden = true;
  const direction = $('traffic-direction').value;
  $('pair-traffic-csv').hidden = true;
  let params;
  try {
    const start = new Date($('traffic-from').value + '+08:00'), end = new Date($('traffic-to').value + '+08:00');
    if (!Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) throw new Error('请填写有效且起始早于结束的北京时间。');
    params = new URLSearchParams({interface_id: $('traffic-interface').value, from:start.toISOString(), to:end.toISOString(), direction});
  } catch (error) { $('traffic-result').textContent = `统计失败：${error.message}`; $('pair-traffic').replaceChildren(); return; }
  try {
    const x = await api('/traffic?' + params);
    const directionLabel = {in:'仅入方向',out:'仅出方向',both:'入方向与出方向'}[direction] || direction;
    const totals = direction === 'in' ? `入方向：${show(x.in_bytes)} byte`
      : direction === 'out' ? `出方向：${show(x.out_bytes)} byte`
        : `入方向：${show(x.in_bytes)} byte\n出方向：${show(x.out_bytes)} byte`;
    const a = x.attribution;
    const mapping = a ? `\n观测设备：${show(a.device_id)}\n子网：${a.networks.join('、') || '未确认'}（${named(a.mapping_status)}）\n入方向：${a.in_direction}；出方向：${a.out_direction}\n${a.note}` : '';
    $('traffic-result').textContent = `观测接口：${x.observation_point || x.interface_id}\n时间窗：${localTime(x.from)} 至 ${localTime(x.to)}\n方向：${directionLabel}\n${totals}\n覆盖率：${x.coverage_pct}%\n完整窗口：${x.complete ? '是' : '否'}\n无效区间：${x.invalid_intervals.length}\n来源：${named(x.data_source)}\n${x.note}${mapping}`;
    $('traffic-csv').href = base + '/traffic/export.csv?' + params; $('traffic-csv').hidden = false;
  } catch (error) { $('traffic-result').textContent = `统计失败：${error.message}`; }
  $('pair-traffic-csv').hidden = true;
  const pairParams = new URLSearchParams({from:params.get('from'), to:params.get('to')});
  try {
    const pairs = await api('/traffic/pairs?' + pairParams);
    table('pair-traffic', ['客户端 → 服务端', '接收字节', '成功任务数', '任务依据', '来源'],
      pairs.items.map(item => [`${item.client_device_id} → ${item.server_device_id}`, item.received_bytes,
        item.task_count, item.task_ids.join('、'), badge(item.data_source)]));
    $('pair-traffic-note').textContent = pairs.note || '仅统计成功 iperf3 测试传输量。';
    $('pair-traffic-csv').href = base + '/traffic/pairs/export.csv?' + pairParams;
    $('pair-traffic-csv').hidden = false;
  } catch (error) { failed('pair-traffic-note', error); $('pair-traffic').replaceChildren(); }
});
$('collect').addEventListener('click', async () => {
  const reason = networkActionReason('collect');
  if (reason) { $('operation-result').textContent = reason; return; }
  $('collect').disabled = true; $('operation-result').textContent = '正在采集…';
  try {
    const result = await api('/collect', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:'{}'});
    $('operation-result').textContent = `采集 #${result.run_id}：${named(result.state)}`; await refresh();
  } catch (error) { $('operation-result').textContent = `采集失败：${error.message}`; }
  finally { $('collect').disabled = Boolean(networkActionReason('collect')); }
});
$('iperf-form').addEventListener('submit', async event => {
  event.preventDefault();
  const reason = networkActionReason('iperf');
  if (reason) { $('iperf-result').textContent = reason; return; }
  $('iperf-result').textContent = '正在创建任务…';
  try {
    const task = await api('/tasks/iperf3', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:JSON.stringify({client_device_id:$('iperf-client').value,
        server_device_id:$('iperf-server').value,duration_s:2,target_bps:10000000})});
    $('iperf-result').textContent = `任务 ${task.task_id}：${named(task.task_state)}。启用自动刷新时会更新结果，也可点击“刷新数据”。`; await refresh();
  } catch (error) { $('iperf-result').textContent = `任务创建失败：${error.message}`; }
});
$('baseline-form').addEventListener('submit', async event => {
  event.preventDefault(); $('security-result').textContent = '正在保存基线…';
  try {
    await api('/service-baselines', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:JSON.stringify({device_id:$('baseline-device').value,
        port:Number($('baseline-port').value),allowed:$('baseline-allowed').value === 'true',
        note:$('baseline-note').value.trim()})});
    baselineInitialized = false;
    $('security-result').textContent = '基线已保存。'; await refresh();
  } catch (error) { $('security-result').textContent = `保存失败：${error.message}`; }
});
$('baseline-device').addEventListener('change', syncBaselineForm);
$('baseline-port').addEventListener('change', syncBaselineForm);
$('probe-service').addEventListener('click', async () => {
  const reason = networkActionReason('probe');
  if (reason) { $('security-result').textContent = reason; return; }
  $('security-result').textContent = '正在检查指定端口…';
  try {
    const result = await api('/security/probe', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:JSON.stringify({device_id:$('baseline-device').value,
        port:Number($('baseline-port').value)})});
    const stateLabels = {open:'开放',closed:'关闭（连接被拒绝）',unavailable:'未知（连接未能确认端口状态）'};
    $('security-result').textContent = `端口检查：${stateLabels[result.state] || result.state}。${result.state === 'unavailable' ? '不可达不表示端口已关闭。' : ''}`;
    await refresh();
  } catch (error) { $('security-result').textContent = `检查失败：${error.message}`; }
});
$('ack-risk').addEventListener('click', async () => {
  const id = Number($('risk-id').value);
  if (!Number.isInteger(id) || id < 1) { $('security-result').textContent = '请填写有效的风险编号。'; return; }
  try {
    const risk = await api(`/risks/${id}/acknowledge`, {method:'POST', headers:{'X-Admin-Token':$('admin-token').value}});
    $('security-result').textContent = `风险 #${id}：${named(risk.state)}`; await refresh();
  } catch (error) { $('security-result').textContent = `确认失败：${error.message}`; }
});
$('refresh').addEventListener('click', refresh);
if (globalThis.NmsAuth) globalThis.NmsAuth.ready.then(() => {
  if (currentHealth) updateNetworkControls(currentHealth);
});
refresh();
