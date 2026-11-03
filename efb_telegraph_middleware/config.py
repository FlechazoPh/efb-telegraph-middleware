"""Configuration loading with defaults and ``${ENV}`` expansion."""

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_SKIP_DOMAINS = [
    # handled by the video parser middleware / pure video sites
    "douyin.com", "iesdouyin.com", "b23.tv", "bilibili.com/video", "m.bilibili.com/video",
    "youtube.com", "youtu.be", "kuaishou.com", "ixigua.com", "v.qq.com", "tiktok.com",
    "channels.weixin.qq.com", "weixin.qq.com/sph",
    # not articles
    "support.weixin.qq.com", "open.weixin.qq.com", "wxapp.tenpay.com", "tenpay.com", "wxaurl.cn",
    "t.me", "telegram.me", "telegra.ph", "apps.apple.com", "play.google.com",
    "music.163.com", "y.qq.com", "kuwo.cn", "kugou.com",
    "item.jd.com", "item.m.jd.com", "share.m.jd.com", "u.jd.com", "item.taobao.com", "e.tb.cn",
    "m.tb.cn", "s.click.taobao.com", "detail.tmall.com", "yangkeduo.com", "pinduoduo.com",
    "meituan.com", "dianping.com", "map.qq.com", "amap.com", "map.baidu.com",
]

DEFAULTS: Dict[str, Any] = {
    "telegraph_token": "",          # empty: create an account automatically
    "telegraph_short_name": "EFB",
    "author_name": "",              # fallback author shown on pages
    "proxy": "",                    # proxy for fetching source pages and images
    "fallback_proxy": "",           # retry through this proxy when direct access is blocked
    "telegraph_proxy": "",          # proxy for api.telegra.ph
    "process_links": True,          # WeChat share cards (MsgType.Link)
    "process_text_urls": True,      # URLs inside text messages
    "max_text_urls": 3,
    "text_prefix": "📰 ",
    "skip_domains": [],             # extra domains/path prefixes to ignore
    "use_default_skip_domains": True,
    "only_domains": [],             # if set, archive only these domains
    "min_text_length": 80,          # generic pages with less readable text are skipped
    "timeout": 25,                  # seconds budget per link before the page is published
    "image_timeout": 300,           # background budget for re-hosting a page's images
    "cache_days": 30,               # reuse the same page for the same URL
    "footer": True,
    "image_host": {
        "type": "github",           # github | proxy | none
        "rehost": "protected",      # protected | all | none
        "repo": "",                 # owner/name, created automatically if missing
        "branch": "main",
        "token": "",
        "path_prefix": "",
        "private": False,           # must stay public, Telegraph loads images anonymously
        "url_template": "https://raw.githubusercontent.com/{repo}/{branch}/{path}",
        "proxy_template": "",       # for type=proxy, e.g. https://wsrv.nl/?url={url}
        "api_proxy": "",
        "max_side": 1600,
        "jpeg_quality": 85,
        "extra_protected_domains": [],
    },
}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    config = _expand(_merge(DEFAULTS, data))
    skip = list(config.get("skip_domains") or [])
    if config.get("use_default_skip_domains", True):
        skip = DEFAULT_SKIP_DOMAINS + skip
    config["skip_domains"] = skip
    return config
