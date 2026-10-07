# 智能体系统架构

本系统使用一个 LangGraph 多智能体图：一个编排器、一个分析子代理、一个定时任务子代理，以及一条
用于创建和编辑定时任务的确定性流水线。

## 编排器（Orquestrador）

中枢节点，接收用户的所有消息并决定如何路由：

| 请求类型 | 路由方式 |
| --- | --- |
| 数据查询（图表、表格、报告） | `consultar_analista()` —— 时间段以自然语言传入，并在工具内部解析 |
| 概念性问题（什么是X，Y是如何计算的） | `rag_*()` 工具 → 直接回答 |
| 当前仪表盘（屏幕上显示的内容、提醒）或附带的图片 | `get_dashboard_charts()` |
| 创建或编辑任务或监控 | `criar_tarefa_agendada()` / `editar_tarefa_agendada()` |
| 列出、暂停、恢复或删除任务 | `gerenciar_agenda()` |
| 仪表盘上的自定义面板（添加、编辑、删除） | `consultar_analista()` → `test_widget_code()` → `add_chart_to_dashboard()`；另有 `update_widget()` 和 `delete_dashboard_widget()` |
| 简单问题（日期、时间、问候语） | 直接回答 |

编排器通过 SQLite checkpointer 保留完整的会话历史——在同一会话内绝不会丢失之前对话的上下文
（发送给模型的是最近若干轮交互组成的窗口）。当无法区分是数据查询还是定时任务或面板请求时，它按
查询处理：只有请求中明确出现定时或固定到仪表盘的措辞时，才会创建任务或面板。给用户的最终回答由
编排器根据分析子代理返回的简明报告，用用户的语言撰写。

## 分析子代理（Sub-agente analista）

由编排器通过 `consultar_analista()` 触发。执行分析分三个步骤：

1. 技能 `dominio.md` — 配置包中的 schema、关联关系和规则。它已经加载在分析子代理的提示词中
   （`read_skill()` 只用于其他技能）
2. 数据查询，结果以 DataFrame 形式进入环境。具体方式取决于业务领域（`database.modo`）：
   - SQL：`executar_sql(query, chave)`
   - GraphQL：`executar_graphql(...)`（明细行）、`executar_graphql_agregado(...)`（在服务器端
     按分组求和、计数、平均、最小或最大），以及仅在技能与服务器不一致时使用的
     `executar_graphql_schema()`（真实 schema）
3. `analisar_dataframe(script)` — 用 pandas 处理数据，生成图表（`result = fig`）或表格
   （`result = df`）

该环境在同一会话内的多次调用之间持续存在（类似 Jupyter 的方式）——在某一步创建的 DataFrame 会在
后续步骤中继续可用（闲置10分钟后过期）。超过100行的结果只会被描述（列、类型、第一行）而不完整
展示，以节省上下文。分析子代理只会根据 `analisar_dataframe` 的真实结果作答：不会编造或模拟数据。

### 子代理的技能（Skill）

| 文件 | 覆盖内容 |
| --- | --- |
| `dominio.md`（config/） | 已配置业务领域的分析技能——schema、关联关系和规则 |
| `task_code_sandbox.md`（skills/） | `task_code` 沙箱的规则和示例——创建任务和面板时使用 |

## 定时任务子代理（Sub-agente de scheduling）

由编排器通过 `gerenciar_agenda()` 触发。负责处理已存在任务的操作：列出、暂停/恢复、删除，以及
修改字段（名称、频率、时间、邮箱）。创建任务或编辑任务的**代码**不经过它——那由下面的流水线完成。

## 创建和编辑任务 — 确定性流水线

`criar_tarefa_agendada()` 和 `editar_tarefa_agendada()` 在工具内部执行一个固定流程；编排器不参与
各个步骤：

1. 从请求中提取参数（名称、频率、时间、时间段、报告或监控类型、条件），并以 `draft`（草稿）状态
   创建任务
2. 分析子代理查询真实数据，并返回上下文（查询、列、样例、筛选条件）
3. 生成器编写 `task_code`
4. 代码在沙箱中测试（预览会显示在聊天中）；失败则修复，最多3次尝试
5. 如果请求提到了分析而代码中没有包含，生成器会补上（最多2次尝试）
6. 通过验证的代码被保存，任务变为 `active` —— 编辑时，旧版本会被归档以便回滚

