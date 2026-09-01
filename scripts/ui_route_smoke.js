#!/usr/bin/env node
// CDP smoke test for all SPA routes; requires Chrome on port 9222.
const CDP = process.env.CDP || 'http://127.0.0.1:9222';
const ORIGIN = process.env.DASHBOARD_URL || 'http://127.0.0.1:8501/';
async function main() {
  const target = await (await fetch(`${CDP}/json/new?${encodeURIComponent(ORIGIN)}`, {method:'PUT'})).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0; const waiting=new Map(), exceptions=[];
  ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&waiting.has(m.id)){waiting.get(m.id)(m);waiting.delete(m.id);}if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails.text);};
  const call=(method,params={})=>new Promise(resolve=>{const n=++id;waiting.set(n,resolve);ws.send(JSON.stringify({id:n,method,params}));});
  await call('Runtime.enable'); await call('Page.enable'); await call('Network.setCacheDisabled',{cacheDisabled:true});
  const routes=['trade','live-account','backtest','factor-lab','explore','frontier','symbols','intro','symbols/ACM'];
  const results=[];
  for(const route of routes){
    exceptions.length=0;
    await call('Page.navigate',{url:`${ORIGIN}?smoke=${Date.now()}#/${route}`});
    await new Promise(r=>setTimeout(r,2500));
    const result=await call('Runtime.evaluate',{expression:`JSON.stringify({hash:location.hash,error:document.querySelector('.route-error')?.textContent||'',text:(document.querySelector('#app')?.innerText||'').slice(0,120),katexCss:[...document.styleSheets].some(s=>(s.href||'').includes('katex'))})`,returnByValue:true});
    const value=JSON.parse(result.result.result.value);
    results.push({route,...value,exceptions:[...exceptions]});
  }
  console.log(JSON.stringify(results,null,2));
  ws.close(); await fetch(`${CDP}/json/close/${target.id}`);
  if(results.some(x=>x.error||x.exceptions.length||!x.text))process.exitCode=1;
}
main().catch(e=>{console.error(e);process.exit(1);});
