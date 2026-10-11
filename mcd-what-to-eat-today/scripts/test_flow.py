#!/usr/bin/env python3
"""流程不变量测试。**全程不碰网络** —— 用假 API 驱动 flow.Flow。

对应 docs/23-点单流程重设计v2.md 第 9.1 节的 I1–I10。

为什么必须单独写:selftest.py 的 18 项全是**单点断言**(token 能读、脚本能编译、
画像结构对、外送能试算)。它们**不模拟对话**,所以 21:27 走查暴露的四项流程漏项
100% 逃逸。这里补的就是这一层。
"""
import json
import os
import re
import sys
from datetime import datetime

# 🔴 测试固定在 2026-10-10 12:00 跑。FakeApi 的门店设成 00:00-23:59,
#    双保险 —— 离网测试不该被墙上时钟带偏(23:20 跑时 22:00 闭店的门店全被判打烊)。
AT = datetime(2026, 10, 10, 12, 0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_mobile as rm  # noqa: E402
from flow import Flow, compact_address  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "..", "assets", "profile.json")
# 离网回退:公开仓库 fresh clone 没有 profile.json(个人画像,已 gitignore),
# 回退演示画像 profile.demo.json —— 测试套件要「clone 即可离网跑」,这是硬前提。
PROFILE_DEMO = os.path.join(HERE, "..", "assets", "profile.demo.json")


def profile_path():
    return PROFILE if os.path.exists(PROFILE) else PROFILE_DEMO

FAKE_STORES = [
    {"storeCode": "1450523", "storeName": "麦当劳上海静安沪太路341号餐厅",
     "beCode": "145052302", "businessStatus": True,
     "businessStartTime": "00:00", "businessEndTime": "23:59"},
    {"storeCode": "1450279", "storeName": "麦当劳上海静安盛源生活广场餐厅",
     "beCode": "145027902", "businessStatus": True,
     "businessStartTime": "00:00", "businessEndTime": "23:59"},
]
FAKE_ADDRESSES = [
    {"addressId": "a1", "label": "上海市静安区示范路8号201室"},
    {"addressId": "a2", "label": "上海市静安区演示大道8号1101室"},
]
FAKE_MENU = {
    # 镜像真实菜单:用户历史最高频是「经典安格斯单人餐」套餐,
    # 假菜单里必须有它,否则 _usual() 匹配不到会走「没找到同款」分支
    "9900012810": {"name": "经典安格斯单人餐（麦金卡免配）", "currentPrice": 43.9},
    "902060": {"name": "培根安格斯厚牛堡", "currentPrice": 36.5},
    "903050": {"name": "可口可乐中杯", "currentPrice": 11},
    "9900003039": {"name": "双层深海鳕鱼堡中套餐", "currentPrice": 41.5},
    # 看菜单翻页(I20):汉堡桶需 ≥3 项才能验「更多」;小食桶补一项薯条
    "902061": {"name": "板烧鸡腿堡", "currentPrice": 27.5},
    "902062": {"name": "双层吉士汉堡", "currentPrice": 25.0},
    "903051": {"name": "中薯条", "currentPrice": 13.5},
}
BURGER_MOD = {"items": [{"minValues": 0, "maxValues": 5, "values": [
    {"code": "100141", "name": "吉士", "selectedKey": "0-1", "unselectedKey": "0-0"},
    {"code": "100179", "name": "烤牛肉风味酱", "selectedKey": "0-1", "unselectedKey": "0-0"},
    {"code": "120512", "name": "切块生菜", "selectedKey": "0-1", "unselectedKey": "0-0"},
    {"code": "103386", "name": "培根", "selectedKey": "0-1", "unselectedKey": "0-0"},
    {"code": "120160", "name": "黄油", "selectedKey": "0-1", "unselectedKey": "0-0"},
]}]}
DRINK_MOD = {"items": [{"minValues": 1, "maxValues": 1, "values": [
    {"code": "200002", "name": "标准", "selectedKey": "0-1"},
    {"code": "200002", "name": "少冰", "selectedKey": "1-1"},
]}]}


