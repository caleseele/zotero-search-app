#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zotero_upstream_fetch.py
========================
上游文献自动获取与入库：关键词 -> PubMed 检索 -> 质量筛选 -> 去重 -> 写入 Zotero 库。

与现有程序的分工（本文件不修改任何已有代码，只新增上游环节）：
    [本文件]  关键词 -> PubMed 检索 -> 质量打分排序 -> 去重 -> 建条目入库
        ↓（新条目落到 Zotero 库）
    [zotero_auto_fetch.py]  轮询检测新条目 -> 多源抓全文 -> 挂 PDF / 结构化 PPT 摘要

工作流：
  1. KEYWORDS  读取每日关键词列表（配置文件 / --keywords / --keywords-file，支持 PubMed 布尔语法）
  2. SEARCH    PubMed E-utilities esearch + efetch，取元数据（标题/摘要/作者/期刊/日期/DOI/PMID/MeSH）
  3. ENRICH    OpenAlex 批量补引用数、期刊 2yr 均被引（IF 代理）、OA 状态；Europe PMC 补 PMCID/全文可得性
  4. SCORE     四维加权打分：全文可得 > 期刊影响因子 > 被引次数 > 关键词相关度
  5. DEDUP     与 Zotero 本地库（DOI/PMID/标题）+ 本模块历史入库记录双重去重
  6. INGEST    Web API 建 journalArticle 顶层条目（Zotero 开着也能写），打关键词标签
  7. LOG       逐条记录命中/入库/跳过原因 -> upstream_fetch.log + logs/upstream_YYYYMMDD.json

用法：
  # 用配置里的关键词跑一轮（正式入库）
  python zotero_upstream_fetch.py --run

  # 临时指定关键词（覆盖配置）
  python zotero_upstream_fetch.py --run --keywords "platelet cytoskeleton" "megakaryocyte actin"

  # 从文件读关键词（每行一个，# 开头为注释）
  python zotero_upstream_fetch.py --run --keywords-file keywords.txt

  # 只检索打分、不入库（体检模式，强烈建议先跑这个）
  python zotero_upstream_fetch.py --run --dry-run

  # 查看今日额度与历史入库统计
  python zotero_upstream_fetch.py --status

说明：
  - 只调用公开学术 API（NCBI E-utilities / OpenAlex / Europe PMC），不碰盗版源。
  - 入库只「新增条目」，绝不修改或删除库里已有文献。
  - 全部阈值、权重、每日上限都在 upstream_config.json 中可配。
