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
  acceptBox: read("acceptBox"),
  blockedList: read("blockedList"),
  blockedHtml: document.getElementById("blockedList").innerHTML,
  workTableBox: read("workTableBox"),
  workTableHidden: document.getElementById("workTableFold").hidden,
  needClass: document.getElementById("need").className,
  recentHtml: document.getElementById("recentBox").innerHTML,
  routeText: read("routeBox"),
  stewardBar: read("stewardBar"),
  autonomyLine: read("autonomyLine"),
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
        # 只列复核步骤、不给出口，是验收项长期挂着的直接原因；每条必须带
        # 「点它会怎样 / 不点会怎样」和一个真的能收口的按钮。
        fails += has("验收项带可点的收口动作", review_out["acceptBox"], "我确认通过")
        fails += has("验收项写明不点的后果", review_out["acceptBox"], "不点就继续")
        # 二级工单表：每条必须把「来源 → 项目 → 泳道 → 分支 → stage → 证据」
        # 串出来，stage 用控制层算好的值照抄。只显示一个标题等于没回答
        # 「这条到底走到哪了、依据是什么」。
        wrows = dict(payload)
        wrows["work_rows"] = [
            {"id": "L-9001", "title": "真实工单标题", "kind": "bug",
             "source": "inbox", "project": "demo-project", "lane": "codex/lane-x",
             "branch": "codex/lane-x", "stage": "doing", "stage_label": "进行中",
             "evidence": "npm run build -> exit 0"},
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
        bempty["blocked"] = {"failures": [], "resolve": [], "exec": [], "port": []}
        bempty["error"] = None
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

        # 管家健康条：各管家 + 全局项目健康必须在一个地方可读，且四态语义不能混。
        # 这是用户「万法归一入口 + 背后许多管家」设想的落地面——面板此前没有任何
        # 一处能一眼看出「哪个管家断了」。unknown 必须如实说「取不到」，绝不用绿点
        # 冒充正常；off（用户自己关的）也不能被当成故障报警。
        stew = payload.get("stewards") or {}
        rows = stew.get("rows") or []
        fails += has("管家健康条上屏", out["stewardBar"], "管家")
        # accept 是这一轮新增的验收管家：它管的是「先自己验完，再来找你」这个顺序。
        # 少一条断言，就等于这个管家断了也没人知道。
        for key in ("context", "route", "merge", "tune", "exec", "pm", "accept", "health"):
            got = [r for r in rows if r.get("key") == key]
            if not got:
                fails += 1
                print(f"  FAIL  管家健康条缺 {key}")
            else:
                print(f"  PASS  管家健康条含 {key}")
        # 每个管家的 label 都要真的渲染到页面上（不是只存在于 payload）。
        for r in rows:
            fail = has(f"健康条渲染：{r.get('key')}", out["stewardBar"], r.get("label"))
            if fail:
                fails += fail
        # unknown 态不得伪装正常：空 payload 下必须出现「取不到 / 没在跑」这类词。
        blank = dict(payload)
        blank["stewards"] = central_mcp._steward_health({})
        bout2 = run_once(blank)
        fails += has("健康条取不到时不冒充正常", bout2["stewardBar"],
                     "取不到", "没在跑")
        # 内部空值不得泄漏到读者面。实测缺陷（2026-11-17）：守护没跑时合并管家那行
        # 直接印出「可合 None · 冲突 None」。None/True/False 这类 Python 字面量
        # 一旦上屏就是没交付——这是本项目硬规则（内部读数不上读者面）。
        for bad in ("None", "True", "False"):
            if bad in str(out["stewardBar"]) or bad in str(bout2["stewardBar"]):
                fails += 1
                print(f"  FAIL  健康条泄漏内部字面量 {bad}")
            else:
                print(f"  PASS  健康条不泄漏 {bad}")

        # 自治分级：这一拍跑过哪些工序、多少条是明确授权、多少条只在观察，
        # 必须原样上屏。取不到时不许显示成「0 条在观察」——那是把未知冒充正常。
        au = payload.get("autonomy") or {}
        fails += has("自治分级摘要上屏", out["autonomyLine"], au.get("label"))
        blank["autonomy"] = central_mcp._autonomy_summary({})
        bout3 = run_once(blank)
        fails += has("自治分级取不到时如实说明", bout3["autonomyLine"], "取不到")
        if "0 条" in str(bout3["autonomyLine"]):
            fails += 1
            print("  FAIL  自治分级把取不到显示成 0 条")
        else:
            print("  PASS  自治分级不把取不到显示成 0 条")

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
