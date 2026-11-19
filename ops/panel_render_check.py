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
PANEL = REPO / "ui" / "panel.html"
if not PANEL.exists():
    PANEL = REPO / "plugin" / "ui" / "panel.html"
SHOT = REPO / "docs" / "panel-first-screen.png"

WRAPPER = """<!doctype html><meta charset="utf-8">
<style>html,body{margin:0;background:#fff}#f{width:560px;height:1500px;border:0;display:block}</style>
<iframe id="f" src="panel.html__QUERY__"></iframe>
<script>
var payload = __PAYLOAD__;
var routePayload = __ROUTE_PAYLOAD__;
window.addEventListener("message", function(ev){
  var msg = ev.data;
  if (!msg || msg.jsonrpc !== "2.0" || !msg.method) return;
  var name = (msg.params && msg.params.name) || "";
  var out = (name === "autopilot_status") ? payload
    : (name === "route_preview") ? routePayload
    : {ok: true, __text__: ""};
  ev.source.postMessage({jsonrpc: "2.0", id: msg.id,
    result: {isError: false, structuredContent: out,
             content: [{type: "text", text: ""}]}}, "*");
});
setTimeout(function(){
  var d = document.getElementById("f").contentDocument;
  var input = d ? d.getElementById("intakeText") : null;
  if (input) {
    input.value = "knowledge-graph 的面板归属测试";
    input.dispatchEvent(new Event("input", {bubbles: true}));
  }
}, 800);
setTimeout(function(){
  var d = document.getElementById("f").contentDocument;
  var out = {};
  if (__OPEN_FOLD__) {
    var pmFold = d.getElementById("pmBriefingFold");
    if (pmFold) pmFold.open = true;
  }
  __IDS__.forEach(function(id){
    var domId = (id === "pmBriefingFoldOpen") ? "pmBriefingFold" : id;
    var n = d.getElementById(domId);
    if (id === "pmBriefingFoldOpen") {
      out[id] = n ? ((n.open || n.hasAttribute("open")) ? "1" : "0") : "";
    } else {
      out[id] = n ? n.textContent.trim() : "";
    }
  });
  var intake = d ? d.getElementById("intakeText") : null;
  out["intakeInAdvanced"] = intake && intake.closest
    ? (intake.closest("#advanced") ? "1" : "0") : "";
  out["intakeVisible"] = intake
    ? ((intake.getClientRects && intake.getClientRects().length) ? "1" : "0") : "";
  var systemFold = d ? d.getElementById("systemEvidenceFold") : null;
  out["systemEvidenceOpen"] = systemFold
    ? ((systemFold.open || systemFold.hasAttribute("open")) ? "1" : "0") : "";
  out["__body__"] = (d.body ? d.body.innerText : "");
  out["bodyClass"] = d.body ? d.body.className : "";
  var ns = d.getElementById("nativeState");
  out["nativeState"] = ns ? ns.textContent.trim() : "";
  var pre = document.createElement("pre");
  pre.id = "panel-mirror";
  pre.style.display = "none";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 5000);
</script>
"""

FIRST_SCREEN_IDS = [
    "stateLabel", "statusTime", "projectLine", "focusStatus", "focusDoing",
    "focusStep", "hierProject", "hierRoad", "hierWork", "sysLine",
    "intakeRoute", "needText", "systemDetail",
]
ADV_SCREEN_IDS = FIRST_SCREEN_IDS + [
    "pmBriefingBox", "acceptBox", "pmBriefingFoldOpen",
    "relationshipGoalsBox", "outcomeScoreBox",
]


