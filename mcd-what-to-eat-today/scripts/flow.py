#!/usr/bin/env python3
"""门店服务流程状态机 S0–S9。

## 为什么有这个东西
2026-10-10 21:27 首次真人走查,四轮对话把整件事压成一张 60 行的确认卡丢给用户:
渠道没问、地址没确认、门店没让选、选项要读两遍。
根因是把**门店点单**当成了表单。门店点单是串行状态机:店员一句、顾客一句、推进一位。

## 流程(对照《麦当劳操作与训练手册》服务程序)
```
S0 迎宾      手册①「欢迎光临 / 喜欢您来」
S1 渠道      手册③「堂吃还是外带」—— 🔴 必问,不能靠历史占比替用户决定
S2 门店      到店系(orderType=1):确认去哪家
S3A 地址     外送系:送到哪 —— 🔴 必问,未确认地址不得查门店(2026-10-11 用户反馈)
S3B 门店     外送系:谁家送 —— 手册④复诵;全部打烊走 S3Z 如实报开门时间
S4 放置点    外送系:放哪儿 → 写进 create-order.remark
S5 点餐      手册③「您要点什么」+ 只建议一次
S6 细节      每轮只问一件,最多 2 轮
S7 复诵报价  手册④⑤ 复诵 + 报价,🔴 必须独立一轮
S8 支付      支付发生在麦当劳 APP,Skill 不碰钱
S9 送客      手册⑧「祝您用餐愉快」
```

## 硬纪律
1. **编号只在上一条消息内有效**,跨轮立即作废(`self.options` 每轮重置)
2. **一次只问一件**,被否定立刻换方向,不连续追问
3. **只陈述不预测**(实测点单间隔 1~16 天,方差过大,不做「你该吃了」)
4. **渠道必须问**,历史置信度只决定选项排序,不给免问权
5. 所有 API 调用可注入,`test_flow.py` 用假 API 跑,不碰网络

用法:
    from flow import Flow
    f = Flow(profile)
    print(f.turn_text())          # S0
    print(f.reply("3").text)      # 送到家
"""
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_mobile as rm  # noqa: E402
import voice as vc          # noqa: E402  B1:口播工具(中文数字/口语归一/复述确认)
import menu_browse as mb    # noqa: E402  2026-10-11:S5 看菜单(分类→翻页→选品)
from channel import (CHANNELS, addresses_rich, channel_params,  # noqa: E402
                     delivery_stores, nearby_stores, next_slot, opens_next,
                     pick_store, pick_when, resolve_channel)

DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "assets", "profile.json")
# 用户明确声明的偏好/拒单落这里;fetch_profile 的 build_avoid 只读合并。
# 🔴 只有 CLI / 演示驱动才调 flush_declared() 落盘,库调用(含测试)不写文件。
DECLARED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "assets", "declared.json")

BRAND = "喜欢您来♥️"
FAREWELL = "喜欢您来，慢走。"

# S1 四个选项 → (渠道, takeWayCode)。takeWayCode 只能从 calculate-price 的
# takeWayList 取,这里的取值与实测一致,首次组装时仍会向 takeWayList 核对。
CHANNEL_MENU = [
    {"key": "堂食", "channel": "到店自取", "takeWay": "eat-in"},
    {"key": "外带", "channel": "到店自取", "takeWay": "take-in-store"},
    {"key": "送到家", "channel": "麦乐送", "takeWay": None},
    {"key": "得来速", "channel": "得来速", "takeWay": "take-dt-quick"},
]
DROPOFF = [("门口", "放门口"), ("前台", "放前台"),
           ("门口+电话", "放门口,电话联系")]

# 地址一页最多展示几个(≤4 选项协议,留 1 个位置给「更多地址」)
ADDR_PAGE_SIZE = 3


# ------------------------------------------------------------------ 真 API
class McdApi:
    """真实 MCP 调用。所有网络访问集中在这里,便于测试替身。"""

    def __init__(self, call):
        self._call = call

    def addresses(self):
        return addresses_rich()

    def delivery_stores(self, address_id):
        return delivery_stores(address_id, 2)

    def nearby(self, city, keyword, be_type):
        return nearby_stores(be_type, city, keyword)

    def menu(self, store_code, params, when):
        args = dict(params, storeCode=store_code, reservationDate=when)
        return (self._call("query-meals", args).get("data") or {}).get("meals") or {}

    def menu_full(self, store_code, params, when):
        """meals + 服务端 categories(S5 看菜单用,2026-10-11)。

        与 menu() 各发一次 query-meals;看菜单是低频动作,不为此改 menu() 的
        既有调用方与 FakeApi 契约。
        """
        args = dict(params, storeCode=store_code, reservationDate=when)
        d = self._call("query-meals", args).get("data") or {}
        return {"meals": d.get("meals") or {}, "categories": d.get("categories") or []}

    def detail(self, code, store_code, params, when):
        return self._call("query-meal-detail",
                          dict(params, code=code, storeCode=store_code,
                               reservationDate=when)).get("data") or {}

    def quote(self, store_code, params, items):
        return self._call("calculate-price",
                          dict(params, storeCode=store_code, items=items)).get("data") or {}

    def nutrition(self):
        from recommend import fetch_nutrition
        return fetch_nutrition()


def default_api():
    from mcd_client import call_tool
    return McdApi(call_tool)


# ------------------------------------------------------------------ 工具
def load_profile(path=None):
    p = path or DEFAULT_PROFILE
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


_ADDR_NOTE = re.compile(r"[（(][^）)]*[）)]")


def compact_address(full, limit=13):
    """把地址压成能进编号选项的短标签。完整地址在 S3 复诵轮展示。"""
    s = re.sub(r"^(广东省|上海市|北京市|天津市|重庆市)?", "", full or "")
    s = re.sub(r".*?市", "", s, count=1)
    s = s.strip()
    if rm.width(s) <= limit:
        return s
    head, tail = s[:int(limit * 0.6)], s[-int(limit * 0.3):]
    return f"{head}…{tail}"


def _short_channel(name):
    return {"到店自取": "到店", "麦乐送": "外送", "得来速": "得来速",
            "企业团餐": "团餐"}.get(name, name)


def store_label(s, limit=12):
    """门店短标签。「麦当劳上海静安沪太路341号餐厅」→「上海静安沪太路341号」。

    超长时**保留尾部**——地址的区分度在后面那截,砍头会变成同一家店。
    """
    n = re.sub(r"^麦当劳", "", (s or {}).get("storeName") or "?")
    n = re.sub(r"(餐厅|得来速餐厅)$", "", n)
    if rm.width(n) <= limit:
        return n
    return n[-int(limit):]


_BRACKET = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_COMBO_KW = ("套餐", "单人餐", "三件套", "四件套", "五件套", "双人餐", "组合",
             "全家桶", "欢享餐", "桶")


def _norm_name(name):
    """只剥括号里的变体描述,保留「单人餐」这类形态词。

    🔴 不能直接 short_name —— 它会把「经典安格斯单人餐」压成「安格斯」,
       套餐与同名单品就此无法区分(23:22 实测事故的根因)。
    """
    return _BRACKET.sub("", name or "").strip()


def _applicable(sig, name):
    """按品类裁剪签名:汉堡挂去芝士,饮料挂冰量,其余不挂。"""
    n = name or ""
    if any(k in n for k in ("堡", "汉堡")):
        return {"去芝士": sig.get("去芝士")}
    if any(k in n for k in ("可乐", "雪碧", "橙汁", "茶", "咖啡", "美式", "牛奶", "豆浆")):
        return {"少冰": sig.get("少冰")}
    return {}


