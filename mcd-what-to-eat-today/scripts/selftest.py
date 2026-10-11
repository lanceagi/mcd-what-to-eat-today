#!/usr/bin/env python3
"""新会话自检:装完技能后先跑这个,确认环境可用。

用法:
    python3 scripts/selftest.py
    python3 scripts/selftest.py --full      # 含真实MCP 调用(约 20 次请求)

检查项:
    A. 环境      Token 可读 / 脚本可执行 / 路径可移植
    B. 静态      语法编译 / 隐私白名单 / 画像结构完整
    C. 冷启动    空画像不崩 / 四类降级路径判定
    D. 语音      9 项自检 + 口播不泄漏内部信息
    E. 真实链路  MCP 连通 / 渠道判定 / 外送试算(--full 才跑)
"""
import argparse
import json
import os
import inspect
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PROFILE = os.path.join(ROOT, "assets", "profile.json")
PROFILE_DEMO = os.path.join(ROOT, "assets", "profile.demo.json")


def profile_path():
    """真实画像优先;公开仓库 fresh clone 没有 profile.json(个人画像,已
    gitignore),回退演示画像 profile.demo.json —— 与 test_flow 同规则。"""
    return PROFILE if os.path.exists(PROFILE) else PROFILE_DEMO


def profile_files():
    """所有实际存在的画像文件(隐私检查要逐个扫,一个都不能漏)。"""
    return [p for p in (PROFILE, PROFILE_DEMO) if os.path.exists(p)]


PASS, FAIL, SKIP = "✓", "✗", "—"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((PASS, name, detail or ""))
        print(f"  {PASS} {name}" + (f"  — {detail}" if detail else ""))
        return True
    except Exception as e:
        results.append((FAIL, name, str(e)[:100]))
        print(f"  {FAIL} {name}  — {type(e).__name__}: {str(e)[:90]}")
        return False


# ---------- A. 环境 ----------
def a_token():
    from mcd_client import load_token
    tok = load_token()
    assert len(tok) > 20, f"Token 长度异常:{len(tok)}"
    return f"{len(tok)} 字节,未打印内容"


def a_paths():
    assert os.path.isfile(os.path.join(ROOT, "SKILL.md")), "SKILL.md 不在技能根目录"
    assert os.path.isfile(profile_path()), \
        "profile.json / profile.demo.json 至少要有其一"
    real = os.path.realpath(HERE)
    assert "mcd-what-to-eat-today" in real, f"路径异常:{real}"
    return "相对路径解析正常"


def a_frontmatter():
    txt = open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    assert txt.startswith("---"), "SKILL.md 缺 frontmatter"
    head = txt.split("---")[1]
    assert "name:" in head and "description:" in head, "frontmatter 缺 name 或 description"
    name = [l for l in head.splitlines() if l.startswith("name:")][0]
    return name.strip()


# ---------- B. 静态 ----------
def b_compile():
    pycs = [f for f in os.listdir(HERE) if f.endswith(".py")]
    r = subprocess.run([sys.executable, "-m", "py_compile"] + pycs,
                       cwd=HERE, capture_output=True, text=True)
    assert r.returncode == 0, f"编译失败:{(r.stderr or '')[:120]}"
    return f"{len(pycs)} 个脚本"


def b_privacy():
    """画像绝不能含个人信息(竞赛规则:参赛材料不得含他人个人信息)

    ⚠️ 手机号判定用**整字段精确匹配**,不用裸正则。
       实测踩坑(2026-10-10):裸正则 1[3-9]\\d{9} 会在31 位orderId 里
       匹配出 5 个「疑似手机号」子串,全是误报 —— 例如
       orderId=9001390000000000000000000001 里含 13900000000。
       → 规则:11 位数字必须**整个字段值就是它**,且该字段名不含 id/code/number。
    """
    banned = ["deliveryInfo", "customerNickname", "mobilePhone", "deliveryAddress",
              "riderNickName", "riderMobilePhone", "addressDetail", "payId",
              "pickupCode", "lockerCode", "fullAddress", "contactName", "phone"]
    files = profile_files()
    assert files, "profile.json / profile.demo.json 至少要有其一"
    for p in files:
        txt = open(p, encoding="utf-8").read()
        hit = [b for b in banned if b in txt]
        assert not hit, f"{os.path.basename(p)} 含敏感字段名:{hit}"

        d = json.loads(txt)
        sus = []
        for path, val in _walk(d):
            if not isinstance(val, str) or len(val) != 11 or not val.isdigit():
                continue
            if val[0] not in "13":
                continue
            leaf = path.split(".")[-1].split("[")[0].lower()
            # 业务 ID 类字段允许(订单号/门店码/productCode 都是数字串)
            if any(t in leaf for t in ("id", "code", "number", "no")):
                continue
            sus.append((path, val))
        assert not sus, f"{os.path.basename(p)} 疑似真实手机号字段:{sus}"

        # 姓名类字段整体禁止(门店名允许,是商家信息)
        for path, val in _walk(d):
            if isinstance(val, str) and val.strip() in ("朱老师", "李先生"):
                raise AssertionError(f"{os.path.basename(p)} 含联系人姓名:{path}")
    return f"{len(files)} 个画像文件 × ({len(banned)} 类字段名 + 11位数字精确匹配 + 姓名),均无"


