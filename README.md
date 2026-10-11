<div align="center">

# 🍔 今天吃什么 · 最懂你的麦门小姐姐

**「今天中午吃啥？不想动脑。」**

一句话问出去，回来的是**照你最近几单的老样子**，外加一条**有理由的偏离**——
去芝士、少冰这些你早就写在订单里的偏好，它记得，不用你再说一遍。

<br>

[![MCP](https://img.shields.io/badge/MCD--MCP-35%20tools-DA291C.svg)](MCP_INTEGRATION.md)
[![Skill](https://img.shields.io/badge/WorkBuddy-Skill-FFC72C.svg)](mcd-what-to-eat-today/SKILL.md)
![Deps](https://img.shields.io/badge/Dependencies-zero-success.svg)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**零第三方依赖** · **纯 Python 标准库** · **clone 下来即可运行**

> 💛 如果这个思路对你有用，点个 **⭐ Star** 支持一下！本作品正在参加「麦当劳程序员节创意开发大赛」，**Star 数决定排名** 🙏

</div>

---

## 🧭 30 秒看懂

```text
你：今天吃什么

麦门：喜欢您来♥️，11 天没见了。
      您平时都是 12 点前后，今天晚了不少。

      🔥 照你最近的
      经典安格斯单人餐（麦金卡免配）
      培根安格斯厚牛堡（去芝士）+ 中薯条 + 可口可乐中杯（少冰）
      原价 ¥102 → 实付 ¥69.9

      🔄 换个吃？
      双层深海鳕鱼堡（去芝士）套餐
      比老样子少 367 kcal、少 481mg 钠，价差 +¥2
      这是你在麦麦吃过 7 次的选择
```

**它凭什么知道你不要芝士？** 因为你最近 4 单都写了「高达干酪片(不要)」「吉士(不要)」。这些字藏在订单明细的定制字段里——官方接口有这个字段，但几乎没有 Skill 用它。

**它凭什么知道你 11 天没吃了？** `order-list` 返回你最近一单的 `createTime`，这是真数据不是猜的。

---

## 🎯 它和别的「麦当劳 Skill」差在哪

| 做法 | 典型代表 | 它回答的问题 | 门槛 |
|---|---|---|---|
| 查券 / 省钱清单 | mcd-savings 系列 | 「有什么券能领」 | 要你自己去用 |
| 卡路里统计 | mcd-order-calories | 「我上周吃了多少卡」 | 事后复盘 |
| 活动地图 | mcd-event-radar | 「附近哪家有活动」 | 要先选城市 |
| 营养推荐 | mcd-happy-meal | 「孩子吃什么健康」 | 要报年龄+餐段 |
| **本项目** | 今天吃什么 | **「我现在该点啥」** | **零参数，一句话** |

差异不在功能多少，在**站在哪个时刻**：

**其他项目都在用户已经想清楚之后服务；本项目服务的是「还没想清楚的那 3 秒」——打开 App 之前那段最卡的犹豫。**

所以本项目的三条硬约束：

| 约束 | 为什么 |
|---|---|
| **默认零参数** | 不问城市、不问餐段、不问忌口。历史订单里有全部答案 |
| **不问「你想吃什么」** | 用户回「随便」就直接给最高频组合，不列菜单、不做问卷 |
| **只给两单，不给菜单** | 一单照旧（两秒能确认），一单偏离（带理由）。**给菜单等于把决策成本推回给用户** |

---

## 🚀 三步开吃

> **想先看效果？** 不需要安装也不用 Token，clone 下来直接跑第 3 步的命令即可。

### 第 1 步 · 把这句话发给 WorkBuddy

不用打开任何命令行——WorkBuddy 自己就会干活。复制下面这句，粘贴进 WorkBuddy 对话框，回车：

```text
帮我安装这个技能：https://github.com/lanceagi/mcd-what-to-eat-today
把仓库里 mcd-what-to-eat-today 文件夹复制到用户级技能目录（~/.workbuddy/skills/），
确认 SKILL.md 存在，装完提醒我重开一轮会话
```

它会自己下载、安装、验收。装好后**新开一轮会话**。

### 或者 · 一行命令装（任意 Agent，免对话）

不用 WorkBuddy 也能装。复制对应系统的一行命令，跑完即可：

**macOS / Linux**（技能装到 `~/.agents/skills/`，WorkBuddy 用户把目标目录换成 `~/.workbuddy/skills/`）：

```bash
git clone --depth 1 https://github.com/lanceagi/mcd-what-to-eat-today /tmp/mcd-skill \
  && mkdir -p ~/.agents/skills \
  && cp -R /tmp/mcd-skill/mcd-what-to-eat-today ~/.agents/skills/ \
  && rm -rf /tmp/mcd-skill && echo "✅ 装好了，技能目录：~/.agents/skills/mcd-what-to-eat-today"
```

**Windows（PowerShell）**：

```powershell
git clone --depth 1 https://github.com/lanceagi/mcd-what-to-eat-today $env:TEMP\mcd-skill; `
  New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null; `
  Copy-Item -Recurse "$env:TEMP\mcd-skill\mcd-what-to-eat-today" "$HOME\.agents\skills\"; `
  Remove-Item -Recurse "$env:TEMP\mcd-skill"; "✅ 装好了，技能目录：$HOME\.agents\skills\mcd-what-to-eat-today"
```

**没装 git？** 下载 [zip 包](https://github.com/lanceagi/mcd-what-to-eat-today/archive/refs/heads/main.zip) 解压，把里面的 `mcd-what-to-eat-today/` 文件夹放进你的 Agent 技能目录。

> 各家 Agent 的技能目录对照：WorkBuddy `~/.workbuddy/skills/` · ZCode `~/.agents/skills/` · Claude Code `~/.claude/skills/`。装完都要**新开一轮会话**才会生效。
>
> 仓库根目录的四件套（README / 声明 / MCP 文档 / 示例配置）是参赛材料，装技能时不需要拷贝，上面的命令只取 `mcd-what-to-eat-today/` 子目录。

### 第 2 步 · 申请 MCP Token（免费）

访问 <https://open.mcd.cn/mcp> → 手机号登录 → 右上角「控制台」→ 点「激活」→ 一键复制。

### 第 3 步 · 说一句话

```
今天吃什么
```

首次使用会先向你举证推断出的偏好：

```
我从你最近 3 单里看到：
  4 单「吉士(不要)」、1 单「高达干酪片(不要)」、1 单「冰(少冰)」
推断：汉堡去芝士、可乐少冰。以后默认这么点，对吗？
```

**回「对」之后**，这些偏好存进本机画像文件，以后一句「今天吃什么」就够了。

---

## ✅ 它给的这些数字，哪些是真的？

| 你看到的 | 是真的吗 | 一句话解释 |
|---|---|---|
| 「11 天没见了」 | ✅ 真 | `order-list` 里最近一单的 `createTime`，算出来的 |
| 「去芝士」「少冰」 | ✅ 真 | `query-order` 的 `comboItemList[].itemName` 字段原文，只有它含定制 |
| 「原价 ¥102 → 实付 ¥69.9」 | ✅ 真 | `query-order` 的 `totalAmount` / `realTotalAmount`，不是估算 |
| 「比老样子少 367 kcal」 | ⚠️ 需查表 | 来自 `list-nutrition-foods`；**表里查不到的项会写「不可估算」，不估值冒充** |
| 「省 ¥30.0」 | ✅ 官方核价 | 菜单层 `currentPrice` 已含优惠；最终价以 `calculate-price` 为准 |
| 「照你最近的」 | ✅ 真 | `order-list` + `query-order` 的真实历史，不是最贵/最好吃排序 |
| 「可领券 6 张」 | ❌ **本项目不主动领** | `auto-bind-coupons` 是写操作，需你明确要求才执行 |

**不给模糊数字，不拿估算冒充实测，查不到就写「不可估算」** —— 这是本项目的底线。

### 三条数据诚实红线

1. **「没查到」就说没查到**，不用模型记忆或示例数据冒充实时结果
2. **比例必须带口径**（「33%(42 单去重口径)」），时点数据标「截至」
3. **不预测你该吃饭了** —— 实测 10 单间隔 1–16 天、中位数 5 天，方差太大，只陈述「你已 N 天没吃」

---

## 👥 谁需要它

| 你是 | 你会问 |
|---|---|
| 上班族日常 | 「今天中午吃啥，懒得想」 |
| 健身增肌期 | 「今天想吃麦的，怎么点不毁训练」 |
| 每周至少一次麦门 | 「照上次那样再来一份」 |
| 有忌口的人 | 「每次都要说不加芝士，累」 |
| 选择困难 | 「随便，给个推荐」 |

---

## ❓ 小白常见问题

**Q：它会偷偷帮我下单吗？**
不会。默认只查询。真要下单，它会把商品、每项定制、原价、券后价、取餐方式全部列出来，
**你逐项点头之后**才动手。而且**付款必须在麦当劳支付页或 APP 里自己完成**——MCP 的 35 个工具里没有任何支付工具，脚本层也不含下单提交路径。

**Q：Token 安全吗？**
相当于一把只开你自己家门的钥匙。只从环境变量 `MCD_MCP_TOKEN` 或本机 `~/.mcd_token`（权限 600）读取，不进仓库、不进日志、不在对话里回显。介意的话随时去官方平台重新申请作废旧的。

**Q：它会读到我的手机号和地址吗？**
`query-order` 的返回里确实含收件人姓名、手机号、地址。本项目用**白名单机制**，这些字段一律丢弃、不落盘、不输出。参赛规则明确禁止提交他人个人信息，代码里对此做了硬约束。

**Q：我没有历史订单怎么办？**
冷启动。「喜欢您来♥️，第一次来。」然后直接给你当前菜单里的推荐，一样零参数。

**Q：会乱领券吗？**
不会。实测发现 `auto-bind-coupons` 这个工具名字像只读，实际会一次领走全部可领券（开发时踩过这个坑，给账户领入 6 张）。本项目把它按**写操作**对待，未经你明确要求绝不调用。

**Q：装完没反应？**
重启 WorkBuddy 或新开一轮会话，再说「今天吃什么」。

---

## 🔬 给爱看细节的同学

以下是技术部分，普通用户不用读。

### 四个实测发现（2026-10-10 真实 Token 联调，非文档推测）

**1️⃣ 线上有 35 个工具，官方文档只写了 33 个**
未收录的 2 个是 `query-survey-coupon`（查订单 CSAT 答卷 + 奖券核销状态）和 `query-promotions`（团餐满减满折）。
更坑的是文档里**工具名写错了 2 个**：

| 官方文档写的 | 线上真实名 |
|---|---|
| `query-partystore-date` | **`query-party-store-date`** |
| `query-partystore-session` | **`query-party-store-session`** |

照文档名字写代码必然调用失败。

**2️⃣ 定制信息的唯一来源是一个没人用的字段**
```
query-order → data.orderProductList[].comboItemList[].itemName
```
官方字段说明原文「子项或特调定制信息」。实测返回值：
```json
{ "itemName": "芝士安格斯厚牛堡 高达干酪片(不要)", "itemQuantity": 1 }
{ "itemName": "可口可乐中杯 冰(少冰)", "itemQuantity": 1 }
```
**「少冰」的 key 是 `1-1`，不是把标准冰减一**——所有冰镇饮料统一 `code=200002`，靠 key 区分：去冰 `0-0`／标准 `0-1`／多冰 `0-2`／**少冰 `1-1`**。写错会静默退化成标准冰。

**3️⃣ 菜单层 `currentPrice` 已经含优惠，别重复算折扣**
实测菜单 107 项里，0–9 元区间有 16 项，全是酱料配料。纯按价格升序排，这 16 项会冲到推荐列表最前面。本项目设了 `MIN_ITEM_PRICE = 15` 门槛。

**4️⃣ `auto-bind-coupons` 是写操作，名字骗人**
官方描述「自动领取麦麦省所有当前可用的优惠券」，看起来像查询。实测调用后账户**真的领入 6 张券**（卡包可见 5 张）。第二次调用返回「暂无可领取的优惠券」，幂等。
**教训**：凡带 `auto-` / `bind` / `create` / `draw` 前缀的工具，一律按写操作对待。

### 架构一句话

**Agent 编排 + 确定性脚本**：数字与规则链全部钉在零依赖 Python 脚本里，LLM 只负责理解需求、执行流程和守护确认门控。

```
用户一句话
  → greet.py      门店式开场（读本地画像，不调接口）
  → fetch_profile.py   order-list → query-order×3 → 签名推断 → profile.json
  → recommend.py   now-time-info + query-meals + 券四路 → 场景排序
  → order_builder.py   calculate-price 官方核价 → 确认卡（无 submit 路径）
  → ⛔ 用户明确同意后才create-order
```

**为什么这么分？** 价格、券、营养这些数字不该交给模型猜——它们全部来自 MCP 返回，确定性脚本算出来的结果可复现、可审计。

### 券要查四路，漏一路就找不到最优

| 来源 | 工具 | 层级 |
|---|---|---|
| 卡包现有 | `query-my-coupons` | 🟢 |
| 麦麦省可领 | `auto-bind-coupons` | 🔴 **写操作** |
| 门店券 | `query-store-coupons` | 🟢 |
| 活动券 | `available-coupons` | 🟢 |

实测省钱场景的输出：卡包 5 张券，**2 张可用、3 张因不在有效期被跳过**。券时效过滤是实测出来的——不筛就会推荐用不了的券，这是省钱工具最大的失信点。

### 真实运行输出（2026-10-10 实测）

```console
$ python3 scripts/recommend.py --scene saving
口径：菜单 107 项 | 营养 155 条 | 卡包券 5 张（可用 2 / 跳过 3）
门店 <STORE_CODE 已脱敏> | 取餐时段 2026-10-11 12:00 | 场景 saving
==============================================================================
【签名】去芝士 + 少冰（自动带上，无需再说）

【照你最近的】
  2026-09-29 12:07:25｜麦当劳<门店名已脱敏>餐厅
  经典安格斯单人餐（麦金卡免配） + 双层深海鳕鱼堡
  原价 ¥102 → 实付 ¥69.9
  定制：去芝士（芝士安格斯厚牛堡 高达干酪片(不要)）
  定制：少冰（可口可乐中杯 冰(少冰)）
  定制：去芝士（风味双层深海鳕鱼堡 吉士(不要)）

【saving · 排序结果 前 6 项】
  可用券 2 张：9.9元中杯冰美式、人气麦旋风买一送一
  已跳过 3 张（不在有效期）：
    · 巧克力味厚松饼猪柳蛋套餐 — 不在有效期(2026-10-11 05:00~10:29)

  堡堡随心拼（7件任选）           ¥73.0 | 原价¥103.0 省¥30.0
  人气超值四件套随心选             ¥28.0 | 原价¥57.0  省¥29.0
  安格斯厚牛堡四件套随心选          ¥37.0 | 原价¥66.0  省¥29.0
  龙焰鸡腿堡四件套                 ¥29.0 | 原价¥57.0  省¥28.0
```

### 常用命令

```bash
python3 scripts/greet.py                                # 开场问候 + 猜的方向
python3 scripts/greet.py --at "2026-10-11 08:00"        # 指定时刻（测试分支用）
python3 scripts/fetch_profile.py                        # 重建画像（11 次只读请求）
python3 scripts/recommend.py --scene saving             # 省钱场景
python3 scripts/recommend.py --scene fitness            # 健身场景（≤900kcal 且钠≤700mg）
python3 scripts/recommend.py --scene saving --with-nutrition --limit 8
python3 scripts/order_builder.py --code 511781 --code 3050 --force-ice   # 组装+试算，不提交
```

### 项目结构

```
mcd-what-to-eat-today/
├── SKILL.md                      # 技能主文件（Agent 运行时加载）
├── scripts/
│   ├── mcd_client.py             # MCP 客户端（纯标准库手写 Streamable HTTP 握手）
│   ├── fetch_profile.py          # 订单指纹 → 签名推断 → profile.json
│   ├── greet.py                  # 门店式开场问候（纯本地，不调接口）
│   ├── recommend.py              # 场景推荐：saving / fitness
│   └── order_builder.py          # 订单组装 + 试算 + 确认卡（不含 submit 路径）
├── references/
│   └── mcp-api.md                # 接口速查：工具清单 / 定制取值 / 下单规则 / 隐私红线
├── assets/
│   ├── today-card.html           # 今日麦门卡（单文件 HTML，零依赖，可截图分享）
│   └── profile.json              # 本地画像（运行时生成，已 gitignore）
```

### 已知限制

- `order-list` **硬上限 10 单无分页**（5 种翻页参数实测全无效），口味漂移的用户画像会过时
- 签名推断**首次必须经你确认**，不允许静默修改你的偏好
- 营养表 160 条覆盖不全，套餐靠子项求和；子项查不到则输出「不可估算」
- `random` / `try_new` / `hurry` 三个场景尚未实装（`saving`/`fitness` 已实装）

---

## 📋 参赛材料

| 要求 | 文件 |
|---|---|
| 项目介绍 / 安装 / 使用示例 / 目标用户 | 本 README |
| 参赛声明（官方原文未改动） | [CONTEST_DECLARATION.md](CONTEST_DECLARATION.md) |
| MCP 接入说明（Server / Tool / 调用流程 / 业务价值） | [MCP_INTEGRATION.md](MCP_INTEGRATION.md) |
| 脱敏 MCP 配置示例（仅环境变量占位符） | [mcp-config.example.json](mcp-config.example.json) |
| 源代码 | `mcd-what-to-eat-today/` |
| 项目结构 |  |
| --- | --- |
| 源代码 | `mcd-what-to-eat-today/` |
| WorkBuddy 开发上下文 | [workbuddy.md](workbuddy.md) |

### 🖼 今日麦门卡

打开 [`mcd-what-to-eat-today/assets/today-card.html`](mcd-what-to-eat-today/assets/today-card.html) 即可看到单文件 HTML 卡片——**零依赖、可直接双击打开**，数据全部来自官方 MCP 实时查询，门店与商品编码已脱敏。

测完就能截图发群，这是本项目 Star 的主要来源。

### 合规说明

- `CONTEST_DECLARATION.md` 为官方原文，**文件名与内容均未改动**
  （SHA-256 `cd28101bcb29cc8fb24b372fe91ce6b42c4a1487a54e01f7ade0c8c6a84241b7`）
- 仓库内**不含任何真实 Token、密钥、账号凭证或他人个人信息**；配置文件仅使用环境变量占位符
- `query-order` 返回的 `deliveryInfo`（收件人姓名／手机号／地址）由白名单机制整块丢弃，不落盘不输出
- `assets/profile.json` 含用户饮食偏好，已在 `.gitignore` 中
- 本项目**只读优先**：写操作（`create-order` / `auto-bind-coupons` 等）必须经用户明确确认，`order_builder.py` 代码层不含提交路径
- 不贬低品牌、不与其他品牌对比；输出不构成医疗、营养或其他专业建议，餐品信息以麦当劳官方渠道实时结果为准

---

## 📄 License

[MIT](LICENSE) · 本项目为「麦当劳程序员节创意开发大赛」参赛作品，由参赛者独立开发，**非麦当劳官方产品**。