#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
truthctl —— 项目真相层的唯一读写入口（stdlib only）

它补的是自动挡缺的那一半：`kgctl` 管「输入不分叉 + 合并串行」，
truthctl 管「这个项目现在到底是什么状态」。

  mainline.json      产品主线（手写，改方向才动）
  product_state.json 产品状态：产品今天是怎么工作的（随代码同步更新，人只看它的变化）
  decisions.jsonl    决策台账
  judgments.jsonl    判断逻辑台账：人为什么这么判断（每轮蒸馏回写）
  constraints.jsonl  约束台账：用户说过的预期/返修/决策/禁忌，带 scope 精准调取
  ledger.jsonl       问题台账（bug/视觉/体验/架构）
  progress.jsonl     进展台账
  acceptance.jsonl   验收台账

命令：
  truthctl                     一屏真相（并写 docs/PROJECT_STATUS.md）
  truthctl snapshot            只写 docs/PROJECT_STATUS.md
  truthctl ls [type]           列出台账
  truthctl add <type> "<标题>"  追加一条（细节走环境变量）
  truthctl promote <n> <kind>  把 ops/tasks/inbox.jsonl 第 n 条提升为问题台账
  truthctl product             产品今天是怎么工作的（含相对主干的变化）
  truthctl product set "<模块>" "<行为>"  更新一个模块（why/files 走环境变量）
  truthctl distill             回看这一轮人额外说了什么，列候选判断
  truthctl distill add "<判断>"  把判断背后的判断逻辑写回（why/scope 走环境变量）
  truthctl constraints add "<约束>"  记一条约束（kind/scope/enforceable 走环境变量）
  truthctl constraints [--files a:b]  列出约束；带 --files 只列命中的
  truthctl retire <decision|judgment|constraint|progress> <id>  追加式停用一条真相
  truthctl close <L-id> "<结论>"  标 done（必须带证据：KG_TRUTH_EVIDENCE 或 KG_TRUTH_COMMIT）
  truthctl reopen <L-id> "<原因>"  把已关闭的问题重新打开
  truthctl closure-package show <L-id>  查看闭环施工包与缺口
  truthctl closure-package fill <L-id> --json '{"..."}'  补齐施工包字段（仍停在规划未完成）
  truthctl closure-package confirm <L-id> --note "..." --evidence "..."  确认完整施工包进入 ready
  truthctl accept "<标题>" [--human --steps "..."]  登记验收项，分清机器自证/人工验收
  truthctl accept --id <A-id> --status verified [--note "..."]  收敛一条验收项（人工拍板/机器复核）
  truthctl goal <summary|list|create|update|link|close>  跨项目长期目标（显式账本）
  truthctl synthesize          把决策/进展/问题/验收综合成 docs/PRODUCT_MAINLINE.md
  truthctl synthesize --if-changed  常驻自追踪用：内容无变化时不重写生成时间

add 的环境变量：
  KG_TRUTH_DETAIL KG_TRUTH_STATUS KG_TRUTH_LANE KG_TRUTH_FILES
  KG_TRUTH_LEVEL KG_TRUTH_EVIDENCE KG_TRUTH_COMMIT KG_TRUTH_SUMMARY
  KG_TRUTH_DECISION KG_TRUTH_WHY KG_TRUTH_ALTERNATIVES KG_TRUTH_SOURCE
