#!/usr/bin/env python3
"""语音层:把 Skill 的内部输出改写为「能被念出来」的形态。

## B2 重构(2026-10-11):状态机话术并入 flow.py
本模块曾自带一套 S1–S6 静态话术(speak_greeting/speak_narrow/…),
与 flow.py 的 S0–S9 是**两套状态机**——同一轮对话两种台词,必然漂移。
现在:
- **每轮话术由 flow.Turn.speech 同源产出**(与 text 同一批变量),本模块只提供工具:
  中文数字(cn_number/cn_count)、口语归一(normalize_terms)、
  口播净化(sayable/strip_internal)、复述确认(confirm_heard)。
- 旧的静态话术已删除——其中「欢迎您来」写法本就违反品牌检查
  (标准招呼语是「喜欢您来♥️」,见 greet.py 头注)。

## 为什么需要这个模块
确认卡是给眼睛设计的:code / key / 字段名 / [依据] / [间隔]。
念出来是灾难。语音层的职责是**同一份数据,两种形态**。

## 关键事实(20:35 实测)
MCP 35 个工具中零个语音接口 → 语音输入输出通道在宿主 App,
不在 MCP 协议内。本模块做「让输出可被念」+「让口语能进状态机」。

## 四条硬约束
0. **不念编号**,只念选项名(用户大脑里存的是菜名,不是序号)
1. **单句≤30 字**(约 6 秒),超长即打断用户
2. **禁止念内部信息**:code / key / 字段名 / 括号内部值 / [依据] / [间隔]
3. **识别错误必须显式复述确认**:「您是说麦香鱼?」不能默认听对了

用法:
    python3 voice.py --selfcheck   # 9 项自检(含 flow 口播不变量)
    python3 voice.py --flow-demo   # S0→S8 全流程演示,每轮打印 text 与 speech
"""
import argparse
import re

# ---- 术语等效词典(用户口语 → 门店标准说法) ----
# 实测口径:门店/取餐术语在语音里识别率低,必须做等效映射。
# 🔴 B3:flow._reply 的**入口**就调 normalize_terms —— ASR 文本先进词典,
#    再进编号/包含匹配,「打包」「车上取」「不要芝士」都能直达选项。
TERMS = {
    "车上取": "得来速", "开车窗口": "得来速", "路边取": "得来速", "免下车": "得来速",
    "大杯": "加大杯", "中杯": "中杯", "小杯": "小杯",
    "不要芝士": "去芝士", "不加芝士": "去芝士", "去芝士": "去芝士", "不要吉士": "去芝士",
    "少冰": "少冰", "冰少": "少冰", "多点冰": "多冰", "不要冰": "去冰", "常温": "标准冰",
    "随便": "照旧", "老样子": "照旧", "照旧": "照旧", "都行": "照旧",
    "打包": "外带", "带走": "外带", "堂食": "堂食", "店里吃": "堂食",
}

# 口语里绝对不能出现的内部符号
INTERNAL_PATTERNS = [
    (re.compile(r"\[\s*burger\s*\]|\[\s*drink\s*\]|\[\s*side\s*\]"), ""),  # 品类标签
    (re.compile(r"code\s*=\s*\S+"), ""),                                     # code
    (re.compile(r"key\s*=\s*\S+"), ""),                                      # key
    (re.compile(r"\(.*?\)"), ""),                                            # 括号内部值
    (re.compile(r"\[依据\].*"), ""),                                         # 审计行
    (re.compile(r"\[间隔\].*"), ""),                                         # 审计行
    (re.compile(r"¥"), "块钱"),                                              # 符号
]

CN_NUM = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五",
          "6": "六", "7": "七", "8": "八", "9": "九", "10": "十"}
# 🔴 计数一律用「二」(二十/二十八);「两」只用在「元」本身(cn_number 的 2 元)。
# B2 注:曾有的 _cn_qty(两份)随静态话术一起删除——Turn.speech 里
# 数量词走 sayable 自然语言,不再需要专门的量词转换。


def _cn_int(n: int) -> str:
    """整数转中文口语。0-99 精确;100 以上回退阿拉伯数字。

    🔴 100 以上不做「十十块」这类拼接(实测曾输出「十十块五」),
    直接念「一百」比错误拼接更自然 —— 语音输出宁朴素不出错。
    """
    if n < 0:
        return "负数"
    if n < 10:
        return CN_NUM.get(str(n), str(n))
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN_NUM.get(str(n - 10), "")
    if n < 100:
        tens, ones = divmod(n, 10)
        return (CN_NUM.get(str(tens), "") + "十"
                + (CN_NUM.get(str(ones), "") if ones else ""))
    return str(n)


