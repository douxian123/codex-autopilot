#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hyapi_freewatch.py —— HyAPI 自建通道「当日签到积分」收盘预警

背景（2026-11-15 核实，教程原文）：
  「每份套餐每天线路都会自动签到，一共可以获得 5 万积分…这 5 万积分当天
    必须用完不然作废，但是第二天签到还可以继续领 5 万」
  → 签到积分是**当日清零**的。跑不完 = 白扔。

做法：
  - 读 /admin/accounts，把每个账号的 credits / free_quota 聚合；
  - 跟**今天早上的基线**比，算出今天还剩多少签到积分没烧；
  - 超阈值就通过 macOS 通知 + 写日志提醒；--quiet 只写日志不弹通知。

用法：
  python3 hyapi_freewatch.py            # 检查并提醒
  python3 hyapi_freewatch.py --quiet    # 只记录不弹窗
  python3 hyapi_freewatch.py --json     # 输出机器可读 JSON
"""
import json, os, sys, subprocess, datetime, urllib.request

from hyapi_secrets import require_env

BASE = os.environ.get("HYAPI_BASE", "http://192.168.1.25:7863")
TOKEN = require_env("HYAPI_ADMIN_TOKEN")
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".codex-proxy", "state")
LOG = os.path.join(HOME, ".codex-proxy", "logs", "freewatch.log")
BASELINE = os.path.join(STATE_DIR, "freewatch_baseline.json")

# 1 积分 ≈ 10000 token（教程：5 万积分 ≈ 5 亿 token）
TOKENS_PER_POINT = 10000
# 晚上提醒阈值：剩余积分超过这个数就提醒
WARN_POINTS = int(os.environ.get("FREEWATCH_WARN_POINTS", "20000"))


# 2026-11-15：launchd 环境下访问局域网会被系统代理拦住 → Errno 60 超时。
# 显式绕过代理 + 重试，保证被 launchd 拉起来也能正常工作。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(path, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                BASE + path, headers={"Authorization": "Bearer " + TOKEN})
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if i < retries - 1:
                import time as _t
                _t.sleep(2 * (i + 1))
    raise last


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, body):
    """macOS 通知。失败不抛（headless 环境无通知中心）。"""
    try:
        s = body.replace('"', '\\"')
        t = title.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{s}" with title "{t}" sound name "Glass"'],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_baseline():
    try:
        with open(BASELINE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_baseline(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    quiet = "--quiet" in sys.argv
    as_json = "--json" in sys.argv

    try:
        d = fetch("/admin/accounts")
    except Exception as e:
        log(f"读取 /admin/accounts 失败: {type(e).__name__}: {e}")
        return 2

    accs = d.get("accounts") or []
    if not accs:
        log("账号列表为空，跳过")
        return 0

    credits = sum(int(a.get("credits") or 0) for a in accs)
    free = sum(int(a.get("free_quota") or 0) for a in accs)
    dead = [a for a in accs if int(a.get("credits") or 0) <= 0]
    cooling = [a for a in accs if a.get("cooling")]
    disabled = [a for a in accs if a.get("disabled")]

    today = datetime.date.today().isoformat()
    base = load_baseline()
    # 基线规则：
    #  1) 换天 → 重建基线（新一天签到后又是满额）
    #  2) 同一天若 free 反而变**大**了（当天又签到/补了额度）→ 抬高基线
    #  否则基线保持不变，这样「今日已烧」才是真实累计消耗。
    prev_first = int(base.get("free_at_first_seen", -1)) if base else -1
    need_new = (not base) or base.get("date") != today
    need_raise = (not need_new) and free > prev_first
    if need_new or need_raise:
        base = {"date": today, "free_at_first_seen": free,
                "credits_at_first_seen": credits,
                "first_seen": datetime.datetime.now().isoformat(timespec="seconds")}
        save_baseline(base)
        log(f"{'新建' if need_new else '抬高'}今日基线 date={today} "
            f"free={free:,} credits={credits:,}")

    burned_today = max(0, int(base.get("free_at_first_seen", free)) - free)
    remain = free
    remain_tok = remain * TOKENS_PER_POINT

    if as_json:
        print(json.dumps({
            "date": today, "accounts": len(accs),
            "credits": credits, "free": free,
            "burned_today": burned_today,
            "remain_points": remain, "remain_tokens_est": remain_tok,
            "dead_accounts": len(dead),
            "cooling": len(cooling), "disabled": len(disabled),
            "warn_threshold": WARN_POINTS,
        }, ensure_ascii=False, indent=2))
        return 0

    log(f"账号={len(accs)} credits={credits:,} free={free:,} "
        f"今日已烧={burned_today:,} 死号={len(dead)} 冷却={len(cooling)} 禁用={len(disabled)}")

    if dead:
        names = ",".join(a.get("nickname", "?") for a in dead[:5])
        log(f"⚠️ 有 {len(dead)} 个账号余额为 0: {names}")
        if not quiet:
            notify("HyAPI 账号余额告急",
                   f"{len(dead)} 个账号积分为 0，可能拖慢并发")

    if remain >= WARN_POINTS:
        msg = (f"今天还剩 {remain:,} 签到积分（约 {remain_tok/1e8:.1f} 亿 token）"
               f"没用完，今晚 24:00 作废")
        log("💡 " + msg)
        if not quiet:
            notify("HyAPI 签到积分将作废", msg)
    else:
        log(f"✅ 今日签到积分基本用尽（剩 {remain:,} < 阈值 {WARN_POINTS:,}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
