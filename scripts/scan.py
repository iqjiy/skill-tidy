"""skill-tidy 第一阶段：扫描已知目录，输出 scan.json。只读。

只用标准库，兼容 macOS 自带的 python3 3.9。

用法：
    python3 scan.py [--home 主目录] [--project 项目目录] [--listings usage.json] [--out scan.json]

--listings 读其中的 listings 字段。Codex 插件目录不往下扫，只收清单里列出的 SKILL.md。
范围也取自这里：只报 listings 里的 agent，其他 agent 对同一实体的引用仅用于保护。
"""
import argparse
from collections import deque
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOTS = os.path.normpath(os.path.join(HERE, '..', 'references', 'roots.json'))

# ---------------------------------------------------------------- 头部检查
#
# 只用来解释"为什么被跳过"，以及取出 name、description 等顶层字段。
# 只认本机实际用到的写法：普通的 键: 值、双引号和单引号、> 或 | 开头的多行文本、
# 嵌套字段、单行的 [...] 和 {...}、列表。其他写法记进 unverified。见 DESIGN.md 第 4 节。

KEY_RE = re.compile(r'^([A-Za-z0-9_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?$')
BLOCK_HEADER_RE = re.compile(r'^([|>])([1-9]?)([+-]?)([1-9]?)[ \t]*(#.*)?$')
YAML_PREFIX = re.compile(r'[&!]\S*(?:\s+|$)')
SOURCE_FIELDS = ('source', 'repository', 'homepage')
COLLECTION = object()  # 列表、嵌套字段：只检查写法，不取值


def check_frontmatter(text):
    res = {'status': 'ok', 'line': None, 'name': None, 'description': None,
           'fields': {}, 'hints': [], 'unverified': []}
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != '---':
        res['status'] = 'no_frontmatter'
        return res
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == '---'), None)
    block = lines[1:end] if end is not None else lines[1:]

    def mark(kind):
        if kind not in res['unverified']:
            res['unverified'].append(kind)

    body_line = comment_start = None
    i = 0
    while i < len(block):
        raw, lineno = block[i], i + 2
        if not raw.strip():
            i += 1
            continue
        if raw.lstrip().startswith('#'):
            mark('comment')
            comment_start = comment_start or lineno
            i += 1
            continue
        if raw[0] in ' \t':  # 没有上级字段的缩进行
            mark('indented')
            i += 1
            continue
        m = KEY_RE.match(raw.rstrip())
        if not m:
            # 正文前面紧挨着的 "# 标题" 也算正文
            body_line = comment_start or lineno
            break
        comment_start = None
        j = i + 1
        while j < len(block) and (not block[j].strip() or block[j][0] in ' \t'):
            j += 1
        cont = block[i + 1:j]
        while cont and not cont[-1].strip():
            cont.pop()
        key = m.group(1)
        value = _value(key, (m.group(2) or '').strip(), cont, lineno, mark, res['hints'])
        if key in SOURCE_FIELDS:
            value = _source_value(key, value)
        elif key == 'disable-model-invocation' and isinstance(value, str):
            value = value.lower() if value.lower() in ('true', 'false', 'on', 'off', 'yes', 'no') else None
        elif key not in ('name', 'description'):
            value = None
        if isinstance(value, str):
            res['fields'][key] = value
        i = j

    for key in ('name', 'description'):
        v = res['fields'].get(key)
        res[key] = v if isinstance(v, str) and v.strip() else None
    res['line'] = body_line
    if end is None:
        res['status'] = 'unclosed'
    elif body_line:
        res['status'] = 'body_in_frontmatter'
    elif not res['name']:
        res['status'] = 'missing_name'
    elif not res['description']:
        res['status'] = 'missing_description'
    elif res['unverified']:
        res['status'] = 'unverified'
    return res


