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
  truthctl close <L-id> "<结论>"  把问题台账里的一条标成 done，并追加进展
  truthctl reopen <L-id> "<原因>"  把已关闭的问题重新打开
  truthctl accept "<标题>" [--human --steps "..."]  登记验收项，分清机器自证/人工验收
  truthctl synthesize          把决策/进展/问题/验收综合成 docs/PRODUCT_MAINLINE.md

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
from state_root import main_worktree, resolve_state_root  # noqa: E402
import central_config  # noqa: E402

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
          'judgment': 'J'}
LEDGER_KINDS = {'visual', 'ux', 'bug', 'arch', 'gate'}
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
         'judgment': 'judgments.jsonl'}


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
    remote = subprocess.run(
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
    result = subprocess.run(
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
                      evidence=os.environ.get('KG_TRUTH_EVIDENCE', ''))
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
                                [--level P0] [--lane <分支>] [--status todo]
    """
    if not args:
        sys.exit('用法: truthctl accept "<标题>" [--human --steps "..."] [--level P1]')
    human = '--human' in args
    title_parts, steps, level, lane, status = [], '', None, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--human':
            i += 1
        elif a == '--steps' and i + 1 < len(args):
            steps = args[i + 1]
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
    title = ' '.join(title_parts)
    if not title:
        sys.exit('accept 需要一句标题：truthctl accept "<标题>"')
    rec = _append_new('acceptance', title=title,
                      level=level or os.environ.get('KG_TRUTH_LEVEL', 'P1'),
                      status=status or os.environ.get('KG_TRUTH_STATUS', 'todo'),
                      needs_human=human or os.environ.get('KG_TRUTH_NEEDS_HUMAN', '') == '1',
                      steps=steps or os.environ.get('KG_TRUTH_STEPS', ''),
                      evidence=os.environ.get('KG_TRUTH_EVIDENCE', ''),
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
    rec = _append_new('ledger', kind=kind, title=it.get('text', '')[:80],
                      detail=it.get('text', ''), status='open',
                      lane=it.get('lane'), files=it.get('files') or [],
                      source=f"inbox#{n}")
    # 回写 inbox 的 routed 标记
    items[n - 1]['routed'] = True
    items[n - 1]['lane'] = rec['id']
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


def _find_ledger(rid: str) -> dict:
    return _latest_by_id(_read(TRUTH / FILES['ledger'])).get(rid)


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


def _append_ledger(kind, title, detail, files, source, lane) -> dict:
    return _append_new('ledger', kind=kind, title=title, detail=detail,
                       status='open', lane=lane, files=files or [], source=source)


def _candidates(kind, text, files):
    """在既有泳道里找这条 intake 该去哪：文件路径优先，代码标识匹配兜底。"""
    files = list(files or [])
    query_tokens = _tokens(text or '')
    out = []
    for line in _read(TRUTH / FILES['ledger']):
        if line.get('status') not in ('open', 'doing', 'todo'):
            continue
        own = list(line.get('files') or [])
        exact = len(set(files) & set(own))
        broad = 0
        for f in files:
            for o in own:
                if f != o and (f.startswith(o + '/') or o.startswith(f + '/')):
                    broad += 1
        own_text = ' '.join([line.get('title', ''), line.get('detail', ''), *own])
        token_hits = len(query_tokens & _tokens(own_text)) if query_tokens else 0
        if not exact and not broad and not token_hits:
            continue
        out.append((1 if (exact or token_hits) else 0, exact, broad, line['id'],
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
    lanes = []
    if LANES.exists():
        try:
            lanes = json.loads(LANES.read_text())
        except Exception:
            lanes = []
    names = {r.get('name') for r in lanes} or {r.get('name') for r in CFG.get('lanes') or []}
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
                     'files': it.get('files') or [], 'cand': cand[:2]})
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
            it['lane'], it['routed'] = ref, True
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
                    path = TRUTH / FILES['ledger']
                    rows = _read(path)
                    for i, rec in enumerate(rows):
                        if rec.get('id') == ref:
                            rows[i] = target
                    with path.open('w') as fh:
                        for rec in rows:
                            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        else:
            rec = _append_ledger(p['kind'], p['text'][:80].replace('\n', ' '),
                                 p['text'], p['files'], f"inbox#{p['n']}", p['lane'])
            it['lane'], it['routed'] = rec['lane'], True
            made.append(rec['id'])
    merged = 0
    for p in dups:
        it = items[p['n'] - 1]
        ref = seen[p['text'][:120]]
        it['lane'], it['routed'] = ref, True
        note = f"[inbox#{p['n']}] {p['text']}"
        path = TRUTH / FILES['ledger']
        rows = _read(path)
        for j, rec in enumerate(rows):
            if rec.get('id') == ref:
                ev = list(rec.get('evidence') or [])
                if note not in ev:
                    ev.append(note)
                    rec['evidence'] = ev
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
    led = [r for r in _latest_by_id(_read(TRUTH / 'ledger.jsonl')).values()
           if r.get('status') in ('open', 'doing')]
    led.sort(key=lambda r: (PRIORITY.get(r.get('kind'), 9), r.get('ts', '')))
    acc = [r for r in _latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values()
           if r.get('status') == 'blocked']
    print('=== 自动挡 · 现在做什么 ===')
    if acc:
        print('被卡住的验收项（需要用户拍板或外部条件）：')
        for r in acc:
            print(f"  {r['id']} [{r.get('level','')}] {r.get('title','')}")
    if not led and not acc:
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
    p = subprocess.run(['git', '-C', str(REPO), 'diff', '--unified=1', '--no-color',
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


def _set_ledger_status(rid, status, note, evidence=''):
    # JSONL 只追加：同 id 的新记录覆盖旧记录，得到当前状态
    with _truth_lock():
        rec = _latest_by_id(_read(TRUTH / FILES['ledger'])).get(rid)
        if rec is None:
            sys.exit('问题台账里没有 %s（先 truthctl ls ledger 看现有条目）' % rid)
        merged = dict(rec)
        merged['status'] = status
        merged['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if note:
            merged['detail'] = (merged.get('detail', '') + '\n' if merged.get('detail') else '') + note
        if evidence:
            merged['evidence'] = evidence
        if status == 'done':
            merged['closed'] = merged['ts']
        _append(merged)
        return merged


def cmd_close(args):
    """把一条问题台账标成 done，并追加一条进展：从「收到」走到「已关闭」。"""
    if not args:
        sys.exit('用法: truthctl close <L-id> "<结论/证据>"（KG_TRUTH_COMMIT 可选）')
    rid = args[0]
    note = ' '.join(args[1:]) if len(args) > 1 else ''
    rec = _set_ledger_status(rid, 'done', note, os.environ.get('KG_TRUTH_EVIDENCE', ''))
    commit = os.environ.get('KG_TRUTH_COMMIT') or None
    prog = _append_new('progress', title='关闭 %s：%s' % (rid, rec.get('title', '')),
                       summary=note, commit=commit, landed=bool(commit),
                       lane=rec.get('lane'), files=rec.get('files') or [],
                       source=os.environ.get('KG_TRUTH_SOURCE', 'agent'),
                       status='done')
    print('已关闭 [%s] %s（同时记入进展 %s）' % (rid, rec.get('title', ''), prog['id']))


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
    """
    ml = _read_json(TRUTH / 'mainline.json')
    dec = [r for r in _latest_by_id(_read(TRUTH / 'decisions.jsonl')).values()
           if r.get('status') == 'active']
    led = list(_latest_by_id(_read(TRUTH / 'ledger.jsonl')).values())
    prg = sorted(_latest_by_id(_read(TRUTH / 'progress.jsonl')).values(),
                 key=lambda r: r.get('ts', ''))
    acc = list(_latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values())
    ps = _read_json(PRODUCT_STATE)
    modules = ps.get('modules') if isinstance(ps.get('modules'), list) else []
    open_led = [r for r in led if r.get('status') in ('open', 'doing')]
    done_led = [r for r in led if r.get('status') == 'done']
    landed = [r for r in prg if r.get('landed')]
    verified = [r for r in acc if r.get('status') == 'verified']

    L = ['# 产品主线（自动综合，勿手改）', '',
         '> 生成时间 %s　·　由 `python3 ops/truthctl.py synthesize` 从真相层综合' % time.strftime('%Y-%m-%d %H:%M:%S'),
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

    PRODUCT_MAINLINE.parent.mkdir(parents=True, exist_ok=True)
    PRODUCT_MAINLINE.write_text('\n'.join(L) + '\n')
    print('\n'.join(L))
    print('\n已写出 %s' % PRODUCT_MAINLINE.relative_to(REPO), file=sys.stderr)


def cmd_snapshot(args):
    ml = json.loads((TRUTH / 'mainline.json').read_text()) if (TRUTH / 'mainline.json').exists() else {}
    dec = list(_latest_by_id(_read(TRUTH / 'decisions.jsonl')).values())
    led = list(_latest_by_id(_read(TRUTH / 'ledger.jsonl')).values())
    prg = sorted(_latest_by_id(_read(TRUTH / 'progress.jsonl')).values(),
                 key=lambda r: r.get('ts', ''))
    acc = list(_latest_by_id(_read(TRUTH / 'acceptance.jsonl')).values())

    open_led = [r for r in led if r.get('status') in ('open', 'doing')]
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
    if not p0_open and not blocked_acc:
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


CMDS = {'add': cmd_add, 'ls': cmd_ls, 'snapshot': cmd_snapshot, 'promote': cmd_promote,
        'route': cmd_route, 'lanes': cmd_lanes, 'next': cmd_next,
        'product': cmd_product, 'distill': cmd_distill,
        'close': cmd_close, 'open': cmd_open, 'synthesize': cmd_synthesize,
        'accept': cmd_accept}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        cmd_snapshot([])
    elif sys.argv[1] in CMDS:
        CMDS[sys.argv[1]](sys.argv[2:])
    else:
        print(__doc__)
        print('命令：' + ' | '.join(['snapshot', 'ls', 'add', 'promote', 'route', 'lanes',
                                     'next', 'product', 'distill', 'close', 'open',
                                     'synthesize']))
        sys.exit(1)
