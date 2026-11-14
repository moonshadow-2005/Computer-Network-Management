const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/auth.js','utf8');

async function scenario(stage, method) {
  const timers = new Map(); let next=0, requests=0, signal, deadline;
  const context = {AbortController,document:{getElementById:()=>null},
    setTimeout:(fn,ms)=>{deadline=ms;timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id),
    fetch:async (url,options)=>{
      if(url.includes('/auth/status')) return {ok:false,headers:{get:()=>null},json:async()=>({})};
      requests++;signal=options.signal;
      const stall=()=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true}));
      if(stage==='headers') return stall();
      return {ok:true,json:()=>stage==='body'?stall():Promise.resolve({status:'ok',data:42})};
    }};
  vm.runInNewContext(source,context);
  await context.NmsAuth.ready;
  assert.equal(timers.size,0);
  const promise=context.NmsRequest('/api/v1/example',{method,headers:{'X-NMS-Local-Auth':'1'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(deadline,method==='POST'?75000:30000,'manual collection must have time to finish');
  if(stage==='success') assert.equal((await promise).body.data,42);
  else {
    const rejected=assert.rejects(promise,method==='POST'?/操作可能已提交/:/点击刷新重试/);
    for(const fire of [...timers.values()]) fire();
    await rejected;assert(signal.aborted);
  }
  assert.equal(requests,1,'must not retry a mutation or a read automatically');
  assert.equal(timers.size,0,'must clear deadline after success or failure');
}
const watchdog=setTimeout(()=>{console.error('Timeout test did not settle');process.exitCode=1;},5000);
(async()=>{
  for(const stage of ['headers','body','success']) for(const method of ['GET','POST']) await scenario(stage,method);
  console.log('请求超时检查通过：响应头/正文停滞、读写提示、无自动重试、定时器清理。');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(watchdog));
