# -*- coding: utf-8 -*-
"""
热点收集助手 - 单机版
双击 exe 自动打开浏览器管理界面：手动录入热点 + 抓取公开热榜 + 本地 SQLite 存储
"""
import os
import sys
import json
import sqlite3
import threading
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime

from flask import Flask, request, jsonify, render_template, send_from_directory

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

# ---------- 热榜数据源（公开 API，失败自动降级） ----------
SOURCES = {
    "wechat": {"name": "公众号精选", "url": "https://60s.viki.moe/v2/60s"},
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
        # data = {date, news: [...], ...}
        news = (data or {}).get("news", []) if isinstance(data, dict) else []
        for i, t in enumerate(news):
            items.append({"title": t, "source": name, "link": "", "note": "", "hot": len(news) - i})
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

# ---------- API 路由 ----------
@app.route("/")
def index():
    return render_template("index.html")

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
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        conn.execute(
            "INSERT INTO hotspots(title, source, link, note, tag, hot, created_at) VALUES(?,?,?,?,?,?,?)",
            (title, (it.get("source") or "").strip(), (it.get("link") or "").strip(), (it.get("note") or "").strip(),
             it.get("tag") or "热榜抓取", int(it.get("hot", 0) or 0), now),
        )
        n += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": f"已收藏 {n} 条", "count": n})

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    """抓取指定榜单，返回归一化结果（不入库，先给用户预览确认）"""
    body = request.get_json(silent=True) or {}
    key = body.get("source", "wechat")
    if key not in SOURCES:
        return jsonify({"ok": False, "msg": "未知数据源"}), 400
    try:
        payload = http_get_json(SOURCES[key]["url"])
        items = parse_source(key, payload)
        if not items:
            return jsonify({"ok": False, "msg": "该榜单暂未返回数据，请稍后再试"})
        return jsonify({"ok": True, "items": items, "name": SOURCES[key]["name"]})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"请求被限流或拒绝({e.code})，稍后再试或换个榜单"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"抓取失败：{str(e)[:80]}"})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    conn = get_db()
    conn.execute("DELETE FROM hotspots")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已清空全部数据"})

# ---------- 启动 ----------
def open_browser():
    webbrowser.open("http://127.0.0.1:56789")

if __name__ == "__main__":
    # 延迟打开浏览器，等服务起来
    threading.Timer(1.2, open_browser).start()
    print("=" * 50)
    print("热点收集助手已启动：http://127.0.0.1:56789")
    print(f"数据库位置：{DB_PATH}")
    print("关闭此窗口即退出程序")
    print("=" * 50)
    app.run(host="127.0.0.1", port=56789, debug=False, threaded=True)
