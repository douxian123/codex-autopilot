#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板渲染检查：用真浏览器渲染真面板，喂真实控制层数据，核对首屏文字。

为什么必须用 iframe：面板是往 window.parent 发消息的。旧版本把面板直接放
在顶层窗口里，parent 等于自己，消息被自己接住，画面永远停在“读取中”，
而门禁又在整页文本里搜注入脚本里的数据，于是出现“画面没渲染却全部 PASS”
的假通过。这里按真实宿主结构（父窗口 + iframe）驱动，只看 iframe 内的文本。

用法：
  python3 ops/panel_render_check.py              # 检查 + 截图
  python3 ops/panel_render_check.py --no-shot    # 只检查，不截图
  退出码 = 未通过项数；找不到浏览器时退出码 2 并如实说明。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
PANEL = REPO / "plugin" / "ui" / "panel.html"
SHOT = REPO / "docs" / "panel-first-screen.png"

WRAPPER = """<!doctype html><meta charset="utf-8">
<style>html,body{margin:0;background:#fff}#f{width:560px;height:1500px;border:0;display:block}</style>
<iframe id="f" src="panel.html"></iframe>
<script>
var payload = __PAYLOAD__;
window.addEventListener("message", function(ev){
  var msg = ev.data;
  if (!msg || msg.jsonrpc !== "2.0" || !msg.method) return;
  var name = (msg.params && msg.params.name) || "";
  var out = (name === "autopilot_status") ? payload : {ok: true, __text__: ""};
  ev.source.postMessage({jsonrpc: "2.0", id: msg.id,
    result: {isError: false, structuredContent: out,
             content: [{type: "text", text: ""}]}}, "*");
});
setTimeout(function(){
  var d = document.getElementById("f").contentDocument;
  var out = {};
  ["stateLabel","statusTime","projectLine","focusStatus","focusDoing","focusStep",
   "hierProject","hierRoad","hierWork"].forEach(function(id){
    var n = d.getElementById(id);
    out[id] = n ? n.textContent.trim() : "";
      out["__body__"] = (d.body ? d.body.innerText : "");
      var ns = d.getElementById("nativeState");
      out["nativeState"] = ns ? ns.textContent.trim() : "";
  });
  var pre = document.createElement("pre");
  pre.id = "panel-mirror";
  pre.style.display = "none";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 5000);
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
    for name in ("chromium", "google-chrome", "chrome", "Google Chrome"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).is_file():
        return mac
    return None


def _run(chromium, workdir, args):
    base = [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            "--window-size=560,1600",
            "--user-data-dir=" + str(workdir / "profile")]
    try:
        return subprocess.run(base + args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", "replace")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", "replace")
        return subprocess.CompletedProcess(
            exc.cmd, 124, stdout.decode("utf-8", "replace"),
            (stderr + b"\nchromium timeout after 30s").decode("utf-8", "replace"))


def main(argv):
    want_shot = "--no-shot" not in argv
    sys.path.insert(0, str(OPS))
    os.environ.setdefault("KG_REPO", str(REPO))
    import central_mcp

    payload = central_mcp._status_payload(fast=True)
    project = payload.get("project") or {}
    # 路线里未必有显式 "next"。有就用 next，否则用进行中的步骤，都没有则跳过这条。
    roadmap = payload.get("roadmap") or []
    next_step = next((r.get("step") for r in roadmap if r.get("status") == "next"), None)
    if not next_step:
        next_step = next((r.get("step") for r in roadmap if r.get("status") == "doing"), None)
    step_check = ("路线里显示下一步", next_step) if next_step else None

    chromium = find_chromium()
    if not chromium:
        print("找不到 Chromium/Chrome，面板渲染检查跳过。")
        print("  可以设 AUTOPILOT_CHROMIUM 指向一个 Chrome 可执行文件后重跑。")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="autopilot-panel-render-"))
    html = PANEL.read_text(encoding="utf-8")
    if "</body>" not in html:
        print("面板 HTML 结构异常：没有 </body>")
        return 1
    (workdir / "panel.html").write_text(html, encoding="utf-8")
    wrapper = workdir / "panel-harness.html"
    wrapper.write_text(
        WRAPPER.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)),
        encoding="utf-8")

    dom_proc = _run(chromium, workdir, ["--virtual-time-budget=9000", "--dump-dom",
                                        "--allow-file-access-from-files",
                                        wrapper.as_uri()])
    dom = dom_proc.stdout or ""
    import re
    m = re.search(r'id="panel-mirror"[^>]*>(.*?)</pre>', dom, re.S)
    snapshot = {}
    if m:
        raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
               .replace("&lt;", "<").replace("&gt;", ">"))
        try:
            snapshot = json.loads(raw)
        except Exception:
            snapshot = {}
    if not snapshot:
        print("面板没有渲染出快照（浏览器可能启动失败）。stderr 摘要：")
        print((dom_proc.stderr or "")[-600:])
        return 1

    # 注入的 payload 和面板源码都在 <script> 里。比对前必须移除脚本，
    # 否则脚本中的答案会把「页面已渲染」伪装成通过。
    rendered = re.sub(r"<script\b[^>]*>.*?</script>", "", dom,
                      flags=re.IGNORECASE | re.DOTALL)

    def visible(text):
        if not text:
            return False
        return any(text in v for v in snapshot.values())

    checks = [
        ("首屏显示绑定项目名", project.get("name")),
        ("首屏显示绑定项目路径", project.get("path")),
        ("首屏显示总状态文案", payload.get("state_label")),
        # 三问②的显示优先级：需要人行动的最优先（与 J-0001 一致）——
        # 有人工验收时显示验收项，否则显示正在做的事。
        ("首屏显示正在做什么", (payload.get("human_acceptance") or [{}])[0].get("title")
         if payload.get("human_acceptance") else payload.get("doing")),
        ("首屏显示原生直连结论", (payload.get("native") or {}).get("label")),
        ("有「项目文件夹」区块", "项目文件夹"),
        ("有「需要你介入」区块", "需要你介入"),
        ("有「工作项走到哪了」区块", "工作项走到哪了"),
        ("有「工程详情与证据」区块", "工程详情与证据"),
    ]
    folders = ((payload.get("project_folders") or {}).get("folders") or [])
    if folders:
        checks.append(("项目文件夹显示当前步骤", folders[0].get("current_step")))
    if step_check:
        checks.append(step_check)

    fails = 0
    print("Codex 自动挡 面板渲染检查")

    stuck = [i for i in ("stateLabel", "projectLine", "focusStatus", "focusStep")
             if snapshot.get(i, "").strip() in ("", "读取中", "读取中…")]
    if stuck:
        print("  FAIL  首屏停在“读取中”：" + ", ".join(stuck))
        fails += 1
    else:
        print("  PASS  首屏已渲染真实数据（非“读取中”）")

    for label, expected in checks:
        if visible(expected):
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}（期望文本：{expected!r}）")
            fails += 1

    if want_shot:
        shot_proc = _run(chromium, workdir, [f"--screenshot={SHOT}",
                                             "--virtual-time-budget=9000",
                                             "--allow-file-access-from-files",
                                             wrapper.as_uri()])
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