def _walk(obj, path=""):
    """深度遍历,产出 (路径, 值)"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            yield from _walk(x, f"{path}[{i}]")
    else:
        yield path, obj


def b_profile():
    d = json.load(open(profile_path(), encoding="utf-8"))
    for k in ("signature", "channel", "source"):
        assert k in d, f"画像缺字段:{k}"
    has_orders = bool(d.get("orders"))
    if has_orders:
        assert all("orderType" in o for o in d["orders"]), "订单缺 orderType(渠道判定会失效)"
        n_ch = len(d["channel"].get("rank") or [])
        assert n_ch > 0, "channel.rank 为空"
        return f"observed 态,{len(d['orders'])} 单,渠道={d['channel']['default']}"
    return "cold_start 态(新用户画像)"


def b_no_dead_code():
    """死代码检查:私有函数(下划线前缀)必须被调用。

    实测坑(21:38):render_legacy() 定义后从未被调用,20:50 之后一直是死代码,
    里面还留着一份旧的标题文案,与 rank_* 的实际排序逻辑已经不一致。
    → 留着会让人以为有两套渲染路径,读代码时误判行为。

    **只检查下划线前缀的私有函数**,三类公开函数一律豁免:
    - SKILL.md 有记载的对外 API(如 speak_narrow,由宿主按语音流程调用)
    - 故意抛 NotImplementedError 的占位(如 nearby_stores,防上层误用)
    - 有意保留的参考实现
    """
    import ast
    pycs = sorted(f for f in os.listdir(HERE) if f.endswith(".py"))
    src = "\n".join(open(os.path.join(HERE, f), encoding="utf-8").read() for f in pycs)

    dead = []
    for f in pycs:
        t = ast.parse(open(os.path.join(HERE, f), encoding="utf-8").read())
        for node in t.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("_") or node.name == "__main__":
                continue
            if src.count(node.name) <= 1:
                dead.append(f"{f}:{node.name}")
    assert not dead, f"死代码(私有函数定义后从未被调用):{dead}"
    return f"{len(pycs)} 脚本无死私有函数"


def _diff_scripts(other_root):
    """对比本副本与另一副本 scripts/*.py 的 sha256,返回不一致的文件名列表。

    🔴 HERE 是 scripts 目录本身(不是 skill 根),别再拼一层 scripts。
    """
    import hashlib
    names = sorted(f for f in os.listdir(HERE) if f.endswith(".py"))

    def digest(root, n):
        p = os.path.join(root, n)
        if not os.path.exists(p):
            return None
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    mine = {n: digest(HERE, n) for n in names}
    theirs = {n: digest(other_root, n) for n in names}
    return [n for n in names
            if mine[n] and theirs[n] and mine[n] != theirs[n]]


def b_copy_drift():
    """🔴 双副本漂移检测(2026-10-11):开发仓与 ~/.agents 曾漂移 4 个文件。

    同步只能走 Read+Edit 手术(Mimosa hook 拦 cp),人工同步必有漏网 ——
    把「漂移后知后觉」变成「每次自检即报警」。共享四件套同时核对
    mcd-ip-toys-master(逐字拷贝方)。找不到对方副本 = 环境受限,跳过不算失败。

    注:submit/ 脱敏副本对开发副本的**有意差异**(假地址/合成画像)也会被本检查
    报出 —— 在开发机上属预期噪音,评审 fresh clone 无第二副本,自动跳过。
    """
    import glob
    here = os.path.realpath(HERE)
    cands = [os.path.expanduser("~/.agents/skills/mcd-what-to-eat-today"),
             os.path.expanduser("~/Desktop/_AGI-公众号选题/*/mcd-what-to-eat-today")]
    others = sorted({os.path.realpath(p) for pat in cands for p in glob.glob(pat)
                     if os.path.realpath(p) != here})
    if not others:
        return "未找到另一副本,跳过"
    bad = []
    for o in others:
        bad += [f"{os.path.basename(os.path.dirname(o))}/{n}"
                for n in _diff_scripts(o)]
    child = os.path.join(os.path.dirname(os.path.dirname(here)),
                         "mcd-ip-toys-master", "scripts")
    if os.path.isdir(child):
        shared = ("mcd_client.py", "channel.py", "order_builder.py",
                  "render_mobile.py")
        import hashlib
        for n in shared:
            p = os.path.join(child, n)
            mine = os.path.join(HERE, n)
            if os.path.exists(p) and os.path.exists(mine) and hashlib.sha256(
                    open(mine, "rb").read()).hexdigest() != hashlib.sha256(
                    open(p, "rb").read()).hexdigest():
                bad.append(f"mcd-ip-toys-master/{n}")
    if bad:
        raise AssertionError("副本漂移: " + ", ".join(sorted(set(bad))))
    detail = f"{len(others)} 副本 scripts 一致"
    if os.path.isdir(child):
        detail += " + mcd-ip-toys-master 共享四件套一致"
    return detail


