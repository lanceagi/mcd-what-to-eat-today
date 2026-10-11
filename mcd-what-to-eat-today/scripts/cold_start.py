#!/usr/bin/env python3
"""冷启动画像:为「没有历史订单」的新用户在零提问或少提问下建立可用画像。

## 问题背景
五层设计里有四层建立在「有历史订单」上。新用户第一次使用:
  order-list 返回空 → fetch_profile 空 → greet 只剩冷启动分支
  → recommend 无画像候选 → order_builder 无签名可挂
新用户若要走完与老用户同等的能力,必须**在没有历史的情况下拿到画像**。

## 设计原则
1. **先榨干零历史信号,再问用户**。能不问就不问。
2. **每个问题都必须有真实决策后果**,不做问卷式收集。
3. **第一单本身就是数据采集**——下单后 order-list 有了,历史循环启动。
4. **不知道就说不知道**,不使用硬编码口味默认值冒充推断结果。

## 零历史可用的信号(2026-10-10 20:22 实测)
| 信号 | 来源 | 含义 |
|---|---|---|
| `query-my-account` | availablePoint > 0 | 有积分=注册过麦当劳账号;含麦金卡则 type=3 |
| `query-meals.frequent` | tags 含「我的常点」 | **官方自己算的常点**,有则直接复用,连 Q1 都能省 |
| `query-meals.categories` | 16 个分类 | 含「人气热卖」,零历史推荐的兜底起点 |
| `query-my-coupons` | 卡包券 | 券名反映口味倾向(如「9.9元中杯冰美式」= 喝美式) |

## 用法
```bash
python3 cold_start.py# 只探测信号,输出该走哪条路径
python3 cold_start.py --answers '{"main":"牛肉","cheese":"去芝士","ice":"少冰"}'
python3 cold_start.py --answers '...' --write   # 写入 profile.json(标记 source=cold_start)
```

⚠️ `--write` 生成的画像 source=cold_start,level 全为 declared(非 observed)。
后续 fetch_profile 拿到真实订单后,**必须覆盖升级为 observed**——
否则一次冷启动的声明会永久伪装成"推断出的高置信偏好"。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcd_client import call_tool, call_tool_text, parse_coupons  # noqa: E402

DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "assets", "profile.json")

# 三问的合法取值(与实测定制取值表一一对应,保证能直接落成 create-order 参数)
VALID = {
    "main": ["牛肉", "鸡", "鱼", "都行"],
    "cheese": ["去芝士", "保留芝士", "都行"],
    "ice": ["标准冰", "少冰", "多冰", "去冰", "都行"],
}

QUESTIONS = {
    "main": "常点哪个汉堡类?(牛肉/鸡/鱼/都行)",
    "cheese": "汉堡要不要去芝士?(去芝士/保留芝士/都行)",
    "ice": "饮料冰量?(标准冰/少冰/多冰/去冰/都行)",
}


def probe_signals(store_code="1450523", reservation_date=None):
    """探测零历史可用信号。任一接口失败不中断,只记录。"""
    sig = {"has_account": None, "has_points": None, "point_balance": None,
           "frequent_code": None, "frequent_name": None, "categories": [],
           "coupon_hints": [], "errors": []}

    try:
        acc = call_tool("query-my-account", {}).get("data") or {}
        pts = acc.get("availablePoint")
        sig["point_balance"] = float(pts) if pts not in (None, "") else None
        sig["has_points"] = (sig["point_balance"] or 0) > 0
        sig["has_account"] = True
    except (RuntimeError, TypeError, ValueError) as e:
        sig["errors"].append(f"query-my-account: {str(e)[:120]}")

    args = {"storeCode": store_code, "orderType": 1, "beType": 1}
    if reservation_date:
        args["reservationDate"] = reservation_date
    try:
        meals = (call_tool("query-meals", args).get("data") or {})
        freq = meals.get("frequent") or {}
        code = freq.get("code")
        if code and code in (meals.get("meals") or {}):
            sig["frequent_code"] = code
            sig["frequent_name"] = meals["meals"][code]["name"]
        sig["categories"] = [c.get("name") for c in (meals.get("categories") or [])
                            if c.get("name")]
        sig["menu_size"] = len(meals.get("meals") or {})
    except (RuntimeError, TypeError, ValueError) as e:
        sig["errors"].append(f"query-meals: {str(e)[:120]}")

    try:
        for c in parse_coupons(call_tool_text("query-my-coupons", {})):
            nm = c.get("name", "")
            if nm and nm not in sig["coupon_hints"]:
                sig["coupon_hints"].append(nm)
    except RuntimeError as e:
        sig["errors"].append(f"query-my-coupons: {str(e)[:120]}")

    return sig


def route_of(sig) -> str:
    """决定走哪条路径。返回 path 标识与需要问的问题列表。"""
    if sig.get("frequent_code") and sig.get("has_points"):
        return "auto_zero_question"          # 官方常点 + 有积分
    if sig.get("frequent_code"):
        return "reuse_frequent_ask2"         # 复用官方常点,只问签名
    if sig.get("has_points"):
        return "ask_main_only"               # 有账号无订单,只问主食偏好
    return "ask_all_three"                   # 完全空,三问


def build_cold_profile(sig, answers) -> dict:
    """把三问答案转成 profile.json 结构。

    ⚠️ 全部标记 level=declared,source=cold_start —— 与observed 严格区分。
    """
    signature, notes = {}, []
    sig_raw = (sig.get("frequent_name") or "") + " " + " ".join(sig.get("coupon_hints") or [])

    if answers.get("cheese") == "去芝士":
        signature["去芝士"] = {"count": 1, "orders": 1, "level": "declared",
                           "source": "cold_start_question"}
    if answers.get("ice") in ("少冰", "多冰", "去冰", "标准冰"):
        signature["少冰" if answers["ice"] == "少冰" else "冰量偏好"] = {
            "count": 1, "orders": 1, "level": "declared", "source": "cold_start_question",
            "value": answers["ice"]}

    if sig.get("frequent_name"):
        notes.append(f"复用官方「我的常点」:{sig['frequent_name']}(tags含「我的常点」)")
    else:
        notes.append("官方常点为空,主食来自用户声明")

    # 蛋白来源分布:冷启动只有 1 条记录,不能声称统计
    main_pref = answers.get("main", "都行")
    main_item = sig.get("frequent_name") or f"{main_pref}类汉堡(待确认)"

    return {
        "version": "v1",
        "source": "cold_start",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "basis": "冷启动:无历史订单,数据来自用户声明 + 零历史接口信号",
        "warning": "本画像 level 全为 declared,不代表统计推断。拿到真实订单后必须重新运行 "
                   "fetch_profile.py 覆盖升级为 observed。",
        "signature": signature,
        "answers": answers,
        "topStore": None,
        "storeRank": [],
        "avgAmount": None,
        "couponsUsed": [],
        "signals": {k: sig.get(k) for k in
                    ("has_account", "has_points", "point_balance",
                     "frequent_code", "frequent_name", "menu_size", "coupon_hints")},
        "mainPreference": {"declared": main_pref, "item": main_item},
        "coldNotes": notes,
        "orders": [],
    }


def render_probe(sig, path):
    L = []
    L.append("=" * 64)
    L.append("冷启动信号探测(未提问)")
    L.append("=" * 64)
    L.append(f"  麦当劳账号:{'有' if sig.get('has_account') else '未探测到'}"
             f" | 积分余额:{sig.get('point_balance')}")
    L.append(f"  官方「我的常点」:{sig.get('frequent_name') or '【空】'}"
             f"({sig.get('frequent_code') or '无 code'})")
    L.append(f"  菜单商品数:{sig.get('menu_size')} | 分类数:{len(sig.get('categories') or [])}")
    hints = sig.get("coupon_hints") or []
    L.append(f"  卡包券:{len(hints)} 张{hints[:3] if hints else ''}")
    for e in sig.get("errors") or []:
        L.append(f"  ⚠️ {e}")
    L.append("")
    L.append(f"【判定路径】{path}")
    route_map = {
        "auto_zero_question": "官方常点 + 有积分 → 零提问,直接复用官方数据",
        "reuse_frequent_ask2": "有官方常点 → 复用它,只问 Q2/Q3(去芝士 / 冰量)",
        "ask_main_only": "有账号无订单 → 只问 Q1 主食偏好",
        "ask_all_three": "完全空 → 问三问 + 人气热卖兜底",
    }
    L.append(f"  {route_map.get(path, path)}")
    L.append("")
    if path != "auto_zero_question":
        L.append("【需要问的问题】(每问都有真实决策后果)")
        need = {"reuse_frequent_ask2": ["cheese", "ice"],
                "ask_main_only": ["main", "cheese", "ice"],
                "ask_all_three": ["main", "cheese", "ice"]}.get(path, [])
        for k in need:
            L.append(f"  {k}: {QUESTIONS[k]}")
    else:
        L.append("  无需提问。")
    L.append("")
    L.append("【第一条话术】")
    if sig.get("frequent_name"):
        L.append(f"  「欢迎您来,记得您常点{sig['frequent_name']},今天还是它吗?」")
    else:
        L.append("  「欢迎您来,第一次见面。先问您三件事,以后就不用再说了。」")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="冷启动画像生成(不调 create-order)")
    ap.add_argument("--answers", default=None,
                    help="JSON 字符串,如'{\"main\":\"牛肉\",\"cheese\":\"去芝士\",\"ice\":\"少冰\"}'")
    ap.add_argument("--write", action="store_true", help="写入 profile.json")
    ap.add_argument("--date", default="2026-10-11 12:00")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    args = ap.parse_args()

    sig = probe_signals(reservation_date=args.date)
    path = route_of(sig)

    if not args.answers:
        print(render_probe(sig, path))
        return

    try:
        answers = json.loads(args.answers)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--answers 不是合法 JSON:{e}")
    bad = [f"{k}={v}" for k, v in answers.items()
           if k in VALID and v not in VALID[k]]
    if bad:
        raise SystemExit(f"非法取值:{bad}。合法值:{VALID}")

    profile = build_cold_profile(sig, answers)
    if args.write:
        with open(args.profile, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        print(f"冷启动画像已写入 {args.profile}")
    print(json.dumps(profile, ensure_ascii=False, indent=2)[:900])


if __name__ == "__main__":
    main()