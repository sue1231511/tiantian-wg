#!/bin/bash
# 同一容器内拉起两个独立进程：
#   main.py            —— 消息进程（Process A）：QQ/TG/微信/rikkahub 实时收发 + HTTP
#   background_main.py —— 后台进程（Process B）：主动思考/提醒/凌晨总结/自由活动/全平台压缩+摘要整理
# 两者完全独立的操作系统进程，不共享内存，只共享 Supabase。
#
# 任意一个异常退出，就把另一个也杀掉、整个容器退出（exit code 透传），
# 让 Zeabur 的重启策略接管，两个进程作为一个整体一起重启——不会出现
# "消息进程还在跑、后台进程早就死了没人知道"这种半死不活的状态。
set -e

python background_main.py &
BG_PID=$!

python main.py &
MAIN_PID=$!

wait -n "$BG_PID" "$MAIN_PID"
EXIT_CODE=$?

kill "$BG_PID" "$MAIN_PID" 2>/dev/null || true
wait "$BG_PID" "$MAIN_PID" 2>/dev/null || true

exit "$EXIT_CODE"
