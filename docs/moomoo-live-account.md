# Moomoo真实账户模块：连接与启用

## 当前状态

页面：`#/live-account`

当前生产状态是安全默认：

- 官方`moomoo-api` SDK已安装；
- Moomoo OpenD尚未连接；
- 真实账户尚未选择；
- 真实下单关闭；
- B16自动交易关闭；
- 页面不会读取或回退到模拟交易数据库。

## 数据来源

连接后，以下信息只从Moomoo OpenD读取：

- 真实账户总资产、现金、购买力；
- 当前持仓和可卖数量；
- Moomoo实时快照行情；
- 订单和订单状态；
- 成交记录；
- 下单、撤单结果。

Dashboard会把Moomoo净值快照保存到独立文件`data/moomoo_live_audit.db`，用于“连接后收益”与净值曲线。该文件不包含交易密码、交易令牌或预览签名，也不会写入`quant-trading/data/trading.db`。

## 安全连接OpenD

不要把OpenD的`11111`端口暴露到公网。优先选择：

1. OpenD运行在Dashboard同一台受保护主机，绑定`127.0.0.1`；或
2. OpenD运行在受信任设备，通过SSH/VPN私网隧道让Dashboard只能访问本地回环端口。

先登录OpenD并确认Moomoo App侧授权正常，再配置Dashboard。

## 服务器环境变量

建议通过权限为`0600`的systemd EnvironmentFile加载，不要写入仓库：

```bash
MOOMOO_OPEND_HOST=127.0.0.1
MOOMOO_OPEND_PORT=11111
MOOMOO_SECURITY_FIRM=FUTUAU
MOOMOO_TRADE_MARKET=US
MOOMOO_CURRENCY=USD
MOOMOO_ACCOUNT_ID=<真实账户ID>
MOOMOO_DEDICATED_ACCOUNT_CONFIRMED=false

# 第一阶段保持关闭
MOOMOO_TRADING_ENABLED=false
MOOMOO_AUTO_TRADING_ENABLED=false

# 仅在完成只读验收后配置；不要发到聊天或提交Git
MOOMOO_READ_API_TOKEN=<独立高熵只读令牌>
MOOMOO_TRADE_API_TOKEN=<高熵随机令牌>
MOOMOO_TRADE_PASSWORD_MD5=<交易密码MD5，只保存在服务器环境>
```

应用重启后重新读取环境：

```bash
sudo systemctl restart trading-dashboard.service
```

## B16实盘策略参数

页面默认展示此前盘中参数搜索得到的影子候选：

```bash
MOOMOO_STRATEGY_ID=B16
MOOMOO_TOP_N=6
MOOMOO_POSITION_TARGET_PCT=0.147
MOOMOO_GROSS_TARGET_PCT=0.88
MOOMOO_STOP_LOSS_PCT=0.08
MOOMOO_STOP_COOLDOWN_HOURS=72
MOOMOO_MIN_HOLD_DAYS=0
MOOMOO_HOLD_BAND_MULT=4
MOOMOO_REBALANCE_HOURS=12
```

订单级安全参数：

```bash
MOOMOO_MINIMUM_NAV=10000
MOOMOO_MAX_ORDER_NOTIONAL=2500
MOOMOO_MAX_DAILY_ORDER_NOTIONAL=5000
MOOMOO_MAX_LIMIT_DEVIATION_PCT=0.02
MOOMOO_PREVIEW_TTL_SECONDS=90
MOOMOO_RTH_ONLY=true
MOOMOO_MAX_QUOTE_AGE_SECONDS=120
MOOMOO_ACTIVITY_LOOKBACK_DAYS=90
```

## 启用阶段

### 阶段一：只读验收

保持：

```bash
MOOMOO_TRADING_ENABLED=false
MOOMOO_AUTO_TRADING_ENABLED=false
```

核对：

- 页面账户ID与Moomoo一致；
- 资产币种为USD；
- 总净值至少$10,000；
- 每一只持仓、数量、可卖数量和成本一致；
- Moomoo行情时间与价格合理；
- 订单和成交列表一致；
- 页面明确显示`READ-ONLY SAFE MODE`。

### 阶段二：人工小额限价单

仅在阶段一通过后设置：

```bash
MOOMOO_TRADING_ENABLED=true
MOOMOO_AUTO_TRADING_ENABLED=false
```

页面只允许：

