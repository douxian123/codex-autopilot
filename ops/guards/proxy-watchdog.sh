#!/bin/bash
# Codex 代理看门狗(fail-open · 只兜底)
#
# 2026-10-28 事故与修正:
#   旧版本只做「15721 没监听 → nohup python3 proxy.py start」。
#   watchdog 自己是 launchd 的 StartInterval job,跑完即退出,
#   launchd 会回收该 job 的子进程树 —— 包括它刚 nohup 起来的代理。
#   于是代理被 SIGKILL → 下一次看门狗再拉起 → 每 60 秒断一次流,
#   Codex 侧表现为 "stream disconnected before completion: error sending request"。
#   修法:代理已改由 ai.danta.codex-proxy(KeepAlive)直接托管。
#   看门狗现在只做一件事:如果那个 launchd job 没在跑、端口也没人听,就把它 kickstart 回来。
#   绝不 nohup 起子进程,绝不再产生「退出即被杀」的孤儿。
#
# 关掉看门狗:touch ~/.codex-proxy/WATCHDOG_OFF
# 关掉整个代理:launchctl bootout gui/$(id -u)/ai.danta.codex-proxy

export PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin

PROXY_JOB="gui/$(id -u)/ai.danta.codex-proxy"
STATE="$HOME/.codex-proxy/watchdog.log"

[ -f "$HOME/.codex-proxy/WATCHDOG_OFF" ] && exit 0

port_alive() { lsof -nP -iTCP:15721 -sTCP:LISTEN >/dev/null 2>&1; }
job_loaded() { launchctl print "$PROXY_JOB" >/dev/null 2>&1; }

port_alive && exit 0                      # 正常 → 静默退出

# 端口没在听:先看 launchd job 是否还加载着。加载着就直接 kickstart(它会自己起进程)。
if job_loaded; then
    launchctl kickstart -k "$PROXY_JOB" >/dev/null 2>&1
    sleep 3
    if port_alive; then
        echo "$(date '+%F %T') 端口无监听,已 kickstart launchd job,恢复成功" >> "$STATE"
        exit 0
    fi
    echo "$(date '+%F %T') 🚨 端口无监听,kickstart 后仍未恢复(查 ~/.codex-proxy/proxy.log)" >> "$STATE"
    exit 0
fi

# job 根本没加载(被 bootout 过 / plist 丢了)→ 重新加载。fail-open:失败只记日志。
PLIST="$HOME/Library/LaunchAgents/ai.danta.codex-proxy.plist"
if [ -f "$PLIST" ]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1
    sleep 3
    if port_alive; then
        echo "$(date '+%F %T') launchd job 未加载,已 bootstrap 并恢复" >> "$STATE"
        exit 0
    fi
fi

echo "$(date '+%F %T') 🚨 代理挂了且自动拉起失败(15721 无监听,plist=${PLIST})" >> "$STATE"
exit 0