def _value(key, val, cont, lineno, mark, hints):
    has_cont = any(l.strip() for l in cont)
    if val.startswith(('&', '!')):
        pos = 0
        while (prefix := YAML_PREFIX.match(val, pos)) is not None:
            mark('anchor' if val[pos] == '&' else 'tag')
            pos = prefix.end()
        val = val[pos:]
        if not val:
            return None
    if not val:
        if not has_cont:
            return None
        for l in cont:
            s = l.strip()
            if s.startswith('#'):
                mark('comment')
            elif re.search(r'(^-|:)\s+&\w', s):
                mark('anchor')
            elif re.search(r'(^-|:)\s+\*\w', s):
                mark('alias')
        return COLLECTION
    c = val[0]
    if c in '|>':
        hm = BLOCK_HEADER_RE.match(val)
        if not hm:
            mark('block_header')
            return val
        if hm.group(5):
            mark('comment')
        return _block_scalar(hm.group(1), hm.group(3), cont)
    if c in '"\'':
        q = _quoted(val)
        if q is None:
            mark('multiline_quoted')
            return ' '.join([val[1:]] + [l.strip() for l in cont]).rstrip(c).strip()
        content, rest = q
        if rest:
            mark('comment' if rest.startswith('#') else 'trailing_text')
        if has_cont:
            mark('multiline_quoted')
        return content
    if c in '[{':
        if has_cont or not val.rstrip().endswith(']' if c == '[' else '}'):
            mark('multiline_flow')
        return COLLECTION
    if c == '*':
        mark('alias')
        return COLLECTION
    # 普通写法
    v = val
    ci = v.find(' #')
    if ci >= 0:
        mark('comment')
        v = v[:ci].rstrip()
    if re.search(r':(\s|$)', v):
        hints.append({'kind': 'unquoted_colon', 'key': key, 'line': lineno})
    if has_cont:
        mark('multiline_plain')
        v = ' '.join([v] + [l.strip() for l in cont if l.strip()])
    return v


def _quoted(val):
    """解析单行的引号字符串。返回 (内容, 引号后剩下的文字)；没在本行闭合时返回 None。"""
    q = val[0]
    k = 1
    while k < len(val):
        ch = val[k]
        if q == '"' and ch == '\\':
            k += 2
            continue
        if ch == q:
            if q == "'" and val[k + 1:k + 2] == "'":
                k += 2
                continue
            raw, rest = val[1:k], val[k + 1:].strip()
            if q == "'":
                return raw.replace("''", "'"), rest
            try:
                return json.loads('"' + raw + '"', strict=False), rest
            except ValueError:
                simple = {'n': '\n', 't': '\t', '"': '"', '\\': '\\', '/': '/'}
                return re.sub(r'\\(.)', lambda m: simple.get(m.group(1), m.group(1)), raw), rest
        k += 1
    return None


def _block_scalar(style, chomp, cont):
    first = next((l for l in cont if l.strip()), '')
    indent = len(first) - len(first.lstrip(' '))
    lines = [l[indent:] if l.strip() else '' for l in cont]
    if style == '|':
        text = '\n'.join(lines)
    else:
        text = ''
        for l in lines:
            if not l:
                text += '\n'
            else:
                if text and not text.endswith('\n'):
                    text += ' '
                text += l
    if chomp == '-':
        return text.rstrip('\n')
    if chomp == '+':
        return text + '\n'
    return text.rstrip('\n') + '\n' if text.strip() else ''


def scan(home, project=None, listings=None, roots_file=None):
    return Scan(home, project, listings, roots_file or DEFAULT_ROOTS).run()


# ---------------------------------------------------------------- 扫描

SKIP_DIRS = {'node_modules', '__pycache__'}  # 另外跳过所有隐藏目录；.system 作为单独的根目录扫
LOCK_FILES = ('.skills_store_lock.json', os.path.join('..', '.skill-lock.json'))  # 相对根目录
LOCK_FIELDS = ('source', 'sourceUrl', 'zip_url')  # 只留来源，别的字段可能是令牌


def lock_paths(roots):
    return {os.path.normpath(os.path.join(root, rel)) for root in roots for rel in LOCK_FILES}


