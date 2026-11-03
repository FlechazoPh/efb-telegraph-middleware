import io
import json
import time
import types

import pytest

from efb_telegraph_middleware import extractors, images, nodes, telegraph, urls
from efb_telegraph_middleware.archiver import Archiver
from efb_telegraph_middleware.config import load_config
from efb_telegraph_middleware.http import Page


# ------------------------------------------------------------------ urls ---
def test_extract_urls_stops_at_cjk_punctuation():
    text = "看这个https://a.com/x?y=1，还有 https://b.cn/p。以及(https://c.io/q)"
    assert urls.extract_urls(text) == ["https://a.com/x?y=1", "https://b.cn/p", "https://c.io/q"]


def test_url_matches_domain_and_path_prefix():
    skip = ["douyin.com", "bilibili.com/video"]
    assert urls.url_matches("https://v.douyin.com/abc/", skip)
    assert urls.url_matches("https://www.bilibili.com/video/BV1x", skip)
    assert not urls.url_matches("https://www.bilibili.com/read/cv1", skip)
    assert not urls.url_matches("https://mp.weixin.qq.com/s/x", ["weixin.qq.com/sph"])


def test_normalize_weixin_and_tracking():
    a = urls.normalize("http://mp.weixin.qq.com/s?__biz=AA==&mid=1&idx=2&sn=ff&chksm=x&scene=1#rd")
    b = urls.normalize("https://mp.weixin.qq.com/s?__biz=AA%3D%3D&mid=1&idx=2&sn=ff&mpshare=1")
    assert a == b
    assert urls.normalize("https://x.com/a?utm_source=w&id=3") == "https://x.com/a?id=3"


# ----------------------------------------------------------------- nodes ---
def test_nodes_lift_images_and_wrap_inline():
    html = """<div><section>Hello <b>bold</b> <span>world</span>
      <img data-src="//img.cn/a.jpg" src="data:image/gif;base64,xx"> after</section>
      <h1>Title</h1><p>para <a href="/rel">link</a></p><img src="https://img.cn/a.jpg">
      <ul><li>one</li><li>two</li></ul><script>bad()</script>
      <p style="display:none">hidden</p><img src="https://t.cn/p.gif" width="1"></div>"""
    out = nodes.html_to_nodes(html, "https://site.com/post")
    tags = [n["tag"] for n in out.nodes]
    assert tags == ["p", "figure", "p", "h3", "p", "ul"]
    assert out.nodes[0]["children"][0] == "Hello "
    assert out.nodes[1]["children"][0]["attrs"]["src"] == "https://img.cn/a.jpg"
    assert len(out.images) == 1  # duplicate and 1px tracker dropped
    assert out.nodes[4]["children"][1]["attrs"]["href"] == "https://site.com/rel"
    assert "hidden" not in json.dumps(out.nodes) and "bad()" not in json.dumps(out.nodes)


def test_nodes_table_and_blockquote():
    html = "<table><tr><td>a</td><td>b</td></tr></table><blockquote><p>q1</p><p>q2</p></blockquote>"
    out = nodes.html_to_nodes(html, "https://x/")
    assert out.nodes[0] == {"tag": "p", "children": ["a | b"]}
    assert [n["tag"] for n in out.nodes[1:]] == ["blockquote", "blockquote"]


# ------------------------------------------------------------ extractors ---
WX_HTML = """<html><head><meta property="og:title" content="OG"></head><body>
<h1 id="activity-name"> 文章标题 </h1><a id="js_name">某公众号</a>
<script>var ct = "1700000000";</script>
<div id="js_content" style="visibility: hidden;"><p>第一段内容。</p>
<p><img data-src="https://mmbiz.qpic.cn/a/640?wx_fmt=png"></p><p>第二段</p></div></body></html>"""

XHS_HTML = """<html><script>window.__INITIAL_STATE__={"noteData":{"data":{"noteData":{
"type":"normal","title":"标题","desc":"第一行 #话题[话题]#\\n第二行","time":1700000000000,
"user":{"nickname":"小红"},"imageList":[{"urlDefault":"http://sns-webpic-qc.xhscdn.com/1.jpg"},
{"infoList":[{"url":"https://sns-webpic-qc.xhscdn.com/2.jpg"}]}],"tagList":[{"name":"t1"}],
"extra":undefined}}}}</script></html>"""


def fake_fetch(html, final_url):
    return lambda session, url, timeout=12, headers=None: Page(final_url, html, "text/html")