def _mount(holder, sig):
    """按实测两类结构组装 modification.values,返回 (values, notes, 是否成功去掉芝士)。"""
    values, notes, removed = [], [], False
    groups = (holder.get("modification") or {}).get("items") or []
    if not groups:
        return [], ["无可定制项"], False
    for grp in groups:
        vals = grp.get("values") or []
        if any(v.get("unselectedKey") for v in vals):     # A 型:全量传
            for v in vals:
                k = v.get("selectedKey")
                if sig.get("去芝士") and v.get("name") in ("吉士", "高达干酪片"):
                    k = v.get("unselectedKey")
                    removed = True
                    notes.append(f"去{v.get('name')}")
                values.append({"code": v.get("code"), "key": k, "quantity": 1})
        else:                                            # B 型:单选
            want = sig.get("少冰")
            hit = None
            if want == "去冰":
                hit = next((v for v in vals if v.get("name") == "去冰"), None)
            elif want is True:
                hit = next((v for v in vals if v.get("name") == "少冰"), None)
            if hit is None:
                hit = next((v for v in vals if v.get("name") == "标准"), None)
            hit = hit or (vals[0] if vals else None)
            if hit:
                notes.append(f"冰量={hit.get('name')}")
                values.append({"code": hit.get("code"),
                               "key": hit.get("selectedKey"), "quantity": 1})
    return values, notes, removed


_DROP_LABEL = ("麦金卡", "免配", "免打包", "服务费", "精选", "超值", "升级")


def _menu_label(name, limit=8):
    """点餐选项里的商品名。**必须保留套餐形态词**(单人餐/三件套/四件套/中套)。

    🔴 2026-10-10 23:25 用户明确要求:「你要告诉我 这是套餐还是单点一个汉堡」。
       之前的 `short_name()` 把「单人餐」当噪音删掉,标签只剩「安格斯」,
       套餐和同名单品在屏幕上长得一模一样。
       → 形态词一律保留;修饰词才删。超长时砍头(尾部是区分度)。
    """
    s = _norm_name(name)
    for n in _DROP_LABEL:
        s = s.replace(n, "")
    for n in ("经典", "风味"):
        if s.startswith(n):
            s = s[len(n):]
            break
    s = re.sub(r"\s+", "", s)
    if rm.width(s) <= limit:
        return s
    # 🔴 超长时**砍头留尾** —— 形态词在尾部(单人餐/中套/件套),
    #    砍尾会把「这是套餐」这个最关键的信息砍掉(23:25 用户要求)。
    return s[-int(limit):]


def _is_combo(name):
    return any(k in (name or "") for k in _COMBO_KW)


def _yuan(v):
    """分 → 元。calculate-price 返回单位是分,create-order 是元,两个接口不一致。"""
    try:
        return round(float(v) / 100, 2)
    except (TypeError, ValueError):
        return None


def _slot_of(hour):
    for name, lo, hi in (("早餐", 6, 10), ("午餐", 10, 15),
                         ("下午茶", 15, 17), ("晚餐", 17, 21)):
        if lo <= hour < hi:
            return name
    return "夜宵" if hour >= 21 else "早餐"


def _mins(hhmm):
    """'HH:MM' → 当日分钟数。"""
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


class Turn:
    """一轮对话。text 给眼睛(带编号/审计行),speech 给耳朵(口语)。

    B1(2026-10-11):speech 与 text 同源产出 —— 都来自同一批变量,
    不允许 speech 从 text 反向剥离生成(剥离会把编号、代码念出来)。
    硬约束(voice.py 四条):单句≤30字(约6秒)、不念编号、零内部信息、
    识别歧义必须复述确认。test_flow.py 逐轮断言。
    """

    def __init__(self, state, text, options=None, meta=None, speech=None):
        self.state = state
        self.text = text
        self.options = list(options or [])
        self.meta = meta or {}
        self.speech = (speech or "").strip() or None

    def __repr__(self):
        return f"<Turn {self.state} opts={self.options}>"


