#!/usr/bin/env python3
"""场景推荐器:老样子 + 一条带理由的偏离。

数据来源(全部实测,无估算):
- 菜单 query-meals:菜单层自带 originalPrice / currentPrice / discountType
  → **券后价已在菜单层体现**,无需再算
- 券 query-my-coupons(markdown):优惠额 + 有效期 + 标签(到店/外送)
- 营养 list-nutrition-foods:160 款 kcal / 蛋白 / 脂肪 / 钠
- 画像 assets/profile.json:去芝士签名、老样子高频项

**已实现场景**(5 个,choices 与实现一一对应,selftest 逐项校验):
- saving  省钱:卡券四路筛有效期 → 排除酱料/配件/多人份 → 有折扣按省额降序
- fitness 健身:卡 kcal≤900 且钠≤700mg(实测阈值)→ 按蛋白密度降序
- hurry   赶时间:点过的优先 → 无限时标记优先 → 单品优先 → 价格升序
- random  随缘:冠军组合(历史整单复现最多的一套)+ 一条吃过≥2次的替代
- try_new 尝新:没点过的主食 + 六维解释(是什么/像什么/热量差/钠差/价差/下架风险)

**场景开关诚实**:choices 只列已实现的;random 无历史时显式说「没有依据」,
不做「传 random 却拿到 saving 结果」的静默降级。

用法:python3 recommend.py --scene saving|fitness
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcd_client import call_tool, call_tool_text, parse_coupons  # noqa: E402
from channel import (CHANNELS, channel_params, list_addresses,  # noqa: E402
                     delivery_stores, resolve_channel)

STORE_CODE = "1450523"          # 默认门店:静安沪太路(画像 52%)
DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "assets", "profile.json")

# 健身场景硬阈值(实测 157 条营养数据筛出的健身友好项)
FIT_MAX_KCAL = 900
FIT_MAX_NA = 700
FIT_MIN_PROTEIN = 15


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_menu(reservation_date: str, store_code=None, channel_params=None) -> dict:
    """按渠道拉菜单。

    🔴 2026-10-10 修正:原先硬编码 orderType=1/beType=1,是「发现 27」的受害者——
       同一门店两渠道菜单重叠仅 26/122(实测:外送 122/ 到店 107),
       老用户走外送时会推荐出「菜单里有但外送点不了」的品。
    """
    args = {"storeCode": store_code or STORE_CODE, "reservationDate": reservation_date}
    args.update(channel_params or {"orderType": 1, "beType": 1})
    j = call_tool("query-meals", args)
    return (j.get("data") or {}).get("meals") or {}


def resolve_anchor(profile, channel_hint=None, city="上海市"):
    """确定本次推荐用的 (storeCode, 渠道参数, 渠道名, 依据)。

    外送场景门店由地址决定,不能沿用历史主店。
    🔴 2026-10-11 修(test-4 待修正项1):原来盲取 stores[0](列表序),
       实测锚到盛源生活广场 1450279,而对话里 flow 按历史主店选了
       沪太路 341 号 1450523 —— 同一会话两个门店,推荐价口径漂移。
       → 历史主店在可送列表里就优先它(与 flow.pick_store 同口径),
         取不到才回退 stores[0] 并在依据里注明。
    """
    name, why = resolve_channel(profile, channel_hint)
    spec = CHANNELS[name]
    if spec["needAddress"]:
        addrs = list_addresses()
        if not addrs:
            return None, None, None, f"{name} 但用户无配送地址,请先创建地址或改渠道"
        aid = next((a["addressId"] for a in addrs if a["city"] == city), addrs[0]["addressId"])
        stores = delivery_stores(aid, spec["beType"])
        if not stores:
            return None, None, None, f"{name} 但该地址无可送门店,请换地址或改渠道"
        # 剥「麦当劳」前缀再取 6 字:「上海静安沪太」能区分同区多店
        # (盛源广场是「麦当劳上海静安盛源…」,不含「沪太」,不会误命中)
        top = (profile.get("topStore") or "").replace("麦当劳", "")
        key = top[:6]
        pick = next((s for s in stores if key and key in (s.get("storeName") or "")), None)
        tag = "历史主店"
        if pick is None:
            pick, tag = stores[0], "列表首店(历史主店不在可送列表)"
        return pick["storeCode"], channel_params(name, be_code=pick.get("beCode"),
                                                 address_id=aid), name, f"{why} / 地址 {aid[:10]}… → {pick['storeName']}({tag})"
    return STORE_CODE, channel_params(name), name, why


def fetch_coupons() -> list[dict]:
    """已入卡包的券(auto-bind-coupons 属写操作,由调用方决定是否先领)。"""
    return parse_coupons(call_tool_text("query-my-coupons", {}))


def fetch_nutrition() -> dict:
    """返回 {商品名: {kcal, protein, fat, na}}。解析 list-nutrition-foods 的文本表格。"""
    j = call_tool("list-nutrition-foods", {})
    raw = j.get("data") or ""
    rows = {}
    for line in raw.splitlines()[1:]:
        m = re.match(r"\s*(.+?),null,(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\s*$", line)
        if m:
            rows[m.group(1).strip()] = {
                "kcal": int(m.group(3)), "protein": int(m.group(4)),
                "fat": int(m.group(5)), "na": int(m.group(7)),
            }
    return rows


def load_profile(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 营养表与菜单命名不一致的实测差异(20:02):
#   菜单「原味双层深海鳕鱼堡」 vs 营养表「双层深海鳕鱼堡」
#   菜单「风味双层深海鳕鱼堡」 同理
# 处理:逐级剥离「原味/风味」等口味前缀后再匹配。查不到返回 None,不估算。
FLAVOR_PREFIX = ("原味", "风味", "经典", "特选")


def _variants(name: str):
    stripped = re.sub(r"\(.*?\)", "", name).strip()
    yield stripped
    for p in FLAVOR_PREFIX:
        if stripped.startswith(p) and len(stripped) > len(p):
            yield stripped[len(p):].strip()


def nut_of(nutrition: dict, name: str):
    """按名称精确 → 去括号/剥离口味前缀 → 包含 三级匹配。取不到返回 None(不估算)。"""
    if not name or not nutrition:
        return None
    for v in _variants(name):
        if v in nutrition:
            return nutrition[v]
    for v in _variants(name):
        for k, val in nutrition.items():
            if k and (k in v or v in k):
                return val
    return None


# 单品加购门槛:低于此价的是酱料/配料/圆筒,不构成一餐(实测 107 项中 0-9 元共 16 项全是此类)
MIN_ITEM_PRICE = 15.0

# 排除的品类关键词(酱料/配料/儿童玩具等),实测这些项 currentPrice 低但不可组餐
EXCLUDE_KEYWORDS = ("酱", "蘸", "配料", "玩具", "积木", "盲盒")

# 多人份关键词:价格高、省额大,会霸榜省额排序,但单人用户买不了
# 实测坑(21:25):原表只有「双人/多人/分享/四宫格/聚餐」,漏了「三人/四人」,
# 导致省钱场景首推「加牛欢享四人餐¥116 省¥89」「吃鸡盛宴四人餐」——单人根本吃不完。
MULTI_KEYWORDS = ("双人", "两人", "多人", "分享", "四宫格", "聚餐", "三人", "四人", "翅桶", "全家桶", "桶")


def combo_nutrition(combo_name: str, detail: dict, nutrition: dict):
    """套餐热量 = 子项叠加。MCP 不直接给套餐营养(实测 query-meal-detail 无 nutrition 字段)。

    做法:取 query-meal-detail 的 rounds → 每轮取默认/首个 choices → 查营养表累加。
    **完整性校验**:任一轮查不到该子项营养则整体返回 None 并给出缺失项,
    因为少算一个汉堡会让套餐热量严重偏低(实测:漏算汉堡本体时921,
    补上应为 1406)。宁可报「数据不全」也不给错数字。
    """
    total = {"kcal": 0, "protein": 0, "fat": 0, "na": 0}
    missing = []
    rounds = detail.get("rounds") or []
    if not rounds:
        return None, ["无 rounds 数据"]
    for rnd in rounds:
        choices = rnd.get("choices") or []
        pick = next((c for c in choices if c.get("isDefault")), None) or (choices[0] if choices else None)
        if not pick:
            continue
        n = nut_of(nutrition, pick.get("name"))
        if n:
            for k in total:
                total[k] += n[k]
        else:
            missing.append(pick.get("name"))
    if missing:
        return None, missing
    return total, []


def rank_saving(meals: dict, nutrition: dict, exclude_multi: bool = True):
    """省钱场景。

    实测教训(19:55-20:00,两轮修正):
    1. 纯按 currentPrice 升序会把酱料、配料包推到前排(0-9 元 16 项全是此类)→ 加 MIN_ITEM_PRICE 与 EXCLUDE_KEYWORDS
    2. 只按省额排序会被双人/分享套餐霸榜(省 ¥59-65 但单人吃不完)→ 加 MULTI_KEYWORDS 过滤
    3. currentPrice 与 originalPrice 相同 = 无折扣,如实标注,不假装有优惠
    排序:有折扣的按省额降序;同级比券后价;无折扣的按券后价垫后
    """
    out = []
    for code, m in meals.items():
        cur, orig = _f(m.get("currentPrice")), _f(m.get("originalPrice"))
        if cur is None or cur < MIN_ITEM_PRICE:
            continue
        if any(k in m["name"] for k in EXCLUDE_KEYWORDS):
            continue
        if exclude_multi and any(k in m["name"] for k in MULTI_KEYWORDS):
            continue
        save = round(orig - cur, 2) if orig and orig > 0 else None
        out.append({
            "code": code, "name": m["name"],
            "final": cur, "original": orig,
            "save": save,
            "discount": (save / orig) if (save and orig) else None,
            "on_sale": bool(save and save > 0),
            "nutrition": nut_of(nutrition, m["name"]),
            "canWithOrder": m.get("canWithOrder"),
        })
    out.sort(key=lambda x: (not x["on_sale"], -(x["save"] or 0), x["final"]))
    return out


_COMBO_KW = ("套餐", "单人餐", "三件套", "四件套", "五件套", "双人餐", "组合", "桶")


def rank_fitness(meals: dict, nutrition: dict, api=None, limit_detail=12):
    """健身场景:先卡热量与钠,再比价格。

    🔴 2026-10-10 23:26 修正(外部 agent 两轮实测抓出):
       原实现只用 `nut_of(nutrition, m["name"])`,而营养表里**没有套餐**,
       → 所有套餐被 `if not n: continue` 全部排除,只剩裸堡。
       于是报出「麦香鱼 325kcal / 钠 556mg」——那是**一个堡**的数字,
       用户以为是整份套餐的热量,实际整份是 761kcal / 钠 721mg。
       用户当场问「怎么比你之前报的高这么多」。

       修法:
       1. 套餐走 `combo_nutrition()` 叠���子项,口径标 `整份`
       2. 单品保留,口径标 `单品`
       3. **每一行都必须带 `scope` 字段**,渲染时必须写出来
          —— 不标明口径的数字没有意义(实测教训)
    """
    out = []
    combos = [(c, m) for c, m in meals.items()
              if any(k in (m.get("name") or "") for k in _COMBO_KW)]
    for code, m in meals.items():
        cur = _f(m.get("currentPrice"))
        if cur is None:
            continue
        # 🔴 套餐名必须走叠加口径。`nut_of` 会把「麦香鱼套餐」模糊匹配到
        #    「麦香鱼」的 325kcal —— 那是**一个堡**的数字,报出去会被当成整份。
        if any(k in (m.get("name") or "") for k in _COMBO_KW):
            continue
        n = nut_of(nutrition, m["name"])
        if n:                                   # 单品:直接命中营养表
            out.append({"code": code, "name": m["name"], "final": cur,
                        "nutrition": n, "scope": "单品", "discount": None})
    # 套餐:要额外查 detail 叠子项,按价格升序限量,避免请求爆炸
    if api is not None:
        combos.sort(key=lambda x: _f(x[1].get("currentPrice")) or 0)
        for code, m in combos[:limit_detail]:
            cur = _f(m.get("currentPrice"))
            if cur is None:
                continue
            d = api.detail(code)
            got = combo_nutrition(m["name"], d, nutrition) if d else None
            if not d:
                out.append({"code": code, "name": m["name"], "final": cur,
                            "nutrition": None, "scope": "整份",
                            "nutrition_missing": ["详情查询失败"], "discount": None})
                continue
            if not got or got[0] is None:
                out.append({"code": code, "name": m["name"], "final": cur,
                            "nutrition": None, "scope": "整份",
                            "nutrition_missing": got[1] if got else ["无数据"],
                            "discount": None})
                continue
            out.append({"code": code, "name": m["name"], "final": cur,
                        "nutrition": got[0], "scope": "整份", "discount": None})
    out = [x for x in out
           if x["nutrition"] and x["nutrition"]["kcal"] <= FIT_MAX_KCAL
           and x["nutrition"]["na"] <= FIT_MAX_NA]
    # 🔴 2026-10-10 23:38 修正:原排序只看蛋白/价格,前几名全是小食
    #   (吉士汉堡包 ¥16.5 / 汉堡包 ¥15 / 麦乐鸡5块)—— 健身场景要的是**一份主食**,
    #   不是垫肚子的零嘴。→ 主食/套餐优先,再比蛋白,再比价。
    def _is_main(nm):
        return any(k in (nm or "") for k in
                   ("堡", "套餐", "单人餐", "件套", "中套", "组合"))
    out.sort(key=lambda x: (0 if _is_main(x["name"]) else 1,
                            -x["nutrition"]["protein"], x["final"]))
    return out


def coupons_in_effect(coupons: list[dict], when: datetime):
    """筛出当前时段可用的券。返回 (券列表, 跳过列表)——跳过的必须可见,不静默吞掉。"""
    live, skipped = [], []
    for c in coupons:
        v = c.get("有效期", "")
        m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})-(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", v)
        if not m:
            skipped.append((c.get("name"), "有效期无法解析:" + v))
            continue
        start = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{m.group(3)} {m.group(4)}", "%Y-%m-%d %H:%M")
        if start <= when <= end:
            live.append(c)
        else:
            skipped.append((c.get("name"), f"不在有效期({start}~{end})"))
    return live, skipped


# ---------- C 组场景(2026-10-11 阶段 A C1/C2/C3) ----------
# 波动词:名称带这些词的商品供应不稳定,赶时间场景要避开,尝新场景反而优先。
VOLATILE_KEYWORDS = ("新品", "限定", "季节", "活动", "回归", "限时", "尝鲜")
# 常驻经典:实测长期在售的品类词,下架风险低的启发式信号。
STAPLE_KEYWORDS = ("巨无霸", "麦香鱼", "板烧", "麦辣", "安格斯", "薯条", "可乐",
                   "麦乐鸡", "双层", "猪柳蛋", "麦满分", "圆筒", "派")


def _norm_item(name: str) -> str:
    """剥掉（）/【】变体描述后的商品名 —— 与 flow._norm_name 同口径。"""
    return re.sub(r"[（(【].*?[）)】]", "", name or "").strip()


def seen_names_of(profile: dict) -> set:
    """历史点过的商品名集合(剥变体)。尝新/赶时间用它分「点过/没点过」。"""
    seen = set()
    for o in profile.get("orders") or []:
        for it in o.get("items", []):
            n = _norm_item(it.get("name") or "")
            if n:
                seen.add(n)
    return seen


def rank_hurry(meals: dict, nutrition: dict, seen: set, limit: int = 6):
    """C1 赶时间:低风险、常备、配料稳定。

    排序依据(全部可复核):
    1. 点过的优先 —— 点过 N 次的东西不用看菜单,出错率最低(事实)
    2. 无波动词优先 —— 名称带新品/限定/季节的供应不稳(名称启发式,如实标注)
    3. 结构简单优先 —— 单品 < 套餐,定制点少、后厨出错面小
    4. 价格升序
    """
    out = []
    for code, m in meals.items():
        cur = _f(m.get("currentPrice"))
        name = m.get("name") or ""
        if cur is None or cur < MIN_ITEM_PRICE:
            continue
        if any(k in name for k in EXCLUDE_KEYWORDS):
            continue
        if any(k in name for k in MULTI_KEYWORDS):
            continue
        n_seen = sum(1 for s in seen if s in name or name in s)
        volatile = any(k in name for k in VOLATILE_KEYWORDS)
        is_combo = any(k in name for k in _COMBO_KW)
        if n_seen:
            reason = f"赶时间:点过{('' if n_seen == 1 else '≥')}{n_seen}次,不用想"
        elif volatile:
            reason = "带限时标记,赶时间不推"      # 只作垫底,排序上压制
        elif is_combo:
            reason = "常驻套餐,定制点比单品多"
        else:
            reason = "常驻单品,无新品/限定标记"
        out.append({
            "code": code, "name": name, "final": cur,
            "original": _f(m.get("originalPrice")),
            "save": None, "on_sale": False,
            "nutrition": nut_of(nutrition, name), "scope": "单品",
            "seen": n_seen, "volatile": volatile, "combo": is_combo,
            "reason": reason,
        })
    out.sort(key=lambda x: (-x["seen"], x["volatile"], x["combo"], x["final"]))
    return out[:limit]


def rank_random(meals: dict, nutrition: dict, orders: list):
    """C2 随缘(不叫 random 的「随机」——随机与「比店员更懂你」冲突,随缘仍是听你的)。

    冠军组合 = 历史整单里出现最多的同一套商品(按剥变体后的品名集合分组);
    替代 = 吃过 ≥2 次且不在冠军组合里的另一项。
    两行都带 热量/钠/券后价 三标记。无历史时不硬编 —— 返回空,由调用方如实说。
    """
    if not orders:
        return []
    groups = {}
    for o in orders:
        names = tuple(sorted({_norm_item(it.get("name") or "")
                              for it in o.get("items", []) if it.get("name")}))
        if names:
            groups.setdefault(names, []).append(o.get("createTime"))
    if not groups:
        return []
    champ_names, champ_times = max(groups.items(), key=lambda kv: len(kv[1]))

    def _menu_entry(nm):
        """在当轮菜单里找这个历史品名(包含匹配),拿券后价。"""
        for code, m in meals.items():
            mn = _norm_item(m.get("name") or "")
            if mn and (mn in nm or nm in mn):
                cur = _f(m.get("currentPrice"))
                if cur is not None:
                    return code, cur, _f(m.get("originalPrice"))
        return None, None, None

    # 冠军组合整行(多品相加;单品就是一项)
    entries = []
    total, kcal, na, miss, codes = 0, 0, 0, [], []
    for nm in champ_names:
        code, cur, orig = _menu_entry(nm)
        if cur is None:
            miss.append(nm)
            continue
        total += cur
        codes.append(code)
        n = nut_of(nutrition, nm)
        if n:
            kcal += n["kcal"]
            na += n["na"]
        else:
            miss.append(nm + "(营养)")
    entries.append({
        "name": " + ".join(champ_names), "code": ",".join(codes),
        "final": round(total, 2), "original": None, "save": None,
        "nutrition": (None if miss else {"kcal": kcal, "protein": 0, "fat": 0, "na": na}),
        "nutrition_missing": miss or None, "scope": "整单",
        "reason": f"冠军组合:这一套点过 {len(champ_times)} 次(整单复现)",
    })
    # 替代:吃过 ≥2 次且不在冠军组合里
    freq = {}
    for o in orders:
        for it in o.get("items", []):
            nm = _norm_item(it.get("name") or "")
            if nm and nm not in champ_names:
                freq[nm] = freq.get(nm, 0) + (it.get("qty") or 1)
    for nm, n in sorted(freq.items(), key=lambda kv: -kv[1]):
        if n < 2:
            continue
        code, cur, orig = _menu_entry(nm)
        if cur is None:
            continue
        nn = nut_of(nutrition, nm)
        entries.append({
            "name": nm, "code": code, "final": cur, "original": orig,
            "save": (round(orig - cur, 2) if orig and orig > cur else None),
            "nutrition": nn, "scope": "单品",
            "reason": f"替代:吃过 {n} 次,想换就换这个",
        })
        break
    return entries


def freq_of(profile: dict) -> dict:
    """历史品名 → 次数(剥变体)。尝新的「像什么」兜底用:点得最多的最有类比资格。"""
    freq = {}
    for o in profile.get("orders") or []:
        for it in o.get("items", []):
            nm = _norm_item(it.get("name") or "")
            if nm:
                freq[nm] = freq.get(nm, 0) + (it.get("qty") or 1)
    return freq


def rank_try_new(meals: dict, nutrition: dict, seen: set, limit: int = 3,
                 freq: dict = None):
    """C3 尝新:没点过的品,六维解释(是什么/像什么/热量差/钠差/价差/下架风险)。

    「像什么」用 A2 的蛋白分类表对桶(fetch_profile.TASTE_PROTEIN):
    同桶里取名称字符重叠最大者;重叠为 0 时(如「汉堡包」vs「安格斯单人餐」
    无公共字)退到同桶点得最多的一项 —— 「同为牛肉堡」仍是诚实的类比。
    下架风险是**名称启发式**,输出里如实标注依据,不冒充实测。
    """
    from fetch_profile import TASTE_PROTEIN, _first_hit

    def _bucket(nm):
        return _first_hit(nm, TASTE_PROTEIN)

    def _similar(nm):
        """与 nm 最像的已点项:先同蛋白桶,再按字符重叠打分。"""
        b = _bucket(nm)
        pool = [s for s in seen if (not b or _bucket(s) == b)] or list(seen)
        best, score = None, 0
        for s in pool:
            ov = len(set(nm) & set(s))
            if ov > score:
                best, score = s, ov
        if best is None and pool:
            best = max(pool, key=lambda s: (freq or {}).get(s, 0))
        return best

    # 已点项在当轮菜单的参考价(价差的基准)
    ref_price = {}
    for s in seen:
        for m in meals.values():
            mn = _norm_item(m.get("name") or "")
            if mn and (mn in s or s in mn):
                cur = _f(m.get("currentPrice"))
                if cur is not None:
                    ref_price[s] = cur
                    break

    out = []
    for code, m in meals.items():
        cur = _f(m.get("currentPrice"))
        name = m.get("name") or ""
        nm = _norm_item(name)
        if cur is None or cur < MIN_ITEM_PRICE:
            continue
        if any(k in name for k in EXCLUDE_KEYWORDS + MULTI_KEYWORDS):
            continue
        if not any(k in nm for k in ("堡", "餐", "套", "卷")):
            continue                      # 尝新也只看主食,不推小食
        if any((s in nm or nm in s) for s in seen):
            continue                      # 点过的不是尝新
        like = _similar(nm)
        n_c, n_l = nut_of(nutrition, nm), (nut_of(nutrition, like) if like else None)
        kcal_d = (n_c["kcal"] - n_l["kcal"]) if (n_c and n_l) else None
        na_d = (n_c["na"] - n_l["na"]) if (n_c and n_l) else None
        price_d = (cur - ref_price[like]) if (like and like in ref_price) else None
        volatile = any(k in name for k in VOLATILE_KEYWORDS)
        staple = any(k in name for k in STAPLE_KEYWORDS)
        risk = ("高:名称带限时/新品标记" if volatile
                else "低:常驻经典品类" if staple
                else "中:无标记,仅凭名称判断")
        out.append({
            "code": code, "name": name, "final": cur,
            "original": _f(m.get("originalPrice")),
            "save": None, "nutrition": n_c, "scope": "单品",
            "seen": False, "volatile": volatile,
            "reason": "尝新:菜单里你从没点过的" + ("(限时标记)" if volatile else ""),
            "explain": {
                "是什么": f"{nm}({_bucket(nm) or '主食'})",
                "像什么": f"最像你点过的「{like}」" if like else "没有可类比的历史项",
                "热量差": (f"{kcal_d:+d}kcal(vs{like})" if kcal_d is not None else "数据不全"),
                "钠差": (f"{na_d:+d}mg(vs{like})" if na_d is not None else "数据不全"),
                "价差": (f"{price_d:+.1f}元(vs{like}菜单价)" if price_d is not None
                        else "无历史参考价"),
                "下架风险": risk,
            },
        })
    # 限时/新品优先(尝新的本意),再比价格
    out.sort(key=lambda x: (not x["volatile"], x["final"]))
    return out[:limit]


def render_scene(menu_ranked, scene: str, profile: dict, coupons_live, skipped,
                 limit=6, is_delivery=False):
    """决策层输出:老样子(照画像)+ 一条带理由的偏离(照场景),数字全部来自实测。"""
    sig = list((profile.get("signature") or {}).keys())
    sig_txt = " + ".join(sig) if sig else "无历史定制记录"
    L = []
    L.append(f"【签名】{sig_txt}（自动带上，无需再说）")
    L.append("")
    L.append("【照你最近的】")
    # 🔴 外送场景:历史门店 ≠ 本次下单门店(门店由地址决定)。
    #    照旧只保证「餐品与定制一致」,门店/配送以本单地址为准 —— 不能说成"照旧门店送"。
    if profile.get("orders"):
        last = profile["orders"][0]
        names = [i.get("name") for i in last.get("items", []) if i.get("name")]
        if names:
            L.append(f"  {last.get('createTime','')}｜{last.get('storeName','')}"
                     + ("(历史门店,外送由地址决定实际送哪家)" if is_delivery else ""))
            L.append(f"  {' + '.join(names)}")
            if last.get("totalAmount") and last.get("realTotalAmount"):
                L.append(f"  原价 ¥{last['totalAmount']} → 实付 ¥{last['realTotalAmount']}")
            for c in last.get("customs", []):
                L.append(f"  定制：{c.get('label')}（{c.get('raw','')}）")
    else:
        L.append("  无历史订单,建议先跑 fetch_profile.py")
    L.append("")
    L.append(f"【{scene} · 排序结果 前 {limit} 项】")
    if coupons_live:
        L.append(f"  可用券 {len(coupons_live)} 张：" + "、".join(c["name"] for c in coupons_live))
    else:
        L.append("  当前时段无可用券")
    if skipped:
        L.append(f"  已跳过 {len(skipped)} 张（不在有效期）：")
        for n, why in skipped:
            L.append(f"    · {n} — {why}")
    L.append("")
    for r in menu_ranked[:limit]:
        n = r.get("nutrition")
        miss = r.get("nutrition_missing") or []
        # 🔴 口径必须写出来。2026-10-10 外部实测:报了「麦香鱼 325kcal」,
        #    用户以为是整份套餐,实际整份 761kcal。数字不带口径 = 没有意义。
        scope = r.get("scope") or "整份"
        if n:
            nut = f"{scope} {n['kcal']}kcal 蛋白{n['protein']}g 钠{n['na']}mg"
        elif miss:
            nut = f"热量数据不全(缺{len(miss)}项:{miss[0]})"
        else:
            nut = "营养数据缺失"
        deal = f"原价¥{r['original']} 省¥{r['save']}" if r.get("save") else f"¥{r['final']} 无折扣"
        L.append(f"  {r['name'][:26]:28s} ¥{r['final']:<6} | {deal:22} | {nut}")
        if r.get("reason"):
            L.append(f"    ↳ {r['reason']}")
        if r.get("explain"):
            for k, v in r["explain"].items():
                L.append(f"    · {k}:{v}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="saving",
                    choices=["saving", "fitness", "hurry", "random", "try_new"])
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--date", default=None, help="yyyy-MM-dd HH:mm,默认明天12:00")
    ap.add_argument("--limit", type=int, default=6, help="展示前 N 项")
    ap.add_argument("--with-nutrition", action="store_true",
                    help="为套餐逐个调 query-meal-detail 叠加热量(多 N 次请求)")
    ap.add_argument("--channel-hint", default=None,
                    help="用户指定的渠道口语,如「送到家」「自取」。不给则按画像众数推")
    ap.add_argument("--city", default="上海市", help="外送场景的地址城市")
    ap.add_argument("--store", default=None, help="直接指定 storeCode")
    args = ap.parse_args()

    when = datetime.now()
    rdate = args.date or (when.replace(hour=12, minute=0, second=0, microsecond=0)
                          + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    profile = load_profile(args.profile)
    store_code, cp, ch_name, ch_why = resolve_anchor(profile, args.channel_hint, args.city)
    if store_code is None:
        print(f"[渠道] {ch_why}")
        return
    args.store = store_code
    print(f"[渠道] {ch_name}  依据：{ch_why}")
    print(f"[门店] {store_code}  params={cp}")
    print()
    meals = fetch_menu(rdate, store_code, cp)
    nutrition = fetch_nutrition()
    coupons = fetch_coupons()
    live, skipped = coupons_in_effect(coupons, when)

    if args.scene == "fitness":
        class _Api:
            """🔴 单个 code 查不到 detail 时返回 None,不要炸掉整轮推荐。

            实测(外送渠道):菜单里的某个套餐 code 在同门店 query-meal-detail
            报 `60001 未匹配到商品` —— 菜单与详情对不齐是线上真实情况。
            一个品查不到就跳过,不该让整个 fitness 场景失败。
            """
            @staticmethod
            def detail(code):
                try:
                    return (call_tool("query-meal-detail",
                                      dict(cp, code=code, storeCode=store_code,
                                           reservationDate=args.date)).get("data") or {})
                except Exception:
                    return {}
        ranked = rank_fitness(meals, nutrition, api=_Api())
    elif args.scene == "hurry":
        ranked = rank_hurry(meals, nutrition, seen=seen_names_of(profile))
    elif args.scene == "random":
        ranked = rank_random(meals, nutrition, profile.get("orders") or [])
        if not ranked:
            print("[随缘] 无历史订单或无可复现整单 —— 随缘的依据是「你最常点的那套」,")
            print("       没点过就没有冠军组合。先跑 fetch_profile.py 或先点一单。")
            return
    elif args.scene == "try_new":
        ranked = rank_try_new(meals, nutrition, seen=seen_names_of(profile),
                              freq=freq_of(profile))
    else:
        ranked = rank_saving(meals, nutrition)
    # 为展示的前 N 项补齐套餐热量(逐个调 query-meal-detail,数量少可控)
    if args.scene == "fitness" or args.with_nutrition:
        n_detail = 0
        for r in ranked[: args.limit]:
            if r.get("nutrition"):
                continue
            try:
                d = call_tool("query-meal-detail",
                              dict(cp, code=r["code"],
                                   storeCode=store_code, reservationDate=rdate))["data"]
                tot, missing = combo_nutrition(r["name"], d, nutrition)
                r["nutrition"] = tot
                r["nutrition_missing"] = missing
                n_detail += 1
            except RuntimeError:
                r["nutrition"] = None
                r["nutrition_missing"] = ["query-meal-detail 调用失败"]
            if n_detail >= args.limit:
                break

    print(f"口径：菜单 {len(meals)} 项 | 营养 {len(nutrition)} 条 | 卡包券 {len(coupons)} 张"
          f"（可用 {len(live)} / 跳过 {len(skipped)}）")
    print(f"门店 {args.store} | 渠道 {ch_name} | 取餐时段 {rdate} | 场景 {args.scene}")
    print("=" * 78)
    print(render_scene(ranked, args.scene, profile, live, skipped, limit=args.limit,
                    is_delivery=bool(cp.get("orderType") == 2)))
    print()
    print("提示：以上为推荐排序，尚未下单。真实价格以 calculate-price 试算为准。")


if __name__ == "__main__":
    main()