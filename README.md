# spa-seo-bridge

给 **JS 单页应用（SPA）** 做搜索引擎与 AI 爬虫的 HTML 直出，不引入 Node、不跑 headless 浏览器、不改前端构建。

原理一句话：**nginx 按 User-Agent 分流，爬虫请求转到一个几十行的 Python 服务；这个服务直接读你业务库里的数据，拼成完整 HTML 返回。**

```
             ┌──────────── 普通用户 ────────────┐
GET /blog/x ─┤                                  ├─→ SPA（Vue/React 打包产物，原样不动）
             └──────────── 爬虫 / AI Bot ───────┘
                              │
                              ▼
                     Python 直出服务 :8090
                              │
                        read-only SQLite
```

## 为什么不用 Prerender.io / Rendertron

这类方案的共同点是**起一个 headless Chrome，把页面渲染完再吐 HTML**。代价：

| 方案 | 内存 | 冷启动 | 依赖 | 适合 |
|---|---|---|---|---|
| SSR 重写（Nuxt/Next） | 中 | 慢 | 整个前端要重构 | 新建项目 |
| 预渲染（build 时静态） | 无 | 无 | 内容一变就要重新构建 | 内容固定 |
| Prerender.io / Rendertron | **几百 MB～GB** | **2～5 秒** | 要维护 Chrome 环境 | 内容来自第三方页面 |
| **本项目（数据层直出）** | **~30 MB** | **<10ms** | 只有 Python 标准库 | 内容存在自己的库 |

如果你的标题、正文、价格本来就存在数据库里（博客、商品、文档、问答），那跑一遍浏览器只是把数据库里的字段重新排版一次——纯浪费。直接拼字符串就够了。

## 快速开始

```bash
# 1. 准备一份只读库（表结构见下）
sqlite3 app.db < example.sql

# 2. 起服务（只用标准库，无第三方依赖）
python3 spa_seo_bridge.py --db ./app.db --site https://example.com --port 8090

# 3. 模拟爬虫访问，看拿到的是不是完整 HTML
curl -s -A "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)" \
     http://127.0.0.1:8090/blog/hello-world | head -40

# 4. 对比普通 UA（应该是空壳，证明分流只对爬虫生效）
curl -s http://127.0.0.1:8090/blog/hello-world | head -20

# 5. sitemap 与 llms.txt
curl -s http://127.0.0.1:8090/sitemap.xml
curl -s http://127.0.0.1:8090/llms.txt
```

## 数据库表结构

只要三张表，字段名可改，改 `CONFIG` 里的映射即可。

```sql
CREATE TABLE posts (
  id          INTEGER PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,
  title       TEXT NOT NULL,
  summary     TEXT,
  content     TEXT,              -- HTML 片段，可含 <h2 data-faq="1"> 标记问答
  published_at TEXT              -- 建议存 UTC，格式 YYYY-MM-DD HH:MM:SS
);

CREATE TABLE categories (id INTEGER PRIMARY KEY, slug TEXT, name TEXT);
```

## nginx 配置

见 `nginx-seo-split.conf`。核心就三段：`map` 判 UA、`map` 选后端、`proxy_pass` 到变量。

三个必须注意的点写在配置注释里：

1. **静态资源不能被接管**。`/assets/app.js` 这类请求必须回源到 SPA，否则爬虫拿到 HTML 却加载不到 CSS/JS，渲染质量分反而掉。
2. **`proxy_pass` 用变量时不会自动带 URI 和 Host**，得手写 `proxy_set_header Host $host`。
3. **`if ($is_bot) { proxy_pass ... }` 是官方明确警告的写法**，用 `map` + 变量选 upstream，不要用 if。

## 直出页面必须和 SPA 一致

不然就是 cloaking（ cloak 作弊），Google 的《动态渲染》文档里说得很清楚：**只要内容基本一致，动态渲染是可接受的；内容不一致就是作弊**。

自检清单：

- [ ] `<h1>`、`<title>`、正文首段，两边一致
- [ ] 价格/数字/日期一致（尤其电商，别给爬虫写"面议"、给用户写实价）
- [ ] `robots` meta 一致（别给爬虫 `index`、给用户 `noindex`）
- [ ] canonical 两边都指向同一个绝对 URL
- [ ] 未知 slug 必须返回 **真 404**，不能 200 + 空壳

## 结构化数据

直出服务顺手注入 JSON-LD，比前端 JS 注入更早被读到：

- `Article` / `BlogPosting`：标题、摘要、发布时间
- `FAQPage`：正文里 `<h2 data-faq="1">问题</h2><p>答案</p>` 会被抽成 Q/A
- `Organization`：站点主体
- OpenGraph：分享卡片，Bing/微信/Twitter 都读

## 附带产出

- `/sitemap.xml`：从库里实时生成，只含已发布、非草稿
- `/llms.txt`：给 GPTBot / ClaudeBot / PerplexityBot 的站点索引，格式是一串 `- [标题](绝对URL): 摘要`
- IndexNow 推送：新内容发布即推，不依赖已废弃的 `ping?sitemap=`

## 验证是否真的生效

```bash
# 线上域名（不要用 127.0.0.1，Host 不匹配会 404）
curl -s -o /dev/null -w "%{http_code}\n" -A "bingbot/2.0" https://example.com/blog
curl -s -A "bingbot/2.0" https://example.com/blog | grep -c "<h2"

# 两个 UA 的正文字数差，应该 > 0；差值为 0 说明分流没生效
```

搜索引擎侧：Bing Webmaster → URL Inspection → 看 `Crawl` 段和 `Index` 段是否分叉；Google Search Console → 网址检查 → 看"已抓取的页面"里有没有正文。

## 已知局限

- **前端改版后要同步改直出模板**，两套模板是维护成本，这是这个方案唯一的真实代价。
- 直出服务挂了会让爬虫拿到 502，建议 systemd `Restart=always` + nginx `proxy_next_upstream`。
- 不适合高度依赖前端交互才出内容的页面（比如无限滚动列表），那种页面爬虫本来也不该收。

## License

MIT
