#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zotero 网页文献检索工具 —— 本地 PubMed 风格检索框
====================================================================
功能：
  1. 输入关键词（标题/作者/DOI/PMID 等），选择数据源检索文献
  2. 返回匹配文献列表，支持勾选 + 一键导入 Zotero（元数据 + 可获取 PDF 全文）
  3. 高级筛选：出版年份范围、文献类型、语言
  4. 检索历史（服务端 web_search_history.json + 前端 localStorage）
  5. 纯本地网页服务，浏览器打开即用，复用现有 Zotero Web API 代码

设计原则（与现有程序的关系）：
  - 【只新增、零修改】本项目所有现有文件（zotero_fulltext_fetch.py /
    zotero_auto_fetch.py / zotero_upstream_fetch.py 等）一律不动。
  - 复用 zotero_upstream_fetch 的 PubMed 检索 / 解析 / 入库逻辑
  - 复用 zotero_fulltext_fetch 的 Web API、全文获取、文件上传逻辑

启动：
  python zotero_web_search.py
  浏览器打开 http://127.0.0.1:8777/
"""
import sys, os, json, time, datetime, re, threading
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zotero_fulltext_fetch as zff
import zotero_upstream_fetch as zuf

CONFIG_PATH = os.path.join(HERE, "web_search_config.json")
HISTORY_PATH = os.path.join(HERE, "web_search_history.json")
HISTORY_MAX = 50
DEFAULT_PORT = 8777

SOURCE_LABELS = {
    "pubmed": "PubMed",
    "crossref": "Crossref",
    "europepmc": "Europe PMC",
    "openalex": "OpenAlex",
}


# ---------------------------------------------------------------- 配置
def load_config():
    cfg = {
        "port": DEFAULT_PORT,
        "sources": ["pubmed", "crossref", "europepmc", "openalex"],
        "default_limit": 30,
        "max_limit": 100,
        "attach_pdf": True,
        "openalex_mailto": "research@example.com",
        "collection_name": "网页检索导入",
    }
    try:
        user = json.load(open(CONFIG_PATH, encoding="utf-8"))
        cfg.update(user)
    except Exception:
        pass
    return cfg


CFG = load_config()

# 访问令牌：云端部署时设 ACCESS_TOKEN 环境变量（Secret），
# 则 /api/import 必须携带正确令牌，防止公网任意写入你的 Zotero 库。
# 本地未设置时视为关闭鉴权（不影响现有用法）。
ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()


def _is_cloud():
    """检测是否在云托管环境（HF Spaces / Render 等）运行。"""
    return bool(os.environ.get("SPACE_ID") or os.environ.get("RENDER"))


def _auth_ok(headers, payload):
    """鉴权：用户自带 Zotero 凭证（多用户模式）→ 放行；
    否则若设了 ACCESS_TOKEN → 必须携带正确令牌（管理员模式）。"""
    zid = (payload.get("zotero_user_id") or "").strip()
    zkey = (payload.get("zotero_api_key") or "").strip()
    if zid and zkey:
        return True
    if not ACCESS_TOKEN:
        return True
    h = headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[len("Bearer "):].strip() == ACCESS_TOKEN
    return (payload.get("token") or "").strip() == ACCESS_TOKEN


# 多用户导入锁：同一时刻只允许一个导入（避免 zff.USER_ID/API_KEY 并发覆盖）
_import_lock = threading.Lock()


def test_zotero_connection(user_id, api_key):
    """测试 Zotero 凭证是否有效。返回 (ok, detail)。"""
    try:
        req = urllib.request.Request(
            f"{zff.WEB_API}/users/{user_id}/items/top?limit=1",
            headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"})
        with _HTTP_OPENER.open(req, timeout=15) as r:
            if r.status == 200:
                return True, "连接成功"
            return False, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:120]}"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- 网络小工具
# 自动探测用的候选代理端口默认值（覆盖常见代理软件）；可在 web_search_config.json 的
# "proxy_candidates" 字段覆盖/增删，无需改代码。
_DEFAULT_PROXY_CANDIDATES = [
    "http://127.0.0.1:7897",   # Clash / 同类
    "http://127.0.0.1:7890",
    "http://127.0.0.1:7898",
    "http://127.0.0.1:10808",  # V2RayN
    "http://127.0.0.1:10809",
    "http://127.0.0.1:8080",   # 系统/HTTP 代理
    "http://127.0.0.1:8888",   # Charles / Fiddler
    "http://127.0.0.1:8118",   # Privoxy
    "http://127.0.0.1:33210",
]
_PROXY_TEST_URL = "https://api.crossref.org/works?rows=0"
# 当前生效的代理（供日志/热重载显示）
_CURRENT_PROXY = ""


def _proxy_is_auto():
    """配置为 auto/空 且允许使用代理时，走自动探测。云端直连、跳过代理扫描。"""
    if _is_cloud():
        return False
    if not CFG.get("use_proxy", True):
        return False
    p = (CFG.get("proxy") or "").strip().lower()
    return p in ("", "auto")


def _detect_proxy():
    """自动探测可用代理：环境变量 → 常见端口连通性测试 → 直连兜底。"""
    # 1) 环境变量优先（代理软件常写入 HTTPS_PROXY / HTTP_PROXY）
    envp = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    if envp:
        return envp
    # 2) 扫描常见本地代理端口，第一个能连通外网的即用
    candidates = CFG.get("proxy_candidates") or _DEFAULT_PROXY_CANDIDATES
    for cand in candidates:
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": cand, "https": cand}))
            req = urllib.request.Request(
                _PROXY_TEST_URL, headers={"User-Agent": "ZoteroWebSearch/1.0"})
            with opener.open(req, timeout=2.0) as r:
                if r.status == 200:
                    return cand
        except Exception:
            continue
    # 3) 直连兜底（无需代理的网络环境）
    return ""


def _resolve_proxy():
    """返回当前应使用的代理地址。use_proxy=false 或显式地址时直接返回。"""
    if not CFG.get("use_proxy", True):
        return ""
    p = (CFG.get("proxy") or "").strip()
    if not p or p.lower() == "auto":
        return _detect_proxy()
    return p


def _make_http_opener():
    proxy = _resolve_proxy()
    global _CURRENT_PROXY
    _CURRENT_PROXY = proxy
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


_HTTP_OPENER = _make_http_opener()


def _http_json(url, params=None, timeout=45, headers=None):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers=headers or {"User-Agent": "ZoteroWebSearch/1.0"})
    last = None
    last_err = None
    # 显式使用代理 opener（配置或环境变量），绕过 zff._robust_urlopen 的直连分支，
    # 因为本环境直连 Crossref/OpenAlex 会 502，而经代理可达。自带 4 次重试。
    # auto 模式下，若遇到连接错误（代理中途变化），自动重探测并重建 opener 后再试。
    for i in range(4):
        if i > 0 and _proxy_is_auto() and isinstance(last_err, urllib.error.URLError):
            global _HTTP_OPENER
            _HTTP_OPENER = _make_http_opener()
        try:
            with _HTTP_OPENER.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600:
                last = f"HTTP {e.code}"
                last_err = None
                time.sleep(1.5 + i)
                continue
            return {"_error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:160]}"}
        except urllib.error.URLError as e:
            last = str(e)[:220]
            last_err = e
            time.sleep(1.5 + i)
            continue
        except Exception as e:
            last = str(e)[:220]
            last_err = None
            time.sleep(1.5 + i)
    return {"_error": last or "request failed"}


# ---------------------------------------------------------------- 记录规范化
def _strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_authors(items, mode):
    out = []
    try:
        if mode == "crossref":
            for a in items or []:
                name = " ".join([a.get("given", ""), a.get("family", "")]).strip()
                if name:
                    out.append(name)
        elif mode == "europepmc":
            for a in (items or {}).get("author", []):
                if a.get("fullName"):
                    out.append(a["fullName"])
        elif mode == "openalex":
            for a in items or []:
                n = (a.get("author") or {}).get("display_name")
                if n:
                    out.append(n)
    except Exception:
        pass
    return out[:50]


def _oa_abstract(inv):
    if not inv:
        return ""
    try:
        pos = [(int(p), w) for w, ps in inv.items() for p in ps]
        if not pos:
            return ""
        maxp = max(p for p, _ in pos)
        arr = [""] * (maxp + 1)
        for p, w in pos:
            arr[p] = w
        return " ".join(arr).strip()
    except Exception:
        return ""


def _norm_type(t):
    if not t:
        return ""
    t = str(t).lower()
    t = t.replace("journal article", "article").replace("journal-article", "article")
    return t


# ---------------------------------------------------------------- 各数据源检索
def search_pubmed(query, limit):
    api_key = os.environ.get("NCBI_API_KEY", "")
    pmids, total = zuf.pubmed_search(query, retmax=max(int(limit), 1), api_key=api_key)
    recs = zuf.pubmed_fetch(pmids, api_key=api_key) if pmids else {}
    out = []
    for pmid in pmids:
        r = recs.get(pmid)
        if not r:
            continue
        r["source"] = "pubmed"
        r["uid"] = f"pmid:{pmid}"
        r.setdefault("has_fulltext", bool(r.get("pmcid")))
        out.append(r)
    return out, total


def search_crossref(query, limit, filters):
    params = {"query": query, "rows": str(limit), "select": ",".join([
        "title", "author", "container-title", "issued", "DOI", "abstract",
        "type", "link"])}
    f = []
    if filters.get("year_from"):
        f.append(f"from-pub-date:{filters['year_from']}-01-01")
    if filters.get("year_to"):
        f.append(f"to-pub-date:{filters['year_to']}-12-31")
    if filters.get("type") == "article":
        f.append("type:journal-article")
    elif filters.get("type"):
        f.append(f"type:{filters['type']}")
    if filters.get("lang"):
        f.append(f"lang:{filters['lang']}")
    if f:
        params["filter"] = ",".join(f)
    j = _http_json("https://api.crossref.org/works", params, timeout=45)
    if j.get("_error"):
        return [], 0, j["_error"]
    items = (j.get("message") or {}).get("items", [])
    total = (j.get("message") or {}).get("total-results", len(items))
    out = []
    for it in items:
        title = (it.get("title") or [""])[0] if it.get("title") else ""
        if not title:
            continue
        doi = it.get("DOI", "")
        # PDF 直链
        pdf_url = None
        for ln in it.get("link", []) or []:
            if "pdf" in (ln.get("content-type") or "").lower():
                pdf_url = ln.get("URL")
                break
        year = ""
        try:
            year = it["issued"]["date-parts"][0][0]
        except Exception:
            pass
        out.append({
            "source": "crossref", "uid": f"doi:{doi}" if doi else f"cr:{title[:40]}",
            "title": title,
            "authors": _norm_authors(it.get("author"), "crossref"),
            "journal": (it.get("container-title") or [""])[0] if it.get("container-title") else "",
            "year": str(year) if year else "",
            "doi": doi, "pmid": it.get("PMID"), "pmcid": "",
            "abstract": _strip_tags(it.get("abstract", "")),
            "type": _norm_type(it.get("type")),
            "language": it.get("language", ""),
            "has_fulltext": bool(pdf_url),
            "pdf_url": pdf_url,
        })
    return out, total, None


def search_europepmc(query, limit, filters):
    params = {"query": query, "format": "json", "pageSize": str(limit),
              "resultType": "core"}
    j = _http_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                   params, timeout=45)
    if j.get("_error"):
        return [], 0, j["_error"]
    results = j.get("resultList", {}).get("result", [])
    total = int(j.get("hitCount", len(results)) or len(results))
    out = []
    for r in results:
        title = r.get("title", "")
        if not title:
            continue
        doi = r.get("doi", "")
        out.append({
            "source": "europepmc", "uid": f"pmid:{r.get('pmid')}" if r.get("pmid") else f"doi:{doi}",
            "title": title,
            "authors": _norm_authors(r.get("authorList"), "europepmc"),
            "journal": (((r.get("journalInfo") or {}).get("journal") or {}).get("title", "")),
            "year": str((r.get("journalInfo") or {}).get("yearOfPublication", "")),
            "doi": doi, "pmid": r.get("pmid"), "pmcid": r.get("pmcid"),
            "abstract": _strip_tags(r.get("abstractText", "")),
            "type": _norm_type((r.get("pubTypeList") or {}).get("pubType", [""])[0]
                               if r.get("pubTypeList") else r.get("docType")),
            "language": r.get("language", ""),
            "has_fulltext": (str(r.get("hasPDF")) == "Y"
                             or str(r.get("hasTextMinedTerms")) == "Y"
                             or bool(r.get("pmcid"))),
            "pdf_url": None,
        })
    return out, total, None


def search_openalex(query, limit, filters):
    params = {"search": query, "per-page": str(limit),
              "mailto": CFG.get("openalex_mailto", "research@example.com")}
    fl = []
    if filters.get("year_from"):
        fl.append(f"from_publication_date:{filters['year_from']}-01-01")
    if filters.get("year_to"):
        fl.append(f"to_publication_date:{filters['year_to']}-12-31")
    if filters.get("type") == "article":
        fl.append("type:article")
    elif filters.get("type") == "review":
        fl.append("type:review")
    elif filters.get("type"):
        fl.append(f"type:{filters['type']}")
    if filters.get("lang"):
        fl.append(f"language:{filters['lang']}")
    if fl:
        params["filter"] = ",".join(fl)
    j = _http_json("https://api.openalex.org/works", params, timeout=45)
    if j.get("_error"):
        return [], 0, j["_error"]
    items = j.get("results", [])
    total = j.get("meta", {}).get("count", len(items))
    out = []
    for w in items:
        title = w.get("display_name") or w.get("title", "")
        if not title:
            continue
        doi = w.get("doi", "")
        oa = w.get("open_access", {}) or {}
        pdf_url = oa.get("oa_url") if oa.get("oa_status") == "open" else None
        src = ((w.get("primary_location") or {}).get("source") or {}) or {}
        out.append({
            "source": "openalex", "uid": f"doi:{doi}" if doi else f"oa:{w.get('id','')}",
            "title": title,
            "authors": _norm_authors(w.get("authorships"), "openalex"),
            "journal": src.get("display_name", ""),
            "year": str(w.get("publication_year", "")),
            "doi": doi, "pmid": (w.get("ids") or {}).get("pmid"),
            "pmcid": "",
            "abstract": _oa_abstract(w.get("abstract_inverted_index")),
            "type": _norm_type(w.get("type")),
            "language": "",
            "has_fulltext": bool(pdf_url) or oa.get("oa_status") == "open",
            "pdf_url": pdf_url,
        })
    return out, total, None


# ---------------------------------------------------------------- 筛选 + 去重
def apply_filters(records, filters):
    yf = filters.get("year_from")
    yt = filters.get("year_to")
    ft = _norm_type(filters.get("type")) if filters.get("type") else ""
    fl = (filters.get("lang") or "").lower().strip()
    out = []
    seen = set()
    for r in records:
        if r["uid"] in seen:
            continue
        seen.add(r["uid"])
        y = r.get("year")
        if y:
            try:
                y = int(y)
                if yf and y < int(yf):
                    continue
                if yt and y > int(yt):
                    continue
            except Exception:
                pass
        if ft and ft not in _norm_type(r.get("type")):
            continue
        if fl and not (r.get("language") or "").lower().startswith(fl):
            continue
        out.append(r)
    return out


def search_all(query, source, limit, filters):
    """统一检索入口。返回 (records, total, error)。"""
    source = (source or "pubmed").lower()
    limit = max(1, min(int(limit or CFG["default_limit"]), int(CFG["max_limit"])))
    try:
        if source == "pubmed":
            recs, total = search_pubmed(query, limit)
            return apply_filters(recs, filters), total, None
        if source == "crossref":
            recs, total, err = search_crossref(query, limit, filters)
            if err:
                return [], 0, err
            return apply_filters(recs, filters), total, None
        if source == "europepmc":
            recs, total, err = search_europepmc(query, limit, filters)
            if err:
                return [], 0, err
            return apply_filters(recs, filters), total, None
        if source == "openalex":
            recs, total, err = search_openalex(query, limit, filters)
            if err:
                return [], 0, err
            return apply_filters(recs, filters), total, None
    except Exception as e:
        return [], 0, f"检索异常: {e}"
    return [], 0, f"未知数据源: {source}"


# ---------------------------------------------------------------- 导入 Zotero
def _att_filename(fetch):
    kind = fetch.get("kind")
    if kind == "pdf":
        return "Full Text.pdf"
    if kind == "abstract":
        return "Abstract.pdf"
    if kind == "pptx":
        return "Summary.pptx"
    if kind == "markdown":
        return "Full Text.md"
    return os.path.basename(fetch.get("path", "")) or "attachment.pdf"


def import_items(items, zotero_user_id=None, zotero_api_key=None):
    """批量导入选中文献到 Zotero。items: [{source, rec}]。
    多用户模式：zotero_user_id/zotero_api_key 由前端传入，临时覆盖 zff 模块变量。
    加锁防并发：同一时刻只跑一个导入（凭证是模块级变量）。"""
    with _import_lock:
        # 备份并覆盖 zff 凭证
        orig_uid, orig_key = zff.USER_ID, zff.API_KEY
        if zotero_user_id and zotero_api_key:
            zff.USER_ID = zotero_user_id.strip()
            zff.API_KEY = zotero_api_key.strip()
        try:
            return _import_items_inner(items)
        finally:
            zff.USER_ID = orig_uid
            zff.API_KEY = orig_key


def _import_items_inner(items):
    """import_items 的实际逻辑（凭证已设好）。"""
    cfg = zuf.load_config()
    cfg["ingest"]["tag_with_keyword"] = False
    coll_key = None
    try:
        if CFG.get("collection_name"):
            coll_key = zuf.ensure_collection(CFG["collection_name"])
    except Exception:
        coll_key = None

    results = []
    for it in items:
        rec = it.get("rec", {})
        src = it.get("source", "web")
        rec["source"] = src
        # build_item_payload 直接访问 rec['subscores']['fulltext']，必须给足字段
        rec.setdefault("score", 0)
        rec["subscores"] = {
            "fulltext": 1 if rec.get("has_fulltext") else 0,
            "if": rec.get("impact_factor") or 0,
            "cites": rec.get("citations") or 0,
            "rel": rec.get("relevance") or 0,
        }
        rec.setdefault("mesh", rec.get("mesh", []))
        cfg["ingest"]["extra_tags"] = ["web-search", f"src:{src}"]
        try:
            ok, key = zuf.ingest_item(rec, "", cfg, collection_key=coll_key)
        except Exception as e:
            results.append({"title": rec.get("title"), "status": "error",
                            "msg": f"入库失败: {e}"})
            continue
        if not ok:
            results.append({"title": rec.get("title"), "status": "error", "msg": str(key)})
            continue
        attached = None
        if CFG.get("attach_pdf"):
            meta = {"title": rec.get("title"), "doi": rec.get("doi"),
                    "pmid": rec.get("pmid"), "pmcid": rec.get("pmcid"),
                    "journal": rec.get("journal"),
                    "abstract": rec.get("abstract") or "",
                    "authors": rec.get("authors") or [],
                    "year": rec.get("year") or ""}
            try:
                fetch = zff.fetch_fulltext(meta)
                if fetch.get("ok") and fetch.get("path"):
                    fn = _att_filename(fetch)
                    aok, adetail = zff._zotero_upload_file(
                        key, fetch["path"], fn, zff.USER_ID, zff.API_KEY)
                    if aok:
                        attached = fn
                    else:
                        attached = f"获取全文但未挂上: {adetail[:80]}"
                else:
                    attached = fetch.get("note", "无可用全文")
            except Exception as e:
                attached = f"全文获取异常: {e}"
        results.append({"title": rec.get("title"), "status": "ok",
                        "key": key, "attached": attached})
    return results


# ---------------------------------------------------------------- 历史
def load_history():
    try:
        return json.load(open(HISTORY_PATH, encoding="utf-8"))
    except Exception:
        return []


# ---------------------------------------------------------------- 翻译（MyMemory 免费接口，无 API key）
# 与检索/导入解耦的独立通道：前端 POST /api/translate {text, target} 拿到中文。
# 云端 Render 直连 MyMemory；本地若代理可用则自动走代理。
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_TRANSLATE_MAX_CHARS = 500   # MyMemory 匿名 IP 每日 5000 字配额，单次截断保护
_TRANSLATE_TIMEOUT = 20      # 秒


def _translate_text(text, target="zh-CN"):
    """调用 MyMemory 翻译单段文本。返回 (translated_text, error_or_None)。"""
    if not text or not text.strip():
        return ("", "empty_text")
    txt = text.strip()
    if len(txt) > _TRANSLATE_MAX_CHARS:
        txt = txt[:_TRANSLATE_MAX_CHARS]
    params = urllib.parse.urlencode({"q": txt, "langpair": f"en|{target}"})
    url = f"{_MYMEMORY_URL}?{params}"
    # 直接用 urllib：云端走直连（_is_cloud() 已返回 False 会让代理逻辑跳过自动探测），
    # 本地有代理时 _http_json 走代理；这里用普通 urlopen 不绕弯。
    req = urllib.request.Request(url, headers={"User-Agent": "ZoteroWebSearch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TRANSLATE_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
        data = json.loads(body)
        if str(data.get("responseStatus")) == "200":
            translated = (data.get("responseData") or {}).get("translatedText", "")
            return (translated or "", None)
        # MyMemory 用 403/429 表示限流；返回 details 便于前端提示
        return ("", f"api_status={data.get('responseStatus')}: {data.get('responseDetails')}")
    except Exception as e:
        return ("", f"network_error: {e.__class__.__name__}: {str(e)[:120]}")


def save_history_entry(entry):
    h = load_history()
    h.insert(0, entry)
    h = h[:HISTORY_MAX]
    try:
        json.dump(h, open(HISTORY_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已断开（导入耗时长，浏览器超时），忽略写错误

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                html = open(os.path.join(HERE, "web_search.html"),
                            encoding="utf-8").read()
            except Exception as e:
                self._send(500, f"前端文件缺失: {e}")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if self.path == "/api/sources":
            self._send(200, json.dumps(
                {"sources": [{"id": s, "label": SOURCE_LABELS.get(s, s)}
                             for s in CFG["sources"]],
                 "default_limit": CFG["default_limit"],
                 "attach_pdf": CFG["attach_pdf"],
                 "zotero_ready": bool(zff.USER_ID and zff.USER_ID != "0" and zff.API_KEY),
                 "multi_user": True}))
            return
        if self.path == "/api/history":
            self._send(200, json.dumps(load_history(), ensure_ascii=False))
            return
        # GET /api/translate?text=...&target=zh-CN —— 便捷的翻译查询（避免前端构造 POST body）
        if self.path.startswith("/api/translate"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            text = (qs.get("text", [""])[0] or "").strip()
            target = (qs.get("target", ["zh-CN"])[0] or "zh-CN").strip()
            if not text:
                self._send(400, json.dumps({"error": "text 不能为空"}))
                return
            translated, err = _translate_text(text, target=target)
            if err:
                self._send(200, json.dumps(
                    {"translated": "", "error": err, "target": target},
                    ensure_ascii=False))
                return
            self._send(200, json.dumps(
                {"translated": translated, "error": None, "target": target},
                ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if self.path == "/api/search":
            query = (payload.get("query") or "").strip()
            if not query:
                self._send(400, json.dumps({"error": "关键词不能为空"}))
                return
            source = payload.get("source", "pubmed")
            limit = payload.get("limit", CFG["default_limit"])
            filters = payload.get("filters", {})
            # 标准化筛选字段
            f = {}
            for k in ("year_from", "year_to", "type", "lang"):
                if payload.get("filters", {}).get(k):
                    f[k] = payload["filters"][k]
            recs, total, err = search_all(query, source, limit, f)
            if err:
                self._send(502, json.dumps({"error": err}))
                return
            # 记录历史
            save_history_entry({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "query": query, "source": source,
                "filters": f, "count": len(recs), "total": total,
            })
            self._send(200, json.dumps(
                {"total": total, "count": len(recs), "results": recs},
                ensure_ascii=False))
            return

        if self.path == "/api/import":
            if not _auth_ok(self.headers, payload):
                self._send(401, json.dumps(
                    {"error": "需要 Zotero 凭证或访问令牌"}))
                return
            items = payload.get("items", [])
            if not items:
                self._send(400, json.dumps({"error": "没有选中任何文献"}))
                return
            zotero_user_id = (payload.get("zotero_user_id") or "").strip()
            zotero_api_key = (payload.get("zotero_api_key") or "").strip()
            # 多用户模式：必须提供凭证（除非服务端配了 env 变量）
            if not zotero_user_id and not (zff.USER_ID and zff.USER_ID != "0"):
                self._send(400, json.dumps(
                    {"error": "请在下方填写 Zotero User ID 和 API Key"}))
                return
            results = import_items(items, zotero_user_id=zotero_user_id,
                                   zotero_api_key=zotero_api_key)
            self._send(200, json.dumps({"results": results}, ensure_ascii=False))
            return

        if self.path == "/api/zotero-test":
            zid = (payload.get("zotero_user_id") or "").strip()
            zkey = (payload.get("zotero_api_key") or "").strip()
            if not zid or not zkey:
                self._send(400, json.dumps({"ok": False, "msg": "请填写 User ID 和 API Key"}))
                return
            ok, detail = test_zotero_connection(zid, zkey)
            self._send(200, json.dumps({"ok": ok, "msg": detail}, ensure_ascii=False))
            return

        if self.path == "/api/translate":
            # 翻译通道（独立于检索/导入，不需要 ACCESS_TOKEN；纯 GET 也支持便于直接调用）
            text = (payload.get("text") or "").strip()
            target = (payload.get("target") or "zh-CN").strip() or "zh-CN"
            if not text:
                self._send(400, json.dumps({"error": "text 不能为空"}))
                return
            translated, err = _translate_text(text, target=target)
            if err:
                # 翻译失败不视为致命错误，前端可显示"翻译暂不可用"
                self._send(200, json.dumps(
                    {"translated": "", "error": err, "target": target},
                    ensure_ascii=False))
                return
            self._send(200, json.dumps(
                {"translated": translated, "error": None, "target": target},
                ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
    else:
        # 云端（HF Spaces / Render）默认 7860；本地保持 8777
        port = 7860 if _is_cloud() else int(CFG.get("port", DEFAULT_PORT))
    host = os.environ.get("HOST")
    if not host:
        # 云环境（HF Spaces 等）自动绑定 0.0.0.0；本地默认 127.0.0.1
        host = "0.0.0.0" if _is_cloud() else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Zotero 网页文献检索工具已启动： http://{host}:{port}/")
    print(f"  数据源: {', '.join(CFG['sources'])}")
    print(f"  Zotero API: {'已配置' if (zff.USER_ID and zff.USER_ID!='0' and zff.API_KEY) else '未配置（导入将失败，请检查 zotero_config.json）'}")
    print(f"  代理: {_CURRENT_PROXY or '直连（无代理 / 自动探测未命中）'}")
    print(f"  导入鉴权: 多用户模式（用户自带 Zotero 凭证）" +
          (f" + 管理员令牌已设" if ACCESS_TOKEN else ""))
    print("  按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