def b_scene_honest():
    """🔴 场景开关必须诚实:未实现的场景不能静默降级。

    实测坑(21:38):--scene 的 choices 写了 random/try_new/hurry,
    但 main() 里只有 fitness 分支,其余全落到 rank_saving()——
    传 --scene random 会输出省钱场景的结果,还标着「saving」。
    用户以为在测随便模式,实际拿到省钱榜,且无任何提示。
    → choices 只列已实现场景,未实现的显式报错。
    """
    import argparse
    import recommend
    src = open(os.path.join(HERE, "recommend.py"), encoding="utf-8").read()
    m = re.search(r'--scene[\s\S]{0,400}?choices=\[([^\]]+)\]', src)
    assert m, "未找到 --scene 的 choices 定义"
    # 2026-10-11 阶段 A C 组:hurry/random/try_new 已实现,同步进集合。
    # 🔴 新增场景必须三处同步:choices / 本集合 / SKILL.md 场景表。
    declared = set(re.findall(r'"(\w+)"', m.group(1)))
    implemented = {"saving", "fitness", "hurry", "random", "try_new"}
    fake = declared - implemented
    assert not fake, f"choices 声明了未实现的场景,会静默降级:{sorted(fake)}"
    missing = implemented - declared
    assert not missing, f"实现了却不在 choices 里,入口不通:{sorted(missing)}"

    # 实际调用一次:不存在的事件名必须被 argparse 拒绝(random 已实现,换探针名)
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "recommend.py"), "--scene", "no_such_scene"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "传未知场景竟成功退出,说明它被静默处理了"
    return f"choices={sorted(declared)},与实现一致"


# ---------- C. 冷启动 ----------
def c_empty_profile():
    """空画像不能崩 —— 新用户必经路径。

    🔴 返回值必须取**实际话术行**,不能取首行(21:50 修正):
       render() 首行是「【给用户看的】」这个标签,不是给用户的话。
       早先版本 `out.splitlines()[0]` 返回标签,断言虽然通过,
       但自检输出里看不到任何真实文案,等于没验证到东西。
       这与「文档声称≠代码存在」是同一类病:绿灯不承载信息。
    """
    from greet import build_greeting, render
    from datetime import datetime
    g = build_greeting({}, datetime(2026, 10, 11, 12, 30))
    out = render(g, {})
    assert "喜欢您来" in out, f"冷启动问候异常(应为品牌招呼语):{out[:60]}"
    assert "第一次" in out, "冷启动未走「第一次来」分支"

    # 取标签之后的第一个非空行 = 真正说给用户听的话
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    speech = next((l for l in lines if not l.startswith("【")), "")
    assert speech, "冷启动未产出任何用户可见话术"
    assert speech in out, "话术行不在输出中"
    return speech[:36]