class Flow:
    def __init__(self, profile=None, api=None, city="上海市",
                 keyword=None, at=None, profile_path=None):
        self.p = profile if profile is not None else load_profile(profile_path)
        self.api = api or default_api()
        self.city = city
        self.keyword = keyword or self._derive_keyword()
        self.now = at or datetime.now()
        self.state = "S0"
        self.options = []              # 🔴 每轮重置,编号不跨轮
        self.ctx = {"turns": []}
        self.selection = None          # S1 选中的渠道
        self.store = None
        self.address = None
        self.dropoff = None
        self.order = None              # {items, quote, recite}

    # -------------------------------------------------- 内部
    def _derive_keyword(self):
        """从历史主店名里取区域词。取不到就用城市名。"""
        name = (self.p.get("topStore") or "")
        m = re.search(r"麦当劳([一-龥]{2,3}?)([一-龥]{2})(?:[一-龥]*?路|店|餐厅)", name)
        if m:
            return m.group(2)
        m = re.search(r"([一-龥]{2,3})(?:[一-龥]*路|店)", name)
        return m.group(1) if m else self.city

    def _sig_on(self):
        sig = {k: v.get("level") in ("declared", "high")
               for k, v in (self.p.get("signature") or {}).items()}
        sig.update(self.ctx.get("sig_override") or {})
        return sig

    def _mark(self, new_state):
        """推进到 new_state 并记账。

        🔴 不能写成 `self.state = self._mark("__HERE__")` —— Python 先求值 RHS,
        那样返回的是**上一个**状态,整条状态机停在 S0 原地踏步。
        """
        self.state = new_state
        self.ctx["turns"].append(new_state)
        return new_state

    def _pick(self, reply):
        """把用户回复映射到本轮选项。返回索引或 None。

        🔴 只认**本轮** options。上一轮的编号在本轮已作废,
           靠这一层防止「上一轮的 1 被当成本轮的 1」这种串轮。
        """
        s = (reply or "").strip()
        if not s:
            return None
        m = re.fullmatch(r"\s*([1-9])\s*", s)
        if m:
            i = int(m.group(1)) - 1
            return i if 0 <= i < len(self.options) else None
        for i, o in enumerate(self.options):
            if o and (o in s or s in o):
                return i
        return None

    # -------------------------------------------------- S0
    def greet(self):
        """门店迎宾。品牌招呼语 + 一件用户不知道服务端知道的事。"""
        orders = self.p.get("orders") or []
        if not orders:
            self.state = self._mark("S0")
            self.options = []
            return Turn(self.state, rm.turn(
                f"{BRAND}，第一次见面。",
                facts=["先问三件事，以后不用再说"],
                options=["牛/鸡/鱼/都行", "汉堡要不要去芝士", "饮料冰量"]),
                speech="喜欢您来，第一次见面。")
        last = datetime.strptime(orders[0]["createTime"], "%Y-%m-%d %H:%M:%S")
        days = (self.now - last).days
        hours = (self.now - last).total_seconds() / 3600
        slot = _slot_of(self.now.hour)
        facts, spoken = [], []
        if hours < 1:
            facts.append("刚吃完没多久")
            spoken.append("喜欢您来，刚吃完没多久。")
        elif hours < 12:
            facts.append(f"{int(hours)} 小时前刚吃过")
            spoken.append(f"喜欢您来，{vc.cn_count(int(hours))}小时前刚吃过。")
        elif days <= 1:
            facts.append("昨天刚吃过")
            spoken.append("喜欢您来，昨天刚吃过。")
        elif days <= 60:
            facts.append(f"{days} 天没见了")
            spoken.append(f"喜欢您来，{vc.cn_count(days)}天没见了。")
        else:
            facts.append("好久不见了")
            spoken.append("喜欢您来，好久不见了。")
        if slot == "夜宵":
            facts.append("这个点还没吃吧？")
            spoken.append("这个点还没吃吧？")
        elif slot == "早餐":
            facts.append("今天早点解决，还是照旧")
            spoken.append("今天早点解决，还是照旧？")
        self.state = self._mark("S0")
        self.options = []
        return Turn(self.state, rm.turn(f"{BRAND}，{facts[0]}。",
                                        facts=facts[1:]),
                    speech=vc.sayable(" ".join(spoken), 60))

    # -------------------------------------------------- S1
    def ask_channel(self):
        """🔴 必问。历史置信度只决定星标,不给免问权。"""
        name, why = resolve_channel(self.p, None)
        # 迎宾语已在 turn_text 里拼在前面,这里不再重复
        order = [c for c in CHANNEL_MENU if c["channel"] != "来得速"]
        # 历史众数加★标,**只影响排序,不免问**(手册③「堂吃还是外带」是必问项)
        order.sort(key=lambda c: 0 if c["channel"] == name else 1)
        opts = [c["key"] + ("★" if c["channel"] == name else "") for c in order]
        self.state = self._mark("S1")
        self.options = [c["key"] for c in order]
        # 🔴 2026-10-10 23:18 修:原来 `self.selection = CHANNEL_MENU[i]`
        #    用**排序后**的下标去索引**未排序**的常量表,导致显示与动作错位:
        #    屏幕上是「1 送到家」,实际拿到的是 CHANNEL_MENU[0]=堂食。
        #    → 必须从排序后的 order 里取。
        self._channel_order = order
        self.ctx["why"] = why
        hist = (self.p.get("channel") or {})
        facts = [f"常订{_short_channel(name)}"] if hist.get("confidence") == "high" else None
        return Turn(self.state, rm.turn("今天店里吃，还是送到家？",
                                        facts=facts, options=opts),
                    speech="今天店里吃，还是送到家？")

    # -------------------------------------------------- S2 到店系
    def ask_store_instore(self):
        be = CHANNELS[self.selection["channel"]]["beType"]
        st = self.api.nearby(self.city, self.keyword, be)
        store = pick_store(st, at=self.now)
        if not store:
            st2 = self.api.nearby(self.city, self.city, be)
            store = pick_store(st2, at=self.now)
        if not store:
            names = "、".join(f"{x.get('storeName','')} "[:14]
                             for x in st[:2])
            raise RuntimeError(
                f"{self.city}「{self.keyword}」搜到的门店此刻都打烊了({names or '无门店'})")
        self.store = store
        when, why = pick_when(store, at=self.now)
        self.ctx["when"] = when
        self.state = self._mark("S2")
        self.options = ["就这家", "换一家"]
        name = store_label(store)
        return Turn(self.state, rm.turn(
            f"{name},{self.selection['key']}。",
            facts=[f"{store.get('businessStartTime')}-{store.get('businessEndTime')}"
                   if store.get("businessStartTime") else None, why],
            options=self.options),
            speech=vc.sayable(f"就这家，{name}，行吗？", 30))

    def swap_store_instore(self):
        be = CHANNELS[self.selection["channel"]]["beType"]
        st = self.api.nearby(self.city, self.keyword, be)
        ids = [s.get("storeCode") for s in st]
        try:
            i = ids.index(self.store.get("storeCode"))
        except ValueError:
            i = -1
        rotated = st[(i + 1) % len(st):] + st[:i + 1] if st else []
        nxt = pick_store(rotated)
        self.store = nxt or self.store
        when, why = pick_when(self.store, at=self.now)
        self.ctx["when"] = when
        self.state = self._mark("S2")
        self.options = ["就这家", "换一家"]
        return Turn(self.state, rm.turn(f"换到{store_label(self.store)}",
                                        facts=[why], options=self.options),
                    speech=vc.sayable(f"换到{store_label(self.store)}，这家行吗？", 30))

    # -------------------------------------------------- S3 外送系
    # 🔴 2026-10-11 用户反馈重构:「你都没和我确认地址,怎么就说我周围有什么店」
    #    S3 拆两轮:S3A 先问送到哪(地址列表,常用置顶★),确认后才查门店(S3B)。
    #    顺带修两个老毛病:
    #    a) 旧版一轮渲染地址+门店,猜错地址时 delivery_stores 白调一次;
    #    b) pick_store 返回 None(全部打烊)被渲染成「?送」——静默降级。
    def ask_address(self):
        """S3A 地址必问:分页列地址,常用置顶★,选完才进 S3B。"""
        addrs = self.ctx.get("_addrs")
        if addrs is None:
            addrs = self.api.addresses()
            if not addrs:
                raise RuntimeError("配送地址列表为空,外送不可用")
            self.ctx["_addrs"] = addrs
        prefer = self._preferred_address(addrs)
        ordered = [a for a in addrs if a["addressId"] != prefer["addressId"]]
        ordered.insert(0, prefer)
        start = self.ctx.get("_addr_start", 0)
        if start >= len(ordered):
            start = 0
        page = ordered[start:start + ADDR_PAGE_SIZE]
        self._addr_list, self._addr_page = ordered, page
        more = start + ADDR_PAGE_SIZE < len(ordered)
        labels = [compact_address(_ADDR_NOTE.sub("", a["label"]).strip())
                  for a in page]
        opts = [l + "★" if a["addressId"] == prefer["addressId"] else l
                for a, l in zip(page, labels)]
        if more:
            opts.append("更多地址")
        self.state = self._mark("S3A")
        self.options = labels + (["更多地址"] if more else [])
        return Turn(self.state, rm.turn(
            "送到哪个地址?",
            facts=[f"共{len(ordered)}个,常用置顶★"],
            options=opts),
            speech=vc.sayable("送到哪个地址？常送的那个排第一。", 30))

    def ask_store_delivery(self):
        """S3B 地址已确认才查门店。手册④复诵:送到哪 + 谁家送。"""
        aid = self.address["addressId"]
        cache = self.ctx.setdefault("_dstores", {})
        if aid not in cache:
            cache[aid] = self.api.delivery_stores(aid)
        self.stores = cache[aid]
        if not self.stores:
            raise RuntimeError("该地址无可送门店,换个地址试试")
        store = pick_store(self.stores, at=self.now)
        if store is None:
            return self._closed_stores_turn()
        self.store = store
        return self._confirm_store_turn()

    def _confirm_store_turn(self, swapped=False):
        when, why = pick_when(self.store, at=self.now)
        self.ctx["when"] = when
        self.state = self._mark("S3B")
        self.options = ["确认", "换店", "换地址"]
        label = _ADDR_NOTE.sub("", self.address["label"]).strip()
        note = _ADDR_NOTE.search(self.address["label"])
        head = f"换{store_label(self.store)}送" if swapped else f"送到{label}"
        return Turn(self.state, rm.turn(
            head,
            facts=[f"{store_label(self.store)}送", why,
                   f"备注:已有「{note.group(0).strip('（）()')}」" if note else None],
            options=self.options, soft=1),
            speech=vc.sayable(
                (f"换{store_label(self.store, 10)}送，行吗？" if swapped else
                 f"送到{compact_address(label)}，{store_label(self.store, 10)}送，对吗？"),
                36).replace("…", ""))

    def _closed_stores_turn(self):
        """门店全打烊:如实报每家开门时间,给「约开门后送」的出路。

        🔴 2026-10-11 凌晨实测:pick_store 返回 None 被旧代码渲染成「?送」
           +「营业时间未知」——没告诉用户为什么,下一步还会拿 None 崩掉。
        """
        def _key(s):
            t, tmr = opens_next(s, self.now)
            return (1 if tmr else 0, _mins(t or "23:59"))
        ranked = sorted(self.stores, key=_key)
        facts = []
        for s in ranked[:3]:
            t, tmr = opens_next(s, self.now)
            if not t:
                facts.append(f"{store_label(s, 8)} 营业时间未知")
                continue
            # 🔴 8 字店名 + 时段 = 宽度 14.5,16 字预算内;12 字店名会超
            facts.append(f"{store_label(s, 8)} {'明日' if tmr else '今日'}{t}开门")
        self.ctx["_earliest"] = ranked[0]
        t0, _ = opens_next(ranked[0], self.now)
        self.state = self._mark("S3Z")
        self.options = ["约开门后送", "换地址", "先不送了"]
        return Turn(self.state, rm.turn(
            "这个点还没有店在送:",
            facts=facts, options=self.options),
            speech=vc.sayable(
                f"这个点能送的店都打烊了，最早{t0 or ''}开门，约开门后送吗？", 34))

    def _preferred_address(self, addrs):
        """优先选和历史主店同一个配送区的地址。取不到就用第一个。"""
        top = self.p.get("topStore") or ""
        for a in addrs:
            for tok in re.findall(r"[一-龥]{2,4}", top):
                if tok in a["label"]:
                    return a
        same_city = [a for a in addrs if self.city and self.city in a["label"]]
        return (same_city or addrs)[0]

    def swap_store(self):
        st = self.stores
        cur = self.store.get("storeCode")
        i = next((k for k, s in enumerate(st) if s.get("storeCode") == cur), -1)
        nxt = pick_store(st[(i + 1) % len(st):] + st[:i + 1], at=self.now)
        self.store = nxt or self.store
        return self._confirm_store_turn(swapped=True)

    def reserve_earliest(self):
        """打烊分支选「约开门后送」:预约时刻必须落在可预约窗口(铁律1)。"""
        self.store = self.ctx.get("_earliest") or self.store
        when, why = next_slot(self.store, at=self.now)
        if not when:
            when, why = pick_when(self.store, at=self.now)
        self.ctx["when"], self.ctx["when_why"] = when, why
        return self.ask_dropoff()

    # -------------------------------------------------- S4 放置点
    def ask_dropoff(self):
        self.state = self._mark("S4")
        self.options = [k for k, _ in DROPOFF]
        return Turn(self.state, rm.turn("放哪儿？", options=self.options),
                    speech="放门口，还是前台？")

    # -------------------------------------------------- S5 点餐
    def ask_items(self):
        usual = self._usual()
        if usual is None:
            # 🔴 铁律3:菜单逐店逐渠道,照旧在这家店/这个渠道匹配不到时
            # 如实降级,不许 None 崩溃;给替代 + 看菜单 + 换店三条出路。
            alt = self._deviation()
            self.ctx["cands"] = [alt] if alt else []
            self.state = self._mark("S5")
            opts, names = [], ["换个", "看菜单", "换店"]
            if alt:
                name = rm.short_name(alt.get("real") or alt["label"], 7)
                opts.append(f"换个 {name} {rm.money(alt['final'])}")
            else:
                names = ["看菜单", "换店"]
            opts += ["看看菜单", "换店"]
            self.options = names
            spoken = (f"您常点的那套没有，换个{name}怎么样？" if alt
                      else "您常点的那套这家店没有，看看菜单或换家店？")
            return Turn(self.state, rm.turn(
                "常点的这套，这家店没有。",
                facts=["给你挑了个替代"] if alt else None,
                options=opts),
                speech=vc.sayable(spoken, 30))
        tag = "套餐" if usual.get("combo") else "单点"
        opts = [f"照旧 {rm.short_name(usual['label'], 7)} {rm.money(usual['final'])}"]
        facts = [f"你点过 {usual.get('n', 1)} 次这套{tag}"]
        alt = self._deviation()
        names = ["照旧"]
        if alt:
            names.append("换个")
            opts.append(f"换 {_menu_label(alt.get('real') or alt['label'])}"
                        f" {rm.money(alt['final'])}")
        names += ["看菜单", "换店"]
        opts += ["看看菜单", "换店"]
        self.state = self._mark("S5")
        self.options = names
        self.ctx["cands"] = [c for c in (usual, alt) if c]
        spoken = (f"照旧{rm.short_name(usual['label'], 7)}，还是换换？"
                  if usual else "您要点什么？")
        return Turn(self.state, rm.turn("您要点什么？", facts=facts, options=opts),
                    speech=vc.sayable(spoken, 30))

    def ask_menu(self):
        """S5M 分类轮(2026-10-11):服务端 15 类归并四桶,≤4 选项。"""
        full = self.ctx.get("_menu_full")
        if full is None:
            full = self.api.menu_full(self.store["storeCode"], self._params(),
                                      self.ctx["when"])
            self.ctx["_menu_full"] = full
        buckets = mb.classify(full.get("categories"), full.get("meals"))
        self.ctx["_mb_buckets"] = buckets
        total = sum(len(v) for v in buckets.values())
        opts = [mb.bucket_label(b, buckets) for b in mb.BUCKETS]
        self.state = self._mark("S5M")
        self.options = list(mb.BUCKETS)
        return Turn(self.state, rm.turn("想看哪一类？",
                                        facts=[f"菜单共 {total} 项"],
                                        options=opts),
                    speech=vc.sayable("套餐、汉堡、小食、饮品，想看哪一类？", 30))

    def ask_menu_page(self):
        """S5MP 翻页轮:每页 2 项 + 更多 + 返回点餐,选品进 S6。"""
        bucket = self.ctx.get("_mb_bucket") or mb.BUCKETS[0]
        start = self.ctx.get("_mb_start", 0)
        codes, more = mb.page((self.ctx.get("_mb_buckets") or {}).get(bucket),
                              start)
        meals = (self.ctx.get("_menu_full") or {}).get("meals") or {}
        labels, keep = [], []
        for c in codes:
            name = (meals.get(c) or {}).get("name") or c
            price = rm.money(float((meals.get(c) or {}).get("currentPrice") or 0))
            lab = f"{_menu_label(name)} {price}"
            labels.append(lab)
            keep.append(c)
        opts = labels + (["更多"] if more else []) + ["返回点餐"]
        self.state = self._mark("S5MP")
        # 编号→code 只认本页;「更多」「返回点餐」按下标在 _reply 里先拦
        self.options = list(labels) + (["更多"] if more else []) + ["返回点餐"]
        self.ctx["_mb_page"] = keep
        pageno = start // mb.PAGE_SIZE + 1
        # 🔴 口播只报名(不带价):SPEECH_BANNED 禁 ¥;单句 ≤30 字由 _menu_label ≤8 保底
        spoken = "、".join(_menu_label((meals.get(c) or {}).get("name") or c)
                           for c in codes) or "这一类没有了"
        return Turn(self.state, rm.turn(f"「{bucket}」第 {pageno} 页",
                                        options=opts),
                    speech=vc.sayable(f"{bucket}类，{spoken}，要哪个？", 60))

    def _menu_pick(self, i):
        """S5MP 选中一项 → 组订单进 S6。订单结构与 _usual/S5S 对齐。"""
        code = (self.ctx.get("_mb_page") or [None])[i]
        meals = (self.ctx.get("_menu_full") or {}).get("meals") or {}
        info = meals.get(code) or {}
        name = info.get("name") or code or ""
        price = float(info.get("currentPrice") or 0)
        self.order = {"label": _menu_label(name), "real": name, "code": code,
                      "combo": _is_combo(name), "orig": price, "final": price,
                      "qty": 1}
        return self.ask_detail()

    def _menu(self):
        if self.ctx.get("_menu") is None:
            cp = self._params()
            self.ctx["_menu"] = self.api.menu(self.store["storeCode"], cp, self.ctx["when"])
        return self.ctx["_menu"]

    def _params(self):
        ch = self.selection["channel"]
        return channel_params(ch, be_code=self.store.get("beCode"),
                              address_id=(self.address or {}).get("addressId"))

    def _usual(self):
        """照旧 = 你**真正点过的那一整份**,不是把套餐里的汉堡单独拿出来。

        🔴 2026-10-10 23:22 实测事故:
           历史原始品名是「经典安格斯单人餐（麦金卡免配）」×3 +
           「经典安格斯单人餐【升级小食送酱】」×2 —— 同一道套餐的两种写法。
           `short_name()` 把「单人餐」当噪音词删掉,两者都压成「安格斯」;
           再用 `best[:3] in menu_name` 匹配,「安格斯」命中了**裸汉堡**
           培根安格斯厚牛堡 ¥36.5。用户问「这是套餐还是什么」才暴露。

           修法两条:
           1. 分组用**原始品名**(只剥【】/（）里的变体描述),不做短名压缩
           2. 匹配要**短名全等 + 品类一致**(套餐不能匹配到单品)
        """
        orders = self.p.get("orders") or []
        if not orders:
            return None
        c = {}
        for o in orders:
            for it in o.get("items", []):
                k = _norm_name(it.get("name") or "")
                if k:
                    c[k] = c.get(k, 0) + it.get("qty", 1)
        best = max(c, key=lambda k: c[k]) if c else None
        if not best:
            return None
        want_combo = _is_combo(best)
        want_short = rm.short_name(best)
        meals = self._menu()
        code = next((k for k, m in meals.items()
                     if _is_combo(m.get("name") or "") == want_combo
                     and rm.short_name(m.get("name") or "") == want_short), None)
        if not code:
            code = next((k for k, m in meals.items()
                         if _is_combo(m.get("name") or "") == want_combo
                         and want_short and want_short in rm.short_name(m.get("name") or "")), None)
        if not code:
            return None
        self._attach_mod(code)
        q = self.api.quote(self.store["storeCode"], self._params(), self.ctx["_items"])
        real = (self.api.detail(code, self.store["storeCode"],
                                self._params(), self.ctx["when"]) or {}).get("name")
        # 复诵要用**看得见的真名**,short_name 会把「单人餐」删掉变成「安悉斯」
        return {"label": best, "real": _norm_name(real or best), "code": code,
                "combo": want_combo, "n": c[best],
                "orig": (q.get("originalPrice") or 0) / 100,
                "final": (q.get("price") or 0) / 100,
                "qty": c[best]}

    def _deviation(self):
        """一条**单人吃得完**的偏离项。

        🔴 2026-10-10 22:3x 干跑实测:不排除分享装时,偏离项选中了
           「加牛欢享四人餐 ¥116」—— 单人吃不完。过滤词直接复用
           recommend.MULTI_KEYWORDS,那份表是按真实菜单反查出来的,
           不在这里另列一份(凭直觉列过一次,漏了「四人餐」)。
        """
        from recommend import MULTI_KEYWORDS
        # 🔴 只推**主食**。实测不筛时选中了「麦乐鸡5块 ¥21.5」——那是小食,
        #    当「换个口味」推给用户是错的。
        NOT_MAIN = ("薯条", "鸡块", "鸡翅", "鸡球", "V翅", "玉米", "沙拉",
                    "甜品", "冰淇淋", "圆筒", "可乐", "雪碧", "咖啡", "奶茶",
                    "麦旋风", "派", "蛋挞", "奶昔", "组合", "全家桶", "桶")
        orders = self.p.get("orders") or []
        seen = set()
        for o in orders:
            for it in o.get("items", []):
                seen.add(rm.short_name(_norm_name(it.get("name") or "")))
        meals = self._menu()
        for name, code in ((rm.short_name(m.get("name") or ""), c)
                           for c, m in meals.items()):
            if any(k in name for k in MULTI_KEYWORDS):
                continue
            if any(k in name for k in NOT_MAIN):
                continue
            if not any(k in name for k in ("堡", "套餐", "三件套", "单人餐", "双层")):
                continue
            if name and name not in seen:
                q = self.api.quote(self.store["storeCode"], self._params(),
                                   [{"productCode": code, "quantity": 1}])
                return {"label": name, "real": _norm_name(
                            next((m.get("name") or "" for k2, m in meals.items()
                                  if k2 == code), name)),
                        "code": code,
                        "orig": (q.get("originalPrice") or 0) / 100,
                        "final": (q.get("price") or 0) / 100}
        return None

    def _compose(self, code):
        """组装单个商品项(单品或套餐),并挂上签名。返回 (item, gaps)。

        把**实际挂上**的签名写进 `ctx["mounted"]` —— 复诵只报这个,
        挂不上的签名要如实说「本单无对应商品,未生效」(缺陷 D4)。

        🔴 套餐的定制在 `rounds[].choices[].modification` 里,**顶层
        `modification` 是 null**。只查顶层会把「去芝士」误报成
        「该商品无芝士配料」(23:23 实测)。
        """
        sig = self._sig_on()
        gaps = []
        d = self.api.detail(code, self.store["storeCode"], self._params(),
                            self.ctx["when"])
        rounds = d.get("rounds") or []
        if rounds:
            item = {"productCode": code, "quantity": 1, "roundList": []}
            cheese_seen = False
            for r in rounds:
                pick = next((c for c in r.get("choices") or []
                             if c.get("quantity") == 1),
                            (r.get("choices") or [{}])[0])
                if not pick.get("code"):
                    continue
                entry = {"code": pick["code"], "quantity": 1}
                vals, notes, removed = _mount(pick, _applicable(sig, pick.get("name", "")))
                cheese_seen = cheese_seen or removed
                if vals:
                    entry["modification"] = {"values": vals}
                item["roundList"].append({"round": str(r["id"]),
                                          "comboItemList": [entry]})
            if sig.get("去芝士") and not cheese_seen:
                gaps.append("这套里没有能去芝士的汉堡")
            self.ctx["mounted"] = (["去芝士"] if cheese_seen else []) + \
                (["少冰"] if self._drink_mounted(item) else [])
            return item, gaps
        item = {"productCode": code, "quantity": 1}
        vals, notes, removed = _mount(d, _applicable(sig, d.get("name", "")))
        if vals:
            item["modification"] = {"values": vals}
        if sig.get("去芝士") and not removed:
            gaps.append("这份的配料表里没有吉士/干酪片")
        self.ctx["mounted"] = (["去芝士"] if removed else []) + \
            (["少冰"] if self._drink_mounted(item) else [])
        return item, gaps

    @staticmethod
    def _drink_mounted(item):
        """本单里有没有真的挂上饮料冰量。"""
        def has(vals):
            return any(v.get("code") == "200002" and v.get("key") != "0-1"
                       for v in vals)
        if item.get("modification"):
            return has(item["modification"].get("values") or [])
        for r in item.get("roundList") or []:
            for e in r.get("comboItemList") or []:
                if e.get("modification") and has(e["modification"].get("values") or []):
                    return True
        return False

    def _attach_mod(self, code):
        item, gaps = self._compose(code)
        self.ctx["_items"] = [item]
        return gaps

    # -------------------------------------------------- S6 细节
    def ask_detail(self):
        gaps = self._compose(self.order["code"])[1] or self._attach_mod(self.order["code"])
        self.state = self._mark("S6")
        self.options = ["就这样", "换个主食"]
        # 🔴 2026-10-10 23:21 实测缺陷 D4(test/test-3):
        #   本单只有汉堡、没有饮料,「少冰」其实无处挂载,但这一轮仍写
        #   「去芝士、少冰 已记住」→ 用户以为少冰也会生效。
        #   → 只展示**本单实际挂上**的签名,挂不上的如实说明。
        mounted = self.ctx.get("mounted") or []
        if mounted:
            facts = ["、".join(mounted) + " 已挂上"]
            # 🔴 不能写「按老样子」——normalize_terms 会把「老样子」归一成
            #    「照旧」,念出来变成「按照旧」。口播直接用「照旧」起句。
            spoken = vc.sayable(f"照旧，{'、'.join(mounted)}挂上了，行吗？", 30)
        else:
            facts = ["这份没有可定制的配料"]
            spoken = "这份没有可定制的，就这样吗？"
        not_mounted = [k for k, v in self._sig_on().items()
                       if v and k not in mounted]
        tail = None
        if not_mounted:
            tail = "本单没有对应商品，" + "、".join(not_mounted) + "未生效"
        elif gaps:
            tail = "⚠️ " + "；".join(gaps)
        return Turn(self.state, rm.turn("定制按老样子?", facts=facts,
                                        tail=tail, options=self.options),
                    speech=spoken)

    # -------------------------------------------------- S7 复诵报价
    def recite_and_quote(self):
        sig = self._sig_on()
        lines = [f"{self.order['label']}"]
        lines.append("、".join(k for k, v in sig.items() if v) or "照默认")
        q = self.order["quote"]
        self.state = self._mark("S7")
        self.options = ["确认下单", "改一下"]
        # 口播三句:菜名 → 定制 → 金额+确认。金额必须中文,数字念出来会听岔
        spoken = " ".join([
            vc.sayable(f"您点的是{rm.short_name(self.order.get('real') or self.order['label'], 8)}。", 24),
            vc.sayable("、".join(k for k, v in sig.items() if v) + "。" if any(sig) else "按默认。", 20),
            vc.sayable(f"{vc.cn_number(_yuan(q.get('price')))}，确认吗？", 24),
        ])
        return Turn(self.state, rm.turn(
            "您点的是:", facts=[f"{self.order.get('real') or self.order['label']}",
                               "、".join(k for k, v in sig.items() if v) or "照默认"],
            tail=rm.price_line(_yuan(q.get("originalPrice")), _yuan(q.get("price"))),
            options=self.options, soft=1), meta={"quote": q}, speech=spoken)

    # -------------------------------------------------- S8 / S9
    def pay(self):
        self.state = self._mark("S8")
        self.options = []
        return Turn(self.state, rm.turn(
            "已下单,扫码或在 APP 里付款。",
            facts=["不付款会自动关闭,不扣钱"]),
            speech="已下单，在APP里付款就行。")

    def farewell(self, pickup=None):
        self.state = self._mark("S9")
        self.options = []
        spoken = (f"喜欢您来，慢走。取餐号{vc.cn_count(pickup)}。"
                  if pickup else "喜欢您来，慢走。")
        return Turn(self.state, rm.turn(
            FAREWELL, facts=[f"取餐号 {pickup}"] if pickup else None),
            speech=spoken)

    # -------------------------------------------------- 场景推荐
    # 🔴 不收「随便」—— voice.TERMS 把「随便」归一成「照旧」(说随便=给你常点的),
    #    这与决策 5 一致:随机场景与「比店员更懂你」冲突,所以这里只认「随缘」。
    SCENES = {"健身": "fitness", "减肥": "fitness", "省": "saving",
              "省钱": "saving", "便宜": "saving",
              "赶时间": "hurry", "快点": "hurry",
              # 「开会前」= 赶时间的别名(test-4 轮次11 实测语义重合:
              # 老样子/官方套餐/配料稳定 ≈ 点过优先/常备/结构简单),不另做第六场景
              "开会前": "hurry", "开会": "hurry",
              "随缘": "random",
              "尝新": "try_new", "换换口味": "try_new"}

    def ask_scene(self, label):
        """按**当轮已确认的渠道**做场景推荐。

        🔴 2026-10-10 23:26 外部实测抓出:`recommend.py` 默认按历史众数渠道
           (麦乐送)拉菜单报价,而用户当轮选的是外带 → 报出的价格是另一个渠道的,
           用户追问「你这个价格是外送的价格吗」才发现。
           → 场景推荐必须吃当前渠道,不许回退到历史众数。
        🔴 2026-10-11 B1 修:原来直连 `recommend.fetch_menu/fetch_nutrition`
           (真实网络),把注入的 FakeApi 整个绕开 —— 离网测试一跑就 600053。
           → 菜单走 self._menu()(带缓存),营养走 self.api.nutrition()。
        """
        from recommend import (freq_of, rank_fitness, rank_hurry,
                               rank_random, rank_saving, rank_try_new,
                               seen_names_of)
        key = self.SCENES.get(label)
        if not key:
            return None
        cp = self._params()
        meals = self._menu()
        try:
            nu = self.api.nutrition()
        except AttributeError:
            from recommend import fetch_nutrition
            nu = fetch_nutrition()

        class _Api:
            """🔴 不传 api 时 rank_fitness 会把**所有套餐**排除(营养表里没有套餐),
            只剩裸堡和小食 —— 健身场景就算不出「一份主食」。"""
            @staticmethod
            def detail(code):
                try:
                    return self.api.detail(code, self.store["storeCode"], cp,
                                           self.ctx["when"])
                except Exception:
                    return {}
        seen = seen_names_of(self.p)
        if key == "fitness":
            ranked = rank_fitness(meals, nu, api=_Api())
        elif key == "saving":
            ranked = rank_saving(meals, nu)
        elif key == "hurry":
            ranked = rank_hurry(meals, nu, seen=seen)
        elif key == "try_new":
            ranked = rank_try_new(meals, nu, seen=seen, freq=freq_of(self.p))
        else:
            ranked = rank_random(meals, nu, self.p.get("orders") or [])
            if not ranked:
                return Turn(self.state, rm.turn(
                    "随缘的依据是你最常点的那套,你还没点过单。",
                    options=self.options),
                    speech="您还没点过单，随缘没有依据，先看看菜单？")
        self.ctx["scene_ranked"] = ranked[:4]
        ch_label = {"到店自取": "到店价", "麦乐送": "麦乐送价",
                    "得来速": "得来速价", "企业团餐": "团餐价"}[self.selection["channel"]]
        opts, facts = [], [f"按{ch_label}给你挑（{len(ranked)} 项达标）"]
        for r in self.ctx["scene_ranked"][:3]:
            n = r.get("nutrition") or {}
            scope = r.get("scope") or "整份"
            # 🔴 口径必须写在选项里(单品/整份),数字不带口径等于没说
            tail = (f" {scope}{n['kcal']}kcal" if n else " 数据不全")
            opts.append(f"{_menu_label(r['name'], 6)} {rm.money(r['final'])}{tail}")
        opts.append("看看菜单")
        self.state = self._mark("S5S")
        self.options = ["1", "2", "3", "4"][:len(opts) - 1] + ["看看菜单"]
        # 🔴 口播不念数量(cn_count 的 2 会念「二」,量词语境该是「两样」;
        #    数字在口播里本来就易听岔,直接说「挑好了」)
        return Turn(self.state, rm.turn(f"{label}的话，", facts=facts, options=opts),
                    speech=vc.sayable(f"{label}的话，给您挑好了。", 30))

    # -------------------------------------------------- 自由文本偏好
    _PREF = [
        ("去芝士", ("不吃芝士", "不要芝士", "不加芝士", "去芝士", "不要吉士", "不吃吉士")),
        ("少冰", ("少冰", "冰少一点", "少放冰")),
        ("标准冰", ("标准冰", "正常冰", "正常冰量")),
        ("去冰", ("去冰", "不要冰", "不加冰")),
    ]

    def _read_preference(self, text):
        """认出偏好声明 → 写进 ctx.signature_overrides → 原地重问当前轮。"""
        t = (text or "").strip()
        if not t or t in ("1", "2", "3", "4"):
            return None
        ov = self.ctx.setdefault("sig_override", {})
        hit = None
        for name, kws in self._PREF:
            if any(k in t for k in kws):
                hit = name
                break
        if not hit:
            return None
        if hit == "去芝士":
            ov["去芝士"] = True
        elif hit == "少冰":
            ov["少冰"] = True
            ov["少冰声明"] = True
        elif hit == "标准冰":
            ov["少冰"] = False
            ov["少冰声明"] = True
        elif hit == "去冰":
            ov["少冰"] = "去冰"
            ov["少冰声明"] = True
        self.ctx["pending_ack"] = hit
        # A3:用户明确说过的话要留底。「去芝士」= 不吃芝士 → kind=avoid;
        # 冰量是偏好不是拒单 → kind=pref。落盘由 flush_declared() 统一做。
        self.ctx.setdefault("declared_new", []).append({
            "kind": "avoid" if hit == "去芝士" else "pref",
            "label": hit,
            "item": "芝士" if hit == "去芝士" else None,
            "raw": t,
            "at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        n = len(self.options)
        q = {"去芝士": "好，汉堡去吉士。", "少冰": "好，饮料少冰。",
             "标准冰": "好，饮料标准冰。", "去冰": "好，饮料去冰。"}[hit]
        return (q, [f"回 1-{n} 继续"] if n else None)

    def flush_declared(self, path=None):
        """把本轮用户明确声明的偏好追加落盘。返回写入条数。

        按 (kind,label,raw) 去重——同一句「不吃芝士」说两遍只留一条。
        🔴 测试与库调用不调它(不写文件);CLI / --flow-demo 驱动在会话结束时调。
        """
        news = self.ctx.get("declared_new") or []
        if not news:
            return 0
        path = path or DECLARED_PATH
        entries = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                entries = []
        seen = {(e.get("kind"), e.get("label"), e.get("raw")) for e in entries}
        written = 0
        for e in news:
            k = (e.get("kind"), e.get("label"), e.get("raw"))
            if k in seen:
                continue
            entries.append(e)
            seen.add(k)
            written += 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        return written

    # -------------------------------------------------- 调度
    def turn_text(self):
        """第一轮:迎宾 + 立刻问渠道。

        🔴 2026-10-10 23:13 修:原来 `self.greet()` 的返回值被直接丢弃,
           只返回了 S1 那一轮 —— **品牌招呼语从来没到过用户眼前**。
           21:27 走查第 1 条批评的第一道修复等于没生效。
        """
        g = self.greet()
        self._greet_lines = list(g.text.split("\n"))
        t = self.ask_channel()
        body = t.text
        head = "\n".join(self._greet_lines)
        out = f"{head}\n\n{body}" if head else body
        self.ctx["_last_text"] = out
        self.ctx["_last_speech"] = " ".join(x for x in (g.speech, t.speech) if x)
        return out

    def reply(self, text):
        out = self._reply(text)
        self.ctx["_last_text"] = out.text
        self.ctx["_last_speech"] = out.speech or ""
        return out

    # -------------------------------------------------- B3:口语 → 选项
    def _candidates(self):
        """本轮可复述确认的候选 (选项索引, 可念名称)。

        编号型选项(S5S 场景轮)的真名在 ctx.scene_ranked 里;
        S5 的候选名在 ctx.cands 里 —— options 的键(照旧/换个)不足以复述。
        """
        out = []
        ranked = self.ctx.get("scene_ranked") or []
        for i, o in enumerate(self.options):
            if re.fullmatch(r"[1-9]", o or ""):
                nm = ranked[i]["name"] if i < len(ranked) else None
            else:
                nm = o
            if nm:
                out.append((i, nm))
        if self.state == "S5":
            for j, c in enumerate(self.ctx.get("cands") or []):
                if j < len(self.options):
                    out.append((j, c.get("real") or c.get("label")))
        return [(i, _menu_label(str(nm), 8)) for i, nm in out if nm]

    def _fuzzy_option(self, norm):
        """口语与候选名互相包含 → 候选索引。语义与 voice.confirm_heard 一致。"""
        if not norm or len(norm) < 2:
            return None
        for i, nm in self._candidates():
            if len(nm) >= 2 and (nm in norm or norm in nm):
                return i
        return None

    def _reply(self, text):
        # B3:ASR 口语先归一(打包→外带 / 车上取→得来速 / 不要芝士→去芝士),
        # 再进编号/包含匹配 —— 语音用户说的是口语,不是屏幕上的词
        norm = vc.normalize_terms((text or "").strip())
        # B3:复述确认的应答。「对/是/嗯/回1」=确认上一轮待认项;
        # 「不是/回2」=否认,回到本轮选项。其余当新输入正常解析。
        pc = self.ctx.pop("pending_confirm", None)
        m_digit = re.fullmatch(r"\s*([1-9])\s*", norm)
        if pc is not None:
            if (m_digit and int(m_digit.group(1)) == 1) or \
               any(k in norm for k in ("对", "是", "嗯", "没错", "确认", "好")):
                i = pc
            elif (m_digit and int(m_digit.group(1)) == 2) or \
                    any(k in norm for k in ("不", "错", "换")):
                n = len(self.options)
                return Turn(self.state,
                            rm.turn(f"好，回 1-{n} 或直接说。", options=self.options),
                            speech="好，您再说一次。")
            else:
                i = self._pick(norm)
        else:
            i = self._pick(norm)
        if i is None:
            # 自由文本偏好:先认签名单项,认出来了就确认并**原地重问**,
            # 不让用户为了说一句「我不吃芝士」还得先报个编号。
            ack = self._read_preference(norm)
            if ack:
                q, opts = ack
                return Turn(self.state, rm.turn(q, options=opts),
                            speech=vc.sayable(q, 30))
            hit_scene = next((k for k in self.SCENES if k in norm), None)
            if hit_scene and self.store:
                t = self.ask_scene(hit_scene)
                if t:
                    return t
            # B3:口语与某个选项名互相包含 → 复述确认,不瞎猜也不直接放弃
            ci = self._fuzzy_option(norm)
            if ci is not None:
                self.ctx["pending_confirm"] = ci
                nm = dict(self._candidates())[ci]
                return Turn(self.state,
                            rm.turn(f"您是说「{nm}」吗？", options=["是", "不是"]),
                            speech=vc.sayable(f"您是说{nm}吗？", 20))
            n = len(self.options)
            tip = (f"没看懂，回 1-{n}。" if n else "说什么都行。")
            spoken = "没听懂，您直接说想吃什么。" if n else "说什么都行。"
            return Turn(self.state, rm.turn(tip), speech=spoken)
        if i < 0 or i >= len(self.options):
            return Turn(self.state,
                        rm.turn(f"没有第 {i+1} 项，回 1-{len(self.options)}。"),
                        speech="没有这一项，您再说一次。")
        s = self.selection
        if self.state == "S5S":
            # 🔴 B1 修(2026-10-11):场景推荐此前没有任何消费分支,
            #    选完落进「已经点完了」死端。现在:选中项立为订单 → S6 细节。
            ranked = self.ctx.get("scene_ranked") or []
            pick = ranked[i] if 0 <= i < len(ranked) and self.options[i] != "看看菜单" else None
            if pick is None:
                return self.ask_items()
            pick = dict(pick, label=pick["name"],
                        real=pick.get("real") or pick["name"])
            self.order = pick
            return self.ask_detail()
        if self.state == "S1":
            self.selection = self._channel_order[i]
            if self.selection["channel"] == "麦乐送":
                return self.ask_address()
            return self.ask_store_instore()
        if self.state == "S2":
            return self.swap_store_instore() if i == 1 else self.ask_store_instore()
        if self.state == "S3A":
            # 🔴 地址先确认:选了地址才查门店;「更多地址」只翻页
            if self.options[i] == "更多地址":
                self.ctx["_addr_start"] = self.ctx.get("_addr_start", 0) + ADDR_PAGE_SIZE
                return self.ask_address()
            self.address = self._addr_page[i]
            return self.ask_store_delivery()
        if self.state == "S3B":
            if i == 1:
                return self.swap_store()
            if i == 2:
                self.ctx["_addr_start"] = self.ctx.get("_addr_start", 0) + ADDR_PAGE_SIZE
                return self.ask_address()
            return self.ask_dropoff()
        if self.state == "S3Z":
            if i == 1:
                self.ctx["_addr_start"] = self.ctx.get("_addr_start", 0) + ADDR_PAGE_SIZE
                return self.ask_address()
            if i == 2:
                return self.farewell()
            return self.reserve_earliest()
        if self.state == "S4":
            self.dropoff = DROPOFF[i][1]
            return self.ask_items()
        if self.state == "S5":
            # 🔴 非下单选项先拦:cands[i] 越界会回落 cands[0],选「看菜单」
            # 却把照旧下单是本域最严重的分派错误(I19)。
            if self.options[i] == "看菜单":
                return self.ask_menu()
            if self.options[i] == "换店":
                if not self.stores:
                    return Turn(self.state,
                                rm.turn("这家店没法换，换个地址试试。"),
                                speech="这家店没法换，您换个地址试试。")
                return self.swap_store()
            cands = self.ctx.get("cands") or []
            pick = cands[i] if 0 <= i < len(cands) else (cands[0] if cands else None)
            if pick is None:
                return Turn(self.state,
                            rm.turn("这家店这个点没得吃了，换一家？"),
                            speech="这家店这个点没得吃了，换一家吗？")
            self.order = dict(pick)
            return self.ask_detail()
        if self.state == "S5M":
            self.ctx["_mb_bucket"] = mb.BUCKETS[i]
            self.ctx["_mb_start"] = 0
            return self.ask_menu_page()
        if self.state == "S5MP":
            lab = self.options[i]
            if lab == "更多":
                self.ctx["_mb_start"] = self.ctx.get("_mb_start", 0) + mb.PAGE_SIZE
                return self.ask_menu_page()
            if lab == "返回点餐":
                return self.ask_items()
            return self._menu_pick(i)
        if self.state == "S6":
            q = self.api.quote(self.store["storeCode"], self._params(), self.ctx["_items"])
            self.order["quote"] = q
            return self.recite_and_quote()
        if self.state == "S7":
            return self.pay()
        if self.state == "S8":
            return self.farewell(self.ctx.get("pickup"))
        return Turn(self.state, rm.turn("已经点完了。"), speech="已经点完了。")

    # -------------------------------------------------- 不变量
    def invariants(self):
        """返回违反的不变量列表,空 = 全过。test_flow.py 直接用。"""
        bad = []
        turns = self.ctx["turns"]
        if "S1" not in turns:
            bad.append("I1 S1 渠道询问被跳过")
        if self.selection and self.selection["channel"] == "麦乐送":
            if not self.address:
                bad.append("I2 外送未确认接收点")
            if not self.dropoff:
                bad.append("I2 外送未确认放置点")
        if self.selection and self.selection["channel"] != "麦乐送":
            if not self.selection.get("takeWay"):
                bad.append("I3 到店未确定 takeWayCode")
        if self.order and "quote" not in self.order:
            bad.append("I4 下单前缺试算")
        if len(self.options) > 4:
            bad.append("I6 选项超过 4 个")
        return bad


if __name__ == "__main__":
    # 单命令推进一轮(2026-10-11):宿主不得手写话术 —— 手写会与
    # flow 内部选项排序错位(实测:「1 堂食 2 外带 3 送到家」手写稿
    # 把用户的 3 当成了外带)。每轮一条命令,快且不会错位。
    import argparse
    ap = argparse.ArgumentParser(
        description="门店流程状态机:不带 --reply = 第一轮;--reply 按顺序重放应答")
    ap.add_argument("--reply", action="append", default=[],
                    help="用户应答(数字或选项名),可重复,按对话顺序")
    ap.add_argument("--at", default=None, help="模拟时刻,如 '2026-10-11 07:05'")
    ap.add_argument("--city", default="上海市")
    ap.add_argument("--speech", action="store_true", help="同时打印口播稿")
    a = ap.parse_args()
    at = datetime.strptime(a.at, "%Y-%m-%d %H:%M") if a.at else None
    f = Flow(at=at, city=a.city)
    f.turn_text()
    t = None
    for r in a.reply:
        t = f.reply(r)
    if t is None:
        # 不带 --reply = 第一轮。🔴 turn_text 返回的是拼接文本(str)而非 Turn,
        # 2026-10-11 实测 t.text 在此必崩;话术与口播都存在 ctx 里。
        print(f.ctx.get("_last_text") or "")
        sp = f.ctx.get("_last_speech")
        if a.speech and sp:
            print()
            print(f"(口播) {sp}")
    else:
        print(t.text)
        if a.speech and t.speech:
            print()
            print(f"(口播) {t.speech}")
    f.flush_declared()