class Scan(object):
    def __init__(self, home, project, listings, roots_file):
        self.home = os.path.abspath(home)
        self.project = os.path.abspath(project) if project else None
        if listings is not None:
            _validate_listings(listings)
        self.listings = listings or {}
        self.agents = sorted(listings) if listings is not None else None
        self.roots = _load_roots(self.home, self.project, roots_file)
        self.listing_roots = [r for r in self.roots if r.get('find') == 'listing']
        self.entries, self.by_path, self.entities, self.issues = [], {}, {}, []
        self.other_skill_files = set()

    def run(self):
        walk_roots = []
        for root in self.roots:
            if root.get('find') == 'installed_plugins':
                walk_roots += _claude_plugin_roots(root)  # 另一端的已装插件也可能链接到范围内实体
            elif not root.get('find'):
                walk_roots.append(root)
        self.walk_paths = {r['path'] for r in walk_roots}
        tops = []
        for root in walk_roots:
            tops += [(top, root) for top in self._walk(root)]
        self._add_listed()
        self._drop_out_of_scope()
        for real in self.entities:
            self.entities[real] = _describe(real, self.home)
        reals = sorted(self.entities)
        for real in reals:
            self.entities[real]['nested'] = [o for o in reals if _inside(o, real) and o != real]
        self._read_locks(walk_roots)
        for top, root in tops:  # 第一层里什么 skill 都没有、也没有别的问题的目录
            if not any(_inside(x['path'], top) for x in self.entries + self.issues):
                self.issue('no_skill_md', top, root)
        return {
            'version': 1,
            'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'home': self.home,
            'project': self.project,
            'roots': [{'path': r['path'], 'agents': r['agents'], 'kind': r['kind'], 'depth': r.get('depth'),
                       'find': r.get('find'), 'exists': os.path.isdir(r['path'])}
                      for r in walk_roots + self.listing_roots if self._in_scope(r)],
            'entries': self.entries,
            'entities': self.entities,
            'issues': self.issues,
        }

    def issue(self, kind, path, root, **extra):
        if self._in_scope(root):  # 范围外 agent 的问题不报
            item = {'type': kind, 'path': path}
            item.update(extra)
            self.issues.append(item)

    def _in_scope(self, x):
        return self.agents is None or any(a in self.agents for a in x['agents'])

    def _drop_out_of_scope(self):
        """另一端只提供引用和来源证据，不把私有入口写进 scan.json。"""
        included = [e for e in self.entries if self._in_scope(e)]
        protected = externally_referenced_paths(self.entries, self.agents, self.other_skill_files)
        self.entities = {e['entity']: None for e in included}
        for e in included:
            if e['path'] in protected:
                e['referenced_by_other_agent'] = True
        # _read_locks 需要从另一端的别名定位到共享实体。
        self.by_path = {e['path']: e for e in self.entries if e['entity'] in self.entities}
        self.entries = included

    def _walk(self, root):
        """从根目录往下扫，根目录算第 0 层。返回第一层的目录，用来报"没有 SKILL.md"。"""
        top = root['path']
        if not os.path.isdir(top):
            return []
        depth, tops = root.get('depth') or 1, []

        def visit(d, level, chain, via):
            try:
                names = sorted(os.listdir(d))
            except OSError as e:
                self.issue('unreadable', d, root, error=str(e))
                return
            for name in names:
                if name.startswith('.') or name in SKIP_DIRS or name == 'SKILL.md':
                    continue  # SKILL.md 在处理它所在的目录时已经看过
                p = os.path.join(d, name)
                if p in self.walk_paths:
                    continue  # 属于另一个更具体的根目录
                is_link = os.path.islink(p)
                if is_link and not os.path.exists(p):
                    self.issue('dead_link', p, root, target=os.readlink(p))
                    continue
                if not os.path.isdir(p):
                    continue
                if level == 1:
                    tops.append(p)
                real = os.path.realpath(p)
                if real in chain:
                    self.issue('loop', p, root, target=os.readlink(p) if is_link else None)
                    continue
                md = os.path.join(p, 'SKILL.md')
                if os.path.islink(md) and not os.path.exists(md):
                    self.issue('dead_link', md, root, target=os.readlink(md))
                elif os.path.isfile(md):
                    self._add(p, root, level, via)
                if level < depth:
                    visit(p, level + 1, chain | {real}, p if is_link else via)

        visit(top, 1, {os.path.realpath(top)}, None)
        return tops

    def _add_listed(self):
        """把清单里列出、但没扫到的入口补进来。插件目录只靠这一步。"""
        for item in self.listings.get('codex', {}).get('entries', []):
            md = item.get('path')
            if not md or not os.path.isabs(md) or os.path.basename(md) != 'SKILL.md':
                continue
            d = os.path.dirname(os.path.normpath(md))
            if d in self.by_path or not os.path.isfile(os.path.join(d, 'SKILL.md')):
                continue
            root = next((r for r in self.listing_roots if _inside(d, r['path'])), None)
            if root:
                self._add(d, root, None, None, plugin=_codex_plugin(root['path'], d))
            else:
                self._add(d, {'path': None, 'kind': 'listed', 'agents': ['codex']}, None, None)

    def _add(self, path, root, level, via, plugin=None):
        md = os.path.join(path, 'SKILL.md')
        if not self._in_scope(root):
            # 即使入口不允许读取，移动它所经过的链接也可能令另一端失效。
            self.other_skill_files.add(md)
        if path in self.by_path:
            return
        real = os.path.realpath(path)
        if os.path.commonpath([real, os.path.realpath(md)]) != real:
            self.issue('unreadable', md, root, error='SKILL.md 链接到 skill 目录外')
            return
        st = os.lstat(path)
        entry = {
            'path': path,
            'root': root['path'],
            'kind': root['kind'],
            'agents': list(root['agents']),
            'level': level,
            'link': os.readlink(path) if stat.S_ISLNK(st.st_mode) else None,
            'via': via,
            'entity': real,
            'installed_at': getattr(st, 'st_birthtime', st.st_mtime),
            'plugin': plugin or root.get('plugin'),
        }
        self.entries.append(entry)
        self.by_path[path] = entry
        self.entities.setdefault(entry['entity'], None)

    def _read_locks(self, walk_roots):
        seen = set()
        for root in walk_roots:
            if root['kind'] == 'plugin':
                continue
            for rel in LOCK_FILES:
                lock = os.path.normpath(os.path.join(root['path'], rel))
                if lock in seen:
                    continue
                seen.add(lock)
                skills = (_load_json(lock) or {}).get('skills')
                if not isinstance(skills, dict):
                    continue
                for name in sorted(skills):
                    entry = self.by_path.get(os.path.join(root['path'], name))
                    if entry:
                        record = skills[name] if isinstance(skills[name], dict) else {}
                        if not self._in_scope(root):
                            if any(isinstance(record.get(k), str) and record[k] for k in LOCK_FIELDS):
                                self.entities[entry['entity']]['other_agent_has_source'] = True
                            continue
                        self.entities[entry['entity']]['source']['locks'].append(
                            {'file': lock, 'name': name,
                             'record': _source_fields(record, LOCK_FIELDS)})
                    else:
                        self.issue('stale_lock', lock, root, name=name)


