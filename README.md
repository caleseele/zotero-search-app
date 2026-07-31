---
title: Zotero 文献检索
emoji: "🔍"
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
---

# Zotero 文献检索（网页版）

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

## 本地运行（可选）

```bash
pip install -r requirements.txt   # 实际上无第三方依赖
python zotero_web_search.py
# 浏览器打开 http://127.0.0.1:8777/
```
