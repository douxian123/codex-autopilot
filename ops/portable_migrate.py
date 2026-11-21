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
import stat
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
    ("plugins/codex-autopilot/releases", "runtime", "自动挡历史版本包（含 ops/skills/ui）"),
    ("plugins/codex-autopilot/build", "regen", "Companion 编译产物，可重跑 build.sh 生成"),
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

# 工具本体会被额外打进包里（放在目标机的 ~/.codex-autopilot/portable_migrate.py）。
# 为什么必须带它：落地后第一件事就是 remap（本机实测要改 6.8 万处写死的绝对路径），
# 而这个工具还没随 releases/ 发版——不带过去，目标机上就没有 remap 可跑。
SELF_PATH_IN_TAR = ".codex-autopilot/portable_migrate.py"

EXCLUDE_DIRS = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "build", "dist", ".next", ".turbo", "DerivedData",
]

# 按「相对 $HOME 的路径前缀」排除的再生件。
#
# .codex-autopilot 整个挂在 core 档，但它里面塞着几百 MB 纯运行历史：exec 107M、
# evidence-archive 131M，还有 daemon.log* 和一堆 .bak-*。这些对「在新机器上接着
# 跑」毫无用处，却是包体的大头。
#
# 顺带修掉一个假警报：这些 exec 日志含 NUL 字节（二进制日志），remap 会按设计
# 跳过它们，于是落地后 grep 还能扫到 7 个文件残留 /Users/danta。不传它们，
# 既省 200M+，也让「remap 后零残留」这条判据真的成立。
EXCLUDE_RELPATHS = [
    ".codex-autopilot/exec",
    ".codex-autopilot/evidence-archive",
    ".codex-autopilot/rotate-runs",
    ".codex-autopilot/fix-backups-20261025",
    ".codex-autopilot/kg-land-trunk-rescue",
    "plugins/codex-autopilot/logs",
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


def _excluded_size() -> tuple[int, list[tuple[str, int]]]:
    """统计会被 EXCLUDE_RELPATHS 剔掉的量（这些在传的条目里面，得从账里扣掉）。"""
    rows: list[tuple[str, int]] = []
    total = 0
    for rel in EXCLUDE_RELPATHS:
        p = HOME / rel
        if p.exists():
            n = _du(p)
            rows.append((rel, n))
            total += n
    return total, rows


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

    drop_total, drop_rows = _excluded_size()
    # 只有落在「要传」条目里的排除项才算真省下的量。
    keep_prefixes = tuple(rel for rel, t, _d in SPEC if t in keep_tiers)
    effective_drop = 0
    for rel, n in drop_rows:
        if any(rel == k or rel.startswith(k + "/") for k in keep_prefixes):
            effective_drop += n
    print("-" * 78)
    print(f"要传（tier={args.tier}）：约 {_human(total_keep - effective_drop)}")
    if total_skip:
        print(f"再生件省掉：约 {_human(total_skip)}")
    if effective_drop:
        print(f"运行历史/备份再省：约 {_human(effective_drop)}")
        for rel, n in sorted(drop_rows, key=lambda x: -x[1])[:6]:
            print(f"    - {rel}  {_human(n)}")
    print("\n传输：`serve` 起临时 HTTP，目标机一条 curl 拉走，源机不落盘。")
    return 0


def _stage_self() -> None:
    """把工具本体放进 $HOME/.codex-autopilot/，好让它随包传到目标机。

    这是有意为之的一处落盘：15KB，且 .codex-autopilot 本来就是 core 条目会被
    整个打走。为什么必须带：落地后第一件事就是 remap（本机实测要改 3.9 万处
    写死的绝对路径），而本工具还没随 releases/ 发版，不带过去目标机就没得跑。

    不用 tar 的 -s 重命名，是因为 bsdtar 在创建模式下不支持（实测报
    "Unrecognized archive format"）。拷贝一份最直白，也最容易验证。
    """
    dst = HOME / SELF_PATH_IN_TAR
    me = Path(__file__).resolve()
    if dst.resolve() == me:
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(me, dst)
        os.chmod(dst, 0o755)
    except OSError as exc:
        print(f"警告：没能把工具本体放进 {dst}（{exc}）；"
              f"目标机上将没有 remap 可跑。", file=sys.stderr)


def _tar_cmd(entries, output: str) -> list[str]:
    _stage_self()
    cmd = ["tar", "-czf", output, "-C", str(HOME)]
    for d in EXCLUDE_DIRS:
        cmd += ["--exclude", d]
    for rel in EXCLUDE_RELPATHS:
        if (HOME / rel).exists():
            cmd += ["--exclude", "./" + rel]
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
    import secrets
    import socketserver

    entries = _entries(args)
    if not entries:
        print("没有可传的条目。", file=sys.stderr)
        return 1
    ip = args.bind or _lan_ip()
    port = args.port

    # 路径里带一次性随机令牌。
    #
    # 为什么必须带：这个 HTTP 服务绑在 0.0.0.0，而包里含 ~/.codex/auth.json
    # （登录凭据）和 .codex/config.toml（各 provider 配置）。裸路径的话，同一个
    # WiFi 下任何扫到 8899 端口的人 GET 一次就把整包拿走。令牌是本次运行的口令，
    # Ctrl-C 即失效；没带令牌的请求一律 404，连「这个端口在服务什么」都不透露。
    token = secrets.token_urlsafe(18)
    path = f"/{token}/{SERVE_NAME}"

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path.rstrip("/") != path:
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
        url = f"http://{ip}:{port}{path}"
        print(f"已在 {ip}:{port} 就绪，提供 {len(entries)} 项。Ctrl-C 结束。\n")
        print("注意：这一包含 ~/.codex/auth.json（登录凭据）等敏感文件，且走的是")
        print("  明文 HTTP。链接里的随机令牌就是本次口令，Ctrl-C 后立即失效；")
        print("  只在可信 WiFi 上跑，传完就 Ctrl-C，别让它常驻。\n")
        print("目标机上执行：")
        print(f'  curl -fL {url} | tar -xzf - -C "$HOME"')
        print()
        print("落地后先 remap 再重载 launchd（目标机用户名与源机不同时必须做）：")
        print('  python3 ~/.codex-autopilot/portable_migrate.py remap --to "$HOME" --go')
        print()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


HOME_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")

# 落地后要扫的范围 = pack 实际会传的那些条目（SPEC 里 core+runtime），再加上用户
# 可能手工搬过来的 launchd plist。
#
# 为什么必须全扫，而不是列几个 glob：实测这台机上写死 /Users/danta 的分布是
#   .codex/config.toml                1 个文件（20+ 处：CODEX_HOME / notify /
#                                     writable_roots / 各 mcp server / projects.*）
#   .codex-autopilot                334 个文件
#   .codex/hooks                    143 个文件
#   plugins/codex-autopilot/releases 35 个文件
#   .codex/plugins/cache/personal     7 个文件
# 只改 .mcp.json 那几处，传过去就是「半个环境」——而且坏得很安静（插件能加载，
# 但 CODEX_HOME、mcp server 路径、project 配置全指着不存在的目录）。
#
# 不碰：二进制（含 NUL 字节）、超大文件、以及 SKIP_DIRS 里的目录。
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist"}
MAX_REMAP_BYTES = 16 << 20


def _remap_roots() -> list[Path]:
    roots = [HOME / rel for rel, tier, _d in SPEC if tier in ("core", "runtime")]
    roots.append(HOME / "Library/LaunchAgents")
    return [r for r in roots if r.exists()]


def _excluded(p: Path) -> bool:
    try:
        rel = str(p.relative_to(HOME))
    except ValueError:
        return False
    return any(rel == r or rel.startswith(r + "/") for r in EXCLUDE_RELPATHS)


def _iter_files(roots):
    seen: set[str] = set()
    for root in roots:
        if root.is_file():
            cand = [root]
        else:
            cand = []
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for f in filenames:
                    cand.append(Path(dirpath) / f)
        for f in cand:
            key = str(f)
            if key in seen:
                continue
            seen.add(key)
            try:
                st = f.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REMAP_BYTES:
                continue
            if _excluded(f):
                continue
            yield f


def _group_of(p: Path) -> str:
    """把命中文件归到它属于哪个 top 条目，便于按组汇报。"""
    rel = p.relative_to(HOME)
    for name, tier, _d in SPEC:
        if tier in ("core", "runtime") and (rel == Path(name) or Path(name) in rel.parents):
            return name
    return str(rel.parts[0]) if len(rel.parts) > 1 else str(rel)


def _read_text(p: Path):
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:          # 二进制，跳过
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _scan():
    """全量扫一遍，返回 [(文件, 文本)] 与 {候选源前缀: 出现次数}。

    注意源前缀不能默认成「当前机器的 HOME」：本工具在目标机上跑，而文件里写死的
    是**源机**的路径。所以从文件内容里自动认出出现最多的 /Users/<name> 当源前缀。
    """
    counts: dict[str, int] = {}
    files: list[tuple[Path, str]] = []
    for p in _iter_files(_remap_roots()):
        txt = _read_text(p)
        if txt is None:
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
    files, counts = _scan()
    src = _pick_source(counts, args.from_home)
    dst = (args.to_home or str(HOME)).rstrip("/")
    print(f"扫描范围：{len(_remap_roots())} 个根、{len(files)} 个文本文件\n")
    if counts and len(counts) > 1:
        print("文件中出现过的 /Users/<name> 前缀（按出现次数）：")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
            flag = "  <= 采用" if name == src else ""
            print(f"  {n:>5}  {name}{flag}")
        print()
    if not src:
        print("没有发现 /Users/<name> 形式的绝对路径，不需要改写。")
        return 0

    hits = [(p, txt.count(src)) for p, txt in files if src in txt]
    if not hits:
        print(f"没有文件引用 {src}。")
        return 0

    groups: dict[str, list[int]] = {}
    for p, n in hits:
        g = groups.setdefault(_group_of(p), [0, 0])
        g[0] += 1
        g[1] += n
    print(f"{'文件数':>6}{'处数':>8}  条目")
    print("-" * 78)
    for g, (fc, oc) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print(f"{fc:>6}{oc:>8}  {g}")
    print("-" * 78)
    total = sum(n for _p, n in hits)
    print(f"合计 {len(hits)} 个文件、{total} 处引用 {src}")
    if args.list:
        print()
        for p, n in sorted(hits, key=lambda x: -x[1]):
            print(f"{n:>5}  {p}")
    else:
        print("（加 --list 看逐文件明细）")

    if dst == src:
        print(f"\n目标 $HOME 与源相同（{src}），无需改写。")
        return 0
    if not args.go:
        print(f"\n预演模式。要真的改成 {dst}，加 --go：")
        print(f"  python3 {Path(__file__).name} remap --to {dst} --go")
        return 0

    n_files = n_occ = 0
    for p, _n in hits:
        txt = _read_text(p)
        if txt is None:
            continue
        n_occ += txt.count(src)
        p.write_text(txt.replace(src, dst), encoding="utf-8")
        n_files += 1
    print(f"\n已改写 {n_files} 个文件、{n_occ} 处：{src} -> {dst}")
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
    p.add_argument("--list", action="store_true",
                   help="逐文件列出命中明细")
    p.set_defaults(func=cmd_remap)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