- US股票；
- BUY或SELL；
- 整数股；
- DAY限价单；
- 禁止卖空、融资和盘前盘后成交；
- 单笔不超过服务器限额；
- 限价与Moomoo最新价偏离不超过2%；
- 买入不超过可用现金；
- 卖出不超过可卖数量；
- 账户净值不少于$10,000；
- 预览90秒内有效；
- 输入`PLACE LIVE ORDER`和独立交易令牌后才能提交。

一次性的隔夜验收可由操作者明确授权后临时设置：

```text
MOOMOO_MANUAL_OVERNIGHT_TEST_ENABLED=true
MOOMOO_AUTO_TRADING_ENABLED=false
```

该门禁仅允许`OVERNIGHT`市场状态下的`BUY 1股`、`DAY LIMIT`，signed preview绑定session；Broker提交必须使用`Session.OVERNIGHT`和`fill_outside_rth=true`。价格偏离以新鲜隔夜ask为基准。验收结束后立即恢复为false。SELL、多股、自动交易、其他扩展时段均拒绝。

### 阶段三：自动量化交易

当前模块展示自动交易参数和独立开关，但**没有开启生产自动下单调度**。在完成至少一次人工小额订单、撤单、成交和审计核对前，不应打开`MOOMOO_AUTO_TRADING_ENABLED`。后续自动执行器应独立完成：冻结信号、幂等订单ID、部分成交处理、订单对账、断线恢复、日内风险预算和kill switch。

## Fail-closed规则

任一条件不满足即拒绝下单：

- OpenD不可达；
- SDK错误；
- 找不到或找到多个真实账户；
- 真实下单总开关关闭；
- 交易令牌错误；
- 解锁MD5缺失；
- 预览被篡改或过期；
- Moomoo实时行情失效；
- 净值、现金、持仓或价格门禁失败。

## 不可突破的$10,000子账本

Moomoo整账户资金和持仓只作为券商事实展示，不能直接形成策略购买力。实盘策略使用独立数据库`data/live_strategy.db`：

```text
初始策略资金：         USD 10,000
最大策略持仓+BUY预留： USD 10,000
强制冻结权益：         USD 7,500
交易时段：             美股正常盘中9:30–16:00 ET
```

这些值是数据库CHECK约束和服务端常量，不属于Dashboard可编辑参数。账户里超过USD 10,000的现金不能增加策略购买力。

只有下列股份属于本系统：

1. 订单备注由服务端生成`dashboard:<strategy>:<preview>`；
2. Moomoo返回该订单的真实成交；
3. 五分钟对账器取得费用记录并成功应用该成交；
4. 成交外部引用只以SHA-256用于幂等，原始引用不进入源码。

其他Moomoo股票显示为`EXTERNAL READ-ONLY`，系统SELL预览会拒绝出售它们。若账户中同一股票同时含系统股份和外部股份，可卖上限仍取独立子账本数量。

市场跳空可能令已持有股票的市值瞬间超过USD 10,000，这是交易系统无法在价格跳变前物理阻止的市场机制。发生后系统禁止加仓、立即freeze并要求在正常交易时段处理减仓。

共享Moomoo账户采用逻辑子账本：Broker股份在账户层合并且可替代，系统只按已证明模块成交维护策略owned数量、成本和收益。系统无法阻止用户在Moomoo App中手工卖出；专用账户仍是风险更低的优先方案，使用`MOOMOO_DEDICATED_ACCOUNT_CONFIRMED=true`。

若券商无法提供专用账户，操作者可明确接受剩余风险并选择受限共享模式：

```text
MOOMOO_ACCOUNT_MODE=SHARED_RESTRICTED
MOOMOO_SHARED_ACCOUNT_RISK_ACCEPTED=true
MOOMOO_DEDICATED_ACCOUNT_CONFIRMED=false
```

显式模式、专用账户证据和共享风险接受必须一致，否则配置为invalid。受限共享模式采用可替代股份的逻辑子仓：

- Broker个人持仓作为透明背景，策略允许BUY同一代码；
- SELL不超过本地可证明的系统持股减去系统挂单预留；
- 个人持仓、成本和收益不导入策略账本；
- 共享模式要求Broker总数量始终不少于本地策略owned数量，低于时freeze；
- 额外Broker现金不增加USD 10,000策略购买力；
- 订单预览绑定账户隔离模式，模式变化后旧预览失效；
- 页面始终显示“逻辑隔离，不是物理隔离”；
- 账户、模式、风险接受、交易开关或策略配置变化都会增加持久control generation、freeze并删除旧同步证明。

