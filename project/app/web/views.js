// Hash navigation retains unsaved forms and manual authorization in this document.
(() => {
  const views = ['overview','fault','config','performance','accounting','security','operations'];
  const titles = ['概览','故障管理','配置管理','性能管理','计费管理','安全管理','运行与采集'];
  function navigate() {
    const requested = location.hash.slice(1);
    const active = views.includes(requested) ? requested : 'overview';
    for (const id of views) $(id).hidden = id !== active;
    for (const link of document.querySelectorAll('nav a')) {
      if (link.hash === '#' + active) link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    }
    document.title = `${titles[views.indexOf(active)]} · 第 2 组网络管理`;
    window.scrollTo(0,0);
  }
  window.addEventListener('hashchange', navigate);
  navigate();
  function quickWindow(minutes) {
    const end = Date.now();
    const beijing = value => new Date(value + 8 * 3600000).toISOString().slice(0,19);
    $('traffic-from').value = beijing(end - minutes * 60000);
    $('traffic-to').value = beijing(end);
  }
  quickWindow(15);
  $('traffic-15m').addEventListener('click', () => quickWindow(15));
  $('traffic-1h').addEventListener('click', () => quickWindow(60));
  // Do not interrupt typing, selection, manual requests, hidden tabs or history reading.
  setInterval(() => {
    if (!$('auto-refresh').checked || document.hidden || $('refresh').disabled) return;
    if (['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName)) return;
    refresh(true);
  }, 30000);

  let historyOffset = 0, historyTotal = 0, historyRequest = 0;
  async function loadHistory() {
    const requestId = ++historyRequest;
    $('address-prev').disabled = $('address-next').disabled = true;
    $('address-position').textContent = '正在查询历史…';
    const params = new URLSearchParams({limit:'10',offset:String(historyOffset)});
    if ($('address-history-device').value) params.set('device_id', $('address-history-device').value);
    try {
      const data = await api('/interface-addresses?' + params);
      if (requestId !== historyRequest) return;
      historyTotal = data.total;
      tablePages.set('address-history',0);
      table('address-history',['发现编号','接口','IP','掩码','采样时间','来源'],data.items.map(x => [
        x.discovery_id,x.interface_id,x.address,x.mask,localTime(x.sampled_at),badge(x.data_source)]));
      $('address-position').textContent = data.total ? `${historyOffset+1}–${Math.min(historyOffset+10,data.total)} / ${data.total} 条历史记录` : '无历史记录';
      $('address-prev').disabled = historyOffset === 0;
      $('address-next').disabled = historyOffset + 10 >= historyTotal;
    } catch (error) {
      if (requestId !== historyRequest) return;
      failed('address-history',error); $('address-position').textContent = '查询失败，请重试';
    }
  }
  $('address-history-panel').addEventListener('toggle', async () => {
    if (!$('address-history-panel').open) return;
    if (!$('address-history-device').options.length) {
      try {
        const data = await api('/devices?limit=500');
        setOptions($('address-history-device'),[{value:'',label:'所有设备'},...data.items.map(x => ({value:x.device_id,label:`${x.device_id} · ${x.address}`}))], '', x=>x.label);
      } catch (error) { failed('address-history',error); return; }
    }
    loadHistory();
  });
  $('address-history-refresh').addEventListener('click', () => { historyOffset=0; loadHistory(); });
  $('address-history-device').addEventListener('change', () => { historyOffset=0; loadHistory(); });
  $('address-prev').addEventListener('click', () => { historyOffset=Math.max(0,historyOffset-10); loadHistory(); });
  $('address-next').addEventListener('click', () => { if (historyOffset+10<historyTotal) { historyOffset+=10; loadHistory(); } });
})();
