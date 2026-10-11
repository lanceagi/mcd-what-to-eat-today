#!/usr/bin/env python3
"""S5 菜单浏览(2026-10-11):服务端分类 → 四桶 → 翻页选品。

query-meals 一直给 15 个服务端 categories(一项可跨多类,实测 100% 覆盖 114 项),
但 flow.api.menu() 只取 meals 丢掉分类 —— S5「看菜单」因此长期只能如实拦截。
本模块把 15 个类目映射成 4 个展示桶(编号协议上限 4):
1. 类目名关键词命中 → 整类入桶(按类目出现顺序,一项先到先得,不重复)
2. 类目名含糊(如「人气热卖」) → 逐项按商品名关键词
3. 仍不中 → 兜底进「小食」(对应服务端「小食甜品/其他」的语义)
每页 PAGE_SIZE 项 + 「更多」+「返回点餐」,不破一屏 4 选项。
纯函数、零依赖,离线可测(test_flow I20)。
"""

PAGE_SIZE = 2
BUCKETS = ("套餐", "汉堡", "小食", "饮品")

# 服务端类目名 → 展示桶。含糊类目(人气热卖等)不列在这里,走逐项判定。
_CAT_RULES = (
    ("套餐", ("多人分享", "口福", "专享", "周末", "大卡", "开心乐园")),
    ("汉堡", ("汉堡", "堡")),
    ("小食", ("蘸酱", "炸鸡", "小食", "甜品")),
    ("饮品", ("咖啡", "饮品")),
)

# 商品名 → 展示桶,判定顺序即元组顺序(先套餐后汉堡,鸡腿堡归汉堡不归小食)。
_ITEM_RULES = (
    ("套餐", ("套餐", "单人餐", "件套", "口福", "欢享", "开心乐园", "早餐")),
    ("汉堡", ("堡", "卷")),
    ("小食", ("薯", "鸡块", "鸡翅", "鸡腿", "鸡", "派", "圆筒", "圣代",
              "麦旋风", "玉米", "沙拉", "酱", "饼")),
    ("饮品", ("可乐", "雪碧", "咖啡", "茶", "汁", "奶昔", "椰", "水")),
)


def _match(name, rules):
    for bucket, kws in rules:
        if any(k in name for k in kws):
            return bucket
    return None


def cat_bucket(cat_name):
    """类目名 → 桶;含糊类目返回 None(逐项判定)。"""
    return _match(cat_name or "", _CAT_RULES)


def item_bucket(item_name):
    """商品名 → 桶;都不中兜底「小食」(服务端「小食甜品/其他」语义)。"""
    return _match(item_name or "", _ITEM_RULES) or "小食"


def classify(categories, meals):
    """(categories, meals) → {桶: [code,...]},桶内顺序=类目出现顺序。"""
    out = {b: [] for b in BUCKETS}
    assigned = set()

    def add(code, bucket):
        if code and code not in assigned:
            assigned.add(code)
            out[bucket].append(code)

    for cat in categories or []:
        name = (cat or {}).get("name") or ""
        b = cat_bucket(name)
        for m in (cat or {}).get("meals") or []:
            code = (m or {}).get("code")
            add(code, b if b else item_bucket(
                ((meals or {}).get(code) or {}).get("name") or ""))
    for code in (meals or {}):          # 不在任何类目里的漏网项(实测为 0)
        add(code, item_bucket((meals.get(code) or {}).get("name") or ""))
    return out


def bucket_label(bucket, buckets):
    return f"{bucket}({len(buckets.get(bucket) or [])})"


def page(codes, start):
    """返回 (本页 code, 是否还有下一页)。"""
    codes = codes or []
    visible = codes[start:start + PAGE_SIZE]
    return visible, start + PAGE_SIZE < len(codes)
