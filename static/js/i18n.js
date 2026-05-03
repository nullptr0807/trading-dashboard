// i18n.js — simple language dictionary + t() helper.
// Default English; user can switch to Chinese from the nav. Choice persists in localStorage.

const I18N_DICT = {
  en: {
    // Brand / nav / layout
    brand: 'Cyber Quant Arena',
    title_doc: 'Cyber Quant Arena',
    nav_trade: 'Trading Overview',
    nav_backtest: 'Backtest Analysis',
    nav_intro: 'Intro',
    intro_loading: 'Loading documentation…',
    intro_error: 'Failed to load README',
    nav_live: 'Live',
    lang_label: 'Language',

    // Trade page hero
    total_equity: 'Total Equity',
    group_a_equity: 'Group A Equity',
    group_b_equity: 'Group B Equity',
    account_count: 'Accounts',
    daily_pnl: "Today's PnL",
    cannot_connect: 'Cannot connect to server',
    dist_title: '📊 Cumulative Return Distribution (per account, since inception)',
    dist_best: 'Best',
    dist_worst: 'Worst',
    retired_badge: 'RETIRED',
    retired_label: 'retired',
    retired_section_label: 'Retired accounts',
    tab_active: 'Active',
    tab_retired: 'Retired',
    tomb_empty: 'No retired accounts. May they all live long. 🍀',
    tomb_rip: 'In Loving Memory of',
    tomb_days: 'days',
    tomb_lifetime_return: 'Lifetime Return',
    tomb_eulogy: '📜 Eulogy',
    tomb_strategy: 'Strategy',
    tomb_group: 'Group',
    tomb_factors: 'Factors',
    tomb_initial: 'Initial cash',
    tomb_final: 'Final equity',
    tomb_trades: 'Total trades',
    tomb_desc: 'Description',
    tomb_cause: '⚰️ Cause of retirement',
    tomb_equity_lifetime: '📈 Lifetime Equity Curve',
    tomb_factors_section: '🧬 Factors / Strategy',
    tomb_final_positions: '🪦 Final Positions (frozen)',
    tomb_all_trades: '📜 All Trades',
    events_cat_lifecycle: 'LIFECYCLE',
    events_cat_inception: 'INCEPTION',
    retired_tooltip: 'Frozen account — no new trades, equity locked at retirement value',
    dist_median: 'Median',
    dist_mean: 'Mean',
    dist_iqr: 'IQR',
    dist_win_rate: 'Win rate',
    dist_wins_losses: 'Wins / Losses',
    dist_hist_title: 'Return distribution across accounts',

    // Trade page sections
    equity_curve: '📈 Equity Curve',
    accounts_overview: '💼 Accounts Overview',
    events_title: '📰 Live Event Stream',
    events_empty: 'No events yet. The feed will populate as the system runs.',
    events_loading: 'Loading…',
    events_cat_data: 'data',
    events_cat_factor: 'factor',
    events_cat_rebalance: 'rebalance',
    events_cat_trade: 'trade',
    events_cat_system: 'system',
    events_cat_risk: 'risk',
    events_cat_guard: 'guard',
    events_loading_more: 'Loading older events…',
    events_no_more: '— end of stream —',
    events_load_failed: 'Failed to load older events',
    sort_by: 'Sort by',
    sort_pnl_desc: 'Return ↓',
    sort_pnl_asc: 'Return ↑',
    sort_name_asc: 'Name A→Z',
    sort_name_desc: 'Name Z→A',
    sort_trades_desc: 'Trades ↓',
    sort_trades_asc: 'Trades ↑',
    sort_sharpe_desc: 'Sharpe ↓',
    sort_sharpe_asc: 'Sharpe ↑',

    // Bench legend
    bench_qqq: 'QQQ NASDAQ-100',
    bench_spy: 'SPY S&P 500',
    bench_suffix: '(benchmark)',

    // Card
    card_equity: 'Equity',
    card_pnl: 'PnL',
    card_return: 'Return',
    card_trades: 'Trades',
    card_sharpe: 'Sharpe',
    gp_evolved_factor: 'GP Evolved Factor',

    // Backtest panel
    bt_account_selection: 'Account Selection',
    bt_loading: 'Loading...',
    bt_params: 'Parameters',
    bt_initial_capital: 'Initial Capital',
    bt_start_date: 'Start Date',
    bt_end_date: 'End Date',
    bt_run: 'Run Backtest',
    bt_running: 'Running...',
    bt_placeholder: 'Select accounts and run backtest',
    bt_summary_stats: 'Summary Statistics',
    bt_equity_dashed: 'Equity Curve (dashed = index benchmark)',
    bt_account_comparison: 'Account Comparison',
    bt_date_hint: 'Default: last 90 days. Backtest uses yfinance to download historical data for bar-by-bar simulation.',
    bt_load_failed: 'Load failed:',
    bt_ungrouped: 'Ungrouped',
    bt_pick_one_account: 'Please select at least one account',
    bt_pick_dates: 'Please pick start / end dates',
    bt_running_title: 'Backtest running...',
    bt_starting: 'Starting...',
    bt_start_failed: 'Start failed:',
    bt_poll_failed: 'Polling failed',
    bt_error_prefix: 'Error:',
    bt_generic_fail: 'Backtest failed',

    // Backtest table headers
    th_account: 'Account',
    th_strategy: 'Strategy',
    th_total_return: 'Total Return',
    th_max_dd: 'Max DD',
    th_win_rate: 'Win Rate',
    th_profit_factor: 'Profit Factor',
    th_total_trades: 'Trades',
    th_time: 'Time',
    th_side: 'Side',
    th_ticker: 'Ticker',
    th_shares: 'Shares',
    th_price: 'Price',
    th_amount: 'Amount',
    th_fees: 'Fees',
    th_realized_pnl: 'Realized PnL',
    th_cost: 'Cost',
    th_current_price: 'Price',
    th_market_value: 'Market Value',
    th_weight: 'Weight',
    th_pnl: 'PnL',
    side_long: 'Long',

    // Metrics
    m_total_return: 'Total Return',
    m_max_dd: 'Max Drawdown',
    m_win_rate: 'Win Rate',
    m_profit_factor: 'Profit Factor',
    m_total_trades: 'Total Trades',

    // Backtest detail modal
    bt_initial: 'Initial',
    bt_trades_count: '{n} trades',
    bt_total_return: 'Total return',
    bt_max_drawdown: 'Max drawdown',
    bt_close: 'Close',
    bt_hover_hint: 'Hover curve for holdings · B/S = Buy/Sell',
    bt_trade_details: 'Trade Details',
    bt_filter_ticker: 'Filter by ticker...',
    bt_no_trades: 'No trades',
    bt_equity_label: 'Equity',
    bt_cash: 'Cash',
    bt_cumulative: 'Cumulative',
    bt_no_positions: 'No positions',
    bt_cost_label: 'cost',
    bt_more_items: '... {n} more',
    bt_pnl_label: 'PnL',

    // Data stats strings
    ds_main_data: '📦 [{interval}] main {univ}/{req} tickers | cached {hit} tickers ({hitRows} rows) | downloaded {dl} tickers ({dlRows} new rows)',
    ds_bench: '📊 [{interval}] benchmark QQQ/SPY | cache {hit} / download {dl}',
    ds_sim: '🕐 Simulated {bars} {interval} bars',

    // Factor blocks
    factor_raw_s: '💻 Raw S-expression',
    factor_math: '🔢 Equivalent Math Formula',
    factor_intuition: '🧭 What it computes (intuition)',
    factor_motivation: '💡 Motivation (why combine these variables)',
    factor_alpha: '🎯 Why this may deliver Alpha',
    factor_vars: '🧬 Feature variables used',
    factor_n: 'Factor {n}',
    factor_composite: '🧮 Final Scoring Formula (how factors combine)',
    factor_no_gp_params: '(no GP params)',
    factor_math_intuition: '📐 Math Intuition',
    factor_trade_motivation: '💡 Trading Motivation',
    factor_no_data: 'No factor data',

    // Detail sections
    detail_equity: 'Equity Curve',
    detail_factors: 'Strategy Factors · Math & Physics',
    detail_positions: 'Current Positions',
    detail_recent_trades: 'Recent Trades',
    no_positions: 'No positions',
    no_trade_records: 'No trade records',
    no_equity_data: 'No equity curve data',
    load_failed: 'Load failed:',

    // Alpha legend
    alpha_strategy: 'Strategy',
    alpha_hint: 'Benchmark comparison: from the first trade, buy $10,000 of QQQ / SPY and hold. Alpha = strategy return − benchmark return.<br>Note: QQQ/SPY only trade during US regular hours (09:30–16:00 ET); off-hours are shown as flat. Strategy equity still moves after hours because holdings are valued with yfinance <code>fast_info.lastPrice</code>, which returns pre/post-market prints.',
    alpha_hint_cn: 'Benchmark comparison: from the first trade, buy ¥100,000 of CSI 300 (000300.SH) and hold. Alpha = strategy return − benchmark return.<br>Note: CSI 300 only trades during A-share hours (09:30–11:30, 13:00–15:00 CST); off-session segments are shown flat.',
  },

  zh: {
    brand: 'Cyber Quant Arena',
    title_doc: 'Cyber Quant Arena — 量化交易仪表盘',
    nav_trade: '交易总览',
    nav_backtest: '回测分析',
    nav_intro: '介绍',
    intro_loading: '正在加载文档…',
    intro_error: '加载 README 失败',
    nav_live: '实时',
    lang_label: '语言',

    total_equity: '总权益',
    group_a_equity: 'A组 权益',
    group_b_equity: 'B组 权益',
    account_count: '账户数',
    daily_pnl: '今日盈亏',
    cannot_connect: '无法连接服务器',
    dist_title: '📊 账户累计收益率分布 (自开户以来)',
    dist_best: '最佳',
    dist_worst: '最差',
    retired_badge: '已退役',
    retired_label: '已退役',
    retired_section_label: '已退役账户',
    tab_active: '活跃中',
    tab_retired: '已退役',
    tomb_empty: '暂无退役账户，愿它们长寿安康 🍀',
    tomb_rip: '深切缅怀',
    tomb_days: '天',
    tomb_lifetime_return: '终身收益率',
    tomb_eulogy: '📜 生平',
    tomb_strategy: '策略',
    tomb_group: '组别',
    tomb_factors: '因子',
    tomb_initial: '初始资金',
    tomb_final: '最终权益',
    tomb_trades: '总交易数',
    tomb_desc: '简介',
    tomb_cause: '⚰️ 退役原因',
    tomb_equity_lifetime: '📈 终身权益曲线',
    tomb_factors_section: '🧬 因子 / 策略说明',
    tomb_final_positions: '🪦 退役时持仓（已冻结）',
    tomb_all_trades: '📜 全部交易',
    events_cat_lifecycle: '生命周期',
    events_cat_inception: '账户启用',
    retired_tooltip: '已冻结账户 — 不再交易，权益锁定在退役时的数值',
    dist_median: '中位数',
    dist_mean: '均值',
    dist_iqr: '四分位间距',
    dist_win_rate: '盈利账户占比',
    dist_wins_losses: '盈利/亏损',
    dist_hist_title: '账户收益率分布直方图',

    equity_curve: '📈 权益曲线',
    accounts_overview: '💼 账户概览',
    events_title: '📰 实时事件流',
    events_empty: '暂无事件。系统运行时事件会陆续出现在这里。',
    events_loading: '加载中…',
    events_cat_data: '数据',
    events_cat_factor: '因子',
    events_cat_rebalance: '换仓',
    events_cat_trade: '交易',
    events_cat_system: '系统',
    events_cat_risk: '风控',
    events_cat_guard: '保护',
    events_loading_more: '加载更早的事件…',
    events_no_more: '— 已到底部 —',
    events_load_failed: '加载历史事件失败',
    sort_by: '排序方式',
    sort_pnl_desc: '收益率 ↓',
    sort_pnl_asc: '收益率 ↑',
    sort_name_asc: '名称 A→Z',
    sort_name_desc: '名称 Z→A',
    sort_trades_desc: '交易次数 ↓',
    sort_trades_asc: '交易次数 ↑',
    sort_sharpe_desc: '夏普率 ↓',
    sort_sharpe_asc: '夏普率 ↑',

    bench_qqq: 'QQQ 纳指100',
    bench_spy: 'SPY 标普500',
    bench_suffix: '(基准)',

    card_equity: '总权益',
    card_pnl: '盈亏',
    card_return: '收益率',
    card_trades: '交易',
    card_sharpe: '夏普率',
    gp_evolved_factor: 'GP 进化因子',

    bt_account_selection: '账户选择',
    bt_loading: '加载中...',
    bt_params: '参数设置',
    bt_initial_capital: '初始资金',
    bt_start_date: '开始日期',
    bt_end_date: '结束日期',
    bt_run: '运行回测',
    bt_running: '运行中...',
    bt_placeholder: '选择账户并运行回测',
    bt_summary_stats: '综合统计',
    bt_equity_dashed: '权益曲线 (虚线 = 指数基准)',
    bt_account_comparison: '账户对比',
    bt_date_hint: '默认: 近 90 天。回测使用 yfinance 实时下载历史数据逐日模拟。',
    bt_load_failed: '加载失败:',
    bt_ungrouped: '未分组',
    bt_pick_one_account: '请至少选择一个账户',
    bt_pick_dates: '请选择开始/结束日期',
    bt_running_title: '回测运行中...',
    bt_starting: '启动...',
    bt_start_failed: '启动失败:',
    bt_poll_failed: '轮询失败',
    bt_error_prefix: '错误:',
    bt_generic_fail: '回测失败',

    th_account: '账户',
    th_strategy: '策略',
    th_total_return: '总收益',
    th_max_dd: '最大回撤',
    th_win_rate: '胜率',
    th_profit_factor: '盈亏比',
    th_total_trades: '总交易数',
    th_time: '时间',
    th_side: '方向',
    th_ticker: '标的',
    th_shares: '数量',
    th_price: '价格',
    th_amount: '金额',
    th_fees: '手续费',
    th_realized_pnl: '已实现盈亏',
    th_cost: '成本',
    th_current_price: '现价',
    th_market_value: '市值',
    th_weight: '仓位占比',
    th_pnl: '盈亏',
    side_long: '多',

    m_total_return: '总收益',
    m_max_dd: '最大回撤',
    m_win_rate: '胜率',
    m_profit_factor: '盈亏比',
    m_total_trades: '总交易数',

    bt_initial: '初始',
    bt_trades_count: '交易 {n} 次',
    bt_total_return: '总收益',
    bt_max_drawdown: '最大回撤',
    bt_close: '关闭',
    bt_hover_hint: '悬停曲线查看持仓 · B/S 为买卖点',
    bt_trade_details: '交易明细',
    bt_filter_ticker: '按 ticker 过滤...',
    bt_no_trades: '无交易',
    bt_equity_label: '权益',
    bt_cash: '现金',
    bt_cumulative: '累计',
    bt_no_positions: '空仓',
    bt_cost_label: '成本',
    bt_more_items: '…另有 {n} 支',
    bt_pnl_label: '盈亏',

    ds_main_data: '📦 [{interval}] 主数据 {univ}/{req} 支 | 缓存命中 {hit} 支 ({hitRows} 行) | 下载 {dl} 支 ({dlRows} 行新数据)',
    ds_bench: '📊 [{interval}] 基准 QQQ/SPY | 缓存 {hit}/下载 {dl}',
    ds_sim: '🕐 模拟 {bars} 根 {interval} K 线',

    factor_raw_s: '💻 原始 S 表达式',
    factor_math: '🔢 等价数学公式',
    factor_intuition: '🧭 这在算什么（直觉）',
    factor_motivation: '💡 动机（组合这些变量的理由）',
    factor_alpha: '🎯 为什么可能带来 Alpha',
    factor_vars: '🧬 用到的特征变量',
    factor_n: '因子 {n}',
    factor_composite: '🧮 最终打分公式（因子如何合成）',
    factor_no_gp_params: '(暂无GP参数)',
    factor_math_intuition: '📐 数学直觉',
    factor_trade_motivation: '💡 交易动机',
    factor_no_data: '暂无因子数据',

    detail_equity: '权益曲线',
    detail_factors: '策略因子 · 数学与物理解释',
    detail_positions: '当前持仓',
    detail_recent_trades: '最近交易',
    no_positions: '暂无持仓',
    no_trade_records: '暂无交易记录',
    no_equity_data: '暂无权益曲线数据',
    load_failed: '加载失败:',

    alpha_strategy: '本策略',
    alpha_hint: '对比基准：从首笔交易时刻起，用 $10,000 分别买入 QQQ / SPY 并持有。Alpha = 本策略收益 − 基准收益。<br>注：QQQ/SPY 仅在盘中（美东 9:30–16:00）有成交，非交易时段显示为水平线。策略权益在盘后仍小幅波动，因为持仓按 yfinance <code>fast_info.lastPrice</code> 估值，该接口会返回盘前/盘后成交价。',
    alpha_hint_cn: '对比基准：从首笔交易时刻起，用 ¥100,000 买入沪深300 (000300.SH) 并持有。Alpha = 本策略收益 − 基准收益。<br>注：沪深300 仅在A股交易时段（09:30–11:30、13:00–15:00 北京时间）有成交，非交易时段显示为水平线。',
  },
};

