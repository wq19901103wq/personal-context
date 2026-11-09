'use strict';

const pageConnection=(()=>{
  const unavailable='连接暂时中断，会自动重连。';
  const unconfirmed='未收到操作确认，请先刷新查看结果，再决定是否重试。';

  async function request(url,data){
    const writing=data!==undefined;
    if(!navigator.onLine)throw Error('网络已断开，联网后会自动同步。');
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),15000);
    try{
      const options={signal:controller.signal,cache:'no-store'};
      if(writing)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      const response=await fetch(url,options);
      if(!response.headers.get('Content-Type')?.includes('application/json'))throw Error(writing?unconfirmed:unavailable);
      const result=await response.json();
      if(!response.ok)throw Error(response.status>=500?(writing?unconfirmed:unavailable):(result.error||'操作失败'));
      return result;
    }catch(error){
      if(['AbortError','TypeError','SyntaxError'].includes(error.name))throw Error(writing?unconfirmed:unavailable);
      throw error;
    }finally{clearTimeout(timer);}
  }

  // Only read operations belong in this loop. Writes are never replayed.
  function poll(update,{interval=8000,onError}={}){
    let timer=null,running=null,queued=null,failures=0;
    function schedule(){
      clearTimeout(timer);
      if(!document.hidden&&navigator.onLine)timer=setTimeout(refresh,Math.min(interval*2**Math.min(failures,6),60000));
    }
    function refresh(){
      clearTimeout(timer);
      if(running){
        // A save or explicit refresh during a read needs one fresh read afterwards.
        if(!queued)queued=running.then(()=>{queued=null;return refresh();});
        return queued;
      }
      if(!navigator.onLine){onError(Error('网络已断开，联网后会自动同步。'));return Promise.resolve(false);}
      running=Promise.resolve().then(update)
        .then(()=>{failures=0;return true;},error=>{failures++;onError(error);return false;})
        .finally(()=>{running=null;schedule();});
      return running;
    }
    window.addEventListener('offline',()=>{clearTimeout(timer);onError(Error('网络已断开，联网后会自动同步。'));});
    window.addEventListener('online',()=>{if(!document.hidden)refresh();});
    document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else refresh();});
    return refresh;
  }
  return {request,poll};
})();
