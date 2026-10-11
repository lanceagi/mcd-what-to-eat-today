#!/bin/bash
# Skill 一键测试:11 组命令,逐条实跑
# 用法: bash run_all_tests.sh
# 🔴 全程只读 + 试算,不调用 create-order,不产生任何费用。
#    第 9 组的 test_scenarios.py 把 call_tool 包了 WriteBlocked 代理,
#    命中 7 个写操作直接抛错,没有绕过开关。

cd "$(dirname "$0")" || exit 1
PY=/Users/lancerosm5/.workbuddy/binaries/python/versions/3.13.12/bin/python3

line() { printf '\n\033[1m%s\033[0m\n' "════ $* ════"; }
fail=0

line "1/11 环境自检(无网络,秒出)"
$PY scripts/selftest.py || fail=1

line "2/11 流程不变量(离网,假 API;含 D1 语音不变量)"
$PY scripts/test_flow.py || fail=1

line "3/11 门店迎宾(读画像,纯本地;A4 门禁)"
$PY scripts/greet.py || fail=1

line "4/11 语音层(9 项自检 + S0→S8 全流程演示,离网)"
$PY scripts/voice.py --selfcheck || fail=1
$PY scripts/voice.py --flow-demo || fail=1

line "5/11 健身场景(真实拉菜单 + 营养库)"
$PY scripts/recommend.py --scene fitness

line "6/11 省钱场景(真实拉卡包券)"
$PY scripts/recommend.py --scene saving

line "7/11 赶时间/随缘/尝新(阶段 A C 组,真实菜单)"
$PY scripts/recommend.py --scene hurry --limit 3
$PY scripts/recommend.py --scene random
$PY scripts/recommend.py --scene try_new --limit 2

line "8/11 麦乐送外送试算(真实地址 → 门店 → beCode → calculate-price)"
$PY scripts/order_builder.py --channel-hint "送到家" --code 902060 --code 903050 --force-ice

line "9/11 四渠道双跑验证(写操作已封锁,各跑两遍)"
$PY scripts/test_scenarios.py || fail=1

line "10/11 画像三层重建(真实订单 → habits/taste/avoid)"
$PY scripts/fetch_profile.py || fail=1

line "11/11 全量自检(含真实 MCP,约 20 次只读请求;30 项)"
$PY scripts/selftest.py --full || fail=1

printf '\n'
if [ "$fail" = "0" ]; then
  printf '\033[1;32m全部跑完。以上没有任何下单动作——create-order 全程未调用。\033[0m\n'
else
  printf '\033[1;31m有测试组失败,见上。\033[0m\n'
fi
exit $fail
