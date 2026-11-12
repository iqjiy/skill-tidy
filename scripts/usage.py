"""skill-tidy 第一阶段：读两边的会话日志，输出 usage.json。只读。

只用标准库，兼容 macOS 自带的 python3 3.9。只取 skill 的名字、路径和时间，不复制对话内容。

用法：
    python3 usage.py --current claude|codex --current-listing 清单.txt [--scope current|all]
                     [--home 主目录] [--out usage.json]

--current-listing 是调用方 agent 从自己上下文里复制出来的加载清单，一行一个 "- 名字: 描述"，
Codex 还要带上 Skill roots 和每行末尾的 (file: …)。另一边的清单取自它最近一次留下清单的会话日志。

统计口径见 DESIGN.md 第 8 节和 references/agents.md 第 5 节：
- Claude Code：调用一次 Skill 工具，或输入一次 /名字，算一次。按名字记，名字对应哪份由 plan.py 判断。
- Codex：一次会话里，SKILL.md 的路径出现在 custom_tool_call / function_call 的参数里，算读过一次。
  工具的输出、item_completed、开场注入的清单都不算。按真实路径合并，插件旧版本并到现存版本，
  对不上现存 skill 的丢弃并记数。
"""
import argparse
import calendar
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

AGENTS = ('claude', 'codex')


# ---------------------------------------------------------------- 读文件

def zstd_reader():
    """返回逐行读 .zst 的函数。先用 zstd 命令，没有就用 Python 3.14 自带的 compression.zstd，都没有返回 None。"""
    if shutil.which('zstd'):
        def read(path):
            proc = subprocess.Popen(['zstd', '-dcq', path], stdout=subprocess.PIPE)
            try:
                for line in proc.stdout:
                    yield line
            finally:
                proc.stdout.close()
                proc.wait()
        return read
    try:
        from compression import zstd
    except ImportError:
        return None

    def read(path):
        with zstd.open(path, 'rb') as fh:
            for line in fh:
                yield line
    return read


def iter_lines(path, zread):
    """逐行读 .jsonl 或 .jsonl.zst，不整个读进内存。"""
    if path.endswith('.zst'):
        for line in zread(path):
            yield line
        return
    with open(path, 'rb') as fh:
        for line in fh:
            yield line


def _loads(raw):
    try:
        o = json.loads(raw)
    except ValueError:
        return {}
    return o if isinstance(o, dict) else {}


