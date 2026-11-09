# Lodestar · 星引 🌌⁂

> 喵~ 洛星，这篇 README 由**一只会喵喵叫的狐狐**代笔——出场就这一次，后面全是喵。喵呜~
> 你把教材、讲义、论文丢进来，我负责盯完整条流水线：**解析 → 分章 → 锚定 → 生成笔记 → 复习巩固 → 导出带走**，喵！
> 每一块笔记都能点回原文对应页码，我绝不瞎编，也**永远不会覆盖 star喵你亲手写下的东西**，喵喵喵~

```
   🦊  <( 把讲义交出来喵~ )        😼 star喵负责学，我负责整理
   📄 ──→ 🔍 ──→ 📖 ──→ ✨ ──→ 🧠 ──→ 📤
   导入    解析    锚定    生成    复习    导出
                    喵~ 全程不用你动手
```

- **唯一规范**：[`docs/自动笔记软件设计方案.md`](docs/自动笔记软件设计方案.md)（v1.6，约 2100 行，含数据模型/坐标契约/验收用例）
- **实现决策**：[`docs/adr.md`](docs/adr.md)（D1–D34）
- **技术验证**：[`docs/tech-validation-11.0.md`](docs/tech-validation-11.0.md)（marker / docling / pypdf 实测）
- **品牌手册**：[`docs/brand/README.md`](docs/brand/README.md)（Lodestar · 星引：命名系统、⁂ 符号、CDIV 铭文层）

---

## 2026-11-04 升级

- **全书导图 · 软件联动**：按书籍、章节、类型展开知识点，搜索并跳回笔记/来源；显示缺章及解析失败页。导出 Markdown、FreeMind `.mm`、OPML，新增可去重的文本摘录箱。现有 Obsidian、Anki 导出继续使用；文件交换不等于账号自动同步。
- **模板工作室**：自定义笔记类型、栏目、写作要求、块类型偏好，选择纯色、渐变或上传封面；可和已配置的 AI 多轮讨论后预览、保存，再应用到单本书。模板只影响后续生成，人工笔记不变；封面同时用于阅读和 HTML 分享。
- **分享 → 长期链接**：保存持久阅读版本、更新同一固定路径、预览、下载和撤销。按本次要求暂不上线，界面显示“待部署”；独立只读服务和后续部署步骤见 [长期分享部署](docs/长期分享部署.md)。
- **底层效率**：按修订复用索引，单次来源点查只复制目标条目；导图有界缓存与折叠按需渲染；新面板按需加载。

## 目录