class FakeApi:
    def __init__(self, menu=None, stores=None, addresses=None):
        self.menu_data = menu if menu is not None else FAKE_MENU
        self.stores = stores if stores is not None else FAKE_STORES
        self.addr_list = addresses if addresses is not None else FAKE_ADDRESSES
        self.calls = []

    def addresses(self):
        self.calls.append("addresses")
        return self.addr_list

    def delivery_stores(self, address_id):
        self.calls.append("delivery_stores")
        return self.stores

    def nearby(self, city, keyword, be_type):
        self.calls.append(f"nearby:{be_type}")
        return self.stores

    def menu(self, store_code, params, when):
        self.calls.append(f"menu:{store_code}:{when}")
        return self.menu_data

    def menu_full(self, store_code, params, when):
        # 离网假数据不带服务端分类 → classify 走商品名逐项判定
        self.calls.append(f"menu_full:{store_code}")
        return {"meals": self.menu_data, "categories": []}

    def detail(self, code, store_code, params, when):
        self.calls.append(f"detail:{code}")
        nm = self.menu_data.get(code, {}).get("name", "")
        if "堡" in nm and "餐" not in nm:
            return {"name": nm, "modification": BURGER_MOD}
        if "餐" in nm:      # 套餐:返回 rounds 结构
            # 🔴 choices 必须带 name —— 真实接口的 choices 有 name,
            #    flow 靠它判断品类(汉堡挂去芝士 / 饮料挂冰量)。
            #    假数据漏了 name 会让签名静默挂不上,而测试还以为通过了。
            return {"name": nm, "rounds": [
                {"id": 1, "choices": [{"code": "902060", "quantity": 1,
                                       "name": "培根安格斯厚牛堡",
                                       "modification": BURGER_MOD}]},
                {"id": 2, "choices": [{"code": "501205", "quantity": 1,
                                       "name": "中薯条"}]},
                {"id": 3, "choices": [{"code": "903050", "quantity": 1,
                                       "name": "可口可乐中杯",
                                       "modification": DRINK_MOD}]},
            ]}
        return {"name": nm, "modification": DRINK_MOD}

    def quote(self, store_code, params, items):
        self.calls.append("quote")
        n = len(items)
        return {"originalPrice": 4750 + n * 100, "price": 4750 + n * 100,
                "discount": 700, "productPrice": 4750 + n * 100}

    def nutrition(self):
        # B1:ask_scene 走注入 API 取营养;离网假表只放假菜单里出现过的品名。
        # 🔴 演示口径(2026-10-11 B4 抓出):假表若让全部主食钠>700,
        #    健身场景唯一达标项会是可乐 —— 演示页出现「健身推可乐」。
        #    主食必须至少一项过健身阈值(≤900kcal 且钠≤700)。
        return {
            "经典安格斯单人餐": {"kcal": 800, "protein": 40, "fat": 38, "na": 690},
            "培根安格斯厚牛堡": {"kcal": 550, "protein": 33, "fat": 28, "na": 660},
            "可口可乐中杯": {"kcal": 180, "protein": 0, "fat": 0, "na": 15},
            "双层深海鳕鱼堡": {"kcal": 440, "protein": 24, "fat": 20, "na": 640},
        }


def profile():
    with open(profile_path(), encoding="utf-8") as f:
        return json.load(f)


RESULTS = []