def _resolution_nodes(path):
    """解析绝对路径经过的目录项，包括中间软链接。只查链接元数据。"""
    nodes, seen = set(), set()
    pending = deque(path.split(os.sep))
    current = os.sep
    while pending:
        part = pending.popleft()
        if part in ('', '.'):
            continue
        if part == '..':
            current = os.path.dirname(current)
            continue
        node = os.path.join(current, part)
        nodes.add(node)
        if not os.path.islink(node):
            current = node
            continue
        state = (node, tuple(pending))
        if state in seen or len(seen) >= 40:
            break
        seen.add(state)
        try:
            target = os.readlink(node)
        except OSError:
            break  # 解析期间链接已变化
        if os.path.isabs(target):
            current = os.sep
        pending.extendleft(reversed(target.split(os.sep)))
    return nodes


def externally_referenced_paths(entries, agents, other_files=()):
    """找出移动后可能断开另一端引用的入口，只返回范围内路径。也适用于旧 scan.json。"""
    if agents is None:
        return set()
    others = [e for e in entries if not set(e['agents']).intersection(agents)]
    files = set(other_files)
    files.update(os.path.join(e['path'], 'SKILL.md') for e in others)
    if not files:
        return set()
    nodes = set()
    for path in files:
        nodes.update(_resolution_nodes(path))
    other_entities = {e['entity'] for e in others}
    protected = set()
    for e in entries:
        if not set(e['agents']).intersection(agents):
            continue
        # 移走入口实际删去的是它所在目录里的这个名字，两个独立链接可指向同一实体。
        parent, name = os.path.split(e['path'])
        slot = os.path.join(os.path.realpath(parent), name)
        if slot in nodes or (not e['link'] and any(_inside(e['entity'], target) or
                _inside(target, e['entity']) for target in other_entities)):
            protected.add(e['path'])
    return protected


def _inside(path, top):
    return path == top or path.startswith(top + os.sep)


