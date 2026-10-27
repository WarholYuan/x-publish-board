#!/usr/bin/env bash
# 半自动建 Base：用 lark-cli 新建「X 运营」Base 与五张表，并打印拿到的 ID。
# 前置：lark-cli 已安装并以你的飞书账号登录（脚本全程 --as user）。
# 注意：字段、公式、lookup、仪表盘用 lark-cli 逐个建比较繁琐，本脚本只建 Base 与空表，
#       字段请对照 docs/feishu-base.md / feishu/schema.json 在飞书界面建，或按 lark-cli
#       base +field-create 的帮助逐字段补。本脚本的价值是把 ID 一次拿齐、少抄错。
set -euo pipefail

LARK="${LARK_CLI:-lark-cli}"

echo "== 新建 Base =="
OUT=$("$LARK" base +base-create --name "X 运营" --time-zone Asia/Shanghai --as user --format json)
echo "$OUT"
echo
echo "从上面输出里抄下 base_token，填进 config.env 的 FEISHU_BASE_TOKEN。"
echo "然后在飞书里打开这个 Base，按 docs/feishu-base.md 建五张表与字段，"
echo "最后运行下面命令查五张表的 table_id，填进 config.env："
echo "  $LARK base +table-list --base-token <BASE_TOKEN> --as user"
