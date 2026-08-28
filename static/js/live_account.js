// live_account.js — real-money Moomoo OpenD account module
(()=>{
let _laControlToken = '';
let _laChart = null;
let _laChartResizeObserver = null;
let _laRefreshTimer = null;
let _laRequestInFlight = false;
const LA_REFRESH_MS = 10000;

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

function laCaptureView(app) {
  const fields = {};
  app.querySelectorAll('input[id], input[data-la-param]').forEach((el) => {
    const key = el.id || `param:${el.dataset.laParam}`;
    fields[key] = el.value;
  });
  return {
    fields,
    activeId: document.activeElement && document.activeElement.id,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    openDetails: [...app.querySelectorAll('details')].map((el) => el.open),
  };
}

function laRestoreView(app, view) {
  if (!view) return;
  app.querySelectorAll('input[id], input[data-la-param]').forEach((el) => {
    const key = el.id || `param:${el.dataset.laParam}`;
    if (Object.prototype.hasOwnProperty.call(view.fields, key)) el.value = view.fields[key];
  });
  [...app.querySelectorAll('details')].forEach((el, i) => { el.open = Boolean(view.openDetails[i]); });
  requestAnimationFrame(() => {
    window.scrollTo(view.scrollX, view.scrollY);
    if (view.activeId) {
      const active = document.getElementById(view.activeId);
      if (active) active.focus({preventScroll:true});
    }
  });
}

function laScheduleRefresh() {
  if (_laRefreshTimer) clearTimeout(_laRefreshTimer);
  _laRefreshTimer = setTimeout(() => {
    _laRefreshTimer = null;
    if (!document.hidden && isRouteCurrent(window.__activeRouteToken, '/live-account')) {
      renderLiveAccountPage(window.__activeRouteToken, {background:true});
    }
  }, LA_REFRESH_MS);
}
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

function laMarketMeta(state) {
  const value=String(state||'UNKNOWN').toUpperCase();
  if(['MORNING','AFTERNOON'].includes(value))return {tone:'live',label:laT('交易中','Market open'),detail:'RTH'};
  if(['PRE_MARKET_BEGIN','PRE_MARKET_END'].includes(value))return {tone:'pre',label:laT('盘前交易','Pre-market'),detail:'EXT'};
  if(['AFTER_HOURS_BEGIN','AFTER_HOURS_END'].includes(value))return {tone:'after',label:laT('盘后交易','After-hours'),detail:'EXT'};
  if(['OVERNIGHT','NIGHT','NIGHT_OPEN'].includes(value))return {tone:'night',label:laT('隔夜交易','Overnight'),detail:'EXT'};
  if(['CLOSED','NONE','WAITING_OPEN','REST','NIGHT_END'].includes(value))return {tone:'closed',label:laT('休市中','Market closed'),detail:'US'};
  return {tone:'unknown',label:laT('市场状态未知','Market unknown'),detail:value};
}

function laHealthMeta(status, state) {
  if(!status.opend_connected)return {tone:'danger',label:laT('连接异常','Disconnected'),detail:'OpenD'};
  if(state.lifecycle!=='ACTIVE')return {tone:'danger',label:laT('系统冻结','System frozen'),detail:state.freeze_reason||'FROZEN'};
  if(!status.sync_proof_current||!status.control_sync_fresh)return {tone:'warn',label:laT('等待对账','Sync required'),detail:'STALE'};
  return {tone:'live',label:laT('健康正常','Healthy'),detail:'ALL CHECKS'};
}

function laHero(control, status) {
  const state=control.state||{}, perf=control.performance_summary||{};
  const market=laMarketMeta(control.market_status&&control.market_status.state);
  const health=laHealthMeta(status,state);
  const execution=status.auto_trading_enabled
    ? {tone:'live',label:laT('自动交易中','Auto trading'),detail:'AUTO'}
    : status.trading_enabled
      ? {tone:'pre',label:laT('真实交易就绪','Live ready'),detail:'MANUAL GATE'}
      : {tone:'closed',label:laT('真实交易关闭','Live trading off'),detail:'SAFE'};
  const ret=Number(perf.total_return_pct||0), pnl=Number(perf.pnl||0), sharpe=perf.sharpe_ratio;
  const pnlText=`${pnl>=0?'+':'-'}$${Math.abs(pnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
  const chips=[market,health,execution,{tone:status.control_sync_fresh?'live':'warn',label:laT('对账','Reconciliation'),detail:status.control_sync_fresh?laT('新鲜','FRESH'):laT('过期','STALE')}];
  return `<section class="la-hero la-hero-pro"><div class="la-hero-copy"><span class="la-kicker">MOOMOO OPENAPI · STRATEGY COMMAND</span><h1>${laT('实盘策略账户','Live Strategy Account')}</h1><p>${laT('独立$10,000策略子账本 · Long-only · Broker持仓逻辑隔离','Independent $10,000 sub-ledger · Long-only · Logically isolated broker inventory')}</p><div class="la-status-rack">${chips.map(x=>`<div class="la-status-chip ${x.tone}"><i></i><span><b>${laEsc(x.label)}</b><small>${laEsc(x.detail)}</small></span></div>`).join('')}</div></div><div class="la-command-deck"><div class="la-return-card ${laClass(ret)}"><small>${laT('累计收益','TOTAL RETURN')}</small><strong>${laPct(ret)}</strong><span>${pnlText}</span></div><div class="la-risk-pair"><div><small>SHARPE · ANN.</small><b>${sharpe==null?'—':Number(sharpe).toFixed(2)}</b><span>${Number(perf.sharpe_observations||0)}/20 ${laT('日收益样本','daily returns')}</span></div><div><small>MAX DRAWDOWN</small><b>${Number(perf.max_drawdown_pct||0).toFixed(2)}%</b><span>${laT('策略子账本','sub-ledger')}</span></div></div><div class="la-hero-sync">${laT('最后对账','Last reconciliation')} · ${laEsc(laTime(state.last_sync_at))}</div></div></section>`;
}

function laPolicyCard(policy, control) {
  const c=(control.config||{}).values||{};
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
  return `<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">EXECUTION POLICY</span><h2>${laT('真实交易参数', 'Live trading policy')}</h2></div><span class="la-source">v${control.config&&control.config.version||'—'} · Moomoo + server guardrails</span></div><div class="la-policy-grid">${rows.map(r=>`<div><small>${laEsc(r[0])}</small><b>${laEsc(r[1])}</b></div>`).join('')}</div><form id="la-config-form" class="la-order-form la-policy-editor">${fields.map(([k,label,type,step])=>`<label>${laEsc(label)}<input data-la-param="${k}" type="${type}" step="${step}" value="${laEsc(c[k])}"></label>`).join('')}<label>${laT('变更原因','Change reason')}<input id="la-config-reason" required maxlength="500"></label><button class="la-btn" type="submit">${laT('保存并Reload（需控制令牌）','Save & reload — control token required')}</button></form><p class="la-note">${laT('编辑后会由服务端校验；保存和Reload必须使用上方控制令牌，并会触发安全冻结与新一轮对账。', 'Changes are validated server-side. Saving requires the control token above and triggers a safety freeze plus fresh reconciliation.')}</p></section>`;
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
  </div></section>`;
}

function laControlPanel(control) {
  const s=control.state||{}, summary=control.execution_summary||{}, frozen=s.lifecycle!=='ACTIVE';
  const ret=(Number(s.strategy_equity||10000)/10000-1)*100;
  return `<section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">$10K STRATEGY SUB-LEDGER</span><h2>${laT('独立策略资金与交易总开关','Independent capital & master switch')}</h2></div><span class="la-risk-badge ${frozen?'':'ok'}">${laEsc(s.lifecycle||'UNKNOWN')}</span></div>
  <div class="la-metrics"><div class="la-metric hero"><small>${laT('策略权益','Strategy equity')}</small><b>${laMoney(s.strategy_equity)}</b><span class="${laClass(ret)}">${laPct(ret)}</span></div><div class="la-metric"><small>${laT('策略现金','Strategy cash')}</small><b>${laMoney(s.allocated_cash)}</b></div><div class="la-metric"><small>${laT('策略持仓市值','Strategy market value')}</small><b>${laMoney(s.owned_market_value)}</b></div><div class="la-metric"><small>${laT('总成交笔数','Total fills')}</small><b>${Number(summary.total_trades||0).toLocaleString()}</b><span>BUY ${Number(summary.buy_trades||0)} · SELL ${Number(summary.sell_trades||0)}</span></div><div class="la-metric"><small>${laT('累计交易费用','Total trading fees')}</small><b>${laMoney(summary.total_fees)}</b></div><div class="la-metric"><small>${laT('累计成交金额','Total traded notional')}</small><b>${laMoney(summary.total_notional)}</b></div><div class="la-metric"><small>${laT('不可变仓位上限','Immutable exposure cap')}</small><b>$10,000.00</b></div><div class="la-metric"><small>${laT('不可变停机线','Immutable loss floor')}</small><b>$7,500.00</b></div><div class="la-metric"><small>${laT('最后对账','Last reconciliation')}</small><b>${laEsc(laTime(s.last_sync_at))}</b></div></div>
  <div class="la-order-warning">${frozen?laT(`系统已冻结：${s.freeze_reason||'未启用'}`,`System frozen: ${s.freeze_reason||'not armed'}`):laT('系统ACTIVE；所有订单仍需通过$10k、持仓归属和盘中门禁。','System ACTIVE; every order still passes capital, ownership and RTH gates.')}</div>
  <div class="la-order-form"><label>${laT('控制令牌（仅页面内存）','Control token — page memory only')}<input id="la-control-token" type="password" autocomplete="off"></label><label>${laT('操作原因','Action reason')}<input id="la-control-reason" maxlength="500" autocomplete="off"></label><button class="la-btn danger" id="la-freeze">FREEZE</button><button class="la-btn" id="la-unfreeze">UNFREEZE</button><button class="la-link danger" id="la-cleanup">${laT('冻结、归档并清理策略','Freeze, archive & clean')}</button></div></section>`;
}

function laOwnedPositions(control) {
  const items=control.owned_positions||[];
  if(!items.length)return `<div class="la-empty">${laT('尚无本系统拥有的仓位；Moomoo原有股票按外部只读处理，共享账户仅为逻辑隔离。','No strategy-owned positions. Existing Moomoo holdings are external read-only; shared accounts have logical isolation only.')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table"><thead><tr><th>${laT('标的','Symbol')}</th><th>${laT('系统拥有数量','Owned qty')}</th><th>${laT('均价','Average cost')}</th><th>${laT('现价','Last')}</th><th>${laT('市值','Market value')}</th><th>${laT('已实现盈亏','Realized P&L')}</th></tr></thead><tbody>${items.map(p=>`<tr><td><b>${laEsc(p.symbol)}</b></td><td>${laNum(p,'quantity')}</td><td>${laMoney(p.average_cost)}</td><td>${laMoney(p.market_price)}</td><td>${laMoney(p.market_value)}</td><td class="${laClass(p.realized_pnl)}">${laMoney(p.realized_pnl)}</td></tr>`).join('')}</tbody></table></div>`;
}

function laSymbolPerformance(control) {
  const items=control.symbol_performance||[];
  if(!items.length)return `<div class="la-empty">${laT('尚无可汇总的策略交易','No strategy trades to summarize yet')}</div>`;
  const total=items.reduce((sum,row)=>sum+laNum(row,'total_pnl'),0);
  return `<div class="la-performance-summary"><span>${laT('按总收益从高到低','Ranked by total P&L')}</span><b class="${laClass(total)}">${laMoney(total)}</b><small>${laT('已实现 + 未实现，含全部交易费用','Realized + unrealized, including all trading fees')}</small></div><div class="la-table-wrap"><table class="la-table la-performance-table"><thead><tr><th>#</th><th>${laT('标的','Symbol')}</th><th>${laT('状态','Status')}</th><th>${laT('持有数量','Qty held')}</th><th>${laT('已实现收益','Realized')}</th><th>${laT('未实现收益','Unrealized')}</th><th>${laT('总收益','Total P&L')}</th><th>${laT('收益率','Return')}</th></tr></thead><tbody>${items.map((p,i)=>`<tr><td class="la-rank">${i+1}</td><td><b>${laEsc(p.symbol)}</b><small>${laT('累计费用','Fees')} ${laMoney(p.fees)}</small></td><td><span class="la-position-state ${p.holding?'held':'closed'}">${p.holding?laT('持有中','HELD'):laT('已平仓','CLOSED')}</span></td><td>${laNum(p,'quantity').toLocaleString()}</td><td class="${laClass(p.realized_pnl)}">${laMoney(p.realized_pnl)}</td><td class="${laClass(p.unrealized_pnl)}">${laMoney(p.unrealized_pnl)}</td><td class="la-total-pnl ${laClass(p.total_pnl)}">${laMoney(p.total_pnl)}</td><td class="${laClass(p.return_pct)}">${p.return_pct==null?'—':laPct(p.return_pct)}</td></tr>`).join('')}</tbody></table></div>`;
}

function laEvents(control) {
  const items=control.events||[];
  if(!items.length)return `<div class="la-empty">${laT('暂无事件','No events')}</div>`;
  return `<div class="la-table-wrap la-event-scroll"><table class="la-table compact"><thead><tr><th>${laT('时间','Time')}</th><th>${laT('类型','Type')}</th><th>${laT('来源','Source')}</th><th>${laT('事件','Event')}</th></tr></thead><tbody>${items.map(e=>`<tr><td>${laEsc(laTime(e.ts))}</td><td><b>${laEsc(e.event_type)}</b><small>${laEsc(e.severity)}</small></td><td>${laEsc(e.source)}</td><td>${laEsc(e.message)}</td></tr>`).join('')}</tbody></table></div>`;
}

function laStrategyFills(control) {
  const items=control.fills||[];
  if(!items.length)return `<div class="la-empty">${laT('尚无策略成交','No strategy fills yet')}</div>`;
  return `<div class="la-table-wrap"><table class="la-table compact"><thead><tr><th>${laT('入账时间','Applied at')}</th><th>${laT('标的','Symbol')}</th><th>${laT('方向','Side')}</th><th>${laT('数量','Qty')}</th><th>${laT('成交价','Fill price')}</th><th>${laT('费用','Fee')}</th><th>${laT('成交金额','Notional')}</th></tr></thead><tbody>${items.map(x=>`<tr><td>${laEsc(laTime(x.applied_at))}</td><td><b>${laEsc(x.symbol||'')}</b></td><td>${laEsc(x.side||'')}</td><td>${laNum(x,'quantity').toLocaleString()}</td><td>${laMoney(x.price)}</td><td>${laMoney(x.effective_fee)}${Number(x.fee_finalized)===1?'':`<small>${laT('暂定','EST.')}</small>`}</td><td>${laMoney(laNum(x,'quantity')*laNum(x,'price'))}</td></tr>`).join('')}</tbody></table></div>`;
}


function laRenderChart(control) {
  const el=document.getElementById('la-nav-chart'), history=control&&control.equity||[]; if(!el || !window.LightweightCharts) return;
  if(_laChartResizeObserver){_laChartResizeObserver.disconnect();_laChartResizeObserver=null;}
  if(_laChart){try{_laChart.remove();}catch{} _laChart=null;}
  el.textContent='';
  _laChart=LightweightCharts.createChart(el,{width:el.clientWidth,height:260,layout:{background:{color:'#111827'},textColor:'#9fb0c7'},grid:{vertLines:{color:'#223049'},horzLines:{color:'#223049'}},rightPriceScale:{borderColor:'#34445e'},timeScale:{borderColor:'#34445e',timeVisible:true}});
  if(history.length){const series=_laChart.addAreaSeries({lineColor:'#33e3a2',topColor:'rgba(51,227,162,.35)',bottomColor:'rgba(51,227,162,.02)',lineWidth:2,title:laT('实盘$10k子账本','Live $10k sub-ledger')});const byDay={};history.forEach(x=>{byDay[String(x.ts).slice(0,10)]=Number(x.equity)});series.setData(Object.entries(byDay).map(([time,value])=>({time,value})));}
  const grouped={};(control.paper_series||[]).forEach(x=>(grouped[x.series_id]??=[]).push(x));
  const colors=['#6aa9ff','#f4b860','#c792ea','#ff6b8a'];Object.entries(grouped).forEach(([id,rows],i)=>{const line=_laChart.addLineSeries({color:colors[i%colors.length],lineWidth:2,lineStyle:2,title:`PAPER · ${rows[0].label}`});const byDay={};rows.forEach(x=>{byDay[String(x.ts).slice(0,10)]=Number(x.equity)});line.setData(Object.entries(byDay).map(([time,value])=>({time,value})));});
  _laChart.timeScale().fitContent();_laChartResizeObserver=new ResizeObserver(()=>{if(_laChart){_laChart.applyOptions({width:el.clientWidth});_laChart.timeScale().fitContent();}});_laChartResizeObserver.observe(el);
}

async function renderLiveAccountPage(token, options={}) {
  if(_laRefreshTimer){clearTimeout(_laRefreshTimer);_laRefreshTimer=null;}
  if(_laRequestInFlight)return;
  _laRequestInFlight=true;
  const app=document.getElementById('app');
  if(!options.background)app.innerHTML=`<div class="la-shell"><section class="la-hero"><div><span class="la-kicker">MOOMOO OPENAPI · STRATEGY SUB-LEDGER</span><h1>${laT('实盘策略账户','Live Strategy Account')}</h1><p>${laT('仅展示独立$10,000策略子账本；个人账户资金、持仓和交易不会发送到此页面。','Only the independent $10,000 strategy sub-ledger is shown. Personal account cash, holdings and activity are never sent to this page.')}</p></div><div class="la-hero-mark">$10K<span>${laT('不可变策略本金','IMMUTABLE CAPITAL')}</span></div></section><div class="la-loading">${laT('正在加载策略子账本…','Loading strategy sub-ledger…')}</div></div>`;
  try {
    const [root,control]=await Promise.all([laFetch('/status'),laFetch('/strategy')]);
    if(token && !isRouteCurrent(token,'/live-account'))return;
    const st=root.status,p=root.policy;
    const view=options.background?laCaptureView(app):null;
    app.innerHTML=`<div class="la-shell">${laHero(control,st)}
      <div class="la-livebar ${st.place_order_ready?'armed':'safe'}"><b>${st.place_order_ready?laT('真实下单已解锁','REAL ORDERS ARMED'):laT('只读/冻结安全模式','READ-ONLY / FROZEN SAFE MODE')}</b><span>${laEsc(st.message||`${st.security_firm} · ${st.market}`)} · ${laT('每10秒自动更新','Auto-updates every 10s')}</span><button class="la-link" id="la-refresh">${laT('立即更新','Update now')}</button></div>
      ${st.account_isolation_mode==='shared_restricted'?`<div class="la-error"><b>${laT('受限共享账户：页面仅展示逻辑策略子仓。','RESTRICTED SHARED ACCOUNT: this page shows the logical strategy sub-position only.')}</b> ${laT('个人持仓不会进入页面；策略只记录并卖出自己经证明成交的数量。','Personal holdings never enter this page. The strategy records and sells only its proven quantity.')}</div>`:''}
      ${laControlPanel(control)}
      <section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">LIVE VS PAPER</span><h2>${laT('策略权益与Paper候选','Strategy equity & paper candidates')}</h2></div><span class="la-source">${laT('实线=实盘子账本 · 虚线=Paper','Solid=live sub-ledger · dashed=paper')}</span></div><div id="la-nav-chart" class="la-chart">${laT('等待权益快照','Awaiting equity snapshots')}</div></section>
      <section class="la-panel"><div class="la-section-head"><div><span class="la-kicker">SYMBOL PERFORMANCE</span><h2>${laT('交易标的收益排行','Traded symbol performance')}</h2></div><span class="la-source">${Number(control.symbol_performance&&control.symbol_performance.length||0)} ${laT('个标的 · 策略子账本','symbols · strategy sub-ledger')}</span></div>${laSymbolPerformance(control)}</section>
      <section class="la-panel"><div class="la-section-head"><h2>${laT('策略持仓','Strategy positions')}</h2><span class="la-source">STRATEGY OWNED ONLY</span></div>${laOwnedPositions(control)}</section>
      <section class="la-panel"><div class="la-section-head"><h2>${laT('策略成交历史','Strategy fill history')}</h2><span class="la-source">${Number(control.execution_summary&&control.execution_summary.total_trades||0)} ${laT('笔','fills')}</span></div>${laStrategyFills(control)}</section>
      ${laSetup(st)}
      ${laPolicyCard(p,control)}
      <section class="la-panel"><div class="la-section-head"><h2>${laT('系统事件时间线','System event timeline')}</h2><span class="la-source">factor · signal · order · freeze · cleanup</span></div>${laEvents(control)}</section>
      <section class="la-panel"><details><summary>${laT('数据来源和运行状态','Data provenance and runtime state')}</summary><pre class="la-raw">${laEsc(JSON.stringify({source:control.data_scope,state:control.state},null,2))}</pre></details></section>
    </div>`;
    laRenderChart(control);laBindControls(control);
    laRestoreView(app,view);
    const refresh=document.getElementById('la-refresh');if(refresh)refresh.addEventListener('click',()=>renderLiveAccountPage(window.__activeRouteToken,{background:true}));
  } catch(e){if(!options.background)app.innerHTML=`<div class="la-shell"><div class="la-fatal"><h2>${laT('策略账户模块不可用','Strategy account module unavailable')}</h2><p>${laEsc(e.message)}</p></div></div>`;}
  finally {
    _laRequestInFlight=false;
    if(isRouteCurrent(window.__activeRouteToken,'/live-account'))laScheduleRefresh();
  }
}

function laControlAuth(){const input=document.getElementById('la-control-token');if(input&&input.value)_laControlToken=input.value;return _laControlToken;}
async function laControlCall(path,method,body){const token=laControlAuth();if(!token)throw new Error(laT('请输入控制令牌','Enter the control token'));return laFetch(path,{method,headers:{'Content-Type':'application/json','X-Moomoo-Control-Token':token},body:JSON.stringify(body)});}
function laBindControls(control){
  const freeze=document.getElementById('la-freeze');if(freeze)freeze.addEventListener('click',async()=>{try{const reason=document.getElementById('la-control-reason').value||'dashboard_one_click_freeze';await laControlCall('/control/freeze','POST',{confirmation:'FREEZE LIVE TRADING',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const unfreeze=document.getElementById('la-unfreeze');if(unfreeze)unfreeze.addEventListener('click',async()=>{if(prompt(laT('输入 UNFREEZE LIVE TRADING 确认','Type UNFREEZE LIVE TRADING to confirm'))!=='UNFREEZE LIVE TRADING')return;try{const reason=document.getElementById('la-control-reason').value||'';await laControlCall('/control/unfreeze','POST',{confirmation:'UNFREEZE LIVE TRADING',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const cleanup=document.getElementById('la-cleanup');if(cleanup)cleanup.addEventListener('click',async()=>{if(prompt(laT('此操作只允许空仓执行。输入 FREEZE ARCHIVE AND CLEAN STRATEGY','Flat strategy only. Type FREEZE ARCHIVE AND CLEAN STRATEGY'))!=='FREEZE ARCHIVE AND CLEAN STRATEGY')return;try{const reason=document.getElementById('la-control-reason').value||'';await laControlCall('/control/cleanup','POST',{confirmation:'FREEZE ARCHIVE AND CLEAN STRATEGY',reason});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
  const form=document.getElementById('la-config-form');if(form)form.addEventListener('submit',async ev=>{ev.preventDefault();const patch={};form.querySelectorAll('[data-la-param]').forEach(el=>{patch[el.dataset.laParam]=Number(el.value);});try{await laControlCall('/control/config','PUT',{expected_version:control.config.version,patch,reason:document.getElementById('la-config-reason').value});renderLiveAccountPage(window.__activeRouteToken);}catch(e){alert(e.message);}});
}

window.renderLiveAccountPage=renderLiveAccountPage;
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden&&isRouteCurrent(window.__activeRouteToken,'/live-account')){
    renderLiveAccountPage(window.__activeRouteToken,{background:true});
  }
});
})();