def _epoch(ts):
    """'2026-11-07T03:36:03.527Z' -> 秒。日志里的时间都是 UTC。"""
    try:
        return calendar.timegm(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return None


def _iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def _period(starts, ends):
    times = [t for t in starts + ends if t is not None]
    if not times:
        return None
    return {'from': _iso(min(times)), 'to': _iso(max(times))}


# ---------------------------------------------------------------- 加载清单

ROOT_LINE = re.compile(r'^- `(r\d+)` = `([^`]+)`\s*$')


def parse_listing_text(text, agent='codex'):
    """解析加载清单原文，两边通用。返回 {'roots': {rN: 路径}, 'entries': [{'name', 'path'}]}。"""
    lines = text.splitlines()
    roots = {}
    heading = next((i for i, line in enumerate(lines) if line.strip() == '### Available skills'), None)
    if agent == 'codex':
        roots = dict(m.groups() for m in (ROOT_LINE.match(line) for line in lines[:heading]) if m)
    if agent == 'codex' and heading is not None:
        block = []
        for l in lines[heading + 1:]:
            if not l.startswith('- '):
                break
            block.append(l)
    else:
        block = [l for l in lines if l.startswith('- ')]
    entries = []
    for line in block:
        body, path = line[2:].rstrip(), None
        if agent == 'codex' and body.endswith(')') and '(file: ' in body:
            body, _, ref = body[:-1].rpartition('(file: ')
            ref = ref.strip()
            alias = re.match(r'^(r\d+)/(.+)$', ref)
            if alias:
                path = os.path.join(roots[alias.group(1)], alias.group(2)) if alias.group(1) in roots else None
            else:
                path = ref
        name = body.split(': ', 1)[0].strip().rstrip(':')
        if name and not name.startswith('`'):  # 名字可以带空格和括号，例如 "SEO (Site Audit + …)"
            entries.append({'name': name, 'path': path})
    return {'roots': roots, 'entries': entries}


def _claude_listing(path):
    """一次 Claude Code 会话开场的加载清单。没有时返回 None。"""
    with open(path, 'rb') as fh:
        for raw in fh:
            if b'"skill_listing"' not in raw:
                continue
            o = _loads(raw)
            a = o.get('attachment') or {}
            if a.get('type') == 'skill_listing' and a.get('isInitial'):
                session = dict((k, o.get(k)) for k in ('timestamp', 'cwd', 'version'))
                session['file'] = path
                return {'from': 'log', 'session': session, 'roots': {},
                        'entries': [{'name': n, 'path': None} for n in a.get('names') or []]}
    return None


def _codex_listing(path, zread):
    """一次 Codex 会话开场的加载清单。没有时返回 None。"""
    session = None
    for raw in iter_lines(path, zread):
        if session is None and b'"session_meta"' in raw:
            p = _loads(raw).get('payload') or {}
            session = dict((k, p.get(k)) for k in ('timestamp', 'cwd', 'originator', 'cli_version'))
            session['file'] = path
        if b'### Skill roots' not in raw:
            continue
        event = _loads(raw)
        if event.get('type') != 'response_item':
            continue
        p = event.get('payload') or {}
        if p.get('type') != 'message' or p.get('role') != 'developer':
            continue  # 只认 Codex 自己注入的清单，用户或工具输出里同样的文字不算
        for part in p.get('content') or []:
            if not isinstance(part, dict) or part.get('type') != 'input_text':
                continue
            text = part.get('text')
            if isinstance(text, str) and '### Skill roots' in text:
                parsed = parse_listing_text(text, 'codex')
                if parsed['entries']:
                    return dict(parsed, **{'from': 'log', 'session': session})
        return None
    return None


def _latest_listing(agent, home, zread):
    """按会话开始时间倒序，取第一个有清单的会话。"""
    if agent == 'claude':
        for p in sorted(_claude_logs(home), key=os.path.getmtime, reverse=True):
            listing = _claude_listing(p)
            if listing:
                return listing
        return None
    for p in _codex_logs(home):
        if p.endswith('.zst') and zread is None:
            continue
        listing = _codex_listing(p, zread)
        if listing:
            return listing
    return None


# ---------------------------------------------------------------- Claude Code 的使用次数

CMD_RE = re.compile(r'<command-name>/([^<\s]+)</command-name>')
TS_RE = re.compile(rb'"timestamp":"([^"]+)"')


def _claude_logs(home):
    return glob.glob(os.path.join(home, '.claude', 'projects', '**', '*.jsonl'), recursive=True)


def _claude_calls(path):
    """一个日志里的 Skill 调用和斜杠命令（按名字），以及第一条记录的时间。"""
    names, first = [], None
    with open(path, 'rb') as fh:
        for raw in fh:
            if first is None:
                m = TS_RE.search(raw)
                first = m.group(1).decode('utf-8', 'replace') if m else None
            if b'"name":"Skill"' in raw:
                for item in ((_loads(raw).get('message') or {}).get('content') or []):
                    if isinstance(item, dict) and item.get('type') == 'tool_use' and item.get('name') == 'Skill':
                        skill = (item.get('input') or {}).get('skill')
                        if isinstance(skill, str):
                            names.append(skill)
            if b'<command-name>/' in raw:
                o = _loads(raw)
                if o.get('type') != 'user':
                    continue
                content = (o.get('message') or {}).get('content')
                if isinstance(content, list):  # 只看用户自己输入的文字，不看 tool_result
                    content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
                if isinstance(content, str):
                    names += CMD_RE.findall(content)
    return names, first


def _claude_usage(home):
    files = _claude_logs(home)
    if not files:
        return {'logs': 0, 'period': None, 'by_name': {}, 'gaps': [{'kind': 'no_logs'}]}
    counts, starts, ends = collections.Counter(), [], []
    for p in files:
        names, first = _claude_calls(p)
        counts.update(names)
        starts.append(_epoch(first))
        ends.append(os.path.getmtime(p))
    return {'logs': len(files), 'period': _period(starts, ends), 'by_name': dict(sorted(counts.items())), 'gaps': []}


# ---------------------------------------------------------------- Codex 的使用次数

START_RE = re.compile(r'rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)')
PLUGIN_VERSION_RE = re.compile(r'^(.*/plugins/cache/[^/]+/[^/]+/)[^/]+(/.+)$')


def _codex_logs(home):
    """会话日志，按文件名里的开始时间倒序。"""
    base = os.path.join(home, '.codex', 'sessions')
    files = (glob.glob(os.path.join(base, '**', 'rollout-*.jsonl'), recursive=True) +
             glob.glob(os.path.join(base, '**', 'rollout-*.jsonl.zst'), recursive=True))
    return sorted(files, key=_codex_start, reverse=True)


def _codex_start(path):
    m = START_RE.search(os.path.basename(path))
    return m.group(1) if m else ''


def _codex_session(path, zread, read_re, home):
    """一次会话的开始时间，和会话里读过的 SKILL.md 路径集合。"""
    started, seen = None, set()
    for raw in iter_lines(path, zread):
        if started is None and b'"session_meta"' in raw:
            started = (_loads(raw).get('payload') or {}).get('timestamp')
        if b'SKILL.md' not in raw or b'### Skill roots' in raw:
            continue
        if b'"type":"custom_tool_call"' not in raw and b'"type":"function_call"' not in raw:
            continue
        p = _loads(raw).get('payload') or {}
        if p.get('type') == 'custom_tool_call':
            text = p.get('input')
        elif p.get('type') == 'function_call':
            text = p.get('arguments')
        else:
            continue
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        for m in read_re.finditer(text):
            s = m.group(1)
            seen.add(home + s[1:] if s.startswith('~/') else s)
    return started, seen


def _resolve(path):
    """读到的 SKILL.md 路径 -> 现存 skill 的真实目录。插件旧版本并到唯一的现存版本。对不上返回 None。"""
    if os.path.isfile(path):
        return os.path.realpath(os.path.dirname(path))
    m = PLUGIN_VERSION_RE.match(path)
    if m:
        hits = [h for h in glob.glob(glob.escape(m.group(1)) + '*' + glob.escape(m.group(2))) if os.path.isfile(h)]
        if len(hits) == 1:
            return os.path.realpath(os.path.dirname(hits[0]))
    return None


def _codex_usage(home, zread):
    files = _codex_logs(home)
    archives = [p for p in files if p.endswith('.zst')]
    out = {'logs': len(files) - len(archives), 'archives': len(archives), 'archives_read': 0, 'sessions': 0,
           'period': None, 'by_entity': {}, 'dropped': {'reads': 0, 'paths': []}, 'gaps': []}
    if not files:
        out['gaps'].append({'kind': 'no_logs'})
        return out
    read_re = re.compile(r'((?:%s|~)/[^"\'\s\\]{1,200}?/SKILL\.md)' % re.escape(home))
    counts, resolved, dropped, starts, ends = collections.Counter(), {}, set(), [], []
    for p in files:
        if p.endswith('.zst'):
            if zread is None:
                continue
            out['archives_read'] += 1
        started, paths = _codex_session(p, zread, read_re, home)
        out['sessions'] += 1
        starts.append(_epoch(started))
        ends.append(os.path.getmtime(p))
        entities = set()
        for x in paths:
            if x not in resolved:
                resolved[x] = _resolve(x)
            if resolved[x]:
                entities.add(resolved[x])
            else:
                out['dropped']['reads'] += 1
                dropped.add(x)
        counts.update(entities)
    if out['archives_read'] < len(archives):
        out['gaps'].append({'kind': 'archives_skipped', 'count': len(archives) - out['archives_read']})
    out['period'] = _period(starts, ends)
    out['by_entity'] = dict(sorted(counts.items()))
    out['dropped']['paths'] = sorted(dropped)
    return out


# ---------------------------------------------------------------- 汇总

def _none_listing():
    return {'from': 'none', 'session': None, 'roots': {}, 'entries': []}


def collect(home, current, scope='current', listing_text=None):
    home = os.path.abspath(home)
    zread = zstd_reader()
    listings, usage = {}, {}
    for agent in ([current] if scope == 'current' else AGENTS):
        latest = _latest_listing(agent, home, zread)
        if agent != current:
            listings[agent] = latest or _none_listing()
        elif listing_text is None:
            listings[agent] = _none_listing()
        else:
            parsed = parse_listing_text(listing_text, agent)
            # 当前会话的开始时间取最近一次会话日志的，只是估计
            session = dict(latest['session'], guessed=True) if latest else None
            listings[agent] = dict(parsed, **{'from': 'context', 'session': session})
        usage[agent] = _claude_usage(home) if agent == 'claude' else _codex_usage(home, zread)
    return {'version': 1, 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'home': home,
            'current': current, 'scope': scope, 'listings': listings, 'usage': usage}


def main(argv=None):
    ap = argparse.ArgumentParser(description='读两边的会话日志，输出 usage.json。只读。')
    ap.add_argument('--home', default=os.path.expanduser('~'), help='主目录，测试时可以换成临时目录')
    ap.add_argument('--current', required=True, choices=AGENTS, help='当前是哪个 agent')
    ap.add_argument('--scope', default='current', choices=('current', 'all'), help='只整理当前 agent，还是两个都整理')
    ap.add_argument('--current-listing', required=True, help='当前会话的加载清单原文，从上下文复制')
    ap.add_argument('--out', help='输出文件，不写就打印到标准输出')
    a = ap.parse_args(argv)
    with open(a.current_listing, encoding='utf-8') as fh:
        listing_text = fh.read()
    text = json.dumps(collect(a.home, a.current, a.scope, listing_text), ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
    else:
        sys.stdout.write(text + '\n')


if __name__ == '__main__':
    main()
