"""问财实盘模拟 (Paper Trading) — 自包含新模块。

职责：一个问财策略绑定一个账户，每天拉自然语言选股结果落盘，按配置的
买卖规则（可引用现有自定义信号 csg_* 列）执行模拟成交，落盘每日快照/成交。

不依赖项目策略引擎、回测矩阵或 data_providers capability 体系；仅复用
app.plugins.iwencai_client（问财客户端）与项目数据仓库的日线 enriched 数据。
"""