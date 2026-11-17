# -*- coding: utf-8 -*-
"""
热点收集助手 - 单机版
双击 exe 自动打开浏览器管理界面：手动录入热点 + 抓取公开热榜 + 本地 SQLite 存储
"""
import os
import re
import sys
import json
import sqlite3
import threading
import webbrowser
import urllib.request
import urllib.error
import urllib.parse
import time as _time
from datetime import datetime, timedelta, date

from flask import Flask, request, jsonify, render_template, send_from_directory
from bs4 import BeautifulSoup

# ---------- 路径处理（兼容 PyInstaller 打包后的资源路径） ----------
def resource_path(relative):
    """获取资源绝对路径：打包后从 _MEIPASS 读，开发时从脚本目录读"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

# 数据库放在用户目录，避免 exe 所在目录无写权限
DATA_DIR = os.path.join(os.path.expanduser("~"), "热点收集助手数据")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "hotspots.db")

app = Flask(__name__, template_folder=resource_path("templates"))

# ---------- 数据库 ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hotspots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT DEFAULT '',
            link TEXT DEFAULT '',
            note TEXT DEFAULT '',
            tag TEXT DEFAULT '手动录入',
            hot INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------- AI 润色配置（存用户目录，OpenAI 兼容格式） ----------
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DEFAULT_CONFIG = {
    "api_base": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    "model": "glm-4-flash",
    "prompt": "你是一个中文热点文案润色助手。请把下面收集的热点文本润色成一段通顺、专业、适合发布的短文。保留事实信息，不要编造。只输出润色后的正文，不要解释。\n\n原文：\n{text}"
}

# 免费优先的大模型预设（OpenAI 兼容；免费政策以各平台实时为准）
MODEL_PRESETS = [
    {"id": "glm-4-flash", "name": "智谱 GLM-4-Flash（永久免费）", "free": True,
     "api_base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
     "note": "免费额度大、中文写作好，需智谱开放平台 Key"},
    {"id": "qwen-turbo", "name": "通义千问 Qwen-Turbo（新用户免费额度）", "free": True,
     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-turbo",
     "note": "阿里云百炼，注册送免费额度"},
    {"id": "deepseek-chat", "name": "DeepSeek Chat（低价/有免费额度）", "free": False,
     "api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "note": "性价比高，新用户有赠送额度"},
    {"id": "custom", "name": "自定义（下方手动填写）", "free": False,
     "api_base": "", "model": "", "note": "任何 OpenAI 兼容接口"},
]

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg or {})
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------- 热榜数据源（公开 API，失败自动降级） ----------
SOURCES = {
    "wechat": {"name": "公众号热文榜(按阅读量)", "url": "https://tophub.today/n/WnBe01o371"},
    "weibo":  {"name": "微博热搜",   "url": "https://60s.viki.moe/v2/weibo"},
    "baidu":  {"name": "百度热搜",   "url": "https://60s.viki.moe/v2/baidu/hot"},
    "quark":  {"name": "夸克热点",   "url": "https://60s.viki.moe/v2/quark"},
    "zhihu":  {"name": "知乎热榜",   "url": "https://60s.viki.moe/v2/zhihu"},
    "douyin": {"name": "抖音热榜",   "url": "https://60s.viki.moe/v2/douyin"},
}

def http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def parse_source(key, payload):
    """把各数据源的原始 JSON 归一化为 [{title, source, link, note, hot}]"""
    data = payload.get("data")
    items = []
    name = SOURCES[key]["name"]
    if key == "wechat":
        # data = {date, news: [...], link: <当天公众号原文>, ...}
        news = (data or {}).get("news", []) if isinstance(data, dict) else []
        article_link = (data or {}).get("link", "") if isinstance(data, dict) else ""
        for i, t in enumerate(news):
            items.append({"title": t, "source": name, "link": article_link, "note": "", "hot": len(news) - i})
    elif isinstance(data, list):
        for i, d in enumerate(data[:30]):
            if not isinstance(d, dict):
                continue
            title = d.get("title") or d.get("word") or ""
            if not title:
                continue
            link = d.get("link") or d.get("url") or d.get("mobileUrl") or ""
            note = d.get("desc") or d.get("summary") or d.get("content") or ""
            hot = d.get("hot_value") or d.get("hotValue") or d.get("hot") or (100 - i)
            try:
                hot = int(hot)
            except (TypeError, ValueError):
                hot = 100 - i
            items.append({"title": title, "source": name, "link": link, "note": note, "hot": hot})
    return items

def parse_wechat_hot(html, name):
    """解析 tophub 微信24h热文榜：按阅读量从高到低，提取标题/阅读量/原文链接"""
    soup = BeautifulSoup(html, "html.parser")
    import re
    def hot_to_num(s):
        """'10.0万' -> 100000, '9.8万' -> 98000, '3.2亿'->320000000"""
        m = re.search(r"([\d.]+)\s*([万亿wW]?)", s or "")
        if not m:
            return 0
        try:
            v = float(m.group(1))
        except ValueError:
            return 0
        unit = m.group(2).lower()
        if unit in ("万", "w"):
            v *= 10000
        elif unit == "亿":
            v *= 100000000
        return int(v)

    items = []
    for tr in soup.select("table tr"):
        tds = tr.select("td")
        if len(tds) < 2:
            continue
        a = tds[1].select_one("a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        link = a.get("href", "")
        # 阅读量：在剩余 td 文本里找形如 10.0万 的数字
        read_text = ""
        for td in tds[2:]:
            txt = td.get_text(" ", strip=True)
            if re.search(r"[\d.]+\s*[万亿wW]", txt):
                read_text = txt
                break
        if not read_text:
            m = re.search(r"([\d.]+\s*[万亿wW])", tr.get_text(" ", strip=True))
            read_text = m.group(1) if m else ""
        items.append({
            "title": title,
            "source": name,
            "link": link,
            "note": f"阅读量 {read_text}" if read_text else "",
            "hot": hot_to_num(read_text),
        })
    # 已按榜单顺序（阅读量降序），保险起见再按 hot 排一次
    items.sort(key=lambda x: x.get("hot", 0), reverse=True)
    return items

# ---------- 搜狗微信关键词搜索（按相关性，取前N篇） ----------
def search_wechat_by_keyword(keyword, limit=20):
    """按关键词搜公众号文章，返回 [{title, account, note, link, rank}]"""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    results = []
    seen = set()
    for page in range(1, 11):  # 最多10页
        url = ("https://weixin.sogou.com/weixin?type=2&query="
               + urllib.parse.quote(keyword) + f"&page={page}&ie=utf8")
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://weixin.sogou.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", "ignore")
        except Exception:
            break
        if "安全验证" in html or "请输入验证码" in html:
            if not results:
                raise RuntimeError("captcha")
            break
        soup = BeautifulSoup(html, "html.parser")
        lis = soup.select("ul.news-list li")
        if not lis:
            break
        for li in lis:
            a = li.select_one("h3 a")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title or title in seen:
                continue
            seen.add(title)
            href = a.get("href", "")
            link = ("https://weixin.sogou.com" + href) if href.startswith("/") else href
            # 公众号名优先取 .all-time-y2（.s-p 整块会混入脚本文本）
            acc_el = li.select_one(".all-time-y2") or li.select_one(".s-p")
            account = acc_el.get_text(" ", strip=True) if acc_el else ""
            note_el = li.select_one(".txt-info")
            note = note_el.get_text(" ", strip=True) if note_el else ""
            # 发布时间：结果页 span.s2 里是 timeConvert('1699…') 的 unix 时间戳
            pub_time = ""
            m = re.search(r"timeConvert\('(\d{9,11})'\)", li.decode() if hasattr(li, "decode") else "")
            if m:
                try:
                    pub_time = datetime.fromtimestamp(int(m.group(1))).strftime("%Y-%m-%d")
                except Exception:
                    pub_time = ""
            results.append({
                "title": title, "account": account,
                "note": note[:140], "link": link,
                "pub_time": pub_time,
                "rank": len(results) + 1,
            })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
        _time.sleep(0.6)
    return results

# ---------- API 路由 ----------
APP_VERSION = "16"  # 页面/程序版本，改动前端时 +1

@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION})

@app.route("/api/sources")
def api_sources():
    return jsonify({"sources": [{"key": k, "name": v["name"]} for k, v in SOURCES.items()]})

@app.route("/api/hotspots", methods=["GET"])
def api_list():
    q = request.args.get("q", "").strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT * FROM hotspots WHERE title LIKE ? OR note LIKE ? OR source LIKE ? OR tag LIKE ? ORDER BY id DESC",
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM hotspots ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"items": [dict(r) for r in rows]})

@app.route("/api/hotspots", methods=["POST"])
def api_add():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "msg": "标题不能为空"}), 400
    conn = get_db()
    conn.execute(
        "INSERT INTO hotspots(title, source, link, note, tag, hot, created_at) VALUES(?,?,?,?,?,?,?)",
        (title, body.get("source", "") or "", body.get("link", "") or "", body.get("note", "") or "",
         body.get("tag", "手动录入") or "手动录入", int(body.get("hot", 0) or 0),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已添加"})

@app.route("/api/hotspots/<int:hid>", methods=["PUT"])
def api_update(hid):
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "msg": "标题不能为空"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE hotspots SET title=?, source=?, link=?, note=?, tag=? WHERE id=?",
        (title, body.get("source", "") or "", body.get("link", "") or "", body.get("note", "") or "",
         body.get("tag", "") or "", hid),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已更新"})

@app.route("/api/hotspots/<int:hid>", methods=["DELETE"])
def api_delete(hid):
    conn = get_db()
    conn.execute("DELETE FROM hotspots WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已删除"})

@app.route("/api/hotspots/batch", methods=["POST"])
def api_batch_add():
    """批量收藏抓取结果"""
    body = request.get_json(silent=True) or {}
    items = body.get("items", [])
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "msg": "没有可收藏的条目"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    n = 0
    skipped = 0
    # 按标题去重：同一篇文章不重复入库
    existing = {r[0] for r in conn.execute("SELECT title FROM hotspots")}
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        if title in existing:
            skipped += 1
            continue
        existing.add(title)
        conn.execute(
            "INSERT INTO hotspots(title, source, link, note, tag, hot, created_at) VALUES(?,?,?,?,?,?,?)",
            (title, (it.get("source") or "").strip(), (it.get("link") or "").strip(), (it.get("note") or "").strip(),
             it.get("tag") or "热榜抓取", int(it.get("hot", 0) or 0), now),
        )
        n += 1
    conn.commit()
    conn.close()
    msg = f"已收藏 {n} 条" if n else "没有新增（都已收藏过）"
    if skipped:
        msg += f"，跳过 {skipped} 条重复"
    return jsonify({"ok": True, "msg": msg, "count": n})

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    """抓取指定榜单，返回归一化结果（不入库，先给用户预览确认）"""
    body = request.get_json(silent=True) or {}
    key = body.get("source", "wechat")
    if key not in SOURCES:
        return jsonify({"ok": False, "msg": "未知数据源"}), 400
    try:
        if key == "wechat":
            # 公众号热文榜来自 tophub 网页，解析 HTML
            req = urllib.request.Request(SOURCES[key]["url"], headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", "ignore")
            items = parse_wechat_hot(html, SOURCES[key]["name"])
        else:
            payload = http_get_json(SOURCES[key]["url"])
            items = parse_source(key, payload)
        if not items:
            return jsonify({"ok": False, "msg": "该榜单暂未返回数据，请稍后再试"})
        return jsonify({"ok": True, "items": items, "name": SOURCES[key]["name"]})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"请求被限流或拒绝({e.code})，稍后再试或换个榜单"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"抓取失败：{str(e)[:80]}"})

@app.route("/api/wechat-search", methods=["POST"])
def api_wechat_search():
    """按关键词搜索公众号文章：keyword / limit(默认20)"""
    body = request.get_json(silent=True) or {}
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"ok": False, "msg": "请输入关键词"})
    try:
        limit = int(body.get("limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))
    try:
        items = search_wechat_by_keyword(keyword, limit)
    except RuntimeError as e:
        if str(e) == "captcha":
            return jsonify({"ok": False, "msg": "搜索源触发了验证，请稍等几分钟再试，或换个关键词"})
        return jsonify({"ok": False, "msg": f"搜索失败：{str(e)[:60]}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"搜索失败：{str(e)[:60]}"})
    if not items:
        return jsonify({"ok": False, "msg": "没搜到相关文章，换个关键词试试"})
    return jsonify({"ok": True, "items": items, "keyword": keyword, "total": len(items)})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    conn = get_db()
    conn.execute("DELETE FROM hotspots")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已清空全部数据"})

# ---------- AI 润色配置与接口 ----------
@app.route("/api/model-presets", methods=["GET"])
def api_model_presets():
    return jsonify({"presets": MODEL_PRESETS})

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    # 密钥脱敏返回，前端显示勾选状态即可
    safe = dict(cfg)
    safe["key_set"] = bool(cfg.get("api_key", "").strip())
    safe["api_key"] = ""
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def api_set_config():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    for k in ("api_base", "model", "prompt"):
        if k in body and isinstance(body[k], str):
            cfg[k] = body[k].strip()
    # 密钥：留空表示不修改，传 __CLEAR__ 表示清空
    if "api_key" in body and isinstance(body["api_key"], str):
        key = body["api_key"].strip()
        if key == "__CLEAR__":
            cfg["api_key"] = ""
        elif key:
            cfg["api_key"] = key
    save_config(cfg)
    return jsonify({"ok": True, "msg": "配置已保存"})

@app.route("/api/ai-polish", methods=["POST"])
def api_ai_polish():
    """调用 OpenAI 兼容的 chat/completions 接口润色文本"""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "文本为空，无可润色内容"})
    target = body.get("target_chars")
    try:
        target = int(target) if target not in (None, "", 0) else 0
    except (TypeError, ValueError):
        target = 0
    cfg = load_config()
    if not cfg.get("api_key", "").strip():
        return jsonify({"ok": False, "msg": "尚未配置 API 密钥，请先在润色弹框里点「AI 设置」填写"})
    api_base = (cfg.get("api_base") or "https://api.deepseek.com/v1").rstrip("/")
    url = api_base + "/chat/completions"
    tmpl = cfg.get("prompt") or DEFAULT_CONFIG["prompt"]
    if target > 0:
        tmpl += f"\n\n请控制正文长度在 {target} 字左右，可适度扩写补充细节或精炼压缩，但不要偏离原意、不要编造事实。"
    prompt = tmpl.replace("{text}", text)
    payload = {
        "model": cfg.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        content = data["choices"][0]["message"]["content"].strip()
        return jsonify({"ok": True, "text": content})
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:180]
        except Exception:
            pass
        return jsonify({"ok": False, "msg": f"接口返回错误({e.code})：{detail}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"AI 润色失败：{str(e)[:100]}"})

# ---------- 启动 ----------
import socket

def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()

def open_browser():
    webbrowser.open("http://127.0.0.1:56789")

if __name__ == "__main__":
    # 单实例保护：若已有服务在跑，只打开浏览器，不再启动第二个实例
    if port_in_use(56789):
        print("检测到程序已在运行，直接打开管理界面。")
        open_browser()
        sys.exit(0)
    print("=" * 50)
    print("热点收集助手已启动：http://127.0.0.1:56789")
    print(f"数据库位置：{DB_PATH}")
    print("关闭此窗口即退出程序")
    print("=" * 50)
    # 延迟打开浏览器，等服务起来
    threading.Timer(1.2, open_browser).start()
    app.run(host="127.0.0.1", port=56789, debug=False, threaded=True)
