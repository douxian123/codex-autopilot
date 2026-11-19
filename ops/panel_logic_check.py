#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板首屏逻辑检查：把面板脚本真跑一遍，核对首屏每个值都来自真实状态。

面板渲染发生在 Codex 宿主里（iframe + postMessage），静态检查看不出来它到底把
哪个字段写到了哪一行。这里用 Node 起一个最小 DOM，把面板脚本原样执行，再按
Codex 宿主的同一条通知把真实状态喂进去，最后把首屏各行的实际文本取出来和
控制层数据逐条对照。

它不是浏览器渲染（布局/样式不参与），而是「首屏数据流」的真执行验证；
排版与观感由 ops/panel_render_check.py 在真实桌面环境里补。

退出码 = 未通过项数；找不到 node 时退出码 2 并如实说明。
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

HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// ---- 最小 DOM：只实现面板脚本真正用到的那几个 API ----
const NOOP = function () {};
const els = new Map();
function escapeAttr(v) {
  return String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function tagOf(n) {
  const m = String((n && n.id) || "").match(/^<([a-zA-Z0-9]+)>/);
  return m ? m[1] : "div";
}
function serializeEl(n) {
  if (!n) return "";
  const attrs = [];
  if (n.className) attrs.push('class="' + escapeAttr(n.className) + '"');
  if (n.title) attrs.push('title="' + escapeAttr(n.title) + '"');
  if (n["aria-label"]) attrs.push('aria-label="' + escapeAttr(n["aria-label"]) + '"');
  if (n["data-key"]) attrs.push('data-key="' + escapeAttr(n["data-key"]) + '"');
  const kids = (n.children || []).map(serializeEl).join("");
  const body = kids || ((n.children || []).length ? "" : escapeAttr(n.textContent || ""));
  return "<" + tagOf(n) + (attrs.length ? " " + attrs.join(" ") : "") + ">" + body + "</" + tagOf(n) + ">";
}
function makeEl(id) {
  const base = {
    id: id || "",
    textContent: "", innerHTML: "", className: "", title: "",
    value: "", checked: false, hidden: false, disabled: false,
    style: {}, children: [],
    _clickHandlers: [],
    classList: { toggle: NOOP, add: NOOP, remove: NOOP, contains: function () { return false; } },
    setAttribute: function (k, v) { this[k] = String(v); },
    getAttribute: function (k) { return this[k]; },
    removeAttribute: NOOP, removeEventListener: NOOP,
    addEventListener: function (type, fn) {
      if (type === "click" && typeof fn === "function") this._clickHandlers.push(fn);
    },
    appendChild: function (c) {
      this.children.push(c);
      this.textContent = (this.textContent || "") + ((c && c.textContent) || "");
      return c;
    },
    insertBefore: function (c) { this.children.push(c); return c; },
    removeChild: NOOP, remove: NOOP, focus: NOOP, click: NOOP,
    querySelectorAll: function () { return []; }, querySelector: function () { return null; },
    getBoundingClientRect: function () { return { width: 0, height: 0, top: 0, left: 0 }; },
    scrollIntoView: NOOP, replaceChildren: NOOP,
  };
  var _html = "";
  Object.defineProperty(base, "innerHTML", {
    get: function () { return _html; },
    set: function (v) {
      _html = v;
      base.children = [];
      base.textContent = "";
    },
  });
  return new Proxy(base, {
    get(t, p) {
      if (p in t) return t[p];
      if (typeof p === "symbol") return undefined;
      return NOOP;                 // 未知成员按“无副作用的空方法”处理
    },
    set(t, p, v) { t[p] = v; return true; },
  });
}

const document = {
  body: makeEl("body"),
  documentElement: makeEl("html"),
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement(tag) { return makeEl("<" + tag + ">"); },
  createDocumentFragment() { return makeEl("<frag>"); },
  addEventListener: NOOP,
  removeEventListener: NOOP,
};

const handlers = [];
const window = {
  addEventListener(type, fn) { handlers.push({ type, fn }); },
  removeEventListener: NOOP,
  postMessage: NOOP,
  location: { search: "" },
  matchMedia() { return { matches: false, addEventListener: NOOP }; },
};
window.parent = window;
const toolCalls = [];
window.postMessage = function (msg) {
  if (msg && msg.method === "tools/call" && msg.params) toolCalls.push(msg.params);
};

globalThis.window = window;
globalThis.document = document;
globalThis.location = window.location;
globalThis.navigator = { clipboard: { writeText() { return Promise.resolve(); } } };
globalThis.setTimeout = function () { return 0; };
globalThis.setInterval = function () { return 0; };
globalThis.clearTimeout = NOOP;
globalThis.URL = { createObjectURL() { return ""; }, revokeObjectURL: NOOP };
globalThis.Blob = function () {};

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(function (m) { return m[1]; });
if (!scripts.length) { console.log(JSON.stringify({ error: "面板里没有脚本" })); process.exit(0); }
(new Function(scripts[scripts.length - 1]))();

const msg = handlers.find(function (h) { return h.type === "message"; });
if (!msg) { console.log(JSON.stringify({ error: "面板没有监听宿主消息" })); process.exit(0); }
const alertText = makeEl("alert-text");
document.getElementById("need").querySelector = function (selector) {
  return selector === ".al-tx" ? alertText : null;
};

function findButton(root, label) {
  if (!root) return null;
  // 最小 DOM 的 createElement 把标签写进 id（如 "<button>"），没有 tag 属性。
  if (String(root.id || "").indexOf("button") >= 0
      && root.textContent === label) return root;
  for (const child of (root.children || [])) {
    const found = findButton(child, label);
    if (found) return found;
  }
  return null;
}

// 截图按钮的点击必须发到 capture_acceptance_screenshot，并显式带 confirm=true。
// 没有 confirm 时 MCP 层会拒绝，因此这里不是「看文案」，是真点按钮取工具参数。

// 走 Codex 宿主同一条通知：structuredContent 就是控制层状态
msg.fn({
  source: window,
  data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
          params: { structuredContent: payload } },
});

// 真点一次「补一张复核截图」：只验证面板发出的工具调用，
// 不调用系统截图器、不写验收台账。按钮没接上时下面三个字段会是空。
var shotButton = findButton(document.getElementById("acceptBox"), "补一张复核截图");
if (shotButton) {
  shotButton._clickHandlers.slice().forEach(function (fn) { fn(); });
}

// 验收标准候选的「确认 / 不算」也必须真点一次：只检查屏幕文字，挡不住按钮没接线。
var acPromoteButton = findButton(document.getElementById("blockedList"), "确认成验收项");
if (acPromoteButton) {
  acPromoteButton._clickHandlers.slice().forEach(function (fn) { fn(); });
}
var acDismissButton = findButton(document.getElementById("blockedList"), "不算");
if (acDismissButton) {
  acDismissButton._clickHandlers.slice().forEach(function (fn) { fn(); });
}
// PM 追问授权也必须真点按钮：候选按钮要带 confirm=true，
// 撤销按钮要带 request_id + reason；只看文案挡不住按钮没接线。
var followAuthorizeButton = findButton(document.getElementById("pmBriefingBox"), "授权追问");
if (followAuthorizeButton) {
  followAuthorizeButton._clickHandlers.slice().forEach(function (fn) { fn(); });
}
var followCancelButton = findButton(document.getElementById("pmBriefingBox"), "撤销授权");
if (followCancelButton) {
  followCancelButton._clickHandlers.slice().forEach(function (fn) { fn(); });
}

