# 知识层规划（全貌）

一个东西的完整方案：**联网搜索 → 本地语料 → 结构切分 → 词法 + 向量检索 → 可引用的答案**。
网页搜索的结果与用户上传的文献落在**同一个语料库**、走同一套分块与引用。

---

## 0. 一页概览

| 项 | 内容 |
|---|---|
| 新增工具 | 5 个：`search_web` / `fetch_webpage` / `import_documents` / `search_corpus` / `read_source` |
| 新增存储 | `<repo>/corpus/`（项目级，gitignore）、`<repo>/.opencode_tui/corpus.toml`（两个开关） |
| 检索 | 词法两路（`unicode61` + `trigram`）+ 向量一路（e5-small 384d）→ RRF → 按节聚合 |
| 引用 | `doc#ab12cd34ef §3.2 p.7` / `web#ab12cd34ef`，只认工具返回值 |
| 依赖变化 | 主依赖 +`pypdf`；语义通道另装 `requirements-embed.txt`（fastembed） |
| 网页端 | 从只读改为「查看只读 + 语料管理」，新增 4 条路由与 `#/library` 页 |
| 分期 | S0 联网 → S1 语料 → S2 网页端 → S3 向量 + 评测 → S4 调优 |
| 不做 | OCR、标签/元数据编辑、向量数据库、远程 embedding、多用户 |

一条主线：**先让"查到的东西"能留下来，再让"留下来的东西"能被找到，最后才谈语义召回。**

---

## 1. 已核对的事实与约束（2026-10-23）

| 项 | 读数 | 来源 |
|---|---|---|
| Search / Fetch | `POST /v1/tools/search`、`POST /v1/tools/fetch`，Bearer 鉴权 | 官方文档 |
| 计费 | 5 元 / 1000 次**成功**请求；空结果、失败、超时不计费；`count` 不影响单价 | 官方文档 |
| 限速 | 账号级 100 / 25 QPS | 官方文档 |
| 实测 | 本机 key 打了一次 `count=1`：HTTP 200，`summary` >900 字 | 实测 |
| **DeepSeek Embeddings** | `/v1/embeddings`、`/embeddings` 均 **404** | 实测 |
| 网页端章程 | 全部路由 GET、只绑 `127.0.0.1`、无鉴权 | `core/web/routes.py:18` |
| SQLite | 3.45.1；FTS5 的 `unicode61` 与 `trigram` 均可用 | 实测 |
| 主依赖 | textual / tomli-w / httpx / python-dotenv | `requirements.txt` |
| torch | 主仓一行不能 import（`check_chemistry_gate` 会炸） | `tests/` |
| 本地向量 | fastembed 0.9（onnxruntime + tokenizers，无 torch）**清单里没有** `intfloat/multilingual-e5-small`；改用 `paraphrase-multilingual-MiniLM-L12-v2`（384d，255MB） | fastembed 清单 + 实测 |
| 前缀 | 默认模型**不需要**前缀；E5 系列要 `passage: ` / `query: `，漏了不报错只掉点（`embed._prefixes` 按模型名自动加） | 模型文档 |
| 目录定位 | `runs_dir()` 按**代码位置**定位到仓根，不按启动目录 | `domain/export.py:717` |

前两条决定了"必须自己维护语料"，第四条决定了"向量只能用本地模型"。

---

## 2. 目标与非目标

**目标**

1. 模型能联网查实时信息，**搜过的网页立刻成为本地语料**——下次免费、离线可查。
2. 用户能把 PDF 放进项目语料库；网页端可上传、改名、删除。
3. 分块按**文档结构**切，引用能落到 `§3.2 p.7`。
4. 一个入口同时覆盖论文与网页，词法 + 向量混合排序。
5. 中文提问能命中英文文献——这是词法唯一无解、必须靠向量的地方。

**非目标**

- 文献管理器：标签、元数据编辑、OCR、PDF 在线阅读器、批量移动。
- 向量数据库（Chroma / FAISS / Milvus）：MB 级语料用暴力余弦就够。
- 远程 embedding：文献正文不外发。
- 多用户、远程访问、云同步。

---

## 3. 总体结构

```
入口层    TUI（工具调用）                web（#/library：列表 / 上传 / 改名 / 删除）
            │                                  │ 只落原始文件到 corpus/inbox/
工具层    search_web  fetch_webpage      import_documents  search_corpus  read_source
            │      └─ 结果 upsert 进语料 ────────┘
            │
存储层    corpus/doc/<id>/   corpus/web/<id>/   corpus/inbox/   corpus/.trash/
            │
解析层    PDF：outline → 标题正则 → 按页        Markdown：标题切
            │
索引层    chunks（正文 + 章节 + 页码）
          ├── FTS5 unicode61（英文词、标识符）
          ├── FTS5 trigram（中文）
          └── vectors（e5-small 384d，本地 ONNX）
            │
排序层    BM25 × 2 + 余弦 → RRF → 邻块补全 → 按节聚合 → top-k
```

