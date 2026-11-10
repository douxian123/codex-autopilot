#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Codex 自动挡（以及它依赖的 Codex 个人环境）搬到另一台机器。

背景：源机磁盘常年 98% 满（可用 4.2Gi），而 ~/.codex 光 thread_history 就 7G。
因此本工具**不在源机落任何中间产物**：打好的东西直接从 tar 流进 HTTP 响应，
目标机边收边解，源机额外磁盘占用为 0。

子命令：
  plan                只算账：要传哪些、各多大、总共多大、哪些再生件不传。
  pack [-o 文件|-]    打包；`-o -` 是流式写 stdout（可管道）。
  serve [--port N]    起临时 HTTP，把 tar 流挂上去，目标机 curl 直拉。
  remap [--to HOME]   目标机落地后改写写死的绝对路径（默认预演，--go 才改）。

搬的是「环境」，不含项目代码 —— 项目代码两台机器都从 git 拿。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HOME = Path.home()

SPEC: list[tuple[str, str, str]] = [
    (".codex/config.toml", "core", "Codex 主配置（模型/provider/hooks）"),
    (".codex/auth.json", "core", "登录凭据"),
    (".agents/marketplace.json", "core", "个人插件市场清单"),
    (".agents/plugins", "core", "市场注册信息"),
    (".agents/skills", "core", "用户级 skills"),
    (".codex/skills", "core", "Codex skills"),
    (".codex/plugins/cache/personal", "core", "个人插件缓存（自动挡运行的那份）"),
    ("plugins/codex-autopilot/install-state.json", "core", "自动挡安装状态（版本指针）"),
    (".codex-autopilot", "core", "自动挡运行态与账本（control/safety/state）"),
    ("plugins/codex-autopilot/releases", "runtime", "自动挡历史版本包"),
    ("plugins/codex-autopilot/plugin", "runtime", "插件实体（skill+面板）"),
    ("plugins/codex-autopilot/ops", "runtime", "运行时 ops/ 代码"),
    (".codex/packages", "runtime", "Codex 依赖包"),
    (".codex/hooks", "runtime", "hooks 脚本"),
    (".codex/thread_history_1.sqlite", "regen", "会话历史主库 ~7G，再生件"),
    (".codex/sessions", "regen", "原始 session ~5.6G，再生件"),
    (".codex/archived_sessions", "regen", "归档会话"),
    (".codex/logs_2.sqlite", "regen", "本地日志库"),
    (".codex/state_5.sqlite", "regen", "本地状态库"),
    (".codex/visualizations", "regen", "可视化缓存"),
    (".codex/computer-use", "regen", "computer-use 缓存"),
]

EXCLUDE_DIRS = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "build", "dist", ".next", ".turbo", "DerivedData",
]