def test_extract_weixin(monkeypatch):
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch(WX_HTML, "https://mp.weixin.qq.com/s/x"))
    art = extractors.extract(None, "https://mp.weixin.qq.com/s/x")
    assert art.title == "文章标题" and art.author == "某公众号" and art.site == "微信公众号"
    assert art.published.year == 2023
    assert [n["tag"] for n in art.nodes] == ["p", "figure", "p"]
    assert art.images[0]["attrs"]["src"].startswith("https://mmbiz.qpic.cn/")
    assert art.referer == "https://mp.weixin.qq.com/"


def test_extract_weixin_deleted(monkeypatch):
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch("<p>该内容已被发布者删除</p>", "https://mp.weixin.qq.com/s/x"))
    assert extractors.extract(None, "https://mp.weixin.qq.com/s/x") is None


def test_extract_xiaohongshu(monkeypatch):
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch(XHS_HTML, "https://www.xiaohongshu.com/discovery/item/1"))
    art = extractors.extract(None, "https://www.xiaohongshu.com/discovery/item/1")
    assert art.title == "标题" and art.author == "小红" and art.site == "小红书"
    assert art.nodes[0] == {"tag": "p", "children": ["第一行 #话题"]}
    assert len(art.images) == 2 and art.images[1]["attrs"]["src"].endswith("2.jpg")


def test_extract_xiaohongshu_login_wall(monkeypatch):
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch("<html></html>", "https://www.xiaohongshu.com/login?x"))
    assert extractors.extract(None, "https://www.xiaohongshu.com/discovery/item/1") is None


def test_extract_generic_and_fallback(monkeypatch):
    body = "<p>" + "这是一段很长的正文内容。" * 20 + "</p>"
    html = f"""<html><head><title>T - Site</title><meta property="og:site_name" content="Site">
    </head><body><nav>menu</nav><article><h1>T</h1>{body}<img data-original="/i.png" src="blank.gif">
    </article></body></html>"""
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch(html, "https://site.com/a"))
    art = extractors.extract(None, "https://site.com/a")
    assert art.site == "Site" and art.text_len > 80
    assert any(i["attrs"]["src"] == "https://site.com/i.png" for i in art.images)
    thin = """<html><head><meta name="description" content="一个很短的页面但是有不错的描述文字内容">
    <meta property="og:image" content="/c.jpg"></head><body><div id=app></div></body></html>"""
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch(thin, "https://spa.com/"))
    art = extractors.extract(None, "https://spa.com/")
    assert art.nodes[0]["tag"] == "figure" and art.nodes[1]["tag"] == "p"
    monkeypatch.setattr(extractors, "fetch_html", fake_fetch("<html><body>x</body></html>", "https://e.com/"))
    assert extractors.extract(None, "https://e.com/") is None


def test_extract_weibo(monkeypatch):
    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    status = {"id": "5", "text": '正文<span class="url-icon"><img alt="[笑]"></span><br>第二行',
              "created_at": "Thu Aug 27 20:21:28 +0800 2026", "user": {"screen_name": "博主"},
              "pics": [{"large": {"url": "https://wx1.sinaimg.cn/large/a.jpg"}}],
              "retweeted_status": {"id": "4", "text": "原文", "user": {"screen_name": "原作者"}}}
    session = types.SimpleNamespace(get=lambda url, **kw: R({"data": status}))
    art = extractors.extract(session, "https://weibo.com/123/5336621962235263")
    assert art.author == "博主" and art.title.startswith("正文[笑]")
    assert art.images[0]["attrs"]["src"].endswith("a.jpg")
    assert any(n.get("tag") == "blockquote" for n in art.nodes)