三条贯穿全篇的纪律：

1. **真相在原件，索引是派生物。** 索引删掉能重建，原件不可变。
2. **一个 chunk 一个身份。** 分块在入库时定死，检索只挑不切——否则同一句话会在三处得到三种页码。
3. **写入者只有一个。** 语料与索引由 TUI / agent 写；web 只往 `inbox/` 落文件，从不打开数据库。

---

## 4. 数据模型

### 4.1 目录布局

```
<repo>/corpus/                          # .gitignore
  inbox/                                # web 唯一写入口
  doc/<sha10>/                          # 文献：ID = 文件内容哈希前 10 位
    source.pdf                          # 原件，不可变
    meta.json                           # title/pages/structure/sha256/origin/added_at/warning
    labels.json                         # 只有 web 写：display_title
    text.jsonl                          # 每行一页 {"page":7,"text":"…"}
  web/<sha10(url)>/                     # 网页：ID = 规范化 URL 的哈希前 10 位
    content.md                          # full = 抓取正文；summary = 标题 + 摘要 + URL
    meta.json                           # url/title/site/date/authority/depth/fetched_at/query
    labels.json
  .index/library.db                     # 派生：sources / chunks / FTS / vectors
  .trash/<kind>-<id>-<时间戳>/           # 删除先挪进来，7 天后由入库顺手清理
```

- **项目级、按代码位置定位**（与 `runs/` 同一种做法），不按启动目录。
- **内容寻址**：同一份 PDF 传两次是 no-op；同一 URL 抓两次也只有一份。
- **URL 规范化**（决定 web ID）：小写 scheme+host、去 fragment、保留 query、去默认端口。

### 4.2 ID 与引用

```
doc#ab12cd34ef §3.2 p.7     有结构、有页码
doc#ab12cd34ef p.7          按页切的文档，没有章节
web#ab12cd34ef              网页，没有页
```

- 10 位十六进制（10⁴ 份文档量级下碰撞 <0.005%），跨会话稳定。
- 引用号**只能来自工具返回**；`read_source` 只接受这个格式，**不接受路径**，
  正则 `^(doc|web)#[0-9a-f]{10}$`。
- 页码来自分块时的真实页码，不是渲染时猜的。

### 4.3 索引

```sql
CREATE TABLE sources (
  id         TEXT PRIMARY KEY,      -- 'doc:ab12cd34ef' / 'web:ab12cd34ef'
  kind       TEXT NOT NULL,         -- doc | web
  title      TEXT NOT NULL,
  locator    TEXT NOT NULL,         -- 文件路径 / URL
  added_at   REAL NOT NULL,
  fetched_at REAL,
  depth      TEXT,                  -- doc: full；web: summary | full
  pages      INTEGER,
  structure  TEXT,                  -- outline | heading | page | markdown
  meta       TEXT NOT NULL          -- JSON：authors/year/doi/site/authority/query/error
);

CREATE TABLE chunks (
  id        INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  section   TEXT NOT NULL DEFAULT '',  -- "3.2 In vitro assay"
  level     INTEGER NOT NULL DEFAULT 0,
  page      INTEGER NOT NULL,          -- 起始页；web 恒为 0
  page_end  INTEGER NOT NULL,
  chunk_no  INTEGER NOT NULL,
  text      TEXT NOT NULL,
  chars     INTEGER NOT NULL
);

-- 两个词法索引、一份正文（external content，不复制文本）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  section, text, content='chunks', content_rowid='id', tokenize='unicode61');
CREATE VIRTUAL TABLE chunks_cjk USING fts5(
  section, text, content='chunks', content_rowid='id', tokenize='trigram');

CREATE TABLE vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  model    TEXT NOT NULL,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL            -- float32 小端，已 L2 归一化
);
```

- 两张 FTS 表用触发器与 `chunks` 同步；整批入库时也可直接 `rebuild`。
- **章节也进索引**：查 "Methods"、"3.2" 能命中章节标题，而不是只命中正文。
- `vectors.model` 与 `dim` 一起存：换模型必须整表重建，不能混算。
- 标题/作者/年份不进索引（可变、不是检索目标），渲染时现读。

### 4.4 两层存储：查询归档 vs 语料

