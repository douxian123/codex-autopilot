#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HyAPI 死号自动收割器（日志式判据，源侧，免重启，不碰在途流）。

背景（2026-11-17 实测钉死）：
  自建通道 HyAPI 的账号池里会混进「已被上家停用/清零」的账号。请求轮到它们时，
  HyAPI 回 HTTP 200 + 115 字节裸 JSON（不是合法 SSE）：
    {"code":"100003","desc":"账号已被停用","trace_id":"...","data":{}}
  代理把它当空回合 → 502 upstream_empty_response → 客户端「回两句话就断」。

  代理侧已识别 100002/100003 并换号重放（HYAPI_RELOGIN_RETRY_MAX=2），
  但当一次请求连续撞上多个死号，重放耗尽就会如实报错，用户端就会看到断流。
  2026-11-17 05:39 手工禁掉 2 个死号后，05:49 又冒出新的死号 —— 说明必须
  自动、周期性地把死号从池里摘掉。

判据（来自 HyAPI 自己的 data/request-log.jsonl，已在 2000+ 条真实流量上验证）：
  一条失败请求被 HyAPI 记为  status=200, ttfb_ms=0, total_ms<1500,
  credit=0, tokens=-1 。这是「回裸 JSON 错误、没真正开始生成」的指纹。
  实测 65 条 ttfb=0 记录里 59 条命中该指纹；剩下 6 条 total_ms>8s ，是别的
  慢请求，被 total_ms<1500 这道闸排除。

停用判据（防误杀）：
  必须同时满足 ①窗口内死亡次数 >= DEAD_MIN_HITS，且 ②该账号最近的两次记录
  **连续都是死号**。第二条才是关键 ——

  为什么不能只看「窗口里有没有成功」：死号真正的伤害形态是**会话粘性**。
  HyAPI 默认 session_sticky_ttl=30m，一个会话被粘到一个账号后，
  该账号在这中间被上家停用，会话就会**连着**撞它（实测 05:36–05:39 连撞 54 次）。
  这种账号在窗口更早处往往还有成功记录，用「窗口内有无成功」当闸会把它放过去，
  正是我们需要摘掉的那一类。
  反过来，健康账号即便偶发一次裸 JSON（网络抖动），下一次也会恢复成 tokens>0，
  尾部就不会连续两次都是死号，因而不会被误杀。

安全性：
  - 只动 disabled 标志，可用 /admin/accounts/{uid}/enable 完全恢复；
  - 同一窗口里还有任何一次真实成功（tokens>0）的账号一律跳过，不误杀间歇性账号；
  - 幂等：已禁用的账号不重复处理。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import http.client
import re
import socket
import subprocess

from hyapi_secrets import require_env

BASE = os.environ.get("HYAPI_BASE", "http://192.168.1.25:7863").rstrip("/")
ADMIN_TOKEN = require_env("HYAPI_ADMIN_TOKEN")
LOG_URL = BASE + "/admin/logs?limit=2000"
STATE_PATH = os.path.expanduser("~/.codex-proxy/hyapi_reaper_state.json")
RUN_LOG = os.path.expanduser("~/.codex-proxy/logs/deadpool_reap.log")

# 出站网卡绑定（2026-11-17 必修）：
#   本机开着 TUN（utun*，198.18.0.0/15）。默认路由到 192.168.1.25 会被抓进 utun，
#   表现为 TCP 能建连、HTTP 永不返回（实测 curl/SSH 全超时）。代理进程用
#   IP_BOUND_IF 绑到物理网卡 en1 就能直连。这里复刻同一手法，否则收割器会一直
#   超时，等于没装。
_IP_BOUND_IF = 25          # macOS: IPPROTO_IP / IP_BOUND_IF
_BIND_IFACE = os.environ.get("HYAPI_BIND_IFACE", "auto")
_IFACE_CACHE = {"name": None, "ts": 0.0}

# 死号指纹阈值（见文件头：指纹=200/ttfb0/total<1.5s/credit0/tokens-1）
DEAD_TOTAL_MS_MAX = 1500
# 自动停用需要的最少「死」样本数：单条偶发不自动杀，两条及以上且无成功才杀。
DEAD_MIN_HITS = 2

# --- 防批量误杀（2026-11-17 事故后追加）------------------------------------
# 事故经过：上游整体拒绝 / 隧道抖动时，窗口内几乎所有账号都只剩 "dead" 记录，
# 尾部连续死亡判据对全体成立，5 分钟一轮连杀 43 个健康账号（err_total 只有 2，
# last_success 就在几十分钟前）。以下三道闸是硬防线，任何一道触发就整轮不停号。
#
# ① 系统性故障闸门：单窗口被判死号的账号占比过高 / 绝对数过多 → 判为上游或
#    网络整体故障，本轮只记录不停号。
SYSTEMIC_RATIO_THRESHOLD = 0.30
SYSTEMIC_MIN_ABS = 8
# ② 单轮停用上限：即使判据都满足，一轮最多停这么多个，逼人工复核。
MAX_DISABLE_PER_ROUND = 3
# ③ 二次确认：被 HyAPI 自己记为最近一次成功的时间，必须早于窗口起点，
#    否则说明这个账号本轮其实拉通过，不许停。


