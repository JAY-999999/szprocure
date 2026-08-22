# SZ Procure SEO 地基说明 + 20 万料号上架路线图

> 生成日期：2026-08-22
> 适用：szprocure-site 静态站（待绑公网域名 www.szprocure.com 后生效）

## 一、本次已完成（地基级，上线前必做）

### A 块 — 9 个现有页面 SEO 地基（P0 + P1）
| 项目 | 状态 | 说明 |
|------|------|------|
| robots.txt | ✅ | 放行全站，声明 sitemap（含 sitemap_parts.xml 预留） |
| sitemap.xml | ✅ | 9 页 + x-default hreflang，提交 Search Console 用 |
| canonical | ✅ 9/9 页 | 自引用规范链接，防重复内容 |
| hreflang x-default | ✅ 9/9 页 | 指向英文版（中文仅内部核对，不进收录） |
| Open Graph | ✅ 9/9 页 | LinkedIn/社媒分享门面（og:title/desc/image） |
| Organization JSON-LD | ✅ 9/9 页 | 公司实体结构化数据，利于 Google/AI 搜索引用 |

**关键决策**：中文版(`data-zh` 客户端切换)仅保留给你自己核对文案用，**不生成真实 `/zh/` URL、不写 zh hreflang 互链**——避免 hreflang 指向死链被 Google 惩罚，也省掉 9 个中文真实页工程。

### B 块 — 20 万型号页架构（数据驱动静态预生成，语义 URL）
| 组件 | 文件 | 状态 |
|------|------|------|
| 生成脚本 | `gen_parts.py` | ✅ 已验证（100 条试生成：100 产品 + 13 品牌 + 25 分类 = 140 URL） |
| 产品页模板 | `/products/{slug}/index.html` | ✅ 含真实价值内容 + Breadcrumb/Product JSON-LD |
| 品牌页模板 | `/manufacturers/{slug}/index.html` | ✅ 捕获 "X distributor China" 类词，链回产品 |
| 分类页模板 | `/categories/{slug}/index.html` | ✅ 大流量枢纽，链回产品+品牌 |
| 聚合 hub 页 | `/manufacturers/`、`/categories/` | ✅ 目录入口 |
| 分页样式 | `assets/styles.css`（追加段） | ✅ breadcrumb / part-index / muted |
| 分批 sitemap | `sitemap_parts_index.xml` | ✅ 自动拆分（每批 4.5 万上限，20万→~5文件） |
| Product + Breadcrumb + Organization JSON-LD | 每页内嵌 | ✅ 三级结构化数据齐全 |

**URL 架构（一次定型，上线前零成本改）**：
- 产品：`/products/{slug}/`（非 `/part/`，语义自描述，AI 可读）
- 品牌：`/manufacturers/{slug}/`
- 分类：`/categories/{slug}/`
- 禁用动态参数 `?id=`

**模板防 thin content 机制**：
- 每页有独特文案（参数、替代料、应用、采购笔记），非空壳
- `Status=scarce` 料号额外渲染 "Why hard to source" 段落（稀缺性是你对标 Digi-Key 的差异化价值）
- `Status=eol` 料号渲染 "end-of-life" 段落（引导替代料查询）
- 替代料自动互链（如 AD7606 → AD7606B/AD7605/AD7616 各自独立页）
- 面包屑层级：Home › Category › Part（BreadcrumbList JSON-LD）

**已知数据问题（后续规整）**：料号库 Mfr 字段写法不统一（"ADI" / "ADI Linear" / "ADI Maxim" 被拆成 3 个品牌页）。20 万条上线前需统一制造商命名，否则品牌页会碎片化。

## 二、20 万料号上架操作流程

```bash
# 1. 把最终 20 万条 CSV 放到指定路径（字段同 料号库.csv）
# 2. 运行生成脚本（约几分钟，全静态输出）
python gen_parts.py --csv "路径/20万料号.csv" --out "."

# 3. 脚本自动：
#    - 生成 /products/{slug}/index.html × 200000
#    - 生成 /manufacturers/{slug}/ 和 /categories/{slug}/ 聚合页
#    - 拆分 sitemap_parts_1.xml ... _5.xml（每批 ≤4.5万）
#    - 生成 sitemap_parts_index.xml

# 4. 部署：把整个 szprocure-site 文件夹推到 Vercel/Netlify
#    （静态托管零服务器成本，20万页秒开）

# 5. 上线后：
#    - Search Console 提交 sitemap.xml + sitemap_parts_index.xml
#    - robots.txt 已声明两个 sitemap
```

## 三、上线前还必须做（本会话未做，需你决策/提供）

| 项 | 说明 | 阻塞点 |
|----|------|--------|
| 绑公网域名 www.szprocure.com | SEO 只在公网生效，本地 127.0.0.1 不被收录 | 需你完成 Namecheap DNS + Vercel 部署 |
| OG 图片优化 | 现用 hero.svg 占位，建议换 1200×630 真实 PNG（分享更美观） | 需设计图 |
| 真实公司信息 | Organization sameAs、WhatsApp 号当前为占位 | 需你填真实值 |
| Core Web Vitals 实测 | 需上线后用 PageSpeed Insights 测真实字段数据 | 需公网 |
| 内容支柱(Pillar) | 写"如何解决 LTB/EOL 缺货"类指南攒 E-E-A-T 权威 | 后续内容任务 |

## 四、SEO 策略对齐（对标 Digi-Key/Mouser 护城河）

| Digi-Key 护城河 | 我们的实现 | 阶段 |
|----------------|-----------|------|
| 每个型号独立可索引页 | `/part/{slug}/` 静态生成 | ✅ 已搭架构 |
| 参数化筛选 | 待做（需搜索/筛选页，属 P2） | ⏳ 后续 |
| 交叉参考页 | 替代料互链已实现（页内） | ✅ 基础版 |
| 海量 SKU | 20 万静态页 | ✅ 脚本就绪 |

**警告**：不要为凑数量生成空壳参数页。Google 对 thin content 惩罚严重。当前模板已内嵌真实价值内容，20 万条也必须保证 CSV 字段完整（尤其 KeySpecs/AltParts/Notes），缺一即退化成薄内容。

## 五、抓取预算控制（20 万页必考虑）

- 型号页 `priority=0.5`，核心页 `0.6~1.0`（已在 sitemap 区分）
- 分页/ facets 未做，暂不产生无限 URL
- 上线后用 Search Console 监控"已编入索引页数 / 已提交"，若抓取慢可提 priority 或加内链