const read = function (id) { return document.getElementById(id).textContent; };
console.log(JSON.stringify({
  ok: true,
  captureToolName: (toolCalls.find(function (c) {
    return c.name === "capture_acceptance_screenshot";
  }) || {}).name || "",
  captureToolId: (((toolCalls.find(function (c) {
    return c.name === "capture_acceptance_screenshot";
  }) || {}).arguments) || {}).id || "",
  captureToolConfirm: (((toolCalls.find(function (c) {
    return c.name === "capture_acceptance_screenshot";
  }) || {}).arguments) || {}).confirm,
  acceptancePromoteCalls: toolCalls.filter(function (c) {
    return c.name === "acceptance_criteria_promote";
  }).map(function (c) { return c.arguments || {}; }),
  acceptanceDismissCalls: toolCalls.filter(function (c) {
    return c.name === "acceptance_criteria_dismiss";
  }).map(function (c) { return c.arguments || {}; }),
  followupAuthorizeCalls: toolCalls.filter(function (c) {
    return c.name === "followup_authorize";
  }).map(function (c) { return c.arguments || {}; }),
  followupCancelCalls: toolCalls.filter(function (c) {
    return c.name === "followup_cancel";
  }).map(function (c) { return c.arguments || {}; }),
  projectLine: read("projectLine"),
  projectTitle: document.getElementById("projectLine").title,
  hierarchyProject: read("hierProject"),
  hierarchyRoad: read("hierRoad"),
  hierarchyWork: read("hierWork"),
  sysLine: read("sysLine"),
  folderBox: read("folderBox"),
  folderSerialized: serializeEl(document.getElementById("folderBox")),
  stateLabel: read("stateLabel"),
  subtitle: read("subtitle"),
  q1State: read("q1State"),
  masterState: read("masterState"),
  focusStatus: read("focusStatus"),
  focusDoing: read("focusDoing"),
  focusStep: read("focusStep"),
  northStarValue: read("nsValue"),
  northStarDetail: read("nsDetail"),
  northStarClass: document.getElementById("northStar").className,
  needText: alertText.textContent,
  needTitle: document.getElementById("need").title || "",
  needAria: document.getElementById("need").getAttribute("aria-label") || "",
  needEvidence: read("needEvidenceBox"),
  needEvidenceHidden: document.getElementById("needEvidenceFold").hidden,
  systemDetail: read("systemDetail"),
  nativeState: read("nativeState"),
  received: read("cReceived"),
  landned: read("cLanded"),
  resolved: read("cResolved"),
  stReview: read("stReview"),
  stDoing: read("stDoing"),
  recentBox: read("recentBox"),
  recentNote: read("recentNote"),
  recentSerialized: serializeEl(document.getElementById("recentBox")),
  acceptBox: read("acceptBox"),
  blockedList: read("blockedList"),
  blockedHtml: document.getElementById("blockedList").innerHTML,
  workTableBox: read("workTableBox"),
  workTableHidden: document.getElementById("workTableFold").hidden,
  needClass: document.getElementById("need").className,
  recentHtml: document.getElementById("recentBox").innerHTML,
  routeText: read("routeBox"),
  stewardBar: read("stewardBar"),
  stewardHtml: serializeEl(document.getElementById("stewardBar")),
  autonomyLine: read("autonomyLine"),
  autonomyAudit: read("autonomyAudit"),
  pmBriefingBox: read("pmBriefingBox") + " " + document.getElementById("pmBriefingBox").innerHTML,
  relationshipGoalsBox: read("relationshipGoalsBox") + " "
    + document.getElementById("relationshipGoalsBox").innerHTML,
  outcomeScoreBox: read("outcomeScoreBox") + " "
    + document.getElementById("outcomeScoreBox").innerHTML,
}));
"""


def find_node():
    return shutil.which("node")


def main(argv):
    quiet = "--quiet" in argv
    node = find_node()
    if not node:
        print("找不到 node，面板首屏逻辑检查跳过。")
        return 2

    sys.path.insert(0, str(OPS))
    os.environ.setdefault("KG_REPO", str(REPO))
    import central_mcp

    payload = central_mcp._status_payload(fast=True)
    project = payload.get("project") or {}
    counters = payload.get("counters") or {}
    human_acc = payload.get("human_acceptance") or []

    workdir = Path(tempfile.mkdtemp(prefix="autopilot-panel-logic-"))
    try:
        harness = workdir / "harness.js"
        fixture = workdir / "payload.json"
        harness.write_text(HARNESS, encoding="utf-8")

        def run_once(state):
            """把一份状态喂给面板脚本，取回首屏实际文本。"""
            fixture.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            proc = subprocess.run([node, str(harness), str(PANEL), str(fixture)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError("面板脚本执行失败：" + (proc.stderr or "")[-600:])
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise RuntimeError("面板脚本没有回传结果：" + (proc.stdout or "")[-300:])
            if data.get("error"):
                raise RuntimeError("检查失败：" + data["error"])
            return data

        try:
            out = run_once(payload)
        except RuntimeError as exc:
            print(exc)
            return 1

        def document_empty(out):
            """空态是 innerHTML 写的，textContent 读不到。"""
            return out.get("recentHtml") or ""

        def has(label, text, *needles):
            # 判据是「这个值有没有出现在首屏」，0 和空串都是合法期望值，不能用真值判断。
            missing = [n for n in needles if n is None or str(n) not in str(text)]
            if missing:
                print(f"  FAIL  {label}（{text!r} 里缺 {missing}）")
                return 1
            print(f"  PASS  {label}")
            return 0

        def lacks(label, text, *needles):
            found = [n for n in needles if str(n) in str(text)]
            if found:
                print(f"  FAIL  {label}（{text!r} 里仍有 {found}）")
                return 1
            print(f"  PASS  {label}")
            return 0

        def human_recent(text):
            """镜像面板的最近收口文案：正文说需求与结果，ID 只留在 title/aria。"""
            value = str(text or "").strip()
            value = re.sub(r"^[A-Z]+-\d+\s+已收口：\s*", "", value)
            value = re.sub(r"^已收口：\s*", "", value)
            if value.endswith("已合入"):
                return "代码已合入"
            topic_box = re.match(r"^【([^】]+)】", value)
            topic = topic_box.group(1) if topic_box else ""
            rest = value[topic_box.end():] if topic_box else value
            short_topic = topic.split("·")[-1] if topic else ""
            combined = topic + rest
            if "面板首屏" in combined or "首屏只回答" in combined:
                return "面板首屏：改为只看三问"
            if "防丢失" in combined or "真相镜像" in combined:
                return "防丢失：落盘状态已和主干同步"
            if "门禁环境" in combined:
                return "门禁环境：可直接验证发布目录"
            rest = re.sub(
                r"(?:~|/Users/|/private/|/var/folders/)[^\s，,；;]+", "", rest)
            rest = re.sub(r"kgctl\s+land", "落盘", rest, flags=re.I)
            rest = re.sub(r"progress\.json\s*/\s*acceptance\.json",
                          "状态台账", rest, flags=re.I)
            rest = re.sub(r"\bmain\b", "主干", rest, flags=re.I)
            rest = re.sub(r"\s+", " ", rest).strip(" :：")
            if short_topic and rest:
                return short_topic + "：" + rest
            if short_topic:
                return short_topic + "：已完成"
            return rest[:42] + "…" if len(rest) > 42 else (rest or "一项工作已完成")

        def hierarchy_work_text(state):
            """镜像面板的工作项优先级，别把现场状态写死成某一种空态。"""
            doing_items = state.get("doing_items") or []
            todo_items = state.get("todo_items") or []
            current = doing_items[0] if doing_items else state.get("exec_now")
            if current is None and todo_items:
                current = todo_items[0]
            if current is not None:
                if isinstance(current, dict):
                    return current.get("title") or current.get("detail") or "正在处理"
                return "正在处理"
            if state.get("human_acceptance") or []:
                return "等待人工验收"
            if state.get("state") in ("blocked", "error"):
                return "等待解除卡点"
            return "等待下一条需求"

        fails = 0
        if not quiet:
            print("Codex 自动挡 面板首屏逻辑检查")
        fails += has("首屏显示绑定项目（名字+路径）", out["projectLine"],
                     project.get("name"), project.get("path"))
        fails += has("绑定项目路径进了 title（悬停可看全）", out["projectTitle"], project.get("path"))
        mainline = payload.get("mainline") or {}
        roadmap = payload.get("roadmap") or []
        current_road = next((item for item in roadmap if item.get("status") == "doing"), None)
        current_road = current_road or next((item for item in roadmap if item.get("status") == "next"), None)
        expected_stage = (current_road or {}).get("step") or "路线待拆解"
        expected_product = mainline.get("product") or project.get("name") or "当前项目"
        expected_project = project.get("name") or str(project.get("path") or "").rstrip("/").split("/")[-1] or "未绑定项目"
        fails += has("路线图显示项目归属", out["hierarchyProject"], expected_project)
        fails += has("路线图显示产品路线与当前阶段", out["hierarchyRoad"], expected_product, expected_stage)
        expected_work = hierarchy_work_text(payload)
        fails += has("路线图显示当前工作项", out["hierarchyWork"], expected_work)
        # 上面那条跟随现场状态；再把两条空态分支单独钉住，避免现场恰好只有一种
        # 状态时另一种分支腐化而无人发现。
        blocked_empty = dict(payload)
        blocked_empty.update({
            "doing_items": [], "todo_items": [], "exec_now": None,
            "human_acceptance": [], "state": "blocked",
        })
        blocked_out = run_once(blocked_empty)
        fails += has("路线图卡点空态显示等待解除卡点",
                     blocked_out["hierarchyWork"], "等待解除卡点")
        idle_empty = dict(blocked_empty, state="ok")
        idle_out = run_once(idle_empty)
        fails += has("路线图普通空态显示等待下一条需求",
                     idle_out["hierarchyWork"], "等待下一条需求")
        folders = ((payload.get("project_folders") or {}).get("folders") or [])
        if folders:
            folder = folders[0]
            folder_items = (folder.get("doing_items") or folder.get("todo_items") or [])
            fails += has("项目文件夹显示项目名", out["folderBox"], folder.get("name"))
            fails += has("项目文件夹显示当前步骤", out["folderBox"], folder.get("current_step"))
            if folder_items:
                fails += has("项目文件夹列出工作项", out["folderBox"],
                             folder_items[0].get("title") or folder_items[0].get("detail"),
                             "进行中" if folder.get("doing_items") else "待处理")
                item_id = str(folder_items[0].get("id") or "")
                item_source = str(folder_items[0].get("source_thread_id") or "")
                if item_id:
                    fails += lacks("项目文件夹正文不露工作项 ID", out["folderBox"], item_id)
                    fails += has("项目文件夹 ID 保留在 title", out["folderSerialized"],
                                 "工作项 " + item_id)
                if item_source:
                    fails += lacks("项目文件夹正文不露来源对话 ID", out["folderBox"], item_source)
                    fails += has("项目文件夹来源对话保留在 title", out["folderSerialized"],
                                 "来源对话 " + item_source)
        fails += has("首屏显示总状态文案", out["stateLabel"], payload.get("state_label"))
        if payload.get("enabled"):
            # 有待人工验收项时，首屏状态会叠加「等你验收」（见下方 review 分支），
            # 这里只校验开启语义；真正关闭态由后面的 closed 分支独立强校验。
            fails += has("一眼三问：状态", out["focusStatus"], "运行中")
        else:
            fails += has("一眼三问：状态", out["focusStatus"], "已关闭")
        # 「现在在做」必须与控制层/面板的优先级阶梯逐级一致。旧断言把 todo_items
        # 也当成「正在做」，于是逼面板在没有任何 doing 时谎报第一条待办；而面板与
        # panel_render_check 都要求此时显示控制层权威结论。新版严格镜像面板顺序
        # （人工验收 → exec_now → doing_items → 卡点 → 控制层 doing），并把最后
        # 的兜底从「子串包含」升级为「逐字相等」——是加强，不是放宽。
        doing_items = payload.get("doing_items") or []
        exec_now = payload.get("exec_now")
        blocked_state = payload.get("blocked") or {}
        has_block = bool(
            (blocked_state.get("failures") or []) or (blocked_state.get("resolve") or [])
            or (blocked_state.get("exec") or []) or (blocked_state.get("port") or []))
        if human_acc:
            fails += has("一眼三问：等你验收优先显示", out["focusDoing"],
                         human_acc[0].get("title") or "人工验收")
        elif exec_now:
            fails += has("一眼三问：正在自动实现时显示该工单", out["focusDoing"],
                         (exec_now.get("title") or "正在自动实现"))
        elif doing_items:
            fails += has("一眼三问：现在在做", out["focusDoing"],
                         doing_items[0].get("title") or doing_items[0].get("detail"))
        elif has_block:
            fails += has("一眼三问：有卡点时显示等待处理卡点", out["focusDoing"],
                         "等待处理卡点")
        else:
            # 没有在做、也没有卡点时，首屏必须原样显示控制层算出的权威 doing，
            # 而不是面板自己写死或用 todo 顶替。控制层在这里已经区分了「守护绑定
            # 别的项目」「已检查一轮没有可合分支」「暂停/关闭」等情况；面板旧实现
            # 硬写「当前没有进行中的工作」，于是控制层说了实话、面板显示空白态
            # （panel-render 门禁实测抓到）。这里要求逐字相等，比旧的子串包含更严。
            expected_doing = payload.get("doing") or "没有进行中"
            actual_doing = str(out["focusDoing"])
            if actual_doing == expected_doing:
                print("  PASS  一眼三问：无工作项时逐字显示控制层权威结论")
            else:
                print("  FAIL  一眼三问：无工作项时未逐字显示控制层权威结论"
                      f"（期望 {expected_doing!r}，实际 {actual_doing!r}）")
                fails += 1
        fails += has("一眼三问：当前步骤", out["focusStep"], expected_stage)
        # 这段无条件跑：不能因为当前项目恰好没有待验收项，就把「验收项必须带出口」
        # 这条判据跳过——那正是让验收项又悄悄退回「只有步骤、没有动作」的空档。
        review = dict(payload)
        review["state"] = "blocked"
        review["state_label"] = "需要你验收"
        review["need_action"] = "需要你验收：真实桌面验收"
        review["human_acceptance"] = [{"id": "A-9001", "title": "真实桌面验收"}]
        review_out = run_once(review)
        fails += has("人工验收升级为明确状态", review_out["stateLabel"], "需要你验收")
        fails += has("人工验收在提示条里明确出现", review_out["needText"], "需要你验收")
        fails += has("有验收时第一问摘要明确要你验收", review_out["q1State"], "要你验收")
        decision = dict(payload)
        decision.update({
            "state": "blocked",
            "state_label": "需要你拍板",
            "need_action": "需要你拍板：分支路径冲突",
            "human_acceptance": [],
        })
        decision_out = run_once(decision)
        fails += has("有决策时第一问摘要明确要你拍板", decision_out["q1State"], "要你拍板")
        # 只列复核步骤、不给出口，是验收项长期挂着的直接原因；每条必须带
        # 「点它会怎样 / 不点会怎样」和一个真的能收口的按钮。
        fails += has("验收项带可点的收口动作", review_out["acceptBox"], "我确认通过")
        fails += has("验收项写明不点的后果", review_out["acceptBox"], "不点就继续")
        # 截图动作必须能点，且点击后真的发到 MCP；只写按钮文案不算闭环。
        fails += has("验收项带可点的补图动作", review_out["acceptBox"], "补一张复核截图")
        fails += has("补图动作写明不会替人收口", review_out["acceptBox"],
                     "补图只追加附件", "不会替你收口")

        # 守护绑定错位曾经把两个绝对路径直接铺在首屏，用户一眼只看到工程内部细节。
        # 这里验证：首屏只留后果，完整路径仍在 title / aria-label / 工程详情里。
        daemon_bound = "/tmp/autopilot-logic-bound-project"
        daemon_current = "/tmp/autopilot-logic-current-project"
        daemon_detail = "绑定的是 " + daemon_bound + "，不是 " + daemon_current
        dm = dict(payload)
        dm.update({
            "state": "blocked",
            "state_label": "需要你处理",
            "need_action": "后台现在在服务别的项目；当前项目要等自己的轮次。",
            "need_action_detail": daemon_detail,
        })
        dout = run_once(dm)
        fails += has("守护异常首屏只显示用户结论", dout["needText"],
                     "后台现在在服务别的项目", "当前项目要等自己的轮次")
        if daemon_bound in str(dout["needText"]):
            fails += 1
            print("  FAIL  守护绑定路径不该进首屏正文")
        else:
            print("  PASS  守护绑定路径不进首屏正文")
        fails += has("守护绑定路径仍可从 title 取到", dout["needTitle"], daemon_bound)
        fails += has("守护绑定路径仍可从 aria-label 取到", dout["needAria"], daemon_bound)
        fails += has("守护详情进入折叠区", dout["needEvidence"], daemon_bound, daemon_current)
        fails += has("守护异常时第一问摘要给出等轮次", dout["q1State"], "等自己的轮次")
        if "不用你" in str(dout["q1State"]):
            fails += 1
            print("  FAIL  守护异常与“不用你”摘要自相矛盾")
        else:
            print("  PASS  守护异常与摘要保持一致")
        if dout.get("needEvidenceHidden") is False:
            print("  PASS  有详情时工程详情折叠区可见")
        else:
            fails += 1
            print("  FAIL  有详情时工程详情折叠区被隐藏")
        clean = dict(payload)
        clean.update({
            "need_action": "不需要你做什么，继续照常提需求就行",
            "need_action_detail": "",
        })
        clean_out = run_once(clean)
        fails += has("无需处理时第一问摘要写不用你", clean_out["q1State"], "不用你")
        say = dict(clean)
        say["need_action"] = "有新需求还不知道归到哪个项目，需要你说一句"
        say_out = run_once(say)
        fails += has("第一问摘要明确要说一句", say_out["q1State"], "要说一句")
        if clean_out.get("needEvidenceHidden") is True:
            print("  PASS  无详情时工程详情折叠区不占空间")
        else:
            fails += 1
            print("  FAIL  无详情时工程详情折叠区仍显示")
        fails += has("补图按钮发起截图工具调用", review_out.get("captureToolName", ""),
                     "capture_acceptance_screenshot")
        fails += has("补图按钮带真实验收 id", review_out.get("captureToolId", ""),
                     "A-9001")
        if review_out.get("captureToolConfirm") is not True:
            print("  FAIL  补图按钮必须显式带 confirm=true")
            fails += 1
        else:
            print("  PASS  补图按钮必须显式带 confirm=true")
        # 人工验收必须能把「机器已看过的现场」带给用户。截图只是复核附件，
        # 不参与自验 gate：没有 evidence 的项仍留在待自验。
        shots = dict(payload)
        shots["state"] = "blocked"
        shots["state_label"] = "需要你验收"
        shots["need_action"] = "需要你验收：看截图"
        shots["human_acceptance"] = [{
            "id": "A-9002", "title": "带截图的验收项",
            "screenshots": ["/tmp/first.png", "/tmp/second.png"],
        }]
        shots_out = run_once(shots)
        fails += has("验收项展示复核截图", shots_out["acceptBox"],
                     "复核截图", "/tmp/first.png", "/tmp/second.png")
        fails += has("截图按钮调用 capture_acceptance_screenshot",
                     shots_out["captureToolName"], "capture_acceptance_screenshot")
        fails += has("截图按钮带上验收 id", shots_out["captureToolId"], "A-9002")
        fails += has("截图按钮显式 confirm=true",
                     str(shots_out["captureToolConfirm"]), "True")
        # 二级工单表：每条必须把「来源 → 项目 → 泳道 → 分支 → stage → 证据」
        # 串出来，stage 用控制层算好的值照抄。只显示一个标题等于没回答
        # 「这条到底走到哪了、依据是什么」。
        wrows = dict(payload)
        wrows["work_rows"] = [
            {"id": "L-9001", "title": "真实工单标题", "kind": "bug",
             "source": "inbox", "project": "demo-project", "lane": "codex/lane-x",
             "branch": "codex/lane-x", "stage": "doing", "stage_label": "进行中",
             "evidence": "npm run build -> exit 0",
             "acceptance_summary": "验收标准：机器 2 条 / 人工 1 条"},
            {"id": "A-9002", "title": "真实待验收标题", "kind": "acceptance",
             "source": "kgctl land", "project": "demo-project", "lane": "codex/lane-y",
             "branch": "codex/lane-y-impl", "stage": "person/review",
             "stage_label": "需要你验收",
             "evidence": ""},
        ]
        wout = run_once(wrows)
        fails += has("工单表列出工作项标题", wout["workTableBox"], "真实工单标题", "真实待验收标题")
        fails += has("工单表带来源与归属项目", wout["workTableBox"], "来源 inbox", "项目 demo-project")
        fails += has("工单表带泳道与分支（同值不重复展示）", wout["workTableBox"],
                     "泳道 codex/lane-x", "分支 codex/lane-y-impl")
        fails += has("工单表 stage 用控制层算好的文案", wout["workTableBox"], "进行中", "需要你验收")
        fails += has("工单表带证据链", wout["workTableBox"], "证据：npm run build -> exit 0")
        fails += has("工单表带验收标准摘要", wout["workTableBox"],
                     "验收标准：机器 2 条 / 人工 1 条")
        wempty = dict(payload)
        wempty["work_rows"] = []
        eout = run_once(wempty)
        fails += 0 if eout["workTableHidden"] is True else has(
            "没有工单时不占空间（折叠隐藏）", str(eout["workTableHidden"]), "True")

        # 卡点区块同一个毛病：只列出「什么卡住了」，用户看到了也推不动。
        # 每条卡点必须带一个真的能点的下一步，并写清点了会怎样 / 不点会怎样。
        blk = dict(payload)
        blk["blocked"] = {
            "failures": [{"target": "codex/lane-z", "kind": "gate_fail"}],
            "resolve": [{"branch": "codex/lane-z", "reason": "churn"}],
            "exec": [{"id": "E-9001", "reason": "未授权"}],
            "port": [],
        }
        bout = run_once(blk)
        fails += has("卡点带可点的下一步动作", bout["blockedList"], "立刻再检查一轮")
        fails += has("冲突暂缓给放宽闸门的入口", bout["blockedList"], "放宽闸门再拆一次")
        fails += has("卡点写明不点的后果", bout["blockedList"], "不点就继续")
        bempty = dict(payload)
        bempty.update({
            "blocked": {"failures": [], "resolve": [], "exec": [], "port": []},
            "error": None,
            # 这三块共用 blockedList：不清空现场候选，空态断言会被真候选污染，
            # 上一次 land 因此在 trunk 上假红。逐项清空后才是确定性的空态夹具。
            "harness_tune": {"status": "ok", "count": 0, "items": []},
            "constraint_candidates": {"status": "ok", "count": 0, "items": []},
        })
        beout = run_once(bempty)
        fails += has("没有卡点时给出空态说明",
                     beout["blockedList"] or beout["blockedHtml"], "没有需要你决策的")

        # 状态管家的调整卡必须真的上屏，并且写清「点了会怎样 / 不点会怎样」。
        # 只把卡片留在 CLI、面板看不到，等于用户永远等不到那一次「点头」——
        # 这正是「机制存在但没有入口」的老毛病。
        tune = dict(payload)
        tune["harness_tune"] = {"status": "ok", "count": 1, "items": [{
            "id": "T-9001", "key": "model_auto_compact_token_limit",
            "label": "上下文压缩线", "old": 160000, "new": 200000,
            "why": "司令窗近 2 小时压缩 5 次",
        }]}
        tout = run_once(tune)
        fails += has("状态管家的调整卡上屏", tout["blockedList"],
                     "T-9001", "上下文压缩线")
        fails += has("调整卡写明改前备份/可回滚", tout["blockedList"], "一键回滚")
        fails += has("调整卡写明不点的后果", tout["blockedList"], "config 一个字都不动")

        # 上下文管家的约束候选也必须真的上屏，并且写清「收进规范会怎样 /
        # 不算会怎样」。候选绝不能自动生效（它一进 constraints.jsonl 就参与落盘
        # 门禁），所以面板必须给出「算 / 不算」两条明确出口，而不是只展示不动作。
        cand = dict(payload)
        cand["constraint_candidates"] = {"status": "ok", "count": 1, "items": [{
            "id": "CC-9001", "text": "以后都不许用 git add -A",
            "title": "以后都不许用 git add -A", "kind": "taboo",
            "scope": ["AGENTS.md"], "signals": ["以后都", "不许"],
        }]}
        cout = run_once(cand)
        fails += has("上下文管家的约束候选上屏", cout["blockedList"], "CC-9001")
        fails += has("候选附上用户原话", cout["blockedList"], "以后都不许用 git add -A")
        fails += has("候选写明命中信号", cout["blockedList"], "以后都")
        fails += has("候选写明不算就什么都不生效", cout["blockedList"],
                     "不算", "什么都不生效")
        lout_payload = dict(cand)
        lout_payload["constraint_candidates"] = {"status": "ok", "count": 1, "items": [{
            "id": "CC-9002", "text": "面板不要展示和节点无关的信息",
            "title": "面板不要展示和节点无关的信息", "kind": "expectation",
            "source": "context_learning", "mentions": 5, "threads": 3,
            "scope": ["plugin/ui/panel.html"], "signals": ["context_learning"],
        }]}
        lout = run_once(lout_payload)
        fails += has("自学习候选上屏", lout["blockedList"], "CC-9002",
                     "面板不要展示和节点无关的信息")
        fails += has("自学习候选写清重复证据", lout["blockedList"],
                     "出现 5 次 / 跨 3 个会话")

        # 验收标准候选是「先确认什么算成，再进入验收台账」的前半截。
        # 必须真点一次确认/不算：只检查文案挡不住按钮接错工具，也挡不住
        # confirm 漏传后 MCP 静默拒绝；确认后仍不能伪造自验证据。
        ac = dict(payload)
        ac.update({
            "blocked": {"failures": [], "resolve": [], "exec": [], "port": []},
            "error": None,
            "harness_tune": {"status": "ok", "count": 0, "items": []},
            "constraint_candidates": {"status": "ok", "count": 0, "items": []},
            "acceptance_criteria": {"status": "ok", "count": 1, "items": [{
                "id": "AC-9001",
                "text": "落盘后必须能在面板看到闭环结果",
                "needs_human": True,
                "signals": ["必须能", "看到"],
                "steps": "1) 打开面板 2) 看结果",
            }]},
        })
        acout = run_once(ac)
        fails += has("验收标准候选上屏", acout["blockedList"],
                     "AC-9001", "落盘后必须能在面板看到闭环结果")
        fails += has("验收标准候选写明确认边界", acout["blockedList"],
                     "确认成验收项", "不写证据", "机器项仍要真门禁")
        promote_calls = acout.get("acceptancePromoteCalls") or []
        if (len(promote_calls) == 1
                and promote_calls[0].get("id") == "AC-9001"
                and promote_calls[0].get("confirm") is True):
            print("  PASS  验收标准确认按钮带 id 与 confirm=true")
        else:
            fails += 1
            print(f"  FAIL  验收标准确认按钮调用不对：{promote_calls}")
        dismiss_calls = acout.get("acceptanceDismissCalls") or []
        if len(dismiss_calls) == 1 and dismiss_calls[0].get("id") == "AC-9001":
            print("  PASS  验收标准不算按钮调用正确工具")
        else:
            fails += 1
            print(f"  FAIL  验收标准不算按钮调用不对：{dismiss_calls}")

        # 管家健康条：七个管家 + 一层全局项目健康必须在一个地方可读，四态语义不能混。
        # 新版首屏只展示四态摘要 + 短名状态条，完整解释下沉到 title / aria-label。
        # 门禁必须同时检查这两个出口：短名可见保证一眼能扫，完整解释可访问保证
        # 信息没有被压缩吞掉；只查其中一个都会让「更轻」变成「丢信息」。
        stew = payload.get("stewards") or {}
        rows = stew.get("rows") or []
        counts = stew.get("counts") or {}
        states = {"ok": "正常", "attention": "需关注", "off": "已关闭", "unknown": "取不到"}
        short_names = {"context": "上下文", "route": "路由", "merge": "合并",
                       "tune": "状态", "exec": "实现", "pm": "项目",
                       "accept": "验收", "health": "全局健康"}
        fails += has("管家健康条上屏", out["stewardBar"], "管家")
        # 精确键集合：七个管家必须齐全；健康层单独校验，既不是第八个管家，也不得缺失。
        expected_keys = {"context", "route", "merge", "tune", "exec", "pm", "accept"}
        actual_keys = {r.get("key") for r in rows if r.get("key") != "health"}
        health_rows = [r for r in rows if r.get("key") == "health"]
        if actual_keys != expected_keys:
            fails += 1
            print(f"  FAIL  管家键集合漂移：多={sorted(actual_keys - expected_keys)} "
                  f"少={sorted(expected_keys - actual_keys)}")
        else:
            print("  PASS  管家键集合正好是七个")
        if len(health_rows) != 1:
            fails += 1
            print(f"  FAIL  全局健康应恰好 1 行，实得 {len(health_rows)} 行")
        else:
            print("  PASS  全局健康恰好 1 行")
        if len(rows) != 8:
            fails += 1
            print(f"  FAIL  健康条应有 7 个管家 + 1 层全局健康，实得 {len(rows)} 行")
        else:
            print("  PASS  健康条行数：7 个管家 + 1 层全局健康")
        # 路线/效能检测是状态管家内部的只读能力，不是第八个运行时角色。
        # 曾经的风险是「加一个管家行」让首屏越来越像组织架构图，这里反向锁住。
        route_key_rows = [r for r in rows
                          if str(r.get("key") or "").startswith("route_health")]
        if route_key_rows:
            fails += 1
            print(f"  FAIL  路线与效能不应新增管家行：{route_key_rows}")
        else:
            print("  PASS  路线与效能不新增管家行")
        # accept 是新增的验收管家：它管的是「先自己验完，再来找你」这个顺序。
        # 少一条断言，就等于这个管家断了也没人知道。
        for key in ("context", "route", "merge", "tune", "exec", "pm", "accept", "health"):
            got = [r for r in rows if r.get("key") == key]
            if not got:
                fails += 1
                print(f"  FAIL  管家健康条缺 {key}")
            else:
                print(f"  PASS  管家健康条含 {key}")
        # 四态是语义，不是装饰：未授权的新状态必须在聚合层就被发现。
        bad_states = sorted({r.get("state") for r in rows} - set(states))
        if bad_states:
            fails += 1
            print(f"  FAIL  管家健康条出现未知状态：{bad_states}")
        else:
            print("  PASS  管家健康条只使用四种约定状态")
        # 摘要计数是首屏真正读到的全局结论，逐项检查，不能因为现场恰好只有一种状态就跳过。
        summary_needles = []
        for key in ("attention", "unknown", "off", "ok"):
            if isinstance(counts.get(key), int):
                summary_needles.append(f"{counts[key]} {states[key]}")
        if (summary_needles and set(counts) <= set(states)
                and sum(counts.values()) == len(rows)):
            fails += has("健康摘要逐项显示四态计数", out["stewardBar"], *summary_needles)
        else:
            fails += 1
            print(f"  FAIL  健康摘要计数与 8 行状态不一致：{counts}")
        steward_html = str(out.get("stewardHtml") or "")
        # 每个管家都要同时留下两样东西：短名上屏可扫，完整解释在 title / aria-label 可取。
        for r in rows:
            key = r.get("key")
            state = r.get("state") or "unknown"
            short = short_names.get(key, str(r.get("label") or "").split("：")[0])
            fails += has(f"健康条短名上屏：{key}", out["stewardBar"], short)
            full = str(r.get("label") or "") + "（" + states.get(state, state) + "）"
            rendered_attrs = [f'data-key="{key}"', f"s-{state}"]
            for attr in ("title", 'aria-label'):
                rendered_attrs.append(f'{attr}="{full}"')
            missing = [needle for needle in rendered_attrs if needle not in steward_html]
            if missing:
                fails += 1
                print(f"  FAIL  健康条完整解释/状态不可访问：{key} 缺 {missing}")
            else:
                print(f"  PASS  健康条短名与完整解释都在：{key}")
        # 状态语义用确定性夹具锁住：unknown/off/attention/ok 四态各有明确文案与颜色类，
        # 防止现场恰好没有某一态时，那一态的实现腐化而无人发现。
        semantic_rows = [
            {"key": "context", "state": "attention", "label": "上下文管家：1 条候选等你确认"},
            {"key": "route", "state": "unknown", "label": "路由管家：守护没在跑，收口状态取不到"},
            {"key": "merge", "state": "off", "label": "合并管家：自动合入已关"},
            {"key": "exec", "state": "ok", "label": "实现管家：已开，会按槽位自动派活"},
        ]
        semantic_counts = {"attention": 1, "unknown": 1, "off": 1, "ok": 1}
        sem_out = run_once(dict(payload, stewards={"rows": semantic_rows, "counts": semantic_counts}))
        sem_html = str(sem_out.get("stewardHtml") or "")
        fails += has("四态摘要各自如实显示", sem_out["stewardBar"],
                     "1 需关注", "1 取不到", "1 已关闭", "1 正常")
        for r in semantic_rows:
            full = r["label"] + "（" + states[r["state"]] + "）"
            missing = [needle for needle in (
                f'data-key="{r["key"]}"', f"s-{r['state']}",
                f'title="{full}"', f'aria-label="{full}"',
            ) if needle not in sem_html]
            if missing:
                fails += 1
                print(f"  FAIL  状态语义夹具渲染错误：{r['key']} 缺 {missing}")
            else:
                print(f"  PASS  状态语义夹具渲染正确：{r['key']}/{r['state']}")
        # unknown 态不得伪装正常：空 payload 下必须出现「取不到 / 没在跑」这类词。
        # 新 UI 把它们放在短名的 title/aria-label 里，检查必须同时覆盖文本和 HTML。
        blank = dict(payload)
        blank["stewards"] = central_mcp._steward_health({})
        bout2 = run_once(blank)
        blank_view = str(bout2["stewardBar"]) + " " + str(bout2.get("stewardHtml") or "")
        fails += has("健康条取不到时不冒充正常", blank_view, "取不到", "没在跑")
        if "s-ok" in str(bout2.get("stewardHtml") or ""):
            fails += 1
            print("  FAIL  空状态下仍出现正常绿点")
        else:
            print("  PASS  空状态下不出现正常绿点")
        # 路线判据异常时必须进状态管家那一行，且只说复核，不说自动纠偏。
        # 新 UI 的完整解释在 title/aria-label，检查必须同时覆盖文本和 HTML。
        route_unknown = dict(payload)
        route_unknown["harness_tune"] = {"status": "ok", "count": 0, "items": []}
        route_unknown["harness_health"] = {"status": "ok", "level": "ok",
                                           "headline": "上下文健康：正常（测试夹具）"}
        route_unknown["route_health"] = {
            "status": "ok", "level": "unknown",
            "headline": "路线与效能判据取不到：scope 外改动数取不到",
            "unknown": ["scope 外改动数取不到"], "reasons": [], "action": "none",
        }
        route_unknown["stewards"] = central_mcp._steward_health(route_unknown)
        rout_out = run_once(route_unknown)
        route_unknown_view = (str(rout_out["stewardBar"]) + " "
                              + str(rout_out.get("stewardHtml") or ""))
        fails += has("路线判据不足时状态管家明说取不到", route_unknown_view,
                     "路线与效能判据取不到")
        fails += has("路线提示只进状态管家", route_unknown_view, "状态管家")
        if 'data-key="tune"' not in str(rout_out.get("stewardHtml") or ""):
            fails += 1
            print("  FAIL  路线提示没有落在状态管家节点上")
        else:
            print("  PASS  路线提示落在状态管家节点上")
        route_attention = dict(route_unknown)
        route_attention["route_health"] = {
            "status": "ok", "level": "attention",
            "headline": "实际工作已经偏离 mandate 声明的范围",
            "unknown": [], "reasons": ["实际工作已经偏离 mandate 声明的范围"],
            "action": "propose_review",
        }
        route_attention["stewards"] = central_mcp._steward_health(route_attention)
        ratt_out = run_once(route_attention)
        route_attention_view = (str(ratt_out["stewardBar"]) + " "
                                + str(ratt_out.get("stewardHtml") or ""))
        fails += has("路线异常时提示复核", route_attention_view,
                     "路线与效能需复核（只提复核，不自动纠偏）")
        # 内部空值不得泄漏到读者面。实测缺陷（2026-11-17）：守护没跑时合并管家那行
        # 直接印出「可合 None · 冲突 None」。None/True/False 这类 Python 字面量
        # 一旦上屏就是没交付——这是本项目硬规则（内部读数不上读者面）。
        for bad in ("None", "True", "False"):
            if bad in blank_view or bad in str(out["stewardBar"]) or bad in steward_html:
                fails += 1
                print(f"  FAIL  健康条泄漏内部字面量 {bad}")
            else:
                print(f"  PASS  健康条不泄漏 {bad}")

        # 授权情况：这一拍跑过哪些工序、多少条是明确授权、多少条只在观察，
        # 必须原样上屏。取不到时不许显示成「0 条在观察」——那是把未知冒充正常。
        au = payload.get("autonomy") or {}
        fails += has("授权情况摘要上屏", out["autonomyLine"], au.get("label"))
        blank["autonomy"] = central_mcp._autonomy_summary({})
        bout3 = run_once(blank)
        fails += has("授权情况标签用人话", str(blank["autonomy"].get("label") or ""),
                     "授权情况")
        fails += lacks("授权情况不再露出旧内部词", str(blank["autonomy"].get("label") or ""),
                       "自治分级")
        fails += has("授权情况取不到时如实说明", bout3["autonomyLine"], "取不到")
        if "0 条" in str(bout3["autonomyLine"]):
            fails += 1
            print("  FAIL  授权情况把取不到显示成 0 条")
        else:
            print("  PASS  授权情况不把取不到显示成 0 条")
        fails += has("逐条授权情况取不到时如实说明", bout3["autonomyAudit"],
                     "逐条授权来源", "取不到")
        # 逐条审计必须来自 last_passes，不让面板自己重算第二套真相；
        # 首屏只留摘要，明细放工程详情折叠区，避免把内部读数糊到读者面。
        audit_passes = [
            {"name": "exec", "mode": "auto", "status": "ok",
             "authorization": "explicit", "effect": "destructive"},
            {"name": "route", "mode": "observe", "status": "proposed",
             "authorization": "inferred", "effect": "reversible"},
        ]
        audit_summary = central_mcp._autonomy_summary(
            {"last_passes": audit_passes})
        if len(audit_summary.get("passes") or []) != len(audit_passes):
            fails += 1
            print("  FAIL  自治摘要没有原样带出逐条工序")
        else:
            print("  PASS  自治摘要原样带出逐条工序")
        audit = dict(payload)
        audit["autonomy"] = audit_summary
        aout = run_once(audit)
        fails += has("逐条授权情况上屏", aout["autonomyAudit"],
                     "逐条授权来源", "exec", "明确授权", "破坏性")
        fails += has("逐条授权情况不丢观察项", aout["autonomyAudit"],
                     "route", "观察", "系统推断", "可逆", "待转正")

        # PM 简报：预期、让路、追问候选必须来自控制层聚合，面板只照抄。
        # 这里同时锁住两个反向边界：缺失时不说 0 / 正常；候选只展示，不自动发消息。
        pm_payload = dict(payload)
        pm_payload["pm_briefing"] = {
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
                    "needs_human": False,
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
                    "suggested_prompt": "继续处理 L-0021：回报当前卡点。",
                }],
                "note": "只生成候选，不会自动追问；你点头才会发消息。",
                "missing": [],
            },
        }
        # 授权账本是顶层状态，不属于 PM 简报子对象；面板按这个契约读取。
        pm_payload["authorized_followups"] = {
            "status": "ok", "items": [{
                "request_id": "FU-abcdef12", "ticket_id": "L-0021",
                "thread_id": "thread-abc", "state": "approved",
                "prompt": "继续处理 L-0021：回报当前卡点。",
            }],
            "counts": {"approved": 1, "sent": 0, "cancelled": 0, "failed": 0},
            "note": "只有你逐项点头的候选才会进入自动追问；可随时撤销未发送项。",
        }
        pm_out = run_once(pm_payload)
        fails += has("PM 简报上屏：待确认预期", pm_out["pmBriefingBox"],
                     "待确认", "绝不影响用户正在用的服务")
        fails += has("PM 简报上屏：验收标准候选", pm_out["pmBriefingBox"],
                     "验收标准", "AC-9001", "先跑门禁")
        fails += has("PM 简报上屏：让路工作项", pm_out["pmBriefingBox"],
                     "L-0021", "L-0020", "ops/truthctl.py")
        fails += has("PM 简报上屏：追问候选", pm_out["pmBriefingBox"],
                     "90 分钟", "thread-abc", "不会自动追问")
        fails += has("PM 简报上屏：追问授权摘要", pm_out["pmBriefingBox"],
                     "追问授权", "1 条待发")
        authorize_calls = pm_out.get("followupAuthorizeCalls") or []
        if len(authorize_calls) != 1 or authorize_calls[0].get("confirm") is not True \
           or authorize_calls[0].get("ticket_id") != "L-0021" \
           or authorize_calls[0].get("thread_id") != "thread-abc" \
           or authorize_calls[0].get("prompt") != "继续处理 L-0021：回报当前卡点。" \
           or not authorize_calls[0].get("reason"):
            fails += 1
            print("  FAIL  授权追问按钮必须显式带 confirm 与完整授权参数")
        else:
            print("  PASS  授权追问按钮显式带 confirm 与完整授权参数")
        cancel_calls = pm_out.get("followupCancelCalls") or []
        if len(cancel_calls) != 1 or cancel_calls[0].get("request_id") != "FU-abcdef12" \
           or not cancel_calls[0].get("reason"):
            fails += 1
            print("  FAIL  撤销授权按钮必须带 request_id 与 reason")
        else:
            print("  PASS  撤销授权按钮带 request_id 与 reason")
        auth_missing = run_once(dict(pm_payload, authorized_followups=None))
        fails += has("追问授权账本缺失时如实说取不到", auth_missing["pmBriefingBox"],
                     "追问授权账本取不到")
        pm_missing = run_once(dict(payload, pm_briefing=None))
        fails += has("PM 简报缺失时如实说取不到",
                     pm_missing["pmBriefingBox"], "取不到")

        # 关系层：跨项目长期目标在高级区折叠展示。它必须照抄控制层摘要，
        # 缺失时说取不到，不能把 G-id / 优先级 / 需要人的原因藏在首屏。
        rel_payload = dict(payload)
        rel_payload["relationship_goals"] = {
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
        }
        rel_out = run_once(rel_payload)
        fails += has("关系层摘要上屏", rel_out["relationshipGoalsBox"],
                     "跨项目目标", "G-9001", "codex-autopilot")
        fails += has("关系层需要人原因上屏", rel_out["relationshipGoalsBox"],
                     "需要人", "等用户判断要不要改产品方向")
        fails += has("关系层北极星上屏", rel_out["relationshipGoalsBox"],
                     "北极星", "用户只说一次，跨项目长期记住并推进")
        fails += has("关系层工作项与验收关联上屏", rel_out["relationshipGoalsBox"],
                     "工作项 L-0076", "验收 A-0069")
        fails += has("关系层来源窗口上屏", rel_out["relationshipGoalsBox"],
                     "来源窗口", "thread-relationship-20261118")
        fails += has("关系层明示不派活", rel_out["relationshipGoalsBox"],
                     "不会自动派活")
        rel_missing = run_once(dict(payload, relationship_goals=None))
        fails += has("关系层缺失时如实说取不到",
                     rel_missing["relationshipGoalsBox"], "取不到")

        # 结果代理分：高级区只读展示，必须带机器绿灯不等于用户结果的边界。
        out_payload = dict(payload)
        out_payload["outcome_score"] = {
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
        }
        outcome_out = run_once(out_payload)
        fails += has("结果代理分上屏", outcome_out["outcomeScoreBox"],
                     "结果代理分 72/100", "首问闭环", "全局健康")
        fails += has("结果代理分不冒充用户结果", outcome_out["outcomeScoreBox"],
                     "机器命令通过不等于用户结果变好", "取不到，不计分")
        outcome_missing = run_once(dict(payload, outcome_score=None))
        fails += has("结果代理分缺失时如实说取不到",
                     outcome_missing["outcomeScoreBox"], "取不到")

        fails += has("首屏开关文案与控制层一致", out["masterState"],
                     "已关闭" if not payload.get("enabled") else "正在运行")
        fails += has("原生直连结论如实显示", out["nativeState"],
                     (payload.get("native") or {}).get("label"))
        fails += has("计数：收到反馈", out["received"], counters.get("received"))
        fails += has("计数：已合入", out["landned"], counters.get("landed"))
        fails += has("计数：待你验收", out["stReview"], len(human_acc))

        # 系统状态行只给人一句可信结论；PID、队列、-daemon 绑定等判据仍要能在
        # 折叠的工程详情里查到，不能因为降噪把可观测性删掉。
        hh = payload.get("harness_health") or {}
        health_text = hh.get("headline") or (
            "上下文健康：" + (hh.get("reason") or "取不到"))
        q = payload.get("queue") or {}
        fails += has("系统首屏只显示健康结论", out["sysLine"], health_text)
        fails += lacks("系统首屏不铺开队列与守护读数", out["sysLine"],
                       "可落盘 ", "冲突 ", "常驻运行中", "常驻未运行")
        if q.get("clean") is not None:
            fails += has("工程详情保留可落盘读数", out["systemDetail"],
                         "可落盘 " + str(q.get("clean")))
        if q.get("conflict") is not None:
            fails += has("工程详情保留冲突读数", out["systemDetail"],
                         "冲突 " + str(q.get("conflict")))

        recent = payload.get("recent_completed") or []
        if recent:
            fails += has("首屏显示最近完成了什么", out["recentBox"],
                         human_recent(recent[0]))
            fails += lacks("最近收口正文不露原始 ID", out["recentBox"],
                           *[r for r in recent if re.match(r"^[A-Z]+-\d+", str(r))])
            fails += lacks("最近收口正文不露工程路径与命令", out["recentBox"],
                           "kgctl", "~/.plugins", "progress.json", "acceptance.json")
            if re.match(r"^[A-Z]+-\d+", str(recent[0])):
                fails += has("最近收口 ID 保留在 title/aria", out["recentSerialized"],
                             str(recent[0]), "收口详情：" + str(recent[0]))
            visible_recent = min(len(recent), 3)
            fails += has("最近收口首屏只报前三条", out["recentNote"],
                         "最近 " + str(visible_recent) + " 项")
            got_recent = str(out["recentSerialized"]).count("收口详情：")
            if got_recent == visible_recent:
                print("  PASS  最近收口正文最多三条")
            else:
                fails += 1
                print(f"  FAIL  最近收口正文条数错误：期望 {visible_recent}，实得 {got_recent}")
        else:
            fails += has("没有收口记录时给出空态说明", out["recentBox"] or document_empty(out), "还没有收口记录")

        # 北极星首问闭环率必须在首屏；算得出给数字，算不出必须显示「取不到」，
        # 绝不许用 0% 冒充（这条是产品价值成立与否的唯一数字）。
        cr = payload.get("closure_rate") or {}
        if cr.get("status") == "ok":
            fails += has("首屏显示北极星闭环率数字", out["northStarValue"],
                         format(float(cr.get("rate_pct")), ".1f") + "%")
            fails += has("首屏显示闭环率分子分母", out["northStarDetail"],
                         f"{cr.get('numerator')}/{cr.get('denominator')}")
        else:
            fails += has("闭环率取不到时首屏明写取不到", out["northStarValue"], "取不到")
            if "0%" in str(out["northStarValue"]):
                print("  FAIL  闭环率取不到时显示了 0%，会伪装成「测过了但没达标」")
                fails += 1

        # 关掉总闸后必须有一个明确的关闭态，而不是还显示「正在运行」。
        closed = dict(payload)
        closed.update(enabled=False, lifecycle="off", paused=False,
                      state="off", state_label="自动挡已关闭，Codex 已恢复原生状态")
        try:
            shut = run_once(closed)
        except RuntimeError as exc:
            print(exc)
            return 1
        fails += has("关闭后开关行说清楚已关闭", shut["masterState"], "已关闭")
        if "正在运行" in str(shut["masterState"]):
            print("  FAIL  关闭后开关行仍显示「正在运行」")
            fails += 1
        else:
            print("  PASS  关闭后不再显示正在运行")
        fails += has("关闭后总状态是关闭态", shut["stateLabel"], "已恢复原生状态")
        fails += has("关闭后副标题说明 Codex 回到原生", shut["subtitle"], "自动挡已关闭")

        # 窗口集合按官方协议的真实形状分组：集合挂在嵌套 section.id 上，
        # 不是扁平 sectionId。写错字段名时集合会永远显示「暂无窗口」。
        routed = dict(payload)
        routed["route"] = {
            "thread_note": "",
            "native": {
                "sections": [{"id": "sec-1", "name": "Codex 自动挡 · 面板检查"}],
                "threads": [
                    {"id": "t-in", "name": "归进集合的子对话",
                     "section": {"id": "sec-1", "name": "Codex 自动挡 · 面板检查"}},
                    {"id": "t-out", "name": "没归组的子对话", "section": None},
                ],
            },
        }
        try:
            grouped = run_once(routed)
        except RuntimeError as exc:
            print(exc)
            return 1
        fails += has("窗口集合按官方嵌套字段分组显示会话", grouped["routeText"],
                     "Codex 自动挡 · 面板检查", "归进集合的子对话")
        if "没归组的子对话" in str(grouped["routeText"]):
            print("  FAIL  没归组的会话被塞进了集合")
            fails += 1
        else:
            print("  PASS  没归组的会话没有混进集合")

        if not quiet:
            print("\n结果：" + ("全部通过" if fails == 0 else f"{fails} 项未通过"))
        return fails
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
