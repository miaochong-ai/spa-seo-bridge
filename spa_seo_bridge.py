#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spa-seo-bridge —— 给 SPA 做爬虫 HTML 直出，只用 Python 标准库。

设计要点：
1. 只读业务库，绝不做写操作（连接用 mode=ro，万一库被锁也不影响业务）。
2. 每个请求现拼 HTML，不做缓存；数据改了立刻生效，不用重启。
3. 未知 slug 返回真 404，不给搜索引擎喂空壳。
4. 所有 URL 都拼绝对地址（canonical / og:url / sitemap 都要求绝对地址）。

用法：
    python3 spa_seo_bridge.py --db ./app.db --site https://example.com --port 8090
"""

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------- 配置

CFG = {
    "site": "https://example.com",
    "name": "Example",
    "tagline": "一个用 Python 直接吐 HTML 的小站",
    "db": "./app.db",
    # 表名 / 字段映射：换成你自己的表名即可
    "table": "posts",
    "cols": {
        "slug": "slug",
        "title": "title",
        "summary": "summary",
        "content": "content",
        "published": "published_at",
    },
}

TAG_RE = re.compile(r"<[^>]+>")
FAQ_RE = re.compile(r'<h2[^>]*data-faq[^>]*>(.*?)</h2>\s*<p>(.*?)</p>', re.S)


def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "").replace("&nbsp;", " ")


def plain_len(s: str) -> int:
    """中文按字算、英文按词算的粗估，用来判断直出页是否有实质正文。"""
    txt = strip_tags(s)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
    words = len(re.findall(r"[A-Za-z0-9]+", txt))
    return cjk + words


def db():
    # mode=ro：只读打开，业务库在写也不冲突（WAL 模式下更安全）
    conn = sqlite3.connect("file:%s?mode=ro" % CFG["db"], uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def posts(limit=None):
    c = CFG["cols"]
    sql = ("SELECT %s AS slug, %s AS title, %s AS summary, %s AS content, %s AS published "
           "FROM %s ORDER BY %s DESC") % (
        c["slug"], c["title"], c["summary"], c["content"], c["published"],
        CFG["table"], c["published"])
    if limit:
        sql += " LIMIT %d" % limit
    with db() as conn:
        return [dict(r) for r in conn.execute(sql)]


def post(slug):
    c = CFG["cols"]
    sql = ("SELECT %s AS slug, %s AS title, %s AS summary, %s AS content, %s AS published "
           "FROM %s WHERE %s = ?") % (
        c["slug"], c["title"], c["summary"], c["content"], c["published"],
        CFG["table"], c["slug"])
    with db() as conn:
        row = conn.execute(sql, (slug,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- HTML 骨架

def page(title, desc, canonical, body, head_extra=""):
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<meta name="description" content="%s">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">
<link rel="canonical" href="%s">
<meta property="og:type" content="website">
<meta property="og:site_name" content="%s">
<meta property="og:title" content="%s">
<meta property="og:description" content="%s">
<meta property="og:url" content="%s">
%s
<style>body{max-width:760px;margin:40px auto;padding:0 16px;font:16px/1.75 -apple-system,"PingFang SC",sans-serif;color:#222}
h1{font-size:26px;line-height:1.35}a{color:#0b5fff}li{margin:6px 0}.meta{color:#888;font-size:13px}</style>
</head>
<body>
%s
</body>
</html>""" % (
        escape(title), escape(desc), escape(canonical),
        escape(CFG["name"]), escape(title), escape(desc), escape(canonical),
        head_extra, body)


def faq_pairs(content):
    """从 <h2 data-faq="1">Q</h2><p>A</p> 里抽问答，喂给 FAQPage。"""
    out = []
    for q, a in FAQ_RE.findall(content or ""):
        qq, aa = strip_tags(q).strip(), strip_tags(a).strip()
        if qq and aa and len(qq) <= 80:
            out.append((qq, aa))
    return out