1. [我能做什么喵](#我能做什么喵)
2. [界面一览喵](#界面一览喵)
3. [新手引导（第一次来先看这个喵）](#新手引导第一次来先看这个喵)
4. [快速开始](#快速开始)
5. [使用流程（八步走）](#使用流程八步走)
6. [分享喵（发给别人看）](#分享喵发给别人看)
7. [学科模板与自定义块类型](#学科模板与自定义块类型)
8. [AI 出题与参考题库](#ai-出题与参考题库)
9. [单块 AI 编辑与知识点互链](#单块-ai-编辑与知识点互链)
10. [命令行参考](#命令行参考)
11. [配置参考](#配置参考)
12. [解析器与 OCR](#解析器与-ocr)
13. [显卡与依赖（N 卡 / 核显 / A 卡）](#显卡与依赖n-卡--核显--a-卡)
14. [接入大模型](#接入大模型)
15. [数据与目录布局](#数据与目录布局)
16. [架构概览](#架构概览)
17. [测试与质量](#测试与质量)
18. [常见问题（排错喵）](#常见问题排错喵)
19. [密钥与隐私（不会进仓库喵）](#密钥与隐私不会进仓库喵)
20. [安全说明](#安全说明)
21. [路线图](#路线图)

---

## 我能做什么喵

| 能力 | 说明喵 |
|---|---|
| **PDF / PPTX 导入** | 教材、论文两种 profile；PPTX 讲义直接抽幻灯片（文本+几何+插图）；拖进窗口就行喵；自动分章（目录 → 标题启发式 → 手动覆盖） |
| **讲义追加** | 老师一份份发的讲义，点「追加讲义」接到同一本书尾巴上：全书重解析重锚定，**已有笔记一字不丢**，新页只带来新章节，喵！ |
| **扫描书 OCR** | 纯图片页自动本地 OCR；**认不好的页自动交给视觉模型重识别**（每页才一点点 token，嫌贵可以关） |
| **学科模板** | 数学 · 物理 · 计算机 · 医学 · 化学 · 论文六套预设（定义/定理/定律/算法/症状/反应…），**还能自己定义块类型**喵 |
| **防幻觉来源** | 每块必须带来源锚点 + 原文引文，写入前程序逐字校验引文确实出自原文；AI 综合的内容会老老实实标上 `basis=synthesis` |
| **双向定位** | 笔记→教材：点来源徽标跳页高亮；教材→笔记：翻页/选中文字实时列出关联块，喵~ |
| **单块 AI 编辑** | 每块一个「✨AI 改」：更精简/更详细/更准确/更通俗或自己写指令 → **只给建议不动笔**，你点「应用」才落盘（原内容进历史可回滚） |
| **知识点互链** | 每块一个「🔗关联」：搜别的块一键互链（**跨章也行**），把知识连成网 |
| **AI 出题** | 按笔记出卷子（选择/填空/计算/简答/证明，题量难度随便挑）；**能上传往年题做参考题库**，出的题就贴着你老师那味儿，喵 |
| **人工成果保护** | 人工/agent 改过的块会打上保护位，再生成**永不覆盖**；软删的块有墓碑，AI 复活不了它 |
| **版本历史** | 每次改动一个 revision + 完整快照，一键回滚；历史只追加不删除喵 |
| **多格式导出** | Markdown / LaTeX / Obsidian / Anki 闪卡；整书或单章随你挑；导完**弹窗告诉你文件在哪 + 一键打开文件夹** |
| **全书生成** | 一键整本；**默认跳过已生成的章节**（不重复烧 token），想全量重来也能选，喵~ |
| **多模型接入** | 任何 OpenAI 兼容端点（DeepSeek / MiMo / Qwen / Ollama / vLLM…），按任务路由，带成本上限和用量仪表盘 |
| **可解释失败** | 余额没了、密钥失效这类全局故障会**立刻停下**并说清原因——绝不留一句让人猜的「部分完成」给你 |
| **本地优先** | 服务只绑 127.0.0.1；解析、浏览、编辑全离线可用；JSONL 主数据可以进 git，喵 |
| **可恢复** | 跨文件提交走 intent→COMMIT→DONE 日志，崩了也能幂等前滚；备份恢复带校验和与 Zip Slip 防护 |

---

## 界面一览喵

```
┌──────────────────┬──────────────────────────┬────────────────────┐
│  教材视图（PDF）  │     笔记视图（块流）       │   块详情 / 选区命中  │
│  · PDF.js 渲染    │  · 卡片/源码/预览 三视图   │  · 来源列表（可跳页） │
│  · 高亮 + 脉冲    │  · 点卡片放大阅读 ⤢       │  · 链接 / 历史时间线  │
│  · 选区匹配       │  · ✨AI 改 · 🔗关联        │  · 回滚 / 删除 / 重排 │
└──────────────────┴──────────────────────────┴────────────────────┘
  顶部工具栏：教材/章节选择 · 导入讲义 · 追加讲义 · 改名 · 生成本章 · 全书生成 ·
             导出 · AI 出题 · 待审核(带角标) · 生成记录 · 刷新图资源 ·
             使用引导 · 配置指南 · 设置 · 全局检索(/)
```

快捷键喵：**单击卡片 = 放大阅读**；`J/K` 上下选块，`E` 源码，`S` 切换源码/预览，`/` 全局检索，`Ctrl+S` 存草稿，`Esc` 关弹窗，喵~

---

## 新手引导（第一次来先看这个喵）

**第一次打开**，我会弹一个四步教学向导（看完点「开始使用」，以后不再打扰；随时能点工具栏**「使用引导」**重看，喵）：

1. **认识产品** —— 五步流程 + 两条铁律（笔记带出处、人工不被覆盖）
2. **界面导览** —— 三个区域 + 工具栏按钮 + 快捷键表
3. **三分钟上手** —— 导入 → 生成 → 复习/出题/导出
4. **配置自检** —— 模型/密钥/路由/解析器四项状态，一键进「配置指南」

**配置指南**（工具栏按钮，或首次没接模型时自动弹出）分六步，左边实时自检 13 项：环境 → 接入模型 → 任务路由 → 解析器 → 输出 → 预算，喵！

| 步骤 | 内容 | 必须喵？ |
|---|---|---|
| 1. 环境检查 | Python ≥ 3.11、`data/` 可写、本地安全令牌 | ✅ |
| 2. 接入模型 | base_url + API Key（或环境变量名）+ 模型 id，可一键「测试」连通性 | ✅ |
| 3. 任务路由 | default / outline / fill / agent / link / ocr / quiz 七个槽位 | — |
| 4. 解析器 | 解析器（auto/marker/docling/pypdf）、OCR 模式、视觉补 OCR、解析器 venv | ✅ |
| 5. 输出 | 输出语言（zh/en/ja）与导出编码（utf-8/gbk/big5/utf-8-sig） | ✅ |
| 6. 预算 | 并发、单章成本上限、温度 | — |

- **就绪判定**：四个必须步骤全过就是 ready；`GET /api/v1/setup/status` 给完整自检 JSON，能脚本化巡检喵
- **密钥安全**：界面只回显末 4 位；把掩码原样传回去**不会**覆盖真密钥

---

## 快速开始

### 方式一：双击启动（最省事喵）

工作区根目录有 **`启动星引.bat`**（仓库内副本：[`scripts/启动星引.bat`](scripts/启动星引.bat)）：

```bat
启动星引.bat                :: 平时双击这个（自动装依赖/构建前端/开浏览器）
启动星引.bat --no-browser   :: 只启动服务
启动星引.bat 9000           :: 换端口，喵
```

### 方式二：手动

```bash
# 1) 装依赖（Python >= 3.11）
python -m pip install -r requirements.txt

# 2) 构建前端
cd viewer && pnpm install && pnpm run build && cd ..

# 3) 启动
python cli.py serve            # → http://127.0.0.1:8787
```

### 方式三：纯命令行

```bash
python cli.py run data/textbooks/我的教材.pdf --doc-id my-book --title "我的教材"
# 一键串联：ingest → anchor → generate → link → reindex → export
```

> 喵~ 提醒 star喵：`generate` 需要先配模型（见[接入大模型](#接入大模型)）；只做解析/锚点/导出不用模型也能跑。

---

## 使用流程（七步走）

| 步骤 | 操作 | 说明喵 |
|---|---|---|
| 1. 导入 | UI「导入讲义」/ 拖拽 PDF·PPTX / `cli.py ingest` | 自动分章；扫描件自动 OCR（还有视觉补识别兜着） |
| 2. 追加 | UI「追加讲义」（选中教材后出现） | 新讲义接到本书尾巴；已有笔记与生成记录原样保留 |
| 3. 生成 | UI「生成本章」或「全书生成」 | 提纲 → 逐块填充，带来源校验与缓存；全书生成默认跳过已生成的章 |
| 4. 修改 | 卡片 / 源码 / 预览；「✨AI 改」；「🔗关联」 | 保存即出新版本；人工块受保护，AI 只递建议 |
| 5. 复习 | 「待审核」（带未审数量角标） | 确认 AI 块 / 处理失败块；失败块能被「继续生成」零成本补全 |
| 6. 自测 | 「AI 出题」 | 选题型/题量/难度，先传往年题还能更贴考点 |
| 7. 导出 | 「导出」→ 选格式与范围 | md / tex / Obsidian / Anki；完了弹窗给文件位置，喵 |
| 8. 分享 | 「分享」→ 选当前章节或整本 | 复制 Markdown / 下载 .md / 下载单文件 HTML / 复制纯文本；全部本地生成，喵 |

---

## 分享喵（发给别人看）

点工具栏「**分享**」，选范围（当前章节 / 整本），四种产物随便挑：

| 产物 | 适合 | 说明 |
|---|---|---|
| **复制 Markdown** | 贴进 Notion / Obsidian / Typora / 飞书 | 公式保持 LaTeX，粘过去就渲染 |
| **下载 .md** | 归档、二次加工、投稿 | 与「导出 Markdown」同一套渲染（同一事实源） |
| **下载单文件 HTML** | 发给别人 / 存档 / 打印 | 公式字体（KaTeX 20 个 woff2）与插图全部内嵌进一个文件，对方断网双击也能看 |
| **复制纯文本** | 粘进微信 / QQ 聊天 | 公式退化成 TeX 源码，最省事 |

几个设计取舍喵：

- **不经过任何服务器**：分享文件在浏览器里现场生成（KaTeX 渲染 + 字体转 base64 + 插图转 data URL），没有任何上传动作
- **出处保留**：每个知识点下面带「出处 p.211」这种页码，别人能对照原文核对
- **HTML 体积**：字体约 250 KB + 插图按张算 + 正文，单章通常 300-600 KB（换来的是断网也能看公式）
- 想更小就选「复制 Markdown」——纯文本，几十 KB

### `.ngshare` 分享包：对方装了 Lodestar 就能「像读自己的笔记一样」看喵

这是专门给**也装了本软件的人**的格式（纯 JSON，图片内嵌，单章约 200 KB）：

1. 你这边：分享 → 选范围 → **导出 .ngshare** → 把文件发给对方（微信/邮件/U 盘都行）
2. 对方那边：分享 → **收件箱** → 导入 .ngshare → 点条目即可阅读（公式渲染、出处页码、插图齐全）
3. 同一个包重复导入**不会堆副本**（按内容哈希去重）；收件箱里的「删除」只删本机副本，不影响发送方

和 HTML 的区别：HTML 谁都能看（只要浏览器）；`.ngshare` 只有在 Lodestar 里才有「导入 → 收件箱 → 结构化阅读」这套体验。

> 安全：导入包按白名单校验（格式/版本/块大小/插图必须是图片 data URL），坏包会直接告出哪一项不对；收件箱存放在 `data/shares/<id>/share.json`，只读收录、不碰任何现有教材。
---

## 学科模板与自定义块类型

洛星，课本可不都按「定义 → 定理 → 证明」组织，所以模板准备了六套，喵~

| 模板 | 块类型 |
|---|---|
| 数学 / 教材 | 定义 · 定理 · 动机 · 图示 · 延伸链接 |
| 论文 | 上面五类 + 贡献 · 方法 · 实验 |
| 物理 | 定律 · 现象 · 实验（+ 通用五类） |
| 计算机 | 算法 · 数据结构 · 复杂度（+ 通用五类） |
| 医学 | 症状 · 诊断 · 治疗（+ 通用五类） |
| 化学 | 物质 · 反应 · 性质（+ 通用五类） |

每个类型都带**专属写作指引**，会直接进生成提示词——医学的「剂量单位不得改动」、计算机的「算法：伪代码 + 复杂度」，才不拿"定义/定理"的模子硬套所有学科呢，喵！

**自定义块类型**（设置 → 笔记模板 → 自定义块类型 → 「+ 添加类型」），star喵可以自己造类型：

| 字段 | 作用喵 |
|---|---|
| id | 英文标识（`case`、`lemma`…；同 id 可**覆盖预设**，比如只改"定理"的写作要求） |
| 名称 / 图标 | 界面显示（卡片图标会自动跟上） |
| 写作指引 | 进生成提示词的写作要求（例如「病例：主诉/现病史/处置」） |
| 来源策略 | 需来源 / 来源可选 / 无来源——校验器会**强制**执行（选了"需来源"，就绝不敢无据生造，喵） |

配置写在 `config.yaml → generate.block_types`；全链路同一个事实源：生成 schema 枚举、提示词指引、校验器、前端图标，喵喵。

> 导入教材时用当前模板（`generate.profile`）；不同学科的书，建议先在设置里切好模板再导入喵~

---

## AI 出题与参考题库

工具栏「**AI 出题**」——期末考试前，star喵喊一声就行，卷子立刻有，喵！

| 选项 | 内容 |
|---|---|
| 范围 | 仅当前章节 / 整本综合 |
| 题型（多选） | 选择题 · 填空题 · 计算题 · 简答题 · 证明题 |
| 题量 | 5 / 10 / 15 / 20 道 |
| 难度 | 基础 / 期末 / 考研 |

**参考题库**：把往年题、课后习题（PDF/txt/md）丢上来 → 自动按段落进 `data/<doc_id>/qbank.jsonl`（重复的自动去重）。出题时会取样当**风格与考点样本**，并明令禁止照抄原题里的数字和文字。传两套往年卷，出的题就有那味儿了，喵~

两条纪律喵：

1. **只考笔记范围**——答案必须能从你的笔记推出来，绝不考没学过的内容
2. **结构化校验**——选择题必须带选项与唯一答案、还有解析字段；格式不合法的一律不落盘

题目存在 `data/<doc_id>/quiz/<quiz_id>.json`；弹窗里逐题「显示答案」+ 解析，不满意还能「再出一套」。出题模型走 `generate.models.quiz`（留空就用 fill/default，喵）。

---

## 单块 AI 编辑与知识点互链

**✨AI 改**（卡片右上角 / 放大层标题栏都有）：

1. 点个快捷指令（更精简 / 更详细 / 更准确 / 更通俗），或者自己写指令
2. 我生成一份**修改建议**——**不落盘**，你觉得行不行可以「对比原文」，建议本身还能再手改
3. 点「**应用修改**」才写入：这是**你亲手拍板**的动作（`author=human`），原内容进历史，随时回滚，喵

> 规矩（规范 5.2，喵呜~）：**机器不得覆盖人工确认的内容**。所以 AI 只递建议、你来拍板；`manually_edited` 的块对 agent 只读，想让它动得先解锁。

**🔗关联**：搜索任意块（**跨章也行喵**）一键互链，已链的显示成 chip，点一下能跳、再点能删；卡片上会显示 `🔗N` 计数。Obsidian 导出时会渲染成 `[[双链]]`。

---

## 命令行参考

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `ingest <pdf>` | 解析 PDF | `--doc-id` `--title` `--profile textbook\|paper` `--parser auto\|marker\|docling\|pypdf` `--ocr` |
| `anchor <doc_id>` | 生成锚点索引 | `--force` |
| `generate <doc_id>` | 生成笔记块 | `--chapter` `--model` `--language zh\|en\|ja` `--concurrency` `--force` `--no-cache` `--merge/--replace --yes` |
| `link <doc_id>` | 反向索引 + 块间链接 | `--chapter` |
| `export <doc_id>` | 导出 | `--chapter` `--format md\|tex\|both\|md-obsidian\|anki` `--out` `--encoding utf-8\|utf-8-sig\|gbk\|big5` `--language` |
| `run <pdf>` | 一键全链路 | 以上参数透传 |
| `reindex <doc_id>` | 重建 SQLite 索引 | `--embed`（先算向量） |
| `report <doc_id>` | 汇总各阶段报告 | 含死链/保护跳过清单 |
| `eval <doc_id>` | 生成质量评测 | `--update-baseline`（固化基线） |
| `backup <doc_id>` | 备份（含校验和） | `--out` |
| `delete <doc_id>` | 删除教材（进回收站，别慌，能恢复喵） | `--yes`（必填） |
| `restore <zip>` | 恢复（防 Zip Slip） | `--as-new-doc` |
| `serve` | 启动 Web 服务 | `--host` `--port` |

所有命令都支持 `--dry-run`；退出码：**0** 成功 / **1** 参数错误 / **2** 运行错误，喵。

---

## 配置参考（`config.yaml`）

```yaml
app:
  host: 127.0.0.1          # 只绑本地（NFR-1）
  port: 8787
  cors_origins: ["http://127.0.0.1:5173"]
parser:
  default: auto            # auto（按需分流）| marker | docling | pypdf
  ocr_mode: auto           # auto（仅无文字层页）| force（全页）| off
  ocr_vision: auto         # 视觉补 OCR：弱识别页交视觉模型重识别（auto=有视觉模型就启用）| off
  python: null             # 解析器 venv 解释器（见下文）
chapters_override: []      # 手动章区间覆盖（最高优先级）
generate:
  profile: textbook        # 笔记模板：textbook/paper/physics/cs/medicine/chemistry（或自定义名）
  block_types: []          # 自定义块类型：[{id,name,icon,hint,source_policy,profiles}]
  models:                  # 任务 → 模型路由（provider/model 或唯一短名）
    default: "deepseek/deepseek-flash"
    outline: null          # 提纲：可以指个便宜的模型
    fill: null             # 填充：调用量最大的一环
    agent: null            # 必须带 tools 能力
    link: null
    ocr: null              # 视觉补 OCR（需 vision 能力；空则自动挑 fill/default 里有视觉的）
    quiz: null             # AI 出题（空则用 fill/default）
  output_language: zh      # zh | en | ja
  outline_max_in: 32000
  fill_max_in: 5000        # 单次填充输入预算（超了先裁来源原文）
  fill_batch: 4            # 小块合批：N 块一次调用（失败自动回退逐块）
  outline_batch_out: 6000  # 提纲分批目标（批越小首批越快）
  chapter_concurrency: 3   # 整本生成并行章数（写入仍串行，不会打架喵）
  max_concurrent_requests: 10  # 全局在途请求上限（防触发供应商限流）
  reasoning_effort: low    # 推理深度（low 显著提速）
  thinking: disabled       # 思考开关（实测 disabled 快 5 倍）
  max_cost_per_chapter: 0.0   # 美元，0=不限
  temperature: 0.2
  quiz_max_out: 8000       # 出题单次输出上限
export:
  encoding: utf-8          # utf-8 | utf-8-sig | gbk | big5（tex 恒为 UTF-8）
embedding:
  provider: none           # none | onnx_bge | hashed_lexical
cache: { dir: "cache/gen" }
match: { contain_ratio: 0.8, cover_ratio: 0.5, top_k: 8 }
history: { keep_recent: 50 }
```

> 懒得开文件也行：**设置** 里就能改（模板/自定义类型/解析器/OCR/语言/编码/路由），保存即原子写回，喵~

## 解析器与 OCR

star喵，解析器这块我会自己挑：**有文字层的数字版 PDF 走 pypdf 秒级路径**，扫描书才动用重型解析器，喵~

| 解析器 | 特点 | 实测（本机，见 11.0 报告） |
|---|---|---|
| **pypdf** | 内置、极快、零依赖（数字版首选 / 兜底） | 5/5 通过，百页秒级 |
| **marker** | 结构/公式/旋转页质量最好 | 5/5 样本通过，坐标合法率 100% |
| **docling** | 复杂双栏/论文结构强 | 4/5（旋转页坐标异常时按规范拒绝该页） |
| **OCR rapidocr** | 纯图片/扫描页本地识别 | 2 页扫描件 6 秒；百页书约 3-8 秒/页 |
| **视觉补 OCR** | 本地认不好的页（乱码/公式堆）交给视觉模型 | 实测真实扫描页 3.9 秒 → 15 行带定位转录，喵 |

**兜底链**：`marker → docling → pymupdf → pypdf` 一级级降；**「成功但零文本」不算成功**（继续试下一个），全链都没文字层就交给 OCR —— 纯图片书就是走这条门进来的，喵！

**零文本闸**：解析完要是整本 0 块，任务会**直接报错**并给建议（查 `.venv-parsers`/OCR 或换解析器），绝不静默产出一本空书糊弄你。

重型解析器装在**独立虚拟环境**里（不污染主环境，喵）：

```bash
python -m venv .venv-parsers
.venv-parsers/Scripts/python -m pip install marker-pdf docling rapidocr onnxruntime pypdfium2
# config.yaml → parser.python: ".venv-parsers/Scripts/python.exe"
python scripts/tech_validation_11.py      # 生成实测报告
```

**已知限制喵**

- 扫描书 OCR 慢（294 页约 25 分钟）——导入任务跑着的时候千万别关软件
- 视觉补 OCR 需要**有余额的视觉模型**；每页约 1-2k token，心疼可以 `parser.ocr_vision: off` 关掉
- Pix2Text 公式 OCR 依赖 `rapidocr<2`（仅 Python ≤ 3.12），3.13 下自动跳过并告警
- docling 默认后端在**含中文的安装路径**下会加载资源失败；已自动回退 pypdfium2 后端

---

## 显卡与依赖（N 卡 / 核显 / A 卡）

先说结论喵：**生成笔记走的是云端大模型 API，GPU 帮不上忙**——本机显卡只影响**解析阶段**（marker / docling 的版面模型，以及可选的 OCR 加速）。

### 本机实测环境（N 卡 CUDA，可以直接抄喵）

```
.venv-parsers
├── torch 2.14.1+cu130 · torchvision 0.29.1+cu130      # CUDA 13.0
├── onnxruntime 1.31.0（CPU 后端） · rapidocr 2.0.6     # 扫描页 OCR
├── pymupdf 1.28.2 · docling 2.137.0 · marker-pdf 2.0.0
├── pillow 10.4.0（marker/surya 要求 <11，千万别升！）
└── pypdfium2 5.10.1
torch.version.cuda = 13.0 · cuda_available = True
device = NVIDIA GeForce RTX 5060 Laptop GPU
```

N 卡安装：

```bash
python -m venv .venv-parsers
.venv-parsers/Scripts/python -m pip install marker-pdf docling rapidocr onnxruntime pypdfium2
# GPU 版 torch：走官方 cu130 索引（cu128 最高只到 2.11.0，新卡必须 cu130）
.venv-parsers/Scripts/python -m pip install --proxy '' \
  --index-url https://download.pytorch.org/whl/cu130 torch==2.14.1+cu130 torchvision==0.29.1+cu130
.venv-parsers/Scripts/python -m pip install "pillow==10.4.0"   # 装完 torch 把它钉回来
```

两个坑（本机真踩过喵）：`--proxy ''` 用来绕开失效的 pip 代理（本机曾配过 `127.0.0.1:7890`，早就没在听了）；`pillow` 必须留在 10.x——装 GPU 版 torch 时很容易被顶到 11，然后 marker/surya 就报错。

### 核显 / 没有独显（CPU 方案，功能齐全喵）

全部功能都能用，只是解析慢：

```bash
.venv-parsers/Scripts/python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu torch torchvision
```

- **数字版 PDF**：保持 `parser.default: auto` → 走 pypdf，百页秒级，**完全不碰显卡**
- **扫描书**：OCR 本来就在 CPU 跑（onnxruntime），约 3-8 秒/页，能接受
- **重排版书**：marker/docling 在 CPU 上很慢，建议只对真正必需的书启用

### A 卡（AMD）/ Intel 核显

Windows 上 **ROCm 基本不可用**（官方只支持 Linux），所以现实路线是这三条：

| 路线 | 怎么做喵 | 适用 |
|---|---|---|
| **OCR 吃 GPU** | `pip uninstall -y onnxruntime` → `pip install onnxruntime-directml`（DirectML 后端在 Windows 上支持 AMD / Intel / NVIDIA 显卡，rapidocr 检测到就会用） | 扫描书 OCR 提速最省事 |
| **重排版解析** | Windows 下 marker/docling 老实走 CPU；真想用 ROCm 就把**整套部署到 Linux**（本项目 worker 按 Windows 解释器路径调用，不能只把解析 venv 塞进 WSL 里） | 有大量重解析需求 |
| **云端/换机** | 只在有 N 卡或云 GPU 的机器上跑解析，把 `anchors/blocks` 数据带回来继续用 | 临时加速 |

> Intel Arc / 核显：`onnxruntime-directml` 同样可用；torch XPU（intel-extension-for-pytorch）兼容性没验证过，不建议折腾喵。

### 一条命令看清现在用的是哪种

```bash
python scripts/env_report.py
```

会列出：Python 与依赖版本、解析器 venv 是否就绪、CUDA 可用性与设备名、ONNX 执行后端，并顺手做一遍密钥安全检查（见下一节喵）。

---

## 接入大模型

去 **设置 → 供应商与模型** 添加（或手改 `data/models.json`）：

```json
{
  "providers": [
    {
      "id": "deepseek", "label": "DeepSeek", "protocol": "openai_compat",
      "base_url": "https://api.deepseek.com/v1",
      "api_key_env": "DEEPSEEK_API_KEY",
      "capabilities": { "json_schema": true, "tools": true, "vision": false },
      "models": [
        { "id": "deepseek-chat", "context_window": 65536, "max_output_tokens": 8192,
          "capabilities": { "json_schema": true, "tools": true, "vision": true },
          "pricing": { "input_per_million": 0.27, "output_per_million": 1.1, "currency": "USD" } }
      ]
    }
  ]
}
```

- **本地模型**：Ollama（`base_url: http://127.0.0.1:11434/v1`）、vLLM 同理，喵
- **能力标志驱动行为**：`json_schema` 决定结构化输出；`tools` 是 agent 编辑的前提；`vision` 决定能不能做视觉补 OCR
- **密钥**：`api_key_env`（环境变量，推荐）或 `api_key`（本地明文，接口只回显末 4 位）
- **成本控制**：`max_cost_per_chapter` 在每次调用前原子预留；价格未知的模型在启用预算时会被拒（不假装免费喵）
- **并发自适应**：供应商按余额档位给并发上限（打满就 429）；客户端会自己降档 + 退避重试，**并发类 429 不会被误判成余额耗尽**
- **缓存联动**：换模型 / 换提示词后缓存键自动失效，既不重复收费也不拿旧结果糊弄洛星

---

## 数据与目录布局

```
lodestar/
├── data/
│   ├── runtime.db                 # 不可重建：任务/用量/agent 会话
│   ├── models.json                # 模型注册表（含密钥，勿入库喵）
│   ├── textbooks/{doc_id}.pdf|pptx # 原文件
│   └── {doc_id}/                  # ← 文档事实源
│       ├── manifest.json          # 页面几何/章节/阶段指纹/data_revision
│       ├── anchors/{ch}.jsonl     # 锚点（段落/公式/图/表 + bbox + 哈希）
│       ├── blocks/{ch}.jsonl      # 笔记块（唯一编辑对象）
│       ├── history/{block_id}.jsonl  # 版本快照
│       ├── qbank.jsonl            # 参考题库（AI 出题用）
│       ├── quiz/{quiz_id}.json    # 出过的卷子
│       ├── figures/  refs/  index.db
├── ingest_out/  cache/  reports/  notes_out/   # 都能重建
└── viewer/                        # React + PDF.js 前端
```

**不变量喵**：JSONL 是唯一事实源，SQLite / 导出 / 缓存随时能重建；`cli.py reindex` 修索引层，`cli.py link` 修反向索引层。

---

## 架构概览

```
表现层   Viewer（React + PDF.js，双向对照）
应用层   FastAPI（块 CRUD/乐观锁 · 对照检索 · 生成触发 · AI 编辑/关联/出题 · 导出 · agent 两步编辑）
管线层   ingest → anchor → generate → link → export（每阶段可独立重跑、幂等）
数据层   JSONL 主存 + SQLite 派生索引 + 文件系统（图/原文件）
```

关键设计（细节看规范文档，喵）：

- **可恢复提交**：跨文件变更走 intent → COMMIT → DONE 日志，崩了也能幂等前滚
- **坐标契约**：存未旋转的原始 PDF 用户空间，前端只用 PDF.js viewport 正/逆变换（旋转/CropBox/UserUnit 不会算两次）
- **再生成合并**：身份键匹配 + 保护位 + 墓碑抑制，人工成果永不丢
- **锚点身份 = 内容哈希**：解析/分章重建后锚点身份不变，生成缓存与报告继续有效，喵

---

## 测试与质量

```bash
python -m pytest -q                       # 340+ 项单元/集成测试（1 项按环境跳过）
python -m ruff check --select F,E9 src tests   # 静态检查
python scripts/bench_ops.py               # 底层操作基准（真实教材的临时副本，不动原数据）
python scripts/edge_case_probe.py         # 边界探测（异常输入/错误码）
python scripts/edge_case_probe2.py        # 边界探测（agent/备份/任务/约束）
python scripts/tech_validation_11.py      # 解析器技术验证
python cli.py eval <doc_id>               # 生成质量评测（三维度 + 基线回归告警）
```

评测三维度：**faithfulness**（忠实度）/ **formula**（公式正确率）/ **coverage**（锚点覆盖率）；任一维度掉到基线以下 5 个百分点就告警（防"改 prompt 改坏质量"，喵）。

### 底层性能（线性代数 364 页 / 1858 块实测喵）

| 操作 | 优化前 | 优化后 |
|---|---|---|
| 单块编辑一次完整提交 | 634 ms | **~336 ms** |
| reindex（只重刷变更章） | 92 ms（全书） | **~40 ms** |
| reindex（全书全量） | 310 ms | ~250 ms |

三个热点：① 块编辑只重刷**本章**索引与 `doc_map`（不再 DELETE+INSERT 全书 1858 行）；② schema 校验只跑**本次真改的块**（不再整章 452 块全跑 pydantic）；③ 去掉 `os.replace` 之后多余的目标文件 `fsync`（数据已在临时文件句柄上刷过盘，落盘顺序仍是 写临时+fsync → rename → fsync 目录）。三者都不改变可恢复提交协议与 I2/I3 校验的覆盖面。

> 我的规矩，star喵记一下喵：**每个 bug 修复都要带回归测试**，而且测试要能变红（先证明它真能抓到问题）。涉及 worker 子进程的行为，必须有 worker 级测试——只靠 monkeypatch 的单测会漏掉真问题，喵呜。

---

## 常见问题（排错喵）

| 现象 | 原因与处理 |
|---|---|
| 任务显示「**部分完成 / 失败**」 | 任务卡和章节报告里都写了原因喵。**余额不足（HTTP 402）/ 密钥失效**属全局故障：我会立刻中止余下章节，并提示「已完成 N 章，充值后点继续生成会自动跳过已完成章节」 |
| 生成点了没反应 / 说没有锚点 | 这本书可能压根没解析出内容：翻任务报告。整本 **0 块**时解析会直接报错（不会静默出空书）；纯图片书请确认 OCR 可用喵 |
| 章节列表混进小节（如「1.4策略」当了一章） | 电子书**扁平书签** + 标题粘目录页码闹的。已修复（只认「第X章」样式、剥页码、剔控制字符）。重新导入或在任务里「重建导入」即可，喵~ |
| 某章笔记「后面就没了」 | 章块列表接口是分页的（默认 100），界面以前只加载首页——**已修复为自动翻页取全**；刷新页面就能看到全部卡片，喵！ |
| 来源行显示 `p.?` | 详情面板以前没预取锚点（卡片/放大层有）——已修复；刷新页面就好 |
| 待审核里一堆「生成失败」 | 那是**当时的**失败快照（行内已经标了记录时间）。它不是终点：这些块没被保护，点「继续生成」会用缓存补全这一块（其他块零成本，喵） |
| 扫描件导入后是空的 | 看看 设置 → OCR 模式 是不是 `off`；纯图片书依赖 `.venv-parsers` 里的 rapidocr。整本零文本现在会明确报错，不会装没事 |
| 导入卡在「解析/OCR」很久 | 扫描书 OCR 约 3-8 秒/页（294 页 ≈ 25 分钟），进度条会显示页数；这段时间**别关软件**喵 |
| 导入跑一半被我关掉了 | 任务持久化在 `runtime.db`：重开软件后在任务托盘「续跑」就行，喵~ |
| 提示"文档已存在" | 同一个文件重复导入会被拒绝，为了保护已有笔记；确认了再加 `overwrite=true` 重建 |
| 误删了教材 | 删除走回收站：`data/.trash/<doc_id>-<时间戳>/`，把目录移回 `data/` 就回来了，别慌喵 |
| 配置指南没弹出来 | 点工具栏「配置指南」；自动弹只在首次（没接模型）触发，关闭状态记在 localStorage；新人向导的标记是 `lodestar-onboarded`，喵 |
| 生成提示"未配置生成模型" | 去 设置 → 任务路由 指一个，或改 `config.yaml generate.models.default` |
| agent 按钮是灰的 | 它需要带 `tools` 能力的模型喵 |
| 导出的 .tex 编译不了 | tex 恒为 UTF-8；请用 `xelatex`，并保证 `ctex` 宏包可用 |
| 两个窗口同时改同一块 | 后手会收到 409（乐观锁），刷新重试就好 |
| 端口被占用 | `启动星引.bat 9000` 换个端口喵 |
| 重启电脑后打不开页面 | 已提供 `scripts/install_autostart.ps1`：跑一次即可开机自启（装进 `shell:startup`，`-Remove` 卸载、`-DryRun` 预览）。它经 `scripts/autostart_launch.ps1` 启动：**隐藏窗口 + 3 次重试**，日志在 `data/logs/autostart.log`（服务输出 `serve.out.log` / `serve.err.log`）。不想自启就手动双击 `启动星引.bat` |
| 「点来源跳转」变慢 | 大概率是锚点→文档映射被清空（历史 bug，已修）：跑 `python cli.py reindex <doc_id>` 恢复；`python scripts/integrity_report.py` 会报「⚠️ 锚点映射缺失」 |
| 删掉任务记录后导出文件还在吗 | **在**。任务托盘的「删除」只删记录；导出文件始终留在 `notes_out/<doc>/`（历史 bug：曾把导出文件一并删掉，已修） |
| 待审核里出现「只有部分来源」的块 | 扫描书的锚点原文是 OCR 文本，模型很难逐字照抄引文。现在重试到末期若仍对不上，会**丢弃对不上的来源、保住整块并转待审核**（绝不用弱匹配伪造引文）；对照原文补一条来源或直接确认即可 |

---

## 密钥与隐私（不会进仓库喵）

star喵放心，**你的 API Key 不在仓库里**：`.gitignore` 第 2 行就是 `data/`，而密钥住在 `data/models.json`、本地访问令牌住在 `data/.local_token`——这两个都被忽略了。

本文档写作时的实测核查结果：

| 检查项 | 结果 |
|---|---|
| `data/models.json`（含密钥） | ✅ 已忽略，且**从未进入任何提交** |
| `data/.local_token`（本地令牌） | ✅ 已忽略，且从未进入任何提交 |
| `config.yaml`（会被提交） | ✅ 不含密钥，只有路由/模板/解析器设置 |
| 仓库里像密钥的字符串 | 仅测试夹具里的**假 key**（`sk-NEW-KEY-...`，用来验证掩码逻辑） |

自己随时验一遍（`python scripts/env_report.py` 里也内置了同一套检查喵）：

```bash
git check-ignore -v data/models.json data/.local_token    # 应回显 .gitignore:2:data/
git log --all --name-only --pretty=format: | Sort-Object -Unique | Select-String "models.json"
#  ↑ 应为空：历史里从未出现过密钥文件
```

**推荐做法喵**：

- 优先用 **`api_key_env`**（环境变量，密钥不落盘）：`data/models.json` 里写 `"api_key_env": "DEEPSEEK_API_KEY"`
- 一定要写明文时，只写进 `data/models.json`（已忽略）；**不要**图省事塞进 `config.yaml`
- 界面只回显末 4 位；把掩码原样回传**不会**覆盖真密钥（这是专门测过的行为）

> **万一真提交过密钥**：先去供应商后台**吊销并换新**——这才是止损动作；然后才谈清理历史（`git filter-repo` 或 BFG）。历史清理不能撤回一个已经泄露的 key，喵呜。

---

## 安全说明

- 服务**只绑 127.0.0.1**；所有 `/api` 请求都要 `X-Local-Token`（页面 `<meta>` 注入 / Vite 代理注入）
- **CSRF 防护**：校验精确 Host/Origin/Fetch-Metadata，拒绝 `Origin:null` 与跨站请求
- **Prompt 注入防护**：PDF 原文一律放 `<source_data>` 数据分隔区，里面的指令无效；服务端再叠一层锚点白名单校验
- **渲染安全**：Markdown 关闭原始 HTML；TeX 导出不接受外部包注入
- **路径安全**：doc_id/chapter_id/figure_id 严格白名单 + resolve 校验；恢复备份拒绝 Zip Slip，喵

---

## 路线图

- [x] MVP：单章解析 → 生成 → 编辑 → 导出 → 双向定位
- [x] v1：UI 导入/任务/检索、CRUD/回滚、安全边界、备份恢复
- [x] v1.5：论文 profile、多模型管理、TeX/Obsidian 导出
- [x] v2 增量：语义检索、Anki 闪卡、评测回归、扫描件 OCR、GPU 解析
- [x] 近期：学科模板与自定义块类型 · AI 出题与参考题库 · 单块 AI 编辑 · 知识点互链 ·
      PPTX 讲义与追加 · 视觉补 OCR · 新人引导 · 全书生成跳过已生成章节 · 可解释失败
- [ ] 下一步喵：增量重解析自动修复、跨书链接、Electron 打包

---

## 开发

```bash
python -m pytest -q            # 后端测试（改完必跑喵）
python -m ruff check --select F,E9 src tests
cd viewer && pnpm dev          # 前端开发（/api 代理到 8787）
cd viewer && pnpm build        # 生产构建 → viewer/dist
```

约定（都是踩过坑换来的，喵）：

- 字段名/枚举/路径是规范的一部分；行为跟文档冲突时**先改文档并登记 ADR**，再改代码
- 后端改动要重启服务（`启动星引.bat --no-browser`）才生效；**纯前端改动构建完刷新页面就行**
- 重启前先确认没有任务在跑（导入/生成中途重启会打断它，喵呜）

---

喵~ 写到这里就完事啦！🦊🐾

> 洛星，这套东西的脾气我摸得差不多了，你也一样：**笔记要能点回原文、人工写的东西谁也别想覆盖**。
> 哪一块看着不顺眼，点开它喊一声「✨AI 改」；考期快到了，把往年题丢进题库再来一句「AI 出题」。
> star喵，剩下的交给我，喵喵喵~ 😼