def find_chromium():
    override = os.environ.get("AUTOPILOT_CHROMIUM")
    if override and Path(override).is_file():
        return override
    home = Path.home()
    patterns = (
        "Library/Caches/ms-playwright/chromium_headless_shell-*/"
        "chrome-headless-shell-mac-*/chrome-headless-shell",
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
    single_process = Path(chromium).name == "chrome-headless-shell"
    base = [chromium]
    if single_process:
        base.append("--single-process")
    base += ["--headless=new", "--disable-gpu", "--no-sandbox",
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

    def build_wrapper(name, state, query="", ids=None, open_fold=False,
                      route_payload=None):
        if route_payload is None:
            route_name = (state.get("project") or {}).get("name") or "当前绑定项目"
            route_payload = {
                "decision": "resolved",
                "project": route_name,
                "root": (state.get("project") or {}).get("path"),
                "reasons": ["原话点名了项目：knowledge-graph"],
                "__text__": "将落到：" + str(route_name),
            }
        path = workdir / name
        path.write_text(
            WRAPPER.replace("__QUERY__", query)
                   .replace("__OPEN_FOLD__", "true" if open_fold else "false")
                   .replace("__IDS__", json.dumps(ids or FIRST_SCREEN_IDS))
                   .replace("__ROUTE_PAYLOAD__", json.dumps(route_payload, ensure_ascii=False))
                   .replace("__PAYLOAD__", json.dumps(state, ensure_ascii=False)),
            encoding="utf-8")
        return path

    def render_snapshot(wrapper, label):
        proc = _run(chromium, workdir, ["--virtual-time-budget=9000", "--dump-dom",
                                        "--allow-file-access-from-files",
                                        wrapper.as_uri()])
        if not (proc.stdout or "").strip() and proc.returncode != 0:
            candidates = find_chromium()
            candidates = [c for c in candidates if c != chromium] if isinstance(candidates, list) else []
            for fallback in candidates:
                print(f"  ! {Path(chromium).name} 启动失败，改用 {Path(fallback).name} 重试")
                proc = _run(fallback, workdir, ["--virtual-time-budget=9000", "--dump-dom",
                                                "--allow-file-access-from-files",
                                                wrapper.as_uri()])
                if (proc.stdout or "").strip():
                    break
        dom = proc.stdout or ""
        m = re.search(r'id="panel-mirror"[^>]*>(.*?)</pre>', dom, re.S)
        result = {}
        if m:
            raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                   .replace("&lt;", "<").replace("&gt;", ">"))
            try:
                result = json.loads(raw)
            except Exception:
                result = {}
        if not result:
            print(f"{label}没有渲染出快照（浏览器可能启动失败）。stderr 摘要：")
            print((proc.stderr or "")[-600:])
        return result, dom, proc

    wrapper = build_wrapper("panel-harness.html", payload)
    snapshot, dom, dom_proc = render_snapshot(wrapper, "首屏")
    if not snapshot:
        return 1

    # 注入的 payload 和面板源码都在 <script> 里。比对前必须移除脚本，
    # 否则脚本中的答案会把「页面已渲染」伪装成通过。
    rendered = re.sub(r"<script\b[^>]*>.*?</script>", "", dom,
                      flags=re.IGNORECASE | re.DOTALL)

    def visible(text):
        if not text:
            return False
        return any(text in v for v in snapshot.values())

    # 面板的「正在做什么」有固定优先级：人工验收 > 自动实现 > 工作项 > 卡点 >
    # 控制层 doing。门禁必须照同一优先级算期望，并只核对 focusDoing；
    # 在全文里搜字符串会放过「期望文案出现在别处、真正的位置却错了」。
    def expected_doing_text(state):
        human = state.get("human_acceptance") or []
        if human:
            return "等待你验收：" + (human[0].get("title") or "人工验收")
        exec_now = state.get("exec_now")
        if exec_now:
            return ((exec_now.get("id") + " · ") if exec_now.get("id") else "") + \
                   (exec_now.get("title") or "正在自动实现")
        doing_items = state.get("doing_items") or []
        if doing_items:
            return doing_items[0].get("title") or doing_items[0].get("detail") or \
                   "正在处理工作项"
        blocked = state.get("blocked") or {}
        if any(blocked.get(k) for k in ("failures", "resolve", "exec")):
            return "等待处理卡点"
        return state.get("doing") or "当前没有进行中的工作"

    def visible_doing(text):
        return str(snapshot.get("focusDoing") or "").strip() == text

    checks = [
        ("首屏显示绑定项目名", project.get("name")),
        ("首屏显示绑定项目路径", project.get("path")),
        ("输入落点预览显示将落到项目",
         "将落到：" + str(project.get("name") or "")),
        ("首屏显示总状态文案", payload.get("state_label")),
        # 状态管家的结论必须真的上屏：这句以前只写进日志，用户看不到，
        # 于是「harness 健康吗」在面板上永远是个盲区。
        ("首屏显示上下文健康结论",
         (payload.get("harness_health") or {}).get("headline")
         or ("上下文健康：" + ((payload.get("harness_health") or {}).get("reason") or "取不到"))),
        # 三问②的显示优先级与 plugin/ui/panel.html setFocusAnswers 一致。
        ("首屏显示正在做什么", expected_doing_text(payload)),
        ("首屏显示原生直连结论", (payload.get("native") or {}).get("label")),
        ("有「项目文件夹」区块", "项目文件夹"),
        ("有「需要你介入」区块", "需要你介入"),
        ("有「工作项走到哪了」区块", "工作项走到哪了"),
        ("有「工程详情与证据」区块", "工程详情与证据"),
    ]
    folders = ((payload.get("project_folders") or {}).get("folders") or [])
    # 该状态根没有雷达/门禁数据时，这两条不是「面板没显示」，而是「没东西可显示」。
    # 以前会把 None 当成期望文本去比对，必然失败——门禁自己造了一个永远红的假红灯。
    # 没有数据就如实跳过并打印 SKIP，既不假通过，也不假失败。
    skipped = []
    radar_folder = next((f for f in folders if (f.get("engineering_radar") or {}).get("layers")), None)
    gate_folder = next((f for f in folders if (f.get("quality_gate") or {}).get("checks")), None)
    if radar_folder:
        radar = radar_folder["engineering_radar"]
        checks.append(("项目文件夹显示 19 层工程进度",
                       "19 层工程进度 " + str(radar.get("overall") or "")))
        if radar.get("generated_at"):
            checks.append(("项目文件夹显示工程快照日期",
                           "快照 " + str(radar["generated_at"])[:10]))
        if radar.get("next_steps"):
            checks.append(("项目文件夹显示工程下一步", "下一步：" + str(radar["next_steps"][0])))
        if radar.get("blockers"):
            checks.append(("项目文件夹显示工程卡点", "卡点：" + str(radar["blockers"][0])))
    else:
        skipped.append("项目文件夹显示 19 层工程进度（该状态根还没有工程雷达数据）")
    if gate_folder and gate_folder["quality_gate"].get("status"):
        gate_status = gate_folder["quality_gate"].get("status")
        gate_text = ("质量门禁：严格质量通过" if gate_status == "pass" else
                     "质量门禁：等待真人盲评" if gate_status == "human_pending" else
                     "质量门禁：门禁历史不可读" if gate_status == "unverified" else
                     "质量门禁：严格质量 RED")
        checks.append(("项目文件夹显示独立质量门禁状态", gate_text))
    else:
        skipped.append("项目文件夹显示独立质量门禁状态（该状态根还没有质量门禁数据）")
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

    for label in skipped:
        print(f"  SKIP  {label}")

    for label, expected in checks:
        ok = visible_doing(expected) if label == "首屏显示正在做什么" else visible(expected)
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}（期望文本：{expected!r}）")
            fails += 1

    # 面板层级：Codex 原生输入框是常用入口；面板输入框只在高级区兜底。
    # 系统判据默认折叠，但不能因为降噪把可观测性删掉。
    layer_checks = [
        ("面板输入框收进高级区", str(snapshot.get("intakeInAdvanced")) == "1"),
        ("默认首屏看不到面板输入框", str(snapshot.get("intakeVisible")) == "0"),
        ("系统判据详情默认折叠", str(snapshot.get("systemEvidenceOpen")) == "0"),
        ("系统判据详情仍可查", "可落盘" in str(snapshot.get("systemDetail") or "")),
        ("系统首屏不铺开队列读数", "可落盘" not in str(snapshot.get("sysLine") or "")),
        ("系统首屏不说 automation 黑话", "automation" not in str(snapshot.get("sysLine") or "")),
        ("系统首屏不说越线黑话", "越线" not in str(snapshot.get("sysLine") or "")),
    ]
    for label, ok in layer_checks:
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            fails += 1

    # 落点预览有三种真实返回，不能只锁住 resolved。这里直接给 route_preview
    # 注入真实文案，再在 iframe 里看 intakeRoute 的实际渲染结果。
    route_name = str(project.get("name") or "当前绑定项目")
    route_cases = [
        ("输入落点预览显示判不清落点",
         {"decision": "ambiguous", "project": None,
          "reasons": ["多个项目都可能命中"],
          "__text__": "判不清落点；提交会进全局待路由箱，请在原话里点名项目"},
         "判不清落点；提交会进全局待路由箱，请在原话里点名项目"),
        ("输入落点预览显示默认落点",
         {"decision": "none", "project": route_name,
          "root": project.get("path"),
          "reasons": ["只找到一个可落项目：默认落账"],
          "__text__": "默认落到当前绑定项目：" + route_name +
                      "；如不是，请在原话里点名项目"},
         "默认落到当前绑定项目：" + route_name +
         "；如不是，请在原话里点名项目"),
    ]
    for case_label, case_payload, expected_text in route_cases:
        case_wrapper = build_wrapper(
            "panel-route-" + str(case_payload["decision"]) + ".html",
            payload, route_payload=case_payload)
        case_snapshot, _, _ = render_snapshot(case_wrapper, case_label)
        if not case_snapshot:
            print(f"  FAIL  {case_label}（没有渲染出快照）")
            fails += 1
            continue
        got_text = str(case_snapshot.get("intakeRoute") or "").strip()
        if got_text == expected_text:
            print(f"  PASS  {case_label}")
        else:
            print(f"  FAIL  {case_label}（期望：{expected_text!r}；实际：{got_text!r}）")
            fails += 1

    # 高级区不能靠改真实 payload 上屏，否则会把 PM 简报/截图塞进首屏。
    # 这里用 fixture 单独渲染 ?adv=1 并展开 PM 折叠区，验证按钮和证据真的能上屏。
    adv_payload = dict(payload)
    adv_payload.update({
        "state": "blocked",
        "state_label": "需要你验收",
        "need_action": "需要你验收：真实桌面验收",
        "human_acceptance": [{
            "id": "A-9001", "title": "真实桌面验收",
            "steps": "1) 打开面板 2) 看复核截图",
            "screenshots": ["/tmp/first.png"],
        }],
        "pm_briefing": {
            "status": "attention",
            "expectations": {
                "status": "ok", "pending_count": 1, "active_count": 2,
                "mainline_stage": "架构闭环",
                "pending": [{"id": "CC-9001", "text": "绝不影响用户正在用的服务"}],
                "active": [], "missing": [],
            },
            "acceptance_criteria": {
                "status": "ok", "pending_count": 1,
                "items": [{
                    "id": "AC-9001",
                    "text": "落盘后必须能在面板看到闭环结果",
                    "needs_human": True,
                    "signals": ["必须能", "看到"],
                    "files": ["ops/kgctl.py"],
                }],
                "note": "确认后才登记验收项；机器项仍需真门禁证据。",
            },
            "overlap_holds": {
                "status": "ok", "items": [{
                    "id": "L-0021", "title": "撞文件的后开工项",
                    "holders": [{"id": "L-0020"}], "files": ["ops/truthctl.py"],
                }],
                "note": "正常排队，不计入卡住；前一项落地后自动重算。",
            },
            "follow_up_candidates": {
                "status": "ok", "items": [{
                    "id": "L-0021", "idle_minutes": 90, "thread_id": "thread-abc",
                }],
                "note": "只生成候选，不会自动追问；你点头才会发消息。",
                "missing": [],
            },
        },
        "relationship_goals": {
            "status": "ok", "level": "attention", "count": 1,
            "needs_human_count": 1,
            "headline": "跨项目目标 1 条，其中 1 条需要人判断",
            "note": "只记录你显式登记的目标；不会自动派活，也不会替你决定优先级。",
            "items": [{
                "id": "G-9001",
                "title": "把自动挡做成长期可信的个人助理",
                "priority": "high", "status": "waiting_human",
                "projects": ["codex-autopilot", "personal-memory"],
                "needs_human": True, "reason": "等用户判断要不要改产品方向",
                "north_star": "用户只说一次，跨项目长期记住并推进",
                "source_thread": "thread-relationship-20261118",
                "linked_ledger": ["L-0076"], "linked_acceptance": ["A-0069"],
                "updated_at": "2026-11-18T00:00:00Z",
            }],
        },
        "outcome_score": {
            "status": "ok", "score": 72, "grade": "有基础",
            "headline": "结果代理分 72/100（有基础；覆盖 100%）",
            "note": "这是台账结果代理分；机器命令通过不等于用户结果变好。",
            "components": [
                {"key": "closure", "label": "首问闭环", "status": "ok",
                 "score": 80.0, "weight": 35, "detail": "80.0%（4/5）"},
                {"key": "health", "label": "全局健康", "status": "unknown",
                 "score": None, "weight": 15, "detail": "取不到"},
            ],
            "missing": ["全局健康"], "shortfalls": [],
        },
    })
    adv_wrapper = build_wrapper(
        "panel-adv-harness.html", adv_payload, query="?adv=1",
        ids=ADV_SCREEN_IDS, open_fold=True)
    adv_snapshot, _, _ = render_snapshot(adv_wrapper, "高级区")
    if not adv_snapshot:
        fails += 1
    else:
        print("Codex 自动挡 高级区渲染检查")
        pm_box = str(adv_snapshot.get("pmBriefingBox") or "")
        accept_box = str(adv_snapshot.get("acceptBox") or "")
        rel_box = str(adv_snapshot.get("relationshipGoalsBox") or "")
        outcome_box = str(adv_snapshot.get("outcomeScoreBox") or "")
        adv_checks = [
            ("高级区由 ?adv=1 打开", "adv" in str(adv_snapshot.get("bodyClass") or "")),
            ("PM 简报折叠区实际展开", str(adv_snapshot.get("pmBriefingFoldOpen")) == "1"),
            ("PM 简报上屏：待确认预期",
             "待确认" in pm_box and "绝不影响用户正在用的服务" in pm_box),
            ("PM 简报上屏：验收标准候选",
             "验收标准" in pm_box and "AC-9001" in pm_box
             and "需要人看" in pm_box),
            ("PM 简报上屏：让路与追问候选",
             "L-0021" in pm_box and "thread-abc" in pm_box),
            ("关系层上屏：跨项目目标与需要人的原因",
             "跨项目目标" in rel_box and "G-9001" in rel_box
             and "等用户判断要不要改产品方向" in rel_box),
            ("关系层上屏：北极星与关联工作项",
             "北极星" in rel_box and "用户只说一次，跨项目长期记住并推进" in rel_box
             and "工作项 L-0076" in rel_box and "验收 A-0069" in rel_box),
            ("关系层上屏：来源窗口", "来源窗口" in rel_box
             and "thread-relationship-20261118" in rel_box),
            ("关系层明示不会自动派活", "不会自动派活" in rel_box),
            ("结果代理分上屏：总分、判据与边界",
             "结果代理分 72/100" in outcome_box
             and "机器命令通过不等于用户结果变好" in outcome_box
             and "全局健康" in outcome_box),
            ("人工验收上屏：复核截图与补图按钮",
             "真实桌面验收" in accept_box and "/tmp/first.png" in accept_box
             and "补一张复核截图" in accept_box),
        ]
        for label, ok in adv_checks:
            if ok:
                print(f"  PASS  {label}")
            else:
                print(f"  FAIL  {label}")
                fails += 1

    if want_shot:
        # 不可变的发布目录里没有 docs/（源码仓才有）。截图是验证产物，不是源码：
        # 缺目录就建，别把「发行版布局不同」误报成「面板没渲染出来」。
        try:
            SHOT.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
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