def b_profile_layers():
    """阶段 A A1/A2/A3:画像必须含 habits/taste/avoid 三层且非空壳。

    出口条件②的守门:三层各层都要有真实数据(habits.n>0, taste.n>0;
    avoid 允许为空 —— 没声明过就是没有,空是诚实结论)。
    """
    p = json.load(open(profile_path(), encoding="utf-8"))
    for k in ("habits", "taste", "avoid"):
        assert k in p, f"画像缺 {k} 层(先跑 fetch_profile.py)"
    assert p["habits"].get("n", 0) > 0, "habits 无真实数据(n=0)"
    assert p["taste"].get("n", 0) > 0, "taste 无真实数据(n=0)"
    assert p["habits"].get("topSlot"), "habits 缺 topSlot"
    assert p["taste"].get("proteinRank"), "taste 缺 proteinRank"
    assert "items" in p["avoid"], "avoid 缺 items(可为空列表)"
    return (f"habits {p['habits']['n']}单/{p['habits']['topSlot']} · "
            f"taste {p['taste']['n']}件/{p['taste']['proteinRank'][0][0]} · "
            f"avoid {len(p['avoid']['items'])}条")


def c_greet_gate():
    """阶段 A A4:迎宾门禁 —— 有历史订单时必须命中个性化维度。

    两面都验:真实画像要放行;删掉画像层的通用话术要被拦。
    """
    from datetime import datetime
    from greet import build_greeting, gate, personal_hits
    p = json.load(open(profile_path(), encoding="utf-8"))
    g = build_greeting(p, datetime.now())
    bad = gate(g, p)
    assert not bad, f"真实画像被门禁拦下:{bad}"
    hits = personal_hits(g, p)
    assert hits, "真实画像竟一个维度都不命中"
    # 反面:同一画像,问候行全换成通用客套 → 门禁必须报违规
    g2 = dict(g, lines=["喜欢您来♥️，请看菜单。"], detail="")
    assert gate(g2, p), "通用客套话术未被门禁拦下 —— 门禁失效"
    return f"放行(命中:{'、'.join(hits)}) + 反例被拦"


def c_routes():
    from cold_start import route_of
    cases = [
        ("官方常点+积分", {"frequent_code": "x", "frequent_name": "A", "has_points": True}, "auto_zero_question"),
        ("有常点无积分", {"frequent_code": "x", "frequent_name": "A", "has_points": False}, "reuse_frequent_ask2"),
        ("无常点有积分", {"frequent_code": None, "has_points": True}, "ask_main_only"),
        ("接口全失败", {}, "ask_all_three"),
    ]
    bad = [(l, route_of(s), e) for l, s, e in cases if route_of(s) != e]
    assert not bad, f"路径判定错误:{bad}"
    return f"{len(cases)} 类路径判定正确"


def c_channel_fallback():
    """冷启动无渠道数据 → 必须回落到店,不能卡在需要地址的外送"""
    from channel import resolve_channel, channel_params, CHANNELS
    ch, why = resolve_channel({}, None)
    assert ch == "到店自取", f"冷启动渠道应回落,实际={ch}"
    p = channel_params(ch)
    assert not CHANNELS[ch]["needAddress"], "回落后不应需要地址"
    return f"{ch}({why})"


