#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage_table —— 工作项「走到哪一步」的唯一判据（stdlib only）。

为什么需要这一层
----------------
在这之前，「现在什么状态」这件事在三个地方各写了一遍：

  * `central_mcp._status_payload()` 里一条 `if/elif` 长链，决定 `state` / `state_label`
  * `plugin/ui/panel.html` 的 JS 里又算了一遍四个格子（待处理 / 进行中 / 待你验收 / 卡住）
  * `kgctl doctor` 另有自己的口径

同一份事实写三遍，就会出现「面板说在跑、CLI 说没跑」这类自相矛盾——本项目已经
踩过一次（`daemon_liveness` 那条：CLI 报「未安装」而面板仍显示「后台常驻=开」）。
所以这里把「一个工作项 / 整套面板处在哪一步」收敛成**一个纯函数**，谁都用它。

first-match-wins 为什么对
------------------------
顺序本身就是优先级，这是这份设计里唯一容易出错的地方，所以它必须显式写在数据里，
而不是散在调用方的 `if` 里。任何「谁该盖过谁」的争论，改这一张表就够了，改完
`ops/test-stage-table.py` 会告诉你有没有把优先级弄反。

  已经不在服务里（错 / 关 / 暂停）
    > 有东西正在动（实现中）
      > 卡住要你拍板 > 等你验收 > 归属判不清要你答一句
        > 常驻没接上 > 有分支可合 > 真的没事