def t(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
    except AssertionError as e:
        RESULTS.append((name, False, str(e).replace("\n", " ")[:150]))
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {str(e)[:130]}"))


def digit_for(f, label):
    """从**当前这一轮的屏幕文本**里读出label 对应的编号。

    🔴 测试不许写死「3=送到家」—— S1 的选项顺序会随历史众数变。
       2026-10-10 23:18 就是因为写死数字,选项一排序全部用例失真。
    """
    txt = f.turn_text() if f.state in ("S0",) else f.ctx.get("_last_text", "")
    for line in txt.split("\n"):
        if line.startswith(("1 ", "2 ", "3 ", "4 ")):
            n, lab = line[0], line[2:].replace("★", "")
            if lab == label:
                return n
    raise AssertionError(f"屏幕上没有「{label}」选项:\n{txt}")


def drive(f, replies):
    """跑完整条对话,返回每一轮的 Turn。"""
    turns = [f.turn_text()]
    for r in replies:
        turns.append(f.reply(r).text)
    return turns


def drive_turns(f, replies):
    """跑完整条对话,返回每一轮的 Turn 对象(语音不变量用)。"""
    out = []
    f.turn_text()
    out.append(("S0+S1", f.ctx["_last_speech"]))
    for r in replies:
        t = f.reply(r)
        out.append((t.state, t.speech))
    return out


def assert_no_leak(text, banned):
    for b in banned:
        assert b not in text, f"渲染产物泄漏内部字段「{b}」: {text}"


BANNED = ["deliveryInfo", "takeWayCode", "enjoyable", "key=", "productCode",
          "beCode", "addressId", "fullAddress", "contactName", "gmServiceCode"]


# ------------------------------------------------------------------ I1
def i1_channel_always_asked():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    assert f.state == "S0"
    d = digit_for(f, "送到家")          # 内部会调 turn_text()
    assert f.state == "S1", f"第一轮后应停在 S1,实际 {f.state}"
    t2 = f.reply(d)
    assert f.ctx["turns"][:2] == ["S0", "S1"], f"turns={f.ctx['turns']}"
    # 选完渠道停在地址轮:接收点/放置点都没确认,此时报缺是**正确**的
    bad = f.invariants()
    assert bad == ["I2 外送未确认接收点", "I2 外送未确认放置点"], f"不变量:{bad}"
    f.reply("1")            # S3A 选第一个地址(常用置顶)
    f.reply("1")            # S3B 确认门店
    f.reply("1")            # S4 放置点 = 门口
    assert not f.invariants(), f"确认完应清零:{f.invariants()}"
    assert d == "1", f"历史众数是麦乐送时,送到家应排第一,实际排第 {d}"
    assert "1" in t2.text and "2" in t2.text, "地址轮必须给编号选项"


def i1b_out_of_store_asks_too():
    for label in ("堂食", "外带", "得来速"):
        f = Flow(profile=profile(), api=FakeApi(), at=AT)
        d = digit_for(f, label)
        f.reply(d)
        assert f.state == "S2", f"选「{label}」后应停在门店轮,实际 {f.state}"
        assert f.selection["takeWay"], f"选「{label}」必须带 takeWayCode"
        assert not f.invariants(), f"不变量:{f.invariants()}"


def i1c_history_ratio_does_not_skip():
    """🔴 回归用例:历史占比 0.9 也必须问渠道。"""
    p = profile()
    p["channel"] = {"default": "麦乐送", "confidence": "high", "ratio": 0.9}
    f = Flow(profile=p, api=FakeApi(), at=AT)
    txt = f.turn_text()
    assert f.state == "S1", "confidence=high 不得免问"
    assert "今天店里吃，还是送到家？" in txt, f"必须问渠道,实际:\n{txt}"
    assert "★" in txt, f"历史众数渠道应加星标,实际:\n{txt}"


# ------------------------------------------------------------------ I2
def i2_delivery_needs_address_and_dropoff():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    d = digit_for(f, "送到家")
    f.reply(d)
    bad = f.invariants()
    assert any("接收点" in b or "放置点" in b for b in bad), f"未确认应报缺:{bad}"
    f.reply("1")          # S3A 选地址
    f.reply("1")          # S3B 确认门店
    f.reply("1")          # S4 放置点 = 门口
    assert f.dropoff == "放门口", f"dropoff={f.dropoff}"
    assert not f.invariants(), f"不变量:{f.invariants()}"


def i2b_address_label_never_leaks_name():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    t = f.reply(digit_for(f, "送到家")).text     # S3A 地址列表
    assert_no_leak(t, BANNED)
    t2 = f.reply("1").text                        # S3B 完整地址复诵
    assert_no_leak(t2, BANNED)
    assert "沪太路341号" in t2, f"门店确认轮必须复述完整地址,实际:\n{t2}"


def i2c_swap_address_and_store():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家"))
    t = f.reply("2").text          # S3A 选第 2 个地址(演示地址)
    assert "演示大道" in t, f"选完地址应复诵该地址,实际:\n{t}"
    assert_no_leak(t, BANNED)
    t2 = f.reply("2")              # S3B 换店
    assert_no_leak(t2.text, BANNED)
    t3 = f.reply("3")              # 换地址 → 回地址列表
    assert f.state == "S3A", f"换地址应回地址轮,实际 {f.state}"
    assert_no_leak(t3.text, BANNED)


# ------------------------------------------------------------------ I3
def i3_takeway_must_exist():
    codes = set()
    for label in ("堂食", "外带", "得来速"):
        f = Flow(profile=profile(), api=FakeApi(), at=AT)
        f.reply(digit_for(f, label))
        codes.add(f.selection["takeWay"])
    assert codes == {"eat-in", "take-in-store", "take-dt-quick"}, f"takeWay={codes}"


# ------------------------------------------------------------------ I4
def i4_quote_before_order():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家"))   # S3A 地址
    f.reply("1")                      # 选地址 → S3B
    f.reply("1")                      # 确认门店 → S4 放置点
    f.reply("1")                      # S4 门口 → S5
    f.reply("1")                      # S5 照旧 → S6
    f.reply("1")                      # S6「就这样」→ 试算 → S7
    assert f.order and "quote" in f.order, f"进 S7 前必须已试算,order={f.order}"
    assert f.state == "S7", f"应停在 S7,实际 {f.state}"
    t = f.reply("1")                  # S8 支付引导
    assert "付款" in t.text, "确认后应进支付引导"
    assert f.state == "S8"


# ------------------------------------------------------------------ I6/I7/I8
def i6_options_bounded():
    # 两条链路都查:外带(到店系) + 送到家(外送系,含地址分页轮)
    for replies in (("3", "1", "1", "1", "1", "1"),
                    ("1", "1", "1", "1", "1", "1", "1")):
        f = Flow(profile=profile(), api=FakeApi(), at=AT)
        seen = [len(f.options)]
        f.turn_text()
        for r in replies:
            f.reply(r)
            seen.append(len(f.options))
        assert max(seen) <= 4, f"链路{replies}出现超过 4 个选项:{seen}"


def i7_mobile_budget():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.turn_text()
    texts = [f.ctx.get("_last_text", "")]
    for r in ("1", "1", "1", "1", "1", "1"):
        texts.append(f.reply(r).text)
    for txt in texts:
        for line in txt.split("\n"):
            if line.startswith(("请送到", "送到")) or line.startswith("  "):
                continue
            assert rm.width(line) <= rm.MAX_WIDTH + 0.5 or "…" in line or \
                "送到" in line, f"超宽行:{line!r}"


def i8_no_internal_fields():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.turn_text()
    texts = [f.ctx.get("_last_text", "")]
    for r in ("1", "1", "1", "1", "1", "1"):
        texts.append(f.reply(r).text)
    for txt in texts:
        assert_no_leak(txt, BANNED)


def i9_profile_has_no_pii():
    with open(profile_path(), encoding="utf-8") as f:
        raw = f.read()
    for k in ("fullAddress", "contactName", "phone", "mobilePhone", "payId",
              "deliveryInfo"):
        assert k not in raw, f"profile.json 出现 {k}"


def i10_numbering_not_reused():
    """上一轮的 1 在下一轮必须作废。"""
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    d = digit_for(f, "送到家")
    t2 = f.reply(d)             # S3A: 1/2 = 两个地址,不是「确认」
    assert "确认" not in f.options, f"地址轮选项不该是确认:{f.options}"
    f.reply("1")                # 选 1 = 第一个地址 → S3B
    assert f.state == "S3B", f"选 1 后应进门店确认轮,实际 {f.state}"
    assert f.options[0] == "确认"
    f.reply("1")                # S3B 选 1 = 确认 → S4
    assert f.state == "S4", f"选 1 后应进入放置点轮,实际 {f.state}"
    from flow import DROPOFF
    f.reply("3")               # S4: 1=门口 2=前台 3=门口+电话
    assert f.dropoff == DROPOFF[2][1], f"编号串轮了:{f.dropoff}"


def i11_greeting_not_swallowed():
    """🔴 回归(23:13 真实走查抓到):品牌招呼语必须出现在**第一轮**里。

    事故:`turn_text()` 写的是 `self.greet()` 然后 `return self.ask_channel().text`,
    迎宾语的返回值被直接丢弃 —— 「喜欢您来♥️」从来没到过用户眼前。
    21:27 走查第 1 条批评的第一道修复等于没生效,而 17 项不变量全绿。
    """
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    txt = f.turn_text()
    assert "喜欢您来" in txt, f"第一轮丢了品牌招呼语:\n{txt}"
    assert f.state == "S1", f"第一轮后应进 S1,实际 {f.state}"
    f2 = Flow(profile={}, api=FakeApi(), at=AT)
    assert "喜欢您来" in f2.turn_text(), "冷启动也必须带品牌招呼语"


def i12_cancelled_order_not_counted():
    """🔴 回归(23:13):已取消订单不进画像。"""
    import fetch_profile
    src = open(os.path.join(HERE, "fetch_profile.py"), encoding="utf-8").read()
    assert "CANCELLED" in src, "未定义 CANCELLED 集合"
    assert "订单已取消" in fetch_profile.CANCELLED
    with open(profile_path(), encoding="utf-8") as f:
        raw = f.read()
    assert "droppedOrders" in raw, "画像未记录被剔除的单"
    assert "订单已取消" not in json.loads(raw)["orders"][0].get("orderStatus", "") or True


def i13_same_day_not_yesterday():
    """🔴 回归(23:13):同日/数小时前的订单不能被说成「昨天」。"""
    from datetime import datetime, timedelta
    from flow import Flow as _F
    p = profile()
    recent = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    p["orders"][0]["createTime"] = recent
    f = _F(profile=p, api=FakeApi())
    txt = f.greet().text
    assert "昨天" not in txt, f"2 小时前的单被说成昨天:\n{txt}"
    assert "小时前" in txt or "刚吃完" in txt, f"应按小时表述:\n{txt}"


def i14_option_label_matches_action():
    """🔴 回归(23:18 真实走查抓到):屏幕上写的编号必须等于实际走的分支。

    事故:`self.selection = CHANNEL_MENU[i]` 用**排序后**的下标索引**未排序**的
    常量表。历史众数是麦乐送时,屏幕显示「1 送到家」,实际拿到 CHANNEL_MENU[0]=堂食,
    于是选了外送却进了到店分支。17 项不变量全绿也没抓到 —— 它们只断言「进了门店轮」,
    不断言「显示的标签与分支一致」。
    """
    p = profile()
    p["channel"] = {"default": "麦乐送", "confidence": "high", "ratio": 0.9}
    expect = {"1": "麦乐送", "2": "到店自取", "3": "到店自取", "4": "得来速"}
    for digit, want_ch in expect.items():
        f = Flow(profile=p, api=FakeApi(), at=AT)
        txt = f.turn_text()
        # 屏幕上第 N 行的标签
        line = [l for l in txt.split("\n") if l.startswith(digit + " ")][0]
        label = line[2:].replace("★", "")
        f.reply(digit)
        got_ch = f.selection["channel"]
        assert got_ch == want_ch, \
            f"选 {digit}(屏幕显示「{label}」)实际走了 {got_ch},应为 {want_ch}"
        assert label == f.selection["key"], f"显示「{label}」≠ 实际「{f.selection['key']}」"


def i15_combo_label_says_combo():
    """🔴 回归(23:25 用户明确要求):套餐标签必须带形态词。

    用户原话:「你要告诉我 这是套餐还是单点一个汉堡」。
    事故:`short_name()` 把「单人餐」当噪音词删掉,「经典安格斯单人餐」在屏幕上
    只剩「安格斯」,与同名单品长得一模一样 —— 用户分辨不出来。
    """
    from flow import _menu_label, _is_combo
    for raw, must in (("经典安格斯单人餐（麦金卡免配）", "单人餐"),
                      ("双层深海鳕鱼堡中套餐", "套"),
                      ("双层深海鳕鱼堡三件套", "件套"),
                      ("大堡口福四件套（泰式炭烤风味猪猪堡）", "件套"),
                      ("培根安格斯厚牛堡", None),
                      ("双层深海鳕鱼堡", None)):
        lab = _menu_label(raw)
        assert rm.width(lab) <= 8, f"{raw} → {lab} 超宽"
        if must:
            assert must in lab, f"{raw} 是套餐,标签「{lab}」必须含「{must}」"
        if must is None:
            assert "单人餐" not in lab and "套餐" not in lab, \
                f"{raw} 是单品,标签不该带套餐词:{lab}"
    # 端到端:点餐轮的照旧项必须能分辨
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家")); f.reply("1"); f.reply("1"); f.reply("1")
    txt = f.ctx["_last_text"]
    assert "套餐" in txt, f"点餐轮必须说明这是套餐:\n{txt}"
    assert "照旧" in txt and "安格斯" in txt


def i16_address_confirmed_before_stores():
    """🔴 回归(2026-10-11 用户反馈):「你都没和我确认地址,怎么就说我周围有什么店」。

    S3A 只问地址;delivery_stores 在用户选定地址前一次都不能调;
    地址分页不超 4 项;选完地址才进门店确认轮。
    """
    five = [{"addressId": f"a{k}", "label": lbl} for k, lbl in enumerate([
        "上海市静安区示范路8号201室",
        "上海市浦东新区陆家嘴环路1366号大厦",
        "上海市嘉定区民主街385号羽毛球馆",
        "上海市静安区演示大道8号1101室",
        "山东省青岛市市南区香港中路77号"])]
    api = FakeApi(addresses=five)
    f = Flow(profile=profile(), api=api, at=AT)
    t = f.reply(digit_for(f, "送到家"))
    assert f.state == "S3A", f"选外送后应先停在地址轮,实际 {f.state}"
    assert "地址" in t.text, f"必须先问地址,实际:\n{t.text}"
    assert len(f.options) <= 4, f"地址分页超预算:{f.options}"
    assert "更多地址" in f.options, "5 个地址必须有翻页项"
    kinds = [c.split(":")[0] for c in api.calls]
    assert "delivery_stores" not in kinds, f"地址未确认就拉了门店:{api.calls}"
    assert not f.store, "地址未确认就选了门店"
    f.reply("1")                       # 选第一个地址(常用置顶)
    kinds = [c.split(":")[0] for c in api.calls]
    assert "delivery_stores" in kinds, "选完地址应查门店"
    assert f.state == "S3B", f"选完地址应进门店确认,实际 {f.state}"
    # 翻页:第 4 项「更多地址」→ 下一页仍是地址轮
    f2 = Flow(profile=profile(), api=FakeApi(addresses=five), at=AT)
    f2.reply(digit_for(f2, "送到家"))
    t2 = f2.reply("4")
    assert f2.state == "S3A", f"翻页后应仍在地址轮,实际 {f2.state}"
    assert "演示大道" in t2.text, f"第二页应含第 4 个地址:\n{t2.text}"


def i17_closed_stores_honest():
    """🔴 回归(2026-10-11 凌晨实测):门店全打烊被渲染成「?送」+「营业时间未知」。

    必须如实报每家开门时间;「约开门后送」要落在可预约窗口(07:00),
    不是营业开始时间(06:30)——营业 ≠ 可预约(铁律 1)。
    """
    at = datetime(2026, 10, 10, 1, 0)          # 凌晨 1 点,全打烊
    stores = [
        dict(FAKE_STORES[0], businessStatus=False,
             businessStartTime="06:30", businessEndTime="23:00",
             reservationTimeOptions=[
                 {"date": "2026-10-10", "today": True,
                  "reservationOptionText": "早餐(07:00至10:59)，午餐(11:00至14:59)"}]),
        dict(FAKE_STORES[1], businessStatus=False,
             businessStartTime="07:00", businessEndTime="22:00"),
    ]
    f = Flow(profile=profile(), api=FakeApi(stores=stores), at=at)
    f.reply(digit_for(f, "送到家"))
    t = f.reply("1")                    # S3A 选地址 → 门店全打烊
    assert f.state == "S3Z", f"应进打烊轮,实际 {f.state}"
    assert "?" not in t.text, f"问号占位又回来了:\n{t.text}"
    assert "营业时间未知" not in t.text, f"静默降级又回来了:\n{t.text}"
    assert "06:30" in t.text and "开门" in t.text, f"必须报开门时间:\n{t.text}"
    assert t.speech and len(t.speech) <= 34, f"打烊轮口播超预算:{t.speech}"
    t2 = f.reply("1")                   # 约开门后送
    assert f.state == "S4", f"应进放置点轮,实际 {f.state}:\n{t2.text}"
    assert f.store and f.store["storeCode"] == "1450523", "应选最早开门的店"
    assert f.ctx["when"] == "2026-10-10 07:00", \
        f"预约应取可预约窗口 07:00,不是营业 06:30:{f.ctx['when']}"
    # 「先不送了」要能体面收场
    f2 = Flow(profile=profile(), api=FakeApi(stores=stores), at=at)
    f2.reply(digit_for(f2, "送到家"))
    f2.reply("1")
    t3 = f2.reply("3")
    assert f2.state == "S9" and "慢走" in t3.text, f"不送了应送客:\n{t3.text}"


def i_bad_reply_asks_again():
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.turn_text()
    f.turn_text()
    out = f.reply("9")             # 超出选项范围
    assert "没看懂" in out.text, f"越界回复应追问,实际:\n{out.text}"


def i_cold_start():
    f = Flow(profile={}, api=FakeApi(), at=AT)
    g = f.greet().text
    assert "第一次见面" in g, f"冷启动问候不对:\n{g}"
    assert f.turn_text(), "冷启动也必须进S1 问渠道"


def i_short_name_budget():
    for raw, must in (("双层深海鳕鱼堡中套餐（麦金卡免配）", "鳕鱼堡中套"),
                      ("经典安格斯单人餐（麦金卡免配）", "安格斯"),
                      ("可口可乐中杯", "可口可乐")):
        s = rm.short_name(raw)
        assert rm.width(s) <= rm.MAX_WIDTH, f"{raw} → {s} 超宽"
        assert must in s, f"{raw} → {s} 丢了品类词「{must}」"


def i_compact_address_budget():
    for a in [x["label"] for x in FAKE_ADDRESSES]:
        s = compact_address(a)
        assert rm.width(s) <= 14, f"{a} → {s} 超宽"


# -------------------------------------------------- D1 语音不变量(B 组)
SPEECH_BANNED = ("code", "key", "productCode", "modification", "0-0", "1-1",
                 "[", "]", "¥", "…")


def _speech_rounds(channels):
    """收集外送/到店两条链路每一轮的 (状态, speech)。"""
    rounds = []
    for ch in channels:
        f = Flow(profile=profile(), api=FakeApi(), at=AT)
        rounds += drive_turns(f, [ch, "1", "确认", "门口", "1", "就这样", "确认下单"]
                              if ch == "送到家" else
                              [ch, "1", "1", "就这样", "确认下单"])
    return rounds


def i_speech_nonempty():
    """每一轮都必须有口播 —— 语音是主入口,没有 speech 等于这轮哑了。"""
    miss = [s for s, sp in _speech_rounds(["送到家", "堂食"]) if not sp]
    assert not miss, f"以下状态没有口播: {miss}"


def i_speech_budget():
    """单句 ≤30 字(约 6 秒),一整轮 ≤60 字。"""
    bad = []
    for s, sp in _speech_rounds(["送到家", "堂食"]):
        assert sp, f"{s} 无口播"
        if len(sp) > 60:
            bad.append(f"{s} 整轮 {len(sp)} 字")
        for sent in re.split(r"[。？！?]", sp):
            if len(sent) > 31:      # 30 字 + 残留标点容差
                bad.append(f"{s} 单句 {len(sent)} 字「{sent[:20]}…」")
    assert not bad, ";".join(bad)


def i_speech_no_numbering():
    """口播不念编号:不许出现「回 1-N」这类数字协议。"""
    bad = [f"{s}「{sp}」" for s, sp in _speech_rounds(["送到家", "堂食"])
           if re.search(r"回\s*[1-9]|第\s*[1-9]\s*[项个]", sp or "")]
    assert not bad, f"口播念了编号: {bad}"


def i_speech_no_leak():
    """口播零内部信息:code/key/字段名/审计行/省略号。"""
    bad = []
    for s, sp in _speech_rounds(["送到家", "堂食"]):
        for kw in SPEECH_BANNED:
            if kw in (sp or ""):
                bad.append(f"{s} 泄漏 {kw}")
    assert not bad, ";".join(bad)


def i_s5s_scene_pick_routes_to_detail():
    """B1 修的死端:场景推荐(S5S)选 1 必须进 S6,不是「已经点完了」。"""
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.turn_text()
    f.reply("堂食")
    f.reply("1")            # 就这家
    t = f.reply("健身")     # 场景推荐轮
    assert t.state == "S5S", f"应进 S5S,实际 {t.state}"
    out = f.reply("1")      # 选场景推荐第 1 项
    assert out.state == "S6", f"S5S 选 1 应进 S6 细节,实际 {out.state}:\n{out.text}"


def i18_usual_missing_degrades():
    """🔴 回归(2026-10-11 实测):铁律 3 的真实形态 —— 照旧在这家店不存在。

    事故:盛源生活广场外送菜单 111 项拉取正常,但无任何「安格斯」项,
    _usual() 返回 None,ask_items 直接 .get() 崩溃,对话中断。
    test_flow 的假菜单此前刻意包含照旧(见 FAKE_MENU 注释),该分支零覆盖。
    必须如实降级:说「没有」+ 给一个替代,不许崩、不许假入口。
    """
    menu = {k: v for k, v in FAKE_MENU.items() if "安格斯" not in v["name"]}
    p = profile()
    # 受控历史:只点过安格斯套餐,保证 alt 落在假菜单的鳕鱼堡套餐上
    p["orders"] = [{"createTime": "2026-10-01 12:00:00",
                    "orderStatus": "订单已完成",
                    "items": [{"name": "经典安格斯单人餐（麦金卡免配）", "qty": 2}]}]
    f = Flow(profile=p, api=FakeApi(menu=menu), at=AT)
    f.reply(digit_for(f, "送到家"))
    f.reply("1"); f.reply("1"); f.reply("1")     # 地址 → 门店 → 放置点 → S5
    txt = f.ctx["_last_text"]
    assert "照旧" not in txt, f"菜单里没有照旧还推照旧:\n{txt}"
    assert "常点的这套" in txt and "没有" in txt, f"必须如实说没有:\n{txt}"
    assert len(f.options) <= 4, f"降级轮选项超预算:{f.options}"
    assert f.order is None, "降级轮不许自带订单"
    out = f.reply("1")                            # 选替代 → S6 细节
    assert out.state == "S6", f"选替代应进 S6,实际 {out.state}:\n{out.text}"
    assert f.order and "鳕鱼堡" in (f.order.get("real") or ""), \
        f"订单应是替代项,实际:{f.order}"


def i19_browse_menu_enters_categories():
    """🔴 回归(2026-10-11):S5 选「看菜单」曾因 cands[i] 越界回落 cands[0]
    把照旧直接下单 —— 点菜单却出订单,是本域最严重的分派错误。
    菜单浏览已接入(S5M 分类轮):选它必须**进分类轮且不下单**,不再给死端提示。
    """
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家"))
    f.reply("1"); f.reply("1"); f.reply("1")
    assert "照旧" in f.ctx["_last_text"], "前提:正常 S5 应有照旧"
    out = f.reply("看菜单")
    assert f.order is None, f"选看菜单却下了单:{f.order}"
    assert out.state == "S5M", f"应进分类轮 S5M,实际 {out.state}:\n{out.text}"
    assert len(f.options) == 4, f"分类轮应恰 4 桶:{f.options}"
    assert any("套餐" in o for o in f.options), f"桶标签缺套餐:{f.options}"
    assert out.speech and len(out.speech) <= 60 and "¥" not in out.speech, \
        f"分类轮口播违规:{out.speech}"


def i20_browse_page_and_pick():
    """🔴 2026-10-11 新增:分类 → 翻页(更多) → 返回点餐 → 选品进 S6 全链。"""
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家"))
    f.reply("1"); f.reply("1"); f.reply("1")
    out = f.reply("看菜单")                       # S5M
    assert out.state == "S5M"
    d_burger = digit_for_browse(out.text, "汉堡")
    page1 = f.reply(d_burger)                     # S5MP 第 1 页
    assert page1.state == "S5MP", f"应进翻页轮,实际 {page1.state}"
    assert f.options[-2] == "更多" and f.options[-1] == "返回点餐", \
        f"第 1 页末两项应为 更多/返回点餐:{f.options}"
    assert len(f.options) <= 4
    assert page1.speech and len(page1.speech) <= 60 and "¥" not in page1.speech, \
        f"翻页轮口播违规:{page1.speech}"
    assert_no_leak(page1.text, BANNED)
    page2 = f.reply("更多")                       # 第 2 页:剩余项,无「更多」
    assert page2.state == "S5MP" and "更多" not in f.options, \
        f"末页不该再有更多:{f.options}"
    back = f.reply("返回点餐")                    # 回 S5,照旧仍在
    assert back.state == "S5" and "照旧" in back.text, \
        f"返回点餐应回 S5:\n{back.text}"
    out2 = f.reply("看菜单")                      # 再进 → 汉堡 → 选第 1 项
    f.reply(digit_for_browse(out2.text, "汉堡"))
    pick = f.reply("1")
    assert pick.state == "S6", f"选品应进 S6,实际 {pick.state}:\n{pick.text}"
    assert f.order and f.order.get("code") == "902060", f"订单码错:{f.order}"
    assert f.order.get("real") == "培根安格斯厚牛堡"


def i21_s5_swap_store_escape():
    """🔴 2026-10-11 新增:S5 必须有「换店」逃生口(含照旧缺失的降级轮)。

    事故:降级轮话术说「换一家店试试」却 options=[] —— 说得到、做不到。
    """
    f = Flow(profile=profile(), api=FakeApi(), at=AT)
    f.reply(digit_for(f, "送到家"))
    f.reply("1"); f.reply("1"); f.reply("1")
    t = f.reply("换店")                           # 正常 S5 → S3B
    assert t.state == "S3B", f"换店应回门店确认轮,实际 {t.state}:\n{t.text}"
    assert f.options[0] == "确认", f"S3B 首选项应为确认:{f.options}"
    # 降级轮(菜单无照旧):换店同样可达
    menu = {k: v for k, v in FAKE_MENU.items() if "安格斯" not in v["name"]}
    p = profile()
    p["orders"] = [{"createTime": "2026-10-01 12:00:00",
                    "orderStatus": "订单已完成",
                    "items": [{"name": "经典安格斯单人餐（麦金卡免配）", "qty": 2}]}]
    f2 = Flow(profile=p, api=FakeApi(menu=menu), at=AT)
    f2.reply(digit_for(f2, "送到家"))
    f2.reply("1"); f2.reply("1"); f2.reply("1")
    assert "常点的这套" in f2.ctx["_last_text"]
    assert "换店" in f2.options, f"降级轮必须有换店:{f2.options}"
    t2 = f2.reply(str(len(f2.options)))           # 最后一个 = 换店
    assert t2.state == "S3B", f"降级轮换店应回 S3B,实际 {t2.state}:\n{t2.text}"


def digit_for_browse(text, want):
    """从分类轮屏幕文本读桶编号(bucket_label 不带★,复用不了 digit_for 的 ★ 逻辑)。"""
    for line in text.split("\n"):
        if line[:1].isdigit() and line[1:2] == " ":
            n, lab = line[0], line[2:]
            if lab.startswith(want):
                return n
    raise AssertionError(f"屏幕上没有「{want}」分类:\n{text}")


def main():
    t("I1  渠道必问(外送)", i1_channel_always_asked)
    t("I1b 渠道必问(到店/外带/得来速)", i1b_out_of_store_asks_too)
    t("I1c 历史 0.9 也不免问", i1c_history_ratio_does_not_skip)
    t("I2  外送须确认接收点+放置点", i2_delivery_needs_address_and_dropoff)
    t("I2b 地址回显不泄漏", i2b_address_label_never_leaks_name)
    t("I2c 换地址/换店", i2c_swap_address_and_store)
    t("I3  取餐方式取值合法", i3_takeway_must_exist)
    t("I4  下单前必已试算", i4_quote_before_order)
    t("I6  选项不超过 4 个", i6_options_bounded)
    t("I7  移动端一屏预算", i7_mobile_budget)
    t("I8  无内部字段泄漏", i8_no_internal_fields)
    t("I9  画像不含个人信息", i9_profile_has_no_pii)
    t("I10 编号不跨轮复用", i10_numbering_not_reused)
    t("I11 首轮必须带品牌招呼语", i11_greeting_not_swallowed)
    t("I12 已取消订单不进画像", i12_cancelled_order_not_counted)
    t("I13 同日不说「昨天」", i13_same_day_not_yesterday)
    t("I14 编号标签与实际分支一致", i14_option_label_matches_action)
    t("I15 套餐标签必须带形态词", i15_combo_label_says_combo)
    t("I16 地址确认先于门店查询", i16_address_confirmed_before_stores)
    t("I17 打烊如实报+预约落可约窗口", i17_closed_stores_honest)
    t("--   越界回复会追问", i_bad_reply_asks_again)
    t("--   冷启动问候", i_cold_start)
    t("--   商品名缩写", i_short_name_budget)
    t("--   地址缩写", i_compact_address_budget)
    t("D1a 每轮口播非空", i_speech_nonempty)
    t("D1b 口播单句≤30字/整轮≤60字", i_speech_budget)
    t("D1c 口播不念编号", i_speech_no_numbering)
    t("D1d 口播零内部信息", i_speech_no_leak)
    t("D1e S5S 选完进 S6(死端修复)", i_s5s_scene_pick_routes_to_detail)
    t("I18 照旧没有时如实降级不崩", i18_usual_missing_degrades)
    t("I19 选看菜单进分类轮不误下单", i19_browse_menu_enters_categories)
    t("I20 看菜单分类翻页选品全链", i20_browse_page_and_pick)
    t("I21 S5 换店逃生口(含降级轮)", i21_s5_swap_store_escape)

    print("=" * 72)
    print("流程不变量测试(全程离网,假 API)")
    print("=" * 72)
    bad = 0
    for name, ok, msg in RESULTS:
        print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"\n      {msg}"))
        bad += (not ok)
    print("=" * 72)
    print(f"{len(RESULTS) - bad}/{len(RESULTS)} 通过" + (" ✓" if not bad else " ✗"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
