#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hyapi_burner.py —— HyAPI 当日签到积分「收盘燃烧器」

为什么需要（2026-11-15 核实）：
  教程原文：「这 5 万积分当天必须用完不然作废，但是第二天签到还可以继续领 5 万」
  → 签到积分当日清零，跑不完就是白扔。

实测标定（2026-11-15）：
  1 积分 ≈ 517 token（points_consumed=6 / 3101 token）
  单并发吞吐 ≈ 282 token/s；20 并发 ≈ 5,640 token/s
  → 5.6 万积分约 0.29 亿 token，20 并发需 1.5~3 小时

安全设计（这是替你花额度，所以处处设闸）：
  1. 只在 22:00-23:55 窗口内跑；窗口外启动直接退出
  2. 每轮都重新读 /admin/accounts，剩额低于阈值立即收工
  3. 并发默认 8（不是 20），给你的本职工作留足算力
  4. 只打低价值模型（默认 deepseek-v4-flash-1017）
  5. 硬上限：最多跑 MAX_CYCLES 轮 / 最多花 MAX_POINTS 积分
  6. dry-run 模式可先空跑看看会做什么

用法：
  python3 hyapi_burner.py --dry-run     # 只看不烧
  python3 hyapi_burner.py               # 真烧
  python3 hyapi_burner.py --target 5000 # 自定义剩额阈值（低于此值停）
"""
import json, os, sys, time, random, datetime, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from hyapi_secrets import require_env

BASE = os.environ.get("HYAPI_BASE", "http://192.168.1.25:7863")
ADMIN = require_env("HYAPI_ADMIN_TOKEN")
APIKEY = require_env("HYAPI_API_KEY")
HOME = os.path.expanduser("~")
LOG = os.path.join(HOME, ".codex-proxy", "logs", "burner.log")

# ── 安全闸门（都可被环境变量覆盖）────────────────────────────
DEFAULT_TARGET_POINTS = int(os.environ.get("BURN_TARGET_POINTS", "8000"))
MAX_CONCURRENCY = int(os.environ.get("BURN_CONCURRENCY", "8"))
MAX_CYCLES = int(os.environ.get("BURN_MAX_CYCLES", "600"))
MAX_POINTS = int(os.environ.get("BURN_MAX_POINTS", "80000"))
BURN_MODEL = os.environ.get("BURN_MODEL", "loomy/deepseek-v4-flash-1017")
WINDOW_START = int(os.environ.get("BURN_WINDOW_START", "22"))
WINDOW_END_HHMM = os.environ.get("BURN_WINDOW_END", "23:55")

_lock = threading.Lock()
_total_pts = [0]
_total_req = [0]
_total_err = [0]


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# 2026-11-15 实测：launchd 环境下访问局域网会走系统代理并 60s 超时
# (手动跑没事、被 launchd 拉起来就 Errno 60)。显式绕过代理 + 重试 3 次。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url, headers, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise last


def get_free():
    """返回 (free_points, credits, n_accounts)。失败返回 (None,None,0)。"""
    try:
        d = _http_json(BASE + "/admin/accounts",
                       {"Authorization": "Bearer " + ADMIN})
        accs = d.get("accounts") or []
        free = sum(int(a.get("free_quota") or 0) for a in accs)
        cred = sum(int(a.get("credits") or 0) for a in accs)
        return free, cred, len(accs)
    except Exception as e:
        log(f"读额度失败: {type(e).__name__}: {e}")
        return None, None, 0


def in_window():
    now = datetime.datetime.now()
    end_h, end_m = (int(x) for x in WINDOW_END_HHMM.split(":"))
    start = now.replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now <= end


def burn_one(i):
    """发一次低价值长输出请求。返回 points_consumed。"""
    n = random.randint(1000000, 9999999)
    topic = random.choice([
        "分布式共识算法的完整实现细节", "操作系统调度器的设计权衡",
        "数据库索引的底层数据结构", "编译器中端优化的常见 pass",
        "TCP 拥塞控制的发展史", "垃圾回收器的分代策略",
    ])
    body = {
        "model": BURN_MODEL,
        "messages": [{"role": "user", "content":
                      f"编号{n}。请就「{topic}」写一份详尽的技术说明，"
                      f"要求至少 3000 字，分 8 个小节，每节要有具体例子，"
                      f"禁止省略、禁止写『略』。必须完整写到底。"}],
        "max_tokens": 3072,
        "stream": False,
    }
    try:
        req = urllib.request.Request(
            BASE + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + APIKEY,
                     "Content-Type": "application/json"})
        with _OPENER.open(req, timeout=300) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        u = d.get("usage") or {}
        pts = int(u.get("points_consumed") or 0)
        with _lock:
            _total_pts[0] += pts
            _total_req[0] += 1
        return pts
    except Exception as e:
        with _lock:
            _total_err[0] += 1
        return 0


def main():
    dry = "--dry-run" in sys.argv
    target = DEFAULT_TARGET_POINTS
    for a in sys.argv:
        if a.startswith("--target="):
            target = int(a.split("=", 1)[1])

    free, cred, n = get_free()
    if free is None:
        log("无法读取额度，退出")
        return 2

    log(f"启动: free={free:,} credits={cred:,} 账号={n} "
        f"目标阈值={target:,} 并发={MAX_CONCURRENCY} 模型={BURN_MODEL} "
        f"窗口={WINDOW_START}:00-{WINDOW_END_HHMM} dry_run={dry}")

    if free <= target:
        log(f"✅ 剩余 {free:,} 已低于阈值 {target:,}，无需燃烧，退出")
        return 0

    if not in_window():
        log(f"⏰ 当前不在燃烧窗口（{WINDOW_START}:00-{WINDOW_END_HHMM}），退出")
        return 0

    if dry:
        est = (free - target)
        log(f"[DRY-RUN] 将尝试烧掉约 {est:,} 积分 "
            f"(≈{est*517/1e8:.2f} 亿 token)，"
            f"预估需要 {est*517/(282*MAX_CONCURRENCY)/60:.0f} 分钟。不实际发送请求。")
        return 0

    cycles = 0
    while cycles < MAX_CYCLES:
        if not in_window():
            log("⏰ 已到窗口末尾，收工")
            break
        free, _, _ = get_free()
        if free is None:
            break
        if free <= target:
            log(f"✅ 已降到 {free:,}（阈值 {target:,}），收工")
            break
        if _total_pts[0] >= MAX_POINTS:
            log(f"🛑 已达积分硬上限 {MAX_POINTS:,}，收工")
            break

        batch = min(MAX_CONCURRENCY, 8)
        with ThreadPoolExecutor(max_workers=batch) as ex:
            futs = [ex.submit(burn_one, i) for i in range(batch)]
            for f in as_completed(futs):
                f.result()
        cycles += 1
        if cycles % 5 == 0:
            log(f"进度: 轮次={cycles} 请求={_total_req[0]} "
                f"错误={_total_err[0]} 已烧={_total_pts[0]:,} 剩={free:,}")

    log(f"结束: 轮次={cycles} 请求={_total_req[0]} 错误={_total_err[0]} "
        f"共烧={_total_pts[0]:,} 积分")
    return 0


if __name__ == "__main__":
    sys.exit(main())