本模块**不做 I/O、不读文件、不调子进程**：给事实，还结论。这样它才能被单测直接
穷举，也才能被面板、CLI、doctor 三处同时放心调用。
"""

from __future__ import annotations

# UI 状态只有这几个：panel.html 的 body.s-* 类名依赖它们，不要随手加。
UI_IDLE = "idle"
UI_WORKING = "working"
UI_PAUSED = "paused"
UI_BLOCKED = "blocked"
UI_ERROR = "error"
UI_SESSION_ONLY = "session_only"
UI_OFF = "off"

# 面板四个计数格子用得到的粗分类。
BUCKET_TODO = "todo"
BUCKET_DOING = "doing"
BUCKET_REVIEW = "review"
BUCKET_STUCK = "stuck"


def _row(stage, ui_state, label, owner, test):
    """一行判据。`test` 收一个事实字典，返回真值表示命中。"""
    return {"stage": stage, "ui_state": ui_state, "label": label,
            "owner": owner, "test": test}


# first-match-wins。顺序 = 优先级，改这里之前先读模块 docstring。
PANEL_ROWS = (
    _row("error", UI_ERROR, "状态异常，已安全暂停", "system",
         lambda f: bool(f.get("error"))),
    _row("off", UI_OFF, "自动挡已关闭，Codex 已恢复原生状态", "person",
         lambda f: not f.get("enabled", True)),
    _row("paused", UI_PAUSED, "已暂停", "person",
         lambda f: bool(f.get("paused"))),
    _row("working/exec", UI_WORKING, "正在自动实现一条需求", "machine",
         lambda f: bool(f.get("exec_running"))),
    _row("person/blocked", UI_BLOCKED, "有分支需要你拍板", "person",
         lambda f: int(f.get("blocked_count") or 0) > 0),
    _row("person/review", UI_BLOCKED, "需要你验收", "person",
         lambda f: int(f.get("review_count") or 0) > 0),
    _row("person/answer", UI_BLOCKED, "有需求判不清归属，需要你说一句", "person",
         lambda f: int(f.get("answer_count") or 0) > 0),
    # daemon_live 缺项 = 不知道，**不命中**：宁可落到下一行，也不要凭空报一句
    # 「常驻未开」把没事的现场说成有事。只有明确读到 False 才判这一条。
    _row("session_only", UI_SESSION_ONLY, "仅会话内运行（常驻未开）", "system",
         lambda f: f.get("daemon_live") is False),
    _row("working/queued", UI_WORKING, "正在检查可合入的分支", "machine",
         lambda f: int(f.get("queued_count") or 0) > 0),
    _row("idle", UI_IDLE, "运行中，暂无待办", "machine", lambda f: True),
)

# 「常驻没接上」的文案依赖是否在服务别的项目，所以单独留一条可覆盖的。
SESSION_ONLY_OTHER_PROJECT = "当前项目未连接常驻守护"

# 单个工作项走到哪。台账的 status 词形很杂（open/todo/doing/wip/blocked…），
# 归一成这四个之后，面板和台账才说得上是同一件事。
ITEM_STATUS_ALIASES = {
    "open": BUCKET_TODO,
    "todo": BUCKET_TODO,
    "next": BUCKET_TODO,
    "queued": BUCKET_TODO,
    "pending": BUCKET_TODO,
    "doing": BUCKET_DOING,
    "wip": BUCKET_DOING,
    "in_progress": BUCKET_DOING,
    "in-progress": BUCKET_DOING,
    "active": BUCKET_DOING,
    "ongoing": BUCKET_DOING,
    "current": BUCKET_DOING,
    "blocked": BUCKET_STUCK,
    "stuck": BUCKET_STUCK,
    "failed": BUCKET_STUCK,
    "verified": BUCKET_REVIEW,
    "done": BUCKET_REVIEW,
    "closed": BUCKET_REVIEW,
}


def norm_item_status(status) -> str:
    """台账里的自由状态词 → 四个桶之一。认不出的按 todo（宁可低估，不虚报完成）。"""
    key = str(status or "").strip().lower()
    return ITEM_STATUS_ALIASES.get(key, BUCKET_TODO)


def item_stage(status=None, needs_human=False, is_verified=False) -> str:
    """一个工作项当前 stage。需要人拍板的验收项单列，不混进「进行中」。"""
    if needs_human and not is_verified:
        return "person/review"
    return norm_item_status(status)


# 单工作项 stage → 给人看的词。面板、CLI 都用这一份，别再各写一套文案。
ITEM_STAGE_LABELS = {
    "person/review": "需要你验收",
    BUCKET_TODO: "待处理",
    BUCKET_DOING: "进行中",
    BUCKET_STUCK: "卡住",
    BUCKET_REVIEW: "已验收",
}


def item_stage_label(stage) -> str:
    """stage → 人可读文案。认不出的按「待处理」（宁可低估，不虚报完成）。"""
    return ITEM_STAGE_LABELS.get(str(stage or ""), "待处理")


def panel_stage(facts=None) -> dict:
    """整套面板处在哪一步。返回 `{stage, ui_state, label, owner}`，永不抛异常。

    `facts` 缺项一律按「未知即最保守」处理：没有 `error` 就是没错、没有 `enabled`
    就是开着。这与面板「读不到就写读不到」的原则一致——不把未知伪装成正常，
    但也绝不因为某个字段缺失就误报故障。
    """
    f = facts if isinstance(facts, dict) else {}
    for row in PANEL_ROWS:
        try:
            hit = row["test"](f)
        except Exception:
            hit = False
        if hit:
            label = row["label"]
            if row["stage"] == "session_only" and f.get("daemon_other_project"):
                label = SESSION_ONLY_OTHER_PROJECT
            return {"stage": row["stage"], "ui_state": row["ui_state"],
                    "label": label, "owner": row["owner"]}
    # PANEL_ROWS 末行恒真，走到这里说明表被改坏了；给一个安全结论而不是崩掉面板。
    return {"stage": "idle", "ui_state": UI_IDLE,
            "label": "运行中，暂无待办", "owner": "machine"}


def stage_counts(*, doing=0, todo=0, review=0, stuck=0, exec_running=False) -> dict:
    """面板四个计数格子。与 `panel_stage` 同源，避免面板 JS 自己再算一遍。

    `doing` 要把「此刻真有一条实现在跑」算进去：那是一条真实推进中的工作，
    不是「没有进行中」。
    """
    doing_n = int(doing or 0) + (1 if exec_running else 0)
    return {
        BUCKET_TODO: int(todo or 0),
        BUCKET_DOING: doing_n,
        BUCKET_REVIEW: int(review or 0),
        BUCKET_STUCK: int(stuck or 0),
    }


def stage_line(stage) -> str:
    """给人看的一行。CLI / 文本兜底共用，保证和面板说的是同一句话。"""
    if isinstance(stage, dict):
        return str(stage.get("label") or "")
    return ""