编辑时，流程从任务的当前代码开始：应用所要求的修改，然后从第4步继续。如果流程彻底失败，或用户
停止了回复，草稿会被丢弃，不会留下任何已安排的任务。任务在保存有效的 `task_code`（先用
`test_task_code` 测试）之前，会一直处于 `draft`（草稿）状态，不会执行。`instructions` 只是用于
编写该代码的参考步骤——单靠它本身并不会激活任务。

## 任务的执行

守护进程（daemon）每隔几秒检查一次任务，并直接执行已保存的 `task_code`
（`def run(from_date, to_date, ctx)`）——**在定时执行时不涉及 LLM**（唯一的例外是下文的
`ctx.analisar`）。失败时，任务会以递增的等待时间重新安排（5、15 和 60 分钟），最多再尝试3次；
用尽后进入 `error` 状态等待人工处理。每次运行最长10分钟。运行历史和代码版本显示在"任务"面板中。

### 监控

- **简单**（一个指标、一次比较——例如"当天订单数 >= 160"）：守护进程在运行代码*之前*先评估条件，
  `task_code` 只返回当前值。SQL 业务领域中条件是一条查询；GraphQL 业务领域中则是一个 JSON 规格
- **复合**（与/或、多个指标或对象——例如"JAG > 50 且 BPL < 10"）：由 `task_code` 自己评估完整的
  条件，并在每次运行中调用一次 `ctx.alert(triggered, message)`

两种情况下，提醒都只在条件从"未满足"变为"满足"时发出，并在条件不再成立后重新生效。它会显示在
仪表盘的提醒铃铛中。

### `ctx` 对象

注入到 `run()` 中的 `ctx` 对象提供以下方法：

| 方法 | 作用 |
| --- | --- |
| `ctx.sql(query)` | 在已配置的数据库上执行 SELECT，返回字典列表（仅限 SQL 业务领域） |
| `ctx.graphql_table(table, where, ...)` | 通过 GraphQL 获取明细行，返回字典列表（仅限 GraphQL 业务领域） |
| `ctx.graphql_aggregate(table, group_by, metric_field, metric_op, ...)` | 通过 GraphQL 在服务器端聚合（按某一列做 SUM、COUNT、AVG、MIN 或 MAX）（仅限 GraphQL 业务领域） |
| `ctx.api(url)` | 调用 `/alerts` 接口（通知铃铛）并以列表形式返回数据 |
| `ctx.today()` | 以 `YYYY-MM-DD` 格式返回当前日期 |
| `ctx.date_range(days=N)` | 返回最近 N 天的 `(from_date, to_date)` |
| `ctx.save_chart(fig)` | 保存 matplotlib 图形并返回 `[chart:uuid]` 令牌 |
| `ctx.generate_pdf(titulo, conteudo)` | 生成 PDF 并返回 `[pdf:uuid]` 令牌 |
| `ctx.generate_excel(df, nome)` | 生成 Excel 文件（一个 DataFrame，或字典 `{工作表: DataFrame}`）并返回 `[excel:uuid]` 令牌 |
| `ctx.analisar(fatos, foco, fallback)` | 由 AI 根据代码已计算好的事实撰写分析文字；其中引用的每个数字都会对照事实核对，出现任何问题则改用 `fallback` |
| `ctx.notify(msg, value, threshold)` | 在仪表盘上触发通知（用于报告类任务；监控会自行通知） |
| `ctx.alert(triggered, message, value, threshold)` | 报告复合监控的结果；仅在变为"满足"的那一刻通知 |

## 仪表盘面板

每个面板都是一段 Python 代码（`def run(from_date, to_date, ctx)`），返回一个图表配置；它在每次
刷新时重新运行，时间段由面板中保存的时间窗口（例如"最近30天"）计算得出。**默认**面板来自配置包
（每种语言一个文件），不能删除；**自定义**面板通过聊天创建、保存在数据库中，所有者可以将其共享给
其他用户。代码运行在与任务同类的沙箱中，可用的库更少。

## 界面

回答以实时方式送达（SSE 流式传输）：推理过程和每个步骤——查询、分析、代码生成——在发生时就会
显示出来，"停止回复"按钮可以中断智能体（取消是协作式的：正在进行的步骤会先完成再停止）。输入可以
是文字、语音（由 AI 转写）或附加的屏幕截图。界面、回答和业务领域内容都遵循所选择的语言
（葡萄牙语、英语或中文）。
