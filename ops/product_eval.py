#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""product_eval - 对外发布前的评估闭环（stdlib only，默认只读）。

为什么要有它
------------
「这个产品能不能对外发」此前只能靠人翻日志拍脑袋。本项目已经踩过一次：
control.json 全绿、面板显示「后台常驻=开」，插件的 .mcp.json 却把 KG_REPO 指到
一个空目录。开关全绿、界面正常、什么都不抛，真实项目里一条需求都没落。
这类失效不会亮红灯，用户唯一的感受就是「装了跟没装一样」。

所以评估本身必须是一个**闭环**，不是一次看片：

  * 冻结对象    一次评估锚定到 commit / release 版本 / 项目路径。
  * 可执行判据  每条判据落成纯函数，证据统一成 命令 + cwd + 退出码 + 原始输出。
  * 双向验证    每条判据都配一个**阴性对照**：喂它一个已知的坏现场，它必须变红。
                只证过能绿的判据不算判据；`--selftest` 就是这个方向的证明。
  * 会红的认证物 所有结论收敛成一份 certification；`release_ready` 为假时明确
                列出是哪几条、差多少，而不是含糊地说「还差一点」。

五条发布判据（全绿 + 阴性对照全过，才算「能对外」）
  1 装得上     安装目录里有一个完整、可加载的 release
  2 跑得起来   常驻守护在跑，且绑定的是当前项目
  3 服务对象对 安装的 .mcp.json 里 KG_REPO 指向一个真实项目，且就是期望项目
  4 有效       北极星首问闭环率有真实分母，且台账是活的（不是八天前的旧数）
  5 一致可回滚 release / Codex 缓存 / 主干三处版本一致，且有旧版本可回退

只读契约：默认不写目标项目任何文件。只有显式 --write 才把认证物落进
<repo>/ops/truth/release-certification.json。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))
DEFAULT_REPO = OPS_DIR.parent

# 阈值预先写死，不事后挑一个好看的。
MIN_DENOMINATOR = 5          # 分母太小说明这个项目还没真用过，比例不可信
MIN_RATE_PCT = 50.0
MAX_LEDGER_AGE_DAYS = 14.0   # 台账这么久没动过，说明自动挡其实在空转


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _home() -> Path:
    return Path(os.environ.get("KG_EVAL_HOME", str(Path.home()))).expanduser()


def install_dir() -> Path:
    return Path(os.environ.get(
        "KG_EVAL_INSTALL_DIR",
        str(_home() / "plugins" / "codex-autopilot"))).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get(
        "KG_EVAL_CACHE_DIR",
        str(_home() / ".codex" / "plugins" / "cache" / "personal"
            / "codex-autopilot"))).expanduser()


def trunk_dir(repo=None) -> Path:
    env = os.environ.get("KG_EVAL_TRUNK") or os.environ.get("KG_TRUNK")
    if env:
        return Path(env).expanduser()
    repo = Path(repo or DEFAULT_REPO).resolve()
    return repo.parent / (repo.name + "-trunk")


def ev(argv, cwd=None, note=""):
    """跑一条只读命令，把证据压成固定结构。绝不抛异常，拿不到就是拿不到。"""
    argv = [str(a) for a in argv]
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=30)
        code, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        code, out, err = -1, "", "%s: %s" % (type(exc).__name__, exc)
    return {"cmd": " ".join(argv), "cwd": str(cwd or Path.cwd()), "exit": code,
            "out": out.strip()[:1200], "err": err.strip()[:400], "note": note}


def _git(args, cwd):
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _git_ok(args, cwd):
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.returncode == 0


def _age_days(path: Path):
    try:
        return (time.time() - path.stat().st_mtime) / 86400.0
    except OSError:
        return None


def _result(cid, name, status, evidence, detail="", shortfall=None):
    return {"id": cid, "name": name, "status": status, "evidence": evidence,
            "detail": detail, "shortfall": shortfall or []}


