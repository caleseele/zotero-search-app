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

## 方案一（推荐）：Render —— 免费 + 自动 HTTPS + 公开子域名

> 为什么选它：对国内 IP 友好，不易像 HF 那样因代理 IP 被风控 403。
> 前提：需要一个 **免费的 GitHub 账号**（Render 必须从 Git 仓库拉代码，不能拖文件上传）。

### 第 0 步：把代码推到 GitHub（只需做一次）
1. 注册 https://github.com （免费）。
2. 新建一个**私有或公开**仓库，命名为 `zotero-search`（不要勾选自动生成 README）。
3. 在本机 `cyyimprove_space` 文件夹里执行（把 `<你的GitHub用户名>` 替换掉）：
   ```bash
   cd C:\Users\希儿\Desktop\cyyimprove_space
   git remote add origin https://github.com/<你的GitHub用户名>/zotero-search.git
   git branch -M main
   git push -u origin main
   ```
   > 推送时若提示登录，用 GitHub 用户名 + 一个 **Personal Access Token**（不是账户密码）。
   > 注册/登录 GitHub 时建议**临时关闭代理**用真实 IP，避免也踩风控。

### 第 1 步：在 Render 创建服务
1. 注册/登录 https://render.com （免费）。
2. 右上角 **New + → Web Service**。
3. 选择 **Connect a repository** → 授权 GitHub → 选中刚建的 `zotero-search` 仓库。
4. Render 检测到仓库里的 `render.yaml` 会自动套用配置：
   - Runtime: Python 3.11
   - Build: `pip install -r requirements.txt`
   - Start: `python zotero_web_search.py`
   - Plan: **Free**
   - 实例区域：可保持默认（Oregon）或改 Singapore。
   若没自动识别，手动填上面的 Build / Start 命令即可。
5. 点 **Create Web Service**。

### 第 2 步：设置环境变量（关键，Security）
进入服务 **Environment** 标签，添加以下变量（建议全部勾选 "Secret"）：

| 变量 | 值 | 说明 |
|---|---|---|
| `ZOTERO_USER_ID` | 你的 Zotero 用户 ID | zotero.org → Settings → API Keys 页顶部 |
| `ZOTERO_API_KEY` | 你的 Zotero API Key | 同页 → Create new key |
| `ACCESS_TOKEN` | 任取一段强随机串 | 导入接口令牌，别人无此串不能写你库 |

> 注意：`render.yaml` 里这些变量标记为 `sync: false`，表示**由你在后台手动填**，
> 不会从仓库读取——这正是为了安全（凭证不进代码）。

### 第 3 步：等待部署
保存后会自动构建（约 1–2 分钟）。完成后 Render 分配公开地址：
`https://zotero-search.onrender.com/`

把它发给别人即可访问。免费层在长时间无访问后会**休眠**，首次打开冷启动约 30–50 秒，稍等即可。

---

## 方案二：Hugging Face Spaces（免费 + 自带 HTTPS，但代理 IP 易被风控）

> ⚠️ 实测：部分代理 IP 访问 huggingface.co/join 会被返回 403（反滥用风控）。
> 若遇到，请**关闭代理用真实 IP** 注册，或改用上面的 Render。

1. 注册/登录 https://huggingface.co （免费，真实 IP 访问）。
2. 右上角 **New Space** → 名称 `zotero-search` → **SDK 选 Docker** → Public → Create。
3. **Settings → Variables and secrets** 添加 `ZOTERO_USER_ID` / `ZOTERO_API_KEY` / `ACCESS_TOKEN`。
4. 把本文件夹内容上传：
   - 网页：Space 的 Files 标签拖入全部文件；或
   - Git：`git clone <Space 的 git 地址>` 后复制文件再 push；或
   - CLI：`pip install -U huggingface_hub && huggingface-cli upload <用户名>/zotero-search ./`
5. 构建完成访问 `https://<用户名>-zotero-search.hf.space/`。

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
- **导入需要令牌**：页面底部「访问令牌」框填入你设的 `ACCESS_TOKEN` 再点导入。

## 安全红线（务必遵守）

1. `ZOTERO_API_KEY` 只放平台的 Variables/Secrets，绝不写进代码或公开仓库。
2. 公网务必设置 `ACCESS_TOKEN`，否则任何人都能往你 Zotero 库塞文献。
3. 本仓库的 `.gitignore` 已排除 `zotero_config.json` 等本地凭证文件。
