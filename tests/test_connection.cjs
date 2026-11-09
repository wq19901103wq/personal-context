// Run with: node --test tests/test_connection.cjs
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const source=readFileSync(require('node:path').join(__dirname,'../presentation/connection.js'),'utf8');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};

function environment(fetch){
  let next=0;
  const timers=new Map();
  const eventTarget=()=>({listeners:{},addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);},
    async dispatch(name){for(const fn of this.listeners[name]||[])fn();await flush();}});
  const window=eventTarget(),document=Object.assign(eventTarget(),{hidden:false}),navigator={onLine:true};
  const context=vm.createContext({window,document,navigator,fetch,AbortController,
    setTimeout(fn,delay){const id=++next;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);}});
  vm.runInContext(source+'\nglobalThis.connection=pageConnection;',context);
  return {connection:context.connection,window,document,navigator,timers,
    delays:()=>[...timers.values()].map(t=>t.delay),
    async fire(){assert.equal(timers.size,1);const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();await flush();}};
}
const json=(value={},status=200)=>({ok:status<400,status,headers:new Headers({'Content-Type':'application/json'}),json:async()=>value});

test('a stalled read aborts after 15s and releases its timer',async()=>{
  const env=environment((url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')))));
  const read=env.connection.request('/data');
  const rejected=assert.rejects(read,/自动重连/);
  assert.deepEqual(env.delays(),[15000]);
  await env.fire();await rejected;
  assert.equal(env.timers.size,0);
});

test('proxy HTML and invalid JSON produce a useful reconnect error',async()=>{
  for(const response of [
    {ok:false,status:502,headers:new Headers({'Content-Type':'text/html'})},
    {...json(),json:async()=>{throw new SyntaxError('invalid JSON');}},
    json({error:'internal'},503),
  ]){
    const env=environment(async()=>response);
    await assert.rejects(env.connection.request('/data'),/自动重连/);
    assert.equal(env.timers.size,0);
  }
});

test('an uncertain write is sent once and is never replayed',async()=>{
  let calls=0;
  const env=environment(async(url,options)=>{calls++;assert.equal(options.method,'POST');assert.equal(options.body,'{"status":"done"}');throw new TypeError('disconnected');});
  await assert.rejects(env.connection.request('/data',{status:'done'}),/先刷新查看结果/);
  await env.window.dispatch('online');
  assert.equal(calls,1);assert.equal(env.timers.size,0);
});

test('normal API validation errors are preserved',async()=>{
  const env=environment(async()=>json({error:'请填写标题'},400));
  await assert.rejects(env.connection.request('/data',{title:''}),/请填写标题/);
  assert.equal(env.timers.size,0);
});

test('refreshes share one active read and one trailing read, awaited by callers',async()=>{
  const env=environment(),first=deferred(),second=deferred();
  let calls=0,active=0,maxActive=0,finished=false;
  const refresh=env.connection.poll(async()=>{calls++;maxActive=Math.max(maxActive,++active);await (calls===1?first:second).promise;active--;},{onError:assert.fail});
  const original=refresh(),trailing=refresh(),alsoTrailing=refresh();
  assert.equal(trailing,alsoTrailing);
  trailing.then(()=>finished=true);
  await flush();assert.equal(calls,1);assert.equal(env.timers.size,0);
  first.resolve();await original;await flush();
  assert.equal(calls,2);assert.equal(finished,false);assert.equal(maxActive,1);
  second.resolve();await trailing;
  assert.equal(finished,true);assert.deepEqual(env.delays(),[8000]);
});

test('failed reads back off to 60s; a successful read resets the interval',async()=>{
  const env=environment();let fails=true,errors=0;
  const refresh=env.connection.poll(()=>{if(fails)throw Error('down');},{onError:()=>errors++});
  await refresh();assert.deepEqual(env.delays(),[16000]);
  await env.fire();assert.deepEqual(env.delays(),[32000]);
  await env.fire();assert.deepEqual(env.delays(),[60000]);
  await env.fire();assert.deepEqual(env.delays(),[60000]);
  assert.equal(errors,4);
  fails=false;await env.fire();assert.deepEqual(env.delays(),[8000]);
});

test('offline and hidden pages pause polling; returning to the page resumes immediately',async()=>{
  const env=environment();let calls=0,errors=0;
  const refresh=env.connection.poll(()=>{calls++;},{onError:()=>errors++});
  await refresh();assert.equal(calls,1);
  env.navigator.onLine=false;await env.window.dispatch('offline');
  assert.equal(env.timers.size,0);await refresh();assert.equal(calls,1);
  env.document.hidden=true;env.navigator.onLine=true;await env.window.dispatch('online');
  assert.equal(calls,1);
  env.document.hidden=false;await env.document.dispatch('visibilitychange');
  assert.equal(calls,2);assert.deepEqual(env.delays(),[8000]);
  env.document.hidden=true;await env.document.dispatch('visibilitychange');
  assert.equal(env.timers.size,0);
  env.document.hidden=false;await env.document.dispatch('visibilitychange');
  assert.equal(calls,3);
  env.navigator.onLine=false;await env.window.dispatch('offline');
  env.navigator.onLine=true;await env.window.dispatch('online');
  assert.equal(calls,4);assert.ok(errors>0);
});