def cn_number(s):
    """金额专用:'34.5' → '三十四元五'。

    🔴 金额单位用「元」不用「块」(用户 20:37 指定)。
    🔴 不能用于计数 —— 计数绝不带单位。
    """
    try:
        v = float(s)
    except (TypeError, ValueError):
        return str(s)
    if v < 0:
        return "金额异常"
    whole = int(v)
    frac = int(round((v - whole) * 10))
    money = _cn_int(whole)
    if whole == 2:
        money = "两"
    elif 20 <= whole < 100 and whole % 10 == 2:
        money = _cn_int(whole)
    elif whole >= 100:
        if whole == 100:
            money = "一百"
        elif whole < 110:
            money = "一百零" + CN_NUM.get(str(whole - 100), "")
        else:
            hund, rest = divmod(whole, 100)
            if rest == 0:
                money = _cn_int(hund) + "百"
            elif rest < 10:
                money = _cn_int(hund) + "百零" + CN_NUM.get(str(rest), "")
            else:
                money = _cn_int(hund) + "百" + _cn_int(rest)
    if frac == 0:
        return f"{money}元"
    return f"{money}元{CN_NUM.get(str(frac), '')}"


def cn_count(s):
    """计数专用:'11' → '十一';'28' → '二十八'。

    🔴 不能用 cn_number —— 金额的「元」会污染天数与取餐号。
    🔴 取餐号只念数字,>99 或非法值不转换,直接说「取餐号请看小程序」。
    """
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "请看小程序"
    if n <= 0 or n > 99:
        return "请看小程序"
    return _cn_int(n)


def strip_internal(text: str) -> str:
    """剔除所有不该被念出来的内部信息。"""
    for pat, repl in INTERNAL_PATTERNS:
        text = pat.sub(repl, text)
    return text


def normalize_terms(text: str) -> str:
    """口语术语 → 门店标准说法。"""
    for k, v in TERMS.items():
        if k in text:
            text = text.replace(k, v)
    return text


def sayable(text: str, max_chars: int = 30) -> str:
    """生成口播文本:剔除内部信息 + 术语归一 + 长度控制。"""
    t = strip_internal(text)
    t = normalize_terms(t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"^[\s\-·>]+", "", t)
    if len(t) > max_chars:
        t = t[:max_chars].rstrip("，,、") + "。"
    return t


def confirm_heard(asr_text, candidates, question="您是说"):
    """识别错误显式复述确认。

    禁止默认听对了。候选项来自菜单真实商品名,做包含匹配给建议。
    """
    if not asr_text:
        return None
    best = None
    for c in candidates:
        name = c.get("name", "")
        if not name:
            continue
        if asr_text in name or name in asr_text or any(
                ch in asr_text and ch in name for ch in set(asr_text) if len(ch) > 1):
            best = name
            break
    if not best:
        return None
    return sayable(f"{question}{best}吗?", 20)


# ---- 自检(9 项,编号固定,selftest 依赖) ----

def _selfcheck():
    print("=" * 60)
    print("语音层自检(工具 + flow 口播不变量)")
    print("=" * 60)
    ok = [0]

    def check(label, cond, detail=""):
        ok[0] += bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}" + (f" — {detail}" if detail else ""))

    print("\n[1] 剔除内部信息")
    raw = ("· 培根安格斯厚牛堡 ×1  [burger] key=0-0(移除) [依据] 历史最集中=12点")
    st = strip_internal(raw)
    check("code/key/品类标签/审计行全被剥掉",
          all(k not in st for k in ("key=", "[依据]", "[burger]")))

    print("\n[2] 金额中文(金额语义)")
    cases = {"34.5": "三十四元五", "2": "两元", "105": "一百零五元", "9.9": "九元九"}
    check("34.5/2/105/9.9 全对", all(cn_number(k) == v for k, v in cases.items()),
          " ".join(f"{k}→{cn_number(k)}" for k in cases))

    print("\n[3] 计数中文(计数语义,不带单位)")
    cases = {"11": "十一", "28": "二十八", "2": "二", "150": "请看小程序"}
    check("11/28/2/150 全对", all(cn_count(k) == v for k, v in cases.items()),
          " ".join(f"{k}→{cn_count(k)}" for k in cases))

    print("\n[4] 术语归一(ASR 口语 → 标准说法)")
    cases = {"我要大杯": "我要加大杯", "车上取": "得来速", "打包": "外带",
             "不要芝士": "去芝士", "店里吃": "堂食"}
    check("5 组口语全归一", all(normalize_terms(k) == v for k, v in cases.items()))

    print("\n[5] 复述确认")
    q = confirm_heard("麦香鱼", [{"name": "麦香鱼"}, {"name": "麦香鸡"}])
    miss = confirm_heard("完全无关的话", [{"name": "麦香鱼"}])
    check("命中复述 + 无关不硬猜",
          q == "您是说麦香鱼吗?" and miss is None, f"{q} / {miss}")

    print("\n[6] 口播长度上限")
    long = sayable("x" * 60, 30)
    check("超长截断到 ≤31 字", len(long) <= 31, f"{len(long)} 字")

    print("\n[7] flow S0 迎宾口播(≤6 秒)")
    from flow import Flow
    import test_flow as tf
    f = Flow(profile=tf.profile(), api=tf.FakeApi(), at=tf.AT)
    g = f.greet()
    check("非空 ≤60 字且含品牌招呼语",
          g.speech and len(g.speech) <= 60 and "喜欢您来" in g.speech,
          f"{len(g.speech or '')} 字")

    print("\n[8] flow S7 复诵口播(金额中文)")
    f2 = Flow(profile=tf.profile(), api=tf.FakeApi(), at=tf.AT)
    f2.turn_text()                 # S0+S1(greet 之后必须先问渠道)
    for r in ("送到家", "示范路", "确认", "门口"):   # S3A 口语选地址 → S3B → S4
        f2.reply(r)
    f2.reply("安格斯")            # B3:模糊命中 → 复述确认
    f2.reply("对")                # 确认 → S6
    f2.reply("就这样")            # S6 → S7
    s7 = f2.ctx.get("_last_speech") or ""
    check("S7 口播非空且金额为中文",
          bool(s7) and ("元" in s7) and not re.search(r"\d+\.\d", s7), s7[:40])

    print("\n[9] 全流程零泄漏 + 不念编号")
    f2 = Flow(profile=tf.profile(), api=tf.FakeApi(), at=tf.AT)
    speeches = [f2.turn_text() and f2.ctx["_last_speech"]]
    for r in ("送到家", "示范路", "确认", "门口", "1", "就这样", "确认下单"):
        f2.reply(r)
        speeches.append(f2.ctx.get("_last_speech") or "")
    bad = []
    for sp in speeches:
        for kw in ("code", "key", "[依据]", "[", "]", "0-0", "1-1", "productCode"):
            if kw in sp:
                bad.append(f"泄漏 {kw}")
        if re.search(r"回\s*[1-9]", sp):
            bad.append(f"念了编号:{sp}")
    check("每轮无内部信息、无「回N」", not bad, ";".join(bad[:3]) or "全过")

    print("=" * 60)
    print(f"{ok[0]}/9 通过" + (" ✓" if ok[0] == 9 else " ✗"))
    return 0 if ok[0] == 9 else 1


