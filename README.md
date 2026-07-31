---
title: Zotero 文献检索
emoji: "🔍"
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
---

# Zotero 文献检索（网页版）

> 部署到 Render：见仓库根目录 `render.yaml`（已配置 Python 运行时 + Free 计划 + 环境变量占位）。
> 部署到 Hugging Face Spaces：本 README 的 frontmatter 即 Space 元数据，直接拖入即可。

类 PubMed 的文献检索框：支持 **PubMed / Crossref / Europe PMC / OpenAlex** 多数据源检索，
结果可勾选后一键导入你的 Zotero 库（元数据 + 可获取的全文 PDF/PPT）。

- 检索为公开只读（任何人可访问，不依赖 Zotero 凭证）
- 导入接口受 `ACCESS_TOKEN` 保护，防止公网任意写入你的 Zotero 库

## 部署时必须配置的环境变量（Space 设置 → Variables and secrets）

| 变量名 | 说明 | 是否必填 |
|---|---|---|
| `ZOTERO_USER_ID` | 你的 Zotero 用户 ID（在 zotero.org 设置 → 密钥 页面） | 必填（导入需要） |
| `ZOTERO_API_KEY` | 你的 Zotero API Key | 必填（导入需要） |
| `ACCESS_TOKEN` | 自定义访问口令，导入接口需携带 `Authorization: Bearer <token>` | 强烈建议 |

> ⚠️ 切勿把 `ZOTERO_API_KEY` 写进代码或提交到公开仓库，一律用平台 Secret / 环境变量注入。

## 使用说明

- **检索**：任何人打开网页即可输入关键词检索（公开只读，不消耗你的 Zotero 配额）。
- **导入**：页面底部有「访问令牌（导入用，选填）」输入框。公开部署时，在这里填入你设置的
  `ACCESS_TOKEN` 再点导入；留空且服务端未设 `ACCESS_TOKEN` 时也可导入（仅建议本地使用）。
- 令牌会保存在浏览器 localStorage，下次自动填充。

## 本地运行（可选）

```bash
pip install -r requirements.txt   # 实际上无第三方依赖
python zotero_web_search.py
# 浏览器打开 http://127.0.0.1:8777/
```
