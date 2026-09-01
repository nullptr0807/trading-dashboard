// app.js — SPA router, route-scoped resources and utilities
const state = { market: 'US' };
window.state = state;
try { const m=(new URLSearchParams(location.search).get('market')||'US').toUpperCase(); if(['US','CN'].includes(m)) state.market=m; } catch {}

function currencySymbol(market){ return (market||state.market)==='CN'?'¥':'$'; }
function formatMoney(n,market){ if(n==null||isNaN(n))return currencySymbol(market)+'0.00';const sym=currencySymbol(market),abs=Math.abs(n);return(n<0?'-'+sym:sym)+abs.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function formatCurrency(n){return formatMoney(n,state.market);}
function formatPercent(n){return n==null||isNaN(n)?'0.00%':Number(n).toFixed(2)+'%';}
function formatDate(s){if(!s)return'—';try{const d=new Date(s);return d.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})+' '+d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});}catch{return s;}}
function animateNumber(el,target,duration,prefix=''){if(!el)return;if(matchMedia('(prefers-reduced-motion: reduce)').matches){el.textContent=prefix+Math.abs(target).toLocaleString();return;}const started=performance.now();function tick(now){const p=Math.min((now-started)/duration,1),v=target*(1-Math.pow(1-p,3));el.textContent=prefix+Math.abs(v).toLocaleString('en-US',{maximumFractionDigits:0});if(p<1)requestAnimationFrame(tick);}requestAnimationFrame(tick);}

const _tickerNameCache={US:{},CN:null};
async function loadTickerNames(market){market=(market||state.market).toUpperCase();if(_tickerNameCache[market])return _tickerNameCache[market];try{const lang=typeof getLang==='function'?getLang():'en',res=await fetch(`/api/trade/ticker-names?market=${market}&lang=${lang}`,{signal:getActiveRouteSignal()});_tickerNameCache[market]=res.ok?await res.json():{};}catch(e){if(e.name!=='AbortError')_tickerNameCache[market]={};}return _tickerNameCache[market]||{};}
function formatTicker(ticker,opts){if(!ticker)return'';const cache=_tickerNameCache[(opts&&opts.market)||state.market];if(!cache||!cache[ticker])return ticker;const meta=cache[ticker],lang=typeof getLang==='function'?getLang():'en',name=(lang==='zh'||lang==='cn')?(meta.cn||meta.en):(meta.en||meta.cn);return name?`${ticker} ${name}`:ticker;}
Object.assign(window,{formatTicker,loadTickerNames});

let _routeController=null,_routeCleanups=[];
function getActiveRouteSignal(){return _routeController&&_routeController.signal;}
function registerRouteCleanup(fn){if(typeof fn==='function')_routeCleanups.push(fn);return fn;}
function disposeActiveRoute(){if(_routeController){_routeController.abort();_routeController=null;}const cleanups=_routeCleanups.splice(0);for(const fn of cleanups.reverse()){try{fn();}catch(e){console.warn('route cleanup',e);}}}
Object.assign(window,{getActiveRouteSignal,registerRouteCleanup,disposeActiveRoute});

async function api(path,options={}){const lang=typeof getLang==='function'?getLang():'en',sep=path.includes('?')?'&':'?',url='/api'+path+sep+'lang='+lang+'&market='+encodeURIComponent(state.market);const init={...options};if(!init.signal)init.signal=getActiveRouteSignal();const res=await fetch(url,init);if(!res.ok)throw new Error(`API ${res.status}`);return res.json();}

const ROUTE_ASSETS={
  '/live-account':['static/js/live_account.js?v=19'],
  '/backtest':['static/js/backtest.js?v=18'],
  '/factor-lab':['https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css','static/vendor/katex/katex.min.js?v=0.16.10','static/vendor/katex/auto-render.min.js?v=0.16.10','static/vendor/marked/marked.min.js?v=12.0.2','static/js/factor_lab.js?v=18'],
  '/symbols':['static/js/symbols.js?v=15'],
};
const _assetPromises=new Map();
function routeAssetUrl(src){const base=window.BASE||'';const absolute=/^(https?:)?\/\//.test(src)?src:`${base}/${src}`.replace(/\/+/g,'/');return encodeURI(absolute);}
function loadScript(src){if(_assetPromises.has(src))return _assetPromises.get(src);const p=new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=routeAssetUrl(src);s.async=false;s.onload=resolve;s.onerror=()=>reject(new Error(`Failed to load ${src}`));document.head.appendChild(s);});_assetPromises.set(src,p);return p;}
function loadStyle(src){if(_assetPromises.has(src))return _assetPromises.get(src);const p=new Promise((resolve,reject)=>{const l=document.createElement('link');l.rel='stylesheet';l.href=routeAssetUrl(src);l.onload=resolve;l.onerror=()=>reject(new Error(`Failed to load ${src}`));document.head.appendChild(l);});_assetPromises.set(src,p);return p;}
async function loadRouteAssets(key){for(const src of ROUTE_ASSETS[key]||[])await (/\.css(?:\?|$)/.test(src)?loadStyle(src):loadScript(src));}
window.loadRouteAssets=loadRouteAssets;

