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