# ---- B4:全流程语音演示 ----

def flow_demo():
    """S0→S8 离网演示(FakeApi):每轮打印屏幕文本(text)与口播(speech)。

    演示序列覆盖三条核心意图:
    - 意图2 语音主入口:口语「送到家/安格斯/对/我不吃芝士」全程不碰数字
    - B3 复述确认:「安格斯」模糊命中 → 「您是说…吗?」→「对」
    - A3 声明留底:「我不吃芝士」进 ctx.declared_new(演示不落盘)
    """
    from flow import Flow
    import test_flow as tf

    f = Flow(profile=tf.profile(), api=tf.FakeApi(), at=tf.AT)

    def show(label, text, speech):
        print(f"\n── {label} ──────────────────────")
        for ln in (text or "").splitlines():
            print(f"  {ln}")
        print(f"  🔊 {speech}")

    print("=" * 64)
    print("门店点单全流程演示(离网假 API · S0→S8 · 语音为主入口)")
    print("=" * 64)
    show("S0+S1 迎宾+渠道", f.turn_text(), f.ctx["_last_speech"])

    script = [
        ("S3A 地址(口语:送到家)", "送到家"),
        ("S3A 选地址(口语:示范路)", "示范路"),
        ("S3B 门店(确认)", "确认"),
        ("S4 放置点", "门口"),
        ("B3 复述确认(口语:安格斯)", "安格斯"),
        ("S6 细节(确认:对)", "对"),
        ("A3 声明留底(口语:我不吃芝士)", "我不吃芝士"),
        ("S7 复诵报价", "1"),
        ("S8 支付(口语:确认下单)", "确认下单"),
    ]
    for label, say in script:
        t = f.reply(say)
        show(f"{label} [用户说:{say}]", t.text, t.speech)

    print("\n── 声明留底(A3,演示不落盘) ──────────────")
    for e in f.ctx.get("declared_new") or []:
        print(f"  kind={e['kind']} label={e['label']} raw=「{e['raw']}」")
    print("  真实宿主在会话结束调 flow.flush_declared() 追加进 assets/declared.json")
    print("\n── 不变量 ──────────────────────")
    bad = f.invariants()
    print("  " + ("全过 ✓" if not bad else "✗ " + ";".join(bad)))
    print("\n🔴 演示到此为止:create-order 全程未调用,不产生任何费用。")
    return 0 if not bad else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="语音层:口播工具与流程演示")
    ap.add_argument("--selfcheck", action="store_true", help="跑 9 项自检")
    ap.add_argument("--flow-demo", action="store_true",
                    help="S0→S8 全流程演示,每轮打印 text 与 speech")
    args = ap.parse_args()
    if args.selfcheck:
        raise SystemExit(_selfcheck())
    if args.flow_demo:
        raise SystemExit(flow_demo())
    raise SystemExit(_selfcheck())
