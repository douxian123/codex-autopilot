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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
PANEL = REPO / "plugin" / "ui" / "panel.html"

HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// ---- 最小 DOM：只实现面板脚本真正用到的那几个 API ----
const NOOP = function () {};
const els = new Map();
function makeEl(id) {
  const base = {
    id: id || "",
    textContent: "", innerHTML: "", className: "", title: "",
    value: "", checked: false, hidden: false, disabled: false,
    style: {}, children: [],
    classList: { toggle: NOOP, add: NOOP, remove: NOOP, contains: function () { return false; } },
    setAttribute: function (k, v) { this[k] = String(v); },
    getAttribute: function (k) { return this[k]; },
    removeAttribute: NOOP, addEventListener: NOOP, removeEventListener: NOOP,
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
    set: function (v) { _html = v; base.textContent = ""; },
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

// 走 Codex 宿主同一条通知：structuredContent 就是控制层状态
msg.fn({
  source: window,
  data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
          params: { structuredContent: payload } },
});

const read = function (id) { return document.getElementById(id).textContent; };
console.log(JSON.stringify({
  ok: true,
  projectLine: read("projectLine"),
  projectTitle: document.getElementById("projectLine").title,
  hierarchyProject: read("hierProject"),
  hierarchyRoad: read("hierRoad"),
  hierarchyWork: read("hierWork"),
  folderBox: read("folderBox"),
  stateLabel: read("stateLabel"),
  subtitle: read("subtitle"),
  masterState: read("masterState"),
  focusStatus: read("focusStatus"),
  focusDoing: read("focusDoing"),
  focusStep: read("focusStep"),
  northStarValue: read("nsValue"),
  northStarDetail: read("nsDetail"),
  northStarClass: document.getElementById("northStar").className,
  needText: alertText.textContent,
  nativeState: read("nativeState"),
  received: read("cReceived"),
  landned: read("cLanded"),
  resolved: read("cResolved"),
  stReview: read("stReview"),
  stDoing: read("stDoing"),
  recentBox: read("recentBox"),
  recentNote: read("recentNote"),
  needClass: document.getElementById("need").className,
  recentHtml: document.getElementById("recentBox").innerHTML,
  routeText: read("routeBox"),
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
        current_items = payload.get("doing_items") or payload.get("todo_items") or []
        expected_work = ((current_items[0].get("title") or current_items[0].get("detail"))
                         if current_items
                         else ("等待人工验收" if human_acc else "等待下一条需求"))
        fails += has("路线图显示当前工作项", out["hierarchyWork"], expected_work)
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
        fails += has("首屏显示总状态文案", out["stateLabel"], payload.get("state_label"))
        fails += has("一眼三问：状态", out["focusStatus"], "运行中" if payload.get("enabled") else "已关闭")
        if human_acc:
            fails += has("一眼三问：等你验收优先显示", out["focusDoing"],
                         human_acc[0].get("title") or "人工验收")
        elif current_items:
            fails += has("一眼三问：现在在做", out["focusDoing"],
                         current_items[0].get("title") or current_items[0].get("detail"))
        else:
            fails += has("一眼三问：没有进行中工作时给空态", out["focusDoing"], "没有进行中")
        fails += has("一眼三问：当前步骤", out["focusStep"], expected_stage)
        if human_acc:
            review = dict(payload)
            review["state"] = "blocked"
            review["state_label"] = "需要你验收"
            review["need_action"] = "需要你验收：真实桌面验收"
            review["human_acceptance"] = [{"title": "真实桌面验收"}]
            review_out = run_once(review)
            fails += has("人工验收升级为明确状态", review_out["stateLabel"], "需要你验收")
            fails += has("人工验收在提示条里明确出现", review_out["needText"], "需要你验收")
        fails += has("首屏开关文案与控制层一致", out["masterState"],
                     "已关闭" if not payload.get("enabled") else "正在运行")
        fails += has("原生直连结论如实显示", out["nativeState"],
                     (payload.get("native") or {}).get("label"))
        fails += has("计数：收到反馈", out["received"], counters.get("received"))
        fails += has("计数：已合入", out["landned"], counters.get("landed"))
        fails += has("计数：待你验收", out["stReview"], len(human_acc))
        recent = payload.get("recent_completed") or []
        if recent:
            fails += has("首屏显示最近完成了什么", out["recentBox"], recent[0])
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
