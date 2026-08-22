# SZ Procure 部署到 Vercel（外贸站 · 静态站）

> 适用：纯静态站（`gen_parts.py` 预生成 HTML，无后端）。面向海外买家，英文为主、中文不收录。

## 为什么选 Vercel
- 纯静态 JAMstack，全球 CDN，欧美买家访问快，LCP 易达标（<2.5s）。
- 免费 HTTPS + 自动 CI（git push 即部署）。
- 预渲染 HTML，Google 直接抓取，不存在 JS 渲染抓不到的问题——这是 20 万型号页护城河的基础。

## 前置准备
1. 安装 Git（已装可跳过）。
2. 注册 GitHub 账号（免费）。
3. 注册 Vercel 账号，用 GitHub 登录。

## 步骤 1：把代码推到 GitHub
在本机（或在此工作目录）执行：
```bash
cd /path/to/szprocure-site
git init
git add -A
git commit -m "SZ Procure static site v1"
git branch -M main
git remote add origin https://github.com/<你的用户名>/szprocure-site.git
git push -u origin main
```
> 注意：`.gitignore` 已排除 `_old_*` 备份、`serve_local.py`、预览脚本等，不会进仓库。

## 步骤 2：Vercel 导入并部署
1. 打开 https://vercel.com → “Add New” → “Project”。
2. 选刚推的 `szprocure-site` 仓库 → Import。
3. Framework Preset 选 **Other**（纯静态，无需构建）。
4. Build Command 留空；Output Directory 填 `.`（点号，根目录即输出）。
5. Deploy。几十秒后拿到 `*.vercel.app` 临时域名，打开验证页面/搜索/图片是否正常。

## 步骤 3：绑定正式域名 www.szprocure.com
1. 在 Vercel 项目 → Settings → Domains → 输入 `www.szprocure.com` → Add。
2. Vercel 会给你两条记录：
   - **CNAME** `www` → `cname.vercel-dns.com`
   - （可选）ANAME / 根域 `@` → 按提示填
3. 去 Namecheap（你的域名注册商）：
   - 改 **CNAME 记录**：主机 `www` → 值 `cname.vercel-dns.com`。
   - **不要动 MX 记录**（邮件不受影响）。
   - 若用根域 `@` 跳转 www，按 Vercel 提示加 URL Redirect。
4. 等 DNS 生效（几分钟~几小时），Vercel 自动签发 TLS 证书（HTTPS）。

## 步骤 4：提交 sitemap 到 Google
1. 打开 https://search.google.com/search-console （用 Google 账号）。
2. 添加属性 `https://www.szprocure.com`（URL 前缀模式）。
3. 验证：选 “HTML 标记” 或 “DNS 记录”（推荐 DNS，在 Namecheap 加一条 TXT）。
4. 左侧 → Sitemaps → 提交：
   - `https://www.szprocure.com/sitemap.xml`
   - `https://www.szprocure.com/sitemap_parts_index.xml`
5. 之后每周跑一次 `gen_parts.py` 重新生成页面，Vercel 会自动重新部署（git push 触发）。

## 日常更新 SKU 流程
```bash
# 1. 把新料号追加进 data/sample_parts.csv（或用正式料号库）
# 2. 重新生成全站静态页
python gen_parts.py
# 3. 提交并触发 Vercel 部署
git add -A && git commit -m "add parts batch" && git push
```

## 注意事项 / 红线
- **图片**：`Image` 字段只填本站自托管素材（如 `/assets/img/xxx.svg`），**绝不做第三方商城图片热链/搬运**（版权 + Google 降权风险）。
- **中文页**：当前 `data-zh` 仅本地预览核对用，不进 Google 收录（无 `/zh/` 路由、hreflang 只留 x-default），避免重复内容惩罚。
- **成本**：静态站流量不大时 Vercel 免费层足够；真到百万 PV 再考虑升级或换 Cloudflare Pages（抗 DDoS 更强，适合外贸站）。

## 备选：Cloudflare Pages
若后续需要更强海外抗攻击 / 更便宜带宽，可改投 Cloudflare Pages——同样纯静态、同样 git 驱动，步骤类似（导入仓库 → 输出目录 `.` → 绑域名）。SEO 效果一致。
