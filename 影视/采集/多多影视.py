# -*- coding: utf-8 -*-
# @name 多多影视
# @author Doubao
# @description 多多影视：https://323433ssdfd.top ，Python版，接入首页、分类、搜索、详情与播放解码(WASM)链路
# @version 1.0.0
# @dependencies wasmtime
# -----------------------------------------------------------------------------
# 站点说明：
#   1. 全站接口位于 /api.php/web/...，请求需带固定头 X-Client 与 web-sign（网页前端
#      由 axios 拦截器统一注入，这里是按同样方式加在请求头上）。
#   2. 接口返回 UTF-8 编码 JSON，脚本内统一按 UTF-8 解码。
#   3. 反爬：当接口返回 HTTP 456/457/458 时，网页会跳验证页；本脚本不实现验证码绕过，
#      遇到该状态会重试一次，仍失败则返回空结果。
#   4. 播放：vod_play_url 里的地址是加密串，需先经 wasm（create_decode_request）编码成
#      protobuf 请求 POST 到 /api.php/web/decode/url，再解析响应得到真实 m3u8。
#      脚本运行时下载站方官方 wasm，并用 wasmtime 加载执行（与网页前端行为一致）。
# -----------------------------------------------------------------------------
import asyncio
import base64
import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from spider_runner import OmniBox, run