# ----------------------------------------------------------------- 判据 1
def crit_installable(idir=None):
    """1 装得上：安装目录里有一个完整、可加载的 release。"""
    idir = Path(idir or install_dir())
    evidence, shortfall = [], []

    state_p = idir / "install-state.json"
    evidence.append(ev(["cat", str(state_p)]))
    version = None
    if not state_p.exists():
        shortfall.append("没有 install-state.json：%s" % state_p)
    else:
        try:
            version = json.loads(state_p.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError) as exc:
            shortfall.append("install-state.json 读不了：%s" % exc)

    if not version:
        shortfall.append("install-state.json 里没有 version 字段")
    else:
        release = idir / "releases" / str(version)
        for sub in ("ops/central_mcp.py", ".mcp.json",
                    ".codex-plugin/plugin.json", "skills"):
            if not (release / sub).exists():
                shortfall.append("安装的 release 缺 %s：%s" % (sub, release / sub))

    status = "pass" if not shortfall else "fail"
    detail = ("安装版本 %s" % version if not shortfall else "；".join(shortfall))
    return _result("installable", "1 装得上", status, evidence, detail, shortfall)


# ----------------------------------------------------------------- 判据 2
def crit_runnable(repo=None, launcher=None):
    """2 跑得起来：常驻守护在跑，且绑定的是当前项目。"""
    import daemon_liveness

    repo = Path(repo or DEFAULT_REPO).resolve()
    st = daemon_liveness.daemon_liveness(str(repo), launcher=launcher)
    evidence = [ev(["launchctl", "print",
                    "gui/%s/com.codex-autopilot.daemon" % os.getuid()],
                   note="守护存活判据（与 kgctl status / doctor 同源）")]
    detail = daemon_liveness.liveness_line(st, daemon_enabled=True)

    shortfall = []
    if st.get("status") != "running":
        shortfall.append("守护没在跑（status=%s）" % st.get("status"))
    elif st.get("matches_repo") is False:
        shortfall.append("守护在跑，但绑定 %s，不是当前项目 %s"
                         % (st.get("bound_repo"), repo))
    status = "pass" if not shortfall else "fail"
    return _result("runnable", "2 跑得起来", status, evidence, detail, shortfall)