const LANG_STORE_KEY = 'cqa_lang';

// Map canonical (Chinese) strategy / benchmark names → English. B-group GP
// names are overridden in the card code to just "GP Evolved Factor", so we
// only need to translate A-group and index labels here.
const STRATEGY_EN = {
  '动量Alpha': 'Momentum Alpha',
  '均值回归': 'Mean Reversion',
  '量价策略': 'Price-Volume Strategy',
  '趋势跟踪': 'Trend Following',
  '波动率突破': 'Volatility Breakout',
  '综合多因子': 'Composite Multi-Factor',
  '短期动量': 'Short-term Momentum',
  '价值+动量': 'Value + Momentum',
  '反转策略': 'Reversal Strategy',
  '自适应策略': 'Adaptive Strategy',
  '纳斯达克100指数': 'NASDAQ-100 Index',
  '标普500指数': 'S&P 500 Index',
  '测试策略': 'Test Strategy',
};

function tStrategy(name, accountId) {
  if (!name) return '';
  // B-group: collapse all verbose GP nicknames to a neutral label in both
  // languages (matches how trade-overview cards present it).
  if (accountId && /^B/i.test(accountId)) return t('gp_evolved_factor');
  if (getLang() === 'zh') return name;
  if (STRATEGY_EN[name]) return STRATEGY_EN[name];
  // Fallback: GP·… / 测试策略 etc — strip Chinese when we don't have a mapping
  // but keep ASCII / alphanum (e.g. "GP Evolved Factor" for stray B rows).
  if (/^GP[·・]/.test(name)) return t('gp_evolved_factor');
  return name;
}
window.tStrategy = tStrategy;