| 存什么 | 在哪 | 生命周期 | 用途 |
|---|---|---|---|
| **查询 → 结果**（原始响应） | `~/.opencode_tui/search/web-*.json` | **不过期** | 同一 query 不重复计费；旧结果的档案 |
| **URL → 页面**（正文/摘要） | `<repo>/corpus/web/<id>/` | 直到用户删除 | 参与检索、可引用、离线可用 |

查询归档回答"这句话我搜过、当时返回了什么"；语料回答"这个页面有什么、能不能引用"。
混成一个，引用号就会随清理失效。

---

## 5. 工作流 A：联网搜索

### 5.1 契约

| 工具 | 参数 | 默认 / 夹取 | 超时 |
|---|---|---|---|
| `search_web` | `query`（必填，1–128 字符）、`count`、`refresh` | count 5，夹 1–10；refresh=false | 30s |
| `fetch_webpage` | `url`、`max_chars`、`refresh` | 3000，夹 500–8000 | 60s |

- 请求恒用 `count=10`（`fetch` 存 20000 字），**本地按参数切片**；`need_content` 恒 false。
- 只解 `title / url / site_name / date / authority / summary`；`site_icon` 丢弃，
  `rerank_score` 只进 `meta`；摘要渲染截 400 字。
- 空结果 → `reason: "checked"`，**不是 failed**（沿用 `rsearch._empty_meta` 的分档）。

### 5.2 入库规则

- `search_web` 成功后，10 条结果**逐条 upsert 成 `web` 源**（`depth=summary`），
  立即分块、写 FTS、算向量，并在返回里给出引用号。**它不再是纯查询工具**，
  这一点要写进描述，否则模型会以为"搜一下没有副作用"。
- `fetch_webpage` 成功后 upsert 同一 URL（`depth=full`），**替换**摘要块，保留 `labels.json`。
  已经是 full 且没给 `refresh` 时不再请求，直接读本地。
- 同一 URL 的反复搜索只更新 `fetched_at` 与来源 query，不新增源。

### 5.3 凭据与错误

```
1. VSA_SEARCH_API_KEY（可配 VSA_SEARCH_BASE_URL，默认 https://api.deepseek.com）
2. connection.toml —— 仅当 host 恰好是 api.deepseek.com
3. 都没有 → 不联网，返回三条可执行路径
```

每次调用现读，不缓存凭据；**错误信息里绝不回显 key**。

| 码 | 处理 | 要点 |
|---|---|---|
| 401 | 不重试 | 未配置或无效，给三条路径 |
| 402 | 不重试 | 余额不足，重试无用 |
| 403 | 不重试 | 抓取被站点策略拒绝 |
| 404 | 不重试 | 拿不到可用正文（不可达 / 拦截 / 无正文 / 无文本） |
| 408 | **可重试** | 读请求体超时 |
| 413 | 不重试 | 超过大小限制 |
| 429 / 5xx / 连不上 | 可重试 | 交内核 |

`http.py` 三处向后兼容的小改：4xx 抛带 `status_code` 的 `ResponseError`（按码分档，
不做字符串匹配）；`post_json(retry_statuses=(408,))`；`_message` 认 OpenAI 形状的
`{"error":{"message":…}}`。

---

## 6. 工作流 B：文献入库

### 6.1 管线

```
inbox/*.pdf → sha256 → 已存在则跳过 → corpus/doc/<id>/source.pdf
            → pypdf 抽文本（layout 模式，逐页）
            → 结构切分（大纲 → 标题正则 → 按页）
            → 分块 → sources / chunks / FTS / vectors
            → meta.json → 删除 inbox 里的那一份
```

- 支持 `.pdf` / `.txt` / `.md`；其余扩展名上传时拒绝，入库再校验一次。
- **两栏排版**：`extraction_mode="layout"` 保留列位置；仍会串栏的（部分 Springer 模板）
  写 `meta.warning`，让模型知道这里的引用要打折。
- 扫描件（无文本层）**照实失败**：「没有文本层，需要 OCR，本轮不支持」。塞空白块进索引，
  会让检索结果看着正常、内容全空。
- 解析失败保留原件、写 `meta.error`；网页列表显示"解析失败"及原因，可删除重传。
- `pypdf` **进主依赖**（纯 Python、约 1MB）：缺了整条语料链不可用，没有"少了也能跑"的余地。
  仍按纪律在函数体内导入，缺了返回一条带 `pip install pypdf` 的失败说明。

### 6.2 结构切分

**两级**：先切 `section[]`，再在 section 内切 chunk。**chunk 永不跨 section。**

| 来源 | 结构从哪来 | 兜底 |
|---|---|---|
| 网页（Fetch 返回 Markdown） | `#`–`###` 标题 | 整页一块 |
| `.md` / `.txt` | Markdown 标题 / 空行段落 | 整文件一块 |
| PDF | `PdfReader.outline`（带页码） | ①大纲 → ②标题正则 → ③按页 |

