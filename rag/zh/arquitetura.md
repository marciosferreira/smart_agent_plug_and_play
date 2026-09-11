# 智能体系统架构

本系统使用一个由三个智能体协作组成的 LangGraph 多智能体图。

## 编排器（Orquestrador）

中枢节点，接收用户的所有消息并决定如何路由：

| 请求类型                                     | 路由方式                                         |
|-----------------------------------------------|---------------------------------------------------|
| 数据查询（图表、表格、报告）                 | `calcular_periodo()` → `consultar_analista()`     |
| 概念性问题（什么是X，Y是如何计算的）         | `rag_*()` 工具 → 直接回答                         |
| 当前仪表盘（屏幕上显示的内容、KPI、提醒）    | `get_dashboard_charts()`                          |
| 定时任务（创建、编辑、列出、暂停、删除）     | `gerenciar_agenda()`                              |
| 简单问题（日期、时间、问候语）               | 直接回答                                          |

编排器通过 SQLite checkpointer 保留完整的会话历史——在同一会话内绝不会丢失
之前对话的上下文。

在任何代码编辑操作（`get_task_code`）之后，一个守护节点会自动检查
`save_task_code` 是否已被调用。如果没有，则会注入一个修正，强制编排器
完成整个流程。

## 分析子代理（Sub-agente analista）

由编排器通过 `consultar_analista()` 触发。执行分析分三个必要步骤：

1. `read_skill('dominio.md')` — 读取配置包中的 schema、关系和规则
2. `executar_sql(query, chave)` — 在已配置的数据库上执行 SELECT，并将 DataFrame 注入到环境中
3. `analisar_dataframe(script)` — 用 pandas 处理数据，生成图表（`result = fig`）或表格（`result = df`）

该环境在同一会话内的多次调用之间持续存在（类似 Jupyter 的方式）——在某一步创建的 DataFrame 会在后续步骤中继续可用。

### 子代理的技能（Skill）

| 文件 | 覆盖内容 |
| --- | --- |
| `dominio.md`（config/） | 唯一的分析技能——已配置业务领域的 schema、关联关系和规则 |

## 定时任务子代理（Sub-agente de scheduling）

由编排器通过 `gerenciar_agenda()` 触发。专门负责定时任务的相关操作：
创建、列出、编辑、暂停、删除、保存和测试代码。

守护进程（daemon）直接执行 `task_code`（`def run(from_date, to_date, ctx)`）——
在定时执行时不涉及 LLM。任务在保存有效的 `task_code`（先用 `test_task_code`
测试）之前，会一直处于 `draft`（草稿）状态，不会执行。`instructions` 只是
用于编写该代码的参考步骤——单靠它本身并不会激活任务。

注入到 `run()` 中的 `ctx` 对象提供以下方法：

| 方法 | 作用 |
| --- | --- |
| `ctx.api(url)` | 调用 `/alerts` 接口（通知铃铛）并以列表形式返回数据 |
| `ctx.sql(query)` | 在已配置的数据库上执行 SELECT，返回字典列表 |
| `ctx.today()` | 以 `YYYY-MM-DD` 格式返回当前日期 |
| `ctx.date_range(days=N)` | 返回最近 N 天的 `(from_date, to_date)` |
| `ctx.save_chart(fig)` | 保存 matplotlib 图形并返回 `[chart:uuid]` 令牌 |
| `ctx.generate_pdf(titulo, conteudo)` | 生成 PDF 并返回 `[pdf:uuid]` 令牌 |
| `ctx.generate_excel(df, nome)` | 生成 Excel 文件并返回 `[excel:uuid]` 令牌 |
| `ctx.notify(msg, value, threshold)` | 在仪表盘上触发通知 |
