// live_account.js — real-money Moomoo OpenD account module
(()=>{
let _laPreview = null;
let _laReadToken = '';
let _laChart = null;

function laLang() { return (typeof getLang === 'function' && getLang() === 'zh') ? 'zh' : 'en'; }
function laT(zh, en) { return laLang() === 'zh' ? zh : en; }
function laEsc(v) { const d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }
function laNum(row, ...keys) {
  for (const k of keys) { const n = Number(row && row[k]); if (Number.isFinite(n)) return n; }
  return 0;
}
function laMoney(v) { return '$' + Number(v || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function laPct(v) { const n = Number(v || 0); return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`; }
function laClass(v) { return Number(v) > 0 ? 'la-positive' : Number(v) < 0 ? 'la-negative' : '' ; }
async function laFetch(path, options) {
  options = options ? {...options} : {};
  options.headers = {...(options.headers || {})};
  if (_laReadToken) options.headers['X-Moomoo-Read-Token'] = _laReadToken;
  const res = await fetch('/api/live-account' + path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `API ${res.status}`);
  return body;
}

function laStatusItem(ok, label, detail) {
  return `<div class="la-check ${ok ? 'ok' : 'off'}"><span>${ok ? '✓' : '×'}</span><div><b>${laEsc(label)}</b><small>${laEsc(detail || '')}</small></div></div>`;
}

function laPolicyCard(policy) {
  const rows = [
    [laT('策略', 'Strategy'), policy.strategy_id],
    [laT('目标持仓', 'Target positions'), `${policy.top_n} ${laT('只', 'stocks')}`],
    [laT('单股目标', 'Target per position'), `${(policy.position_target_pct*100).toFixed(1)}%`],
    [laT('总目标敞口', 'Target gross exposure'), `${(policy.gross_target_pct*100).toFixed(0)}%`],
    [laT('固定止损', 'Fixed stop'), `${(policy.stop_loss_pct*100).toFixed(1)}%`],
    [laT('止损冷却', 'Stop cooldown'), `${policy.stop_cooldown_hours}h`],
    [laT('最小持仓', 'Minimum hold'), `${policy.min_hold_days}${laT('日', 'd')}`],
    [laT('持有缓冲', 'Hold band'), `Top ${policy.hold_rank_max}`],
    [laT('再平衡基准', 'Rebalance base'), `${policy.rebalance_hours}h`],
    [laT('固定止盈', 'Take profit'), policy.take_profit_pct == null ? laT('无', 'None') : laPct(policy.take_profit_pct*100)],
    [laT('移动止损', 'Trailing stop'), policy.trailing_stop_pct == null ? laT('关闭', 'Off') : laPct(policy.trailing_stop_pct*100)],
    [laT('订单类型', 'Order type'), laT('仅限价单', 'Limit only')],
    [laT('单笔上限', 'Max order'), laMoney(policy.max_order_notional)],
    [laT('每日订单上限', 'Daily order limit'), laMoney(policy.max_daily_order_notional)],
    [laT('价格偏离上限', 'Max price deviation'), `${(policy.max_limit_deviation_pct*100).toFixed(1)}%`],
    [laT('行情最大年龄', 'Max quote age'), `${policy.max_quote_age_seconds}s`],
    [laT('盘前盘后成交', 'Extended-hours fills'), policy.fill_outside_rth ? laT('允许','Allowed') : laT('禁止','Blocked')],
    [laT('卖空/融资', 'Short / margin'), laT('禁止', 'Blocked')],
    [laT('订单/成交历史', 'Order / deal history'), `${policy.activity_lookback_days}d`],
  ];
  return `<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">EXECUTION POLICY</span><h2>${laT('真实交易参数', 'Live trading policy')}</h2></div><span class="la-source">Moomoo + server guardrails</span></div><div class="la-policy-grid">${rows.map(r=>`<div><small>${laEsc(r[0])}</small><b>${laEsc(r[1])}</b></div>`).join('')}</div><p class="la-note">${laT('这些是服务器当前准备采用的实盘参数；自动交易开关与手工下单开关彼此独立。', 'These are the server-side live policy values. Automated execution and manual ordering have independent safety switches.')}</p></section>`;
}

function laSetup(status) {
  return `<section class="la-panel la-setup"><div class="la-section-head"><div><span class="la-kicker">CONNECTION</span><h2>${laT('连接Moomoo OpenD', 'Connect Moomoo OpenD')}</h2></div></div>
  <div class="la-check-grid">
    ${laStatusItem(status.sdk_installed, laT('官方Python SDK','Official Python SDK'), status.sdk_installed ? laT('已安装','Installed') : laT('未安装','Missing'))}
    ${laStatusItem(status.opend_connected, 'Moomoo OpenD', status.opend_connected ? laT('已连接','Connected') : (status.message || '127.0.0.1:11111'))}
    ${laStatusItem(status.real_account_selected, laT('真实账户','Real account'), status.account_id ? `ID ${status.account_id}` : laT('尚未选择','Not selected'))}
    ${laStatusItem(status.trade_token_configured, laT('交易授权令牌','Trade authorization token'), status.trade_token_configured ? laT('已配置','Configured') : laT('未配置；只能读取','Missing; read-only'))}
    ${laStatusItem(status.read_token_configured, laT('账户读取授权','Account read authorization'), status.read_access_granted ? laT('本次页面已授权','Authorized for this page') : status.read_token_configured ? laT('需要输入读取令牌','Token required') : laT('服务器尚未配置','Not configured on server'))}
    ${laStatusItem(status.unlock_secret_configured, laT('交易解锁密文','Trade unlock secret'), status.unlock_secret_configured ? laT('已配置','Configured') : laT('未配置；无法下单','Missing; ordering blocked'))}
    ${laStatusItem(status.trading_enabled, laT('真实下单总开关','Real-order master switch'), status.trading_enabled ? laT('已开启','Enabled') : laT('关闭（安全默认）','Off — safe default'))}
  </div>
  <div class="la-guide"><b>${laT('服务器配置步骤','Server setup')}</b><ol>
    <li>${laT('在受保护的本机或内网主机安装并登录Moomoo OpenD。','Install and sign in to Moomoo OpenD on a protected local/private host.')}</li>
    <li><code>MOOMOO_SECURITY_FIRM=FUTUAU</code> · <code>MOOMOO_ACCOUNT_ID=...</code></li>
    <li>${laT('先保持','Keep')} <code>MOOMOO_TRADING_ENABLED=false</code> ${laT('验证账户、持仓和行情。','while validating account, positions and quotes.')}</li>
    <li>${laT('交易令牌和解锁MD5只能放在服务器环境变量，不能输入网页或提交Git。','Keep the trade token and unlock MD5 in server environment only—never in the page or Git.')}</li>
  </ol></div></section>`;
}

function laReadAuthCard() {
  return `<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">PRIVATE DATA</span><h2>${laT('解锁真实账户只读数据','Unlock private account data')}</h2></div></div><p class="la-note">${laT('读取令牌只保留在当前页面内存，不写入浏览器存储。','The read token stays in this page memory and is not written to browser storage.')}</p><div class="la-order-form"><label>${laT('账户读取令牌','Account read token')}<input id="la-read-token" type="password" autocomplete="off"></label><button class="la-btn" id="la-read-unlock">${laT('读取Moomoo账户','Load Moomoo account')}</button></div></section>`;
}

function laMetrics(snapshot) {
  const a = snapshot.account || {};
  const nav = laNum(a,'total_assets','total_asset','net_asset');
  const cash = laNum(a,'cash','cash_balance');
  const power = laNum(a,'power','available_funds','max_power_short');
  const market = laNum(a,'market_val','market_value','securities_assets');
  const pl = (snapshot.positions || []).reduce((s,p)=>s+laNum(p,'pl_val'),0);
  const hist = snapshot.nav_history || [];
  const tracked = hist.length > 1 ? (nav / Number(hist[0].total_assets) - 1) * 100 : 0;
  return `<div class="la-metrics">
    <div class="la-metric hero"><small>${laT('总资产净值','Net liquidation value')}</small><b>${laMoney(nav)}</b><span>${laT('真实账户 · USD','Real account · USD')}</span></div>
    <div class="la-metric"><small>${laT('现金','Cash')}</small><b>${laMoney(cash)}</b><span>${nav? (cash/nav*100).toFixed(1)+'%':''}</span></div>
    <div class="la-metric"><small>${laT('证券市值','Market value')}</small><b>${laMoney(market)}</b><span>${nav? (market/nav*100).toFixed(1)+'%':''}</span></div>
    <div class="la-metric"><small>${laT('购买力','Buying power')}</small><b>${laMoney(power)}</b></div>
    <div class="la-metric"><small>${laT('持仓未实现盈亏','Position unrealized P&L')}</small><b class="${laClass(pl)}">${laMoney(pl)}</b></div>
    <div class="la-metric"><small>${laT('连接后净值收益','Return since tracking')}</small><b class="${laClass(tracked)}">${hist.length>1?laPct(tracked):'—'}</b><span>${hist.length>1?`${hist.length} snapshots`:laT('等待第二个快照','Awaiting second snapshot')}</span></div>
  </div>`;
}

function laPositions(items, accountNav) {
  if (!items.length) return `<div class="la-empty">${laT('当前没有持仓','No open positions')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table"><thead><tr><th>${laT('标的','Symbol')}</th><th>${laT('数量/可卖','Qty / sellable')}</th><th>${laT('现价','Last')}</th><th>${laT('成本','Cost')}</th><th>${laT('市值','Market value')}</th><th>${laT('盈亏','P&L')}</th><th>${laT('组合占比','Weight')}</th></tr></thead><tbody>${items.map(p=>{
    const pl=laNum(p,'pl_val'), ratio=laNum(p,'pl_ratio'), mv=laNum(p,'market_val');
    return `<tr><td><b>${laEsc(p.code)}</b><small>${laEsc(p.stock_name||p.name||'')}</small></td><td>${laNum(p,'qty').toLocaleString()}<small>${laT('可卖','sell')} ${laNum(p,'can_sell_qty','qty').toLocaleString()}</small></td><td>${laMoney(laNum(p,'nominal_price','last_price'))}</td><td>${laMoney(laNum(p,'cost_price'))}</td><td>${laMoney(mv)}</td><td class="${laClass(pl)}"><b>${laMoney(pl)}</b><small>${laPct(ratio)}</small></td><td>${accountNav>0?(mv/accountNav*100).toFixed(1)+'%':'—'}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function laActivity(items, type, ready) {
  if (!items.length) return `<div class="la-empty">${type==='order'?laT('暂无订单','No orders'):laT('暂无成交','No deals')}</div>`;
  const isOrder=type==='order';
  return `<div class="la-table-wrap"><table class="la-table compact"><thead><tr><th>${laT('时间','Time')}</th><th>${laT('标的','Symbol')}</th><th>${laT('方向','Side')}</th><th>${laT('数量','Qty')}</th><th>${isOrder?laT('价格/状态','Price / status'):laT('成交价','Fill price')}</th>${isOrder?'<th></th>':''}</tr></thead><tbody>${items.slice(0,100).map(x=>`<tr><td>${laEsc(x.updated_time||x.create_time||x.create_time_str||x.deal_time||'—')}</td><td><b>${laEsc(x.code||'')}</b></td><td>${laEsc(x.trd_side||'')}</td><td>${laNum(x,'qty','deal_qty').toLocaleString()}</td><td>${laMoney(laNum(x,'price','dealt_avg_price','deal_price'))}<small>${laEsc(x.order_status||'')}</small></td>${isOrder?`<td>${ready && !['FILLED_ALL','CANCELLED_ALL','FAILED','DISABLED'].includes(String(x.order_status))?`<button class="la-link danger" data-la-cancel="${laEsc(encodeURIComponent(String(x.order_id||'')))}">${laT('撤单','Cancel')}</button>`:''}</td>`:''}</tr>`).join('')}</tbody></table></div>`;
}

function laOrderTicket(ready) {
  return `<section class="la-panel la-order-panel"><div class="la-section-head"><div><span class="la-kicker">LIMIT ORDER</span><h2>${laT('真实订单工作台','Live order ticket')}</h2></div><span class="la-risk-badge">${laT('真钱','REAL MONEY')}</span></div>
  <div class="la-order-warning">${ready?laT('下单前必须先获取Moomoo实时行情并预览；最终提交只接受完全一致且90秒内的预览。','A fresh Moomoo quote and signed preview are required; submission accepts only the exact preview within 90 seconds.'):laT('当前为只读模式。所有安全门禁满足前，服务器不会调用Moomoo下单API。','Read-only mode. The server cannot call Moomoo place-order until every safety gate passes.')}</div>
  <form id="la-order-form" class="la-order-form">
    <label>${laT('股票代码','Symbol')}<input id="la-code" value="AAPL" maxlength="16" autocomplete="off"></label>
    <label>${laT('方向','Side')}<select id="la-side"><option value="BUY">BUY</option><option value="SELL">SELL</option></select></label>
    <label>${laT('股数','Quantity')}<input id="la-qty" type="number" min="1" step="1" value="1"></label>
    <label>${laT('限价 USD','Limit USD')}<input id="la-price" type="number" min="0.01" step="0.01"></label>
    <button type="submit" class="la-btn" ${ready?'':'disabled'}>${laT('获取行情并预览','Quote & preview')}</button>
  </form><div id="la-preview"></div></section>`;
}

function laRenderChart(history) {
  const el=document.getElementById('la-nav-chart'); if(!el || !history || history.length<2 || !window.LightweightCharts) return;
  if(_laChart){try{_laChart.remove();}catch{} _laChart=null;}
  _laChart=LightweightCharts.createChart(el,{width:el.clientWidth,height:230,layout:{background:{color:'#111827'},textColor:'#9fb0c7'},grid:{vertLines:{color:'#223049'},horzLines:{color:'#223049'}},rightPriceScale:{borderColor:'#34445e'},timeScale:{borderColor:'#34445e',timeVisible:true}});
  const series=_laChart.addAreaSeries({lineColor:'#33e3a2',topColor:'rgba(51,227,162,.35)',bottomColor:'rgba(51,227,162,.02)',lineWidth:2});
  const byDay={}; history.forEach(x=>{byDay[String(x.ts).slice(0,10)]=Number(x.total_assets)});
  series.setData(Object.entries(byDay).map(([time,value])=>({time,value})));_laChart.timeScale().fitContent();
  new ResizeObserver(()=>{if(_laChart){_laChart.applyOptions({width:el.clientWidth});_laChart.timeScale().fitContent();}}).observe(el);
}

async function renderLiveAccountPage(token) {
  const app=document.getElementById('app');
  app.innerHTML=`<div class="la-shell"><section class="la-hero"><div><span class="la-kicker">MOOMOO OPENAPI · LIVE CAPITAL</span><h1>${laT('真实账户','Live Account')}</h1><p>${laT('账户、行情、持仓、订单和成交全部直接来自Moomoo OpenD；不混用模拟账本。','Account, quotes, positions, orders and deals come directly from Moomoo OpenD—never the paper ledger.')}</p></div><div class="la-hero-mark">$10K<span>${laT('最低净值门槛','MIN NAV GATE')}</span></div></section><div class="la-loading">${laT('正在检查Moomoo连接…','Checking Moomoo connection…')}</div></div>`;
  try {
    const root=await laFetch('/status'); if(token && !isRouteCurrent(token,'/live-account'))return;
    const st=root.status,p=root.policy; let snapshot=null;
    if(st.opend_connected&&st.real_account_selected&&st.read_access_granted){try{snapshot=await laFetch('/snapshot');}catch(e){st.message=e.message;}}
    if(token && !isRouteCurrent(token,'/live-account'))return;
    app.innerHTML=`<div class="la-shell"><section class="la-hero"><div><span class="la-kicker">MOOMOO OPENAPI · LIVE CAPITAL</span><h1>${laT('真实账户','Live Account')}</h1><p>${laT('账户、行情、持仓、订单和成交全部直接来自Moomoo OpenD；不混用模拟账本。','Account, quotes, positions, orders and deals come directly from Moomoo OpenD—never the paper ledger.')}</p></div><div class="la-hero-mark">$10K<span>${laT('最低净值门槛','MIN NAV GATE')}</span></div></section>
      <div class="la-livebar ${st.place_order_ready?'armed':'safe'}"><b>${st.place_order_ready?laT('真实下单已解锁','REAL ORDERS ARMED'):laT('只读安全模式','READ-ONLY SAFE MODE')}</b><span>${laEsc(st.message||`${st.security_firm} · ${st.market}`)}</span><button class="la-link" onclick="renderLiveAccountPage(window.__activeRouteToken)">${laT('刷新','Refresh')}</button></div>
      ${laSetup(st)}${(st.read_token_configured&&!st.read_access_granted)?laReadAuthCard():''}${snapshot?laMetrics(snapshot):''}
      ${snapshot?`<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">NAV HISTORY</span><h2>${laT('连接后净值轨迹','NAV since connection')}</h2></div><span class="la-source">Moomoo snapshots</span></div><div id="la-nav-chart" class="la-chart">${(snapshot.nav_history||[]).length<2?laT('收集第二个快照后显示曲线','Chart appears after a second snapshot'):''}</div></section>`:''}
      ${laPolicyCard(p)}
      ${snapshot?`<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">POSITIONS</span><h2>${laT('当前持仓','Current positions')}</h2></div><span class="la-source">${snapshot.positions.length} ${laT('只','positions')}</span></div>${laPositions(snapshot.positions||[],laNum(snapshot.account,'total_assets','total_asset','net_asset'))}</section>`:''}
      ${snapshot?laOrderTicket(st.place_order_ready):''}
      ${(snapshot&&snapshot.activity_warnings&&snapshot.activity_warnings.length)?`<div class="la-error">${laT('部分历史流水读取失败：','Some activity history could not be loaded: ')} ${laEsc(snapshot.activity_warnings.join(' · '))}</div>`:''}
      ${snapshot?`<div class="la-two"><section class="la-panel"><div class="la-section-head"><h2>${laT('订单','Orders')}</h2></div>${laActivity(snapshot.orders||[],'order',st.place_order_ready)}</section><section class="la-panel"><div class="la-section-head"><h2>${laT('成交','Deals')}</h2></div>${laActivity(snapshot.deals||[],'deal',false)}</section></div>`:''}
      <section class="la-panel"><details><summary>${laT('完整原始账户字段与数据来源','All raw account fields and provenance')}</summary><pre class="la-raw">${laEsc(JSON.stringify(snapshot?{source:snapshot.source,account_id:snapshot.account_id,fetched_at:snapshot.fetched_at,account:snapshot.account}:root,null,2))}</pre></details></section>
    </div>`;
    if(snapshot){
      laRenderChart(snapshot.nav_history||[]);
      const form=document.getElementById('la-order-form');if(form)form.addEventListener('submit',laPreviewOrder);
      document.querySelectorAll('[data-la-cancel]').forEach(btn=>btn.addEventListener('click',()=>laCancelOrder(decodeURIComponent(btn.dataset.laCancel||''))));
    }
    const readUnlock=document.getElementById('la-read-unlock');
    if(readUnlock)readUnlock.addEventListener('click',()=>{_laReadToken=document.getElementById('la-read-token').value||'';renderLiveAccountPage(window.__activeRouteToken);});
  } catch(e){app.innerHTML=`<div class="la-shell"><div class="la-fatal"><h2>${laT('真实账户模块不可用','Live account module unavailable')}</h2><p>${laEsc(e.message)}</p></div></div>`;}
}

async function laPreviewOrder(ev){ev.preventDefault();const out=document.getElementById('la-preview');out.innerHTML='<div class="la-loading">Checking Moomoo quote…</div>';_laPreview=null;
  try{const body={code:document.getElementById('la-code').value,side:document.getElementById('la-side').value,qty:Number(document.getElementById('la-qty').value),limit_price:Number(document.getElementById('la-price').value)};const p=await laFetch('/orders/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});_laPreview=p;out.innerHTML=`<div class="la-preview-card"><div><small>Moomoo last / bid / ask</small><b>${laMoney(p.quote.last_price)} · ${laMoney(p.quote.bid_price)} · ${laMoney(p.quote.ask_price)}</b></div><div><small>${laT('订单名义金额','Order notional')}</small><b>${laMoney(p.notional)}</b></div><div><small>${laT('有效期','Preview expiry')}</small><b>${p.expires_in_seconds}s</b></div><label>${laT('交易授权令牌（仅保存在本页内存）','Trade token (page memory only)')}<input id="la-auth" type="password" autocomplete="off"></label><label>${laT('输入确认语句','Type confirmation')}<input id="la-confirm" autocomplete="off" placeholder="PLACE LIVE ORDER"></label><button class="la-btn danger" onclick="laPlaceOrder()" ${p.place_order_ready?'':'disabled'}>${laT('提交真实限价单','PLACE LIVE LIMIT ORDER')}</button></div>`;}catch(e){out.innerHTML=`<div class="la-error">${laEsc(e.message)}</div>`;}}
async function laPlaceOrder(){if(!_laPreview)return;const authInput=document.getElementById('la-auth'),auth=authInput.value,confirmation=document.getElementById('la-confirm').value;try{const r=await laFetch('/orders/place',{method:'POST',headers:{'Content-Type':'application/json','X-Moomoo-Trade-Token':auth},body:JSON.stringify({preview_token:_laPreview.preview_token,confirmation})});authInput.value='';alert(laT(`Moomoo已接受订单 ${r.order.order_id||''}`,`Moomoo accepted order ${r.order.order_id||''}`));_laPreview=null;renderLiveAccountPage(window.__activeRouteToken);}catch(e){authInput.value='';alert(e.message);}}
async function laCancelOrder(orderId){const confirmation=prompt(laT('输入 CANCEL LIVE ORDER 确认撤单','Type CANCEL LIVE ORDER to cancel'));if(confirmation!=='CANCEL LIVE ORDER')return;const auth=prompt(laT('输入交易授权令牌（不会保存）','Enter trade authorization token (not saved)'))||'';try{await laFetch(`/orders/${encodeURIComponent(orderId)}/cancel`,{method:'POST',headers:{'Content-Type':'application/json','X-Moomoo-Trade-Token':auth},body:JSON.stringify({confirmation})});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}}

window.renderLiveAccountPage=renderLiveAccountPage;
window.laPlaceOrder=laPlaceOrder;
window.laCancelOrder=laCancelOrder;
})();