# ==================== 配置区域 ====================
HOST = os.environ.get("DUODUO_HOST", "https://323433ssdfd.top").rstrip("/")
UA = os.environ.get(
    "DUODUO_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
# 前端 axios 固定注入的两个头
CLIENT_ID = "8f3d2a1c7b6e5d4c9a0b1f2e3d4c5b6a"
WEB_SIGN = "ddtvf65f3a83d6d9ad6f"
# 首页各分类默认抓取条数
HOME_CATEGORY_PAGE_SIZE = 18
# 分类页大小（与前端一致，每页约 18 条；hasMore 按 >=18 判定）
CATEGORY_PAGE_SIZE = 18
# 反爬状态码：网页遇到这些会弹验证
ANTIBOT_STATUS = (456, 457, 458)

# 前端筛选项（按分类名组织，源自 TypePage 组件的 ie 配置）。
# T(start, extra) = 从今年倒推到 start，再追加 extra 年份段；与前端逻辑一致。
def _year_options(start, extra):
    this_year = time.localtime().tm_year
    years = [str(y) for y in range(this_year, start - 1, -1)]
    years.extend(extra)
    return years

FILTER_BY_TYPE_NAME = {
    "电影": {
        "class": ["动作", "喜剧", "爱情", "科幻", "恐怖", "悬疑", "犯罪", "战争", "动画", "冒险", "历史", "灾难", "纪录", "剧情"],
        "area": ["大陆", "香港", "台湾", "美国", "日本", "韩国", "泰国", "印度", "英国", "法国", "德国", "加拿大", "西班牙", "意大利", "澳大利亚"],
        "year": _year_options(2016, ["2015-2011", "2010-2000", "90年代", "80年代", "更早"]),
    },
    "剧集": {
        "class": ["爱情", "古装", "武侠", "历史", "家庭", "喜剧", "悬疑", "犯罪", "战争", "奇幻", "科幻", "恐怖"],
        "area": ["大陆", "香港", "台湾", "美国", "日本", "韩国", "泰国", "英国"],
        "year": _year_options(2021, ["2020-2016", "2015-2011", "2010-2000", "更早"]),
    },
    "综艺": {
        "class": ["真人秀", "音乐", "脱口秀", "歌舞", "爱情"],
        "area": ["大陆", "香港", "台湾", "美国", "日本", "韩国"],
        "year": _year_options(2011, ["更早"]),
    },
    "动漫": {
        "class": ["冒险", "奇幻", "科幻", "武侠", "悬疑"],
        "area": ["大陆", "日本", "欧美"],
        "year": _year_options(2011, ["更早"]),
    },
}
SORT_OPTIONS = [
    {"name": "人气", "value": "hits"},
    {"name": "最新", "value": "time"},
    {"name": "评分", "value": "score"},
    {"name": "年份", "value": "year"},
]
TYPE_NAMES = {}  # 运行时从首页接口填充 type_id -> type_name

def build_filters_by_type_id(type_id_to_name):
    """把按分类名组织的筛选项转换成 OmniBox 需要的按 type_id 组织。"""
    out = {}
    for tid, tname in (type_id_to_name or {}).items():
        cfg = FILTER_BY_TYPE_NAME.get(tname)
        if not cfg:
            continue
        out[str(tid)] = [
            {"key": "class", "name": "类型", "value": [{"name": "全部", "value": ""}] + [{"name": x, "value": x} for x in cfg["class"]]},
            {"key": "year", "name": "年份", "value": [{"name": "全部", "value": ""}] + [{"name": x, "value": x} for x in cfg["year"]]},
            {"key": "area", "name": "地区", "value": [{"name": "全部", "value": ""}] + [{"name": x, "value": x} for x in cfg["area"]]},
            {"key": "sort", "name": "排序", "value": SORT_OPTIONS},
        ]
    return out

# 兜底静态分类（接口失败时使用）；真正首页会用接口返回的真实映射
SITE_CLASS = [
    {"type_id": "1", "type_name": "电影"},
    {"type_id": "2", "type_name": "剧集"},
    {"type_id": "3", "type_name": "动漫"},
    {"type_id": "4", "type_name": "综艺"},
]
# 兜底筛选项（type_id=1 电影）
SITE_FILTERS = build_filters_by_type_id({c["type_id"]: c["type_name"] for c in SITE_CLASS})
# ==================== 配置区域结束 ====================

CACHE = {}
CACHE_LOCK = threading.Lock()

# ---------- 通用缓存 ----------
def get_cache(key, ttl_ms):
    hit = CACHE.get(key)
    if not hit:
        return None
    if time.time() * 1000 - hit["ts"] > ttl_ms:
        CACHE.pop(key, None)
        return None
    return hit["data"]

def set_cache(key, data):
    with CACHE_LOCK:
        CACHE[key] = {"ts": time.time() * 1000, "data": data}

async def log(level, message):
    try:
        await OmniBox.log(level, message)
    except Exception:
        pass

def clean_html_text(value):
    import html as _html
    text = _html.unescape(str(value or ""))
    text = text.replace("\xa0", " ")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()

# ---------- HTTP ----------
def _request(url, method="GET", body=None, headers=None, timeout=10000):
    hd = {
        "User-Agent": UA,
        "Accept": "application/json",
        "X-Client": CLIENT_ID,
        "web-sign": WEB_SIGN,
        "Referer": HOST + "/",
    }
    if headers:
        hd.update(headers)
    req = urllib.request.Request(url, data=body, headers=hd, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout / 1000) as resp:
            raw = resp.read()
            return int(resp.status), raw
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except Exception:
        return 0, b""

def _api(path, params=None, method="GET", body=None):
    url = f"{HOST}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if qs:
            url = f"{url}?{qs}"
    if body is not None:
        st, raw = _request(url, method=method, body=body, headers={
            "Content-Type": "application/x-protobuf", "Accept": "application/x-protobuf"})
    else:
        st, raw = _request(url, method=method)
    text = raw.decode("utf-8", "replace") if raw else ""
    if st in ANTIBOT_STATUS:
        return None, st
    try:
        return json.loads(text), st
    except Exception:
        return None, st

# ---------- 列表映射 ----------
def to_vod_list(items):
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("vod_id") or "")
        vname = str(item.get("vod_name") or "")
        if not vid or not vname:
            continue
        area = item.get("vod_area")
        if isinstance(area, list):
            area = ",".join(map(str, area))
        cls = item.get("vod_class")
        if isinstance(cls, list):
            cls = ",".join(map(str, cls))
        out.append({
            "vod_id": vid,
            "vod_name": vname,
            "vod_pic": str(item.get("vod_pic") or ""),
            "vod_remarks": str(item.get("vod_remarks") or ""),
            "vod_content": clean_html_text(item.get("vod_content") or item.get("vod_blurb") or ""),
            "vod_year": str(item.get("vod_year") or ""),
            "vod_area": str(area or ""),
            "vod_lang": str(item.get("vod_lang") or ""),
            "vod_director": clean_html_text(item.get("vod_director") or ""),
            "vod_actor": clean_html_text(item.get("vod_actor") or ""),
            "vod_douban_score": str(item.get("vod_douban_score") or ""),
            "type_name": str(item.get("type_name") or ""),
        })
    return out