"""
import sys, os, re, json, time, math, argparse, sqlite3
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zotero_fulltext_fetch as zff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "upstream_config.json")
IF_TABLE_PATH = os.path.join(BASE_DIR, "impact_factors.json")
STATE_PATH = os.path.join(BASE_DIR, ".zuf_state.json")
LOG_PATH = os.path.join(BASE_DIR, "upstream_fetch.log")
REPORT_DIR = os.path.join(BASE_DIR, "logs")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX = "https://api.openalex.org"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

DEFAULT_CONFIG = {
    "keywords": [],
    "search": {"per_keyword_retmax": 30, "recent_days": 1825,
               "sort": "relevance", "ncbi_api_key": ""},
    "filter": {"daily_max_ingest": 10, "min_impact_factor": 0.0, "min_citations": 0,
               "min_relevance": 0.15, "min_total_score": 0.30,
               "require_fulltext": False, "exclude_no_abstract": True},
    "weights": {"fulltext": 0.35, "impact_factor": 0.25, "citations": 0.20,
                "relevance": 0.20, "if_cap": 20.0, "citation_cap": 500},
    "ingest": {"enabled": True, "collection_key": "", "extra_tags": ["auto-upstream"],
               "tag_with_keyword": True, "per_keyword_collections": True,
               "parent_collection_name": "上游检索"},
}

# PubMed 检索语法里的保留词/字段标签，计算关键词相关度时要剔除
_STOP = {"and", "or", "not", "the", "of", "in", "on", "for", "with", "a", "an",
         "to", "by", "from", "at", "is", "are", "as"}
_FIELD_TAG = re.compile(r"\[[^\]]+\]")


# ---------------------------------------------------------------- 配置 / 日志 / 状态
def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            zff.log(f"  [warn] 配置解析失败，使用默认值: {e}")
    return _deep_merge(DEFAULT_CONFIG, {k: v for k, v in cfg.items() if not k.startswith("_")})


def load_if_table():
    try:
        with open(IF_TABLE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {k.lower(): float(v) for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, (int, float))}
    except Exception:
        return {}


def flog(msg=""):
    """同时输出到控制台与 upstream_fetch.log（追加，永不覆盖历史）。"""
    zff.log(msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
    else:
        s = {}
    s.setdefault("ingested", {})       # pmid -> {doi,title,key,date,score,keyword}
    s.setdefault("seen_pmids", [])     # 检索过但被筛掉的，避免重复评估刷屏
    s.setdefault("daily", {})          # YYYY-MM-DD -> 入库数
    s.setdefault("runs", 0)
    return s


def save_state(s):
    """原子写入（先写 .tmp 再 rename），避免中途崩溃丢状态。"""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 1. KEYWORDS
def resolve_keywords(cfg, cli_keywords=None, keywords_file=None):
    if cli_keywords:
        return [k.strip() for k in cli_keywords if k.strip()]
    if keywords_file:
        path = keywords_file if os.path.isabs(keywords_file) else os.path.join(BASE_DIR, keywords_file)
        if not os.path.exists(path):
            flog(f"  [warn] 关键词文件不存在: {path}")
            return []
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    return [k.strip() for k in cfg.get("keywords", []) if k.strip()]


# ---------------------------------------------------------------- 2. SEARCH（PubMed）
def _eutils_get(endpoint, params, api_key="", timeout=45):
    if api_key:
        params = dict(params, api_key=api_key)
    url = f"{EUTILS}/{endpoint}?" + urllib.parse.urlencode(params)
    res = zff.http_get(url, timeout=timeout)
    # 无 api_key 时 NCBI 限 3 req/s，主动节流避免 429
    time.sleep(0.12 if api_key else 0.36)
    return res


def pubmed_search(keyword, retmax=30, recent_days=0, sort="relevance", api_key=""):
    """esearch：返回 PMID 列表（保留 PubMed 的排序次序，用于相关度加成）。"""
    params = {"db": "pubmed", "term": keyword, "retmax": str(retmax),
              "retmode": "json", "sort": sort}
    if recent_days and int(recent_days) > 0:
        params["datetype"] = "pdat"
        params["mindate"] = (datetime.now() - timedelta(days=int(recent_days))).strftime("%Y/%m/%d")
        params["maxdate"] = datetime.now().strftime("%Y/%m/%d")
    res = _eutils_get("esearch.fcgi", params, api_key)
    if not res.get("ok"):
        flog(f"  [warn] PubMed 检索失败 ({keyword}): {res.get('error') or res.get('code')}")
        return [], 0
    try:
        j = json.loads(res["data"])
        er = j.get("esearchresult", {})
        return er.get("idlist", []), int(er.get("count", 0) or 0)
    except Exception as e:
        flog(f"  [warn] PubMed 结果解析失败 ({keyword}): {e}")
        return [], 0


def _text(node):
    return "".join(node.itertext()).strip() if node is not None else ""


def _parse_article(art):
    """从 PubmedArticle XML 节点解析元数据。"""
    rec = {"pmid": None, "doi": None, "title": None, "abstract": None,
           "journal": None, "year": None, "pubdate": None, "authors": [],
           "mesh": [], "pubtypes": []}
    pm = art.find(".//MedlineCitation/PMID")
    rec["pmid"] = _text(pm) or None

    for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            rec["doi"] = (_text(aid) or "").lower() or None
        elif aid.get("IdType") == "pmc":
            rec["pmcid"] = _text(aid) or None
    if not rec.get("doi"):
        for eid in art.findall(".//Article/ELocationID"):
            if eid.get("EIdType") == "doi":
                rec["doi"] = (_text(eid) or "").lower() or None

    rec["title"] = _text(art.find(".//Article/ArticleTitle")) or None

    # 结构化摘要：拼接 Label + 正文
    parts = []
    for ab in art.findall(".//Article/Abstract/AbstractText"):
        label = ab.get("Label") or ab.get("NlmCategory")
        body = _text(ab)
        if not body:
            continue
        parts.append(f"{label.strip().upper()}: {body}" if label else body)
    rec["abstract"] = "\n".join(parts) or None

    rec["journal"] = (_text(art.find(".//Article/Journal/Title"))
                      or _text(art.find(".//Article/Journal/ISOAbbreviation")) or None)

    y = _text(art.find(".//Article/Journal/JournalIssue/PubDate/Year"))
    if not y:
        md = _text(art.find(".//Article/Journal/JournalIssue/PubDate/MedlineDate"))
        m = re.search(r"(19|20)\d{2}", md or "")
        y = m.group(0) if m else ""
    rec["year"] = int(y) if y.isdigit() else None
    mon = _text(art.find(".//Article/Journal/JournalIssue/PubDate/Month"))
    day = _text(art.find(".//Article/Journal/JournalIssue/PubDate/Day"))
    rec["pubdate"] = "-".join([p for p in (y, mon, day) if p]) or None

    for a in art.findall(".//Article/AuthorList/Author"):
        last, fore = _text(a.find("LastName")), _text(a.find("ForeName"))
        name = (fore + " " + last).strip() or _text(a.find("CollectiveName"))
        if name:
            rec["authors"].append(name)

    rec["mesh"] = [_text(d) for d in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if _text(d)]
    rec["pubtypes"] = [_text(p) for p in art.findall(".//Article/PublicationTypeList/PublicationType") if _text(p)]
    return rec


def pubmed_fetch(pmids, api_key="", batch=50):
    """efetch：批量取完整元数据。返回 {pmid: rec}。"""
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        res = _eutils_get("efetch.fcgi", {"db": "pubmed", "id": ",".join(chunk),
                                          "retmode": "xml"}, api_key, timeout=60)
        if not res.get("ok"):
            flog(f"  [warn] PubMed 元数据抓取失败（{len(chunk)} 条）: {res.get('error') or res.get('code')}")
            continue
        try:
            root = ET.fromstring(res["data"])
        except Exception as e:
            flog(f"  [warn] PubMed XML 解析失败: {e}")
            continue
        for art in root.findall(".//PubmedArticle"):
            rec = _parse_article(art)
            if rec.get("pmid"):
                out[rec["pmid"]] = rec
    return out


# ---------------------------------------------------------------- 3. ENRICH
_SOURCE_CACHE = {}


def _openalex_source_if(source_id):
    """OpenAlex 期刊 2yr_mean_citedness —— 无 JCR 表时的影响因子代理指标。"""
    if not source_id:
        return None
    sid = source_id.rstrip("/").split("/")[-1]
    if sid in _SOURCE_CACHE:
        return _SOURCE_CACHE[sid]
    val = None
    res = zff.http_get(f"{OPENALEX}/sources/{sid}", timeout=30)
    if res.get("ok"):
        try:
            val = json.loads(res["data"]).get("summary_stats", {}).get("2yr_mean_citedness")
        except Exception:
            val = None
    _SOURCE_CACHE[sid] = val
    return val


def _apply_openalex_work(rec, w):
    rec["citations"] = int(w.get("cited_by_count") or 0)
    oa = w.get("open_access") or {}
    if oa.get("is_oa"):
        rec["is_oa"] = True
        rec["oa_url"] = oa.get("oa_url") or rec.get("oa_url")
    src = ((w.get("primary_location") or {}).get("source") or {})
    rec["openalex_source_id"] = src.get("id")
    if not rec.get("journal") and src.get("display_name"):
        rec["journal"] = src["display_name"]


def openalex_enrich(records):
    """批量补引用数 / OA 状态 / 期刊指标。优先按 DOI 批量查（省请求），无 DOI 的按 PMID 单查。"""
    by_doi = {r["doi"]: r for r in records if r.get("doi")}
    dois = list(by_doi.keys())
    for i in range(0, len(dois), 25):
        chunk = dois[i:i + 25]
        flt = "doi:" + "|".join(chunk)
        url = f"{OPENALEX}/works?filter={urllib.parse.quote(flt, safe=':|/.')}&per-page=50"
        res = zff.http_get(url, timeout=45)
        if not res.get("ok"):
            continue
        try:
            for w in json.loads(res["data"]).get("results", []):
                doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                if doi in by_doi:
                    _apply_openalex_work(by_doi[doi], w)
        except Exception as e:
            flog(f"  [warn] OpenAlex 批量解析失败: {e}")
        time.sleep(0.2)

    for r in records:
        if "citations" in r or not r.get("pmid"):
            continue
        res = zff.http_get(f"{OPENALEX}/works/pmid:{r['pmid']}", timeout=30)
        if res.get("ok"):
            try:
                _apply_openalex_work(r, json.loads(res["data"]))
            except Exception:
                pass
        time.sleep(0.2)

    for r in records:
        r.setdefault("citations", 0)
        r["journal_2yr"] = _openalex_source_if(r.get("openalex_source_id"))


def epmc_fulltext_status(rec):
    """Europe PMC 查 PMCID / 是否有可获取全文（比只看 OA 标志更贴近"真能拿到正文"）。"""
    q = f"DOI:{rec['doi']}" if rec.get("doi") else f"EXT_ID:{rec.get('pmid')} AND SRC:MED"
    url = f"{EPMC}/search?query={urllib.parse.quote(q)}&format=json&resultType=lite&pageSize=1"
    res = zff.http_get(url, timeout=30)
    if not res.get("ok"):
        return
    try:
        rs = json.loads(res["data"]).get("resultList", {}).get("result", [])
    except Exception:
        return
    if not rs:
        return
    r0 = rs[0]
    if r0.get("pmcid"):
        rec["pmcid"] = r0["pmcid"]
    if str(r0.get("isOpenAccess", "")).upper() == "Y":
        rec["is_oa"] = True
    if str(r0.get("inEPMC", "")).upper() == "Y" or str(r0.get("hasTextMinedTerms", "")).upper() == "Y":
        rec["in_epmc"] = True
    time.sleep(0.12)


def has_fulltext(rec):
    return bool(rec.get("pmcid") or rec.get("is_oa") or rec.get("in_epmc") or rec.get("oa_url"))


# ---------------------------------------------------------------- 4. SCORE
def _norm_journal(name):
    if not name:
        return ""
    s = name.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve_impact_factor(rec, if_table):
    """先查本地 JCR 参考表（精确名 -> 去 the/of 前缀的宽松匹配），查不到回退 OpenAlex 2yr 均被引。"""
    jn = _norm_journal(rec.get("journal"))
    if jn:
        if jn in if_table:
            return if_table[jn], "table"
        loose = re.sub(r"^(the|journal of the|journal of)\s+", "", jn)
        if loose in if_table:
            return if_table[loose], "table"
        for k, v in if_table.items():
            if k and (k in jn or jn in k) and abs(len(k) - len(jn)) <= 12:
                return v, "table~"
    j2 = rec.get("journal_2yr")
    if isinstance(j2, (int, float)) and j2 > 0:
        return float(j2), "openalex"
    return 0.0, "unknown"


def keyword_terms(keyword):
    kw = _FIELD_TAG.sub(" ", keyword or "").replace('"', " ")
    toks = re.split(r"[^A-Za-z0-9\-]+", kw.lower())
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def relevance_score(keyword, rec, rank=0, total=1):
    """关键词相关度 0~1：标题命中权重最高，其次 MeSH 主题词，再次摘要；叠加 PubMed 排序位次加成。"""
    terms = keyword_terms(keyword)
    if not terms:
        return 0.0
    title = (rec.get("title") or "").lower()
    abstract = (rec.get("abstract") or "").lower()
    mesh = " ".join(rec.get("mesh") or []).lower()
    hit = 0.0
    for t in terms:
        w = 0.0
        if t in title:
            w = 1.0
        elif t in mesh:
            w = 0.75
        elif t in abstract:
            w = 0.5
        hit += w
    coverage = hit / len(terms)
    rank_bonus = (1.0 - (rank / max(total, 1))) * 0.15   # PubMed 相关度排序位次
    return round(min(coverage * 0.85 + rank_bonus, 1.0), 4)


def score_record(rec, keyword, cfg, if_table, rank=0, total=1):
    w = cfg["weights"]
    ift, if_src = resolve_impact_factor(rec, if_table)
    rec["impact_factor"] = round(ift, 3)
    rec["if_source"] = if_src
    rec["fulltext"] = has_fulltext(rec)
    rec["relevance"] = relevance_score(keyword, rec, rank, total)

    s_ft = 1.0 if rec["fulltext"] else 0.0
    s_if = min(ift / float(w.get("if_cap") or 20.0), 1.0)
    cap = float(w.get("citation_cap") or 500)
    s_cite = math.log10(1 + rec.get("citations", 0)) / math.log10(1 + cap) if cap > 1 else 0.0
    s_cite = min(s_cite, 1.0)
    s_rel = rec["relevance"]

    total_score = (w["fulltext"] * s_ft + w["impact_factor"] * s_if
                   + w["citations"] * s_cite + w["relevance"] * s_rel)
    rec["subscores"] = {"fulltext": round(s_ft, 4), "impact_factor": round(s_if, 4),
                        "citations": round(s_cite, 4), "relevance": round(s_rel, 4)}
    rec["score"] = round(total_score, 4)
    return rec


def passes_filter(rec, cfg):
    """返回 (是否通过, 跳过原因)。"""
    f = cfg["filter"]
    if f.get("exclude_no_abstract") and not rec.get("abstract"):
        return False, "无摘要"
    if f.get("require_fulltext") and not rec.get("fulltext"):
        return False, "无可获取全文"
    if rec.get("impact_factor", 0) < float(f.get("min_impact_factor") or 0):
        return False, f"影响因子 {rec.get('impact_factor')} < 阈值 {f.get('min_impact_factor')}"
    if rec.get("citations", 0) < int(f.get("min_citations") or 0):
        return False, f"被引 {rec.get('citations')} < 阈值 {f.get('min_citations')}"
    if rec.get("relevance", 0) < float(f.get("min_relevance") or 0):
        return False, f"相关度 {rec.get('relevance')} < 阈值 {f.get('min_relevance')}"
    if rec.get("score", 0) < float(f.get("min_total_score") or 0):
        return False, f"综合分 {rec.get('score')} < 阈值 {f.get('min_total_score')}"
    return True, ""


# ---------------------------------------------------------------- 5. DEDUP
def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:120]


def library_index():
    """只读扫描本地 Zotero 库，建立 DOI / PMID / 标题 三套指纹用于去重（Zotero 开着也能读）。"""
    idx = {"dois": set(), "pmids": set(), "titles": set()}
    if not os.path.exists(zff.DB_PATH):
        return idx
    try:
        conn = sqlite3.connect(f"file:{zff.DB_PATH}?immutable=1", uri=True, timeout=5)
        cur = conn.cursor()
        att_t = cur.execute("SELECT itemTypeID FROM itemTypes WHERE typeName='attachment'").fetchone()[0]
        note_t = cur.execute("SELECT itemTypeID FROM itemTypes WHERE typeName='note'").fetchone()[0]
        rows = cur.execute(f"""
            SELECT f.fieldName, v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID=v.valueID
            JOIN fields f ON f.fieldID=d.fieldID
            JOIN items i ON i.itemID=d.itemID
            WHERE i.itemTypeID NOT IN ({att_t},{note_t})
              AND f.fieldName IN ('DOI','title','extra','url')
        """).fetchall()
        conn.close()
        for fname, val in rows:
            if not val:
                continue
            if fname == "DOI":
                idx["dois"].add(val.strip().lower())
            elif fname == "title":
                idx["titles"].add(_norm_title(val))
            else:
                for m in re.finditer(r"PMID:?\s*(\d{5,9})", val, re.I):
                    idx["pmids"].add(m.group(1))
                for m in re.finditer(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})", val, re.I):
                    idx["pmids"].add(m.group(1))
    except Exception as e:
        flog(f"  [warn] 读取本地库索引失败（去重降级为仅按历史记录）: {e}")
    return idx


def is_duplicate(rec, idx, state):
    if rec.get("pmid") and rec["pmid"] in state["ingested"]:
        return True, "本模块此前已入库"
    if rec.get("pmid") and rec["pmid"] in idx["pmids"]:
        return True, "库中已有相同 PMID"
    if rec.get("doi") and rec["doi"].strip().lower() in idx["dois"]:
        return True, "库中已有相同 DOI"
    if rec.get("title") and _norm_title(rec["title"]) in idx["titles"]:
        return True, "库中已有相同标题"
    return False, ""


# ---------------------------------------------------------------- 6. INGEST（Zotero Web API）
def _web_headers():
    return {"Zotero-API-Key": zff.API_KEY, "Zotero-API-Version": "3",
            "Content-Type": "application/json"}


def _lib_version():
    req = urllib.request.Request(f"{zff.WEB_API}/users/{zff.USER_ID}/items/top?limit=1",
                                 headers={"Zotero-API-Key": zff.API_KEY, "Zotero-API-Version": "3"})
    with zff._robust_urlopen(req, timeout=30) as r:
        return r.headers.get("Last-Modified-Version") or "0"


# ---------------------------------------------------------------- 集合（按关键词分目录）
_COLLECTION_CACHE = {}   # (name, parent_key) -> key，进程内复用，避免重复建


def _collection_name_for_keyword(kw):
    """把 PubMed 检索式整理成可用的 Zotero 集合名（去字段标签/引号，限长）。"""
    name = _FIELD_TAG.sub(" ", kw or "").replace('"', " ")
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = (kw or "关键词")[:60]
    return name[:80]


def ensure_collection(name, parent_key=None):
    """确保某集合存在，返回其 key；已存在则直接复用。"""
    cache_key = (name, parent_key)
    if cache_key in _COLLECTION_CACHE:
        return _COLLECTION_CACHE[cache_key]
    # 先查是否已有同名同级的集合
    try:
        req = urllib.request.Request(
            f"{zff.WEB_API}/users/{zff.USER_ID}/collections?limit=100&format=json",
            headers={"Zotero-API-Key": zff.API_KEY, "Zotero-API-Version": "3"})
        with zff._robust_urlopen(req, timeout=30) as r:
            existing = json.loads(r.read().decode())
        for c in existing:
            d = c.get("data", {})
            if d.get("name") == name and (d.get("parentCollection") or None) == parent_key:
                _COLLECTION_CACHE[cache_key] = d["key"]
                return d["key"]
    except Exception as e:
        flog(f"  [warn] 列举集合失败: {e}")
    # 没有则新建
    col = {"name": name}
    if parent_key:
        col["parentCollection"] = parent_key
    payload = json.dumps([col]).encode("utf-8")
    headers = _web_headers()
    try:
        headers["If-Unmodified-Since-Version"] = str(_lib_version())
    except Exception:
        pass
    req = urllib.request.Request(f"{zff.WEB_API}/users/{zff.USER_ID}/collections",
                                 data=payload, headers=headers)
    try:
        with zff._robust_urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:
        flog(f"  [warn] 创建集合「{name}」失败: {e}")
        return None
    key = (resp.get("success", {}).get("0")
           or resp.get("successful", {}).get("0")
           or resp.get("key"))
    if isinstance(key, dict):
        key = key.get("key")
    if key:
        _COLLECTION_CACHE[cache_key] = key
        return key
    flog(f"  [warn] 创建集合「{name}」未返回 key: {resp}")
    return None


def build_item_payload(rec, keyword, cfg, collection_key=None):
    ing = cfg["ingest"]
    tags = list(ing.get("extra_tags") or [])
    if ing.get("tag_with_keyword") and keyword:
        tags.append(keyword)
    extra = []
    if rec.get("pmid"):
        extra.append(f"PMID: {rec['pmid']}")
    if rec.get("pmcid"):
        extra.append(f"PMCID: {rec['pmcid']}")
    extra.append(f"UpstreamScore: {rec.get('score')} "
                 f"(FT={rec['subscores']['fulltext']}, IF={rec.get('impact_factor')}, "
                 f"Cites={rec.get('citations')}, Rel={rec.get('relevance')})")
    item = {
        "itemType": "journalArticle",
        "title": rec.get("title") or "Unknown",
        "creators": [{"creatorType": "author",
                      "lastName": a.split()[-1] if a.split() else a,
                      "firstName": " ".join(a.split()[:-1])}
                     for a in (rec.get("authors") or [])[:20] if a],
        "abstractNote": rec.get("abstract") or "",
        "publicationTitle": rec.get("journal") or "",
        "date": str(rec.get("pubdate") or rec.get("year") or ""),
        "DOI": rec.get("doi") or "",
        "url": (f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/" if rec.get("pmid") else ""),
        "extra": "\n".join(extra),
        "tags": [{"tag": t} for t in tags if t],
    }
    ck = collection_key if collection_key else ing.get("collection_key")
    if ck:
        item["collections"] = [ck]
    return item


def ingest_item(rec, keyword, cfg, collection_key=None):
    """通过 Web API 新建顶层条目。返回 (ok, key_or_msg)。只新增，不动已有条目。"""
    if not (zff.USER_ID and zff.USER_ID != "0" and zff.API_KEY):
        return False, "缺少 ZOTERO_USER_ID / ZOTERO_API_KEY"
    payload = json.dumps([build_item_payload(rec, keyword, cfg, collection_key)]).encode("utf-8")
    last_err = ""
    for attempt in range(4):
        try:
            headers = _web_headers()
            try:
                headers["If-Unmodified-Since-Version"] = str(_lib_version())
            except Exception:
                pass
            req = urllib.request.Request(f"{zff.WEB_API}/users/{zff.USER_ID}/items",
                                         data=payload, headers=headers)
            with zff._robust_urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode())
            key = (resp.get("success", {}).get("0") or resp.get("successful", {}).get("0")
                   or resp.get("key"))
            if isinstance(key, dict):
                key = key.get("key")
            if key:
                return True, key
            failed = resp.get("failed", {})
            last_err = f"未返回 key: {failed or resp}"
            if failed:
                return False, last_err     # 数据本身有问题，重试无意义
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:160]
            last_err = f"HTTP {e.code}: {body}"
            if e.code != 412:
                break                      # 非版本冲突，不重试
        except Exception as e:
            last_err = str(e)[:160]
        time.sleep(1.5 + attempt)
    return False, last_err


# ---------------------------------------------------------------- 7. RUN
def run_once(keywords, cfg, dry_run=False):
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    daily_cap = int(cfg["filter"].get("daily_max_ingest") or 0)
    used = int(state["daily"].get(today, 0))
    quota = max(daily_cap - used, 0) if daily_cap > 0 else 10 ** 9

    flog("")
    flog(f"===== 上游检索启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
         f"{'[DRY-RUN 不入库]' if dry_run else ''} =====")
    flog(f"  关键词 {len(keywords)} 个；今日额度 {used}/{daily_cap if daily_cap > 0 else '∞'}，剩余 {quota if daily_cap > 0 else '∞'}")
    if not keywords:
        flog("  [skip] 没有关键词，退出。请在 upstream_config.json 的 keywords 里填写，或用 --keywords 指定。")
        return {"keywords": [], "ingested": 0}

    if_table = load_if_table()
    idx = library_index()
    flog(f"  本地库指纹：{len(idx['dois'])} DOI / {len(idx['pmids'])} PMID / {len(idx['titles'])} 标题")

    report = {"time": datetime.now().isoformat(timespec="seconds"), "dry_run": dry_run,
              "keywords": [], "ingested_total": 0, "skipped_total": 0}
    seen_this_run = set()
    ingested_total = 0

    # 父集合（"上游检索"），仅真实入库时创建
    parent_coll_key = None
    if cfg["ingest"].get("per_keyword_collections") and not dry_run:
        pname = cfg["ingest"].get("parent_collection_name") or ""
        if pname:
            parent_coll_key = ensure_collection(pname)
            if parent_coll_key:
                flog(f"  父集合「{pname}」已就绪 (key={parent_coll_key})")

    for keyword in keywords:
        flog("")
        flog(f"[keyword] {keyword}")
        # 每个关键词建一个集合，命中论文归进去（真实运行才建，dry-run 不碰库）
        coll_key = None
        if cfg["ingest"].get("per_keyword_collections") and not dry_run:
            kw_coll_name = _collection_name_for_keyword(keyword)
            coll_key = ensure_collection(kw_coll_name, parent_coll_key)
            if coll_key:
                flog(f"  集合「{kw_coll_name}」就绪 (key={coll_key})")
        retmax = int(cfg["search"].get("per_keyword_retmax") or 30)
        pmids, hit_count = pubmed_search(keyword, retmax,
                                         cfg["search"].get("recent_days"),
                                         cfg["search"].get("sort") or "relevance",
                                         cfg["search"].get("ncbi_api_key") or "")
        flog(f"  [search] PubMed 命中 {hit_count} 篇，取前 {len(pmids)} 篇评估")
        kw_report = {"keyword": keyword, "pubmed_hits": hit_count, "evaluated": len(pmids),
                     "ingested": [], "skipped": []}
        if not pmids:
            report["keywords"].append(kw_report)
            continue

        metas = pubmed_fetch(pmids, cfg["search"].get("ncbi_api_key") or "")
        records = []
        for rank, pid in enumerate(pmids):
            r = metas.get(pid)
            if r:
                r["_rank"] = rank
                records.append(r)

        openalex_enrich(records)
        for r in records:
            if not has_fulltext(r):
                epmc_fulltext_status(r)      # 只对 OpenAlex 没判出 OA 的再查一次，省请求
            score_record(r, keyword, cfg, if_table, r.get("_rank", 0), len(pmids))

        # 质量优先级排序：综合分 -> 全文可得 -> 影响因子 -> 被引 -> 相关度
        records.sort(key=lambda x: (x["score"], x["fulltext"], x["impact_factor"],
                                    x["citations"], x["relevance"]), reverse=True)

        for r in records:
            tag = f"PMID {r.get('pmid')} | {(r.get('title') or '')[:58]}"
            if r.get("pmid") in seen_this_run:
                kw_report["skipped"].append({"pmid": r.get("pmid"), "reason": "本轮其他关键词已处理"})
                continue
            seen_this_run.add(r.get("pmid"))

            ok, why = passes_filter(r, cfg)
            if not ok:
                flog(f"  [skip] {tag} -> {why}")
                kw_report["skipped"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                             "reason": why, "score": r.get("score")})
                if r.get("pmid") and r["pmid"] not in state["seen_pmids"]:
                    state["seen_pmids"].append(r["pmid"])
                continue

            dup, why = is_duplicate(r, idx, state)
            if dup:
                flog(f"  [dup]  {tag} -> {why}")
                kw_report["skipped"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                             "reason": why, "score": r.get("score")})
                continue

            detail = (f"分={r['score']} 全文={'有' if r['fulltext'] else '无'} "
                      f"IF={r['impact_factor']}({r['if_source']}) 被引={r['citations']} 相关={r['relevance']}")
            if quota <= 0:
                flog(f"  [quota] {tag} -> 已达今日入库上限，留到明天（{detail}）")
                kw_report["skipped"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                             "reason": "超出今日入库上限", "score": r.get("score")})
                continue

            if dry_run or not cfg["ingest"].get("enabled", True):
                flog(f"  [would-ingest] {tag} -> {detail}")
                kw_report["ingested"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                              "score": r.get("score"), "key": "(dry-run)"})
                quota -= 1
                continue

            ok, key_or_err = ingest_item(r, keyword, cfg, coll_key)
            if ok:
                flog(f"  [ingest] {tag} -> Zotero key={key_or_err} | {detail}")
                kw_report["ingested"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                              "score": r.get("score"), "key": key_or_err})
                state["ingested"][r.get("pmid") or key_or_err] = {
                    "doi": r.get("doi"), "title": r.get("title"), "key": key_or_err,
                    "date": today, "score": r.get("score"), "keyword": keyword}
                if r.get("doi"):
                    idx["dois"].add(r["doi"].lower())
                if r.get("pmid"):
                    idx["pmids"].add(r["pmid"])
                idx["titles"].add(_norm_title(r.get("title")))
                ingested_total += 1
                quota -= 1
                state["daily"][today] = int(state["daily"].get(today, 0)) + 1
                save_state(state)
            else:
                flog(f"  [error] {tag} -> 入库失败: {key_or_err}")
                kw_report["skipped"].append({"pmid": r.get("pmid"), "title": r.get("title"),
                                             "reason": f"入库失败: {key_or_err}"})

        flog(f"  [done] 关键词「{keyword}」入库 {len(kw_report['ingested'])} 篇，"
             f"跳过 {len(kw_report['skipped'])} 篇")
        report["keywords"].append(kw_report)

    report["ingested_total"] = sum(len(k["ingested"]) for k in report["keywords"])
    report["skipped_total"] = sum(len(k["skipped"]) for k in report["keywords"])
    state["runs"] = int(state.get("runs", 0)) + 1
    state["seen_pmids"] = state["seen_pmids"][-5000:]
    save_state(state)

    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, f"upstream_{datetime.now().strftime('%Y%m%d')}.json")
    hist = []
    if os.path.exists(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = []
    hist.append(report)
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

    flog("")
    flog(f"===== 本轮结束：入库 {report['ingested_total']} 篇，跳过 {report['skipped_total']} 篇 =====")
    flog(f"  明细报告：{rp}")
    if report["ingested_total"] and not dry_run:
        flog("  新条目已写入云端库；Zotero 同步后，常驻监视器会自动为其抓全文 / 生成 PPT 摘要。")
    return report


def show_status():
    cfg = load_config()
    s = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    cap = cfg["filter"].get("daily_max_ingest")
    print("=== 上游检索状态 ===")
    print(f"  配置文件      : {CONFIG_PATH}")
    print(f"  关键词        : {len(cfg.get('keywords') or [])} 个 -> {cfg.get('keywords')}")
    print(f"  今日入库      : {s['daily'].get(today, 0)} / {cap if cap else '∞'}")
    print(f"  累计入库      : {len(s['ingested'])} 篇，累计运行 {s.get('runs', 0)} 轮")
    print(f"  评估过未入库  : {len(s['seen_pmids'])} 篇")
    print(f"  日志          : {LOG_PATH}")
    recent = sorted(s["ingested"].items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[:10]
    if recent:
        print("  最近入库：")
        for pmid, v in recent:
            print(f"    [{v.get('date')}] 分={v.get('score')} key={v.get('key')} | {(v.get('title') or '')[:60]}")


def main():
    ap = argparse.ArgumentParser(description="上游文献自动获取与入库（PubMed -> 质量筛选 -> Zotero）")
    ap.add_argument("--run", action="store_true", help="执行一轮检索入库")
    ap.add_argument("--keywords", nargs="+", help="临时关键词（覆盖配置文件）")
    ap.add_argument("--keywords-file", help="关键词文件（每行一个，# 为注释）")
    ap.add_argument("--dry-run", action="store_true", help="只检索打分，不入库")
    ap.add_argument("--max", type=int, help="临时覆盖今日入库上限")
    ap.add_argument("--min-if", type=float, help="临时覆盖影响因子阈值")
    ap.add_argument("--min-citations", type=int, help="临时覆盖最低被引次数")
    ap.add_argument("--require-fulltext", action="store_true", help="只要有全文可获取的文献")
    ap.add_argument("--status", action="store_true", help="查看额度与历史入库")
    args = ap.parse_args()

    if args.status:
        show_status()
        return
    if not args.run:
        ap.print_help()
        return

    cfg = load_config()
    if args.max is not None:
        cfg["filter"]["daily_max_ingest"] = args.max
    if args.min_if is not None:
        cfg["filter"]["min_impact_factor"] = args.min_if
    if args.min_citations is not None:
        cfg["filter"]["min_citations"] = args.min_citations
    if args.require_fulltext:
        cfg["filter"]["require_fulltext"] = True

    keywords = resolve_keywords(cfg, args.keywords, args.keywords_file)
    run_once(keywords, cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