def _du(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += (Path(root) / f).lstat().st_size
            except OSError:
                pass
    return total


def _human(n: float) -> str:
    if n < 1024:
        return f"{int(n)}B"
    for unit in ("K", "M", "G", "T"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}T"


def selected(tier: str):
    if tier == "all":
        want = {"core", "runtime", "regen"}
    elif tier == "core":
        want = {"core"}
    else:
        want = {"core", "runtime"}
    return [(p, t, d) for p, t, d in SPEC if t in want]


def _entries(args) -> list[str]:
    return [rel for rel, _t, _d in selected(args.tier) if (HOME / rel).exists()]


def cmd_plan(args) -> int:
    print(f"源机 $HOME = {HOME}\n")
    print(f"{'处理':<8}{'大小':>10}  路径")
    print("-" * 78)
    keep_tiers = {t for _p, t, _d in selected(args.tier)}
    total_keep = total_skip = 0
    for rel, tier, desc in SPEC:
        p = HOME / rel
        if not p.exists():
            continue
        size = _du(p)
        if tier in keep_tiers:
            total_keep += size
            mark = "传"
        else:
            mark = "跳过"
            if tier == "regen":
                total_skip += size
        print(f"{mark:<8}{_human(size):>10}  {rel}   -- {desc}")
    print("-" * 78)
    print(f"要传（tier={args.tier}）：约 {_human(total_keep)}")
    if total_skip:
        print(f"再生件省掉：约 {_human(total_skip)}")
    print("\n传输：`serve` 起临时 HTTP，目标机一条 curl 拉走，源机不落盘。")
    return 0


def _tar_cmd(entries, output: str) -> list[str]:
    cmd = ["tar", "-czf", output, "-C", str(HOME)]
    for d in EXCLUDE_DIRS:
        cmd += ["--exclude", d]
    cmd += entries
    return cmd


def cmd_pack(args) -> int:
    entries = _entries(args)
    if not entries:
        print("没有可打包的条目。", file=sys.stderr)
        return 1
    out = args.output
    if out != "-":
        free = shutil.disk_usage(HOME).free
        need = sum(_du(HOME / e) for e in entries)
        if need * 0.6 > free:
            print(f"x 磁盘不够：可用 {_human(free)}，粗估至少需要 "
                  f"{_human(need * 0.6)}。改用 `-o -` 流式传，别落盘。",
                  file=sys.stderr)
            return 2
    cmd = _tar_cmd(entries, out)
    print(f"打包 {len(entries)} 项 -> {out}（排除 {', '.join(EXCLUDE_DIRS)}）",
          file=sys.stderr)
    if out == "-":
        os.execvp(cmd[0], cmd)
    return subprocess.call(cmd)


SERVE_NAME = "codex-portable.tar.gz"


def _stream_tar_to(handler, entries) -> None:
    proc = subprocess.Popen(_tar_cmd(entries, "-"), stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        proc.terminate()


def _lan_ip() -> str:
    """挑一个目标机真能连上的 LAN IPv4。

    不能用「UDP connect 再问 socket 要地址」那种常见写法：这台机器跑着代理，
    默认路由落在 utun 上，那种写法会返回 198.18.0.0/15 这段代理 fake-IP
    （实测就返回了 198.18.0.1），目标机按这个地址根本连不过来。
    所以这里显式枚举物理接口，按「私有网段 > 默认路由接口 > en*」打分。
    """
    default_ifaces: list[str] = []
    try:
        out = subprocess.run(["route", "-n", "get", "default"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.strip().startswith("interface:"):
                default_ifaces.append(line.split(":", 1)[1].strip())
    except OSError:
        pass

    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    except OSError:
        out = ""

    cands: list[tuple[str, str]] = []
    iface = ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            cands.append((iface, m.group(1)))

    def score(item: tuple[str, str]) -> int:
        name, ip = item
        s = 0
        if ip.startswith("192.168.") or ip.startswith("10.") or \
           re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            s += 4                                    # 真私有网段
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            s -= 10                                   # 代理 fake-IP 段
        if name in default_ifaces:
            s += 2
        if name.startswith("en"):
            s += 1
        return s

    cands = [c for c in cands if not c[1].startswith("127.")]
    if cands:
        cands.sort(key=score, reverse=True)
        if score(cands[0]) > 0:
            return cands[0][1]
    return "127.0.0.1"


def cmd_serve(args) -> int:
    import http.server
    import socketserver

    entries = _entries(args)
    if not entries:
        print("没有可传的条目。", file=sys.stderr)
        return 1
    ip = args.bind or _lan_ip()
    port = args.port

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path.rstrip("/") not in ("", f"/{SERVE_NAME}"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{SERVE_NAME}"')
            self.send_header("Connection", "close")
            self.end_headers()
            print(f"  [开始拉取] {self.address_string()}")
            _stream_tar_to(self, entries)
            print(f"  [完成] {self.address_string()}")

        def log_message(self, fmt, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as srv:
        url = f"http://{ip}:{port}/{SERVE_NAME}"
        print(f"已在 {ip}:{port} 就绪，提供 {len(entries)} 项。Ctrl-C 结束。\n")
        print("目标机上执行：")
        print(f'  curl -fL {url} | tar -xzf - -C "$HOME"')
        print()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


REMAP_GLOBS = [
    ".codex/plugins/cache/personal/*/*/.mcp.json",
    "plugins/codex-autopilot/install-state.json",
    "Library/LaunchAgents/*.plist",
]


HOME_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")


def _scan(args):
    """扫出「这次要改哪些文件」以及每个文件里的候选源前缀计数。

    注意 from-home 不能默认成「当前机器的 HOME」：本工具是在目标机上跑的，
    而文件里写死的是**源机**的路径。所以不给 --from-home 时，从文件内容里
    自动认出出现最多的 /Users/<name> 那一个作为源前缀。
    """
    counts: dict[str, int] = {}
    files: list[tuple[Path, str]] = []
    for pat in REMAP_GLOBS:
        for p in sorted(HOME.glob(pat)):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files.append((p, txt))
            for m in HOME_RE.findall(txt):
                counts[m] = counts.get(m, 0) + 1
    return files, counts


def _pick_source(counts: dict[str, int], explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def cmd_remap(args) -> int:
    files, counts = _scan(args)
    src = _pick_source(counts, args.from_home)
    dst = (args.to_home or str(HOME)).rstrip("/")
    if counts and len(counts) > 1:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        print("文件中出现过的 /Users/<name> 前缀（按出现次数）：")
        for name, n in top:
            flag = "  ← 采用" if name == src else ""
            print(f"  {n:>4}  {name}{flag}")
        print()
    if not src:
        print("没有在任何插件/plist 文件里发现 /Users/<name> 形式的绝对路径。")
        return 0
    hits = [(p, txt.count(src)) for p, txt in files if src in txt]
    if not hits:
        print(f"没有文件引用 {src}。")
        if dst == src:
            print(f"目标 $HOME 与源相同（{src}），无需改写。")
        return 0
    print(f"{'命中':>4}  文件")
    print("-" * 78)
    for p, n in hits:
        print(f"{n:>4}  {p}")
    print("-" * 78)
    total = sum(n for _p, n in hits)
    print(f"共 {len(hits)} 个文件、{total} 处引用 {src}")
    if dst == src:
        print(f"目标 $HOME 与源相同（{src}），无需改写。")
        return 0
    if not args.go:
        print(f"\n预演模式。要真的改成 {dst}，加 --go：")
        print(f"  python3 {Path(__file__).name} remap --to {dst} --go")
        return 0
    for p, _n in hits:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        p.write_text(txt.replace(src, dst), encoding="utf-8")
    print(f"\n已改写 {len(hits)} 个文件：{src} -> {dst}")
    print("接着重新加载 launchd：launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<plist>")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codex 自动挡跨机移植（零落盘）")
    ap.add_argument("--tier", default="standard",
                    choices=["core", "standard", "all"],
                    help="core=只带必需；standard=+运行时（默认）；all=含再生件")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="只算账，不落任何东西")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("pack", help="打包（-o - 为流式 stdout）")
    p.add_argument("-o", "--output", default="-")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("serve", help="起临时 HTTP 供目标机拉取")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--bind", default="")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("remap", help="目标机落地后改写写死的绝对路径")
    p.add_argument("--from-home", default="",
                   help="源机 HOME 前缀；默认从文件内容自动识别")
    p.add_argument("--to", dest="to_home", default=str(HOME))
    p.add_argument("--go", action="store_true")
    p.set_defaults(func=cmd_remap)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
