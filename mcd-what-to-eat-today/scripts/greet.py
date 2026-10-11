#!/usr/bin/env python3
"""门店式开场:用真实订单数据算出第一句问候 + 一个猜的方向。

🔴 2026-10-10 21:27 走查返工:原来用的是一句自造招呼语,查不到出处。
   麦当劳门店的标准招呼语是「**喜欢您来♥️**」(1992 北京王府井首店启用,
   2005 推广全国;「我就喜欢i'm lovin' it」是品牌宣言,不是招呼语)。
   详见 docs/23-点单流程重设计v2.md 第二节。
   ⚠️ selftest 的品牌检查会拒绝「欢**迎**您来」这个写法再次出现。

设计原则(与门店服务对齐):
1. 品牌招呼语后面必须跟一件用户不知道服务端知道的事——否则是客套,不是服务
2. 一次只猜一个方向,被否定立刻换,不连续追问
3. 猜的依据全部来自实测字段,拿不到就不说(禁止编造间隔天数、禁止预测「你该吃了」)

依据字段:
- order-list的 createTime → 距上次天数、历史下单时段分布
- query-order 的 itemName  → 定制签名(去芝士/少冰)
- profile.json 的高频项 → 照旧内容

用法:python3 greet.py [--at "2026-10-10 20:00"] [--profile ../assets/profile.json]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "assets", "profile.json")

# 时段划分(按门店实际供应:早 06:00 起,午市 11:00 起,夜宵到 23:00)
SLOTS = [
    ("早餐", 6, 10),("午餐", 10, 15),("下午茶", 15, 17),("晚餐", 17, 21),("夜宵", 21, 24),
]


def slot_of(hour: int) -> str:
    for name, lo, hi in SLOTS:
        if lo <= hour < hi:
            return name
    return "夜宵" if hour >= 21 else "早餐"


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def hours_of(orders):
    hs = []
    for o in orders:
        t = o.get("createTime")
        if t:
            try:
                hs.append(datetime.strptime(t, "%Y-%m-%d %H:%M:%S").hour)
            except ValueError:
                continue
    return hs


def top_item(orders):
    """最高频主食项(去掉括号后缀,取出现次数最多的)。"""
    c = Counter()
    for o in orders:
        for it in o.get("items", []):
            n = it.get("name")
            if n:
                c[re.sub(r"（.*?）", "", n).strip()] += 1
    return c.most_common(1)[0] if c else (None, 0)


def signature_text(profile) -> str:
    sig = (profile.get("signature") or {})
    out = []
    if "去芝士" in sig:
        out.append("去芝士")
    if "少冰" in sig:
        out.append("少冰")
    return " + ".join(out)


def build_greeting(profile, now: datetime):
    orders = profile.get("orders") or []
    if not orders:
        return {"lines": ["喜欢您来♥️，第一次见面呀。"],
                "guess": None, "basis": "无历史订单"}

    last = datetime.strptime(orders[0]["createTime"], "%Y-%m-%d %H:%M:%S")
    days = (now - last).days
    slot = slot_of(now.hour)
    hs = hours_of(orders)
    hist_top_hour = Counter(hs).most_common(1)[0][0] if hs else None
    item, item_n = top_item(orders)
    sig = signature_text(profile)
    habits = profile.get("habits") or {}
    taste = profile.get("taste") or {}
    usual_slot = habits.get("topSlot")
    top_protein = (taste.get("proteinRank") or [None])[0]

    # ---- 第一句:问候 + 一件真实的事 ----
    lines = []
    if days <= 1:
        lines.append(f"喜欢您来♥️，昨天刚吃过啦。")
    elif days <= 7:
        lines.append(f"喜欢您来♥️，{days} 天没见您啦。")
    elif days <= 60:
        lines.append(f"喜欢您来♥️，{days} 天没见您啦。")
    else:
        lines.append("喜欢您来♥️，好久不见啦。")

    # ---- 画像层(A1/A2):先走时刻分支;没触发时用时段习惯/口味补位,保证命中 ----
    guess = None
    basis = ""
    if slot == "夜宵":
        # 21 点后:门店服务员的正确反应是「还没吃吧」
        lines.append("这个点还没吃吧？")
        guess = "night"
        basis = f"当前时段=夜宵({now.hour}点),历史最集中时段={hist_top_hour}点"
    elif slot == "早餐":
        # 早餐时段:问「今天早点还是照旧」,而不是「还没吃吧」——8 点说这话是废话
        lines.append("今天早点解决，还是照旧？")
        guess = "morning"
        basis = f"当前时段=早餐({now.hour}点),历史最集中时段={hist_top_hour}点"
    elif hist_top_hour is not None and now.hour >= hist_top_hour + 3 and days >= 5:
        lines.append(f"您常 {hist_top_hour} 点来，今天晚了")
        guess = "late"
        basis = f"历史最集中={hist_top_hour}点,当前={now.hour}点,距上次={days}天"
    elif days >= 7:
        lines.append(f"{days} 天没来，还是老样子？")
        guess = "usual"
        basis = f"距上次={days}天(7 天以上,先给照旧)"
    elif usual_slot and slot == usual_slot:
        # 没有更强的时刻信号时,「您常这个点来」就是那句服务感事实
        n = habits.get("topSlotCount") or 0
        lines.append(f"您常{usual_slot}来" + (f"，{n} 单里最多" if n else "") + "。")
        guess = "usual_slot"
        basis = f"habits.topSlot={usual_slot}({n} 单),当前时段={slot}"

    detail = ""
    if item:
        detail = f"照旧是{item}"
        if sig:
            detail += f",{sig}"
        if item_n > 1:
            detail += f"(点过 {item_n} 次)"
    if top_protein:
        p_name, p_n = top_protein
        detail += ("" if detail else "") + f";口味上常点{p_name}类({p_n} 份)"
    return {"lines": lines, "guess": guess, "basis": basis,
            "detail": detail, "days": days, "slot": slot,
            "hist_top_hour": hist_top_hour, "last": last,
            "usual_slot": usual_slot, "top_protein": top_protein}


def personal_hits(g, profile) -> list:
    """迎宾命中的个性化维度。门禁判据:有历史订单时,至少命中一个。

    三个维度(PLAN 阶段 A A4):
    - 常点什么:detail 里有照旧项或口味偏好
    - 什么时候来:话术里出现历史时段(常X来 / X点前后)
    - 上次要什么:话术里有间隔陈述(N 天没见)
    """
    orders = profile.get("orders") or []
    if not orders:
        return []          # 第一次见面是诚实路径,不算命中也不算违规
    hits = []
    visible = "。".join(g.get("lines") or [])
    if g.get("detail"):
        hits.append("常点什么")
    if re.search(r"常[早午晚夜下午]+来|点前后|这个点正是时候", visible):
        hits.append("什么时候来")
    if re.search(r"天没见|天没来|刚吃过|好久不见", visible):
        hits.append("上次要什么")
    return hits


def gate(g, profile) -> list:
    """迎宾质量门禁。返回违规列表,空 = 放行。

    有历史订单却一个维度都不命中 = 通用客套话,违反
    「品牌招呼语后面必须跟一件用户不知道服务端知道的事」。
    """
    if not (profile.get("orders") or []):
        return []
    if personal_hits(g, profile):
        return []
    return ["有历史订单,但问候未命中 常点什么/什么时候来/上次要任何一维度"]


def render(g, profile):
    L = []
    import render_mobile as rm
    L.append("【给用户看的】")
    L.append(rm.turn(g["lines"][0], facts=g["lines"][1:]))
    L.append("")
    L.append("【内部依据,不进对话】")
    L.append(f"  {g['basis']}")
    if g.get("detail"):
        L.append(f"  照旧:{g['detail']}")
    if g.get("days") is not None:
        L.append(f"  间隔:{g['days']} 天(order-list 最后一单,只陈述不预测)")
    L.append("  下一步:交 flow.Flow 走 S1 渠道必问 → …")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default=None, help="yyyy-MM-dd HH:mm,默认当前时间")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    args = ap.parse_args()
    now = datetime.strptime(args.at, "%Y-%m-%d %H:%M") if args.at else datetime.now()
    profile = load(args.profile)
    g = build_greeting(profile, now)
    print(render(g, profile))
    hits = personal_hits(g, profile)
    bad = gate(g, profile)
    print()
    print("【迎宾门禁】" + ("、".join(hits) if hits else "未命中任何维度") +
          (" → 放行" if not bad else " → ✗ " + bad[0]))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()