def dedupe_vod_list(items, limit=0):
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        key = str(item.get("vod_id") or item.get("vod_name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if limit and len(out) >= limit:
            break
    return out

# ---------- 播放解码（wasmtime 加载官方 wasm） ----------
class WasmDecoder:
    _inst = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls._build()
            return cls._inst

    @staticmethod
    def _discover_wasm_url():
        candidates = [
            f"{HOST}/assets/web_app_wasm_bg-Bxwbrgev.wasm",
        ]
        # 尝试从首页 bundle 里探测当前 wasm 文件名（站点升级后哈希会变）
        try:
            req = urllib.request.Request(f"{HOST}/", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                html_text = r.read().decode("utf-8", "ignore")
            m = re.search(r'assets/(web_app_wasm_bg-[A-Za-z0-9_-]+\.wasm)', html_text)
            if m:
                candidates.insert(0, f"{HOST}/assets/{m.group(1)}")
            m2 = re.search(r'src="([^"]+\.js)"', html_text)
            if m2:
                js_url = m2.group(1)
                if js_url.startswith("http"):
                    pass
                else:
                    js_url = HOST + "/" + js_url.lstrip("/")
                req = urllib.request.Request(js_url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    js = r.read().decode("utf-8", "ignore")
                m3 = re.search(r'assets/(web_app_wasm_bg-[A-Za-z0-9_-]+\.wasm)', js)
                if m3:
                    candidates.insert(0, f"{HOST}/assets/{m3.group(1)}")
        except Exception:
            pass
        for url in candidates:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return url, r.read()
            except Exception:
                continue
        return None, None

    @classmethod
    def _build(cls):
        import wasmtime
        wasm_url, wasm_data = cls._discover_wasm_url()
        if not wasm_data:
            raise RuntimeError("无法获取解码 wasm")
        engine = wasmtime.Engine()
        store = wasmtime.Store(engine)
        module = wasmtime.Module(engine, bytes(wasm_data))

        def mk(params, results, cb):
            return wasmtime.Func(store, wasmtime.FuncType(params, results), cb)
        def noop(*a): return None
        def null_ref(*a): return None
        def ref_identity(*a): return a[0] if a else None
        def zero(*a): return 0
        def zero_i32(*a): return 0
        def now_f64(*a): return time.time() * 1000.0
        def raise_(*a): raise RuntimeError("wasm throw")

        I32 = wasmtime.ValType.i32()
        EXT = wasmtime.ValType.externref()
        F64 = wasmtime.ValType.f64()
        imps = []
        imps.append(mk([EXT, EXT], [], noop))       # randomFillSync
        imps.append(mk([EXT, EXT], [], noop))       # getRandomValues
        for _ in range(5): imps.append(mk([EXT], [EXT], ref_identity))  # crypto/process/versions/node/msCrypto
        imps.append(mk([], [EXT], null_ref))        # require
        imps.append(mk([I32, I32], [EXT], null_ref))# new_no_args
        imps.append(mk([EXT, EXT], [EXT], null_ref))# call_389
        imps.append(mk([EXT, EXT, EXT], [EXT], null_ref))  # call_4708
        imps.append(mk([], [F64], now_f64))         # now
        imps.append(mk([EXT], [I32], zero_i32))     # length
        imps.append(mk([I32, I32, EXT], [], noop))  # prototypesetcall
        imps.append(mk([I32], [EXT], null_ref))     # new_with_length
        imps.append(mk([EXT, I32, I32], [EXT], null_ref))  # subarray
        for _ in range(4): imps.append(mk([], [I32], zero))  # static accessors
        imps.append(mk([I32, I32], [], raise_))     # wbindgen_throw
        for _ in range(4): imps.append(mk([EXT], [I32], zero_i32))  # is_*
        imps.append(mk([], [], noop))               # init_externref_table
        for _ in range(2): imps.append(mk([I32, I32], [EXT], null_ref))  # casts

        inst = wasmtime.Instance(store, module, imps)
        ex = inst.exports(store)
        try:
            ex["__wbindgen_start"](store)
        except Exception:
            pass
        memory = ex["memory"]
        obj = object.__new__(cls)
        obj.store = store
        obj.memory = memory
        obj.malloc = ex["__wbindgen_malloc"]
        obj.cdr = ex["create_decode_request"]
        obj.pdr = ex["parse_decode_response"]
        obj.getters = {n: ex[n] for n in [
            "decoderesult_code", "decoderesult_error_code",
            "decoderesult_data", "decoderesult_msg", "__wbg_decoderesult_free"]}
        return obj

    def _write(self, data):
        b = data if isinstance(data, bytes) else data.encode("utf-8")
        p = self.malloc(self.store, len(b), 1)
        self.memory.write(self.store, b, p)
        return p, len(b)

    def _read(self, ptr, size):
        if size <= 0:
            return b""
        return bytes(self.memory.read(self.store, ptr, ptr + size))

    def create_decode_request(self, url, play_from):
        p1, l1 = self._write(url)
        p2, l2 = self._write(play_from)
        dptr, dlen = self.cdr(self.store, p1, l1, p2, l2)
        return self._read(dptr, dlen)

    def parse_decode_response(self, data):
        ptr, n = self._write(data)
        sp, e1, e2 = self.pdr(self.store, ptr, n)
        if e2:
            return None
        code = self.getters["decoderesult_code"](self.store, sp)
        ec = self.getters["decoderesult_error_code"](self.store, sp)
        dp, dl = self.getters["decoderesult_data"](self.store, sp)
        mp, ml = self.getters["decoderesult_msg"](self.store, sp)
        out = {
            "code": code,
            "error_code": ec,
            "data": self._read(dp, dl).decode("utf-8", "replace"),
            "msg": self._read(mp, ml).decode("utf-8", "replace"),
        }
        try:
            self.getters["__wbg_decoderesult_free"](self.store, sp, 0)
        except Exception:
            pass
        return out

async def decode_play_url(url, play_from, retries=2):
    """wasm 解码播放地址。失败自动重试；返回 None 表示彻底失败，返回 '' 表示明确无可用地址。"""
    if not url:
        return ""
    last_err = ""
    for attempt in range(retries + 1):
        try:
            dec = WasmDecoder.get()
            proto = dec.create_decode_request(url, play_from)
            if not proto:
                last_err = "empty proto"
                continue
            st, resp = _request(f"{HOST}/api.php/web/decode/url", method="POST", body=proto,
                                headers={"Content-Type": "application/x-protobuf", "Accept": "application/x-protobuf",
                                         "Referer": HOST + "/"},
                                timeout=15000)
            if st in ANTIBOT_STATUS or not resp:
                last_err = f"http {st}"
                await asyncio.sleep(0.6)
                continue
            result = dec.parse_decode_response(resp)
            if result and result.get("code") == 1 and result.get("data"):
                m3u8 = str(result["data"]).strip()
                if m3u8.startswith(("http://", "https://")):
                    if attempt > 0:
                        await log("info", f"[多多][play] decode success on attempt {attempt + 1}")
                    return m3u8
                last_err = f"bad url: {m3u8[:60]}"
            else:
                last_err = f"code={result.get('code') if result else None} msg={result.get('msg') if result else ''}"
        except Exception as e:
            last_err = str(e)
        if attempt < retries:
            await asyncio.sleep(0.5)
    await log("warn", f"[多多][play] decode failed after {retries + 1} attempts: {last_err} url={url[:80]} frm={play_from}")
    return None

# ---------- 详情：解析播放源 ----------
def build_sources(play_from, play_url, vodplayer, vod_id="", vod_name=""):
    sources = []
    player_map = {}
    if isinstance(vodplayer, list):
        for p in vodplayer:
            if isinstance(p, dict) and p.get("from"):
                player_map[str(p["from"])] = p
    from_list = [x.strip() for x in str(play_from or "").split("$$$") if x.strip()]
    url_groups = [x.strip() for x in str(play_url or "").split("$$$") if x.strip()]
    for idx, frm in enumerate(from_list):
        meta = player_map.get(frm) or {}
        show = str(meta.get("show") or frm).strip() or frm
        decode_status = str(meta.get("decode_status") or "")
        group = url_groups[idx] if idx < len(url_groups) else ""
        episodes = []
        for k, ep in enumerate(group.split("#"), start=1):
            ep = ep.strip()
            if not ep:
                continue
            name, _, url = ep.partition("$")
            name = (name or f"第{k}集").strip()
            url = url.strip()
            if not url:
                continue
            meta_enc = base64.urlsafe_b64encode(json.dumps({
                "url": url, "frm": frm, "decode": decode_status,
                "name": name, "vod_id": str(vod_id or ""), "vod_name": str(vod_name or ""),
                "from": show,
            }, ensure_ascii=False).encode("utf-8")).decode("utf-8")
            episodes.append({
                "name": name,
                "playId": f"{show}|{idx + 1}|{k}|{meta_enc}",
            })
        if episodes:
            sources.append({"name": show, "episodes": episodes})
    return sources

# ---------- 首页 ----------
async def home(params, context):
    try:
        cached = get_cache("home", 30000)
        if cached is not None:
            return cached
        data, _ = _api("/api.php/web/index/home")
        if not isinstance(data, dict) or data.get("code") != 200:
            return {"class": SITE_CLASS, "filters": SITE_FILTERS, "list": []}
        payload = data.get("data") or {}
        classes = []
        merged = []
        for cat in payload.get("categories") or []:
            if not isinstance(cat, dict):
                continue
            tid = str(cat.get("type_id") or "").strip()
            tname = str(cat.get("type_name") or "").strip()
            if tid and tname:
                classes.append({"type_id": tid, "type_name": tname})
                TYPE_NAMES[tid] = tname
            merged.extend(to_vod_list(cat.get("videos") or []))
        # 轮播/推荐
        for item in (payload.get("banner") or payload.get("swiper") or []):
            if isinstance(item, dict) and (item.get("vod_id") or item.get("id")):
                merged.append({
                    "vod_id": str(item.get("vod_id") or item.get("id")),
                    "vod_name": str(item.get("vod_name") or item.get("name") or ""),
                    "vod_pic": str(item.get("vod_pic") or item.get("pic") or ""),
                    "vod_remarks": str(item.get("vod_remarks") or "轮播推荐"),
                })
        type_id_to_name = {c["type_id"]: c["type_name"] for c in classes} if classes else {}
        dynamic_filters = build_filters_by_type_id(type_id_to_name) or SITE_FILTERS
        result = {
            "class": classes or SITE_CLASS,
            "filters": dynamic_filters,
            "list": dedupe_vod_list(merged),
        }
        set_cache("home", result)
        return result
    except Exception as e:
        await log("error", f"[多多][home] {e}")
        return {"class": SITE_CLASS, "filters": SITE_FILTERS, "list": []}

# ---------- 分类 ----------
async def category(params, context):
    try:
        type_id = str(params.get("categoryId") or params.get("type_id") or "1")
        page = params.get("page") or 1
        try:
            page = int(page)
        except Exception:
            page = 1
        extend = params.get("extend") or params.get("filters") or {}
        if isinstance(extend, str):
            try:
                extend = json.loads(extend)
            except Exception:
                extend = {}
        cache_key = f"cat:{type_id}:{page}:{json.dumps(extend, ensure_ascii=False, sort_keys=True)}"
        cached = get_cache(cache_key, 30000)
        if cached is not None:
            return cached
        data, _ = _api("/api.php/web/filter/vod", {
            "type_id": type_id, "page": page, "sort": extend.get("sort") or "hits",
            "class": extend.get("class"), "area": extend.get("area"), "year": extend.get("year"),
        })
        items = to_vod_list(data.get("data") if isinstance(data, dict) else [])
        # 接口直接返回 total/pageCount/limit，优先使用真实分页
        total = int(data.get("total") or 0) if isinstance(data, dict) else 0
        page_count = int(data.get("pageCount") or 0) if isinstance(data, dict) else 0
        if not page_count:
            page_count = page + 1 if len(items) >= CATEGORY_PAGE_SIZE else page
        if not total:
            total = page * CATEGORY_PAGE_SIZE + len(items) if items else 0
        result = {
            "page": page,
            "pagecount": page_count,
            "total": total,
            "list": items,
        }
        set_cache(cache_key, result)
        return result
    except Exception as e:
        await log("error", f"[多多][category] {e}")
        return {"page": 1, "pagecount": 0, "total": 0, "list": []}

# ---------- 详情 ----------
async def detail(params, context):
    try:
        ids = [x.strip() for x in str(params.get("videoId") or params.get("id") or "").split(",") if x.strip()]
        if not ids:
            return {"list": []}
        out = []
        for vid in ids:
            cache_key = f"detail:{vid}"
            item = get_cache(cache_key, 120000)
            if item is None:
                data, _ = _api("/api.php/web/vod/get_detail", {"vod_id": vid})
                if not isinstance(data, dict) or data.get("code") != 200:
                    continue
                rows = data.get("data") or []
                if not rows:
                    continue
                d = rows[0]
                sources = build_sources(d.get("vod_play_from"), d.get("vod_play_url"), data.get("vodplayer"),
                                        vod_id=str(d.get("vod_id") or vid), vod_name=str(d.get("vod_name") or ""))
                area = d.get("vod_area")
                if isinstance(area, list):
                    area = ",".join(map(str, area))
                cls = d.get("vod_class")
                if isinstance(cls, list):
                    cls = ",".join(map(str, cls))
                item = {
                    "vod_id": str(d.get("vod_id") or vid),
                    "vod_name": str(d.get("vod_name") or ""),
                    "type_id": str(d.get("type_id") or ""),
                    "type_name": str(d.get("type_name") or TYPE_NAMES.get(str(d.get("type_id")), "")),
                    "vod_pic": str(d.get("vod_pic") or ""),
                    "vod_remarks": str(d.get("vod_remarks") or ""),
                    "vod_year": str(d.get("vod_year") or ""),
                    "vod_area": str(area or ""),
                    "vod_lang": str(d.get("vod_lang") or ""),
                    "vod_director": clean_html_text(d.get("vod_director") or ""),
                    "vod_actor": clean_html_text(d.get("vod_actor") or ""),
                    "vod_douban_score": str(d.get("vod_douban_score") or ""),
                    "vod_content": clean_html_text(d.get("vod_content") or ""),
                    "other": "/".join([x for x in [str(area or ""), str(d.get("vod_lang") or "")] if x]),
                    "vod_play_sources": sources,
                }
                set_cache(cache_key, item)
            if item:
                out.append(item)
        return {"list": out}
    except Exception as e:
        await log("error", f"[多多][detail] {e}")
        return {"list": []}

# ---------- 搜索 ----------
async def search(params, context):
    try:
        page = params.get("page") or 1
        try:
            page = int(page)
        except Exception:
            page = 1
        wd = str(params.get("keyword") or params.get("wd") or params.get("key") or "").strip()
        if not wd:
            return {"page": page, "pagecount": page, "total": 0, "list": []}
        cache_key = f"search:{page}:{wd}"
        cached = get_cache(cache_key, 20000)
        if cached is not None:
            return cached
        data, _ = _api("/api.php/web/search/index", {"wd": wd, "page": page, "limit": 15})
        items = to_vod_list(data.get("data") if isinstance(data, dict) else [])
        result = {
            "page": page,
            "pagecount": page + 1 if items else page,
            "total": len(items),
            "list": items,
        }
        set_cache(cache_key, result)
        return result
    except Exception as e:
        await log("error", f"[多多][search] {e}")
        return {"page": 1, "pagecount": 0, "total": 0, "list": []}

# ---------- 播放 ----------
# 播放地址缓存时间：m3u8 token 通常有效期较长，缓存 10 分钟减少重复解码
PLAY_CACHE_TTL_MS = 10 * 60 * 1000

async def _warm_wasm():
    """后台预热 wasm，避免首次播放时冷启动。"""
    try:
        WasmDecoder.get()
    except Exception:
        pass

async def _record_history(vod_id, vod_name, episode_name, play_url, header):
    """记录播放历史（直接 await，不做后台 task，避免被 runtime 丢弃）。"""
    try:
        fn = getattr(OmniBox, "addPlayHistory", None)
        if not callable(fn):
            await log("warn", f"[多多][history] OmniBox.addPlayHistory not available")
            return
        resource_id = str(vod_id or vod_name or play_url or "")
        payload = {
            "episode": episode_name or "播放",
            "episodeName": episode_name or "播放",
            "playUrl": play_url,
            "playHeader": header or {},
        }
        await fn(resource_id, vod_name or "多多影视", payload)
        await log("info", f"[多多][history] recorded vod={vod_name} ep={episode_name}")
    except Exception as e:
        await log("warn", f"[多多][history] failed: {e}")

async def play(params, context):
    try:
        raw = str(params.get("playId") or params.get("id") or "").strip()
        if not raw:
            return {"parse": 0, "url": "", "urls": [], "header": {}}
        parts = raw.split("|")
        meta_enc = parts[-1]
        try:
            meta = json.loads(base64.urlsafe_b64decode(meta_enc.encode("utf-8")).decode("utf-8"))
        except Exception:
            return {"parse": 0, "url": "", "urls": [], "header": {}}
        url = str(meta.get("url") or "")
        frm = str(meta.get("frm") or "")
        name = str(meta.get("name") or "播放")
        decode_status = str(meta.get("decode") or "")
        vod_id = str(meta.get("vod_id") or "")
        vod_name = str(meta.get("vod_name") or "")
        show = str(meta.get("from") or frm or "播放")

        cache_key = f"play:{meta_enc}"
        play_url = get_cache(cache_key, PLAY_CACHE_TTL_MS)
        if play_url:
            header = {"User-Agent": UA, "Referer": f"{HOST}/detail/{vod_id or ''}"}
            await _record_history(vod_id, vod_name, name, play_url, header)
            return {
                "parse": 0, "url": play_url,
                "urls": [{"name": name, "url": play_url}],
                "header": header,
            }

        # 异步预热 wasm（即使 decode_status 不需要解码也加载一下，切线路时不卡）
        asyncio.create_task(_warm_wasm())

        decoded = None
        if decode_status in ("1", "2"):
            decoded = await decode_play_url(url, frm)
        if decoded:
            play_url = decoded
        else:
            # 降级：wasm 解码失败/不需要解码时，如果原 url 本身是 http(s) 直链，直接用
            if url.startswith(("http://", "https://")):
                play_url = url
                if decode_status in ("1", "2"):
                    await log("warn", f"[多多][play] fallback to raw url after decode failed: {url[:80]}")
            else:
                play_url = ""

        if not play_url:
            return {"parse": 0, "url": "", "urls": [], "header": {}}

        set_cache(cache_key, play_url)
        header = {"User-Agent": UA, "Referer": f"{HOST}/detail/{vod_id or ''}"}
        await log("info", f"[多多][play] vod={vod_name} ep={name} frm={show} -> {play_url[:80]}")
        await _record_history(vod_id, vod_name, name, play_url, header)
        return {
            "parse": 0, "url": play_url,
            "urls": [{"name": name, "url": play_url}],
            "header": header,
        }
    except Exception as e:
        await log("error", f"[多多][play] {e}")
        return {"parse": 0, "url": "", "urls": [], "header": {}}

if __name__ == "__main__":
    run({"home": home, "category": category, "detail": detail, "search": search, "play": play})
