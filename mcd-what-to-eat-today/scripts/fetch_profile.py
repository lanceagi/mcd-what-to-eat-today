#!/usr/bin/env python3
"""订单指纹构建器:order-list → 逐单 query-order → 解析定制后缀 → 生成画像。

实测依据(2026-10-10):
- order-list 无参数,固定返回最近 10 单;productName 的括号是套餐自带描述,不含定制
- 定制信息唯一来源:query-order 的 data.orderProductList[].comboItemList[].itemName
  例:"芝士安格斯厚牛堡 高达干酪片(不要)"、"可口可乐中杯 冰(少冰)"
- 子项字段名:order-list 用 name,query-order 用 itemName(解析器须兼容)
- 隐私:query-order 的 deliveryInfo 含手机号/地址,本脚本白名单外字段一律丢弃
- 🔴 2026-10-10 23:13 修正:`order-list` 的每单带 `orderStatus`,取值见过
  「订单已完成」「订单已取消」。**已取消的订单必须剔除** —— 用户没付款的单
  不能算进「照旧」和签名统计。修正前那笔 21:33 的取消单被算成「少冰第 2 次」,
  导致间隔算成「昨天刚吃过」,而它从没成交。

用法:python3 fetch_profile.py [输出路径,默认 assets/profile.json]
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcd_client import call_tool  # noqa: E402

# ---------- 定制归一化表(实测 code 取值,详见 references/mcp-api.md) ----------
CHEESE_ITEMS = {"吉士": "100141", "高达干酪片": "102215"}
# 未成交的订单状态(实测:「订单已取消」)。这些单不进画像。
CANCELLED = {"订单已取消", "已取消", "订单关闭", "已关闭"}
ICE_CODE = "200002"
ICE_KEY = {"去冰": "0-0", "标准": "0-1", "多冰": "0-2", "少冰": "1-1"}

# 白名单:订单级字段(其余全部丢弃,防止个人信息泄漏)
ORDER_FIELDS = {"orderId", "storeCode", "storeName", "orderType", "beType",
                "orderStatus", "createTime", "totalAmount", "realTotalAmount",
                "totalDiscountAmount"}
PRODUCT_FIELDS = {"productName", "quantity", "price"}

# ---------- 渠道映射(官方 create-order schema 原文,实测 2026-10-10 验证) ----------
# orderType: 1-到店(含到店自取+得来速车道取餐) / 2-外送(含麦乐送+企业团餐)
# beType:    1-到店取餐 / 2-麦乐送到家 / 5-得来速(DT) / 6-企业团餐
# 官方传参规则:
#   到店自取(beType=1) → orderType=1,不传 beCode
#   得来速(beType=5)   → orderType=1,必传 beCode(来自 query-nearby-stores)
#   麦乐送(beType=2)   → orderType=2,必传 beCode(来自 delivery-query-stores)+ 必填 addressId
#   团餐(beType=6)→ orderType=2,必传 beCode + 必填 addressId + gmServiceCode
CHANNEL_LABEL = {
    "1/1": "到店自取",
    "1/5": "得来速",
    "2/2": "麦乐送",
    "2/6": "企业团餐",
}

# 用户明确声明过的「不吃」落这里(由 flow._read_preference 写,此处只读合并)
DECLARED_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "assets", "declared.json")

CUSTOM_RE = re.compile(r"\(([^()]+)\)")

# ---------- 口味分类表(A2,2026-10-11) ----------
# 依据:①画像 9 单真实品名(安格斯/鳕鱼堡/猪猪堡/麦满分/四拼小食)
#      ②麦当劳中国常驻菜单。按「先命中先归类」,一个商品只进一个蛋白桶。
#      🔴 吉士汉堡/汉堡包是牛肉堡(不是猪肉),别按「吉士」误进猪桶。
TASTE_PROTEIN = [
    ("牛", ("安格斯", "巨无霸", "牛肉", "吉士汉堡", "双层吉士", "汉堡包")),
    ("鱼", ("鳕鱼", "麦香鱼")),
    ("鸡", ("麦辣鸡", "板烧", "鸡腿", "麦香鸡", "麦乐鸡", "鸡翅", "鸡")),
    ("猪", ("猪猪堡", "猪柳", "培根", "火腿", "麦满分")),
]
# 口味维度(辣/甜/炸),命中计数;0 也是有效结论(「从不点辣」是口味画像的一部分)
TASTE_FLAVOR = [
    ("辣", ("辣",)),
    ("甜", ("甜", "派", "圆筒", "冰淇淋", "麦旋风", "蛋挞", "奶昔", "香芋", "草莓", "巧克力")),
    ("炸", ("麦乐鸡", "鸡翅", "炸", "薯条", "小食", "翅")),
]
# 品类(主食/小食/饮品/甜品),按名称判,先命中先归类
TASTE_CATEGORY = [
    ("饮品", ("可乐", "雪碧", "咖啡", "奶茶", "奶昔", "果汁", "柠檬", "水", "可乐")),
    ("甜品", ("圆筒", "冰淇淋", "麦旋风", "蛋挞", "派", "甜品")),
    ("小食", ("薯条", "麦乐鸡", "鸡翅", "玉米", "沙拉", "小食", "翅", "鸡块")),
    ("主食", ("堡", "套餐", "单人餐", "件套", "汉堡", "卷")),
]


def _first_hit(name: str, table):
    for label, kws in table:
        if any(k in name for k in kws):
            return label
    return None


def build_habits(orders) -> dict:
    """A1 时段习惯:下单时刻分布、星期分布、最活跃时段。

    时段口径与 greet.py 的 SLOTS 完全一致(早餐6-10 午餐10-15
    下午茶15-17 晚餐17-21 夜宵21-24),两处不一致会让「你常什么时候来」
    在问候和画像里各说各话。
    """
    SLOTS = (("早餐", 6, 10), ("午餐", 10, 15), ("下午茶", 15, 17),
             ("晚餐", 17, 21), ("夜宵", 21, 24))
    WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    hours, slots, weekdays = [], Counter(), Counter()
    for o in orders:
        t = o.get("createTime")
        if not t:
            continue
        try:
            dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        hours.append(dt.hour)
        slots[next((n for n, lo, hi in SLOTS if lo <= dt.hour < hi), "夜宵")] += 1
        weekdays[WEEKDAYS[dt.weekday()]] += 1
    n = len(hours)
    if not n:
        return {"basis": "无可用 createTime", "n": 0}
    peak_hour = Counter(hours).most_common(1)[0]
    top_slot = slots.most_common(1)[0]
    # 星期分布按周内顺序输出,方便「你常周六来」这类陈述有据可查
    weekday_rank = [(d, weekdays[d]) for d in WEEKDAYS if weekdays[d]]
    return {
        "n": n,
        "peakHour": peak_hour[0],
        "peakHourCount": peak_hour[1],
        "slotRank": slots.most_common(),
        "topSlot": top_slot[0],
        "topSlotCount": top_slot[1],
        "weekdayRank": sorted(weekday_rank, key=lambda x: -x[1]),
        "basis": f"{n} 单 createTime",
    }


def build_taste(orders) -> dict:
    """A2 口味偏好:从商品品类与名称聚合,不掺任何推断。

    一个商品只进一个桶(先命中先归类);份数按 quantity 计。
    0 次的维度保留(「从不点辣」本身是画像),渲染层决定要不要展示。
    """
    protein, flavor, category = Counter(), Counter(), Counter()
    n_items = 0
    for o in orders:
        for it in o.get("items", []):
            # 🔴 用剥掉（）【】变体后的名称归类,「单人餐(麦金卡免配)」
            #    的括号内容不是口味信号
            name = re.sub(r"[（(【].*?[）)】]", "", it.get("name") or "").strip()
            if not name:
                continue
            q = it.get("qty") or 1
            n_items += q
            p = _first_hit(name, TASTE_PROTEIN)
            if p:
                protein[p] += q
            for label, kws in TASTE_FLAVOR:
                if any(k in name for k in kws):
                    flavor[label] += q
            c = _first_hit(name, TASTE_CATEGORY)
            if c:
                category[c] += q
    if not n_items:
        return {"basis": "无商品可聚合", "n": 0}
    return {
        "n": n_items,
        "proteinRank": protein.most_common(),
        "flavor": {k: flavor.get(k, 0) for k, _ in TASTE_FLAVOR},
        "categoryRank": category.most_common(),
        "basis": f"{n_items} 件商品名聚合(已剥变体后缀)",
    }


def build_avoid(declared_path: str) -> dict:
    """A3 拒单/规避记录:只收用户明确说过的「不吃/不要」,不收推断。

    数据源 assets/declared.json 由 flow 在用户声明偏好时落盘
    (见 flow._read_preference / flush_declared)。没有声明就没有记录,
    空列表是诚实结论,不是数据缺失。
    """
    if not os.path.exists(declared_path):
        return {"items": [], "basis": "无声明记录(declared.json 不存在)", "log": []}
    try:
        with open(declared_path, encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"items": [], "basis": "declared.json 读取失败", "log": []}
    avoids = [e for e in entries if e.get("kind") == "avoid"]
    items = []
    for e in avoids:
        it = e.get("item")
        if it and it not in items:
            items.append(it)
    return {"items": items, "count": len(avoids),
            "basis": f"用户声明 {len(avoids)} 条(仅明确说过的,不含推断)",
            "log": avoids[-10:]}


def parse_custom(item_name: str):
    """从 '吉士(不要)' / '冰(少冰)' 形态解析定制。返回 (标准化签名, 原始文本) 或 None。"""
    hits = CUSTOM_RE.findall(item_name)
    for h in hits:
        h = h.strip()
        base = item_name[: item_name.find(f"({h})")].strip()
        if "不要" in h or "不要" in base:
            for cheese, code in CHEESE_ITEMS.items():
                if cheese in base:
                    return {"type": "remove", "label": "去芝士", "code": code,
                            "key": "0-0", "raw": item_name}, h
        if "冰" in h:
            for label, key in ICE_KEY.items():
                if label in h:
                    return {"type": "ice", "label": label, "code": ICE_CODE,
                            "key": key, "raw": item_name}, h
    return None


def build_profile(max_orders: int = 10) -> dict:
    listing = call_tool("order-list", {})
    orders = (listing.get("data") or {}).get("list") or []
    # 🔴 先剔已取消/已关闭的单,再截断。order-list 硬上限 10 单且无分页,
    #    先截断会漏掉本该算进来的有效订单。
    dropped = [o for o in orders if str(o.get("orderStatus") or "") in CANCELLED]
    orders = [o for o in orders if str(o.get("orderStatus") or "") not in CANCELLED]
    orders = orders[:max_orders]

    clean_orders, customs, failed = [], Counter(), []
    for o in orders:
        oid = o.get("orderId")
        try:
            detail = call_tool("query-order", {"orderId": oid})
        except RuntimeError as e:
            failed.append({"orderId": oid, "error": str(e)[:200]})
            continue
        d = detail.get("data") or {}
        entry = {
            "orderId": oid,
            "storeCode": d.get("storeCode") or o.get("storeCode"),
            "storeName": d.get("storeName") or o.get("storeName"),
            # 🔴 orderType/beType/beCode 只在 order-list 返回，query-order 这三个字段恒为 None
            #    (实测 2026-10-10)。渠道判定必须读order-list，否则画像渠道信息全丢。
            "orderType": o.get("orderType"),
            "beType": o.get("beType"),
            "channel": CHANNEL_LABEL.get(f'{o.get("orderType")}/{o.get("beType")}', "未知"),
            "createTime": d.get("createTime") or o.get("createTime"),
            "totalAmount": d.get("totalAmount"),
            "realTotalAmount": d.get("realTotalAmount"),
            "totalDiscountAmount": d.get("totalDiscountAmount"),
            "coupons": [
                {"name": c.get("couponName"), "save": c.get("discountAmount")}
                for c in (d.get("couponList") or [])
            ],
            "items": [],
            "customs": [],
        }
        for p in d.get("orderProductList") or []:
            entry["items"].append({
                "name": p.get("productName"),
                "qty": p.get("quantity"),
            })
            for c in p.get("comboItemList") or []:
                item_name = c.get("itemName") or c.get("name") or ""
                parsed = parse_custom(item_name)
                if parsed:
                    sig, _raw = parsed
                    customs[sig["label"]] += 1
                    entry["customs"].append(sig)
        clean_orders.append(entry)

    stores = Counter(x["storeName"] for x in clean_orders)
    times = sorted(x["createTime"] for x in clean_orders if x.get("createTime"))
    span_days = None
    if len(times) >= 2:
        t0 = datetime.strptime(times[0], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(times[-1], "%Y-%m-%d %H:%M:%S")
        span_days = (t1 - t0).days

    # 置信度:同一定制 ≥3 次且跨 ≥2 单 → 高
    sig_confidence = {}
    for label, n in customs.items():
        n_orders = sum(1 for x in clean_orders if any(s["label"] == label for s in x["customs"]))
        sig_confidence[label] = {"count": n, "orders": n_orders,
                                 "level": "high" if n >= 3 and n_orders >= 2 else "low"}

    # ---------- 渠道画像(2026-10-10 新增) ----------
    # 默认渠道 = 历史订单里占比最高的渠道,不是硬编码。
    # 🔴 orderType=2(麦乐送)需要 addressId,新用户可能没有地址 →
    #    confidence=low 时 Agent 必须先确认渠道,不能直接下单。
    chan_rank = Counter(x["channel"] for x in clean_orders if x["channel"] != "未知")
    chan_top, chan_top_n = (chan_rank.most_common(1)[0] if chan_rank else (None, 0))
    chan_ratio = round(chan_top_n / max(len(clean_orders), 1), 2)
    delivery_n = sum(1 for x in clean_orders if str(x.get("orderType")) == "2")

    return {
        "version": "v2",
        "source": "observed",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "basis": f"可见 {len(clean_orders)} 单" + (f",跨度 {span_days} 天" if span_days else "")
                 + (f",跳过 {len(failed)} 单(接口异常)" if failed else ""),
        "failedOrders": failed,
        "droppedOrders": [{"orderId": (o.get("orderId") or "")[-8:],
                           "orderStatus": o.get("orderStatus"),
                           "createTime": o.get("createTime")} for o in dropped],
        "signature": {k: v for k, v in sig_confidence.items()},
        "habits": build_habits(clean_orders),
        "taste": build_taste(clean_orders),
        "avoid": build_avoid(DECLARED_PATH),
        "topStore": stores.most_common(1)[0][0] if stores else None,
        "storeRank": stores.most_common(),
        "avgAmount": round(sum(float(x["realTotalAmount"] or 0) for x in clean_orders)
                           / max(len(clean_orders), 1), 1),
        "couponsUsed": Counter(c["name"] for x in clean_orders for c in x["coupons"]).most_common(),
        # 渠道:默认走历史众数;占比 ≥0.6 才算高置信,否则需向用户确认
        "channel": {
            "default": chan_top,
            "rank": chan_rank.most_common(),
            "ratio": chan_ratio,
            "confidence": "high" if chan_ratio >= 0.6 and chan_top else "low",
            "deliveryOrders": delivery_n,
            "note": ("麦乐送(orderType=2)下单需先经delivery-query-addresses 取addressId;"
                     "若用户无地址须先 delivery-create-address") if chan_top == "麦乐送" else "",
        },
        "orders": clean_orders,
    }


def main():
    root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raw = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "assets", "profile.json")
    if ".." in raw:
        raise SystemExit(f"输出路径不得含 ..:{raw}")
    out = os.path.realpath(raw)
    if not out.startswith(root + os.sep):
        raise SystemExit(f"输出路径须在技能目录内:{raw}")
    profile = build_profile()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    Path(out).write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"画像已写入 {out}")
    print(f"口径: {profile['basis']}")
    print("签名推断:")
    for label, s in profile["signature"].items():
        print(f"  {label}: {s['count']} 次 / {s['orders']} 单 → {s['level']}")
    print(f"主店: {profile['topStore']}")
    print("券使用:", profile["couponsUsed"])
    h = profile.get("habits") or {}
    if h.get("n"):
        print(f"时段习惯: 最活跃 {h['topSlot']}({h['topSlotCount']}/{h['n']} 单),"
              f"峰值 {h['peakHour']} 点;星期 " +
              "、".join(f"{d}{n}" for d, n in (h.get("weekdayRank") or [])[:3]))
    t = profile.get("taste") or {}
    if t.get("n"):
        print(f"口味偏好: 蛋白 " + "、".join(f"{k}{v}" for k, v in t["proteinRank"])
              + ";口味 " + "、".join(f"{k}{v}" for k, v in t["flavor"].items()))
    a = profile.get("avoid") or {}
    print(f"拒单记录: {a.get('items') or '无'}({a.get('basis', '')})")


if __name__ == "__main__":
    main()