def _req(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def _log(msg):
    line = "%s\t%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(RUN_LOG), exist_ok=True)
        with open(RUN_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _detect_lan_iface():
    """挑一张带局域网 IPv4 的物理网卡（同 proxy.py 的 _detect_lan_iface）。"""
    now = time.time()
    if now - _IFACE_CACHE["ts"] < 30 and _IFACE_CACHE["name"]:
        return _IFACE_CACHE["name"]
    name = None
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=3).stdout
        cur = None
        for line in out.splitlines():
            m = re.match(r"^([a-zA-Z0-9]+):", line)
            if m:
                cur = m.group(1)
                continue
            if cur and cur.startswith(("en", "bridge")) and name is None:
                if re.search(r"inet (10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", line):
                    name = cur
    except Exception:
        name = None
    _IFACE_CACHE["ts"] = now
    _IFACE_CACHE["name"] = name
    return name


def _iface_index(name):
    try:
        return socket.if_nametoindex(str(name))
    except Exception:
        return 0


def _bound_socket(ifindex, timeout, host, port):
    """建一个先绑网卡、再连接的 TCP socket（顺序很关键：IP_BOUND_IF 必须在
    connect 之前设置，否则会 OSError 49 Can't assign requested address）。
    失败回退普通直连，保证不因绑定问题彻底不可用。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, ifindex)
        s.settimeout(timeout)
    except Exception:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        return socket.create_connection((host, port), timeout)
    try:
        s.connect((host, port))
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return socket.create_connection((host, port), timeout)


class _BoundHTTPConnection(http.client.HTTPConnection):
    """连到局域网主机时把出站 socket 绑到物理网卡，绕开 TUN。"""

    def __init__(self, host, port, ifindex, timeout):
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        self._ifindex = ifindex

    def connect(self):
        if self._ifindex:
            self.sock = _bound_socket(self._ifindex, self.timeout,
                                      self.host, self.port)
        else:
            self.sock = socket.create_connection((self.host, self.port),
                                                 self.timeout)
        if self._tunnel_host:
            self._tunnel()


def _ifindex_for_url(url):
    if _BIND_IFACE.lower() in ("", "direct", "none", "off", "0"):
        return 0
    name = _detect_lan_iface() if _BIND_IFACE.lower() == "auto" else _BIND_IFACE
    return _iface_index(name) if name else 0


def _http_get_json(url, headers=None, timeout=30):
    """带网卡绑定的 GET，返回解析后的 JSON。绕开 urllib（它绑定不了网卡）。"""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    ifindex = _ifindex_for_url(url)
    conn = _BoundHTTPConnection(u.hostname, u.port or 80, ifindex, timeout)
    try:
        conn.request("GET", u.path + (("?" + u.query) if u.query else ""),
                     headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body.decode("utf-8", "ignore") or "{}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _http_post_json(url, payload=None, headers=None, timeout=15):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    ifindex = _ifindex_for_url(url)
    conn = _BoundHTTPConnection(u.hostname, u.port or 80, ifindex, timeout)
    try:
        data = json.dumps(payload or {}).encode()
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        conn.request("POST", u.path + (("?" + u.query) if u.query else ""),
                     body=data, headers=hdrs)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fetch_logs():
    _status, data = _http_get_json(
        LOG_URL, headers={"Authorization": "Bearer " + ADMIN_TOKEN}, timeout=30)
    items = data.get("items")
    if items is None:
        items = data.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
    return items


def is_dead(rec):
    """bridge: HyAPI 回裸 JSON 错误时留下的记录指纹。"""
    try:
        return (int(rec.get("status") or 0) == 200
                and int(rec.get("ttfb_ms") or 0) == 0
                and int(rec.get("total_ms") or 0) < DEAD_TOTAL_MS_MAX
                and int(rec.get("credit") or 0) == 0
                and int(rec.get("tokens") or 0) < 0)
    except Exception:
        return False


def is_ok(rec):
    try:
        return int(rec.get("status") or 0) == 200 and int(rec.get("tokens") or 0) > 0
    except Exception:
        return False


def disable(uid, reason):
    try:
        status = _http_post_json(
            BASE + "/admin/accounts/" + uid + "/disable?token=" + ADMIN_TOKEN,
            {"reason": reason}, timeout=15)
        return status in (200, 204)
    except http.client.HTTPException as e:
        _log("disable %s HTTP 异常: %s" % (uid, e))
    except Exception as e:
        _log("disable %s 失败: %s" % (uid, type(e).__name__))
    return False


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        st.setdefault("disabled", [])
        return st
    except Exception:
        return {"disabled": []}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        _log("写 state 失败: %s: %s" % (type(e).__name__, e))


def run(dry_run=False):
    items = fetch_logs()
    if not items:
        _log("日志为空，跳过（窗口无数据）")
        return 0
    window = "%s .. %s" % (items[0].get("at", "")[:19], items[-1].get("at", "")[:19])
    # 按 seq 升序（日志接口返回即为时间升序，这里再兜一层），得到每 uid 的有序事件流
    try:
        items = sorted(items, key=lambda r: int(r.get("seq") or 0))
    except Exception:
        pass
    events = {}
    for rec in items:
        uid = rec.get("uid")
        if not uid:
            continue
        if is_dead(rec):
            events.setdefault(uid, []).append("dead")
        elif is_ok(rec):
            events.setdefault(uid, []).append("ok")
    st = load_state()
    already = set(st.get("disabled", []))
    newly = []
    skipped = []
    dead_uids = []
    active_uids = [u for u, evs in events.items() if evs]
    candidates = []
    for uid, evs in events.items():
        dead_n = evs.count("dead")
        if dead_n == 0:
            continue
        dead_uids.append(uid)
        if uid in already:
            continue
        if dead_n < DEAD_MIN_HITS:
            skipped.append((uid, dead_n, "死亡次数不足(%d)" % dead_n))
            continue
        if not (len(evs) >= 2 and evs[-1] == "dead" and evs[-2] == "dead"):
            skipped.append((uid, dead_n, "尾部未连续死亡(尾部=%s)" % ",".join(evs[-3:])))
            continue
        candidates.append((uid, dead_n))

    # ① 系统性故障闸门：候选死号占比过高 / 绝对数过多 = 上游或网络整体故障。
    #    真实坏号是零星几个，不会一次出现七八个以上。
    ratio = (len(candidates) / float(len(active_uids))) if active_uids else 0.0
    if (len(candidates) >= SYSTEMIC_MIN_ABS
            or (len(active_uids) >= 4 and ratio >= SYSTEMIC_RATIO_THRESHOLD)):
        _log("【系统性故障闸门】候选死号 %d 个 / 有流量账号 %d 个（占比 %.0f%%），"
             "符合上游整体故障特征，本轮不停号（零破坏）"
             % (len(candidates), len(active_uids), ratio * 100))
        for uid, dead_n in candidates:
            _log("  观察到 uid=%s (死%d) —— 仅记录，未停用" % (uid, dead_n))
        st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["last_window"] = window
        st["last_dead_uids"] = sorted(dead_uids)
        st["last_systemic_hold"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not dry_run:
            save_state(st)
        return 0

    # ③ 二次确认：账号最近一次成功若落在本窗口内，说明它其实通过，不许停。
    live_ok_uids = set()
    ok = False
    try:
        win_start = str(items[0].get("at", ""))[:19]
        _s, acc = _http_get_json(
            BASE + "/admin/accounts",
            headers={"Authorization": "Bearer " + ADMIN_TOKEN}, timeout=20)
        for a in (acc.get("accounts") or []):
            ls = str(a.get("last_success") or "")[:19]
            if ls and win_start and ls >= win_start:
                live_ok_uids.add(a.get("uid"))
        ok = True
    except Exception as e:
        _log("二次确认取账号状态失败(%s)，本轮保守不停号" % type(e).__name__)
    if not ok:
        st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["last_window"] = window
        st["last_dead_uids"] = sorted(dead_uids)
        if not dry_run:
            save_state(st)
        return 0

    stop_budget = MAX_DISABLE_PER_ROUND
    for uid, dead_n in candidates:
        if uid in live_ok_uids:
            skipped.append((uid, dead_n, "窗口内仍有成功记录(二次确认否决)"))
            continue
        if stop_budget <= 0:
            skipped.append((uid, dead_n, "本轮停号配额用尽(上限%d)" % MAX_DISABLE_PER_ROUND))
            continue
        if dry_run:
            _log("[dry-run] 应停用死号 uid=%s (死%d, 尾部连续死亡)" % (uid, dead_n))
            newly.append(uid)
            stop_budget -= 1
            continue
        if disable(uid, "deadpool reap: 200/ttfb0/credit0/tokens-1 x%d tail-consecutive" % dead_n):
            already.add(uid)
            newly.append(uid)
            stop_budget -= 1
            _log("已停用死号 uid=%s (死%d, 尾部连续死亡)" % (uid, dead_n))
    st["disabled"] = sorted(already)
    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["last_window"] = window
    st["last_dead_uids"] = sorted(dead_uids)
    if not dry_run:
        save_state(st)
    _log("窗口 %s | 日志%d条 | 死号uid%d个 | 新停用%d个 | 跳过%d个 | 累计停用%d个"
         % (window, len(items), len(dead_uids), len(newly), len(skipped), len(already)))
    for uid, _n, why in skipped:
        _log("  跳过 uid=%s (%s)" % (uid, why))
    return len(newly)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    args = ap.parse_args()
    if args.loop:
        while True:
            try:
                run(args.dry_run)
            except Exception as e:
                _log("轮次异常: %s: %s" % (type(e).__name__, e))
            time.sleep(args.interval)
    else:
        try:
            run(args.dry_run)
        except Exception as e:
            _log("异常: %s: %s" % (type(e).__name__, e))
            sys.exit(1)


if __name__ == "__main__":
    main()