**标题正则**（无大纲时）：行 ≤80 字、不以句号结尾、匹配 `^(\d+(\.\d+)*)\s+\S` 或
`Abstract|Introduction|Methods|Materials and Methods|Results|Discussion|Conclusion` 一类词。
pypdf 的纯文本抽取不带字号，所以这是**启发式**：`meta.structure` 记下实际用了哪一档，
网页列表里看得见"这份是按页切的"，不蒙在鼓里。

**切分参数**

| 参数 | 值 | 理由 |
|---|---|---|
| 目标块长 | 1200 字 | 有章节兜住上下文，块可以比纯滑窗大 |
| 浮动区间 | 900–1500 字 | 宁可短一点，也要落在段落/句末边界上 |
| 硬上限 | 2000 字 | 超过就按句末切，绝不切在句中 |
| 合并下限 | 300 字 | 同节过短的块合并，避免"一句话一块" |
| 重叠 | 上一块**最后一整句**，≤180 字 | 固定字数会从词中间开始，重叠部分读起来是乱的 |
| 表格 | 整块保留，超长按行切 | 切在行中间等于把表毁掉 |

**边界规则**

1. 切点优先级：**章节（硬）> 段落 > 句末 > 分句 > 词间**，永远不切在词中间。
2. 块从**句首**开始；浮动区间的唯一目的就是让切点落到最近的合法边界。
3. 块首紧贴标题时标题归下一块：`section` 存列里，正文不重复它。
4. 同节相邻块合并时优先保留「标题 + 首段」。
5. 跨块阅读靠检索的**邻块补全**与 `read_source` 整页读取，不靠把块做大。

**不索引的章节**：`References / Bibliography / Acknowledgments / Supplementary / 参考文献 / 致谢`。
常占论文 30–50% 篇幅、信息密度接近零。正文仍留在 `text.jsonl`，`read_source` 读得到，
只是不进检索；跳过哪些写进 `meta.skipped_sections`。

**嵌入输入带章节上下文**

```
passage: 3.2 In vitro assay
<块正文>
```

存进库的 `text` **不含**前缀：正文保持可引用、可原样展示；向量那一路却能吃到章节名。
同一段文字在 "Methods" 与 "Discussion" 里，向量本来就该长得不一样。

---

## 7. 检索

```
query
 ├─ chunks_fts (unicode61)  top 40 ─┐
 ├─ chunks_cjk (trigram)    top 40 ─┼─ 去重 → RRF(k=60) → 邻块 → 按节聚合 → top 8
 └─ vectors（余弦）           top 40 ─┘
```

- **RRF 不用分数相加**：BM25 与余弦量纲不同，直接相加会让某一路单方面支配。
  `score = Σ w/(60 + rank)`，三路等权起步。**S4 扫过权重与 k，等权 k=60 仍是最好的**
  （见 §11 的读数表）。
- **每路 40 条**：这个数同时是「每路召回深度」与「融合后进分组的候选数」。30 → 40
  在评测集上把「标注源进前 8 组」从 0.927 提到 0.945、「答案在片段里」从 0.796 提到
  0.815；50/80 一分不涨。只放大候选宽度、不放深每路召回（30/50）没有这个效果。
- **向量全表暴力扫**：5 万块 × 384 维 = 77MB、2000 万次乘加，CPU 几十毫秒。
  进程内缓存归一化后的矩阵，入库后按版本号失效。个人语料用不上向量库。
- **前缀**：默认模型不需要；E5 系列要 `query: ` / `passage: `，漏了不报错、只掉点。
  前缀只写在 `embed._prefixes` 一处。
- **余弦下限 0.30**：余弦没有「不相关」这个值，随便一句话都能找出 30 个 0.2 分的邻居。
  不设下限的话「本地没有」会变成「命中 29 组」，每组都答非所问。本机实测相关 0.44～0.57、
  无关 0.02～0.23，0.30 落在空档里；换模型要重测，可用 `VSA_EMBED_MIN_SCORE` 覆盖。
- **中文问英文文献**：向量负责语义那一半；词法侧仍要求模型把术语翻成英文再查。
- **邻块补全**：命中块前后各取一块（同 section），仍受 400 字/块预算。
- **按节聚合**：结果按 `(source, section)` 分组，同节多块合并成一段连续引用、只出现一次引用号。
  否则 top 8 会变成"同一页五个碎片 + 三篇无关论文"。
