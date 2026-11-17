# app/core/config.py
"""唯一配置入口。

历史问题：项目曾同时存在 app/core/config.py、app/config/settings.py、
app/settings.py 三份互不兼容的 Settings，导致同一进程内的模块读到不同配置。
本文件是唯一实现，其余位置只能从 here 导入。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 应用 ----------------
    app_name: str = "personal-agent-platform"
    app_version: str = "0.2.0"
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=list)

    # ---------------- 日志 ----------------
    log_level: str = "INFO"
    log_json: bool = False

    # ---------------- 数据库 ----------------
    # 业务库使用 SQLAlchemy 异步驱动：
    #   postgresql+psycopg://...  (psycopg3 支持 async)
    #   sqlite+aiosqlite:///./data/app.db（仅本地/测试）
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    database_echo: bool = False

    # LangGraph 会话状态库（checkpoints / checkpoint_blobs / checkpoint_writes）。
    # 只支持 PostgreSQL：多副本部署时各实例必须看到同一份会话状态，
    # 进程本地的 SQLite 文件无法满足，因此不再提供 SQLite 后端。
    # 留空表示复用 database_url 指向的库（同一个 PG 实例、不同的表）。
    checkpoint_database_url: str = ""
    checkpoint_pool_size: int = 5
    # 是否在启动时执行 LangGraph 的表结构迁移（setup）。
    #
    # 生产必须为 false：应用的数据库角色只有 DML 权限（没有 schema CREATE），
    # 这是 RLS 第二道防线成立的前提——应用运行时不该具备 DDL 能力。
    # 表结构由管理账号离线执行 `python -m scripts.init_checkpointer` 建立。
    # 本地开发/测试保持 true，省去额外一步。
    checkpoint_auto_setup: bool = True

    # ---------------- 审批 ----------------
    approval_expire_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "APPROVAL_EXPIRE_SECONDS",
            "APPROVAL_DEFAULT_TTL_SECONDS",
        ),
    )
    executing_stale_seconds: int = 120
    maintenance_interval_seconds: int = 30
    maintenance_enabled: bool = True
    # 多实例时用 PG 事务级 advisory lock 互斥：同一时刻只有一个实例在扫。
    # 取值范围 0 < key < 2^63，实例之间必须一致。
    maintenance_advisory_lock_key: int = 1347375181
    maintenance_require_leader: bool = True

    # ---------------- 模型 ----------------
    # 这些只是「没有在控制台配供应商时」的兜底值。
    # 实际生效的模型由 model_providers 表中 is_active 的供应商决定，
    # 见 app/services/model_service.py。
    model_name: str = Field(
        default="deepseek-flash",
        validation_alias=AliasChoices("OPENAI_MODEL", "MODEL_NAME"),
    )
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    model_temperature: float = Field(
        default=0.0,
        validation_alias=AliasChoices("AGENT_TEMPERATURE", "MODEL_TEMPERATURE"),
    )
    model_timeout_seconds: int = 30
    model_max_retries: int = 2

    # ---------------- 工具 ----------------
    tool_default_timeout_seconds: int = 20
    read_tool_rate_limit_per_minute: int = 60
    write_tool_rate_limit_per_minute: int = 10

    # ---------------- 代码沙箱 ----------------
    # Agent 运行在本平台，但**不执行**模型或用户给出的代码：
    # 所有执行都通过 HTTP 委托给外部沙箱服务（execute / 轮询结果）。
    # 未配置 base_url 时 execute_code 工具不注册，模型看不到这项能力，
    # 避免出现“能调用但一定失败”的工具。
    sandbox_base_url: str | None = None
    sandbox_api_key: str | None = None
    # 单个执行任务的墙钟预算（秒），包含排队与轮询等待。
    sandbox_timeout_seconds: int = 30
    # 提交返回 202 时的轮询间隔。
    sandbox_poll_interval_seconds: float = 0.5
    sandbox_max_code_bytes: int = 20000
    # 单段输出上限：沙箱可以打印海量内容，超限部分必须截断，
    # 否则会把上下文和审计日志撑爆。
    sandbox_max_output_chars: int = 8000
    # 平台侧语言白名单，与沙箱自身能力取交集。
    sandbox_allowed_languages: list[str] = Field(default_factory=lambda: ["python", "javascript"])
    # 默认要求审批：执行任意代码等同于写操作，可能产生文件、网络等副作用。
    sandbox_requires_approval: bool = True

    # ---------------- 限流 ----------------
    # 限流计数必须跨进程共享，否则 N 个 worker 就是 N 份额度。
    # auto   : 配了 REDIS_URL 走 Redis，否则退回进程内计数（仅限本地调试）
    # redis  : 强制 Redis，连不上即启动失败
    # memory : 强制进程内计数，生产环境会被拒绝
    rate_limit_backend: Literal["auto", "redis", "memory"] = "auto"
    redis_url: str | None = None
    redis_timeout_seconds: float = 1.0
    # Redis 不可用时的策略。默认 fail-closed：宁可拒绝，也不在失去全局限流时放行。
    rate_limit_fail_open: bool = False

    # 场站平台。未配置 base_url 时，场站工具以显式 mock 模式运行，
    # 返回结果会带 source=mock，避免被当成真实外部数据。
    station_platform_base_url: str | None = None
    station_platform_timeout_seconds: int = 10
    station_platform_read_retry_attempts: int = 2

    # ---------------- 远端 MCP ----------------
    # 默认关闭：关闭时不会注册任何 MCP 工具，模型连这些工具的存在都不知道。
    # 开启后也不是「远端有什么就暴露什么」——必须先在 app/tools/mcp_tools.py
    # 的白名单里登记，未登记的远端工具永远不会出现在模型目录里。
    #
    # 这个开关只管**旧路径**（全局单一端点 + 代码白名单 + 环境变量令牌）。
    # 连接器（`/api/v1/connectors`）有自己的开关：每个连接器自带 `enabled`，
    # 建、改、启停都要平台管理员并落审计。两者不绑在一起，是因为绑了会出现
    # 「连接器页显示已接入、工具目录里却没有」这种只有在调用失败时才暴露的状态。
    mcp_enabled: bool = False
    # Streamable HTTP 端点，例如 http://127.0.0.1:8765/mcp
    mcp_server_url: str | None = None
    # 建立会话与拉取工具清单的上限。单个工具的超时在工具表里逐个声明
    # （报表/趋势类要明显更长）。
    mcp_connect_timeout_seconds: float = 15.0
    # 按租户分配的访问令牌，环境变量里写 JSON：
    #   MCP_TENANT_TOKENS={"default": "<token>", "test": "<token>"}
    # 没有配置的租户直接拒绝，不做「回落到某个默认令牌」的隐式授权——
    # 那等于让一个租户用另一个租户的身份去读数据。
    mcp_tenant_tokens: dict[str, str] = Field(default_factory=dict)
    # 单次远端结果回给模型的字符上限。趋势曲线、站点目录动辄上万字符，
    # 原样塞进上下文既贵又会挤掉真正的指令。
    mcp_max_result_chars: int = 12000

    # ---------------- 身份 ----------------
    # dev     : 允许用 X-Authenticated-Subject 直接指定主体，仅限本地开发
    # gateway : 只信任可信身份网关注入的头，生产必须使用该模式
    # jwt     : 本服务自行验签 Bearer Token（HS256），从声明里取租户与角色
    auth_mode: Literal["dev", "gateway", "jwt"] = "dev"
    default_tenant_id: str = "default"
    # 平台管理租户：其成员持有 platform_admin 角色，才能使用跨租户管理通道。
    platform_tenant_id: str = "platform"
    dev_subject: str = "dev-user"

    # JWT（auth_mode=jwt 时生效）。只支持 HS256：
    # OIDC 的 RS256/JWKS 建议由可信身份网关完成验签后走 gateway 模式。
    jwt_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_leeway_seconds: int = 30

    # 请求头里的租户只能作为「切换组织」的申请，必须再校验成员关系。
    tenant_header_override_enabled: bool = True

    # 写租户表时缺少上下文一律失败。关掉只在迁移/数据修复时使用。
    tenant_strict_writes: bool = True
    # 租户工具策略/配额的进程内缓存时间（秒）。
    tenant_policy_cache_seconds: float = 5.0

    # ---------------- 多租户配额 ----------------
    # 单租户未单独配置时沿用的默认上限。
    default_daily_chat_requests: int = 5000
    default_tenant_tool_calls_per_minute: int = 600

    # ---------------- 附件对象存储 ----------------
    # 目录内按 tenants/<tenant>/users/<user>/ 分前缀，密钥不进库。
    artifact_storage_dir: str = "./data/artifacts"
    artifact_max_bytes: int = 10 * 1024 * 1024

    # ---------------- 知识库 ----------------
    # 目录扫描源：运维放进去的文件，``sync`` 会把它当作「该租户的全集」，
    # 目录里没有的 file 类文档视为已删除。
    knowledge_dir: str = "./data/knowledge"
    # 页面上传的文档落盘位置。**必须与 knowledge_dir 分开**：目录扫描认为
    # 「目录里没有的 = 已删除」，两者混在一起会让上传的文档在下次扫描时被清掉。
    knowledge_upload_dir: str = "./data/knowledge_uploads"
    # 仅用于 `scripts.ingest_knowledge --export-json` 导出快照；
    # 运行时检索只读 PostgreSQL 的 documents / document_chunks，不再读本地文件。
    knowledge_store_file: str = "./data/index/chunks.json"
    knowledge_top_k: int = 4
    knowledge_min_score: float = 0.05
    # BM25 分数没有绝对量纲：语料大小、切片长短一变，同样的相关性可以打出
    # 1.9 也可以是 0.02。绝对阈值因此基本拦不住任何东西。这里改用相对值——
    # 低于第一名这个比例的候选直接丢弃，0 表示关闭。
    knowledge_min_relative_score: float = 0.3
    # 语义重筛：BM25 召回池扩到 candidates 条，交给现有 chat 模型判
    # 「能否部分回答问题」，剔除字面撞词的假阳性。不需要 embedding 端点。
    # 关掉它（False）就完全回到纯 BM25，检索链路不会发起任何模型调用。
    knowledge_rerank_enabled: bool = True
    knowledge_rerank_candidates: int = 12
    knowledge_rerank_timeout_seconds: float = 8.0
    # 字段加权：把标题 / section 的分词重复若干次并入 BM25 索引。
    # 原先只索引正文，标题里的词（尤其无标题文档的唯一语义标签）完全不参与召回；
    # 这不等价于向量检索，只是把已有的结构化元数据用起来。0 = 回到只索引正文。
    knowledge_title_boost: int = Field(default=2, ge=0, le=10)
    knowledge_section_boost: int = Field(default=1, ge=0, le=10)
    # 查询侧领域同义词扩展。廉价补丁，只覆盖登记过的词对，不是语义检索；
    # 默认关闭，评测脚本用它做 A/B 对照，别把它当成破召回上限的方案。
    knowledge_query_expansion_enabled: bool = False
    knowledge_max_file_bytes: int = 5 * 1024 * 1024
    # 单篇文档切片上限。防线针对的是**量级失控**（一本 5MB 手册能切几千片，
    # BM25 每轮检索都要全扫），不是「比上一份多一点」。
    # 这个数字改过三次，每次都是被真实的现行国标顶上去的：400 卡住 448 片的
    # GB/T 3836.1，800 卡住 973 片的 GB/T 3836.4（56.8 万字，平均 584 字/片，
    # 颗粒度正常）。所以它现在也是运行时配置项
    # （`knowledge.max_chunks_per_document`，见 `app/services/runtime_settings.py`），
    # 这里只是没有库记录时的回落值：2000 给 973 片留了一倍余量。
    # 真正该拆文档的是「切碎了」——平均每片几十个字，那种入库也检不出东西。
    knowledge_max_chunks_per_document: int = 2000
    # 索引指纹轮询间隔：多进程要在不重启的前提下看到别人重新入库的内容。
    knowledge_index_refresh_seconds: float = 5.0

    # ---------------- OCR（走 MCP） ----------------
    # 识别能力不在平台内：本机 OCR 服务以 MCP（Streamable HTTP）暴露
    # `ocr_file(file_path)`，平台只负责把附件句柄解析成路径再调用它。
    # 没有「启用 OCR」开关——地址为空就是没接，工具会明确报未配置。
    ocr_attachment_dir: str = Field(
        default="./data/uploads",
        validation_alias=AliasChoices("OCR_ATTACHMENT_DIR"),
    )
    ocr_max_image_bytes: int = Field(
        default=10 * 1024 * 1024,
        validation_alias=AliasChoices("OCR_MAX_IMAGE_BYTES"),
    )
    # PDF 与图片分开限：多页 PDF 天然比截图大，用一个上限会逼着改小图片限制。
    ocr_max_pdf_bytes: int = Field(
        default=20 * 1024 * 1024,
        validation_alias=AliasChoices("OCR_MAX_PDF_BYTES"),
    )
    ocr_mcp_url: str = Field(
        default="http://127.0.0.1:4499/mcp",
        validation_alias=AliasChoices("OCR_MCP_URL"),
    )
    # 该服务目前不鉴权，留空即不发 Authorization 头；
    # 换成需要令牌的部署时在这里配，绝不借用燃气 MCP 的租户令牌。
    ocr_mcp_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OCR_MCP_TOKEN"),
    )
    # PDF 逐页渲染再识别，耗时随页数线性增长，120 秒是实测 20 页量级的上限。
    ocr_mcp_timeout_seconds: int = Field(
        default=120,
        ge=5,
        le=600,
        validation_alias=AliasChoices("OCR_MCP_TIMEOUT_SECONDS"),
    )
    # 识别结果回填给模型的上限。MCP 只回一段纯文本，页边界已经拿不到了，
    # 只能用字符数兜住上下文：一份 200 页 PDF 的全文足够顶爆窗口。
    ocr_max_text_chars: int = Field(
        default=40000,
        ge=1000,
        le=200000,
        validation_alias=AliasChoices("OCR_MAX_TEXT_CHARS"),
    )

    # ---------------- 对话附件 ----------------
    # 单条消息允许携带的附件数。前端也是这个上限，两处必须一致，
    # 否则用户会看到「选得上、发不出」。
    chat_max_attachments: int = Field(
        default=6,
        ge=1,
        le=20,
        validation_alias=AliasChoices("CHAT_MAX_ATTACHMENTS"),
    )
    # 单条消息所有附件的原始字节合计上限（不含 base64 膨胀）。
    chat_max_attachment_bytes: int = Field(
        default=40 * 1024 * 1024,
        validation_alias=AliasChoices("CHAT_MAX_ATTACHMENT_BYTES"),
    )
    # ---------------- 智能体推理回路 ----------------
    # 单次请求内允许的「工具执行 → 重新规划」轮次上限。
    # OCR 之类的工具必须能与后续业务工具串联，但必须收敛，不能无限自循环。
    agent_max_tool_steps: int = Field(
        default=3,
        ge=1,
        le=8,
        validation_alias=AliasChoices("AGENT_MAX_TOOL_STEPS"),
    )

    # 单次对话请求的**总时限**（秒），从进入图到出结果。
    # 这是推理回路之外的最后一道闸：客户端断开不会取消后端协程，
    # 光靠「客户端超时」只会让服务端继续空转（2026-11-04 实测空转 45 分钟、
    # 521 次模型调用）。必须由服务端自己放弃。
    # 必须**明显大于** ocr_mcp_timeout_seconds：PDF 单次识别上限 120s，
    # 若总时限也取 120s，大 PDF 永远死在总时限上，识别工具的超时形同虚设。
    # 160s = 120s 识别 + 约 40s 规划/后续工具，仍是有界等待。
    chat_timeout_seconds: int = Field(
        default=160,
        ge=10,
        le=600,
        validation_alias=AliasChoices("CHAT_TIMEOUT_SECONDS"),
    )

    # ---------------- 长对话上下文（docs/CONTEXT.md） ----------------
    # 关掉等于回到「每轮只带当前这一条」的行为：不注入历史、不压缩。
    # 默认开：不开就永远不知道「模型记不住上文」造成了多少追问，
    # 而这个损失是不可见的；开着的成本是可量化的。
    context_enabled: bool = True
    # 模型的上下文窗口。**当前是唯一来源**：供应商级 `models[].context_window`
    # 尚未实现（没人读它，填了会被静默忽略），别照 docs/CONTEXT.md 旧版描述以为
    # 「供应商配了以此为准」。见 §3.2。
    # **默认值刻意保守**：宁可早压，不要撞上限——
    # 早压的代价是少看几轮原文，撞上限的代价是整轮失败。
    context_window_tokens: int = Field(
        default=32000,
        ge=2000,
        le=2_000_000,
        validation_alias=AliasChoices("CONTEXT_WINDOW_TOKENS"),
    )
    # 上一轮实测 token 达到窗口的多少比例时触发压缩。
    # 只有实测，不含估算：估算的误差不足以决定「要不要花一次模型调用」。
    context_compact_ratio: float = Field(
        default=0.6,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CONTEXT_COMPACT_RATIO"),
    )
    # 超过这个比例不再等摘要，直接按窗口硬截断。
    context_hard_ratio: float = Field(
        default=0.85,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CONTEXT_HARD_RATIO"),
    )
    # 摘要生成的独立时限。必须单独设：聊天总时限只包住 graph.invoke，
    # 压缩在它前面跑，不单独设就会变成「压缩 30s + 图 160s」的黑洞。
    context_timeout_seconds: int = Field(
        default=20,
        ge=3,
        le=120,
        validation_alias=AliasChoices("CONTEXT_TIMEOUT_SECONDS"),
    )

    # ---------------- 对外 MCP 通道（docs/MCP_SERVER.md） ----------------
    # 让外部宿主（WorkBuddy 连接器等）把一整件事委派进来。
    #
    # 与上面 `mcp_enabled` 系列不是一回事：那组是 **出向**，PAP 作为客户端
    # 去连别人的 MCP 服务；这组是 **入向**，PAP 作为服务端被别人调用。
    # 两套配置各占一组名字，是因为「连出去」和「被连进来」的凭据、
    # 超时、暴露面全都不一样，合成一个开关必然出现「只想关一边」的场景。
    mcp_inbound_enabled: bool = False
    # 静态兜底令牌。**不是主路径**：正常调用走个人访问令牌（`api_tokens` 表，
    # 控制台自助签发），那样审计里才查得到「谁下的单」。
    # 这一枚只对应一个固定主体，留给单机自测和「表还没建好」的时刻；
    # 留着不配也安全——见下方校验器注释，无凭据时鉴权是 fail-closed。
    mcp_inbound_token: str | None = None
    # 静态令牌对应的主体与租户。**必须指向平台内已存在的账号**：
    # gateway 模式下未知主体一律拒绝，不会自动建号。
    mcp_inbound_subject: str = "workbuddy-connector"
    mcp_inbound_tenant: str | None = None
    # 单次委派的同步等待上限（秒）。超过就返回 pending + task_id，由调用方
    # 用 pap_task_status 取回。**刻意远小于 CHAT_TIMEOUT_SECONDS(160)**：
    # MCP 客户端自己会先超时，让请求挂 160s 的结果是调用方看到连接中断，
    # 而不是「还在跑」。
    mcp_inbound_wait_seconds: float = Field(
        default=30.0,
        ge=3.0,
        le=120.0,
        validation_alias=AliasChoices("MCP_INBOUND_WAIT_SECONDS"),
    )
    # 用整段 JSON 响应代替 SSE 事件流。默认开：现阶段工具调用没有进度事件，
    # SSE 只多出一层「代理缓冲把它吃掉」的风险。要推进度事件时再关掉，
    # 同时必须在反代上 proxy_buffering off。
    mcp_inbound_json_response: bool = True

    # ---------------- 可观测性 ----------------
    otel_service_name: str = "personal-agent-platform"
    otel_exporter_otlp_endpoint: str | None = None

    # 长期记忆：把对话里稳定的事实记下来，下一轮作为背景注入规划 prompt。
    # 关掉之后接口仍可读写（历史还在），但不再注入——排查「模型为什么突然这么答」时用。
    memory_enabled: bool = True
    # 每轮注入的最大条数与字符预算在 memory_service 里，不放配置：
    # 那是和 prompt 结构强耦合的值，改配置会让线上表现悄悄漂移。
    #
    # 自动抽取：一轮回答落地后，用同一份文字模型判断有没有值得记的事实。
    # 抽出来的是**候选**（is_user_confirmed=false），可以在记忆页一键确认或删除。
    # 关掉只是不再新增候选，已有记忆照常注入。
    memory_extract_enabled: bool = True
    # 梦境（dreaming）：离线用会话原文校验自动候选，产出「纠错 / 合并 / 下架」
    # 建议，人工确认后才生效。不做成每轮后台任务——它不是高频操作，
    # 而是一次模型调用换一条建议，按需触发更划算。
    memory_dream_enabled: bool = True
    # 证据窗口：只扫最近 N 天的 user 消息。越久远的消息越不可能是「当前仍成立」的证据。
    memory_dream_days: int = Field(default=30, ge=1, le=365)
    # 单次梦境的记忆条数上限。每条要一次模型调用，跑太多会让脚本串行等很久。
    memory_dream_max_per_run: int = Field(default=20, ge=1, le=200)
    # 技能：控制台维护的作业指令 + 触发词 + 工具白名单，规划前确定性命中并注入。
    # 关掉之后页面照常可编辑，只是不再参与规划——用于「怀疑某个技能把模型带偏了」
    # 时的快速对照，不必删数据。
    skills_enabled: bool = True

    # ---------------- 可观测性 ----------------
    #
    # 观测开关的**权威在库里**（`platform_settings` 的 `observability.enabled`）：
    # 管理员在控制台「运行配置 → 观测」改动，写完立刻生效，不需要重启
    # （`app/services/runtime_settings.py`）。
    #
    # 这里只是**首次默认值**：库里还没有这条记录时才读它。默认 true——
    # PAP 目前处于开发调试期，观测是排障的主要依据，不该由「漏配」意外关掉。
    # 要用环境变量强压时设 OBS_ENABLED=false，但管理员在控制台设过一次之后，
    # 库里就有记录了，此后以库为准（页面会显示当前值来自 runtime 还是 env）。
    obs_enabled: bool = True
    # 只对「成功」的 trace 生效；错误 trace 一律保留（排查价值最高）。
    obs_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # full：完整正文（开发期默认，见文档 §7）；summary：200 字摘要；off：只记结构。
    obs_capture_content: Literal["full", "summary", "off"] = "full"
    # 保留期（天）。14 天 + 完整原文的组合靠「短保留 + 只在开发期开」控制影响面。
    obs_retention_days: int = Field(default=14, ge=1, le=365)
    # 错误 trace 单独保留更久，便于事后复现。
    obs_keep_error_days: int = Field(default=30, ge=1, le=365)
    # 队列容量：满了丢最旧并计数，宁可丢观测不可拖慢对话。
    obs_queue_size: int = Field(default=2000, ge=100, le=100_000)
    # 单条 content 的上限（字符）。0 表示不限制；默认给一个防爆内存的值。
    # 这不是「脱敏截断」——原文完整保留，只是防止某个异常返回把进程撑爆。
    obs_max_content_chars: int = Field(default=200_000, ge=0)

    # 「在跑」这行的存活上限（秒）。超过就判为残行（进程中途被 kill），
    # 由维护任务改成 abandoned。默认 30 分钟，比单轮最长耗时高一个量级：
    # 调小会把「模型正在慢慢想」误判成事故，调大则残行会在界面上装活人。
    obs_running_ttl_seconds: int = Field(default=1800, ge=60, le=86_400)

    # 运行视图里「等太久」的红线（秒）。超过只是标 stale，不改任何状态——
    # 界面要用它把「有人在等一张没人管的单子」挑出来。
    obs_live_stale_seconds: int = Field(default=900, ge=60, le=86_400)

    @property
    def obs_captures_content(self) -> bool:
        return self.obs_capture_content == "full"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    @property
    def resolved_rate_limit_backend(self) -> Literal["redis", "memory"]:
        """实际生效的限流后端。"""
        if self.rate_limit_backend == "memory":
            return "memory"
        if self.rate_limit_backend == "redis":
            return "redis"
        return "redis" if (self.redis_url or "").strip() else "memory"

    @property
    def resolved_checkpoint_database_url(self) -> str:
        """
        检查点库连接串。

        优先 CHECKPOINT_DATABASE_URL；留空则复用业务库地址。
        结果统一成 psycopg 能直接用的形式：去掉 SQLAlchemy 的 "+psycopg" 方言后缀。
        """
        raw = (self.checkpoint_database_url or self.database_url).strip()

        for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
            if raw.startswith(prefix):
                return "postgresql://" + raw[len(prefix) :]

        return raw

    @property
    def checkpoint_uses_postgres(self) -> bool:
        url = self.resolved_checkpoint_database_url
        return url.startswith(("postgresql://", "postgres://"))

    @property
    def trusts_gateway_identity(self) -> bool:
        """是否按可信网关模式解析身份。

        gateway 模式下，服务只接受反代/网关注入的已认证主体，
        缺少该头即拒绝，也不会回落到具名的开发主体。
        """
        return self.auth_mode == "gateway"

    @model_validator(mode="after")
    def _validate_auth_mode(self) -> Settings:
        """
        生产环境必须由可信身份源提供身份，且限流必须跨进程共享。

        dev 模式会信任任意客户端提交的 X-Authenticated-Subject，
        一旦暴露到公网就等于没有认证，因此不允许在 production 下启动。
        进程内限流在 N 个 worker 下等于 N 份额度，同样是“看起来有、实际没有”，
        所以生产环境也不接受 memory 后端。
        """
        if self.app_env == "production":
            if self.auth_mode == "dev":
                raise ValueError(
                    "生产环境不得使用 AUTH_MODE=dev；"
                    "请配置 gateway（可信身份网关）或 jwt（本服务自行验签）。"
                )

            if self.resolved_rate_limit_backend != "redis":
                raise ValueError(
                    "生产环境必须配置 REDIS_URL（RATE_LIMIT_BACKEND=redis）；"
                    "进程内计数在多进程下不共享，等于把配额乘以进程数。"
                )

        if self.auth_mode == "jwt":
            secret = (self.jwt_secret or "").strip()

            if len(secret) < 32:
                raise ValueError(
                    "AUTH_MODE=jwt 时必须提供至少 32 字符的 JWT_SECRET；"
                    "共用短密钥等于把租户与角色声明交给任何人伪造。"
                )

        if self.rate_limit_backend == "redis" and not (self.redis_url or "").strip():
            raise ValueError("RATE_LIMIT_BACKEND=redis 时必须提供 REDIS_URL。")

        # 入向 MCP 通道：不再要求必须有静态令牌。
        #
        # P0 时这条是硬校验，理由是「没有凭据的对外端点就是匿名入口」。
        # 有了个人访问令牌之后这个前提不成立了：凭据来源变成「静态令牌
        # 或 api_tokens 表」，而两条都没有时 `BearerGate` 一律 401——
        # 是**全拒**，不是**全放**。要求先配一枚静态令牌才能启动，
        # 反而会逼着运维留一枚谁都在用的万能令牌。
        #
        # 「开关开着但没有任何可用凭据」这件事要在挂载时提示，
        # 而这里不打日志：Settings 在 import 阶段实例化，此时日志还没配好，
        # 打出来会被丢掉。提示放在 `main.py` 的挂载处。

        if self.checkpoint_pool_size < 1:
            raise ValueError("CHECKPOINT_POOL_SIZE 必须大于 0。")

        if self.default_daily_chat_requests < 0 or self.default_tenant_tool_calls_per_minute < 0:
            raise ValueError("租户配额默认值不能为负。")

        if self.artifact_max_bytes < 1:
            raise ValueError("ARTIFACT_MAX_BYTES 必须大于 0。")

        if self.tenant_policy_cache_seconds < 0:
            raise ValueError("TENANT_POLICY_CACHE_SECONDS 不能为负。")

        if self.context_compact_ratio > self.context_hard_ratio:
            # 顺序反了会出现「还没到压缩线就硬截断」，压缩永远不会发生，
            # 而硬截断是无声的——没人会从回答变短里看出配置写错了。
            raise ValueError(
                "CONTEXT_COMPACT_RATIO 不能大于 CONTEXT_HARD_RATIO；"
                "否则压缩永远不会触发，只剩硬截断。"
            )

        if (self.sandbox_base_url or "").strip():
            # 沙箱是执行任意代码的地方，绝不能匿名可达：
            # 一个没有鉴权的沙箱等于给公网提供免费算力与出网通道。
            if self.app_env == "production" and not (self.sandbox_api_key or "").strip():
                raise ValueError("配置 SANDBOX_BASE_URL 后，生产环境必须同时提供 SANDBOX_API_KEY。")

            if self.sandbox_timeout_seconds < 1:
                raise ValueError("SANDBOX_TIMEOUT_SECONDS 必须大于 0。")

            if not self.sandbox_allowed_languages:
                raise ValueError(
                    "SANDBOX_ALLOWED_LANGUAGES 不能为空；留空会让平台无法拒绝任何语言。"
                )

        return self

    @property
    def sandbox_enabled(self) -> bool:
        """只有显式配置了地址，代码执行工具才会注册。"""
        return bool((self.sandbox_base_url or "").strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