function getLang() {
  return localStorage.getItem(LANG_STORE_KEY) || 'en';
}

function setLang(lang) {
  if (!I18N_DICT[lang]) return;
  localStorage.setItem(LANG_STORE_KEY, lang);
  applyStaticI18n();
  // Re-render current route so all dynamic text refreshes.
  if (typeof navigate === 'function') navigate();
}

function t(key, params) {
  const lang = getLang();
  const dict = I18N_DICT[lang] || I18N_DICT.en;
  let s = dict[key];
  if (s == null) s = (I18N_DICT.en[key] != null) ? I18N_DICT.en[key] : key;
  if (params && typeof s === 'string') {
    s = s.replace(/\{(\w+)\}/g, (_, k) => (params[k] != null ? params[k] : `{${k}}`));
  }
  return s;
}

// Apply translations to elements in the static shell (index.html) that
// have a data-i18n attribute. Also updates <title>.
function applyStaticI18n() {
  document.documentElement.lang = getLang() === 'zh' ? 'zh-CN' : 'en';
  document.title = t('title_doc');
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    el.textContent = t(key);
  });
  // Sync language selector state
  const sel = document.getElementById('lang-select');
  if (sel) sel.value = getLang();
}

window.addEventListener('DOMContentLoaded', () => {
  applyStaticI18n();
  const sel = document.getElementById('lang-select');
  if (sel) {
    sel.value = getLang();
    sel.addEventListener('change', (e) => setLang(e.target.value));
  }
});

window.t = t;
window.getLang = getLang;
window.setLang = setLang;
window.applyStaticI18n = applyStaticI18n;