- **预算**：8 条 ×（引用号 + 标题 + 片段 400 字）≈ 3500 字，在 4000 字压缩线以内。
- **降级**：向量模型未下载 / fastembed 未装 / 开关关闭 → 只跑词法两路，
  `meta.vector_coverage=0`，正文不出现技术噪音。

### 7.1 工具面

| 工具 | 参数 | 说明 |
|---|---|---|
| `search_web` | `query`、`count`、`refresh` | 联网，计费；结果自动入库 |
| `fetch_webpage` | `url`、`max_chars`、`refresh` | 抓单页正文，入库为 full |
| `import_documents` | `path` | 第三个收路径的动作，写进八条纪律；逐条报 新增/重复/失败 |
| `search_corpus` | `query`、`limit`（默认 8，夹 1–20） | 唯一检索入口；`meta` 带三路命中与 `vector_coverage` |
| `read_source` | `ref`、`page`（仅 doc）、`max_chars` | 按页/整页读全文；web 未抓全文时提示用 `fetch_webpage` |

三个本地工具都是纯查询；`import_documents` 不声明 `context_argument`（语料跨会话，不进登记表）。
`search_corpus` 的描述第一段写清：**先查本地（免费、离线），不够再 `search_web`（计费）**。

工具顺序（`all_tools.py`）：`listing → uniprot → structure → chembl → rsearch → literature →
corpus → websearch → receptor → ligand → docking → affinity → export`。
本地检索排在联网搜索之前，与「先本地后联网」的描述一致。

---

## 8. 向量模型

### 8.1 选型与依赖

| 项 | 选择 | 理由 |
|---|---|---|
| 运行时 | `fastembed`（onnxruntime + tokenizers） | CPU 可跑、不依赖 torch |
| 模型 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384d，**255MB**（ONNX 在 `Qdrant/…-onnx-Q`，模型名与仓库名不同） | 中英兼顾；fastembed 清单里唯一合适的 384 维多语言模型 |
| 备选 | `BAAI/bge-small-zh-v1.5`，512d，90MB | 纯中文语料更准，`VSA_EMBED_MODEL` 手动切 |
| 存放 | `~/.opencode_tui/models/` | 不进 exe、不进仓；显式下载 |
| 依赖 | **`requirements-embed.txt`** | 主依赖只多一个 pypdf；不装只掉语义通道 |

不做自动选模型：同一个库混两种模型的向量，距离没有意义。

### 8.2 生命周期

- **下载**：`python -m opencode_tui.core.embed download`，打印大小与进度。
  第一次用到且没有模型时返回一句「语义通道未启用：运行 … download」，**不自动下载**。
- **入库时算**：`passage:` 编码，批量 16；50 页 PDF 约 300 块，CPU 数秒，同步完成并报耗时。
- **换模型**：`vectors.model` 不匹配的行整批删除重建；`sources` / `chunks` 不动。
- **失败**：某块编码失败只跳过并记数，不让整次入库失败。

### 8.3 评测门禁（硬条件）

- 评测集 `corpus/eval/questions.jsonl`，**55 条** `{"q":…, "ref":["web:…"], "tag":…}`，
  从真实使用里攒（同一问题几篇都答得对时给一组 ref）。`tag` 分 `cross-lingual` / `identifier` / `zh-zh`。
- `tools/check_retrieval.py` 跑三种组合：词法单路 / 双词法 + RRF / 三路 + RRF；
  指标 `recall@5`、`MRR@10`、**引用命中率**，并按"中文查询→英文文档"单独统计。
- 三条全满足才上线：
  1. 三路比双词法 `recall@5` 高 **≥8 个百分点**；
  2. 精确标识符查询（`4HJO`、`STI`）不掉 **>2 个百分点**；
  3. 单次检索 < 1s，exe 体积增幅 < 80MB。
- 达不到就只保留词法两路 + RRF——那不是"没有检索"，只是没有语义召回。

### 8.4 打包

onnxruntime + tokenizers 进 exe 约 +40–80MB，且需 `collect_dynamic_libs`。
S3 实测体积与启动时间；超 +80MB 改成「exe 内不带，用户另装」（与 DTA 同一种处理）。

---

## 9. 网页端