const ROUTE_HANDLERS={
  '/trade':'renderTradePage','/live-account':'renderLiveAccountPage','/backtest':'renderBacktestPage','/factor-lab':'renderFactorLabPage','/explore':'renderExplorePage','/frontier':'renderFrontierPage','/symbols':'renderSymbolsPage','/intro':'renderIntroPage'
};
let _navSeq=0,_navTimer=null;
function routeKeyFromHash(hash){if(!hash||hash==='/')return'/trade';if(/^\/explore\//.test(hash))return'/explore';if(/^\/frontier\//.test(hash))return'/frontier';if(/^\/symbols\//.test(hash))return'/symbols';return hash||'/trade';}
function isRouteCurrent(token,key){const hash=location.hash.replace('#','')||'/trade';return token===_navSeq&&routeKeyFromHash(hash)===key;}
Object.assign(window,{routeKeyFromHash,isRouteCurrent});

function navigate(){
  const hash=location.hash.replace('#','')||'/trade',key=routeKeyFromHash(hash),token=++_navSeq,app=document.getElementById('app');
  disposeActiveRoute();_routeController=new AbortController();window.__activeRouteToken=token;
  let active=null;document.querySelectorAll('.nav-link').forEach(link=>{const yes=link.getAttribute('href')==='#'+key;link.classList.toggle('active',yes);link.setAttribute('aria-current',yes?'page':'false');if(yes)active=link;});
  const strip=document.querySelector('.nav-links');if(strip&&active&&strip.scrollWidth>strip.clientWidth)active.scrollIntoView({block:'nearest',inline:'nearest'});
  if(_navTimer)clearTimeout(_navTimer);app.classList.add('fade-out');
  _navTimer=setTimeout(async()=>{if(token!==_navSeq)return;app.classList.remove('fade-out');app.classList.add('fade-in');try{await loadRouteAssets(key);if(token!==_navSeq)return;let fn,arg;if(/^\/explore\//.test(hash)){fn=window.renderExplorePost;arg=decodeURIComponent(hash.slice(9));}else if(/^\/frontier\//.test(hash)){fn=window.renderFrontierPost;arg=decodeURIComponent(hash.slice(10));}else if(/^\/symbols\//.test(hash)){fn=window.renderSymbolDetail;arg=decodeURIComponent(hash.slice(9));}else fn=window[ROUTE_HANDLERS[key]]||window.renderTradePage;if(typeof fn!=='function')throw new Error(`Route unavailable: ${key}`);await fn(arg!==undefined?arg:token,arg!==undefined?token:undefined);}catch(e){if(e.name!=='AbortError'&&token===_navSeq)app.innerHTML=`<div class="route-error" role="alert">${String(e.message||e)}</div>`;}},matchMedia('(prefers-reduced-motion: reduce)').matches?0:120);
}

function paintMarketUI(){document.querySelectorAll('.market-tab').forEach(btn=>{const active=btn.dataset.market===state.market;btn.classList.toggle('active',active);btn.setAttribute('aria-selected',String(active));btn.tabIndex=active?0:-1;});}
function setMarket(m){m=(m||'US').toUpperCase();if(!['US','CN'].includes(m)||m===state.market)return;state.market=m;try{const u=new URL(location.href);u.searchParams.set('market',m);history.replaceState(null,'',u);}catch{}paintMarketUI();navigate();}
function bindMarketTabs(){document.querySelectorAll('.market-tab').forEach(btn=>{if(btn._bound)return;btn._bound=true;btn.addEventListener('click',()=>setMarket(btn.dataset.market));});}
window.addEventListener('hashchange',navigate);
window.addEventListener('DOMContentLoaded',()=>{bindMarketTabs();paintMarketUI();if(!location.hash)location.hash='#/trade';else navigate();});