该模式不能指定券商税务lot，也不能防止用户在Moomoo App中手工卖出；只要Broker总股数仍覆盖策略owned数量，逻辑子仓继续有效。若总股数不足则在后续对账冻结。生产启用必须另行完成RTH人工小额订单验收，不能仅凭设置环境变量直接开启自动交易。

## Freeze状态机

```text
FROZEN -> ACTIVE     仅在Moomoo成功对账、权益高于USD 7,500、明确确认及控制令牌通过后
ACTIVE -> FROZEN     一键手工freeze或任一确定性风险异常
FROZEN -> CLEANED    仅空仓且无BUY预留；先归档，再删除有效策略配置
```

权益小于等于USD 7,500时，loss-floor freeze被锁定；参数修改不能重置。Freeze禁止新订单，但允许系统识别和报告待撤订单。是否在loss-floor触发时强制卖出持仓，需上线前由用户明确决定；当前安全默认是不自动产生新的卖出成交。

Unfreeze要求对账时间晚于最近一次freeze/参数更新且不超过7分钟；freeze前的旧同步不能复用。关闭新订单总开关不会关闭紧急撤单能力，撤单仍仅限本模块经本地preview证明的订单。

## 参数热更新

Dashboard可编辑：持仓数、单股目标、总目标敞口、止损、冷却、最短持仓、持有缓冲、再平衡、订单金额、每日金额、限价偏离、行情最大年龄。

每次更新必须提供：

- 独立`MOOMOO_CONTROL_API_TOKEN`；
- 当前配置版本；
- 变更原因。

服务端采用乐观版本锁并持久化新版本。订单预览每次从SQLite重新读取活动版本，不依赖进程缓存。更新立即写入并自动freeze为`config_changed_requires_review`；旧订单预览绑定旧版本而失效，人工复核后才能unfreeze。`USD 10,000 / USD 7,500 / RTH only / 禁止卖空融资`不可编辑。

## 本地日志与对账

本地权限目录：

```text
logs/live_account/dashboard-api.jsonl
logs/live_account/moomoo-sync.jsonl
logs/live_account/health-watchdog.jsonl
data/live_strategy.db
data/moomoo_live_audit.db
```

结构化日志不记录请求头、正文、令牌或密码。事件表记录因子计算、信号、成交、freeze、参数reload、同步和cleanup。日志文件轮转，权限为`0600`；目录为`0700`。

## 调度模块

以下永久调度已创建但在连接验收前全部暂停：

- 每5分钟：Moomoo成交/费用/持仓/价格对账；
- 每10分钟：确定性健康检测，异常直接freeze并Telegram告警；
- 每10分钟：GPT-5.6健康归因；
- 美股盘中每10分钟：GPT-5.6只读Paper候选研究；
- 工作日22:15 UTC：GPT-5.6盘后报告。

AI没有unfreeze、实盘配置修改或broker mutation权限。确定性watchdog先于AI执行，避免模型/网络失效导致风险门禁失效。

健康AI使用只读诊断快照；只有确定性watchdog可以写freeze或执行紧急撤单。

Cleanup必须同时证明本地零持仓/零BUY预留、Moomoo零本模块活动订单且没有未知Broker结果。SQLite归档使用online backup合并WAL后再打包，不能直接复制运行中的主文件。

## Paper对照

页面实线为USD 10,000实盘子账本；虚线明确标记`PAPER`。当前只读同步包括：

- A02 paper账户参考曲线；
- A09 paper账户参考曲线；
- B16调参候选：8%止损、72小时冷却、Top 6、无移动止损的1小时研究回放。

Paper数据永远不能直接进入实盘执行器。任何候选晋级都必须另行通过时间切分、交易成本、换手、回撤和影子运行门槛。

## 凭据存储

systemd只读取仓库外的可选文件：

```text
/home/gexin/.config/trading-dashboard/moomoo.env
```

上线时该文件必须为`0600`，仅由`gexin`拥有。禁止把真实账户ID、交易流水号、密码、MD5、令牌或密钥写入源码、Git、文档、Telegram摘要或普通日志。连接阶段再通过私密交互逐项提供，不提前收集。