从「只读查看端」改成「查看只读 + 语料投递与管理」。路由仍只有一个真相（`SPEC`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/library` | 列表（标题、类型、页数、切分方式、向量覆盖、状态、占用）+ 待入库数 |
| POST | `/api/library/upload` | `multipart`，落 `corpus/inbox/`，**不写索引** |
| POST | `/api/library/<kind>:<id>/rename` | 只写 `labels.json` |
| POST | `/api/library/<kind>:<id>/delete` | 挪进 `.trash/`，幂等 |

**索引只有一个写者**：web 从不**写** `library.db`，只在列表页以 `mode=ro` 读一次
（块数、向量覆盖）。删除后 `search_corpus` 对命中源做 `exists` 检查，目录没了就跳过；
下次入库统一清悬空行。代价是删除不是"立刻从索引消失"，而是"查不到"——对用户是一样的。

必须防的（都要进 `tools/check_web_api.py`）：

- ID 匹配 `^(doc|web):[0-9a-f]{10}$`，路径由 ID 拼，**不接受用户给的路径**。
- `multipart` 是 CORS 简单请求：恶意网页可跨站往 `127.0.0.1` 发 POST。上传要求自定义头
  （逼出预检）+ 校验 `Origin`；改名/删除走 JSON 同样校验。
- 大小边写边卡（不信 `Content-Length`），单文件 50MB；basename、禁控制字符、不覆盖。
- 临时文件 → `fsync` → 换名；中断不留半截。
- 删除要确认弹窗，写明「可在 `corpus/.trash/` 找回」。

---

## 10. 开关与配置

一个配置文件、两个开关、三个环境变量。**不引入第三套配置机制**。

`<repo>/.opencode_tui/corpus.toml`（项目级，与 `tools.toml` 同目录，gitignore）：

```toml
[corpus]
# search_web 的结果摘要是否自动进语料。关掉后它退回纯查询，
# 只有 fetch_webpage 抓过的页面才入库。
index_web = true

# 是否启用语义通道。模型没下载 / fastembed 没装时会自动降级，这里管的是
# 「明明能装却不想用」的情况（速度优先、要完全可复现的排序）。
embed = true
```

| 设置 | 环境变量覆盖 | 默认 | 作用 |
|---|---|---|---|
| `index_web` | `VSA_CORPUS_INDEX_WEB=0/1` | 开 | 搜索摘要自动入库 |
| `embed` | `VSA_CORPUS_EMBED=0/1` | 开 | 启用向量通道 |
| 模型名 | `VSA_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | 换模型触发整表重建 |

- 环境变量优先（与 `env.py:load_env(override=False)` 同一条规则）。
- 每次调用现读文件，改完立即生效；文件缺失按默认值跑，不报错。
- 浏览器里的 `/library` 只显示当前值，**不能改**——改配置是写操作，
  web 只允许写 `inbox/` 与 `labels.json`。

**清理**：不自动删。`/library` 顶部显示 `查询归档 N 条 / 语料 M 份 / 占用 X MB`，
并提供命令 `python -m opencode_tui.core.corpus prune --summary`（只清 `depth=summary`
的网页源，`full` 与 `doc` 不动）。

索引重建：`python -m opencode_tui.core.index reindex`——改过切分参数、或从 S0 升上来
要回填 `corpus/web/` 时跑它。索引是派生物，删掉整个 `.index/` 再跑一遍就回来了。

---

## 11. 分期与验收

| 期 | 内容 | 触及面 | 验收 |
|---|---|---|---|
| **S0** | 两个联网工具 + 查询归档 + `corpus/web/` 落盘（不建索引）+ `http.py` 三处小改 | 3 个文件，不碰网页端 | `test_agent --only websearch`；`debug_tools.py` 真跑一次（<0.1 元） |
| **S1** | 解析 + 结构切分 + chunks/FTS 双索引 + 三个本地工具 + 回填 `corpus/web/` + pypdf | 新增两个模块、主依赖 +1 | `test_agent --only corpus`；同一批文件重建两次，分块与引用完全一致 |
| **S2** | 网页端 `#/library` + 4 条路由 | 章程变更 | `tools/check_web_api.py`（上传/改名/删除/跨站/穿越/幂等） |
| **S3** | fastembed + vectors + 三路 RRF + `check_retrieval.py` | 新依赖、打包 | 评测三条门禁 |
| **S4** | 调优：RRF 权重、邻块范围、章节加权、References 阈值（实做：每路召回深度 30→40，其余三项为负结果） | 只改检索参数 | 评测集不退化 |

S0 先做：纯增量、无写入风险、当天可验收。S1 是真正的工作量，也是引用质量的根。

**状态（2026-10-23）**：S0、S1、S2、S3、S4 已完成。

- S0：两个联网工具、查询归档、`corpus/web/` 落盘、`http.py` 三处小改。
  `python tests/test_agent.py --only websearch` 绿；真实 key 跑通一次 search + fetch（约 0.01 元）。
- S1：`core/parse.py` / `core/chunker.py` / `core/index.py` / `core/corpus_config.py`、
  三个本地工具、`corpus/web/` 回填（`python -m opencode_tui.core.index reindex`）、
  `pypdf` 进主依赖、两份 PDF 夹具。`python tests/test_agent.py --only corpus` 绿，
  「重建两次逐字相同」已验证。真实网页正文回填后自动认出 `## 鉴权`、`### 请求` 这类小节，
  说明反转义那一步是有效的。
