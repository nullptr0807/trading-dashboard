// live_account.js — real-money Moomoo OpenD account module
(()=>{
let _laPreview = null;
let _laControlToken = '';
let _laChart = null;
let _laRefreshTimer = null;

function laLang() { return (typeof getLang === 'function' && getLang() === 'zh') ? 'zh' : 'en'; }
function laT(zh, en) { return laLang() === 'zh' ? zh : en; }
function laEsc(v) { const d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }
function laNum(row, ...keys) {
  for (const k of keys) { const n = Number(row && row[k]); if (Number.isFinite(n)) return n; }
  return 0;
}
function laMoney(v) { return '$' + Number(v || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function laTime(v) { if(!v)return '—'; const d=new Date(v); return Number.isNaN(d.getTime())?String(v):d.toLocaleString(); }
function laPct(v) { const n = Number(v || 0); return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`; }
function laClass(v) { return Number(v) > 0 ? 'la-positive' : Number(v) < 0 ? 'la-negative' : '' ; }
async function laFetch(path, options) {
  options = options ? {...options} : {};
  options.headers = {...(options.headers || {})};
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
    [laT('独立初始本金', 'Independent initial capital'), '$10,000.00'],
    [laT('不可变仓位上限', 'Immutable exposure cap'), laMoney(policy.strategy_capital_limit)],
    [laT('25%损失冻结线', '25% loss freeze floor'), laMoney(policy.strategy_loss_floor)],
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
  const isolationMode=status.account_isolation_mode||'unverified';
  const isolationText=isolationMode==='dedicated'
    ? laT('专用账户已验收','Dedicated account verified')
    : isolationMode==='shared_restricted'
      ? laT('受限共享账户；仅逻辑隔离','Restricted shared account; logical isolation only')
      : laT('未验收；真实下单禁止','Unverified; live orders blocked');
  return `<section class="la-panel la-setup"><div class="la-section-head"><div><span class="la-kicker">CONNECTION</span><h2>${laT('连接Moomoo OpenD', 'Connect Moomoo OpenD')}</h2></div></div>
  <div class="la-check-grid">
    ${laStatusItem(status.sdk_installed, laT('官方Python SDK','Official Python SDK'), status.sdk_installed ? laT('已安装','Installed') : laT('未安装','Missing'))}
    ${laStatusItem(status.opend_connected, 'Moomoo OpenD', status.opend_connected ? laT('已连接','Connected') : (status.message || '127.0.0.1:11111'))}
    ${laStatusItem(status.real_account_selected, laT('Broker连接','Broker connection'), status.real_account_selected ? laT('已固定真实账户；详情不在页面展示','Real account pinned; details hidden from UI') : laT('尚未选择','Not selected'))}
    ${laStatusItem(isolationMode!=='unverified'&&isolationMode!=='invalid', laT('账户隔离模式','Account isolation mode'), isolationText)}
    ${laStatusItem(status.shared_account_risk_accepted, laT('共享账户剩余风险接受','Shared-account residual-risk acceptance'), status.shared_account_risk_accepted ? laT('已明确接受','Explicitly accepted') : laT('未接受','Not accepted'))}
    ${laStatusItem(true, laT('控制代际','Control generation'), `v${Number(status.control_generation||0)}`)}
    ${laStatusItem(status.sync_proof_current, laT('当前隔离代际同步证明','Current isolation-generation sync proof'), status.sync_proof_current ? laT('匹配','Current') : laT('缺失或已失效','Missing or stale'))}
    ${laStatusItem(status.trade_token_configured, laT('交易授权令牌','Trade authorization token'), status.trade_token_configured ? laT('已配置','Configured') : laT('未配置；只能读取','Missing; read-only'))}

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

function laControlPanel(control) {
  const s=control.state||{}, c=(control.config||{}).values||{}, summary=control.execution_summary||{}, frozen=s.lifecycle!=='ACTIVE';
  const ret=(Number(s.strategy_equity||10000)/10000-1)*100;
  const fields=[
    ['top_n',laT('最大持仓股票数','Max positions'),'number','1'],
    ['position_target_pct',laT('单股目标比例','Target per position'),'number','0.001'],
    ['gross_target_pct',laT('目标总敞口','Target gross exposure'),'number','0.01'],
    ['stop_loss_pct',laT('固定止损','Fixed stop'),'number','0.01'],
    ['stop_cooldown_hours',laT('止损冷却小时','Cooldown hours'),'number','1'],
    ['min_hold_days',laT('最小持仓日','Minimum hold days'),'number','1'],
    ['hold_band_mult',laT('持有缓冲倍数','Hold-band multiple'),'number','1'],
    ['rebalance_hours',laT('再平衡小时','Rebalance hours'),'number','1'],
    ['max_order_notional',laT('单笔上限USD','Max order USD'),'number','1'],
    ['max_daily_order_notional',laT('每日订单上限USD','Daily order USD'),'number','1'],
    ['max_limit_deviation_pct',laT('限价偏离','Limit deviation'),'number','0.001'],
    ['max_quote_age_seconds',laT('行情最大年龄秒','Max quote age seconds'),'number','1'],
  ];
  return `<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">$10K STRATEGY SUB-LEDGER</span><h2>${laT('独立策略资金与交易总开关','Independent capital & master switch')}</h2></div><span class="la-risk-badge ${frozen?'':'ok'}">${laEsc(s.lifecycle||'UNKNOWN')}</span></div>
  <div class="la-metrics"><div class="la-metric hero"><small>${laT('策略权益','Strategy equity')}</small><b>${laMoney(s.strategy_equity)}</b><span class="${laClass(ret)}">${laPct(ret)}</span></div><div class="la-metric"><small>${laT('策略现金','Strategy cash')}</small><b>${laMoney(s.allocated_cash)}</b></div><div class="la-metric"><small>${laT('策略持仓市值','Strategy market value')}</small><b>${laMoney(s.owned_market_value)}</b></div><div class="la-metric"><small>${laT('总成交笔数','Total fills')}</small><b>${Number(summary.total_trades||0).toLocaleString()}</b><span>BUY ${Number(summary.buy_trades||0)} · SELL ${Number(summary.sell_trades||0)}</span></div><div class="la-metric"><small>${laT('累计交易费用','Total trading fees')}</small><b>${laMoney(summary.total_fees)}</b></div><div class="la-metric"><small>${laT('累计成交金额','Total traded notional')}</small><b>${laMoney(summary.total_notional)}</b></div><div class="la-metric"><small>${laT('不可变仓位上限','Immutable exposure cap')}</small><b>$10,000.00</b></div><div class="la-metric"><small>${laT('不可变停机线','Immutable loss floor')}</small><b>$7,500.00</b></div><div class="la-metric"><small>${laT('最后对账','Last reconciliation')}</small><b>${laEsc(laTime(s.last_sync_at))}</b></div></div>
  <div class="la-order-warning">${frozen?laT(`系统已冻结：${s.freeze_reason||'未启用'}`,`System frozen: ${s.freeze_reason||'not armed'}`):laT('系统ACTIVE；所有订单仍需通过$10k、持仓归属和盘中门禁。','System ACTIVE; every order still passes capital, ownership and RTH gates.')}</div>
  <div class="la-order-form"><label>${laT('控制令牌（仅页面内存）','Control token — page memory only')}<input id="la-control-token" type="password" autocomplete="off"></label><label>${laT('操作原因','Action reason')}<input id="la-control-reason" maxlength="500" autocomplete="off"></label><button class="la-btn danger" id="la-freeze">FREEZE</button><button class="la-btn" id="la-unfreeze">UNFREEZE</button><button class="la-link danger" id="la-cleanup">${laT('冻结、归档并清理策略','Freeze, archive & clean')}</button></div>
  <details class="la-config"><summary>${laT('编辑并立即热更新交易参数','Edit and hot-reload trading parameters')} · v${control.config&&control.config.version||'—'}</summary><form id="la-config-form" class="la-order-form">${fields.map(([k,label,type,step])=>`<label>${laEsc(label)}<input data-la-param="${k}" type="${type}" step="${step}" value="${laEsc(c[k])}"></label>`).join('')}<label>${laT('变更原因','Change reason')}<input id="la-config-reason" required maxlength="500"></label><button class="la-btn" type="submit">${laT('验证并立即Reload','Validate & reload now')}</button></form></details></section>`;
}

function laOwnedPositions(control) {
  const items=control.owned_positions||[];
  if(!items.length)return `<div class="la-empty">${laT('尚无本系统拥有的仓位；Moomoo原有股票按外部只读处理，共享账户仅为逻辑隔离。','No strategy-owned positions. Existing Moomoo holdings are external read-only; shared accounts have logical isolation only.')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table"><thead><tr><th>${laT('标的','Symbol')}</th><th>${laT('系统拥有数量','Owned qty')}</th><th>${laT('均价','Average cost')}</th><th>${laT('现价','Last')}</th><th>${laT('市值','Market value')}</th><th>${laT('已实现盈亏','Realized P&L')}</th></tr></thead><tbody>${items.map(p=>`<tr><td><b>${laEsc(p.symbol)}</b></td><td>${laNum(p,'quantity')}</td><td>${laMoney(p.average_cost)}</td><td>${laMoney(p.market_price)}</td><td>${laMoney(p.market_value)}</td><td class="${laClass(p.realized_pnl)}">${laMoney(p.realized_pnl)}</td></tr>`).join('')}</tbody></table></div>`;
}

function laEvents(control) {
  const items=control.events||[];
  if(!items.length)return `<div class="la-empty">${laT('暂无事件','No events')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table compact"><thead><tr><th>${laT('时间','Time')}</th><th>${laT('类型','Type')}</th><th>${laT('来源','Source')}</th><th>${laT('事件','Event')}</th></tr></thead><tbody>${items.slice(0,200).map(e=>`<tr><td>${laEsc(laTime(e.ts))}</td><td><b>${laEsc(e.event_type)}</b><small>${laEsc(e.severity)}</small></td><td>${laEsc(e.source)}</td><td>${laEsc(e.message)}</td></tr>`).join('')}</tbody></table></div>`;
}

function laStrategyFills(control) {
  const items=control.fills||[];
  if(!items.length)return `<div class="la-empty">${laT('尚无策略成交','No strategy fills yet')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table compact"><thead><tr><th>${laT('入账时间','Applied at')}</th><th>${laT('标的','Symbol')}</th><th>${laT('方向','Side')}</th><th>${laT('数量','Qty')}</th><th>${laT('成交价','Fill price')}</th><th>${laT('费用','Fee')}</th><th>${laT('成交金额','Notional')}</th></tr></thead><tbody>${items.map(x=>`<tr><td>${laEsc(laTime(x.applied_at))}</td><td><b>${laEsc(x.symbol||'')}</b></td><td>${laEsc(x.side||'')}</td><td>${laNum(x,'quantity').toLocaleString()}</td><td>${laMoney(x.price)}</td><td>${laMoney(x.fee)}</td><td>${laMoney(laNum(x,'quantity')*laNum(x,'price'))}</td></tr>`).join('')}</tbody></table></div>`;
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

function laRenderChart(control) {
  const el=document.getElementById('la-nav-chart'), history=control&&control.equity||[]; if(!el || !window.LightweightCharts) return;
  if(_laChart){try{_laChart.remove();}catch{} _laChart=null;}
  el.textContent='';
  _laChart=LightweightCharts.createChart(el,{width:el.clientWidth,height:260,layout:{background:{color:'#111827'},textColor:'#9fb0c7'},grid:{vertLines:{color:'#223049'},horzLines:{color:'#223049'}},rightPriceScale:{borderColor:'#34445e'},timeScale:{borderColor:'#34445e',timeVisible:true}});
  if(history.length){const series=_laChart.addAreaSeries({lineColor:'#33e3a2',topColor:'rgba(51,227,162,.35)',bottomColor:'rgba(51,227,162,.02)',lineWidth:2,title:laT('实盘$10k子账本','Live $10k sub-ledger')});const byDay={};history.forEach(x=>{byDay[String(x.ts).slice(0,10)]=Number(x.equity)});series.setData(Object.entries(byDay).map(([time,value])=>({time,value})));}
  const grouped={};(control.paper_series||[]).forEach(x=>(grouped[x.series_id]??=[]).push(x));
  const colors=['#6aa9ff','#f4b860','#c792ea','#ff6b8a'];Object.entries(grouped).forEach(([id,rows],i)=>{const line=_laChart.addLineSeries({color:colors[i%colors.length],lineWidth:2,lineStyle:2,title:`PAPER · ${rows[0].label}`});const byDay={};rows.forEach(x=>{byDay[String(x.ts).slice(0,10)]=Number(x.equity)});line.setData(Object.entries(byDay).map(([time,value])=>({time,value})));});
  _laChart.timeScale().fitContent();new ResizeObserver(()=>{if(_laChart){_laChart.applyOptions({width:el.clientWidth});_laChart.timeScale().fitContent();}}).observe(el);
}

async function renderLiveAccountPage(token) {
  if(_laRefreshTimer){clearTimeout(_laRefreshTimer);_laRefreshTimer=null;}
  _laPreview=null;
  const app=document.getElementById('app');
  app.innerHTML=`<div class="la-shell"><section class="la-hero"><div><span class="la-kicker">MOOMOO OPENAPI · STRATEGY SUB-LEDGER</span><h1>${laT('实盘策略账户','Live Strategy Account')}</h1><p>${laT('仅展示独立$10,000策略子账本；个人账户资金、持仓和交易不会发送到此页面。','Only the independent $10,000 strategy sub-ledger is shown. Personal account cash, holdings and activity are never sent to this page.')}</p></div><div class="la-hero-mark">$10K<span>${laT('不可变策略本金','IMMUTABLE CAPITAL')}</span></div></section><div class="la-loading">${laT('正在加载策略子账本…','Loading strategy sub-ledger…')}</div></div>`;
  try {
    const [root,control]=await Promise.all([laFetch('/status'),laFetch('/strategy')]);
    if(token && !isRouteCurrent(token,'/live-account'))return;
    const st=root.status,p=root.policy;
    app.innerHTML=`<div class="la-shell"><section class="la-hero"><div><span class="la-kicker">MOOMOO OPENAPI · STRATEGY SUB-LEDGER</span><h1>${laT('实盘策略账户','Live Strategy Account')}</h1><p>${laT('仅展示独立$10,000策略子账本；个人账户资金、持仓和交易不会发送到此页面。','Only the independent $10,000 strategy sub-ledger is shown. Personal account cash, holdings and activity are never sent to this page.')}</p></div><div class="la-hero-mark">$10K<span>${laT('不可变策略本金','IMMUTABLE CAPITAL')}</span></div></section>
      <div class="la-livebar ${st.place_order_ready?'armed':'safe'}"><b>${st.place_order_ready?laT('真实下单已解锁','REAL ORDERS ARMED'):laT('只读/冻结安全模式','READ-ONLY / FROZEN SAFE MODE')}</b><span>${laEsc(st.message||`${st.security_firm} · ${st.market}`)}</span><button class="la-link" id="la-refresh">${laT('刷新','Refresh')}</button></div>
      ${st.account_isolation_mode==='shared_restricted'?`<div class="la-error"><b>${laT('受限共享账户：页面仅展示逻辑策略子仓。','RESTRICTED SHARED ACCOUNT: this page shows the logical strategy sub-position only.')}</b> ${laT('个人持仓不会进入页面；策略只记录并卖出自己经证明成交的数量。','Personal holdings never enter this page. The strategy records and sells only its proven quantity.')}</div>`:''}
      ${laControlPanel(control)}
      <section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">LIVE VS PAPER</span><h2>${laT('策略权益与Paper候选','Strategy equity & paper candidates')}</h2></div><span class="la-source">${laT('实线=实盘子账本 · 虚线=Paper','Solid=live sub-ledger · dashed=paper')}</span></div><div id="la-nav-chart" class="la-chart">${laT('等待权益快照','Awaiting equity snapshots')}</div></section>
      <section class="la-panel"><div class="la-section-head"><h2>${laT('策略持仓','Strategy positions')}</h2><span class="la-source">STRATEGY OWNED ONLY</span></div>${laOwnedPositions(control)}</section>
      <section class="la-panel"><div class="la-section-head"><h2>${laT('策略成交历史','Strategy fill history')}</h2><span class="la-source">${Number(control.execution_summary&&control.execution_summary.total_trades||0)} ${laT('笔','fills')}</span></div>${laStrategyFills(control)}</section>
      ${laSetup(st)}
      ${laPolicyCard(p)}
      ${laOrderTicket(st.place_order_ready)}
      <section class="la-panel"><div class="la-section-head"><h2>${laT('系统事件时间线','System event timeline')}</h2><span class="la-source">factor · signal · order · freeze · cleanup</span></div>${laEvents(control)}</section>
      <section class="la-panel"><details><summary>${laT('数据来源和运行状态','Data provenance and runtime state')}</summary><pre class="la-raw">${laEsc(JSON.stringify({source:control.data_scope,state:control.state},null,2))}</pre></details></section>
    </div>`;
    laRenderChart(control);laBindControls(control);
    const form=document.getElementById('la-order-form');if(form)form.addEventListener('submit',laPreviewOrder);
    const refresh=document.getElementById('la-refresh');if(refresh)refresh.addEventListener('click',()=>renderLiveAccountPage(window.__activeRouteToken));
    _laRefreshTimer=setTimeout(()=>{if(isRouteCurrent(window.__activeRouteToken,'/live-account'))renderLiveAccountPage(window.__activeRouteToken);},300000);
  } catch(e){app.innerHTML=`<div class="la-shell"><div class="la-fatal"><h2>${laT('策略账户模块不可用','Strategy account module unavailable')}</h2><p>${laEsc(e.message)}</p></div></div>`;}
}

function laControlAuth(){const input=document.getElementById('la-control-token');if(input&&input.value)_laControlToken=input.value;return _laControlToken;}
async function laControlCall(path,method,body){const token=laControlAuth();if(!token)throw new Error(laT('请输入控制令牌','Enter the control token'));return laFetch(path,{method,headers:{'Content-Type':'application/json','X-Moomoo-Control-Token':token},body:JSON.stringify(body)});}
function laBindControls(control){
  const freeze=document.getElementById('la-freeze');if(freeze)freeze.addEventListener('click',async()=>{try{const reason=document.getElementById('la-control-reason').value||'dashboard_one_click_freeze';await laControlCall('/control/freeze','POST',{confirmation:'FREEZE LIVE TRADING',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const unfreeze=document.getElementById('la-unfreeze');if(unfreeze)unfreeze.addEventListener('click',async()=>{if(prompt(laT('输入 UNFREEZE LIVE TRADING 确认','Type UNFREEZE LIVE TRADING to confirm'))!=='UNFREEZE LIVE TRADING')return;try{const reason=document.getElementById('la-control-reason').value||'';await laControlCall('/control/unfreeze','POST',{confirmation:'UNFREEZE LIVE TRADING',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const cleanup=document.getElementById('la-cleanup');if(cleanup)cleanup.addEventListener('click',async()=>{if(prompt(laT('此操作只允许空仓执行。输入 FREEZE ARCHIVE AND CLEAN STRATEGY','Flat strategy only. Type FREEZE ARCHIVE AND CLEAN STRATEGY'))!=='FREEZE ARCHIVE AND CLEAN STRATEGY')return;try{const reason=document.getElementById('la-control-reason').value||'';await laControlCall('/control/cleanup','POST',{confirmation:'FREEZE ARCHIVE AND CLEAN STRATEGY',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const form=document.getElementById('la-config-form');if(form)form.addEventListener('submit',async ev=>{ev.preventDefault();const patch={};form.querySelectorAll('[data-la-param]').forEach(el=>{patch[el.dataset.laParam]=Number(el.value);});try{await laControlCall('/control/config','PUT',{expected_version:control.config.version,patch,reason:document.getElementById('la-config-reason').value});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
}

async function laPreviewOrder(ev){ev.preventDefault();const out=document.getElementById('la-preview');out.innerHTML='<div class="la-loading">Checking Moomoo quote…</div>';_laPreview=null;
  try{const body={code:document.getElementById('la-code').value,side:document.getElementById('la-side').value,qty:Number(document.getElementById('la-qty').value),limit_price:Number(document.getElementById('la-price').value)};const p=await laFetch('/orders/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});_laPreview=p;out.innerHTML=`<div class="la-preview-card"><div><small>Moomoo last / bid / ask</small><b>${laMoney(p.quote.last_price)} · ${laMoney(p.quote.bid_price)} · ${laMoney(p.quote.ask_price)}</b></div><div><small>${laT('订单名义金额','Order notional')}</small><b>${laMoney(p.notional)}</b></div><div><small>${laT('有效期','Preview expiry')}</small><b>${p.expires_in_seconds}s</b></div><label>${laT('交易授权令牌（仅保存在本页内存）','Trade token (page memory only)')}<input id="la-auth" type="password" autocomplete="off"></label><label>${laT('输入确认语句','Type confirmation')}<input id="la-confirm" autocomplete="off" placeholder="PLACE LIVE ORDER"></label><button class="la-btn danger" onclick="laPlaceOrder()" ${p.place_order_ready?'':'disabled'}>${laT('提交真实限价单','PLACE LIVE LIMIT ORDER')}</button></div>`;}catch(e){out.innerHTML=`<div class="la-error">${laEsc(e.message)}</div>`;}}
async function laPlaceOrder(){if(!_laPreview)return;const authInput=document.getElementById('la-auth'),auth=authInput.value,confirmation=document.getElementById('la-confirm').value;try{const r=await laFetch('/orders/place',{method:'POST',headers:{'Content-Type':'application/json','X-Moomoo-Trade-Token':auth},body:JSON.stringify({preview_token:_laPreview.preview_token,confirmation})});authInput.value='';alert(laT(`Moomoo已接受订单 ${r.order.order_id||''}`,`Moomoo accepted order ${r.order.order_id||''}`));_laPreview=null;renderLiveAccountPage(window.__activeRouteToken);}catch(e){authInput.value='';alert(e.message);}}
async function laCancelOrder(orderId){const confirmation=prompt(laT('输入 CANCEL LIVE ORDER 确认撤单','Type CANCEL LIVE ORDER to cancel'));if(confirmation!=='CANCEL LIVE ORDER')return;const auth=prompt(laT('输入交易授权令牌（不会保存）','Enter trade authorization token (not saved)'))||'';try{await laFetch(`/orders/${encodeURIComponent(orderId)}/cancel`,{method:'POST',headers:{'Content-Type':'application/json','X-Moomoo-Trade-Token':auth},body:JSON.stringify({confirmation})});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}}

window.renderLiveAccountPage=renderLiveAccountPage;
window.laPlaceOrder=laPlaceOrder;
window.laCancelOrder=laCancelOrder;
})();