# ----------------------------------------------------------------- 判据 3
def crit_targets(idir=None, expected_repo=None):
    """3 服务对象对：装的 .mcp.json 里 KG_REPO 指向真实项目，且就是期望项目。"""
    idir = Path(idir or install_dir())
    expected = str(Path(expected_repo or DEFAULT_REPO).resolve())
    evidence, shortfall = [], []

    version = None
    try:
        version = json.loads((idir / "install-state.json")
                             .read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        pass
    mcp = idir / "releases" / str(version) / ".mcp.json"
    evidence.append(ev(["cat", str(mcp)], note="绑定的唯一真相：插件的 .mcp.json"))

    bound = None
    try:
        env = ((json.loads(mcp.read_text(encoding="utf-8"))
                .get("mcpServers") or {}).get("codex-autopilot") or {}).get("env") or {}
        bound = env.get("KG_REPO")
    except (OSError, ValueError) as exc:
        shortfall.append(".mcp.json 读不了：%s" % exc)

    if not bound:
        shortfall.append(".mcp.json 没有 KG_REPO（自动挡没绑定任何项目）")
    else:
        bpath = Path(bound).expanduser()
        if not bpath.is_dir():
            shortfall.append("绑定的状态根不存在：%s" % bpath)
        else:
            top = _git(["rev-parse", "--show-toplevel"], bpath)
            if not top:
                shortfall.append("绑定的状态根不是 git 仓库：%s" % bpath)
            elif str(Path(top).resolve()) != str(bpath.resolve()):
                shortfall.append("绑定的是子目录不是仓库根：%s（根是 %s）" % (bpath, top))
            for sub in ("ops/tasks", "ops/truth"):
                if not (bpath / sub).is_dir():
                    shortfall.append("绑定的状态根缺账本目录：%s" % (bpath / sub))
            if str(bpath.resolve()) != expected:
                shortfall.append("绑定 %s，不是期望项目 %s（自动挡在替别的账本干活）"
                                 % (bpath.resolve(), expected))

    status = "pass" if not shortfall else "fail"
    return _result("targets", "3 服务对象对", status, evidence,
                   "绑定 %s；期望 %s" % (bound, expected), shortfall)


# ----------------------------------------------------------------- 判据 4
def crit_effective(repo=None):
    """4 有效：北极星闭环率有真实分母，且台账是活的。"""
    import closure_rate

    repo = Path(repo or DEFAULT_REPO).resolve()
    res = closure_rate.compute(str(repo))
    ledger = repo / "ops" / "truth" / "ledger.jsonl"
    age = _age_days(ledger)
    evidence = [ev([sys.executable, str(OPS_DIR / "closure_rate.py"),
                    "--repo", str(repo)], note="北极星：首问闭环率"),
                {"cmd": "stat ledger mtime", "cwd": str(Path.cwd()),
                 "exit": 0 if ledger.exists() else 1,
                 "out": ("%.2f 天前" % age) if age is not None else "取不到账本",
                 "err": "", "note": "台账距今多少天"}]

    shortfall = []
    if res.get("status") != "ok":
        shortfall.append("闭环率算不出来（status=%s：%s）"
                         % (res.get("status"), res.get("reason")))
    else:
        denom = res.get("denominator") or 0
        rate = res.get("rate_pct")
        if denom < MIN_DENOMINATOR:
            shortfall.append("分母只有 %s，低于 %s：这个项目还没被真用过，比例不可信"
                             % (denom, MIN_DENOMINATOR))
        elif rate is not None and rate < MIN_RATE_PCT:
            shortfall.append("闭环率 %s%% 低于阈值 %s%%" % (rate, MIN_RATE_PCT))
        if res.get("done_without_evidence"):
            shortfall.append("标了 done 却没有证据：%s" % res["done_without_evidence"])

    if age is None:
        shortfall.append("台账读不到：%s" % ledger)
    elif age > MAX_LEDGER_AGE_DAYS:
        shortfall.append("台账 %.1f 天没动过，自动挡其实在空转（阈值 %.0f 天）"
                         % (age, MAX_LEDGER_AGE_DAYS))

    status = "pass" if not shortfall else "fail"
    return _result("effective", "4 有效", status, evidence,
                   closure_rate.human_line(res), shortfall)


# ----------------------------------------------------------------- 判据 5
def crit_consistent(idir=None, cdir=None, trunk=None):
    """5 一致可回滚：release / Codex 缓存 / 主干三处版本一致，且有旧版本。"""
    idir = Path(idir or install_dir())
    cdir = Path(cdir or cache_dir())
    trunk = Path(trunk or trunk_dir())
    evidence, shortfall = [], []

    try:
        st = json.loads((idir / "install-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    installed_v, installed_c = st.get("version"), st.get("source_commit")
    previous = st.get("previous_source")

    cache_vers = (sorted(p.name for p in cdir.iterdir() if p.is_dir())
                  if cdir.is_dir() else [])
    cache_latest = cache_vers[-1] if cache_vers else None
    trunk_head = _git(["rev-parse", "HEAD"], trunk)
    evidence += [ev(["cat", str(idir / "install-state.json")]),
                 ev(["ls", str(cdir)], note="Codex 实际加载的缓存版本"),
                 ev(["git", "-C", str(trunk), "log", "--oneline", "-1"], cwd=trunk)]

    if not installed_v:
        shortfall.append("读不到安装版本")
    if not cache_latest:
        shortfall.append("缓存目录里没有版本：%s" % cdir)
    elif installed_v and cache_latest != installed_v:
        shortfall.append("Codex 加载的缓存是 %s，安装的是 %s（桌面端还在跑旧版本）"
                         % (cache_latest, installed_v))
    if not trunk_head:
        shortfall.append("主干不是可读的 git 仓库：%s" % trunk)
    elif installed_c:
        ok = _git_ok(["merge-base", "--is-ancestor", installed_c, trunk_head], trunk)
        if ok is False:
            shortfall.append("主干 HEAD %s 不含已发布提交 %s（三条线分叉）"
                             % (trunk_head[:8], installed_c[:8]))
        elif ok is None:
            shortfall.append("无法判断主干是否包含已发布提交")
    if previous:
        prev_name = Path(str(previous).rstrip("/")).name
        if prev_name and not (idir / "releases" / prev_name).is_dir():
            shortfall.append("上一个版本目录不在，回滚没有落点：%s" % previous)

    status = "pass" if not shortfall else "fail"
    detail = "安装 %s / 缓存 %s / 主干 %s" % (installed_v, cache_latest,
                                              trunk_head[:8] or "?")
    return _result("consistent", "5 一致可回滚", status, evidence, detail, shortfall)


# ----------------------------------------------------------------- 阴性对照
def negative_controls():
    """每条判据喂一个已知坏现场，它必须变红。返回 [(判据, 是否如预期变红, 说明)]。

    这是「双向验证」：只证过能绿的判据等于没评估。对照组全部走临时目录，
    绝不碰真实安装与真实项目。
    """
    import tempfile

    out = []
    with tempfile.TemporaryDirectory(prefix="product-eval-neg-") as tmp:
        tmp = Path(tmp)

        # 1 空安装目录：没有 install-state.json，必须红。
        empty = tmp / "install-empty"
        empty.mkdir()
        r = crit_installable(empty)
        out.append(("1 装得上", r["status"] == "fail", "空安装目录 -> %s" % r["status"]))

        # 2 守护 job 不存在 / 绑错项目，都必须红。
        def gone(_argv):
            return 113, 'Could not find service "com.codex-autopilot.daemon"'

        def other(_argv):
            return 0, ("state = running\n\tpid = 42\n"
                       "\tKG_REPO => /tmp/some-other-project\n")

        r = crit_runnable(str(tmp), launcher=gone)
        out.append(("2 跑得起来(未安装)", r["status"] == "fail",
                    "job 不存在 -> %s" % r["status"]))
        r = crit_runnable(str(tmp), launcher=other)
        out.append(("2 跑得起来(绑错项目)", r["status"] == "fail",
                    "守护绑 /tmp/some-other-project -> %s" % r["status"]))

        # 3 绑到一个非 git 目录：必须红。
        bad_install = tmp / "install-bad-bind"
        rel = bad_install / "releases" / "0.0.1"
        rel.mkdir(parents=True)
        (bad_install / "install-state.json").write_text(
            json.dumps({"version": "0.0.1"}), encoding="utf-8")
        fake_target = tmp / "not-a-repo"
        fake_target.mkdir()
        (rel / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"codex-autopilot": {
                "env": {"KG_REPO": str(fake_target)}}}}), encoding="utf-8")
        r = crit_targets(bad_install, expected_repo=str(fake_target))
        out.append(("3 服务对象对(非 git 仓库)", r["status"] == "fail",
                    "绑到非仓库目录 -> %s" % r["status"]))

        # 4 台账全 open（0%）；台账缺失（取不到分母，不是 0%）。都必须红。
        proj = tmp / "proj"
        (proj / "ops" / "truth").mkdir(parents=True)
        (proj / "ops" / "truth" / "ledger.jsonl").write_text(
            "\n".join(json.dumps({"type": "ledger", "id": "L-%04d" % i,
                                  "status": "open", "evidence": ""})
                      for i in range(6)) + "\n", encoding="utf-8")
        r = crit_effective(proj)
        out.append(("4 有效(全未闭环)", r["status"] == "fail",
                    "6 条全 open -> %s" % r["status"]))
        empty_proj = tmp / "proj-empty"
        (empty_proj / "ops" / "truth").mkdir(parents=True)
        r = crit_effective(empty_proj)
        out.append(("4 有效(台账缺失)", r["status"] == "fail",
                    "取不到分母 -> %s" % r["status"]))

        # 5 缓存版本落后于安装版本：必须红。
        ci = tmp / "install-v"
        (ci / "releases" / "0.0.9").mkdir(parents=True)
        (ci / "install-state.json").write_text(
            json.dumps({"version": "0.0.9", "source_commit": "deadbeef"}),
            encoding="utf-8")
        (tmp / "cache-v" / "0.0.8").mkdir(parents=True)
        r = crit_consistent(ci, tmp / "cache-v", tmp / "nodir")
        out.append(("5 一致可回滚(缓存落后)", r["status"] == "fail",
                    "缓存 0.0.8 vs 安装 0.0.9 -> %s" % r["status"]))
    return out


