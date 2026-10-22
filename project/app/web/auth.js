// The local gateway keeps the DEV token outside the browser.
(() => {
  const state = {local:false, available:false};
  const ready = (async () => {
    try {
      const response = await fetch('/api/v1/auth/status', {
        headers:{'X-NMS-Local-Auth':'1'}, cache:'no-store',
      });
      state.available = response.headers.get('X-NMS-Local-Gateway') === '1';
      if (response.ok && state.available) {
        const body = await response.json();
        state.local = body.status === 'ok' && body.data?.authenticated === true;
      }
    } catch { /* Direct connections retain the manual token input. */ }
    const status = document.getElementById('auth-status');
    if (status) status.textContent = state.local ? '本机管理员已连接'
      : state.available ? '本机授权暂不可用' : '手动管理授权';
    for (const id of ['admin-token','detail-token']) {
      const input = document.getElementById(id);
      if (input && state.available) input.closest('label').hidden = true;
    }
    const help = document.getElementById('auth-help');
    if (help) help.textContent = state.local
      ? '已使用这台电脑保存的加密凭据自动授权，可以直接执行管理操作。'
      : state.available ? '本机授权暂不可用，请检查 DEV 连接；若令牌已变更，请更新本机加密凭据后重新连接。'
      : '管理操作需要当前服务的令牌；令牌仅保留在本页，刷新或跳转后需重新填写。';
    return state.local;
  })();
  globalThis.NmsAuth = {
    ready,
    get local() { return state.local; },
    get available() { return state.available; },
    async prepare(options) {
      if (options.method?.toUpperCase() !== 'POST') return options;
      await ready;
      if (state.available && !state.local) throw new Error('本机授权暂不可用，请检查连接或更新本机加密凭据。');
      if (!state.local) return options;
      const headers = {...options.headers, 'X-NMS-Local-Auth':'1'};
      delete headers['X-Admin-Token'];
      return {...options, headers};
    },
  };
})();
