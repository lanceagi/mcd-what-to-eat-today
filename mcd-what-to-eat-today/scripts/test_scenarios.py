#!/usr/bin/env python3
"""场景双跑验证:所有点单场景各跑两遍,只读 + 试算,绝不调 create-order。

## 🔴 写操作物理封锁(本脚本的第一职责)
`call_tool` 被包一层 `_read_only`。命中写操作名单直接抛 `WriteBlocked`。

## 为什么要跑两遍
单跑只证明「这条路能走通」。双跑证明三件事:
1. **确定性** —— 门店排序、价格、菜单项数是否在两次调用之间漂移
2. **幂等性** —— 试算不产生副作用,第一次试算不应改变第二次的价格
3. **降级稳定** —— 失败场景的失败方式是否一致(会不会这次 400、下次静默成功)

## 三条硬约束(2026-10-10 21:5x 实测踩出来的,改代码前先读)
1. **`reservationDate` 必须落在门店营业时间内**,否则 `600057 门店可能已关闭或不在营业时间`。
   营业时间来自门店对象的 `businessStartTime` / `businessEndTime` / `businessStatus`。
   ⚠️ 旧版 channel.py 从不读这三个字段,一律用「当前时间+2h」,晚上必挂。
2. **`query-nearby-stores` 默认 searchType=1 查收藏**,用户没收藏记录时报
   `600050 收藏餐厅列表为空`。必须显式 `searchType=2` + `city` + `keyword`。
   ⚠️ 实测该接口返回的 `longitude`/`latitude` 恒为 0.0 —— 它是**关键词搜索**,
   不是真正的「附近」。`distance` 是关键词命中后的相对值。
3. **`query-meal-assistance` 的 required 是 `storeCode`**,不是 addressId。
   先经 delivery-query-stores 拿 storeCode+beCode,再问助餐服务。

## 用法
    python3 scripts/test_scenarios.py
    python3 scripts/test_scenarios.py --city 上海市 --keyword 静安
    python3 scripts/test_scenarios.py --json /tmp/scen.json
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcd_client import call_tool as _raw_call_tool  # noqa: E402
from channel import (list_addresses, delivery_stores, pick_store,  # noqa: E402
                     pick_when)


class EnvBlocked(RuntimeError):
    """环境不满足(门店全打烊 / 无该城市地址 / 该时段无服务),**不是代码缺陷**。

    2026-10-10 23:26 实测:静安关键词下的到店门店(22:00 闭店)与得来速门店
    (23:00 闭店)在 23 点后全部打烊,这是真实环境状态。
    → 场景脚本必须把它和「试算报错」分开报,否则一键测试会在深夜误报失败。
    """


def _pick_open(stores, at=None):
    s = pick_store(stores, at=at)
    if not s:
        raise EnvBlocked("此刻所有候选门店都打烊(" + "、".join(
            f"{x.get('storeName','')[:12]}{x.get('businessEndTime','')}"
            for x in stores[:3]) + ")")
    return s

WRITE_OPS = {
    "create-order", "cancel-order", "auto-bind-coupons", "draw-lottery",
    "mall-create-order", "party-order-create", "delivery-create-address",
}


class WriteBlocked(RuntimeError):
    pass


def call_tool(name, arguments=None, timeout=30):
    """只读代理。写操作名单命中即抛错,不给任何绕过口子。"""
    if name in WRITE_OPS:
        raise WriteBlocked(f"⛔️ {name} 属写操作,场景验证脚本禁止调用")
    return _raw_call_tool(name, arguments, timeout)


def yuan(v):
    try:
        return round(float(v) / 100, 2)
    except (TypeError, ValueError):
        return None


SIG = {"去芝士": True, "少冰": True}


# ---------------------------------------------------------------- 营业时间
def _hm(s):
    return int(s[:2]) * 60 + int(s[3:5])


def pick_when(store, force=None):
    if force:
        return force, "已钉死(与第一次跑同参数)"
    """按门店营业时间算一个合法的 reservationDate。

    非预约场景本可不传 reservationDate,但 query-meals 强制要一个**未来时间**,
    所以这里返回「当前时间+5 分钟,落在营业区间内」的最近合法点。
    """
    st, et = store.get("businessStartTime"), store.get("businessEndTime")
    if not store.get("businessStatus", True):
        raise RuntimeError(f"门店已打烊({store.get('storeName')})")
    now = datetime.now()
    if not st or not et:
        return (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"), "无营业时段字段,取+5min"
    sm, em, nm = _hm(st), _hm(et), now.hour * 60 + now.minute
    if nm >= em:
        day = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return f"{day} {st}", f"已过营业时间 {st}-{et},改约次日开门"
    if nm < sm:
        return f"{now:%Y-%m-%d} {st}", f"未到营业时间 {st}-{et},取开门时刻"
    return (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"), \
           f"营业中 {st}-{et},取+5min"


# ---------------------------------------------------------------- 定制组装
def kind_of(name):
    if any(k in name for k in ("堡", "汉堡")):
        return "burger"
    if any(k in name for k in ("可乐", "雪碧", "橙汁", "茶", "咖啡", "美式", "牛奶", "豆浆")):
        return "drink"
    return "other"


def mount_modification(holder, kind):
    """按实测两类结构组装 modification.values,返回 (values, notes)。"""
    values, notes = [], []
    groups = (holder.get("modification") or {}).get("items") or []
    if not groups:
        return [], ["无可定制项"]
    for grp in groups:
        gvals = grp.get("values") or []
        if any(v.get("unselectedKey") for v in gvals):
            for v in gvals:                      # A 型:全量传
                key = v.get("selectedKey")
                if SIG.get("去芝士") and v.get("name") in ("吉士", "高达干酪片"):
                    key = v.get("unselectedKey")
                    notes.append(f"去{v.get('name')}")
                values.append({"code": v.get("code"), "key": key, "quantity": 1})
        else:                                    # B 型:单选
            hit = next((v for v in gvals
                        if SIG.get("少冰") and v.get("name") == "少冰"), None)
            if hit is None and kind == "drink":
                hit = next((v for v in gvals if v.get("name") == "标准"), None)
            hit = hit or gvals[0]
            notes.append(f"冰量={hit.get('name')}")
            values.append({"code": hit.get("code"),
                           "key": hit.get("selectedKey"), "quantity": 1})
    return values, notes


def build_combo(combo_code, detail):
    """套餐组装。roundList:[{round:"1", comboItemList:[{code,quantity,modification}]}]
    ⚠️ round 是**字符串**;写成 id/choices 拿 500。"""
    rounds = []
    for r in detail.get("rounds") or []:
        pick = next((c for c in r["choices"] if c.get("quantity") == 1),
                    (r["choices"][0] if r.get("choices") else None))
        if not pick:
            continue
        entry = {"code": pick["code"], "quantity": 1}
        vals, _ = mount_modification(pick, kind_of(pick.get("name", "")))
        if vals:
            entry["modification"] = {"values": vals}
        rounds.append({"round": str(r["id"]), "comboItemList": [entry]})
    return {"productCode": combo_code, "quantity": 1, "roundList": rounds}


# ---------------------------------------------------------------- 场景实现
def _menu(cp, store, when):
    d = call_tool("query-meals", dict(cp, storeCode=store, reservationDate=when))
    return (d.get("data") or {}).get("meals") or {}


def _candidates(meals, wants):
    out = []
    for w in wants:
        for c, m in meals.items():
            if w in (m.get("name") or ""):
                out.append((c, m.get("name")))
    return out


def _build(cp, store, when):
    """单品组合:培根安格斯(去芝士) + 饮料(少冰)。找不到就报缺,绝不跨渠道复用编码。

    🔴 实测坑:到店渠道有两个「可乐」类商品 ——
       `3010 雪碧` 有冰量组(标准/去冰/多冰/少冰),`9900008751 可乐` **modification 为空**。
       按名字顺序取第一个会选到挂不上签名的那个。
       → 必须在候选里挑**真的支持该签名**的那一个,挑不到就如实报「签名未挂上」。
    """
    meals = _menu(cp, store, when)
    items, picked, gaps = [], {}, []

    for wants, kind, sig_key in ((("培根安格斯厚牛堡", "安格斯"), "burger", "去芝士"),
                                 (("可口可乐", "可乐", "雪碧"), "drink", "少冰")):
        cands = _candidates(meals, wants)
        chosen, chosen_name, chosen_mods = None, None, []
        for code, nm in cands:
            d = (call_tool("query-meal-detail",
                           dict(cp, code=code, storeCode=store, reservationDate=when))
                 .get("data") or {})
            vals, notes = mount_modification(d, kind)
            hit = any(n.startswith("去") for n in notes) if sig_key == "去芝士" \
                else any("少冰" in n for n in notes)
            if hit:
                chosen, chosen_name, chosen_mods = code, nm, notes
                break
            if chosen is None:
                chosen, chosen_name, chosen_mods = code, nm, notes
        if not chosen:
            gaps.append(f"{sig_key}:该渠道无候选商品")
            continue
        item = {"productCode": chosen, "quantity": 1}
        if chosen_mods and chosen_mods != ["无可定制项"]:
            item["modification"] = {"values": _rebuild(cp, store, when, chosen, kind)}
        items.append(item)
        mounted = "已挂载" if any(
            ("去" in n if sig_key == "去芝士" else "少冰" in n) for n in chosen_mods) else "未挂上"
        picked[f"{sig_key}→{chosen_name}"] = {
            "code": chosen, "name": chosen_name, "mods": chosen_mods, "sig": mounted}
        if mounted == "未挂上":
            gaps.append(f"{sig_key}:菜单里没有支持该定制的商品")
    if not items:
        raise RuntimeError(f"该渠道菜单无「安格斯/饮料」(菜单 {len(meals)} 项)")
    return meals, items, picked, gaps


def _rebuild(cp, store, when, code, kind):
    d = (call_tool("query-meal-detail",
                   dict(cp, code=code, storeCode=store, reservationDate=when))
         .get("data") or {})
    vals, _ = mount_modification(d, kind)
    return vals


def _nearby(be_type, city, keyword):
    d = call_tool("query-nearby-stores",
                  {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword})
    data = d.get("data")
    lst = data if isinstance(data, list) else \
        ((data or {}).get("stores") or (data or {}).get("list") or [])
    if not lst:
        raise RuntimeError("query-nearby-stores 返回空")
    return lst


def sc_store(ctx, city, keyword):
    """到店(beType=1)。无 beCode。"""
    lst = _nearby(1, city, keyword)
    store = _pick_open(lst)
    when, why = pick_when(store, ctx.get("force_when"))
    ctx["store"] = store["storeCode"]
    cp = {"orderType": 1, "beType": 1}
    meals, items, picked, gaps = _build(cp, store["storeCode"], when)
    ctx["meta"] = {"storeName": store.get("storeName"), "distance": store.get("distance"),
                   "hours": f"{store.get('businessStartTime')}-{store.get('businessEndTime')}",
                   "when": when}
    ctx["note"] = why
    return cp, meals, items, picked, gaps


def sc_curbside(ctx, city, keyword):
    """得来速(beType=5)。beCode 只能来自 query-nearby-stores。"""
    lst = [x for x in _nearby(5, city, keyword) if x.get("beCode")]
    if not lst:
        raise RuntimeError("该关键词下没有得来速门店")
    store = _pick_open(lst)
    when, why = pick_when(store, ctx.get("force_when"))
    ctx["store"] = store["storeCode"]
    cp = {"orderType": 1, "beType": 5, "beCode": store["beCode"]}
    meals, items, picked, gaps = _build(cp, store["storeCode"], when)
    ctx["meta"] = {"storeName": store.get("storeName"), "distance": store.get("distance"),
                   "hours": f"{store.get('businessStartTime')}-{store.get('businessEndTime')}",
                   "when": when}
    ctx["note"] = why
    return cp, meals, items, picked, gaps


def sc_delivery(ctx, city, keyword):
    """麦乐送(beType=2)。"""
    addrs = list_addresses()
    hit = next((a for a in addrs if a["city"] == city), None)
    if not hit:
        cities = sorted({a["city"] for a in addrs if a.get("city")})
        raise EnvBlocked(f"配送地址列表里没有 {city}(现有:{'/'.join(cities)})")
    stores = delivery_stores(hit["addressId"], 2)
    if not stores:
        raise RuntimeError("该地址无可送门店")
    store = _pick_open(stores)
    when, why = pick_when(store, ctx.get("force_when"))
    ctx["store"] = store["storeCode"]
    cp = {"orderType": 2, "beType": 2, "beCode": store["beCode"],
          "addressId": hit["addressId"]}
    meals, items, picked, gaps = _build(cp, store["storeCode"], when)
    ctx["meta"] = {"storeName": store.get("storeName"),
                   "hours": f"{store.get('businessStartTime')}-{store.get('businessEndTime')}",
                   "when": when, "whenWhy": why, "addrCount": len(addrs)}
    return cp, meals, items, picked, gaps


def sc_group(ctx, city, keyword):
    """企业团餐(beType=6)。先 delivery-query-stores,再 query-meal-assistance。"""
    addrs = list_addresses()
    hit = next((a for a in addrs if a["city"] == city), None)
    if not hit:
        cities = sorted({a["city"] for a in addrs if a.get("city")})
        raise EnvBlocked(f"配送地址列表里没有 {city}(现有:{'/'.join(cities)})")
    stores = delivery_stores(hit["addressId"], 6)
    if not stores:
        raise RuntimeError("该地址无团餐门店")
    store = _pick_open(stores)
    when, why = pick_when(store, ctx.get("force_when"))
    ctx["store"] = store["storeCode"]
    cp = {"orderType": 2, "beType": 6, "beCode": store["beCode"],
          "addressId": hit["addressId"]}
    svc = (call_tool("query-meal-assistance",
                     {"storeCode": store["storeCode"], "beType": 6,
                      "orderType": 2, "beCode": store["beCode"]}).get("data") or {})
    # 🔴 实测:返回键是 mealAssistanceItems(不是 list),每项含
    #    gmServiceCode / enable / unusableReason / selected
    items6 = svc.get("mealAssistanceItems") or []
    usable = [x for x in items6 if x.get("enable")]
    if not usable:
        raise RuntimeError("当前时段无可用助餐服务:" + "；".join(
            f"{x.get('gmServiceName')}({x.get('unusableReason')})" for x in items6))
    pick6 = next((x for x in usable if x.get("selected")), usable[0])
    cp["gmServiceCode"] = pick6["gmServiceCode"]
    meals, items, picked, gaps = _build(cp, store["storeCode"], when)
    ctx["meta"] = {"storeName": store.get("storeName"), "when": when, "whenWhy": why,
                   "gmServiceCode": cp["gmServiceCode"],
                   "gmServiceName": pick6.get("gmServiceName"),
                   "assistTotal": len(items6), "assistUsable": len(usable)}
    return cp, meals, items, picked, gaps


SCENARIOS = [
    ("到店·堂食/外带", sc_store),
    ("得来速", sc_curbside),
    ("麦乐送", sc_delivery),
    ("企业团餐", sc_group),
]


def run_once(fn, city, keyword, force_when=None):
    """force_when:把 reservationDate 钉死成第一次的值。
    🔴 不钉死会出**假阳性**:两次调用跨了分钟边界(21:59 / 22:00),
    meta.when 变了就被判成不一致,而实际业务参数完全相同。"""
    ctx = {"force_when": force_when}
    t0 = time.time()
    try:
        cp, meals, items, picked, gaps = fn(ctx, city, keyword)
        data = (call_tool("calculate-price",
                          dict(cp, storeCode=ctx["store"], items=items)).get("data") or {})
        return {
            "ok": True, "store": ctx["store"], "meta": ctx.get("meta"),
            "params": {k: v for k, v in cp.items() if k != "items"},
            "menu_count": len(meals),
            "picked": {k: v["code"] for k, v in picked.items()},
            "mods": {k: v["mods"] for k, v in picked.items()},
            "sigMount": {k: v["sig"] for k, v in picked.items()},
            "gaps": gaps,
            "originalPrice": yuan(data.get("originalPrice")),
            "price": yuan(data.get("price")),
            "discount": yuan(data.get("discount")),
            "takeWayList": [t.get("code") for t in (data.get("takeWayList") or [])],
            "note": ctx.get("note"),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except EnvBlocked as e:
        return {"ok": False, "env_blocked": True, "error": str(e)[:200],
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "env_blocked": False,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed_ms": int((time.time() - t0) * 1000)}


# 参与一致性比对的字段必须排除:
#   elapsed_ms —— 天然每次不同
#   note / whenWhy —— 说明文案,两次跑的措辞必然不同(第二次是"已钉死")
EXCLUDE = {"elapsed_ms", "note"}


def _strip(d):
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in ("whenWhy",)}


def diff(a, b):
    out = []
    for k in sorted((set(a) | set(b)) - EXCLUDE):
        if _strip(a.get(k)) != _strip(b.get(k)):
            out.append(k)
    return out


def main():
    city, keyword, out_json = "上海市", "静安", None
    if "--city" in sys.argv:
        city = sys.argv[sys.argv.index("--city") + 1]
    if "--keyword" in sys.argv:
        keyword = sys.argv[sys.argv.index("--keyword") + 1]
    if "--json" in sys.argv:
        out_json = sys.argv[sys.argv.index("--json") + 1]

    print("=" * 76)
    print(f"场景双跑验证 · {city}/{keyword} · 只读+试算 · 写操作已封锁")
    print("=" * 76)
    report = []
    for name, fn in SCENARIOS:
        r1 = run_once(fn, city, keyword)
        time.sleep(0.4)
        fw = (r1.get("meta") or {}).get("when") if r1["ok"] else None
        r2 = run_once(fn, city, keyword, force_when=fw)
        d = diff(r1, r2)
        report.append({"scenario": name, "run1": r1, "run2": r2, "diff": d})
        print(f"\n【{name}】{'两跑一致 ✓' if not d else '两跑不一致 ✗ ' + ','.join(d)}")
        if r1["ok"] and r2["ok"]:
            m = r1.get("meta") or {}
            print(f"  门店 {r1['store']} {m.get('storeName') or ''} {m.get('hours') or ''}")
            print(f"  预约时间 {m.get('when')}  ({r1.get('note')})")
            print(f"  菜单 {r1['menu_count']} 项 | 编码 {r1['picked']}")
            print(f"  定制 {r1['mods']}")
            print(f"  签名挂载 {r1['sigMount']}")
            if r1.get("gaps"):
                print(f"  ⚠️ 未覆盖 {r1['gaps']}")
            print(f"  原价 ¥{r1['originalPrice']} → 应付 ¥{r1['price']}  优惠 ¥{r1['discount']}")
            print(f"  取餐方式 {r1['takeWayList'] or '—（外送不适用）'}")
            print(f"  耗时 {r1['elapsed_ms']}ms / {r2['elapsed_ms']}ms")
        elif r1.get("env_blocked"):
            print(f"  ⏭ 环境受限  {r1['error']}")
        else:
            print(f"  run1 {'OK' if r1['ok'] else '✗ ' + r1['error']}")
            print(f"  run2 {'OK' if r2['ok'] else '✗ ' + r2['error']}")
    ok = sum(1 for r in report if r["run1"]["ok"] and r["run2"]["ok"])
    env = sum(1 for r in report if r["run1"].get("env_blocked"))
    bad = [r for r in report
           if not r["run1"]["ok"] and not r["run1"].get("env_blocked")]
    same = sum(1 for r in report if not r["diff"])
    print("\n" + "=" * 76)
    print(f"跑通 {ok}/{len(SCENARIOS)} · 两跑一致 {same}/{len(SCENARIOS)}"
          + (f" · 环境受限 {env} 项(非缺陷)" if env else ""))
    print("=" * 76)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"city": city, "keyword": keyword, "scenarios": report},
                      f, ensure_ascii=False, indent=1)
        print(f"原始结果: {out_json}")
    # 🔴 环境受限不算失败 —— 深夜跑到店里都关门了,那是事实不是 bug。
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