# ----------------------------------------------------------------- 认证物
def certify(repo=None):
    """跑完五条判据，产出一份会红的认证物。只读。"""
    repo = Path(repo or DEFAULT_REPO).resolve()
    frozen = {
        "evaluated_at": _now(),
        "expected_repo": str(repo),
        "install_dir": str(install_dir()),
        "cache_dir": str(cache_dir()),
        "trunk": str(trunk_dir(repo)),
        "repo_head": _git(["rev-parse", "HEAD"], repo) or None,
    }
    criteria = [
        crit_installable(),
        crit_runnable(repo),
        crit_targets(expected_repo=repo),
        crit_effective(repo),
        crit_consistent(),
    ]
    controls = [{"criterion": c, "went_red": ok, "note": note}
                for c, ok, note in negative_controls()]
    failed = [c["id"] for c in criteria if c["status"] != "pass"]
    controls_bad = [c for c in controls if not c["went_red"]]
    return {
        "generated_at": _now(),
        "frozen": frozen,
        "criteria": criteria,
        "negative_controls": controls,
        "summary": {
            "passed": len(criteria) - len(failed),
            "total": len(criteria),
            "failed": failed,
            "controls_failed": [c["criterion"] for c in controls_bad],
            "release_ready": not failed and not controls_bad,
        },
    }