- S2：`GET /api/library` + 上传 / 改名 / 删除三条写入口、`#/library` 页（列表、投递、
  拖拽、改名与删除的确认弹窗）。`python tools/check_web_api.py` 42 条全过，含跨站
  POST、异站 `Origin`、文件名穿越、id 穿越、超限、同名不覆盖、删除幂等。
- S3：`core/embed.py`（fastembed + ONNX，**无 torch**）、三路 RRF、余弦下限、
  `requirements-embed.txt`、`tools/check_retrieval.py`。真实语料 64 源 / 301 块、**55 条**
  评测集上跑：词法双路 `recall@5` 0.418 → 三路 0.855（+43.6pp）；「中文问英文答」
  0.314 → 0.829（n=35）；精确标识符不掉；热态 p95 17ms、首次 899ms（加载模型）。
  三条门禁全过。同时补上了 S0/S1 漏掉的一步：网页落盘后**当场进索引**，不再等 `reindex`。
  模型下载直连不通时自动退到 `hf-mirror.com`（打印实际端点，固定关 Xet）。

- S4：调优。**四项里三项是负结果**，只采纳了「每路 40 条」。同时给判据加了组级指标
  （标注源命中 / 答案在片段里 / 平均字数），修了 5 处只在标题里出现的 `must`。

  | 参数 | 试过 | 结论 |
  |---|---|---|
  | RRF 权重 | `cjk∈{0,0.5,1}` × `vec∈{0.5,1,1.5,2}` | 等权最好。`vec=1.5` 起 recall 掉到 0.800；`vec=0.5` MRR +0.009 但答案掉 1 条。**不动** |
  | RRF k | 10 / 20 / 40 / 60 / 80 / 120 | `k=10` MRR +0.015、其余一分不动 —— 只挪了一条的名次，判为噪声。**不动** |
  | 章节加权 | 查询词命中章节名 ×1.05～1.4（另加「章节名 ≤40/80 字」的长度限制） | 无增益；放开长度限制时组命中 0.927 → 0.873（长「章节名」其实是标题+首段的解析残渣）。**弃** |
  | References 阈值 | 行首编号 / 年份占比 ≥50% 判为引文块并丢弃 | 语料里 0 命中：真正的 `References` 章节早在切块时（`label_skip`）就跳过了，剩下的引文块不成片。**弃** |
  | 邻块范围 | span 0 / 1 / 2 | 组文本被 600 字预算截断，三者读数完全相同；只影响「组内有几块」，不影响给模型看的东西。**不动** |
  | 片段预算 | 400/600 → 1200/1600 | 只有 1200/1600 多出 2 条答案，代价是每次检索 2858 → 6378 字（≈2.2 倍 token）。**不动** |
  | 每路召回深度 | 30 / 40 / 50 / 80 | **40 是拐点**：组级源命中 0.927 → 0.945、答案 0.796 → 0.815，MRR 不降；50/80 不涨。**采纳** |
  | 标题拼进向量输入 | 全部 301 块重算 | recall@5 0.855 → 0.891，但**答案在片段里 0.796 → 0.709**、标识符 MRR 0.867 → 0.740：整篇的块长得像同一段，具体那一段反而排不到前面。**弃** |
  | 中文通道改造 | 二元组 / 三元组 OR；二元组单独作一路 | 单路 recall 0.418 → 0.509（zh-zh 0.533 → 0.800），但并进三路后 0.855 → 0.836 —— 与词法那一路高度相关，只是把 RRF 的票分薄了。**弃** |

  读数（同一批 55 条、64 源 / 301 块）：

  ```
  每路/候选 30  块级 recall@5=0.855 MRR=0.715 | 组级 源命中=0.927 答案=0.796
  每路/候选 40  块级 recall@5=0.855 MRR=0.717 | 组级 源命中=0.945 答案=0.815
  每路/候选 50  块级 recall@5=0.855 MRR=0.717 | 组级 源命中=0.945 答案=0.815
  ```

  剩下 3 条组级漏检都是「中文问、英文文献答」且分数只差一点（融合名次 11–13），
  瓶颈在 384 维多语模型的跨语言能力，不在排序参数——换更大的模型是另一件事（体积、
  下载、打包），不在 S4 范围内。

### 文件清单

**S0**：新增 `core/search_config.py`、`core/corpus.py`（目录与原子写，不含索引）、
`domain/websearch.py`；改 `domain/http.py`、`domain/__init__.py`、`domain/all_tools.py`、
`widgets/messages_pane.py`、`debug_tools.py`、`.gitignore`。