def ld_article(p):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": p["title"],
        "description": strip_tags(p["summary"])[:160],
        "datePublished": (p["published"] or "")[:10],
        "url": "%s/blog/%s" % (CFG["site"], p["slug"]),
        "publisher": {"@type": "Organization", "name": CFG["name"]},
    }, ensure_ascii=False)


def ld_faq(pairs):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs
        ],
    }, ensure_ascii=False)


# ---------------------------------------------------------------- 各路由

def route_home():
    items = posts(limit=30)
    lis = "\n".join(
        '<li><a href="/blog/%s">%s</a><div class="meta">%s</div><p>%s</p></li>' % (
            escape(p["slug"]), escape(p["title"]),
            escape((p["published"] or "")[:10]), escape(strip_tags(p["summary"])[:120]))
        for p in items)
    body = "<h1>%s</h1><p>%s</p><ul>%s</ul>" % (
        escape(CFG["name"]), escape(CFG["tagline"]), lis)
    return page(CFG["name"], CFG["tagline"], CFG["site"] + "/", body)


def route_post(slug):
    p = post(slug)
    if not p:
        return None                       # 交给调用方返回 404
    pairs = faq_pairs(p["content"])
    extra = ['<script type="application/ld+json">%s</script>' % ld_article(p)]
    if pairs:
        extra.append('<script type="application/ld+json">%s</script>' % ld_faq(pairs))
    body = ("<h1>%s</h1><div class=\"meta\">%s</div>%s<p><a href=\"/blog\">← 返回列表</a></p>"
            % (escape(p["title"]), escape((p["published"] or "")[:10]), p["content"] or ""))
    return page(p["title"], strip_tags(p["summary"])[:160],
                "%s/blog/%s" % (CFG["site"], slug), body, "\n".join(extra))


def route_sitemap():
    urls = ["%s/" % CFG["site"], "%s/blog" % CFG["site"]]
    urls += ["%s/blog/%s" % (CFG["site"], p["slug"]) for p in posts()]
    items = "".join(
        "<url><loc>%s</loc><changefreq>weekly</changefreq></url>" % escape(u) for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % items)


def route_llms():
    lines = ["# %s" % CFG["name"], "> %s" % CFG["tagline"], ""]
    for p in posts(limit=50):
        lines.append("- [%s](%s/blog/%s): %s" % (
            p["title"], CFG["site"], p["slug"], strip_tags(p["summary"])[:100]))
    return "\n".join(lines)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "spa-seo-bridge"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Robots-Tag", "index, follow")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/sitemap.xml":
            return self._send(200, route_sitemap(), "application/xml; charset=utf-8")
        if path == "/llms.txt":
            return self._send(200, route_llms(), "text/plain; charset=utf-8")
        if path in ("/", "/blog", "/blog/"):
            return self._send(200, route_home())
        m = re.match(r"^/blog/([A-Za-z0-9_-]+)$", path)
        if m:
            html_body = route_post(m.group(1))
            if html_body is None:
                return self._send(404, "<h1>404</h1><p>页面不存在</p>")
            return self._send(200, html_body)
        return self._send(404, "<h1>404</h1>")

    def log_message(self, fmt, *args):
        # 带 UA 打日志，方便事后 grep 到底是哪个爬虫来的
        print('%s "%s" UA="%s"' % (self.log_date_time_string(), fmt % args,
                                   self.headers.get("User-Agent", "-")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--name", default=CFG["name"])
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="127.0.0.1")   # 只监听本机，外部打不到
    a = ap.parse_args()
    CFG.update(db=a.db, site=a.site.rstrip("/"), name=a.name)

    # 自检：库能不能读、表里有没有东西
    try:
        n = len(posts(limit=1))
        print("[ok] db=%s rows=%d site=%s" % (CFG["db"], n, CFG["site"]))
    except sqlite3.Error as e:
        raise SystemExit("[fatal] 读库失败: %s" % e)

    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
