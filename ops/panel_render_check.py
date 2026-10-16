#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板渲染检查：用真浏览器渲染真面板，喂真实控制层数据，核对首屏文字。

静态检查（元素 id、工具名对得上）只能证明「面板看着对」，证明不了首屏真的把
「开没开 / 当前项目 / 正在处理什么 / 需要你处理」渲染出来。这里起一个 headless
Chromium，用与 Codex 宿主同一条 postMessage 桥驱动面板，把渲染后的 DOM 文本与
真实状态逐条对照，并顺手存一张首屏截图。

用法：
  python3 ops/panel_render_check.py              # 检查 + 截图
  python3 ops/panel_render_check.py --no-shot    # 只检查，不截图
  退出码 = 未通过项数；找不到浏览器时退出码 2 并如实说明。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
PANEL = REPO / "plugin" / "ui" / "panel.html"
SHOT = REPO / "docs" / "panel-first-screen.png"

BRIDGE = """
<script>
(function(){
  var payload = %s;
  window.addEventListener("message", function(ev){
    var msg = ev.data;
    if (!msg || msg.jsonrpc !== "2.0" || !msg.method) return;
    var name = (msg.params && msg.params.name) || "";
    var out = (name === "autopilot_status") ? payload : {ok: true, __text__: ""};
    window.postMessage({jsonrpc: "2.0", id: msg.id,
      result: {isError: false, structuredContent: out,
               content: [{type: "text", text: ""}]}}, "*");
  });
})();
</script>
"""


def find_chromium():
    override = os.environ.get("AUTOPILOT_CHROMIUM")
    if override and Path(override).is_file():
        return override
    home = Path.home()
    patterns = (
        "Library/Caches/ms-playwright/chromium-*/chrome-mac-*/"
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "Library/Caches/ms-playwright/chromium-*/chrome-mac-*/"
        "Chromium.app/Contents/MacOS/Chromium",
    )
    for pattern in patterns:
        for cand in sorted(home.glob(pattern)):
            if cand.is_file():
                return str(cand)
    for name in ("chromium", "google-chrome", "chrome",
                 "Google Chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run(chromium, workdir, args):
    base = [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            "--window-size=560,1600",
            "--user-data-dir=" + str(workdir / "profile")]
    return subprocess.run(base + args, capture_output=True, text=True)


def main(argv):
    want_shot = "--no-shot" not in argv
    sys.path.insert(0, str(OPS))
    os.environ.setdefault("KG_REPO", str(REPO))
    import central_mcp

    payload = central_mcp._status_payload(fast=True)
    project = payload.get("project") or {}
    next_step = next((r.get("step") for r in (payload.get("roadmap") or [])
                      if r.get("status") == "next"), None)

    chromium = find_chromium()
    if not chromium:
        print("找不到 Chromium/Chrome，面板渲染检查跳过。")
        print("  可以设 AUTOPILOT_CHROMIUM 指向一个 Chrome 可执行文件后重跑。")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="autopilot-panel-render-"))
    harness = workdir / "panel-harness.html"
    html = PANEL.read_text(encoding="utf-8")
    if "</body>" not in html:
        print("面板 HTML 结构异常：没有 </body>")
        return 1
    harness.write_text(
        html.replace("</body>", BRIDGE % json.dumps(payload, ensure_ascii=False) + "</body>", 1),
        encoding="utf-8")

    dom_proc = _run(chromium, workdir, ["--virtual-time-budget=8000", "--dump-dom",
                                        harness.as_uri()])
    dom = dom_proc.stdout or ""
    if "绑定项目" not in dom:
        print("面板没有渲染出来（可能是浏览器启动失败）。stderr 摘要：")
        print((dom_proc.stderr or "")[-600:])
        return 1

    def visible(text):
        """渲染后的 DOM 里文本会被转义，比对前还原常见实体。"""
        if not text:
            return False
        probe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        return probe in dom or text in dom

    checks = [
        ("首屏显示绑定项目名", project.get("name")),
        ("首屏显示绑定项目路径", project.get("path")),
        ("首屏显示总状态文案", payload.get("state_label")),
        ("首屏显示原生直连结论", (payload.get("native") or {}).get("label")),
        ("有「需要你处理」区块", "需要你处理"),
        ("有「当前工作」区块", "当前工作"),
        ("有「产品路线」区块", "产品路线"),
    ]
    if next_step:
        checks.append(("路线里显示下一步", next_step))

    fails = 0
    print("Codex 自动挡 面板渲染检查")
    for label, expected in checks:
        if visible(expected):
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}（期望文本：{expected!r}）")
            fails += 1

    if want_shot:
        shot_proc = _run(chromium, workdir, [f"--screenshot={SHOT}",
                                             "--virtual-time-budget=8000",
                                             harness.as_uri()])
        if SHOT.is_file() and SHOT.stat().st_size > 0:
            print(f"  PASS  首屏截图已存：{SHOT}")
        else:
            print("  FAIL  截图未生成")
            print((shot_proc.stderr or "")[-400:])
            fails += 1

    shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n结果：{'全部通过' if fails == 0 else str(fails) + ' 项未通过'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