def test_extract_parsehub_image_and_dispatch(monkeypatch):
    class Resp:
        def __init__(self, data, status=200):
            self._d, self.status_code = data, status
            self.headers = {}

        def json(self):
            return self._d

        def close(self):
            pass

    # image post -> Article with images + caption
    calls = []

    def fake_get(u, **kw):
        calls.append((u, kw.get("params")))
        return Resp({"type": "image", "platform": "xhs", "title": "标题",
                     "content": "第一行\n第二行", "raw_url": "https://www.xiaohongshu.com/x",
                     "media": [{"url": "https://sns.xhscdn.com/1.jpg", "ext": "jpg"},
                               {"url": "https://sns.xhscdn.com/v.mp4", "ext": "mp4",
                                "thumb_url": "https://sns.xhscdn.com/v.jpg"}]})

    session = types.SimpleNamespace(get=fake_get)
    art = extractors.extract(session, "https://www.xiaohongshu.com/discovery/item/1",
                             parsehub_api="http://ph:8000")
    assert art.site == "小红书" and art.title == "标题"
    assert [n["tag"] for n in art.nodes] == ["p", "p", "figure", "figure"]
    # live-photo/video item falls back to its still thumbnail
    assert art.images[1]["attrs"]["src"].endswith("v.jpg")
    assert calls[0][0].endswith("/parse") and calls[0][1] == {"url": "https://www.xiaohongshu.com/discovery/item/1"}

    # video post via parsehub -> NotAnArticle: left to the video parser, and the
    # native extractor must NOT run (it would produce a junk page)
    def fake_get_video(u, **kw):
        return Resp({"type": "video", "media": {"url": "x.mp4"}})

    native_called = []

    def fake_native(s, u, t):
        native_called.append(u)
        return extractors.Article(url=u, title="native", nodes=[nodes.paragraph("x")], site="小红书")

    monkeypatch.setattr(extractors, "SITE_EXTRACTORS",
                        [(("xiaohongshu.com", "xhslink.com"), fake_native)])
    try:
        extractors.extract(types.SimpleNamespace(get=fake_get_video),
                           "https://www.xiaohongshu.com/discovery/item/2", parsehub_api="http://ph:8000")
        raise AssertionError("expected NotAnArticle for a video post")
    except extractors.NotAnArticle:
        pass
    assert native_called == []

    # parsehub 502 -> fall back to native extractor instead of failing
    native_called.clear()
    art = extractors.extract(types.SimpleNamespace(get=lambda u, **k: Resp({}, 502)),
                             "https://www.xiaohongshu.com/discovery/item/3", parsehub_api="http://ph:8000")
    assert native_called == ["https://www.xiaohongshu.com/discovery/item/3"]


# ------------------------------------------------------------- telegraph ---
def test_split_pages_respects_limit():
    big = [{"tag": "p", "children": ["字" * 3000]} for _ in range(40)]
    pages = telegraph.split_pages(big, limit=20000)
    assert len(pages) > 1 and all(telegraph._size(p) <= 20000 for p in pages)
    huge = telegraph.split_pages([{"tag": "p", "children": ["x" * 50000]}], limit=10000)
    assert telegraph._size(huge[0]) <= 10000


def test_create_page_chains_and_bootstraps_token(tmp_path):
    calls = []

    class S:
        proxies = {}

        def post(self, url, data, timeout):
            calls.append((url.rsplit("/", 1)[1], data))
            method = url.rsplit("/", 1)[1]
            if method == "createAccount":
                result = {"access_token": "tok"}
            else:
                result = {"url": f"https://telegra.ph/p{len(calls)}", "path": f"p{len(calls)}"}
            return types.SimpleNamespace(json=lambda: {"ok": True, "result": result})

    tg = telegraph.Telegraph(token_file=tmp_path / "t")
    tg.session = S()
    many = [{"tag": "p", "children": ["字" * 3000]} for _ in range(30)]
    url = tg.create_page("标题", many, author_url="https://src/")
    assert (tmp_path / "t").read_text() == "tok"
    pages = [c for c in calls if c[0] == "createPage"]
    assert len(pages) >= 2 and url == f"https://telegra.ph/p{len(calls)}"
    # last created page (first chunk) links to the previously created one
    assert "下一页" in pages[-1][1]["content"] and pages[-1][1]["title"].endswith(f"(1/{len(pages)})")


