# 投放台 · 小红书报表与诊断平台

将聚光、乘风计划报表变成可查询的指标、日报和经人工审核的诊断报告。系统整合了作战包的 Pulse（报表）、Brain（知识库与诊断）和 Console（多客户协作）。

## 快速开始

需要 Python 3.10 或以上，演示推荐 3.12。使用系统可用的 Python 命令执行。

```sh
python -m app seed-demo
python -m app serve
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，登录 `demo-manager` / `DemoConsole!2026`。数据库会生成两个租户、两个账户各 28 天的**合成**报表，以及一小套合成 SOP。演示账号只用于本地空库，其他角色使用同一个演示密码。

| 用户 | 角色与操作 |
|---|---|
| `demo-manager` | A 租户主管，管理共享知识、报表、诊断和审计 |
| `demo-operator` | A 账户投手，导入、问数与诊断 |
| `demo-client` | A 账户客户，只读报表与已发布诊断 |
| `demo-other` | B 租户主管，可用于对照租户隔离 |

日常学习也可以直接运行命令行工具：

```sh
python report.py path/to/report.csv --day 2026-11-20
python -m unittest discover -s tests -v
```

## 三分钟演示

1. 主管登录，保持账户 A 默认的 2026-11-20，查看消耗、CTR、CPC、CPL 和计划排行。切换日期范围看消耗趋势。
2. 到「报表问数」输入 `CPL 最高的计划`。到「投放知识库」搜索 `点击率下降怎么排查`，查看文档出处。
3. 到「账户诊断」，点「诊断结束日期」。合成样本刻意降低了当天点击率，可看到「点击」环节、历史数据和三条建议。选中要采纳的建议并发布，可下载 Markdown。以客户只读账号登录，查看已发布结果。

## 导入自己的报表

使用按天、按计划聚合的 CSV，支持 UTF-8、UTF-8 BOM、GB18030。必要字段是日期、计划、消耗、曝光、点击。无日期列的单日 CSV 可以在界面填写日期。线索、互动、订单和成交金额是可选列。缺失值可使相应指标显示为 `—`。各来源的线索口径可能不同，导入前请确认别名对应同一业务含义。

支持 `计划名称/消费/展现量/点击量/直播间留资量` 等表头。特殊列名可以传 JSON，如 `{"spend":"广告费用","leads":"有效留资数"}`。同一个账户同一天只有一份生效版本，选中覆盖会替换整日明细，历史版本可在导入记录中查看。日期和计划重复会整批拒绝。上传上限 4 MiB、50,000 条明细。

私人文件和 SQLite 文件放在仓库外，或放进已忽略的 `private/` 与 `data/`。报告取数、口径和权限见 [架构与设计决策](docs/architecture.md)。

## 运行配置

默认只绑定本机，模型未配置时，规则问数、检索、指标和诊断均可演示。

| 环境变量 | 用途 |
|---|---|
| `APP_DB` | SQLite 文件路径，默认 `data/console.sqlite3` |
| `APP_ORIGIN` | 实际访问地址，HTTPS 部署须设为公开地址以启用 Secure cookie |
| `LLM_PRIMARY_KEY`、`LLM_PRIMARY_URL`、`LLM_PRIMARY_MODEL` | 可选的 OpenAI 兼容聊天接口，默认 URL 为 DeepSeek，模型为 `deepseek-flash` |
| `LLM_BACKUP_KEY`、`LLM_BACKUP_URL`、`LLM_BACKUP_MODEL` | 可选的备用模型 |
| `LLM_PRIMARY_INPUT_RATE` / `OUTPUT_RATE`、`LLM_BACKUP_INPUT_RATE` / `OUTPUT_RATE` | 人民币每百万 token 费率，结合服务商账单调整。默认 2 / 8 |
| `LLM_DAILY_TOKEN_BUDGET` | 每租户每天的用量预算，UTC 重置，默认 120,000，调用前做预留 |
| `REPORT_WEBHOOKS` | 服务端账户 ID → 飞书或企微官方 webhook URL 的 JSON。网页点确认后推送 |

部署到自己的服务器时，使用 `docker compose up -d --build`。服务在主机的 `127.0.0.1:8080`，由 HTTPS 反向代理对外提供访问。使用空库建立自定义账号：

```sh
docker compose exec console python -m app create-user client-a manager-a manager account-a
docker compose exec console python -m app create-user client-a operator-a operator account-a
docker compose exec console python -m app create-user client-a viewer-a client account-a
```

本机运行时可用同样的 `python -m app create-user …`。部署时保留数据库卷并定期备份。模型密钥与 webhook 只通过运行环境配置。

## 评测与交付范围

- 自动化测试覆盖字段映射、脏数据、金额聚合、重复导入、跨租户权限、诊断断点恢复和发布。
- [测试与压测记录](docs/verification.md) 使用合成数据。真实报表对账、诊断准确率及建议可执行率需用你的报表与人工标注复验。
- 当前实现使用 Python 标准库、SQLite、浏览器原生界面和数据库任务状态机。知识库采用中文词片段 BM25 类检索、标题加权重排和可核对的片段引用。FastAPI、DuckDB、LangGraph、独立向量库、双 Agent 和生产监控属于后续升级项。
