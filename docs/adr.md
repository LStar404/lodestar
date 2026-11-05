# 架构决策记录（ADR）

规范第 12 节 D1–D26 为已定稿决策；本文件登记实现期间新增的实现级决策（不改变规范行为契约）。

| # | 决策 | 理由 | 状态 |
|---|---|---|---|
| D27 | 新增内置 `pypdf` 解析适配器（`--parser pypdf`，`auto` 的离线兜底） | Marker/Docling 体积大且需下载模型权重；`auto` 在两者不可用时降级到 pypdf 文本层解析，保证 E 类验收与离线可用；适配器仍按 5.1.5 明确源空间并写 `coordinate_meta`，坐标契约不变 | 实现级 |
| D28 | core 增加 `errors.py`/`paths.py`/`config.py`/`store.py` 辅助模块 | 10.1 列出的模块是必须落位项，不禁止实现辅助模块；store 统一封装 4.2.2 提交入口，避免各层重复实现提交协议 | 实现级 |
| D29 | 图像裁剪支持 pymupdf → pdfplumber → PIL 占位图三级降级，并在报告中标注 | 无渲染器时仍产出合法 Figure 记录，避免 figure 块断链；占位图带图注文本，报告可审计 | 实现级 |
| D30 | 生成阶段合并逻辑实现为 `src/generate/merge.py`（10.1 未列出但 5.7 要求） | 5.7 合并策略为红线需求，需要独立模块与测试 | 实现级 |
| D31 | Marker/Docling/Pix2Text 经 `src/ingest/parser_workers.py` 子进程适配器运行（默认 `.venv-parsers` 独立解释器，`config.yaml parser.python` 可改） | 重型依赖（torch/transformers）与主环境隔离，避免破坏 FastAPI/Pydantic 版本组合；worker 只走 JSON stdio，缺元数据时该页 failed 不猜 scale/rotation | 实现级 |
| D32 | marker 2.x 块 bbox 采用"页面包围盒自标定"换算（scale_x/scale_y + y 轴翻转） | marker 各版本 bbox 空间不统一（0-1 归一化/像素/点），不能硬编码一套公式；自标定参数记入 `coordinate_meta.derivation` 供审计 | 实现级 |
| D33 | embedding 三档 provider：none（默认）/onnx_bge（真语义）/hashed_lexical（明确标注的词法回退） | 语义向量需要 bge ONNX 模型（不随仓库分发）；hashed_lexical 仅用于打通 sqlite-vec 链路并在文档中声明非语义 | v2 |
| D34 | Anki 导出 `.apkg`（genanki）+ CSV 回退；eval 三维度评分与基线回归告警 | 对应 11.4/10.5 交付项；卡片与 deck ID 确定性派生，重复导入即更新 | v2 |
| D35 | 新增配置指南（`GET /api/v1/setup/status` 只读自检 + 前端六步向导，首启未就绪自动弹出）；顺带修复 `upsert_provider` 掩码回写覆盖真密钥 | 首次配置横跨 models.json 注册表/config.yaml/路由/解析器 venv 多处（D21/D31），门槛高易配错；自检复用既有状态（registry/config/parser 探测）不新增写契约，向导只调用既有 PUT models/model-routes/settings 接口，不改变行为契约；掩码值与 `mask_key` 一致即视为未改动、保留原 key（显式新值仍生效），消除 UI 回传掩码把密钥覆盖成 "****xxxx" 的风险 | 实现级 |
| D36 | 调用路径按能力标志降级：模型 `json_schema=false` 时改用 `response_format.type=json_object`（并在提示缺 "json" 时补一条系统指令），不再恒发 json_schema | DeepSeek 等上游对 json_schema 直接回 400「This response_format type is unavailable now」；原实现只在 test_connection 的提示文案里提到能力，调用路径从不检查，导致密钥与路由配好后「生成本章」依旧失败（能力标志形同虚设，与 llm.py 文件头「能力标志驱动行为」自相矛盾）；解析侧 json_repair_loads + schema 校验已容忍非严格 JSON，降级不牺牲健壮性 | 实现级 |

| D37 | 锚点和块点查复用按根目录/文档/修订隔离的 ID 映射，热查只复制目标行 | 避免点击来源时复制整本书；修订变化失效，派生 doc_map 缺失时回退 JSONL；不改提交与人工保护协议 | 已实现 |
| D38 | 全书知识树为有容量上限的派生缓存，按原文/笔记文件变更失效 | 复用现有事实源，导图无需 LLM；显示缺章与解析失败页，不宣称完整知识覆盖 | 已实现 |
| D39 | 模板库版本与每书应用快照分离，生成指纹包含结构指引但不含封面 | 编辑模板库不暗改已应用书籍；AI讨论只产草稿，用户保存和应用；原笔记不改写 | 已实现 |
| D40 | 分享采用显式持久快照、随机固定 ID、修订检查、撤销记录及独立只读服务 | 长期维护同一地址，避免暴露管理令牌；本次按用户要求不部署，不提供假公网地址 | 已实现，未上线 |
| D41 | 外部软件先采用文件交换与独立摘录箱 | 保留 Obsidian/Anki 原能力，新增 MD/MM/OPML 和粘贴摘录；不把文件互通描述为账号同步 | 已实现 |