# ---------------------------------------------------------------- images ---
def _png(size=(50, 40), color=(255, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_compress_resizes_large_images():
    data, ext = images.compress(_png((3000, 2000)), 1600, 85)
    from PIL import Image
    assert ext == "jpg" and max(Image.open(io.BytesIO(data)).size) == 1600
    small = _png()
    assert images.compress(small, 1600, 85) == (small, "png")


def test_image_processor_rehosts_only_protected():
    good, blocked = _png(color=(0, 255, 0)), _png(color=(0, 0, 255))

    class Store:
        prefix = ""
        uploaded = None

        def upload(self, files, message):
            Store.uploaded = files
            return {p: "https://raw/" + p for p in files}

    proc = images.ImageProcessor(mode="protected", store=Store())

    def fake_get(url, referer, timeout):
        if "open.example" in url:
            return images.Downloaded(good, "image/png")
        if referer == images.TELEGRAPH_REFERER:
            return None  # hotlink protection
        return images.Downloaded(blocked, "image/png")

    proc._get = fake_get
    nodes_ = [{"tag": "img", "attrs": {"src": u}} for u in
              ("https://open.example/a.png", "https://blocked.example/b.png", "https://mmbiz.qpic.cn/c")]
    count = proc.process(nodes_, None, time.time() + 10, "t")
    assert count == 2
    assert nodes_[0]["attrs"]["src"] == "https://open.example/a.png"
    assert all(n["attrs"]["src"].startswith("https://raw/") for n in nodes_[1:])
    assert len(Store.uploaded) == 1  # identical bytes are uploaded once


# -------------------------------------------------------------- archiver ---
def test_archiver_skip_cache_and_footer(tmp_path, monkeypatch):
    cfg = load_config(None)
    cfg["image_host"]["type"] = "none"
    arch = Archiver(cfg, tmp_path)
    assert not arch.should_archive("https://v.douyin.com/x/")
    assert not arch.should_archive("https://support.weixin.qq.com/cgi-bin/x")
    created = []
    art = extractors.Article(url="https://a.com/x", title="T", nodes=[nodes.paragraph("hi")], site="S")
    monkeypatch.setattr("efb_telegraph_middleware.archiver.extract", lambda *a, **k: art)
    arch.telegraph.create_pages = lambda title, n, **kw: created.append(n) or [
        telegraph.PageRef("T", "https://telegra.ph/T", title, n)]
    assert arch.archive("https://a.com/x?utm_source=1") == "https://telegra.ph/T"
    assert arch.archive("https://a.com/x") == "https://telegra.ph/T"
    assert len(created) == 1
    text = json.dumps(created[0], ensure_ascii=False)
    assert "原文" in text and "存档于" in text


def test_archiver_publishes_first_then_edits_images(tmp_path, monkeypatch):
    cfg = load_config(None)
    arch = Archiver(cfg, tmp_path)
    img = {"tag": "img", "attrs": {"src": "https://mmbiz.qpic.cn/a"}}
    art = extractors.Article(url="https://mp.weixin.qq.com/s/x", title="T",
                             nodes=[{"tag": "figure", "children": [img]}], images=[img])
    monkeypatch.setattr("efb_telegraph_middleware.archiver.extract", lambda *a, **k: art)
    published, edited = [], []

    def create_pages(title, n, **kw):
        published.append(json.dumps(n))
        return [telegraph.PageRef("T", "https://telegra.ph/T", title, n)]

    arch.telegraph.create_pages = create_pages
    arch.telegraph.edit_pages = lambda pages: edited.append(json.dumps(pages[0].content))

    def fake_process(images_, referer, deadline, title):
        time.sleep(0.2)
        images_[0]["attrs"]["src"] = "https://raw/a.jpg"
        return 1

    arch.images.mode = "protected"
    arch.images.process = fake_process
    assert arch.archive("https://mp.weixin.qq.com/s/x") == "https://telegra.ph/T"
    assert "mmbiz.qpic.cn" in published[0] and not edited  # returned before re-hosting
    arch.executor.shutdown(wait=True)
    assert len(edited) == 1 and "https://raw/a.jpg" in edited[0]


def test_extract_falls_back_to_proxy(monkeypatch):
    import requests as rq
    seen = []

    def fetch(session, url, timeout=12, headers=None):
        seen.append(session)
        if session == "direct":
            raise rq.ConnectionError("reset")
        return Page(url, "<html><head><meta name=description content='足够长的描述内容在这里写着'>"
                         "</head><body></body></html>", "text/html")

    monkeypatch.setattr(extractors, "fetch_html", fetch)
    art = extractors.extract("direct", "https://blocked.site/a", fallback_session="proxy")
    assert seen == ["direct", "proxy"] and art is not None
    monkeypatch.setattr(extractors, "fetch_html", lambda *a, **k: (_ for _ in ()).throw(rq.ConnectionError("x")))
    assert extractors.extract("direct", "https://blocked.site/a") is None


def test_config_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_T", "secret")
    p = tmp_path / "c.yaml"
    p.write_text('image_host:\n  token: "${GH_T}"\n  repo: "${MISSING:-o/r}"\nskip_domains: [x.com]\n')
    cfg = load_config(p)
    assert cfg["image_host"]["token"] == "secret" and cfg["image_host"]["repo"] == "o/r"
    assert cfg["image_host"]["branch"] == "main" and "x.com" in cfg["skip_domains"]
    assert "douyin.com" in cfg["skip_domains"]
