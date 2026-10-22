const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/auth.js', 'utf8');

async function check(authenticated, marker, unavailable = false) {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, {textContent:'',label:{hidden:false},closest() {return this.label;}});
    return nodes.get(id);
  };
  const requests = [];
  const context = {document:{getElementById:get}, fetch:async (path, options) => {
    requests.push({path, options});
    if (unavailable) throw new Error('unavailable');
    return {ok:authenticated, headers:{get:() => marker},
      json:async () => ({status:'ok',data:{authenticated}})};
  }};
  vm.runInNewContext(source, context);
  const expected = authenticated && marker === '1' && !unavailable;
  assert.equal(await context.NmsAuth.ready, expected);
  const available = marker === '1' && !unavailable;
  assert.equal(get('admin-token').label.hidden, available);
  assert.equal(get('detail-token').label.hidden, available);
  if (available && !expected) {
    await assert.rejects(context.NmsAuth.prepare({method:'POST'}),/本机授权暂不可用/);
    return;
  }
  const options = await context.NmsAuth.prepare({method:'POST',headers:{'X-Admin-Token':'manual-value'}});
  if (expected) {
    assert.equal(options.headers['X-NMS-Local-Auth'],'1');
    assert.equal(options.headers['X-Admin-Token'],undefined);
    assert.match(get('auth-status').textContent,/本机管理员已连接/);
  } else {
    assert.equal(options.headers['X-Admin-Token'],'manual-value');
  }
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.headers['X-NMS-Local-Auth'],'1');
}
(async () => {
  await check(true,'1');
  await check(true,null);
  await check(false,null);
  await check(false,'1');
  await check(false,null,true);
  console.log('自动授权前端检查通过：凭据不进入页面、授权状态显示、直连与故障回退。');
})().catch(error => {console.error(error);process.exitCode=1;});