**S1**：新增 `core/parse.py`、`core/chunker.py`、`core/index.py`、`domain/corpus.py`、
`core/corpus_config.py`；改 `core/corpus.py`、`domain/all_tools.py`、
`requirements.txt`（+pypdf）、`tests/test_agent.py`；`tests/fixtures/` 放一份有书签与一份
无书签的 PDF（两条切分路径都要能测）。

**S2**：`core/web/routes.py`、`web/`、`tools/check_web_api.py`。

**S3**：新增 `core/embed.py`、`requirements-embed.txt`、`tools/check_retrieval.py`；
改 `core/index.py`（`upsert_web_page`、`kv` 版本表与三个触发器、`reindex` 之后补向量）、
`core/corpus_config.py`（模型默认值改为 `embed.DEFAULT_MODEL`）、
`core/agent/domain/corpus.py`（第三路 + 入库时算向量）、
`core/agent/domain/websearch.py`（落盘即入索引）、`tests/test_agent.py`（`--only embed`）。
`opencode_tui.spec` **不动**：模型 255MB，超过 +80MB 预算，走「exe 内不带、用户另装」。

---

## 12. 风险

| 风险 | 处理 |
|---|---|
| 结构切分质量参差（无书签、两栏、扫描件） | `meta.structure` 记实际用哪一档，列表可见；两栏加 `warning`；扫描件直接失败 |
| 章节误判，把正文当标题 | 只认 ≤80 字、无句号、匹配编号或白名单的行；不确定就退回按页 |
| 网页结果无限增长 | 10 条摘要/次、按 URL 去重；不自动清理；`/library` 显示占用 + `prune --summary` |
| 查询归档与语料混为一谈 | §4.4 两层生命周期分开；引用号永远指向语料 |
| 向量静默降级 | `meta.vector_coverage`；首次缺模型给一句可执行的下载指引。降级时覆盖率报 0，不报「100% 但没跑」 |
| 余弦没有「不相关」 | `MIN_SCORE=0.30` 下限，可用 `VSA_EMBED_MIN_SCORE` 覆盖；实测相关 0.44+、无关 0.23- |
| 矩阵缓存拿旧 id 配新块 | `kv` 版本号 + 触发器（级联删除也会 bump），缓存键 `(库, 模型, 版本)` |
| 模型下载被墙 / 镜像 401 | `download` 先试 `HF_ENDPOINT`（或直连），失败**自动退到 `hf-mirror.com`** 并打印用了哪个端点；镜像的 Xet 后端会回 401，下载子进程里固定 `HF_HUB_DISABLE_XET=1`。子进程是必须的：`huggingface_hub` 在 import 时就把端点读成常量了 |
| E5 前缀漏写 | 编码函数只有一处，前缀写在常量里，注释说明漏了会掉点 |
| 换模型后新旧向量混算 | `vectors.model` 参与过滤；不匹配整批重建 |
| exe 体积 | 实测：依赖 78MB + 模型 255MB，超 +80MB 预算 → **不进 exe**，用户 `pip install -r requirements-embed.txt` 后自己 `embed download`（与 DTA 同一种处理） |
| 网页端从只读变可写 | 章程同步改；`check_web_api.py` 覆盖跨站、穿越、幂等、半截文件 |
| **抓回的 Markdown 带转义**（实测 `\/`、`\#`、`\-`、`\|`） | **已处理**：`parse.unescape_markdown` 只还原 `\` + ASCII 标点，入库时对网页内容做一次，原件仍是抓回来的样子。 |
| 删除/改名与入库并发 | 改名只写 `labels.json`；删除走 `.trash` 换名；索引单写者 + 存在性检查 |
| 引用被模型编造 | 引用号只能来自工具返回；`read_source` 只认 `(doc|web)#[0-9a-f]{10}` |
| 范围膨胀 | §2 的非目标清单是硬边界；标签、编辑、OCR 一律不做 |

---

## 13. 已定事项

1. 网页搜索**不设过期**：查询归档永久保留，`refresh: true` 是唯一绕行方式。
2. 切分按结构，遵守 §6.2 的边界规则；嵌入带章节前缀；结果按节聚合。
3. `pypdf` 进主依赖；`fastembed` 拆到 `requirements-embed.txt`。
4. 默认 `multilingual-e5-small`，备选只在纯中文语料时手动切。
5. 网页摘要默认入库，`corpus.toml` 的 `index_web` 可关；语义通道 `embed` 可关。
6. 清理不自动做，只给占用显示与 `prune --summary` 命令。
