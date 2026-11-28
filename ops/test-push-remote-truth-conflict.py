#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""push 重试遇到纯真相层冲突时必须按台账规则合并后继续推送。

2026-11-28 实测：受控 151 候选已经过完 land 门禁并写下进展/验收台账，最后 push
被并发 origin/main 顶掉；同步时 ops/tasks/context_probe_cache.json 与
docs/PROJECT_STATUS.md 冲突，旧实现直接 abort，落盘记成失败。这里用本地 bare
remote 把这条真实路径缩成可读夹具。
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))


def run(cmd, cwd=None, check=True):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"命令失败: {' '.join(map(str, cmd))}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-push-truth-conflict-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        remote_wt = root / "remote-truth"
        origin = root / "origin.git"
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "docs").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"n1","text":"base"}\n'
            '{"id":"remote-only","text":"base"}\n', encoding="utf-8")
        write_json(repo / "ops" / "tasks" / "context_probe_cache.json", {"shared": "base"})
        (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (repo / "docs" / "PROJECT_STATUS.md").write_text(
            "# status\n\nbase\n", encoding="utf-8")
        (repo / "app.txt").write_text("base\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            json.dumps({"build": {"cmd": "", "cwd": "."}, "verify": []}), encoding="utf-8")

        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        run(["git", "init", "-q", "--bare", str(origin)])
        git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")

        # 一条已经过完门禁、等着最后 push 的落盘提交留在独立主干工作树。
        git(repo, "worktree", "add", "-q", "--detach", str(trunk), "main")
        (trunk / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"n1","text":"base"}\n'
            '{"id":"remote-only","text":"base"}\n'
            '{"id":"n1","text":"local","ts":"2026-11-28T03:00:00Z"}\n'
            '{"id":"local-only","text":"local"}\n', encoding="utf-8")
        write_json(trunk / "ops" / "tasks" / "context_probe_cache.json",
                   {"shared": "local", "from_local": True})
        (trunk / "docs" / "PROJECT_STATUS.md").write_text(
            "# status\n\nlocal-landing-view\n", encoding="utf-8")
        (trunk / "landed.txt").write_text("landed\n", encoding="utf-8")
        git(trunk, "add", ".")
        git(trunk, "commit", "-q", "-m", "train: land pending push")

        # 并发心跳在另一棵工作树上抢先把 origin/main 推前，并改了同一批真相文件。
        # 共享检出的 main 仍停在基线：旧实现的 trunk HEAD 比较会在这里误判。
        git(repo, "worktree", "add", "-q", "--detach", str(remote_wt), "main")
        (remote_wt / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"n1","text":"base"}\n'
            '{"id":"n1","text":"remote","ts":"2026-11-28T02:00:00Z"}\n'
            '{"id":"remote-only","text":"remote"}\n', encoding="utf-8")
        write_json(remote_wt / "ops" / "tasks" / "context_probe_cache.json",
                   {"shared": "remote", "from_remote": True})
        (remote_wt / "docs" / "PROJECT_STATUS.md").write_text(
            "# status\n\nremote-heartbeat-view\n", encoding="utf-8")
        git(remote_wt, "add", ".")
        git(remote_wt, "commit", "-q", "-m", "chore(truth): concurrent heartbeat")
        git(remote_wt, "push", "-q", "origin", "HEAD:main")
        git(repo, "fetch", "-q", "origin", "main")

        saved = {k: os.environ.get(k) for k in
                 ("KG_REPO", "KG_TRUNK", "KG_TRUTH_ID_STATE_ROOT", "KG_MODEL_PROBE")}
        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(trunk)
        os.environ["KG_TRUTH_ID_STATE_ROOT"] = str(root / "truth-ids")
        os.environ["KG_MODEL_PROBE"] = "off"
        try:
            import kgctl
            importlib.reload(kgctl)
            progress = StringIO()
            with redirect_stdout(progress):
                pushed = kgctl._push_main_with_one_retry(trunk)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        if "已按台账规则合并" not in progress.getvalue():
            raise AssertionError(
                f"push 重试没有走真相层冲突合并: stdout={progress.getvalue()!r} return={pushed!r}")
        if git(trunk, "status", "--porcelain").stdout.strip():
            raise AssertionError("push 重试后主干工作树仍有在途改动")
        if git(repo, "status", "--porcelain").stdout.strip():
            raise AssertionError("push 重试弄脏了共享检出")

        git(repo, "fetch", "-q", "origin", "main")
        remote_head = git(repo, "rev-parse", "origin/main").stdout.strip()
        trunk_head = git(trunk, "rev-parse", "HEAD").stdout.strip()
        if remote_head != trunk_head:
            raise AssertionError("并发同步后 push 未真正推上本次落盘提交")

        def show(rel):
            return git(repo, "show", f"origin/main:{rel}").stdout

        if "landed" not in show("landed.txt"):
            raise AssertionError("代码落盘内容没有进入 origin/main")

        inbox = [json.loads(line) for line in show("ops/tasks/inbox.jsonl").splitlines()
                 if line.strip()]
        texts = {row.get("id"): row.get("text") for row in inbox}
        if texts.get("remote-only") != "remote" or texts.get("local-only") != "local":
            raise AssertionError(f"JSONL 并集丢了并发某一侧记录: {inbox!r}")
        n1 = [row for row in inbox if row.get("id") == "n1"]
        if not n1 or n1[-1].get("text") != "local":
            raise AssertionError(f"JSONL 同 id 的最新记录不是本地落盘版本: {n1!r}")

        cache = json.loads(show("ops/tasks/context_probe_cache.json"))
        if cache.get("from_remote") is not True or cache.get("from_local") is not True:
            raise AssertionError(f"JSON 合并丢了并发某一侧字段: {cache!r}")
        if cache.get("shared") != "local":
            raise AssertionError(f"JSON 冲突键没有按远端为底、本地覆盖: {cache!r}")

        status = show("docs/PROJECT_STATUS.md")
        if "remote-heartbeat-view" not in status or "local-landing-view" in status:
            raise AssertionError(f"生成视图没有保留远端最新快照: {status!r}")

    print("selftest ok: 并发 push 冲突按台账规则解后推上 origin/main，"
          "JSONL/JSON 两侧记录不丢，共享检出保持干净。")


if __name__ == "__main__":
    main()