def _open_regular(base, path):
    """读取实体内的普通文件。先解析合法链接，再逐层打开目录，避免检查后被替换成链接或管道。"""
    base = os.path.realpath(base)
    target = os.path.realpath(path)
    if target == base or os.path.commonpath([base, target]) != base:
        raise OSError('文件位于目录外')
    parts = os.path.relpath(target, base).split(os.sep)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(base, dir_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, dir_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        if not stat.S_ISREG(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
            raise OSError('需要普通文件')
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError('需要普通文件')
    return os.fdopen(fd, 'rb')


def _load_json(path, base=None):
    try:
        with _open_regular(base or os.path.dirname(os.path.abspath(path)), path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_roots(home, project, roots_file):
    with open(roots_file, encoding='utf-8') as fh:
        spec = json.load(fh)
    roots, seen = [], set()
    for r in spec['roots']:
        p = r['path']
        if '{project}' in p:
            if not project:
                continue
            p = p.replace('{project}', project)
        if p == '~' or p.startswith('~/'):
            p = home + p[1:]
        p = os.path.normpath(p)
        key = (os.path.realpath(p), tuple(r['agents']))  # 在主目录里运行时，项目目录和个人目录是同一个
        if key in seen:
            continue
        seen.add(key)
        roots.append(dict(r, path=p))
    return roots


def _claude_plugin_roots(root):
    """按 installed_plugins.json 找到每个已装插件的 skills 目录，扫一层。"""
    plugins_dir = os.path.dirname(root['path'])
    installed = (_load_json(os.path.join(plugins_dir, 'installed_plugins.json')) or {}).get('plugins') or {}
    settings = _load_json(os.path.join(os.path.dirname(plugins_dir), 'settings.json')) or {}
    enabled = settings.get('enabledPlugins') or {}
    out = []
    for pid in sorted(installed):
        for inst in installed[pid] or []:
            if not isinstance(inst, dict) or not inst.get('installPath'):
                continue
            plugin = {'id': pid, 'name': pid.split('@')[0], 'version': inst.get('version'),
                      'git_sha': inst.get('gitCommitSha'), 'enabled': enabled.get(pid)}
            out.append(dict(root, path=os.path.join(inst['installPath'], 'skills'), depth=1, find=None, plugin=plugin))
    return out


def _codex_plugin(cache, d):
    """~/.codex/plugins/cache/<市场>/<插件>/<版本>/skills/... -> 插件信息。"""
    parts = os.path.relpath(d, cache).split(os.sep)
    if len(parts) < 3:
        return None
    return {'id': '%s@%s' % (parts[1], parts[0]), 'name': parts[1], 'version': parts[2], 'git_sha': None, 'enabled': None}


# ---------------------------------------------------------------- 实体

def _describe(real, home):
    md = os.path.join(real, 'SKILL.md')
    try:
        with _open_regular(real, md) as fh:
            data = fh.read()
        fm = check_frontmatter(data.decode('utf-8', 'replace'))
    except OSError:
        data, fm = b'', dict(check_frontmatter(''), status='unreadable')
    fp = fingerprint(real)
    meta = _load_json(os.path.join(real, '_meta.json'), real)
    return {
        'path': real,
        'frontmatter': fm,
        'skill_md': {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)},
        'fingerprint': fp,
        'updated_at': fp['updated_at'],
        'nested': [],
        'source': sanitize_source({'clawhub': meta, 'frontmatter': fm['fields'], 'git': _git(real, home)}),
        'portability': _portability(real, fp['manifest']),
    }


def _git(path, home):
    """从 skill 目录往上找 .git，不看主目录本身。"""
    stop = os.path.realpath(home)
    d = path
    while d != stop and d != os.path.dirname(d):
        g = os.path.join(d, '.git')
        if os.path.isdir(g):
            return {'root': d, 'remote': _origin_url(d)}
        if os.path.isfile(g):
            return {'root': d, 'remote': None}
        d = os.path.dirname(d)
    return None


def _origin_url(repo):
    """让 Git 解析其引号、转义和续行。只给它已核验的文件描述符，不跟随 include。"""
    # Git 在执行 config 之前也会读取所在仓库和用户配置。调用目录可能是第三方项目。
    env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': os.devnull,
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    command = ['git', '--git-dir=' + os.devnull, 'config', '--no-includes', '--file', '-', '--get', 'remote.origin.url']
    try:
        with _open_regular(repo, os.path.join(repo, '.git', 'config')) as fh:
            result = subprocess.run(command, stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    cwd=os.path.sep, env=env)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _source_value('remote', result.stdout.decode('utf-8', 'replace').rstrip('\n'))


def _source_value(field, value):
    """保留仓库定位信息，丢弃认证信息及无法识别的来源格式。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] == '"':  # 兼容旧报告保留的 Git 配置引号
        value = value[1:-1]
    if re.search(r'[\s\x00-\x1f\x7f"<>\\]', value):
        return None
    if '://' in value:
        try:
            url = urlsplit(value)
            host, port = url.hostname, url.port
        except ValueError:
            return None
        if url.scheme not in ('http', 'https', 'ssh', 'git', 'git+ssh', 'git+https', 'ftp', 'ftps') or not host:
            return None
        if ':' in host:
            host = '[' + host + ']'  # urlsplit 已校验 IPv6
        elif not re.fullmatch(r'[\w.-]+', host):
            return None
        return url.scheme + '://' + host + (':' + str(port) if port is not None else '') + url.path
    locator = re.split(r'[?#]', value, maxsplit=1)[0]
    scp = re.fullmatch(r'(?:[\w.-]+@)?([\w.-]+|\[[\da-fA-F:.]+\]):([^:@]+)', locator)
    if scp and ('/' in scp.group(2) or scp.group(2).endswith('.git') or '.' in scp.group(1)):
        return scp.group(1) + ':' + scp.group(2)
    if field in ('source', 'repository') and re.fullmatch(r'[\w.-]+/[\w.-]+', locator):
        return locator
    if field == 'source' and locator in ('skillhub', 'clawhub', 'github', 'gitlab'):
        return locator
    return None


def _source_fields(record, keys):
    clean = {}
    if isinstance(record, dict):
        for key in keys:
            value = _source_value(key, record.get(key))
            if value is not None:
                clean[key] = value
    return clean


def sanitize_source(src):
    """扫描、计划与页面都可读旧报告；只交给下一步已清洗的显示字段。"""
    src = src if isinstance(src, dict) else {}
    meta = src.get('clawhub')
    if isinstance(meta, dict):
        meta = {k: meta[k] for k in ('ownerId', 'slug', 'version', 'publishedAt')
                if isinstance(meta.get(k), (str, int, float))}
    else:
        meta = None
    git = src.get('git')
    if isinstance(git, dict) and isinstance(git.get('root'), str):
        git = {'root': git['root'], 'remote': _source_value('remote', git.get('remote'))}
    else:
        git = None
    locks = []
    raw_locks = src.get('locks')
    for item in raw_locks if isinstance(raw_locks, list) else ():
        if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ('file', 'name')):
            locks.append({'file': item['file'], 'name': item['name'],
                          'record': _source_fields(item.get('record'), LOCK_FIELDS)})
    return {'clawhub': meta or None, 'git': git, 'locks': locks,
            'frontmatter': _source_fields(src.get('frontmatter'), SOURCE_FIELDS)}


# ---------------------------------------------------------------- 目录指纹
#
# 整个目录比较：每个文件的相对路径、类型、内容、可执行权限、链接目标。
# 系统自动生成的文件不参与比较。读不全时 complete 为 False。见 DESIGN.md 第 7 节。

IGNORED_NAMES = {'.DS_Store', '__pycache__'}


def fingerprint(top):
    manifest, state = [], {'complete': True, 'bytes': 0, 'updated_at': 0.0}

    def walk(rel):
        d = os.path.join(top, rel) if rel else top
        try:
            names = sorted(os.listdir(d))
        except OSError:
            state['complete'] = False
            return
        for name in names:
            if name in IGNORED_NAMES or name.endswith('.pyc'):
                continue
            r = rel + '/' + name if rel else name
            p = os.path.join(top, r)
            try:
                st = os.lstat(p)
            except OSError:
                state['complete'] = False
                continue
            state['updated_at'] = max(state['updated_at'], st.st_mtime)
            if stat.S_ISLNK(st.st_mode):
                manifest.append([r, 'l', os.readlink(p), None])
            elif stat.S_ISDIR(st.st_mode):
                manifest.append([r, 'd', None, None])
                walk(r)
            elif stat.S_ISREG(st.st_mode):
                try:
                    digest = hashlib.sha256()
                    with _open_regular(top, p) as fh:
                        for block in iter(lambda: fh.read(1 << 20), b''):
                            digest.update(block)
                except OSError:
                    state['complete'] = False
                    continue
                manifest.append([r, 'f', digest.hexdigest(), bool(st.st_mode & 0o111)])
                state['bytes'] += st.st_size
            else:
                manifest.append([r, 'o', None, None])

    walk('')
    blob = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'sha256': hashlib.sha256(blob).hexdigest(), 'files': sum(1 for m in manifest if m[1] == 'f'),
            'bytes': state['bytes'], 'complete': state['complete'], 'updated_at': state['updated_at'],
            'manifest': manifest}


# ---------------------------------------------------------------- 是否通用
#
# 只标出来，由 agent 再确认。见 DESIGN.md 第 7 节。

PORTABILITY = [
    ('claude_plugin_root', re.compile(r'CLAUDE_PLUGIN_ROOT')),
    ('claude_tool', re.compile(r'\b(?:AskUserQuestion|TodoWrite|ExitPlanMode|EnterPlanMode|NotebookEdit|WebFetch|'
                               r'WebSearch|SlashCommand|MultiEdit|BashOutput|KillShell)\b')),
    ('codex_path', re.compile(r'(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)/\.codex\b')),
]
PORTABILITY_BYTES = re.compile(rb'CLAUDE_PLUGIN_ROOT|AskUserQuestion|TodoWrite|PlanMode|NotebookEdit|WebFetch|'
                               rb'WebSearch|SlashCommand|MultiEdit|BashOutput|KillShell|/\.codex')
MAX_TEXT_BYTES = 1 << 20
MAX_HITS = 50


def _portability(top, manifest):
    hits = []
    for rel, kind, _, _ in manifest:
        if kind != 'f':
            continue
        p = os.path.join(top, rel)
        try:
            if os.path.getsize(p) > MAX_TEXT_BYTES:
                continue
            with _open_regular(top, p) as fh:
                data = fh.read(MAX_TEXT_BYTES + 1)
        except OSError:
            continue
        if len(data) > MAX_TEXT_BYTES or b'\0' in data[:8192] or not PORTABILITY_BYTES.search(data):
            continue
        for no, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            for name, rx in PORTABILITY:
                if rx.search(line):  # 命中行可能含凭据，只保留位置
                    hits.append({'kind': name, 'file': rel, 'line': no})
                    if len(hits) >= MAX_HITS:
                        return hits
    return hits


# ---------------------------------------------------------------- 命令行

def _validate_listings(listings):
    if not isinstance(listings, dict) or not listings or set(listings) - {'claude', 'codex'}:
        raise ValueError('listings 必须包含有效的 agent 清单')
    for listing in listings.values():
        if not isinstance(listing, dict) or not isinstance(listing.get('entries'), list):
            raise ValueError('每个 agent 的清单都需要 entries 列表')
        for entry in listing['entries']:
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                raise ValueError('清单条目必须含有名字')
            if entry.get('path') is not None and not isinstance(entry['path'], str):
                raise ValueError('清单路径必须是字符串')


def _read_listings(path):
    data = _load_json(path)
    if not data or not isinstance(data.get('listings'), dict):
        raise ValueError('--listings 文件不可读、JSON 无效，或缺少 listings')
    listings = data['listings']
    if 'scope' in data or 'current' in data:
        current, scope = data.get('current'), data.get('scope')
        if current not in ('codex', 'claude') or scope not in ('current', 'all'):
            raise ValueError('usage.json 的 current 或 scope 无效')
        expected = {current} if scope == 'current' else {'claude', 'codex'}
        if set(listings) != expected:
            raise ValueError('usage.json 的清单与所选范围不符')
    return listings


def main(argv=None):
    ap = argparse.ArgumentParser(description='扫描 skill 目录，输出 scan.json。只读。')
    ap.add_argument('--home', default=os.path.expanduser('~'), help='主目录，测试时可以换成临时目录')
    ap.add_argument('--project', default=os.getcwd(), help='项目目录，默认是当前目录')
    ap.add_argument('--roots', default=DEFAULT_ROOTS, help='根目录清单，默认 references/roots.json')
    ap.add_argument('--listings', help='含 listings 字段的 JSON，例如 usage.json')
    ap.add_argument('--out', help='输出文件，不写就打印到标准输出')
    a = ap.parse_args(argv)
    try:
        listings = _read_listings(a.listings) if a.listings is not None else None
        result = scan(a.home, a.project, listings, a.roots)
    except ValueError as err:
        ap.error(str(err))
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
    else:
        sys.stdout.write(text + '\n')


if __name__ == '__main__':
    main()