def human_report(cert):
    lines = ["对外发布评估  %s" % cert["generated_at"],
             "冻结对象  项目 %s @ %s" % (cert["frozen"]["expected_repo"],
                                         (cert["frozen"]["repo_head"] or "?")[:8])]
    for c in cert["criteria"]:
        mark = {"pass": "OK  ", "fail": "红  ", "unknown": "看不清"}.get(c["status"], "?")
        lines.append("  %s %s  %s" % (mark, c["name"], c["detail"]))
        for s in c["shortfall"]:
            lines.append("        - %s" % s)
    bad = [c for c in cert["negative_controls"] if not c["went_red"]]
    lines.append("阴性对照  %d/%d 条判据确认能变红%s"
                 % (len(cert["negative_controls"]) - len(bad),
                    len(cert["negative_controls"]),
                    "" if not bad else "（失效：%s）" % [c["criterion"] for c in bad]))
    s = cert["summary"]
    lines.append("结论  %s（%d/%d 通过）" % (
        "可以对外" if s["release_ready"] else "不能对外", s["passed"], s["total"]))
    if not s["release_ready"]:
        lines.append("差什么  未达标判据：%s" % "、".join(s["failed"]))
    return "\n".join(lines)


def write_certification(repo, cert):
    target = Path(repo) / "ops" / "truth" / "release-certification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cert, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    tmp.replace(target)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description="对外发布评估闭环（默认只读）")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="期望服务的目标项目")
    ap.add_argument("--json", action="store_true", help="输出认证物 JSON")
    ap.add_argument("--write", action="store_true",
                    help="把认证物写进 <repo>/ops/truth/release-certification.json")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑阴性对照，证明每条判据都能变红")
    args = ap.parse_args(argv)

    if args.selftest:
        rows = negative_controls()
        bad = [c for c, ok, _n in rows if not ok]
        for c, ok, note in rows:
            print("  %s %s（%s）" % ("OK  " if ok else "失效", c, note))
        if bad:
            print("阴性对照失败：%s" % bad)
            return 1
        print("product-eval selftest ok: 五条判据的阴性对照全部按预期变红。")
        return 0

    cert = certify(args.repo)
    if args.write:
        print("认证物已写入 %s" % write_certification(args.repo, cert))
    if args.json:
        print(json.dumps(cert, ensure_ascii=False, indent=1))
    else:
        print(human_report(cert))
    return 0 if cert["summary"]["release_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
