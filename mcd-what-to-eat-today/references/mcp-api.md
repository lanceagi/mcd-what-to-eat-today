# 麦当劳 MCP 接口速查(v1,基于 2026-10-10 真机实测)

> 完整 35 工具清单与 20 条实测发现见 `../docs/03-MCP接口规格.md`(权威源)。
> 本文件只收录 Skill 运行所需的最小集合。**没有一条来自文档推断,全部实测。**

## 连接

- 端点:`https://mcp.mcd.cn`(streamable HTTP,Bearer Token)
- Token 申请:`https://open.mcd.cn/mcp`(手机号登录 → 控制台 → 激活)
- 本地 Token:环境变量 `MCD_MCP_TOKEN` 或 `~/.mcd_token`
- 限流:官方 600 次/分钟;策略为首屏只查最近 3 单,其余按需

## 核心调用链

```
now-time-info                 # 先确定时段(菜单随时段变,顺序不能反)
order-list                    # 无参数,固定返回最近 10 单,无分页
  └─ orderId → query-order    # 逐单调,拿定制/券/原价(order-list 里没有)
query-meals(storeCode, orderType, beType, reservationDate)   # 当前门店在售菜单
query-meal-detail(code, orderType, beType, storeCode, reservationDate)  # 定制选项表
list-nutrition-foods          # 160 款营养数据(热量/蛋白/脂肪/钠,文本表格)
create-order                  # 🔴 写操作,必须用户逐单确认
```

## 硬规则(实测踩坑)

1. **定制唯一来源**:`query-order.data.orderProductList[].comboItemList[].itemName`,官方字段说明原文「子项或特调定制信息」。`order-list.productName` 的括号是套餐自带描述(如「麦金卡免配」),不含定制。
2. **子项字段名不一致**:order-list 用 `name`,query-order 用 `itemName`。
3. **商品 code 跨门店不通用**:换店后必须重新 `query-meals`,否则 `60001 未匹配到商品`。
4. **`reservationDate` 必须是未来时间**。
5. **含 `unselectedKey` 的特制组,组内所有项都必须传**——去吉士时,酱/生菜/培根/黄油也要各自传「保留」值,漏传即下单失败。
6. **`minValues=1` 且只有 1 项的组必选**,无「不选」选项。

## 定制取值表(关键项,全表 90 项见 docs/03)

| 定制 | code | key |
|---|---|---|
| 去吉士 | `100141` | `0-0` |
| 去高达干酪片 | `102215` | `0-0` |
| 饮料去冰 | `200002` | `0-0` |
| 饮料标准冰 | `200002` | `0-1` |
| 饮料多冰 | `200002` | `0-2` |
| 饮料少冰 | `200002` | `1-1` |

`create-order` 的特制走 `items[].modification.values[]`,每项含 `key` + `code` + `quantity`;券用 `couponId`/`couponCode` 内联在 item 上,下单与用券一次调用完成。

## 券(四路查全,漏一路就找不到最优)

| 工具 | 说明 | 类型 |
|---|---|---|
| `query-my-coupons` | 已入卡包 | 🟢 返回 markdown |
| `auto-bind-coupons` | 🔴 **写操作!**「自动领取麦麦省所有当前可用的优惠券」,无参数,一次领全部 | 🔴 返回 markdown |
| `query-store-coupons` | 门店券 | 🟢 返回 markdown |
| `available-coupons` | 活动券 | 🟢 返回 markdown |

**⚠️ 实测纠正(19:50)**:早期把 `auto-bind-coupons` 当只读,误调后实际给账户领入 6 张券(卡包可见 5 张)。凡 `auto-` / `bind` / `create` / `draw` 前缀一律按写操作对待。

**⚠️ 券类工具返回 markdown 而非 JSON**,必须用 `mcd_client.call_tool_text()` + `parse_coupons()`,`call_tool()` 会抛错。返回中混有 base64 签名的图片 URL,不要用正则硬啃 HTML。

## 付款机制(MCP 不收钱)

**35 个工具中没有任何支付类工具。** 链路:

```
calculate-price(试算) → 用户确认 → create-order(创建,不扣款)
  → 返回 payId / pickupCode / expirePayTime / 支付链接
  → 用户扫码或麦当劳 APP 付款(← 唯一扣款环节,MCP 代办不了)
  → 用户回复「支付完成」→ query-order 回查状态(1待支付→2配餐中→4配送中→6已完成)
```

| 情形 | 是否扣款 |
|---|---|
| 只调 calculate-price / query-* | ❌ 不扣 |
| create-order 后不付款 | ❌ 不扣,订单停在「1 待支付」,到期自动关闭 |
| create-order 且用户自行付款 | ✅ **唯一扣款路径** |

`create-order` 官方 Next 字段原文:「您可以通过支付链接扫码或者打开麦当劳 APP 完成支付,支付完成后请回复『支付完成』或『支付失败』,会帮您查询最新的订单状态」。

**取餐方式 `takeWayCode` 只能取自 `calculate-price` 返回的 `data.takeWayList[].code`**,不可凭空填。

## 隐私红线(违规即取消资格)

`query-order.data.deliveryInfo` 含 `customerNickname` / `mobilePhone` / `deliveryAddress` / `riderNickName` / `riderMobilePhone` / `payId`。
**白名单外字段一律不提取、不落盘、不输出。** 竞赛规则原文:「参赛项目及提交材料中……亦不包含他人个人信息」。

## 禁止周期预测

实测 10 单间隔 1–16 天、中位数 5 天、方差过大;50 单口径同样不稳定。只能陈述「你已 N 天没吃」,禁止输出「你该吃了」类预测。