# ---------- D. 语音 ----------
def d_voice():
    r = subprocess.run([sys.executable, os.path.join(HERE, "voice.py"), "--selfcheck"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"语音自检失败:{(r.stderr or '')[:120]}"
    return "9 项自检通过"


def d_no_leak():
    """口播绝不能泄漏 code/key/字段名 —— B2 后口播由 flow.Turn.speech 产出,
    断言升级为:离网跑两条链路,逐轮检查 speech(不再依赖已删除的 speak_confirm)。"""
    from datetime import datetime
    from flow import Flow
    import test_flow as tf
    banned = ("code", "key=", "productCode", "modification", "0-0", "1-1",
              "[", "]", "deliveryInfo", "takeWayCode")
    worst = 0
    for ch in ("送到家", "堂食"):
        f = Flow(profile=tf.profile(), api=tf.FakeApi(), at=tf.AT)
        speeches = [f.turn_text() and f.ctx["_last_speech"]]
        for r in ([ch, "确认", "门口", "1", "就这样", "确认下单"] if ch == "送到家"
                  else [ch, "1", "1", "就这样", "确认下单"]):
            f.reply(r)
            speeches.append(f.ctx.get("_last_speech") or "")
        for sp in speeches:
            for b in banned:
                assert b not in sp, f"口播泄漏内部信息 {b}:{sp}"
            worst = max(worst, len(sp))
    assert worst <= 60, f"口播过长({worst} 字)会被打断"
    return f"两链路逐轮零泄漏,单轮最长 {worst} 字"


# ---------- E. 真实链路 ----------
def e_tools():
    from mcd_client import list_tools
    n = len(list_tools())
    assert n == 35, f"线上工具数异常:{n}(实测应为 35,文档写 33)"
    return f"{n} 个工具"


def e_channel():
    from channel import resolve_channel
    p = json.load(open(profile_path(), encoding="utf-8"))
    ch, why = resolve_channel(p, None)
    return f"{ch} — {why}"


def e_delivery_quote():
    """外送全链路:地址→门店→菜单→试算(只读)"""
    from mcd_client import call_tool
    from channel import list_addresses, delivery_stores, channel_params, CHANNELS
    addrs = list_addresses()
    assert addrs, "无配送地址"
    aid = next((a["addressId"] for a in addrs if a["city"] == "上海市"), addrs[0]["addressId"])
    from channel import pick_store, pick_when
    stores = delivery_stores(aid, CHANNELS["麦乐送"]["beType"])
    assert stores, "该地址无可送门店"
    # 🔴 不能写死 stores[0]。实测 2026-10-11 00:02:那个门店报
    #    `60003 门店暂停营业`,整组自检因此失败 —— 深夜/凌晨的**环境状态**,
    #    不是代码缺陷。→ 按营业时段挑一家真开着的,挑不到就标「环境受限」。
    s = pick_store(stores)
    if not s:
        return SKIP + " 该地址此刻全部门店打烊/暂停营业(环境限制)"
    when, _why = pick_when(s)
    cp = channel_params("麦乐送", be_code=s["beCode"], address_id=aid)
    menu = call_tool("query-meals", dict(cp, storeCode=s["storeCode"],
                                         reservationDate=when))["data"]["meals"]
    code = next((c for c, m in menu.items() if float(m.get("currentPrice") or 0) >= 20), None)
    assert code, "外送菜单无可试算商品"
    d = call_tool("calculate-price", dict(cp, storeCode=s["storeCode"],
                                          items=[{"productCode": code, "quantity": 1}]))["data"]
    return f"{len(addrs)} 地址 / {s['storeName'][:12]} / 试算 ¥{(d.get('price') or 0)/100:.2f}"


def e_code_differs():
    """🔴 productCode 必须跨渠道不同 —— 20:45 发现 26,防回归"""
    from mcd_client import call_tool
    from channel import list_addresses, delivery_stores, channel_params, CHANNELS
    from datetime import datetime, timedelta
    # 🔴 2026-10-11 12:08 实测:写死 "2026-10-11 12:00" 在当天 12 点后过
    #    服务端 600053(预约时间必须为未来)。联网测试的时刻必须动态取未来。
    rdate = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 12:00")
    mp = call_tool("query-meals", {"storeCode": "1450523", "orderType": 1, "beType": 1,
                                  "reservationDate": rdate})["data"]["meals"]
    addrs = list_addresses()
    aid = next((a["addressId"] for a in addrs if a["city"] == "上海市"), addrs[0]["addressId"])
    stores = delivery_stores(aid, 2)
    s = next((x for x in stores if x["storeCode"] == "1450523"), stores[0])
    cp = channel_params("麦乐送", be_code=s["beCode"], address_id=aid)
    md = call_tool("query-meals", dict(cp, storeCode=s["storeCode"],
                                       reservationDate=rdate))["data"]["meals"]
    pick = "培根安格斯厚牛堡"
    t = next((c for c, m in mp.items() if m["name"] == pick), None)
    d = next((c for c, m in md.items() if m["name"] == pick), None)
    assert t and d, "两渠道都应能找到该商品"
    assert t != d, f"编码未按渠道区分:{t}"
    return f"到店 {t} / 外送 {d} — 不同(正确)"


def e_overlap():
    """菜单重叠度:跨渠道复用菜单会推荐出点不了的品"""
    from mcd_client import call_tool
    from channel import list_addresses, delivery_stores, channel_params, CHANNELS
    from datetime import datetime, timedelta
    rdate = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 12:00")
    mp = call_tool("query-meals", {"storeCode": "1450523", "orderType": 1, "beType": 1,
                                  "reservationDate": rdate})["data"]["meals"]
    addrs = list_addresses()
    aid = next((a["addressId"] for a in addrs if a["city"] == "上海市"), addrs[0]["addressId"])
    stores = delivery_stores(aid, 2)
    s = next((x for x in stores if x["storeCode"] == "1450523"), stores[0])
    cp = channel_params("麦乐送", be_code=s["beCode"], address_id=aid)
    md = call_tool("query-meals", dict(cp, storeCode=s["storeCode"],
                                       reservationDate=rdate))["data"]["meals"]
    overlap = len(set(mp) & set(md))
    assert overlap < len(md) * 0.5, f"重叠 {overlap}/{len(md)} 过高,菜单可能已按渠道拉"
    return f"到店 {len(mp)} / 外送 {len(md)} / 重叠 {overlap}(必须按渠道拉)"


# ---------- F. 2026-10-10 21:27 真人走查回归 ----------
def f_no_dev_jargon():
    """2026-10-11 用户反馈:「等我拉一下菜单」不是点单话术。

    只查会进对话的行(rm.turn / speech= / print 的话术串),注释与
    docstring 不查 —— 内部注释用「拉」是黑话习惯,不约束。
    """
    import glob
    banned = re.compile(r"(拉一下|拉到|拉取|等我拉|参考的菜|查询接口|调用接口|调一下接口)")
    bad = []
    for f in glob.glob(os.path.join(HERE, "*.py")):
        for i, ln in enumerate(open(f, encoding="utf-8"), 1):
            s = ln.strip()
            if not any(k in s for k in ("rm.turn(", "speech=", "speak")):
                continue
            if s.startswith("#") or s.startswith('"""') or '"""' in s:
                continue
            m = banned.search(s)
            if m:
                bad.append(f"{os.path.basename(f)}:{i} 「{m.group(0)}」")
    assert not bad, f"话术里混入开发者黑话:{bad}"
    return "话术零黑话(拉/接口/参考菜品未进对话)"


def f_brand_wording():
    """🔴 回归:品牌招呼语必须是「喜欢您来」,不能是自造的「欢迎您来」。

    走查发现 greet.py 输出了没有出处的「欢迎您来」。麦当劳门店的标准
    招呼语是「喜欢您来」(1992 王府井首店),「我就喜欢」是品牌宣言不是招呼语。
    """
    for f in ("greet.py", "flow.py", "SKILL.md"):
        src = open(os.path.join(HERE, f), encoding="utf-8").read() \
            if f != "SKILL.md" else open(
                os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        assert "喜欢您来" in src, f"{f} 缺品牌招呼语"
        assert "欢迎您来" not in src, f"{f} 仍残留自造话术「欢迎您来」"
    return "喜欢您来♥️(禁用「欢迎您来」)"


def f_render_budget():
    """🔴 回归:渲染器必须真的卡预算。超预算抛错,不许静默截断。"""
    import render_mobile as rm
    over = rm.check(["x" * 40], None)
    assert over, "超宽行竟然通过了检查"
    try:
        rm.turn("今天店里吃,还是送到家?",
                options=["堂食", "外带", "送到家", "来得速", "第五项"])
        raise AssertionError("5 个选项竟然没被拦下")
    except AssertionError as e:
        assert "选项数" in str(e), f"报错信息应指出选项数:{e}"
    assert rm.short_name("双层深海鳕鱼堡中套餐（麦金卡免配）") == "双层深海鳕鱼堡中套"
    return f"≤{rm.MAX_WIDTH:.0f}字/行 ·≤{rm.MAX_OPTIONS}选项 ·超限抛错"


def f_pick_when():
    """🔴 回归:reservationDate 必须落在营业时间内,否则报 600057。"""
    from channel import pick_when
    from datetime import datetime
    st = {"storeName": "T", "businessStatus": True,
          "businessStartTime": "06:30", "businessEndTime": "23:00"}
    when, why = pick_when(st)
    hh = int(when[11:13])
    assert 6 <= hh < 23 or "次日" in why, f"{when} / {why} 落在营业区间外"
    shut = {"storeName": "T", "businessStatus": False}
    try:
        pick_when(shut)
        raise AssertionError("打烊门店竟然没报错")
    except RuntimeError as e:
        assert "打烊" in str(e)
    return f"营业中→{when[11:]} ·打烊→报错"


def f_nearby_no_false_promise():
    """🔴 回归:query-nearby-stores 是关键词搜索,经纬度恒为 0,不许承诺「附近」。"""
    import channel
    src = inspect.getsource(channel)
    assert "searchType" in src and "keyword" in src, "nearby_stores 必须显式传 searchType+keyword"
    skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
    # 只拦「承诺式」表述;文档里写「禁止对用户说附近的门店」是允许的
    promises = ["为您找附近", "推荐附近", "就近的门店", "选择附近的门店",
                "附近的门店送", "离您最近的门店"]
    bad = [p for p in promises if p in skill]
    assert not bad, f"SKILL.md 出现承诺式「附近」表述:{bad}"
    assert "禁止对用户说" in skill or "不承诺" in skill, \
        "SKILL.md 未写明该接口是关键词搜索、不可承诺「附近」"
    return "关键词搜索,已禁用承诺式「附近」"


def f_new_modules():
    """新增模块必须存在且可导入"""
    for m in ("flow", "render_mobile"):
        assert os.path.exists(os.path.join(HERE, m + ".py")), f"缺 {m}.py"
        __import__(m)
    return "flow.py / render_mobile.py 就位"


def f_combo_structure():
    """🔴 回归:套餐写结构是 roundList+comboItemList,不是 rounds+choices。"""
    import order_builder
    detail = {"rounds": [
        {"id": 1, "choices": [{"code": "902060", "quantity": 1,
                               "modification": {"items": []}}]},
        {"id": 4, "choices": [{"code": "903050", "quantity": 0},
                              {"code": "903050", "quantity": 1}]},
    ]}
    item = order_builder.build_combo_item("9900003039", detail)
    rl = item["roundList"]
    assert isinstance(rl[0]["round"], str), "round 必须是字符串"
    assert "comboItemList" in rl[0], "缺 comboItemList"
    assert "choices" not in rl[0], "混进了读结构 choices"
    assert rl[1]["comboItemList"][0]["code"] == "903050"
    return "roundList/comboItemList,round 为字符串"


def f_nutrition_scope():
    """🔴 回归(23:26 外部 agent 实测):营养数字必须带口径(单品/整份)。

    事故:`rank_fitness` 只用 `nut_of(nutrition, m["name"])`,营养表里没有套餐
    → 所有套餐被排除 → 只剩裸堡 → 报「麦香鱼 325kcal / 钠 556mg」。
    那是**一个堡**的数字,整份套餐是 761kcal / 钠 721mg。用户当场发现不一致。
    """
    src = open(os.path.join(HERE, "recommend.py"), encoding="utf-8").read()
    assert "combo_nutrition" in src, "fitness 未走套餐子项叠加"
    assert '"scope"' in src, "推荐结果缺 scope 字段"
    assert "整份" in src and "单品" in src, "渲染未区分口径"
    return "套餐叠加 + 单品/整份标口径"


def f_recommend_channel():
    """🔴 回归(23:26):场景推荐必须吃当轮渠道,不许回退历史众数。"""
    src = open(os.path.join(HERE, "flow.py"), encoding="utf-8").read()
    assert "ask_scene" in src, "flow 缺场景推荐入口"
    seg = src[src.index("def ask_scene"):src.index("def _read_preference")]
    assert "self._params()" in seg, "场景推荐未使用当轮渠道参数"
    assert "rank_fitness(meals, nu, api=" in seg, \
        "场景推荐未传 api → 套餐会被整体排除"
    return "吃当轮渠道 + 传 api 保留套餐"


def f_immediate_order():
    """🔴 回归(test/test-3 缺陷 D2):临近打烊必须走即时单。

    实测:23:19 时 pick_when 产出 reservationDate=23:24,create-order 报
    `600054 预约时间不在可预约时间范围`。改用即时单(不传该字段)成功建单。
    → 「在营业时段内」不等于「可预约」,余量不足时要切即时单。
    """
    from channel import is_immediate, IMMEDIATE_MARGIN
    near = {"businessStatus": True, "businessStartTime": "06:30",
            "businessEndTime": "23:00"}
    far = {"businessStatus": True, "businessStartTime": "06:30",
           "businessEndTime": "23:59"}
    from datetime import datetime
    assert is_immediate(near, at=datetime(2026, 10, 10, 23, 45)), \
        f"距打烊 15 分钟应判即时单(阈值 {IMMEDIATE_MARGIN})"
    assert not is_immediate(far, at=datetime(2026, 10, 10, 12, 0)), \
        "中午距打烊很久,不该判即时单"
    assert is_immediate({"businessStatus": False}), "打烊门店应判即时单"
    return f"余量 <{IMMEDIATE_MARGIN} 分钟 → 即时单"


def f_recite_reflects_mounted_only():
    """🔴 回归(test/test-3 缺陷 D4):复诵只报本单真挂上的签名。

    实测:本单只有汉堡、没有饮料,「少冰」无处挂载,但 S6 仍写「去芝士、少冰 已记住」,
    用户以为少冰也会生效。→ 挂不上的签名要如实说「未生效」。
    """
    src = open(os.path.join(HERE, "flow.py"), encoding="utf-8").read()
    assert 'ctx["mounted"]' in src, "flow 未记录实际挂载的签名"
    assert "未生效" in src, "未对未生效的签名做说明"
    assert "_drink_mounted" in src, "缺饮料挂载判定"
    return "复诵按实际挂载,挂不上如实说未生效"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="含真实 MCP 调用")
    args = ap.parse_args()

    print("=" * 68)
    print("今天吃什么 · 新会话自检")
    print("=" * 68)

    print("\n【A. 环境】")
    check("Token 可读", a_token)
    check("路径可移植", a_paths)
    check("SKILL.md frontmatter", a_frontmatter)

    print("\n【B. 静态】")
    check("脚本编译", b_compile)
    check("隐私白名单", b_privacy)
    check("画像结构", b_profile)
    check("画像三层(阶段A)", b_profile_layers)
    check("无死代码", b_no_dead_code)
    check("场景开关诚实", b_scene_honest)
    check("双副本漂移", b_copy_drift)

    print("\n【C. 冷启动】")
    check("空画像问候", c_empty_profile)
    check("四类降级路径", c_routes)
    check("渠道回落", c_channel_fallback)
    check("迎宾门禁(阶段A)", c_greet_gate)

    print("\n【D. 语音】")
    check("9项自检", d_voice)
    check("口播零泄漏", d_no_leak)

    print("\n【F. 走查回归(21:27 五条批评)】")
    check("话术零黑话", f_no_dev_jargon)
    check("品牌招呼语", f_brand_wording)
    check("移动端预算", f_render_budget)
    check("营业时间闸门", f_pick_when)
    check("门店措辞诚实", f_nearby_no_false_promise)
    check("新增模块就位", f_new_modules)
    check("套餐写结构", f_combo_structure)
    check("营养口径标注", f_nutrition_scope)
    check("场景吃当轮渠道", f_recommend_channel)
    check("临近打烊走即时单", f_immediate_order)
    check("复诵按实际挂载", f_recite_reflects_mounted_only)

    if args.full:
        print("\n【E. 真实 MCP 链路】")
        check("tools/list", e_tools)
        check("渠道判定", e_channel)
        check("外送全链路试算", e_delivery_quote)
        check("编码跨渠道不同", e_code_differs)
        check("菜单重叠度", e_overlap)
    else:
        print("\n【E. 真实 MCP 链路】")
        print(f"  {SKIP} 已跳过(加 --full 跑,约 20 次只读请求)")

    ok = sum(1 for r in results if r[0] == PASS)
    bad = [r for r in results if r[0] == FAIL]
    print()
    print("=" * 68)
    if bad:
        print(f"结果:{ok} 通过 / {len(bad)} 失败")
        for _, n, d in bad:
            print(f"  ✗ {n}: {d}")
        print()
        print("修复后再跑。失败项通常对应实测发现的回归。")
        return 1
    print(f"结果:{ok} 项全部通过 ✓")
    print()
    print("⚠️ 本脚本走直连(Authorization: Bearer <token>),不经过宿主 MCP 连接器。")
    print("   它通过 ≠ 宿主连接器可用。宿主判据:新会话里能否调到 query-meals。")
    if not args.full:
        print("   提示:加 --full 跑真实 MCP 链路,确认线上连通。")
    print()
    print("两条链路的差异与宿主侧排查见 docs/19-MCP接入操作卡.md。")
    print("已实测:手改 connectors/default/mcp.json 的条目从未被消费,")
    print("        界面添加走 custom-mcp: 独立命名空间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
