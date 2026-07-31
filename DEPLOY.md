# 部署文档：Zotero 网页文献检索工具（公网可访问）

目标：把本地 `http://127.0.0.1:8777/` 变成任何人都能打开的公开网站。
本工具是「前端 HTML + Python 后端 + Zotero 凭证」一体，必须前后端同域部署，
**不能**扔到纯静态托管（GitHub Pages / Vercel / Netlify 会让 API 全部失效）。

代码已改造为云端就绪：
- 自动识别云平台（HF Spaces / Render）→ 绑定 `0.0.0.0`、读取平台 `PORT`
- Zotero 凭证走环境变量（`ZOTERO_USER_ID` / `ZOTERO_API_KEY`），不进代码
- 导入接口受 `ACCESS_TOKEN` 保护，未带令牌返回 401
- 云端自动跳过本地代理扫描，直接联网

---

## 方案一：Hugging Face Spaces（最省事，免费 + 自带 HTTPS + 公开子域名）

1. 注册/登录 https://huggingface.co （免费）。
2. 右上角 **New Space** → 填写名称（如 `zotero-search`）→ **SDK 选 Docker** → 可见性 **Public** → Create。
3. 进入 Space 后，左侧 **Settings → Variables and secrets**，添加：
   - `ZOTERO_USER_ID` = 你的 Zotero 用户 ID
   - `ZOTERO_API_KEY` = 你的 Zotero API Key
   - `ACCESS_TOKEN` = 任取一个强口令（导入时用）
4. 把本文件夹内容推上去（三种方式任选）：
   - **网页上传**：Space 页面 Files 标签 → 拖入本文件夹全部文件
   - **Git**：`git clone <Space 的 git 地址>` → 复制本文件夹文件 → `git add -A && git commit -m init && git push`
   - **CLI**：`pip install -U huggingface_hub` → `huggingface-cli upload <你的用户名>/zotero-search ./`
5. 等构建完成（约 1–2 分钟），访问 `https://<用户名>-zotero-search.hf.space/` 即可。

> HF 免费版 Space 是公开子域名（`*.hf.space`），无自定义域名；如需自有域名需付费升级。

---

## 方案二：Render（免费层，自动 HTTPS + 公开子域名）

1. 注册/登录 https://render.com （免费）。
2. **New → Web Service** → 连接你的 GitHub 仓库（先把本文件夹推到 GitHub）。
3. 配置：
   - Runtime: Docker
   - 端口：自动（Render 注入 `PORT`）
   - 在 **Environment** 添加同上的三个变量
4. 免费层会休眠（一段时间无访问后冷启动约 30–50 秒），首次打开稍等。

---

## 方案三：你本机 + Cloudflare Tunnel（零部署、本机常开、可免费绑自有域名）

适合只想立刻拿到公网链接、且电脑一直开机的情况：

```bash
# 1) 本机照常启动服务
python zotero_web_search.py
# 2) 用 cloudflared 建立隧道（需下载 cloudflared）
cloudflared tunnel --url http://127.0.0.1:8777
```
终端会输出一个 `https://xxxx.trycloudflare.com` 公开地址，别人可直接访问。
若要绑定自有域名，用 Cloudflare 控制台建 Named Tunnel 并指向该域名（免费）。

---

## 使用说明（给别人）

- 打开网站 → 输入关键词 → 选数据源（PubMed 等）→ 检索 → 勾选 → 一键导入。
- **检索任何人都能用**（只读，不消耗你的 Zotero 配额）。
- **导入需要令牌**：在导入请求的 Header 带 `Authorization: Bearer <ACCESS_TOKEN>`，
  或由你在前端/脚本里内置该令牌。未设置 `ACCESS_TOKEN` 时导入不校验（不建议公网这样）。

## 安全红线（务必遵守）

1. `ZOTERO_API_KEY` 只放平台的 Variables/Secrets，绝不写进代码或公开仓库。
2. 公网务必设置 `ACCESS_TOKEN`，否则任何人都能往你 Zotero 库塞文献。
3. 本仓库的 `.gitignore` 已排除 `zotero_config.json` 等本地凭证文件。