"""

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---- 受控子进程执行器（说明见 ops/proc.py 顶部）---------------------------
# 全仓 130+ 处 `proc_run(["git", ...])` 旧实现既没有 timeout，又用 pipe 收输出。
# 本仓库是 partial clone（remote.origin.promisor=true / filter=blob:none）：git 遇到
# 本地没落的 blob 会派**网络孙进程**懒加载；git 本体退出后孙进程仍攥着 pipe 写端，
# 于是 `communicate()` 永远等不到 EOF —— 2026-10-27 实测 tick 就这样静默挂 30+ 分钟、
# 0.0% CPU、13 分钟只产出 0.42s CPU 时间（sample 栈帧停在 select_poll_poll→read）。
# `proc.run` 换成「输出落临时文件 + 硬超时 + 超时 killpg 整组 + 禁懒加载」。
# 直接覆盖 `subprocess.run`：130 个调用点一行不改，且 `mock.patch(...subprocess.run)`
# 这类既有门禁继续有效（它们 patch 的就是这个属性）。
import proc as _proc  # noqa: E402
proc_run = _proc.run
from urllib.parse import unquote, urlsplit

try:
    import fcntl
except ImportError:  # 非 POSIX 平台没有 flock：退回无锁，行为与旧实现一致
    fcntl = None

# 真相状态（inbox / 台账 / 泳道 / 看板）只有一处权威目录，见 ops/state_root.py。
# 旧实现取「调用所在 worktree」，而 kgctl 取「git 主工作树」，于是同一个项目能同时
# 存在两份互不相认的账本。两边共用 state_root 之后，从任何 worktree 调用都读同一份；
# KG_REPO 仍然是最高的显式覆盖。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_binding import apply_plugin_env  # noqa: E402
from ledger_state import latest_ledger_by_id  # noqa: E402
from state_root import main_worktree, resolve_state_root  # noqa: E402
import constraints as constraints_mod  # noqa: E402
import central_config  # noqa: E402
import relationship_ledger  # noqa: E402
import acceptance_criteria_compiler  # noqa: E402
import closure_package  # noqa: E402
import pm_decision  # noqa: E402

# 自包含安装下，绑定的项目写在插件 .mcp.json 里；显式 KG_REPO 优先。
apply_plugin_env()
REPO = resolve_state_root()
REFERENCE_REPO = Path(
    os.environ.get('KG_TRUTH_REFERENCE_REPO')
    or (REPO if os.environ.get('KG_REPO') else main_worktree())
).expanduser().resolve()
CFG = central_config.load(REPO)
TRUTH = REPO / 'ops' / 'truth'
INBOX = REPO / 'ops' / 'tasks' / 'inbox.jsonl'
STATUS = REPO / 'docs' / 'PROJECT_STATUS.md'
PRODUCT_STATE = TRUTH / 'product_state.json'
JUDGMENTS = TRUTH / 'judgments.jsonl'
PRODUCT_MAINLINE = REPO / 'docs' / 'PRODUCT_MAINLINE.md'


def _kgctl_cmd() -> str:
    """给用户的下一步命令。自包含安装下运行时在插件目录，不能再提示相对路径。"""
    ops_dir = Path(__file__).resolve().parent
    if (ops_dir.parent / ".mcp.json").exists():
        return str(ops_dir / "kgctl.py")
    return "ops/kgctl.py"


PREFIX = {'decision': 'D', 'ledger': 'L', 'progress': 'P', 'acceptance': 'A',
          'judgment': 'J', 'constraint': 'C'}
LEDGER_KINDS = {'visual', 'ux', 'bug', 'arch', 'gate'}
# 只能由人翻的「完成」：审美、手感、真实产品里点一下才算。机器再怎么跑命令也判不了
# 「这样够不够好看 / 顺不顺手」，所以这类条目关闭时停在 awaiting_human，不直接 done。
# 依据：agent 循环里执行层（磨距离）归机器，判断层（目标本身对不对）归人；把最后一票
# 交给机器，等于删掉了高层反馈 —— 它会又快又狠地冲向一个没人质疑过的错误目标。
HUMAN_JUDGMENT_KINDS = {'visual', 'ux'}
AWAITING_HUMAN = 'awaiting_human'
LANES = REPO / 'ops' / 'tasks' / 'lanes.json'
# 从具体到泛化：第一条命中的规则就是归类结果。可用 central.config.json 的
# lane_rules 覆盖；不配就用这套通用默认。
_DEFAULT_LANE_RULES = [
    ('design', ('visual', '视觉', '排版', '样式', '导航', '侧栏', '侧边栏', 'rail',
                'layout', 'css', 'ui', '遮挡', '裁切', '对齐', '间距', '字号', '颜色',
                '图标', '圆角', '阴影', 'hover', '动画', '按钮位置', '边距')),
    ('design', ('体验', '交互', '流程', '点击', '跳转', '反馈', '入口', '操作路径',
                '上手', '可用性', 'ux', '别扭', '顺手')),
    ('bug', ('bug', '报错', '崩溃', '异常', 'error', 'crash', '不生效', '回归',
             '坏了', '失效', '卡住', '白屏', '闪退', 'undefined', 'null', '失败')),
    ('arch', ('refactor', '重构', '架构', '依赖', '性能', 'perf', '拆分', '抽象',
              '耦合', '内存', '并发', '缓存', '索引')),
    ('gate', ('gate', '门禁', 'verify', '断言', 'ci', 'test', '构建失败', 'build 失败',
              '编译失败', '测试失败')),
]
LANE_RULES = [(r[0], tuple(r[1])) for r in CFG.get('lane_rules') or _DEFAULT_LANE_RULES]
DEFAULT_LANE = CFG.get('default_lane') or (LANE_RULES[0][0] if LANE_RULES else 'bug')
PRIORITY = {'bug': 0, 'gate': 1, 'arch': 2, 'ux': 3, 'visual': 4}


def _files() -> list:
    return [f for f in os.environ.get('KG_TRUTH_FILES', '').split(':') if f]


def _split_screenshots(value) -> list:
    """把 --screenshots / KG_TRUTH_SHOTS 归一成有序、去重的路径列表。

    截图是给人复核用的附件，不参与 verified 的证据闸门：没有 evidence 时，
    就算塞了十张图也不许把人工项推进「需要你验收」。
    """
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else str(value).replace(',', ':').split(':')
    out = []
    for item in raw:
        text = str(item or '').strip()
        if text and text not in out:
            out.append(text)
    return out


def _merge_screenshots(existing, incoming) -> list:
    """幂等合并截图：同 id 的追加收敛不能把上一版附件冲掉。"""
    return _split_screenshots(list(_split_screenshots(existing)) + list(_split_screenshots(incoming)))


def _read(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


FILES = {'decision': 'decisions.jsonl', 'ledger': 'ledger.jsonl',
         'progress': 'progress.jsonl', 'acceptance': 'acceptance.jsonl',
         'judgment': 'judgments.jsonl', 'constraint': 'constraints.jsonl'}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n')


def _append(rec: dict):
    path = TRUTH / FILES[rec['type']]
    with path.open('a') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


LOCK = TRUTH / '.truthctl.lock'


@contextlib.contextmanager
def _truth_lock():
    """把「读现有 id + 追加」关进一个临界区。

    没有这道锁时，两个进程（面板 / 自动挡 / 执行器）同时 add 会算出同一个 id，
    后写的那条按 id 覆盖先写的（JSONL 取最新），账面上静默丢一条 —— 见 L-0056。
    """
    if fcntl is None:
        yield
        return
    TRUTH.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _max_num(t: str) -> int:
    """台账里同类 id 的最大编号（按数字算，不按行数）。"""
    nums = []
    for r in _read(TRUTH / FILES[t]):
        m = re.match(r'^%s-(\d+)$' % PREFIX[t], str(r.get('id') or ''))
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0


def _normalize_project_identity(value: str) -> str:
    """把常见 Git remote 写法规整成跨 clone 一致的项目身份。"""
    text = (value or '').strip()
    scp_like = re.match(r'^(?:[^@/]+@)?([^:/]+):(.+)$', text)
    if scp_like and '://' not in text and not Path(text).is_absolute():
        host, remote_path = scp_like.groups()
        text = f'https://{host}/{remote_path}'
    if '://' in text:
        parsed = urlsplit(text)
        host = (parsed.hostname or '').lower()
        port = f':{parsed.port}' if parsed.port else ''
        remote_path = unquote(parsed.path).strip('/')
        identity = f'{host}{port}/{remote_path}' if host else remote_path
    else:
        identity = text
    return identity.rstrip('/').removesuffix('.git')


def _truth_project_identity() -> str:
    """返回跨 clone / worktree 稳定的项目身份。"""
    explicit = os.environ.get('KG_TRUTH_PROJECT_ID', '').strip()
    if explicit:
        return f'explicit:{explicit}'
    remote = proc_run(
        ['git', 'remote', 'get-url', 'origin'],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if remote.returncode == 0 and remote.stdout.strip():
        normalized = _normalize_project_identity(remote.stdout)
        if normalized:
            return f'remote:{normalized}'
    return f'root:{REPO.resolve()}'


def _truth_id_state_dir() -> Path:
    """返回按项目隔离的跨 worktree 编号状态目录。"""
    explicit = os.environ.get('KG_TRUTH_ID_STATE_DIR')
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_root = Path(
        os.environ.get(
            'KG_TRUTH_ID_STATE_ROOT',
            str(Path.home() / '.codex-autopilot' / 'truth-id-sequences'),
        )
    ).expanduser().resolve()
    digest = hashlib.sha256(
        _truth_project_identity().encode('utf-8')
    ).hexdigest()[:24]
    return state_root / digest


@contextlib.contextmanager
def _truth_id_lock():
    """跨 worktree 串行分配编号；同项目共用一个高水位。"""
    state_dir = _truth_id_state_dir()
    if fcntl is None:
        yield state_dir
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / '.truth-id-sequences.lock'
    with lock_path.open('a+') as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield state_dir
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _next_id(t: str) -> str:
    """下一个空闲 id。必须在 _truth_lock() 临界区里调用，否则并发会撞号。

    旧实现是 len(rows)+1：同 id 的状态覆盖行会让行数虚高、编号跳号，并发时两个
    进程还会算出同一个号（L-0056）。跨 worktree 时，各自台账的最大编号也可能
    相同，因此再叠一层按项目共享的高水位。
    """
    with _truth_id_lock() as state_dir:
        sequence_path = state_dir / 'truth-id-sequences.json'
        try:
            state = json.loads(sequence_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        try:
            reserved = int(state.get(t, 0))
        except (TypeError, ValueError):
            reserved = 0
        taken = _taken_ids(t)
        n = max(_max_num(t), reserved) + 1
        while f'{PREFIX[t]}-{n:04d}' in taken:
            n += 1
        state[t] = n
        temp_path = sequence_path.with_name(
            f'{sequence_path.name}.{os.getpid()}.tmp')
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True) + '\n')
        os.replace(temp_path, sequence_path)
        return f'{PREFIX[t]}-{n:04d}'


def _append_new(t: str, **fields) -> dict:
    """在临界区里分配 id 并追加一条新记录。所有「新条目」都走这里。"""
    with _truth_lock():
        rec = {'id': _next_id(t), 'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'type': t}
        rec.update(fields)
        _append(rec)
        return rec


def _taken_ids(t: str) -> set:
    return {r.get('id') for r in _all_truth_rows(t)}


def _origin_main_rows(t: str) -> list:
    relative = Path('ops') / 'truth' / FILES[t]
    if REFERENCE_REPO == REPO:
        return []
    result = proc_run(
        ['git', 'show', f'origin/main:{relative.as_posix()}'],
        cwd=REFERENCE_REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _truth_sources(t: str) -> list:
    """每个真相根单独返回，保留各自的 append-only 顺序。"""
    relative = Path('ops') / 'truth' / FILES[t]
    sources = [_read(TRUTH / FILES[t])]
    if REFERENCE_REPO != REPO:
        sources.append(_read(REFERENCE_REPO / relative))
        origin_rows = _origin_main_rows(t)
        if origin_rows:
            sources.append(origin_rows)
    return sources


def _all_truth_rows(t: str) -> list:
    return [row for source in _truth_sources(t) for row in source]


def next_free_id(t: str, start: int) -> str:
    taken = _taken_ids(t)
    n = start
    while f'{PREFIX[t]}-{n:04d}' in taken:
        n += 1
    return f'{PREFIX[t]}-{n:04d}'


def cmd_add(args):
    if len(args) < 2 or args[0] not in PREFIX:
        sys.exit('用法: truthctl add <decision|ledger|progress|acceptance> "<标题>"')
    t, title = args[0], ' '.join(args[1:])
    fields = {'title': title,
              'lane': os.environ.get('KG_TRUTH_LANE') or None,
              'files': _files(),
              'source': os.environ.get('KG_TRUTH_SOURCE', 'agent')}
    if t == 'ledger':
        fields.update(kind=os.environ.get('KG_TRUTH_KIND', 'bug'),
                      detail=os.environ.get('KG_TRUTH_DETAIL', ''),
                      status=os.environ.get('KG_TRUTH_STATUS', 'open'))
        # 登记时就能声明「这条的完成只能人判」：比如门禁类条目里含一步「上线前手动点一遍」。
        # 只有 acceptance 支持这个字段是不够的 —— 收口门禁看的是 ledger。
        if os.environ.get('KG_TRUTH_NEEDS_HUMAN', '') == '1':
            fields['needs_human'] = True
    elif t == 'decision':
        fields.update(decision=os.environ.get('KG_TRUTH_DECISION', ''),
                      why=os.environ.get('KG_TRUTH_WHY', ''),
                      alternatives=[a for a in os.environ.get('KG_TRUTH_ALTERNATIVES', '').split('|') if a],
                      status=os.environ.get('KG_TRUTH_STATUS', 'active'))
    elif t == 'progress':
        fields.update(summary=os.environ.get('KG_TRUTH_SUMMARY', ''),
                      commit=os.environ.get('KG_TRUTH_COMMIT') or None,
                      landed=os.environ.get('KG_TRUTH_LANDED', '') == '1',
                      status=os.environ.get('KG_TRUTH_STATUS', 'doing'))
    elif t == 'acceptance':
        fields.update(level=os.environ.get('KG_TRUTH_LEVEL', 'P1'),
                      status=os.environ.get('KG_TRUTH_STATUS', 'todo'),
                      needs_human=os.environ.get('KG_TRUTH_NEEDS_HUMAN', '') == '1',
                      steps=os.environ.get('KG_TRUTH_STEPS', ''),
                      evidence=os.environ.get('KG_TRUTH_EVIDENCE', ''),
                      source_hash=os.environ.get('KG_TRUTH_SOURCE_HASH') or None)
    elif t == 'judgment':
        fields.update(statement=os.environ.get('KG_TRUTH_STATEMENT', '') or title,
                      why=os.environ.get('KG_TRUTH_WHY', ''),
                      scope=os.environ.get('KG_TRUTH_SCOPE', 'process'),
                      status='active')
    rec = _append_new(t, **fields)
    print(f"已记入真相 [{rec['id']}] {t}：{title[:70]}")


def cmd_accept(args):
    """登记一个验收项，并分清楚「机器能自证」还是「必须人看」。

    机器能自证的（改完跑门禁、命令 + 退出码写进证据）：走 add acceptance，落盘时
    `kgctl land` 会自动写成 verified，面板归到「已机器验收」。

    需要人拍板的（审美、手感、真实产品里点一下才算）：加 --human，并把「人要怎么
    复核」用 --steps 写清楚。面板会把它单列成「需要你验收」，原样展示这些步骤——
    这样用户不用读代码、不用猜要怎么看。

    用法：
      truthctl accept "<标题>" [--human] [--steps "1) 打开X 2) 点Y 3) 看到Z"]
                                [--screenshots "path1:path2"]
                                [--level P0] [--lane <分支>] [--status todo]
      truthctl accept --id <A-id> --status verified [--note "..."]
                                 （收敛一条已有验收项：人工拍板或机器复核后翻状态）
    """
    if not args:
        sys.exit('用法: truthctl accept "<标题>" [--human --steps "..."] [--level P1]')
    human = '--human' in args
    title_parts, steps, screenshots, level, lane, status = [], '', [], None, None, None
    acc_id, note = None, ''
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--human':
            i += 1
        elif a == '--id' and i + 1 < len(args):
            acc_id = args[i + 1]
            i += 2
        elif a == '--note' and i + 1 < len(args):
            note = args[i + 1]
            i += 2
        elif a == '--steps' and i + 1 < len(args):
            steps = args[i + 1]
            i += 2
        elif a == '--screenshots' and i + 1 < len(args):
            screenshots = _split_screenshots(args[i + 1])
            i += 2
        elif a == '--level' and i + 1 < len(args):
            level = args[i + 1]
            i += 2
        elif a == '--lane' and i + 1 < len(args):
            lane = args[i + 1]
            i += 2
        elif a == '--status' and i + 1 < len(args):
            status = args[i + 1]
            i += 2
        else:
            title_parts.append(a)
            i += 1
    if acc_id:
        # 收敛路径：同 id 追加一条状态覆盖行，不新开条目。
        if not status:
            sys.exit('accept --id 必须带 --status（todo / wip / blocked / verified）')
        rec = _set_acceptance_status(
            acc_id, status, note,
            evidence=os.environ.get('KG_TRUTH_EVIDENCE', ''),
            source=os.environ.get('KG_TRUTH_SOURCE'),
            screenshots=_split_screenshots(os.environ.get('KG_TRUTH_SHOTS', '')))
        print('已收敛验收 [%s] -> %s：%s'
              % (rec['id'], rec['status'], rec.get('title', '')[:70]))
        return
    title = ' '.join(title_parts)
    if not title:
        sys.exit('accept 需要一句标题：truthctl accept "<标题>"')
    rec = _append_new('acceptance', title=title,
                      level=level or os.environ.get('KG_TRUTH_LEVEL', 'P1'),
                      status=status or os.environ.get('KG_TRUTH_STATUS', 'todo'),
                      needs_human=human or os.environ.get('KG_TRUTH_NEEDS_HUMAN', '') == '1',
                      steps=steps or os.environ.get('KG_TRUTH_STEPS', ''),
                      screenshots=_merge_screenshots(
                          [], screenshots or os.environ.get('KG_TRUTH_SHOTS', '')),
                      evidence=os.environ.get('KG_TRUTH_EVIDENCE', ''),
                      source_hash=os.environ.get('KG_TRUTH_SOURCE_HASH') or None,
                      lane=lane or os.environ.get('KG_TRUTH_LANE') or None,
                      files=_files(),
                      source=os.environ.get('KG_TRUTH_SOURCE', 'agent'))
    who = '需要人工验收' if rec['needs_human'] else '机器自证'
    print(f"已登记验收 [{rec['id']}]（{who}）{title[:70]}")
    if rec['needs_human'] and not rec['steps']:
        print('  提示：这是人工验收项，但没写 --steps。面板会展示步骤，建议补一句'
              '「人要怎么复核」。')


def cmd_promote(args):
    if len(args) < 2:
        sys.exit('用法: truthctl promote <inbox 序号> <visual|ux|bug|arch>')
    n, kind = int(args[0]), args[1]
    if kind not in LEDGER_KINDS:
        sys.exit(f'kind 必须是 {sorted(LEDGER_KINDS)}')
    items = _read(INBOX)
    if n < 1 or n > len(items):
        sys.exit(f'inbox 只有 {len(items)} 条，序号 {n} 越界')
    it = items[n - 1]
    rec = _append_ledger_with_pm(
        kind, it.get('text', '')[:80], it.get('text', ''), it.get('files') or [],
        f"inbox#{n}", it.get('lane'))
    # 回写 inbox 的 routed 标记
    items[n - 1]['routed'] = True
    items[n - 1]['ledger_id'] = rec['id']
    with INBOX.open('w') as fh:
        for x in items:
            fh.write(json.dumps(x, ensure_ascii=False) + '\n')
    print(f"inbox #{n} -> 真相 [{rec['id']}] {kind}")


def _next_ledger_id() -> str:
    with _truth_lock():
        return _next_id('ledger')


def _latest_by_id(rows: list) -> dict:
    """JSONL 只追加，id 不变：同 id 的后续记录覆盖前一条，得到当前状态。"""
    out = {}
    for r in rows:
        rid = r.get('id')
        if rid:
            out[rid] = r
    return out


def _latest_ledger(rows: list) -> dict:
    """问题台账按事件时间收敛；其它 JSONL 仍按物理追加顺序取最新。"""
    return latest_ledger_by_id(rows)


def _find_ledger(rid: str) -> dict:
    return _latest_ledger(_read(TRUTH / FILES['ledger'])).get(rid)


def _rewrite_jsonl(path: Path, rows: list) -> None:
    """就地重写台账：只用于状态翻转，把同 id 记录收敛成一条。"""
    with path.open('w') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


def _tokens(text: str) -> set:
    """提取可区分的代码路径、文件名和 CamelCase 标识，避免按泛词误路由。"""
    out = set()
    for raw in re.findall(r'[A-Za-z0-9_./-]+', text or ''):
        token = raw.strip('`\'"(),:;[]{}<>').lower()
        if len(token) < 8:
            continue
        if any(ch in token for ch in './_-'):
            out.add(token)
            base = token.rsplit('/', 1)[-1]
            if len(base) >= 8:
                out.add(base)
            stem = base.rsplit('.', 1)[0]
            if len(stem) >= 8:
                out.add(stem)
    for raw in re.findall(r'[A-Za-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*', text or ''):
        if len(raw) >= 8:
            out.add(raw.lower())
    return out


def infer_kind(text: str, files, current: str = 'unknown') -> str:
    """把用户的自然语言粗分成 kind；未知不是默认 bug，而是继续按语义猜。"""
    if current in ('visual', 'ux', 'bug', 'arch'):
        return current
    low = ((text or '') + ' ' + ' '.join(files or [])).lower()
    buckets = [
        # gate 先于 bug：门禁红灯常带「失败/断言」，不先判就会被泛词吞成 bug。
        ('gate', ('gate', '门禁', 'verify', '断言', 'ci', 'test', '构建失败',
                  'build 失败', '编译失败', '测试失败')),
        ('bug', ('bug', '报错', '崩溃', '异常', 'error', 'crash', '不生效', '回归',
                 '坏了', '失效', '卡住', '白屏', '闪退', 'undefined', 'null', '失败')),
        ('arch', ('refactor', '重构', '架构', '依赖', '性能', 'perf', '拆分', '抽象',
                  '耦合', '内存', '并发', '缓存', '索引')),
        ('visual', ('visual', '视觉', '排版', '样式', '导航', '侧栏', '侧边栏', 'rail',
                    'layout', 'css', 'ui', '遮挡', '裁切', '对齐', '间距', '字号',
                    '颜色', '图标', '圆角', '阴影', 'hover', '动画', '按钮位置', '边距')),
        ('ux', ('体验', '交互', '流程', '点击', '跳转', '反馈', '入口', '操作路径',
                '上手', '可用性', 'ux', '别扭', '顺手')),
    ]
    for kind, keys in buckets:
        if any(k.lower() in low for k in keys):
            return kind
    return current if current in LEDGER_KINDS else 'bug'


_KIND_LANE = {'visual': 'design', 'ux': 'design', 'bug': 'bug',
              'arch': 'arch', 'gate': 'gate'}


def infer_lane(kind: str, text: str, files) -> str:
    """kind 决定泳道，关键词只做兜底。

    之前是关键词优先，于是「点击导出没反馈，疑似 bug」里的「点击 / 反馈」把一条
    bug 拽进了 design 泳道——泛化词会盖住真正的分诊结果。kind 已经是语义分诊的
    产物，泳道只是它的归属，所以先按 kind 落，kind 拿不准时才看关键词。
    """
    if kind in _KIND_LANE:
        return _KIND_LANE[kind]
    low = ((text or '') + ' ' + ' '.join(files or [])).lower()
    for lane, keys in LANE_RULES:
        if any(k.lower() in low for k in keys):
            return lane
    return DEFAULT_LANE


def _append_ledger(kind, title, detail, files, source, lane,
                   acceptance_plan=None, closure_package_draft=None,
                   pm_research=None) -> dict:
    fields = {'kind': kind, 'title': title, 'detail': detail,
              'status': 'open', 'lane': lane, 'files': files or [], 'source': source}
    if acceptance_plan is not None:
        fields['acceptance_plan'] = acceptance_plan
    if closure_package_draft is not None:
        fields['closure_package'] = closure_package_draft
    if pm_research is not None:
        fields['pm_research'] = pm_research
    return _append_new('ledger', **fields)


def _append_ledger_with_pm(kind, title, detail, files, source, lane,
                           acceptance_plan=None, closure_package_draft=None) -> dict:
    """一次写入 ledger 与 PM 绑定，保持稳定 intent_id 且不制造重复工作项。"""
    with _truth_lock():
        rec = {'id': _next_id('ledger'), 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
               'type': 'ledger', 'kind': kind, 'title': title, 'detail': detail,
               'status': 'open', 'lane': lane, 'files': files or [], 'source': source}
        if acceptance_plan is not None:
            rec['acceptance_plan'] = acceptance_plan
        if closure_package_draft is not None:
            rec['closure_package'] = closure_package_draft
        rec.update(_pm_fields(_pm_research_for_ledger(rec, source=source)))
        _append(rec)
        return rec


def _pm_research_for_ledger(rec, source='route') -> dict:
    """为新工作项创建 PM 研究请求；研究只挂到 ledger，不替用户做决策。

    这是 route 的写入阶段才调用的副作用。预演仍然只显示分级，不创建 research.jsonl，
    这样「看一眼会怎么路由」不会污染项目真相。
    """
    try:
        return pm_decision.ensure_for_ledger(
            REPO, ledger_id=rec.get('id'), title=rec.get('title') or rec.get('detail'),
            kind=rec.get('kind') or 'unknown', files=rec.get('files') or [],
            project_id=os.environ.get('KG_TRUTH_PROJECT_ID'), source=source)
    except Exception as exc:
        # 研究模块失效不能阻塞原有唯一入口；保留可见的 unknown，后续 PM 简报会如实报错。
        return {'status': 'unknown', 'error': str(exc),
                'framing': {'research_level': 'unknown', 'needs_research': None}}


def _pm_fields(result: dict) -> dict:
    """把 PM 研究请求投影到首次 ledger 记录，避免为同一工作项制造重复行。"""
    if not result or result.get('status') in ('not_required', 'unknown'):
        return {}
    request = result.get('request') or {}
    fields = {'pm_research': {
        'status': result.get('status'),
        'research_id': request.get('research_id'),
        'research_level': (result.get('framing') or {}).get('research_level'),
        'risk_level': (result.get('framing') or {}).get('risk_level'),
    }}
    return fields if fields['pm_research']['research_id'] else {}


def _compile_acceptance_plan(text, kind, files, lane) -> dict:
    """路由预演时编译验收标准；编译器异常不能阻塞唯一入口。"""
    try:
        return acceptance_criteria_compiler.compile_criteria(
            text=text or '', kind=kind or '', files=list(files or []),
            lane=lane or '', cfg=CFG)
    except Exception as exc:
        return {
            'status': 'unknown',
            'summary': '验收标准编译失败：%s' % exc,
            'lane': lane or '',
            'machine_checks': [],
            'human_checks': [],
            'needs_human': False,
            'source': 'acceptance-criteria-compiler',
        }


def _compile_closure_package(text, kind, files, lane, acceptance_plan=None) -> dict:
    """把一条新路由项编成 PRD 5.0 的闭环施工包草案。

    路由时大多数字段只能从原话和文件线索里推到草案，不能伪装成已经完整。
    这里统一委托给 closure_package.for_ledger，避免路由草案和人工收敛草案
    两套逻辑漂移；草案永远是 planning_incomplete，不能直接进入 execute。
    """
    return closure_package.for_ledger(
        {"title": text, "detail": text, "files": files or [], "lane": lane,
         "kind": kind, "acceptance_plan": acceptance_plan},
        text=text, kind=kind, lane=lane, files=files,
        acceptance_plan=acceptance_plan)


def _lane_names() -> set:
    """固定泳道名的唯一读取口径：lanes.json 优先，缺文件时用配置内置。"""
    data = []
    if LANES.exists():
        try:
            data = json.loads(LANES.read_text())
        except Exception:
            data = []
    if not isinstance(data, list):
        data = []
    return {r.get('name') for r in data if isinstance(r, dict) and r.get('name')} \
        or {r.get('name') for r in CFG.get('lanes') or [] if r.get('name')}


def _candidates(kind, text, files):
    """在既有泳道里找这条 intake 该去哪：文件路径优先，代码标识匹配兜底。

    归并必须同时满足两点：
      1) kind 相同。arch 和 gate 都可能改同一个守护进程文件，但它们是两件不同的事；
         只凭文件重叠归并，会把一条架构需求挂到磁盘门禁下面。
      2) 至少有一个**具体标识**命中（同一文件路径，或同一个代码标识）。只共享目录
         前缀 / 同一个大文件名不算：`central_daemon.py` 这种公共文件几乎每条记录都有。
    """
    files = list(files or [])
    query_tokens = _tokens(text or '')
    out = []
    for line in _read(TRUTH / FILES['ledger']):
        if line.get('status') not in ('open', 'doing', 'todo'):
            continue
        if kind and line.get('kind') and line.get('kind') != kind:
            continue
        own = list(line.get('files') or [])
        exact = len(set(files) & set(own))
        own_text = ' '.join([line.get('title', ''), line.get('detail', ''), *own])
        token_hits = len(query_tokens & _tokens(own_text)) if query_tokens else 0
        if not exact and not token_hits:
            continue
        out.append((1 if (exact or token_hits) else 0, exact, 0, line['id'],
                    line.get('lane'), line.get('title', ''), token_hits))
    out.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[6], x[3]))
    return out


def cmd_lanes(args):
    lanes = _read(LANES) if False else None
    # lanes.json 是对象数组，单独读
    data = []
    if LANES.exists():
        try:
            data = json.loads(LANES.read_text())
        except Exception:
            data = []
    if not isinstance(data, list):
        data = []
    if args and args[0] == 'init':
        if data:
            print('lanes.json 已存在，不覆盖'); return
        seed = CFG.get('lanes') or []
        LANES.parent.mkdir(parents=True, exist_ok=True)
        LANES.write_text(json.dumps(seed, ensure_ascii=False, indent=1) + '\n')
        print(f'已建 {LANES.relative_to(REPO)}（{len(seed)} 条固定泳道）')
        return
    if not data:
        print('lanes.json 不存在，先跑 truthctl lanes init'); return
    print('固定泳道（每个 intake 必须落到其中一条）：')
    for r in data:
        print(f"  {r['name']:14s} {r.get('title','')}   gate={r.get('gate','')}")


def cmd_route(args):
    go = '--go' in args
    items = _read(INBOX)
    pending = [(i, it) for i, it in enumerate(items, 1) if not it.get('routed')]
    if not pending:
        print('没有待路由项'); return
    names = _lane_names()
    plan = []
    for i, it in pending:
        kind = infer_kind(it.get('text', ''), it.get('files'), it.get('kind'))
        cand = _candidates(kind, it.get('text', ''), it.get('files'))
        lane = cand[0][4] if cand and cand[0][4] else None
        if lane and names and lane not in names:
            lane = None
        if not lane:
            lane = infer_lane(kind, it.get('text', ''), it.get('files'))
        plan.append({'n': i, 'kind': kind, 'lane': lane, 'text': it.get('text', ''),
                     'files': it.get('files') or [], 'cand': cand[:2],
                     'acceptance_plan': _compile_acceptance_plan(
                         it.get('text', ''), kind, it.get('files') or [], lane)})
        plan[-1]['closure_package'] = _compile_closure_package(
            it.get('text', ''), kind, it.get('files') or [], lane,
            acceptance_plan=plan[-1]['acceptance_plan'])
    # text[:120] -> 已有台账 id。命中即「这条其实已经进过台账」，不再新建，但必须把它
    # 归并到那条台账并把 inbox 标 routed —— 否则重复输入会永远停在「待路由」，
    # 每轮 tick 都重复报同一批待办，进度永远清不干净。
    seen = {}
    for rec in _read(TRUTH / FILES['ledger']):
        rid = rec.get('id')
        seen.setdefault((rec.get('detail') or '')[:120], rid)
        for ev in rec.get('evidence') or []:
            seen.setdefault(ev.split('] ', 1)[-1][:120], rid)
    dups = [p for p in plan if p['text'][:120] in seen]
    plan = [p for p in plan if p['text'][:120] not in seen]
    if not plan and not dups:
        print('没有待路由项'); return
    print(f"待路由 {len(plan) + len(dups)} 项（新增 {len(plan)} / 归并既有 {len(dups)}）；"
          "每条先落固定泳道，再写入或归并问题台账，最后回写 inbox。\n")
    for p in plan:
        mark = f"  <已有 {p['cand'][0][3]}>" if p['cand'] else ''
        print(f" #{p['n']:03d} [{p['kind']:6s}] -> {p['lane']:14s} {p['text'][:78]}{mark}")
        label = acceptance_criteria_compiler.summary_label(p['acceptance_plan'])
        if label:
            print(f"       {label}")
        pkg = p.get('closure_package') or {}
        if pkg.get('status') == closure_package.STATUS_PLANNING_INCOMPLETE:
            print("       闭环施工包：规划未完成，补齐前不能进入 execute")
    for p in dups:
        print(f" #{p['n']:03d} [{p['kind']:6s}] -> 归并 {seen[p['text'][:120]]}  {p['text'][:70]}")
    if not go:
        print('\n预演结束。确认后加 --go 落账（只追加/归并 ledger、回写 routed，不动代码）。')
        return
    made = []
    for p in plan:
        it = items[p['n'] - 1]
        if p['cand']:
            ref = p['cand'][0][3]
            it['lane'], it['ledger_id'], it['routed'] = p['lane'], ref, True
            target = None
            for rec in _read(TRUTH / FILES['ledger']):
                if rec.get('id') == ref:
                    target = rec
                    break
            note = f"[inbox#{p['n']}] {p['text']}"
            if target is not None:
                ev = list(target.get('evidence') or [])
                if note not in ev:
                    ev.append(note)
                    target['evidence'] = ev
                if not target.get('acceptance_plan'):
                    target['acceptance_plan'] = p['acceptance_plan']
                if not target.get('closure_package'):
                    target['closure_package'] = p['closure_package']
                path = TRUTH / FILES['ledger']
                rows = _read(path)
                for i, rec in enumerate(rows):
                    if rec.get('id') == ref:
                        rows[i] = target
                with path.open('w') as fh:
                    for rec in rows:
                        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        else:
            rec = _append_ledger_with_pm(
                p['kind'], p['text'][:80].replace('\n', ' '), p['text'], p['files'],
                f"inbox#{p['n']}", p['lane'], acceptance_plan=p['acceptance_plan'],
                closure_package_draft=p['closure_package'])
            it['lane'], it['ledger_id'], it['routed'] = rec['lane'], rec['id'], True
            made.append(rec['id'])
    merged = 0
    for p in dups:
        it = items[p['n'] - 1]
        ref = seen[p['text'][:120]]
        it['lane'], it['ledger_id'], it['routed'] = p['lane'], ref, True
        note = f"[inbox#{p['n']}] {p['text']}"
        path = TRUTH / FILES['ledger']
        rows = _read(path)
        for j, rec in enumerate(rows):
            if rec.get('id') == ref:
                ev = list(rec.get('evidence') or [])
                if note not in ev:
                    ev.append(note)
                    rec['evidence'] = ev
                if not rec.get('acceptance_plan'):
                    rec['acceptance_plan'] = p['acceptance_plan']
                if not rec.get('closure_package'):
                    rec['closure_package'] = p['closure_package']
                rows[j] = rec
                break
        with path.open('w') as fh:
            for rec in rows:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        merged += 1
    with INBOX.open('w') as fh:
        for x in items:
            fh.write(json.dumps(x, ensure_ascii=False) + '\n')
    print(f"\n已落账：新增问题台账 {len(made)} 条 {made}，归并 {merged} 条到既有台账；inbox 全部标 routed。")


def cmd_next(args):
    """只回答一个问题：现在最该动的是哪几条，用户不用自己翻台账。"""
    all_led = list(_latest_ledger(_read(TRUTH / 'ledger.jsonl')).values())
    led = [r for r in all_led if r.get('status') in ('open', 'doing')]
    led.sort(key=lambda r: (PRIORITY.get(r.get('kind'), 9), r.get('ts', '')))
    waiting = [r for r in all_led if r.get('status') == AWAITING_HUMAN]
    acc = [r for r in _latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values()
           if r.get('status') == 'blocked']
    print('=== 自动挡 · 现在做什么 ===')
    if waiting:
        # 单列：这些活机器已经做完了，「完成」这一票在你手上。混进「优先处理」
        # 会让 agent 以为还有活要干，反复重做同一件事。
        print('等你确认（做完了，但要你看过才算完）：')
        for r in waiting:
            print(f"  {r['id']} [{r.get('kind','')}] {r.get('title','')}")
    if acc:
        print('被卡住的验收项（需要用户拍板或外部条件）：')
        for r in acc:
            print(f"  {r['id']} [{r.get('level','')}] {r.get('title','')}")
    if not led and not acc and not waiting:
        print('没有未决项：可以跑 kgctl reap --go 回收已合并分支，或接收新输入。')
        return
    print('优先处理：')
    for i, r in enumerate(led[:8], 1):
        lane = f" -> {r.get('lane')}" if r.get('lane') else ' -> 未归属'
        print(f"  {i}. {r['id']} [{r.get('kind','')}] {r.get('title','')[:88]}{lane}")
    print(f'\n下一步：python3 {_kgctl_cmd()} tick（收拢新输入并刷新）/ land <分支>（落盘已验证分支）')


def cmd_ls(args):
    t = args[0] if args else None
    types = [t] if t in PREFIX else list(PREFIX)
    for tt in types:
        rows = _read(TRUTH / FILES[tt])
        print(f'\n=== {tt}（{len(rows)}）===')
        for r in rows:
            lane = f" lane={r['lane']}" if r.get('lane') else ''
            print(f"  [{r['id']}] {r.get('status',''):8s} {r.get('title','')[:70]}{lane}")


def cmd_product(args):
    """产品状态：产品今天是怎么工作的。

    它不是又一份需求文档，而是「线上/当前这个产品实际会怎么表现」的一段可 diff 的
    事实。Coding Agent 改完行为，在同一个改动里更新它；人验收时只看它的变化，
    不读实现。
    """
    if args and args[0] in ('set', 'upsert'):
        if len(args) < 3:
            sys.exit('用法: truthctl product set "<模块>" "<行为>"（why/files 走环境变量）')
        area, behavior = args[1], ' '.join(args[2:])
        data = _read_json(PRODUCT_STATE)
        modules = data.get('modules') if isinstance(data.get('modules'), list) else []
        now = time.strftime('%Y-%m-%d')
        rec = {'area': area, 'behavior': behavior,
               'why': os.environ.get('KG_PS_WHY', ''),
               'files': [f for f in os.environ.get('KG_PS_FILES', '').split(':') if f],
               'updated': now}
        replaced = False
        for i, m in enumerate(modules):
            if m.get('area') == area:
                modules[i] = rec
                replaced = True
                break
        if not replaced:
            modules.append(rec)
        data['modules'] = modules
        data['updated'] = now
        _write_json(PRODUCT_STATE, data)
        print(f"{'已更新' if replaced else '已记录'}产品状态 [{area}]：{behavior[:70]}")
        return

    data = _read_json(PRODUCT_STATE)
    modules = data.get('modules') if isinstance(data.get('modules'), list) else []
    print('=== 产品状态 · 产品今天是怎么工作的 ===')
    if not modules:
        print('（还没有记录。改动的会话应在同一轮写进来：')
        print('  truthctl product set "<模块>" "<行为>"，why/files 走 KG_PS_WHY / KG_PS_FILES）')
    for m in modules:
        print(f"  [{m.get('area', '')}] {m.get('behavior', '')}")
        if m.get('why'):
            print(f"      为什么：{m.get('why')}")
    print(f"\n共 {len(modules)} 个模块行为，最后更新 {data.get('updated') or '—'}")
    diff = _product_diff()
    if diff:
        print('\n相对主干的变化（人验收只看这一段）：')
        print(diff)


def _product_diff() -> str:
    """相对主干的产品状态变化。人只需要看这一段，不需要读代码或 PR。"""
    main = CFG.get('main_branch') or 'main'
    rel = str(PRODUCT_STATE.relative_to(REPO))
    p = proc_run(['git', '-C', str(REPO), 'diff', '--unified=1', '--no-color',
                        main, '--', rel],
                       capture_output=True, text=True, errors='replace')
    if p.returncode != 0 or not p.stdout.strip():
        return ''
    lines = [ln for ln in p.stdout.splitlines()
             if ln[:1] in '+-' and not ln.startswith(('+++', '---'))]
    return '\n'.join(lines[:60])


def cmd_distill(args):
    """蒸馏：每轮回看人额外说了什么，只把「判断背后的判断逻辑」写回。

    只记一次性细节没有用——下次换个地方还得重新说。要记的是「为什么这么判断」，
    Agent 才能把它用到没见过的情况上。
    """
    if args and args[0] == 'add':
        if len(args) < 2:
            sys.exit('用法: truthctl distill add "<判断逻辑>"（why/scope 走环境变量）')
        statement = ' '.join(args[1:])
        rec = _append_new('judgment', title=statement[:80], statement=statement,
                          why=os.environ.get('KG_TRUTH_WHY', ''),
                          scope=os.environ.get('KG_TRUTH_SCOPE', 'process'),
                          source=os.environ.get('KG_TRUTH_SOURCE', 'agent'),
                          status='active')
        print(f"已蒸馏 [{rec['id']}]（{rec['scope']}）：{statement[:70]}")
        return

    print('=== 蒸馏回看 ===')
    print('这一轮人额外说了什么（原话，来自唯一入口）：')
    inbox = _read(INBOX)
    recent = inbox[-12:]
    if recent:
        for i, it in enumerate(recent, len(inbox) - len(recent) + 1):
            print(f"  #{i:03d} {it.get('text', '')[:96]}")
    else:
        print('  （唯一入口还没有收到输入）')

    blocked = [r for r in _latest_by_id(_read(TRUTH / FILES['acceptance'])).values()
               if r.get('status') == 'blocked']
    if blocked:
        print('\n被卡住、需要人拍板的地方：')
        for r in blocked:
            print(f"  {r['id']} {r.get('title', '')}")

    active = [r for r in _latest_by_id(_read(TRUTH / FILES['judgment'])).values()
              if r.get('status') == 'active']
    print(f"\n已蒸馏的判断逻辑（{len(active)} 条）：")
    if not active:
        print('  （还没有。把「为什么这么判断」写回：truthctl distill add "<判断逻辑>"）')
    for r in active[-10:]:
        print(f"  [{r['id']}/{r.get('scope', '')}] {r.get('statement') or r.get('title', '')}")
    print('\n只把「背后的判断逻辑」写回，不记一次性细节；范围用 KG_TRUTH_SCOPE'
          '（product/design/arch/process）。')


def cmd_constraints(args):
    """约束台账：把用户说过的预期/返修/决策/禁忌收成一条带 scope 的约束。

    与 `judgments.jsonl` 的区别（这条分工是有意的，别混）：
      * judgment 记「以后遇到同类情况该怎么判断」（判断逻辑，跨场景复用）；
      * constraint 记「这个项目的这个范围必须/不得如何」（带 scope，可被精准调取，
        实现某处改动时只带相关的那几条）。
    约束写进来不是终点：能机器验证的走 `enforceable=gate`，审美/手感走
    `checklist`，剩下的诚实标 `none`（它就是在说「这条没有机器兜底」）。
    """
    mode = args[0] if args else None

    if mode in ('add', 'set', 'upsert'):
        statement = ' '.join(args[1:]).strip()
        if not statement:
            sys.exit('用法: truthctl constraints add "<约束原话>"'
                     '（KG_CONSTRAINT_KIND / KG_CONSTRAINT_SCOPE / '
                     'KG_CONSTRAINT_ENFORCEABLE 走环境变量）')
        kind = (os.environ.get('KG_CONSTRAINT_KIND') or 'expectation').strip()
        enforceable = (os.environ.get('KG_CONSTRAINT_ENFORCEABLE') or 'none').strip()
        scope = [s for s in re.split(r'[:,\n]', os.environ.get('KG_CONSTRAINT_SCOPE', ''))
                 if s.strip()]
        # 标了 gate 就必须带命令，否则「有门禁」是空话：落盘那一刻没有东西可跑，
        # 面板却会渲染成有机器兜底（2026-11-17 实测 C-0001 就是这种假绿灯）。
        cmd = (os.environ.get('KG_CONSTRAINT_CMD') or '').strip()
        cwd = (os.environ.get('KG_CONSTRAINT_CWD') or '').strip() or None
        if enforceable == 'gate' and not cmd:
            sys.exit('enforceable=gate 必须同时给 KG_CONSTRAINT_CMD（真正要跑的验证命令）；'
                     '写不出断言就老实标 checklist 或 none。')
        rec = _append_new('constraint',
                          statement=statement, title=statement[:80],
                          kind=kind, enforceable=enforceable,
                          scope=scope, cmd=cmd or None, cwd=cwd,
                          policy_version=os.environ.get('KG_CONSTRAINT_POLICY') or None,
                          supersedes=os.environ.get('KG_CONSTRAINT_SUPERSEDES') or None,
                          source=os.environ.get('KG_TRUTH_SOURCE', 'user'),
                          status='active')
        where = '、'.join(scope) if scope else '全局'
        suffix = f"，门禁：{cmd}" if cmd else ""
        print(f"已记入约束 [{rec['id']}]（{kind}/{enforceable}，范围：{where}{suffix}）："
              f"{statement[:70]}")
        return

    if mode in ('retire', 'drop'):
        rid = (args[1] if len(args) > 1 else '').strip()
        if not rid:
            sys.exit('用法: truthctl constraints retire <C-id>')
        path = TRUTH / FILES['constraint']
        with _truth_lock():
            row = _latest_by_id(_read(path)).get(rid)
            if row is None:
                sys.exit(f'约束台账里没有 {rid}')
            merged = dict(row)
            merged['status'] = 'retired'
            merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _append(merged)
        print(f'已停用约束 [{rid}]')
        return

    if mode in ('-h', '--help'):
        print('用法: truthctl constraints add "<约束>" | truthctl constraints'
              ' [--files a:b] | truthctl constraints retire <C-id>')
        return

    # 列出台账；带 --files 时只列命中这些路径的（pid=精准调取）。
    files = []
    if '--files' in args:
        idx = args.index('--files')
        if idx + 1 < len(args):
            files = [s for s in re.split(r'[:,\n]', args[idx + 1]) if s.strip()]
    path = TRUTH / FILES['constraint']
    # 按 id 收敛后再数：追加式台账里同 id 会有多条状态覆盖行，直接数物理行会把
    # 停用条数算虚（实测把 1 条报成 2 条）。停用数 = 唯一 id 数 - 生效条数。
    unique_ids = {r.get('id') for r in constraints_mod.read_rows(path) if r.get('id')}
    retired = len(unique_ids) - len(constraints_mod.active_constraints(path))
    rows = (constraints_mod.matching_constraints(path, files) if files
            else constraints_mod.active_constraints(path))
    print(f'=== 约束台账（{len(rows)} 条生效'
          + (f'，另 {retired} 条已停用' if retired else '') + '）===')
    if files:
        print(f'（只看命中这些路径的：{"、".join(files)}）')
    if not rows:
        print('  （还没有约束。用 truthctl constraints add "<约束>" 记一条。）')
    for r in rows:
        print('  ' + constraints_mod.render_line(r))
    print('\n调取约定：scope 为空 = 全局约束（恒命中）；写了 scope 就只在改动命中时带上。')


def cmd_retire(args):
    """按 id 追加一条「已停用」投影；决策/判断/约束/进展统一走加法。"""
    if len(args) != 2 or args[0] not in ('decision', 'judgment', 'constraint', 'progress'):
        sys.exit('用法: truthctl retire <decision|judgment|constraint|progress> <id>')
    t, rid = args
    with _truth_lock():
        row = _latest_by_id(_read(TRUTH / FILES[t])).get(rid)
        if row is None:
            sys.exit(f'{t} 台账里没有 {rid}')
        merged = dict(row)
        merged['status'] = 'retired'
        merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        merged['retired_from'] = row.get('status')
        _append(merged)
    print(f'已停用 {t} [{rid}]')


def _coerce_evidence(evidence):
    """evidence 历史上出现过 str / list / None 三种类型；统一成单个字符串。"""
    if not evidence:
        return ''
    if isinstance(evidence, str):
        return evidence.strip()
    if isinstance(evidence, (list, tuple)):
        return '\n'.join(str(x) for x in evidence).strip()
    return str(evidence).strip()


def _set_ledger_status(rid, status, note, evidence='', commit='', extra_fields=None):
    # JSONL 只追加：同 id 的新记录覆盖旧记录，得到当前状态
    with _truth_lock():
        rec = _latest_ledger(_read(TRUTH / FILES['ledger'])).get(rid)
        if rec is None:
            sys.exit('问题台账里没有 %s（先 truthctl ls ledger 看现有条目）' % rid)
        merged = dict(rec)
        merged['status'] = status
        merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if note:
            merged['detail'] = (merged.get('detail', '') + '\n' if merged.get('detail') else '') + note
        if status == 'done':
            # fail-closed：标 done = 声明「这件事有可复核的证据证明已经关掉」。
            # 只有自由文本结论、既无证据又无可追溯 commit 的，一律拒绝——
            # 凭空标 done 会把首问闭环率这类度量喂成假的（实测 169 条 done 里
            # 有 89 条既无 evidence 也无 commit）。
            ev = _coerce_evidence(evidence)
            commit = str(commit or '').strip()
            if not ev and not commit:
                sys.exit(
                    '拒绝把 %s 标成 done：没有证据。\n'
                    '  KG_TRUTH_EVIDENCE="<命令/退出码/复现结果>"，或\n'
                    '  KG_TRUTH_COMMIT=<已合入 main 的 commit sha>（会据此派生证据）。'
                    % rid)
            if not ev and commit:
                ev = 'commit %s（已合入 main，可用 git show %s 复核）' % (commit, commit)
            merged['evidence'] = ev
            merged['closed'] = merged['ts']
        elif evidence:
            merged['evidence'] = _coerce_evidence(evidence)
        if status != 'done':
            # 重新打开或停在 awaiting_human 时不能留着上一次的关闭时间。
            # 留着就等于对外宣称「这条已经完成过」，面板会把它当已完成渲染。
            merged.pop('closed', None)
        if extra_fields:
            merged.update(extra_fields)
        _append(merged)
        return merged


def _update_ledger_record(rid, fields):
    """只更新指定字段，不改台账状态；用于施工包确认这种中间态。"""
    with _truth_lock():
        rec = _latest_ledger(_read(TRUTH / FILES['ledger'])).get(rid)
        if rec is None:
            sys.exit('问题台账里没有 %s（先 truthctl ls ledger 看现有条目）' % rid)
        merged = dict(rec)
        merged.update(fields)
        merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _append(merged)
        return merged


ACCEPTANCE_STATUSES = ('todo', 'wip', 'blocked', 'verified', 'void')


def _set_acceptance_status(rid, status, note='', evidence='', source=None,
                           screenshots=None):
    """按 id 收敛一条验收项（JSONL 只追加：同 id 的新记录覆盖旧记录）。

    这是「验收关闭口」。之前 acceptance.jsonl 只有 _append_new（永远分配新 id）、
    cmd_accept（只登记不更新），于是 needs_human 的项一旦登记就再也翻不成
    verified——面板「需要你介入」会永远挂着同一批，用户被逼着反复盯盘。人工项
    靠人拍板转 verified，机器项靠证据转 verified，两条都必须能落到同一 id 上。
    """
    if status not in ACCEPTANCE_STATUSES:
        sys.exit('status 必须是 %s' % ' / '.join(ACCEPTANCE_STATUSES))
    with _truth_lock():
        rec = _latest_by_id(_read(TRUTH / FILES['acceptance'])).get(rid)
        if rec is None:
            sys.exit('验收台账里没有 %s（先 truthctl ls acceptance 看现有条目）' % rid)
        merged = dict(rec)
        merged['status'] = status
        merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if note:
            merged['note'] = ((merged.get('note') + '\n') if merged.get('note')
                              else '') + note
        shots = _merge_screenshots(merged.get('screenshots') or [], screenshots or [])
        if shots:
            merged['screenshots'] = shots
        ev = _coerce_evidence(evidence)
        if ev:
            merged['evidence'] = ev
        if status == 'verified':
            # fail-closed：标 verified = 声明「这条已有可复核的落点」。
            # 人工拍板也算证据（人真的看过了）；但要留下是谁、什么时候拍的，
            # 否则事后没人能复核「谁说这条验过了」。
            merged['verified_by'] = source or os.environ.get('KG_TRUTH_SOURCE', 'user')
            merged['verified_at'] = merged['ts']
        else:
            # 停在 awaiting_human / 重新 open 时不能留着上一次的关闭时间：
            # 留着就等于对外宣称「这条已经完成过」，面板会把它当已完成渲染。
            merged.pop('closed', None)
        _append(merged)
        return merged


def cmd_close(args):
    """把一条问题台账标成 done，并追加一条进展：从「收到」走到「已关闭」。

    fail-closed：必须带可复核证据——KG_TRUTH_EVIDENCE（命令/退出码/复现结果）
    或 KG_TRUTH_COMMIT（已合入 main 的 commit sha，会据此派生证据）。
    只有自由文本结论、两者都没有的，拒绝关闭。

    但「done」这一格只能由人翻：视觉/体验（以及登记时标了 needs_human 的）条目，
    agent 跑再多命令也判不了「这样够不够好看 / 顺不顺手」。这类条目 close 时停在
    awaiting_human，把最后一步交回人；机器能自证的（bug/gate/arch）照旧直接 done，
    不打断自动派发的闭环。

    用法：
      truthctl close <L-id> "<结论/证据>"              机器可证的收口；人判类停在 awaiting_human
      truthctl close <L-id> "<结论>" --confirm-human  人已看过并认可，直接 done

    新路由项带闭环施工包时，施工包必须先由 PM 补齐并确认到 ready；关闭时还要给
    真实入口证据（KG_TRUTH_USAGE_EVIDENCE）。历史无施工包条目仍可关闭，但会被
    明确标成 closure_debt，不冒充新 PRD 闭环。
    """
    if not args:
        sys.exit('用法: truthctl close <L-id> "<结论/证据>"'
                 '（需 KG_TRUTH_EVIDENCE 或 KG_TRUTH_COMMIT；人判类可 --confirm-human）')
    rid = args[0]
    rest = [a for a in args[1:] if a != '--confirm-human']
    confirmed = ('--confirm-human' in args
                 or os.environ.get('KG_TRUTH_HUMAN_CONFIRMED', '') == '1')
    note = ' '.join(rest) if rest else ''
    prev = _latest_ledger(_read(TRUTH / FILES['ledger'])).get(rid) or {}
    if _needs_human_verdict(prev) and not confirmed:
        return _park_for_human(rid, prev, note)
    raw_commit = os.environ.get('KG_TRUTH_COMMIT') or ''
    evidence = os.environ.get('KG_TRUTH_EVIDENCE', '')
    if raw_commit.strip() and not evidence:
        evidence = ('commit %s（已合入 main，可用 git show %s 复核）'
                    % (raw_commit.strip(), raw_commit.strip()))
    if confirmed and not evidence:
        # 人的显式确认也是证据；否则 --confirm-human 会被 fail-closed 卡死。
        evidence = '用户确认：%s（--confirm-human）' % (note or '看过，可以')
    if not evidence and not raw_commit.strip():
        sys.exit(
            '拒绝把 %s 标成 done：没有证据。\n'
            '  KG_TRUTH_EVIDENCE="<命令/退出码/复现结果>"，或\n'
            '  KG_TRUTH_COMMIT=<已合入 main 的 commit sha>。' % rid)

    extra = {}
    package = prev.get('closure_package')
    if isinstance(package, dict):
        try:
            closed_pkg = closure_package.close(
                package, note=note, evidence=evidence,
                human_confirmed=confirmed,
                usage_evidence=os.environ.get('KG_TRUTH_USAGE_EVIDENCE', ''))
        except ValueError as exc:
            sys.exit('拒绝把 %s 标成 done：%s' % (rid, exc))
        closure_state = str((closed_pkg.get('closure') or {}).get('status') or '')
        if closure_state != 'closed':
            sys.exit('拒绝把 %s 标成 done：%s' % (
                rid,
                (closed_pkg.get('closure') or {}).get('reason')
                or closed_pkg.get('reason')
                or '闭环施工包未完成'))
        extra['closure_package'] = closed_pkg
        extra['closure_debt'] = False
    else:
        extra['closure_debt'] = True
        extra['closure_debt_reason'] = (
            '历史条目没有闭环施工包；按旧证据关门，不计入 PRD 5.0 闭环口径。')

    rec = _set_ledger_status(rid, 'done', note, evidence, raw_commit,
                             extra_fields=extra)
    commit = raw_commit.strip() or None
    prog = _append_new('progress', title='关闭 %s：%s' % (rid, rec.get('title', '')),
                       summary=note, commit=commit, landed=bool(commit),
                       lane=rec.get('lane'), files=rec.get('files') or [],
                       source=os.environ.get('KG_TRUTH_SOURCE', 'agent'),
                       status='done')
    print('已关闭 [%s] %s（同时记入进展 %s）' % (rid, rec.get('title', ''), prog['id']))


def _ledger_for_closure(rid):
    rec = _latest_ledger(_read(TRUTH / FILES['ledger'])).get(rid)
    if rec is None:
        sys.exit('问题台账里没有 %s（先 truthctl ls ledger 看现有条目）' % rid)
    return rec


def cmd_closure_package(args):
    """查看/补齐/确认一条工作项的闭环施工包。"""
    if not args or args[0] not in ('show', 'fill', 'confirm'):
        sys.exit('用法: truthctl closure-package <show|fill|confirm> <L-id> ...')
    sub, rid = args[0], args[1] if len(args) > 1 else ''
    if not rid:
        sys.exit('closure-package %s 必须带 ledger id' % sub)
    rec = _ledger_for_closure(rid)
    package = rec.get('closure_package')
    if not isinstance(package, dict):
        sys.exit('%s 没有闭环施工包；新路由项会自动生成，历史项需重新规划' % rid)

    if sub == 'show':
        verdict = closure_package.validate(package)
        print(json.dumps({
            'id': rid, 'closure_package': package, 'validation': verdict,
            'gate': closure_package.execution_gate(package),
        }, ensure_ascii=False, indent=2))
        return

    if sub == 'fill':
        raw = os.environ.get('KG_TRUTH_PACKAGE_JSON') or ''
        if len(args) > 2 and args[2] == '--json':
            raw = args[3] if len(args) > 3 else ''
        elif not raw and len(args) > 2:
            raw = args[2]
        if not raw:
            sys.exit('用法: truthctl closure-package fill <L-id> \'<json>\''
                     '（也可设 KG_TRUTH_PACKAGE_JSON）')
        try:
            updates = json.loads(raw)
        except ValueError as exc:
            sys.exit('闭环施工包补丁不是合法 JSON：%s' % exc)
        try:
            filled = closure_package.fill(package, updates)
        except ValueError as exc:
            sys.exit(str(exc))
        _update_ledger_record(rid, {'closure_package': filled})
        print('已补齐 [%s] 闭环施工包：%s' % (rid, filled['reason']))
        return

    rest = args[2:]
    note, evidence = '', ''
    i = 0
    while i < len(rest):
        if rest[i] == '--note' and i + 1 < len(rest):
            note = rest[i + 1]; i += 2
        elif rest[i] == '--evidence' and i + 1 < len(rest):
            evidence = rest[i + 1]; i += 2
        else:
            i += 1
    try:
        confirmed = closure_package.confirm(package, note=note, evidence=evidence)
    except ValueError as exc:
        sys.exit(str(exc))
    if (confirmed.get('confirmation') or {}).get('status') != 'confirmed':
        sys.exit('拒绝确认 [%s] 闭环施工包：%s' % (
            rid, (confirmed.get('confirmation') or {}).get('reason')
            or confirmed.get('reason')))
    _update_ledger_record(rid, {'closure_package': confirmed})
    print('已确认 [%s] 闭环施工包：可以进入 execute' % rid)


def _needs_human_verdict(rec: dict) -> bool:
    """这条的「完成」是不是只能由人判。

    两类：登记时就标了 needs_human 的，和天然属于审美/手感的 kind（视觉、体验）。
    bug/gate/arch 有客观判据（命令退出码、断言、diff），机器自证即可。
    """
    if rec.get('needs_human'):
        return True
    return rec.get('kind') in HUMAN_JUDGMENT_KINDS


def _park_for_human(rid: str, prev: dict, note: str) -> None:
    """把条目停在 awaiting_human：活干完了，但「完成」这一票留给用户。

    不写 closed 时间戳——写了就等于宣称它完成了。同时落一条进展，说明技术上到哪一步，
    这样面板和主线里都能看到「这条在等你，不在等机器」。
    """
    detail = note or '实现已完成，等用户确认视觉/手感'
    rec = _set_ledger_status(rid, AWAITING_HUMAN, detail,
                             os.environ.get('KG_TRUTH_EVIDENCE', ''))
    commit = os.environ.get('KG_TRUTH_COMMIT') or None
    _append_new('progress', title='待你确认 %s：%s' % (rid, rec.get('title', '')),
                summary=detail, commit=commit, landed=bool(commit),
                lane=rec.get('lane'), files=rec.get('files') or [],
                source=os.environ.get('KG_TRUTH_SOURCE', 'agent'),
                status=AWAITING_HUMAN)
    print('已停在「等你确认」[%s] %s' % (rid, rec.get('title', '')))
    print('  原因：这条属于 %s，好不好只能你看过再说，机器不替你签收。'
          % (prev.get('kind') or 'needs_human'))
    print('  你看过认为可以，再让它翻成 done：')
    print('    %s close %s "看过，可以" --confirm-human' % (_cmd_hint(), rid))


def _cmd_hint() -> str:
    """给用户抄的命令前缀（自包含安装下运行时不在项目目录里）。"""
    ops_dir = Path(__file__).resolve().parent
    if (ops_dir.parent / '.mcp.json').exists():
        return 'python3 %s' % (ops_dir / 'truthctl.py')
    return 'python3 ops/truthctl.py'


def cmd_open(args):
    """把已关闭的问题重新打开（回归 / 证据不足 / 用户再次复现）。"""
    if not args:
        sys.exit('用法: truthctl open <L-id> "<原因>"')
    rid = args[0]
    note = ' '.join(args[1:]) if len(args) > 1 else ''
    rec = _set_ledger_status(rid, 'open', note or '重新打开')
    print('已重新打开 [%s] %s' % (rid, rec.get('title', '')))


def _roadmap_rows(ml):
    """mainline.json 的 roadmap 归一成 [{step, expected, status}]。

    每一项可为字符串（只有步骤名）或 {step, expected, status}；
    status 属于 done | doing | next | todo，缺省按 todo。
    """
    rows = []
    for it in (ml.get('roadmap') or []):
        if isinstance(it, str):
            rows.append({'step': it, 'expected': '', 'status': 'todo'})
        elif isinstance(it, dict):
            rows.append({'step': it.get('step', ''), 'expected': it.get('expected', ''),
                         'status': it.get('status') or 'todo'})
    return rows


ROADMAP_MARK = {'done': '✓', 'doing': '●', 'next': '●', 'todo': '○'}
ROADMAP_LABEL = {'done': '已完成', 'doing': '进行中',
                 'next': '下一步', 'todo': '未开始'}


def cmd_synthesize(args):
    """综合：把决策/进展/问题/验收/产品状态合成一页产品主线。

    mainline.json 是人拍板的方向，不改；这份是 Agent 从台账里综合出来的现状。
    人只读这一页，就知道产品现在是什么样、最近在推什么、还差什么。

    `--if-changed` 给守护每拍调用：生成视图正文不变时保留原文件和原生成时间，
    避免常驻自追踪把同一份主线反复写成「刚更新」并制造无意义的 git 脏改动。
    """
    if_changed = '--if-changed' in args
    args = [a for a in args if a != '--if-changed']
    ml = _read_json(TRUTH / 'mainline.json')
    dec = [r for r in _latest_by_id(_read(TRUTH / 'decisions.jsonl')).values()
           if r.get('status') == 'active']
    led = list(_latest_ledger(_read(TRUTH / 'ledger.jsonl')).values())
    # retire 是追加的状态覆盖行：历史行留着，但停用后的进展不许再渲染进
    # 人看的主线（否则重复落盘会印两遍同一件事）。
    prg = sorted([r for r in _latest_by_id(_read(TRUTH / 'progress.jsonl')).values()
                  if (r.get('status') or 'active') != 'retired'],
                 key=lambda r: r.get('ts', ''))
    acc = list(_latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values())
    ps = _read_json(PRODUCT_STATE)
    modules = ps.get('modules') if isinstance(ps.get('modules'), list) else []
    open_led = [r for r in led if r.get('status') in ('open', 'doing')]
    done_led = [r for r in led if r.get('status') == 'done']
    landed = [r for r in prg if r.get('landed')]
    verified = [r for r in acc if r.get('status') == 'verified']

    # 生成时间必须位于正文之外：守护逐拍 synthesize 时，只有真相内容真的变化
    # 才应该刷新它。把时间混在正文里做 diff 会让「什么都没发生」也像新进展。
    generated_at = time.strftime('%Y-%m-%d %H:%M:%S')
    L = ['# 产品主线（自动综合，勿手改）', '',
         '@GEN_TIME@',
         '> 产品方向以 `ops/truth/mainline.json` 为准（人拍板）；这一页是 Agent 从决策/进展/问题/验收里综合出来的现状。', '']
    if ml:
        L += ['## %s' % ml.get('product', '产品'), '',
              ml.get('one_liner', ''), '',
              '- 阶段：%s' % ml.get('stage', ''),
              '- 北极星：%s' % ml.get('north_star', ''),
              '- 路径：%s' % ' → '.join(ml.get('path', [])),
              '- 当前焦点：%s' % '；'.join(ml.get('current_focus', []))]
        if ml.get('not_doing'):
            L.append('- 本阶段不做：%s' % '；'.join(ml.get('not_doing', [])))
        L.append('')
    else:
        L += ['## 产品主线', '', '（mainline.json 还没填，先由人拍板产品名/阶段/北极星）', '']

    road = _roadmap_rows(ml)
    if road:
        L += ['## 产品路线（每一步到哪了）', '']
        for r in road:
            st = r['status']
            line = '- %s %s' % (ROADMAP_MARK.get(st, '○'), r['step'])
            if r.get('expected'):
                line += '：%s' % r['expected']
            line += '　（%s）' % ROADMAP_LABEL.get(st, st)
            L.append(line)
        L.append('')

    L += ['## 现在是什么样', '']
    if modules:
        for m in modules:
            L.append('- **%s**：%s' % (m.get('area', ''), m.get('behavior', '')))
    else:
        L.append('- （还没有产品状态记录；改产品行为的会话应写 product_state）')
    L.append('')

    L += ['## 决策脉络', '']
    if dec:
        for r in dec[-12:]:
            why = ' —— %s' % r.get('why') if r.get('why') else ''
            L.append('- %s%s' % (r.get('title', ''), why))
    else:
        L.append('- （还没有决策记录）')
    L.append('')

    L += ['## 最近在推进', '']
    if prg[-10:]:
        for r in prg[-10:]:
            mark = '已落盘' if r.get('landed') else '在途'
            c = ' @ %s' % r['commit'][:10] if r.get('commit') else ''
            L.append('- [%s]%s %s' % (mark, c, r.get('summary') or r.get('title', '')))
    else:
        L.append('- （还没有进展记录）')
    L.append('')

    # 验收证据索引：验收项的 evidence 里带着真实门禁结果（命令 + 退出码 + 样本），
    # 但在别处只出现 id。缺了这一段，宏观看板会与验收台账形成「矛盾印象」：
    # 例如 mainline.json 的 evidence_boundary 写「网页 live 未跑」，
    # 而同一天的 A-0085 已记 exit 0 —— 委派方因此向司令台发过重复工单。
    # 这里只做索引（id + 结论首句），不替人改方向文件。
    if verified:
        L += ['## 关键验收证据（核对用，不替代 mainline.json）', '']
        for r in sorted(verified, key=lambda x: x.get('ts', ''))[-12:]:
            ev = re.split(r'[\n]', (r.get('evidence') or '').strip())[0]
            if len(ev) > 190:
                ev = ev[:190] + '…'
            L.append('- `%s` [%s] %s' % (r['id'], r.get('level', ''), r.get('title', '')))
            if ev:
                L.append('  - 证据：%s' % ev)
        L.append('')

    L += ['## 还差什么', '']
    by_kind = {}
    for r in open_led:
        by_kind.setdefault(r.get('kind', 'other'), []).append(r)
    if by_kind:
        for kind in ('bug', 'gate', 'visual', 'ux', 'arch', 'other'):
            rows = by_kind.get(kind)
            if rows:
                L.append('**%s（%d 待处理）**' % (kind, len(rows)))
                for r in rows:
                    L.append('- `%s` %s' % (r['id'], r.get('title', '')))
                L.append('')
    else:
        L += ['- 台账里没有未决问题', '']
    blocked = [r for r in acc if r.get('status') == 'blocked']
    if blocked:
        L.append('**被卡住的验收项**')
        for r in blocked:
            L.append('- `%s` %s' % (r['id'], r.get('title', '')))
        L.append('')

    human_pending = [r for r in acc
                    if r.get('needs_human') and r.get('status') in ('todo', 'wip', 'blocked')]
    machine_verified = [r for r in verified if not r.get('needs_human')]
    human_verified = [r for r in verified if r.get('needs_human')]

    L += ['## 需要你验收', '']
    if human_pending:
        for r in human_pending:
            L.append('- `%s` [%s] %s' % (r['id'], r.get('level', ''), r.get('title', '')))
            for step in re.split(r'[\n;\uff1b]+', (r.get('steps') or '').strip()):
                if step.strip():
                    L.append('  - %s' % step.strip())
    else:
        L.append('- （当前没有需要你人工拍板的验收项）')
    L.append('')

    L += ['## 已机器验收', '']
    if machine_verified:
        for r in machine_verified:
            L.append('- `%s` [%s] %s' % (r['id'], r.get('level', ''), r.get('title', '')))
    else:
        L.append('- （还没有机器自证的验收项）')
    L.append('')
    if human_verified:
        L += ['## 你已确认', '']
        for r in human_verified:
            L.append('- `%s` [%s] %s' % (r['id'], r.get('level', ''), r.get('title', '')))
        L.append('')

    L += ['## 进度概览', '',
          '- 累计关闭问题：%d 条' % len(done_led),
          '- 累计落盘：%d 条' % len(landed),
          '- 未决问题：%d 条' % len(open_led),
          '- 已验收：%d 条' % len(verified), '']

    L[2] = '> 生成时间 %s　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合' % generated_at
    text = '\n'.join(L) + '\n'
    PRODUCT_MAINLINE.parent.mkdir(parents=True, exist_ok=True)
    wrote = True
    if if_changed and PRODUCT_MAINLINE.exists():
        old = PRODUCT_MAINLINE.read_text(encoding='utf-8')
        marker = '> 生成时间 '
        def without_stamp(value):
            return '\n'.join(
                line for line in value.splitlines()
                if not line.startswith(marker)
            )
        if without_stamp(old) == without_stamp(text):
            wrote = False
    if wrote:
        PRODUCT_MAINLINE.write_text(text, encoding='utf-8')
    if wrote:
        print('\n'.join(L))
        print('\n已写出 %s' % PRODUCT_MAINLINE.relative_to(REPO), file=sys.stderr)
    else:
        print('产品主线内容未变化，保留原生成时间：%s'
              % PRODUCT_MAINLINE.relative_to(REPO))


def cmd_snapshot(args):
    ml = json.loads((TRUTH / 'mainline.json').read_text()) if (TRUTH / 'mainline.json').exists() else {}
    dec = list(_latest_by_id(_read(TRUTH / 'decisions.jsonl')).values())
    led = list(_latest_ledger(_read(TRUTH / 'ledger.jsonl')).values())
    prg = sorted([r for r in _latest_by_id(_read(TRUTH / 'progress.jsonl')).values()
                  if (r.get('status') or 'active') != 'retired'],
                 key=lambda r: r.get('ts', ''))
    acc = list(_latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values())

    open_led = [r for r in led if r.get('status') in ('open', 'doing')]
    waiting_human = [r for r in led if r.get('status') == AWAITING_HUMAN]
    blocked_acc = [r for r in acc if r.get('status') == 'blocked']
    p0_open = [r for r in led if r.get('status') in ('open', 'doing') and r.get('kind') in ('bug', 'arch', 'gate')]

    L = ['# 项目状态（自动生成，勿手改）', '',
         f"> 生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}　·　由 `python3 ops/truthctl.py` 从 `ops/truth/` 生成　·　改状态请改台账，不要改本文件。", '']

    if ml:
        L += ['## 一、产品主线', '',
              f"**{ml.get('product','')}** — {ml.get('one_liner','')}", '',
              f"- 阶段：{ml.get('stage','')}",
              f"- 北极星：{ml.get('north_star','')}",
              f"- 路径：{' → '.join(ml.get('path', []))}",
              f"- 当前焦点：{'；'.join(ml.get('current_focus', []))}",
              f"- 明确不做：{'；'.join(ml.get('not_doing', []))}",
              f"- 最后更新：{ml.get('updated','')}", '']

    road = _roadmap_rows(ml)
    if road:
        L.append('**产品路线（每一步到哪了）**')
        for r in road:
            st = r['status']
            line = f"- {ROADMAP_MARK.get(st, '○')} {r['step']}"
            if r.get('expected'):
                line += f"：{r['expected']}"
            line += f"　（{ROADMAP_LABEL.get(st, st)}）"
            L.append(line)
        L.append('')

    L += ['## 二、需要你注意', '']
    if p0_open:
        L.append('**待修的问题（bug / 架构）**')
        for r in p0_open:
            L.append(f"- `{r['id']}` {r.get('title','')}")
        L.append('')
    if blocked_acc:
        L.append('**被卡住的验收项**')
        for r in blocked_acc:
            L.append(f"- `{r['id']}` {r.get('title','')}")
        L.append('')
    if waiting_human:
        L.append('**等你确认（机器已做完，好不好只有你能判）**')
        for r in waiting_human:
            L.append(f"- `{r['id']}` {r.get('title','')}")
        L.append('')
    if not p0_open and not blocked_acc and not waiting_human:
        L += ['（当前没有需要你拍板的事项）', '']

    L += ['## 三、问题台账', '']
    for kind in ('bug', 'gate', 'visual', 'ux', 'arch'):
        rows = [r for r in led if r.get('kind') == kind and r.get('status') in ('open', 'doing')]
        if rows:
            L.append(f'**{kind}（{len(rows)} 待处理）**')
            for r in rows:
                lane = f" → `{r['lane']}`" if r.get('lane') else ''
                L.append(f"- `{r['id']}` {r.get('title','')}{lane}")
            L.append('')
    done = [r for r in led if r.get('status') in ('done', 'wontfix', 'duplicate')]
    L.append(f"（历史已关闭 {len(done)} 条）")
    L.append('')

    L += ['## 四、进展', '']
    for r in prg[-8:]:
        mark = '已落盘' if r.get('landed') else '在途'
        c = f" @ `{r['commit'][:10]}`" if r.get('commit') else ''
        L.append(f"- `{r['id']}` [{mark}]{c} {r.get('summary') or r.get('title','')}")
    L.append('')

    L += ['## 五、验收进度', '']
    human_pending = [r for r in acc
                     if r.get('needs_human') and r.get('status') in ('todo', 'wip', 'blocked')]
    machine_done = [r for r in acc if r.get('status') == 'verified' and not r.get('needs_human')]
    human_done = [r for r in acc if r.get('status') == 'verified' and r.get('needs_human')]
    L.append(f'**需要你验收（{len(human_pending)}）**')
    if human_pending:
        for r in human_pending:
            L.append(f"- `{r['id']}` [{r.get('level','')}] {r.get('title','')}")
            steps = (r.get('steps') or '').strip()
            if steps:
                for step in re.split(r'[\n;\uff1b]+', steps):
                    if step.strip():
                        L.append(f"  - {step.strip()}")
    else:
        L.append('- （当前没有需要你人工拍板的验收项）')
    L.append('')
    L.append(f'**已机器验收（{len(machine_done)}）**')
    if machine_done:
        for r in machine_done:
            L.append(f"- `{r['id']}` [{r.get('level','')}] {r.get('title','')}")
    else:
        L.append('- （还没有机器自证的验收项）')
    L.append('')
    if human_done:
        L.append(f'**你已确认（{len(human_done)}）**')
        for r in human_done:
            L.append(f"- `{r['id']}` [{r.get('level','')}] {r.get('title','')}")
        L.append('')

    L += ['## 六、决策记录', '']
    for r in dec:
        if r.get('status') == 'active':
            L.append(f"- `{r['id']}` {r.get('title','')}")
    L.append('')

    ps = _read_json(PRODUCT_STATE)
    modules = ps.get('modules') if isinstance(ps.get('modules'), list) else []
    L += ['## 七、产品状态（产品今天是怎么工作的）', '']
    if modules:
        for m in modules:
            L.append(f"- **{m.get('area','')}**：{m.get('behavior','')}")
            if m.get('why'):
                L.append(f"  - 为什么：{m.get('why')}")
    else:
        L.append('（还没有记录；改动的会话应在同一轮写进来）')
    L.append('')

    jd = [r for r in _read(JUDGMENTS) if r.get('status') == 'active']
    L += ['## 八、判断逻辑（同一个判断，人只做一次）', '']
    if jd:
        for r in jd[-12:]:
            L.append(f"- `{r['id']}` [{r.get('scope','')}] "
                     f"{r.get('statement') or r.get('title','')}")
    else:
        L.append('（还没有蒸馏；每轮把「为什么这么判断」写回，不记一次性细节）')
    L.append('')

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text('\n'.join(L) + '\n')
    if not args or '--print' in args:
        print('\n'.join(L))
    print(f'\n已写出 {STATUS.relative_to(REPO)}', file=sys.stderr)


def cmd_goal(args):
    """关系层显式入口：跨项目长期目标不混进单个项目真相。"""
    if not args:
        print(__doc__)
        print('用法: truthctl goal <summary|list|create|update|link|close> ...')
        return 1
    sub, rest = args[0], args[1:]
    if sub == 'summary':
        result = relationship_ledger.summary(*rest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if sub == 'list':
        path = rest[0] if rest else None
        state = relationship_ledger.summary(path)
        if state.get('status') == 'unknown':
            print('关系目标取不到：%s' % state.get('headline'), file=sys.stderr)
            return 1
        try:
            rows = relationship_ledger.list_goals(path)
        except relationship_ledger.RelationshipError as exc:
            print('关系目标取不到：%s' % exc, file=sys.stderr)
            return 1
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0

    if sub not in ('create', 'update', 'link', 'close'):
        raise SystemExit('用法: truthctl goal <summary|list|create|update|link|close> ...')

    source = os.environ.get('KG_GOAL_SOURCE', 'user').strip()
    if source not in ('user', 'operator'):
        raise SystemExit('KG_GOAL_SOURCE 只能是 user 或 operator')
    path = os.environ.get('KG_RELATIONSHIP_GOALS_PATH') or None

    if sub in ('link', 'close'):
        if not rest:
            raise SystemExit('用法: truthctl goal link <G-id> [--ledger L-id,...] '
                             '[--acceptance A-id,...] [--thread id] | '
                             'truthctl goal close <G-id> "<结论>"')
        gid = rest[0]
        try:
            if sub == 'link':
                def values(flag):
                    if flag not in rest:
                        return []
                    idx = rest.index(flag) + 1
                    if idx >= len(rest):
                        raise SystemExit('%s 后面缺少值' % flag)
                    return [x.strip() for x in rest[idx].split(',') if x.strip()]
                def value(flag):
                    if flag not in rest:
                        return ''
                    idx = rest.index(flag) + 1
                    if idx >= len(rest):
                        raise SystemExit('%s 后面缺少值' % flag)
                    return rest[idx]
                row = relationship_ledger.link(
                    gid, ledger=values('--ledger'),
                    acceptance=values('--acceptance'),
                    thread=value('--thread'),
                    source=source, path=path)
            else:
                if len(rest) < 2 or not rest[1].strip():
                    raise SystemExit('close 必须写结论（证据或人确认的理由）')
                conclusion = rest[1]
                if conclusion.startswith('--'):
                    raise SystemExit('close 的第二个参数必须是结论')
                evidence = conclusion
                if '--evidence' in rest:
                    idx = rest.index('--evidence') + 1
                    if idx >= len(rest):
                        raise SystemExit('--evidence 后面缺少值')
                    evidence = rest[idx]
                row = relationship_ledger.close(
                    gid, conclusion, source=source, evidence=evidence, path=path)
        except relationship_ledger.RelationshipError as exc:
            print('关系目标写入被拒绝：%s' % exc, file=sys.stderr)
            return 2
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0

    def has(flag):
        return flag in rest

    def take(flag, default=''):
        if flag not in rest:
            return default
        index = rest.index(flag) + 1
        if index >= len(rest):
            raise SystemExit('%s 后面缺少值' % flag)
        return rest[index]

    if sub == 'update':
        if not rest or not rest[0].startswith('G-'):
            raise SystemExit('用法: truthctl goal update <G-id> [--title "..." ...]')
        gid = rest[0]
        try:
            fields = relationship_ledger.fields_for(gid, path, source)
        except relationship_ledger.RelationshipError as exc:
            print('关系目标写入被拒绝：%s' % exc, file=sys.stderr)
            return 2
        if has('--title'):
            fields['title'] = take('--title')
        if has('--priority'):
            fields['priority'] = take('--priority')
        if has('--projects'):
            fields['projects'] = [x.strip() for x in take('--projects').split(',')
                                  if x.strip()]
        if has('--status'):
            fields['status'] = take('--status')
        if has('--reason'):
            fields['reason'] = take('--reason')
        if has('--north-star'):
            fields['north_star'] = take('--north-star')
        if has('--thread'):
            fields['source_thread'] = take('--thread')
        if has('--ledger'):
            fields['linked_ledger'] = [x.strip() for x in take('--ledger').split(',')
                                       if x.strip()]
        if has('--acceptance'):
            fields['linked_acceptance'] = [
                x.strip() for x in take('--acceptance').split(',') if x.strip()]
        if has('--evidence'):
            fields['evidence'] = take('--evidence')
        if has('--needs-human'):
            fields['needs_human'] = True
        fields['id'] = gid
        fields['last_touched'] = take('--touched') or relationship_ledger.now()
    else:
        fields = {
            'title': take('--title'),
            'priority': take('--priority', 'normal'),
            'projects': [x.strip() for x in take('--projects').split(',') if x.strip()],
            'status': take('--status', 'proposed'),
            'reason': take('--reason'),
            'north_star': take('--north-star'),
            'source_thread': take('--thread'),
            'linked_ledger': [x.strip() for x in take('--ledger').split(',') if x.strip()],
            'linked_acceptance': [x.strip() for x in take('--acceptance').split(',')
                                  if x.strip()],
            'last_touched': take('--touched') or relationship_ledger.now(),
            'needs_human': has('--needs-human'),
            'source': source,
            'evidence': take('--evidence'),
        }
    try:
        row = (relationship_ledger.update(fields['id'], fields, path)
               if sub == 'update' else relationship_ledger.create(fields, path))
    except relationship_ledger.RelationshipError as exc:
        print('关系目标写入被拒绝：%s' % exc, file=sys.stderr)
        return 2
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


CMDS = {'add': cmd_add, 'ls': cmd_ls, 'snapshot': cmd_snapshot, 'promote': cmd_promote,
        'route': cmd_route, 'lanes': cmd_lanes, 'next': cmd_next,
        'product': cmd_product, 'distill': cmd_distill,
        'constraints': cmd_constraints, 'retire': cmd_retire,
        'close': cmd_close, 'open': cmd_open, 'synthesize': cmd_synthesize,
        'accept': cmd_accept, 'goal': cmd_goal,
        'closure-package': cmd_closure_package}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        cmd_snapshot([])
    elif sys.argv[1] in CMDS:
        CMDS[sys.argv[1]](sys.argv[2:])
    else:
        print(__doc__)
        print('命令：' + ' | '.join(['snapshot', 'ls', 'add', 'promote', 'route', 'lanes',
                                     'next', 'product', 'distill', 'constraints', 'retire',
                                     'close', 'open', 'synthesize', 'accept', 'goal',
                                     'closure-package']))
        sys.exit(1)
