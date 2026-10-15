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

async function api(path, options = {}) {
  const response = await fetch(base + path, options);
  const body = await response.json();
  if (!response.ok || body.status !== 'ok') throw new Error(body.error?.message || `HTTP ${response.status}`);
  return body.data;
}
function localTime(value) { return value ? new Date(value).toLocaleString('zh-CN') : '未知'; }
function show(value) { return value === null || value === undefined || value === '' ? '未知' : String(value); }
function badge(value) {
  const span = document.createElement('span'); span.className = 'badge ' + String(value || '').replace(/[^a-z]/g, '');
  span.textContent = show(value); return span;
}
function table(id, headers, rows) {
  const root = $(id); root.replaceChildren();
  if (!rows.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = '暂无记录'; root.append(p); return; }
  const t = document.createElement('table'), head = document.createElement('thead'), tr = document.createElement('tr');
  for (const h of headers) { const th = document.createElement('th'); th.textContent = h; tr.append(th); } head.append(tr); t.append(head);
  const body = document.createElement('tbody');
  for (const cells of rows) {
    const row = document.createElement('tr');
    for (const value of cells) { const td = document.createElement('td'); td.append(value instanceof Node ? value : document.createTextNode(show(value))); row.append(td); }
    body.append(row);
  }
  t.append(body); root.append(t);
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
  return result.value;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svgNode(name, attributes = {}, text = '') {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text) node.textContent = text;
  return node;
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
  return value ? new Date(value).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '未知';
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
  return sources.length ? sources.join('、') : 'none';
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
      item.valid ? badge('ok') : show(item.invalid_reason), badge(item.data_source)
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
  const params = new URLSearchParams({limit:'30'});
  if (deviceId) params.set('device_id', deviceId);
  const [eventResult, pingResult] = await Promise.allSettled([
    api('/events?' + params), api('/ping-samples?' + params)
  ]);
  if (requestId !== faultHistoryRequestId || $('fault-device').value !== deviceId) return;

  if (eventResult.status === 'fulfilled') {
    const events = eventResult.value.items;
    table('events', ['设备', '类型', '状态', '确认', '确认时间', '首次', '最近', '证据', '来源', '操作'], events.map(item => {
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
      return [item.device_id, item.type, badge(item.state),
        badge(item.acknowledged_at ? '已确认' : '待确认'), localTime(item.acknowledged_at),
        localTime(item.first_seen), localTime(item.last_seen),
        item.evidence_sample_ids.join(', '), badge(item.data_source), acknowledge];
    }));
  } else failed('events', eventResult.reason);

  if (pingResult.status === 'fulfilled') {
    const samples = pingResult.value.items;
    table('ping', ['设备', '时间', 'RTT', '丢包', '查询', '来源'], samples.map(item => [
      item.device_id, localTime(item.sampled_at), item.rtt_ms === null ? '未知' : `${item.rtt_ms.toFixed(2)} ms`,
      item.loss_pct === null ? '未知' : `${item.loss_pct}%`, badge(item.query_status), badge(item.data_source)
    ]));
  } else failed('ping', pingResult.reason);

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
    label: `#${item.snapshot_id} · ${localTime(item.sampled_at)} · ${item.data_source}`
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

async function refresh() {
  const requestId = ++refreshRequestId;
  const isCurrent = () => requestId === refreshRequestId;
  $('refresh').disabled = true;
  try {
    const [healthResult, overviewResult, devicesResult, interfacesResult, routesResult, baselinesResult,
      checksResult, risksResult, runsResult, snapshotsResult, topologyResult, tasksResult] = await Promise.allSettled([
      api('/health'), api('/overview'), api('/devices'), api('/interfaces'), api('/routes'),
      api('/service-baselines'), api('/service-checks?limit=30'), api('/risks'),
      api('/collector-runs?limit=10'), api('/config-snapshots?limit=10'), api('/topology'), api('/tasks?limit=10')
    ]);
    if (!isCurrent()) return;
    const failures = [];
    let liveEnabled = false;
    if (healthResult.status === 'fulfilled') {
      liveEnabled = healthResult.value.live_enabled;
      $('health').textContent = liveEnabled ? '实时采集已启用' : '实时采集未启用';
    } else {
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
    } else {
      failures.push('概览');
      for (const id of ['count-devices', 'count-interfaces', 'count-events', 'count-risks']) $(id).textContent = '未知';
    }
    const devices = renderResource(devicesResult, '设备', 'devices', ['标识', '地址', '角色', '身份', '来源'],
      items => items.map(x => [x.device_id, x.address, x.role, x.identity_status, badge(x.data_source)]), failures);
    const interfaces = renderResource(interfacesResult, '接口', 'interfaces', ['接口', '描述', '管理/运行', '参考速率', '采样时间', '状态', '来源'],
      items => items.map(x => [x.interface_id, x.description,
      `${show(x.admin_status)} / ${show(x.oper_status)}`, x.reference_speed_bps ? `${x.reference_speed_bps} bit/s` : '未知',
      localTime(x.sampled_at), badge(x.query_status), badge(x.data_source)]), failures);
    renderResource(routesResult, '路由', 'routes', ['对象', '目的/掩码', '下一跳', '接口', '采样时间', '来源'],
      items => items.map(x => [x.device_id, `${x.destination} / ${show(x.mask)}`, x.next_hop, x.interface_id,
        localTime(x.sampled_at), badge(x.data_source)]), failures);
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
            $('iperf-result').textContent = `任务 ${x.task_id.slice(0,8)} 返回状态：${task.task_state}。`;
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
    renderResource(risksResult, '风险', 'risks', ['编号', '设备', '规则', '级别', '状态', '最近', '证据', '来源'],
      items => items.map(x => [x.risk_id, x.device_id, x.rule_id,
        x.level, badge(x.state), localTime(x.last_seen), JSON.stringify(x.evidence), badge(x.data_source)]), failures);
    renderResource(runsResult, '采集记录', 'runs', ['开始', '完成', '状态', '详情'], items => items.map(x => [localTime(x.started_at), localTime(x.finished_at),
      badge(x.state), x.details.map(y => `${y.device_id || '系统'}: ${y.status}`).join('；')]), failures);
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
    await loadSnapshotChoices(deviceSelect.value);
    if (!isCurrent()) return;
    const faultSelect = $('fault-device');
    const previousFaultDevice = faultSelect.value;
    const faultOptions = [{value:'',label:'所有设备'}, ...devices.items.map(item => ({
      value:item.device_id, label:`${item.device_id} · ${item.address}`}))];
    setOptions(faultSelect, faultOptions, previousFaultDevice, item => item.label);
    await loadFaultHistory(faultSelect.value);
    if (!isCurrent()) return;
    const pingDevices = devices.items.filter(item => item.role === 'test-host');
    const pingSelect = $('ping-trend-device');
    const previousPingDevice = pingSelect.value;
    setOptions(pingSelect, pingDevices.map(item => ({value:item.device_id,
      label:`${item.device_id} · ${item.address}`})), previousPingDevice || pingDevices[0]?.device_id, item => item.label);
    const metricSelect = $('metric-trend-interface');
    const previousInterface = metricSelect.value;
    const preferredInterface = previousInterface || interfaces.items.find(item => item.data_source === 'live')?.interface_id || interfaces.items[0]?.interface_id;
    setOptions(metricSelect, interfaces.items.map(item => ({value:item.interface_id,
      label:`${item.interface_id} · ${item.description || '未命名接口'} · ${item.data_source}`})), preferredInterface, item => item.label);
    await Promise.all([loadPingTrend(pingSelect.value), loadMetricTrend(metricSelect.value)]);
    if (!isCurrent()) return;
    if (topologyResult.status === 'fulfilled') {
      const topology = topologyResult.value;
      $('topology-summary').textContent = `${topology.nodes.length} 个节点、${topology.edges.length} 条文档逻辑关系。${topology.note}`;
    } else {
      failures.push('逻辑拓扑');
      failed('topology-summary', topologyResult.reason);
    }
    const select = $('traffic-interface'), current = select.value; select.replaceChildren();
    if (!interfaces.items.length) {
      const option = document.createElement('option'); option.value = '';
      option.textContent = interfacesResult.status === 'rejected' ? '接口读取失败，请刷新重试' : '暂无接口样本';
      select.append(option);
    }
    for (const x of interfaces.items) { const option = document.createElement('option'); option.value = x.interface_id; option.textContent = x.interface_id; select.append(option); }
    if (current && interfaces.items.some(x => x.interface_id === current)) select.value = current;
    $('notice').textContent = failures.length
      ? `部分数据读取失败：${failures.join('、')}。成功区块仍已更新，请检查对应提示后重试。`
      : ordinaryNotice;
  } catch (error) {
    if (isCurrent()) $('notice').textContent = `读取失败：${error.message}`;
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
      ? `${result.device_id}：快照 #${result.before_id} → #${result.after_id}，${result.changes.length} 处变化；来源 ${result.data_source}。`
      : `${result.device_id}：快照 #${result.before_id} → #${result.after_id}，没有配置变化；来源 ${result.data_source}。`;
    table('config-diff-changes', ['类别', '标识', '变化', '变更前', '变更后'], result.changes.map(item => [
      item.kind === 'interfaces' ? '接口' : '路由', item.id,
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
  const params = new URLSearchParams({interface_id: $('traffic-interface').value,
    from: new Date($('traffic-from').value).toISOString(), to: new Date($('traffic-to').value).toISOString(), direction});
  try {
    const x = await api('/traffic?' + params);
    const directionLabel = {in:'仅入方向',out:'仅出方向',both:'入方向与出方向'}[direction] || direction;
    const totals = direction === 'in' ? `入方向：${show(x.in_bytes)} byte`
      : direction === 'out' ? `出方向：${show(x.out_bytes)} byte`
        : `入方向：${show(x.in_bytes)} byte\n出方向：${show(x.out_bytes)} byte`;
    $('traffic-result').textContent = `观测接口：${x.observation_point || x.interface_id}\n时间窗：${localTime(x.from)} 至 ${localTime(x.to)}\n方向：${directionLabel}\n${totals}\n覆盖率：${x.coverage_pct}%\n完整窗口：${x.complete ? '是' : '否'}\n无效区间：${x.invalid_intervals.length}\n来源：${x.data_source}\n${x.note}`;
    $('traffic-csv').href = base + '/traffic/export.csv?' + params; $('traffic-csv').hidden = false;
  } catch (error) { $('traffic-result').textContent = `统计失败：${error.message}`; }
});
$('collect').addEventListener('click', async () => {
  $('collect').disabled = true; $('operation-result').textContent = '正在采集…';
  try {
    const result = await api('/collect', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:'{}'});
    $('operation-result').textContent = `采集 #${result.run_id}：${result.state}`; await refresh();
  } catch (error) { $('operation-result').textContent = `采集失败：${error.message}`; }
  finally { $('collect').disabled = false; }
});
$('iperf-form').addEventListener('submit', async event => {
  event.preventDefault(); $('iperf-result').textContent = '正在创建任务…';
  try {
    const task = await api('/tasks/iperf3', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:JSON.stringify({client_device_id:$('iperf-client').value,
        server_device_id:$('iperf-server').value,duration_s:2,target_bps:10000000})});
    $('iperf-result').textContent = `任务 ${task.task_id}：${task.task_state}。完成后刷新查看结果。`; await refresh();
  } catch (error) { $('iperf-result').textContent = `任务创建失败：${error.message}`; }
});
$('baseline-form').addEventListener('submit', async event => {
  event.preventDefault(); $('security-result').textContent = '正在保存基线…';
  try {
    await api('/service-baselines', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token':$('admin-token').value}, body:JSON.stringify({device_id:$('baseline-device').value,
        port:Number($('baseline-port').value),allowed:$('baseline-allowed').value === 'true',
        note:$('baseline-note').value.trim()})});
    $('security-result').textContent = '基线已保存。'; await refresh();
  } catch (error) { $('security-result').textContent = `保存失败：${error.message}`; }
});
$('baseline-device').addEventListener('change', syncBaselineForm);
$('baseline-port').addEventListener('change', syncBaselineForm);
$('probe-service').addEventListener('click', async () => {
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
    $('security-result').textContent = `风险 #${id}：${risk.state}`; await refresh();
  } catch (error) { $('security-result').textContent = `确认失败：${error.message}`; }
});
$('refresh').addEventListener('click', refresh);
refresh();
