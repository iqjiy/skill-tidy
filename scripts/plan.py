"""skill-tidy 第一阶段：把 scan.json、usage.json 和 agent 的判断合成 plan.json。只读。

只用标准库，兼容 macOS 自带的 python3 3.9。

用法：
    python3 plan.py --scan scan.json --usage usage.json [--judgments judgments.json] [--out plan.json]

这里只算机械结论：加载状态、同名组、包内变体、已共享、内容相同或不同、不通用标记、
按 DESIGN.md 第 6 节得出的默认勾选，以及每种选法"如果执行会做哪几步"。
需要判断的部分由 agent 写进 judgments.json：

    {"trigger_conflicts": [{"agent": "codex", "members": ["入口路径", "入口路径"],
                            "example": "同一句用户请求", "keep": "入口路径",
                            "keep_can_do_it": true, "reason": "为什么"}],
     "portable": {"skill 名字": true}}

写不出例句的触发冲突不收，记进 rejected_judgments。
"""
import argparse
import calendar
import json
import os
import sys
import time

from scan import externally_referenced_paths, lock_paths, sanitize_source

FIXED_KINDS = ('builtin', 'plugin')  # 永远不进删除候选
USED = ('loaded', 'manual_only')     # 参与去重和收拢
RANK = {'documented': 2, 'unknown': 1, 'suspected_copy': 0}  # 来源证据：有据可查 > 不知道 > 疑似转载


def build(scan_data, usage_data, judgments=None):
    return Planner(scan_data, usage_data, judgments or {}).run()


def _inside(path, top):
    return path == top or path.startswith(top + os.sep)


def _base(name):
    """插件前缀不算名字的一部分：pdf:pdf、superpowers:brainstorming 和 pdf、brainstorming 算同名。"""
    return name.rsplit(':', 1)[-1]


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return None


class Planner(object):
    def __init__(self, s, u, j):
        self.s, self.u, self.j = s, u, j
        self.current, self.scope = u['current'], u['scope']
        self.agents = [self.current] if self.scope == 'current' else ['claude', 'codex']
        self.scope_roots = [r['path'] for r in s['roots'] if set(r['agents']).intersection(self.agents)]
        self.scope_locks = lock_paths(self.scope_roots)
        # 旧扫描保留了范围外入口，新扫描只保留保护标记。
        outside_roots = [r['path'] for r in s['roots'] if not set(r['agents']).intersection(self.agents)]
        # 全量快照可能只在诊断里记录了另一端的外链 SKILL.md，文件内容依然不可读。
        other_files = [i['path'] for i in s['issues'] if i['type'] == 'unreadable' and
                       os.path.basename(i['path']) == 'SKILL.md' and
                       any(_inside(i['path'], root) for root in outside_roots)]
        self.protected_paths = externally_referenced_paths(s['entries'], self.agents, other_files)
        self.protected_paths.update(e['path'] for e in s['entries'] if e.get('referenced_by_other_agent'))
        shared = [r['path'] for r in s['roots'] if r['kind'] == 'shared']
        self.shared_root = shared[0] if shared else os.path.join(s['home'], '.agents', 'skills')
        self.entries, self.by_path, self.diagnostics, self.builtin, self.rejected = {}, {}, [], {}, []

    def run(self):
        self._make_entries()
        for agent in self.agents:
            self._load_status(agent)
            self._attach_usage(agent)
        issues = self.s['issues']
        if self.scope == 'current':
            issues = [i for i in issues if i['path'] in self.scope_locks or
                      any(_inside(i['path'], root) for root in self.scope_roots)]
        self.diagnostics = [dict(i) for i in issues] + self.diagnostics + self._format_hints()
        dedup = []
        for agent in self.agents:
            dedup += self._dedup_groups(agent)
        dedup += self._trigger_conflicts()
        consolidate = self._consolidate_groups()
        for prefix, groups in (('d', dedup), ('c', consolidate)):
            for n, g in enumerate(groups, 1):
                g['id'] = '%s%d' % (prefix, n)
                for keep, opt in g['options'].items():
                    for k, st in enumerate(opt['steps'], 1):
                        st['id'] = '%s:%s:%d' % (g['id'], keep, k)
        return {
            'version': 1,
            'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'home': self.s['home'],
            'current': self.current,
            'scope': self.scope,
            'agents': self.agents,
            'listings': {a: {'from': self.u['listings'][a]['from'], 'session': self.u['listings'][a]['session'],
                             'count': len(self.u['listings'][a]['entries'])} for a in self.agents},
            'usage': dict((a, dict((k, v) for k, v in self.u['usage'][a].items() if k not in ('by_name', 'by_entity')))
                          for a in self.agents),
            'builtin': self.builtin,
            'entries': self.entries,
            'diagnostics': self.diagnostics,
            'dedup': dedup,
            'consolidate': consolidate,
            'rejected_judgments': self.rejected,
        }

    # ------------------------------------------------------------ 入口

    def _make_entries(self):
        for i, e in enumerate(self.s['entries'], 1):
            agent = next((a for a in e['agents'] if a in self.agents), None)
            if agent is None:
                continue
            ent = self.s['entities'][e['entity']]
            fm, src = ent['frontmatter'], sanitize_source(ent['source'])
            names = []
            for n in (fm['name'], os.path.basename(e['path'])):
                if n and n not in names:
                    names.append(n)
            if e['plugin'] and agent == 'claude':
                names = ['%s:%s' % (e['plugin']['name'], n) for n in names]
            documented = bool(ent.get('other_agent_has_source') or e['plugin'] or (src['git'] and src['git']['remote'])
                              or src['clawhub'] or src['frontmatter'] or any(l['record'] for l in src['locks']))
            if self.scope == 'current':
                src = dict(src, locks=[l for l in src['locks'] if l['file'] in self.scope_locks])
            x = {
                'id': 'e%d' % i, 'path': e['path'], 'agent': agent, 'kind': e['kind'], 'root': e['root'],
                'level': e['level'], 'link': e['link'], 'via': e['via'], 'entity': e['entity'], 'plugin': e['plugin'],
                'names': names, 'name': names[0], 'status': None, 'reason': None, 'reason_line': fm['line'],
                'description': fm['description'],
                'manual': (fm['fields'].get('disable-model-invocation') or '').lower() in ('true', 'yes', 'on'),
                'frontmatter': fm['status'], 'hints': [h['kind'] for h in fm['hints']] + fm['unverified'],
                'uses': None, 'uses_note': None, 'installed_at': e['installed_at'],
                'content': ent['fingerprint']['sha256'], 'complete': ent['fingerprint']['complete'],
                'skill_md_bytes': ent['skill_md']['bytes'], 'updated_at': ent['updated_at'],
                'documented': documented, 'source': src,
                'portability': sorted({h['kind'] for h in ent['portability']}),
                'portability_hits': [{k: hit[k] for k in ('kind', 'file', 'line')} for hit in ent['portability']],
            }
            self.entries[x['id']] = x
            self.by_path[x['path']] = x

    def mine(self, agent):
        return [x for x in self.entries.values() if x['agent'] == agent]

    def _load_status(self, agent):
        listing = self.u['listings'][agent]
        mine = self.mine(agent)
        if listing['from'] == 'none':
            for x in mine:
                x['status'] = 'unknown'
            self.diagnostics.append({'type': 'listing_unknown', 'agent': agent})
            return
        if agent == 'codex':  # 清单里有路径
            listed = {}
            for item in listing['entries']:
                if item.get('path'):
                    listed.setdefault(os.path.dirname(os.path.normpath(item['path'])), item)
            for x in mine:
                if x['path'] in listed:
                    x['status'], x['name'] = 'loaded', listed[x['path']]['name']
            for d, item in listed.items():
                if d not in self.by_path:
                    self.diagnostics.append({'type': 'listed_missing', 'agent': agent, 'name': item['name'], 'path': item['path']})
        else:  # Claude Code 的清单只有名字
            self.builtin[agent] = []
            for item in listing['entries']:
                hits = [x for x in mine if item['name'] in x['names']]
                if len({h['entity'] for h in hits}) == 1:
                    hits[0]['status'], hits[0]['name'] = 'loaded', item['name']
                elif hits:
                    for h in hits:
                        h['status'], h['name'] = 'unlocated', item['name']
                    self.diagnostics.append({'type': 'unlocated', 'agent': agent, 'name': item['name'],
                                             'entries': sorted(h['id'] for h in hits)})
                else:
                    self.builtin[agent].append(item['name'])
        session = _epoch((listing.get('session') or {}).get('timestamp'))
        loaded = {x['entity'] for x in mine if x['status'] == 'loaded'}
        for x in mine:
            if x['status']:
                continue
            if x['entity'] in loaded:
                x['status'] = 'duplicate_path'  # 同一实体已经从别的入口加载
            elif agent == 'claude' and x['manual']:
                x['status'] = 'manual_only'  # 模型清单不列，/名字 照样能用
            elif x['plugin'] and x['plugin'].get('enabled') is False:
                x['status'] = 'plugin_disabled'
            elif session and x['installed_at'] > session:
                x['status'] = 'installed_after'
                self.diagnostics.append({'type': 'installed_after', 'entry': x['id']})
            else:
                x['status'], x['reason'] = 'skipped', x['frontmatter']
                self.diagnostics.append({'type': 'skipped', 'entry': x['id'], 'reason': x['reason']})

    def _attach_usage(self, agent):
        u = self.u['usage'][agent]
        mine = self.mine(agent)
        if any(g['kind'] == 'no_logs' for g in u['gaps']):
            for x in mine:
                x['uses_note'] = 'no_logs'
            return
        if agent == 'codex':
            for x in mine:
                x['uses'] = u['by_entity'].get(x['entity'], 0)
            return
        for x in mine:
            x['uses'] = 0
        for name, n in u['by_name'].items():
            hits = [x for x in mine if name in x['names'] or name == x['name']]
            if len({h['entity'] for h in hits}) == 1:
                for h in hits:
                    h['uses'] += n
            else:
                for h in hits:  # 同名调用无法确定是哪一份
                    h['uses'], h['uses_note'] = None, 'ambiguous_name'

    def _format_hints(self):
        seen, out = set(), []
        for x in self.entries.values():
            if x['status'] in USED and x['hints'] and x['entity'] not in seen:
                seen.add(x['entity'])
                out.append({'type': 'format_hint', 'entry': x['id'], 'hints': x['hints']})
        return out

    def _chars(self, x):
        """预计从清单里移出的字符数，按清单一行的写法估算。"""
        line = '- %s: %s' % (x['name'], ' '.join((x['description'] or '').split()))
        if x['agent'] == 'codex':
            rel = os.path.relpath(os.path.join(x['path'], 'SKILL.md'), x['root'] or os.path.dirname(x['path']))
            line += ' (file: r0/%s)' % rel
        return len(line)

    # ------------------------------------------------------------ 去重

    def _dedup_groups(self, agent):
        if self.u['listings'][agent]['from'] == 'none':
            return []
        by_name = {}
        for x in self.mine(agent):
            if x['status'] in USED:
                by_name.setdefault(_base(x['name']), {}).setdefault(x['entity'], x)
        return [self._dedup_group(agent, name, list(m.values()), None)
                for name, m in sorted(by_name.items()) if len(m) > 1]

    def _dedup_group(self, agent, name, members, kind, judged_keep=None):
        outer = min(members, key=lambda m: len(m['entity']))
        if kind is None:
            kind = 'package_variant' if all(_inside(m['entity'], outer['entity']) for m in members) else 'same_name'
        content = _content(members)
        evidence = dict((m['id'], self._evidence(m, members)) for m in members)
        keep, why = self._default_keep(agent, kind, members, evidence, outer, judged_keep)
        options = dict((m['id'], self._dedup_option(agent, kind, m, members, content)) for m in members)
        level = options[keep]['level'] if keep else ('red' if kind == 'package_variant' else 'yellow')
        return {
            'agent': agent, 'name': name, 'kind': kind, 'content': content,
            'members': [m['id'] for m in members], 'evidence': evidence,
            'default_keep': keep, 'keep_reason': why, 'level': level,
            'process_default': bool(keep) and level != 'red' and not options[keep]['blocked'],
            'options': options, 'diff': _diff(members, self.s),
        }

    def _evidence(self, m, members):
        desc = ' '.join((m['description'] or '').split())
        for o in members:
            if o is not m and desc and ' '.join((o['description'] or '').split()) == desc and m['skill_md_bytes'] < o['skill_md_bytes']:
                return 'suspected_copy'  # 描述和另一份一字不差、内容更短
        return 'documented' if m['documented'] else 'unknown'

    def _default_keep(self, agent, kind, members, evidence, outer, judged_keep):
        """DESIGN.md 第 6 节：前一步能分出来就不看后面的。"""
        if kind == 'package_variant':
            return outer['id'], 'package_variant'
        fixed = [m for m in members if m['kind'] in FIXED_KINDS]
        if fixed:
            return fixed[0]['id'], 'fixed'
        if judged_keep:
            return judged_keep, 'agent'
        top = max(RANK[evidence[m['id']]] for m in members)
        best = [m for m in members if RANK[evidence[m['id']]] == top]
        if len(best) == 1:
            return best[0]['id'], 'evidence'
        if kind != 'same_name' and not self.u['usage'][agent]['gaps'] and all(m['uses'] is not None for m in best):
            most = max(m['uses'] for m in best)
            if [m['uses'] for m in best].count(most) == 1:
                return next(m for m in best if m['uses'] == most)['id'], 'usage'
        if top == RANK['unknown']:
            return None, 'unknown_sources'
        return max(best, key=lambda m: m['updated_at'])['id'], 'newest'

    def _dedup_option(self, agent, kind, keep, members, content):
        keeps = [keep['path']] + [m['path'] for m in members if m['kind'] in FIXED_KINDS and m is not keep]
        steps, blocked, savings, touches_project = [], None, 0, False
        for m in members:
            if m is keep or m['kind'] in FIXED_KINDS:
                continue
            touches_project = touches_project or m['kind'] in ('project', 'listed')
            if _inside(keep['path'], m['path']):
                blocked = '要移走的 %s 里套着要保留的 %s' % (m['path'], keep['path'])
                continue
            st = self._removal(agent, m, keeps)
            steps.append(st)
            if st['type'] != 'impossible' and m['status'] == 'loaded':
                savings += self._chars(m)
        if kind == 'package_variant' or touches_project:
            level = 'red'
        elif kind == 'same_name' and content == 'identical':
            level = 'green'
        else:
            level = 'yellow'
        return {'keep': keep['id'], 'keeps': keeps, 'steps': steps, 'blocked': blocked, 'savings': savings, 'level': level}

    def _removal(self, agent, m, protected):
        """怎样去掉一个入口。DESIGN.md 第 1、9 节。"""
        if m['kind'] in FIXED_KINDS:
            return _step('impossible', m, reason='fixed')
        movable = m['kind'] == 'personal' or (m['kind'] == 'shared' and self.scope == 'all')
        if m['via'] or m['kind'] in ('project', 'listed'):
            movable = False  # 经软链接目录进来的会动到链接另一头；项目目录可能是团队共用
        if m['path'] in self.protected_paths:
            movable = False
        if movable and not m['link']:
            users = [x for x in self.entries.values() if x['entity'] == m['entity'] and x['path'] != m['path']]
            movable = not users  # 共享实体还被别的入口引用时，不移动实体
        if movable:
            return _step('trash', m, removes=[m['path']])
        if agent == 'codex':
            return _step('disable', m, path=os.path.join(m['path'], 'SKILL.md'))
        # Claude Code 按名字关闭会关掉所有同名的（插件 skill 带不带前缀还没验证，按去掉前缀后的名字算）
        same = [x for x in self.mine('claude') if x is not m and x['status'] in USED and _base(x['name']) == _base(m['name'])]
        if any(x['path'] in protected for x in same):
            return _step('impossible', m, reason='name_shared')
        return _step('disable', m, name=m['name'])

    # ------------------------------------------------------------ agent 判断出的触发冲突

    def _trigger_conflicts(self):
        groups = []
        for j in self.j.get('trigger_conflicts') or []:
            if j.get('agent') not in self.agents:
                continue
            members = [self.by_path.get(p) for p in j.get('members') or []]
            if not (j.get('example') or '').strip():
                reason = 'no_example'  # 写不出例句就不算
            elif len(members) < 2 or None in members:
                reason = 'unknown_member'
            elif any(m['agent'] != j.get('agent') or m['status'] not in USED for m in members):
                reason = 'not_loaded'
            else:
                reason = None
            if reason:
                self.rejected.append({'judgment': j, 'reason': reason})
                continue
            keep = self.by_path.get(j.get('keep'))
            g = self._dedup_group(j['agent'], ' / '.join(m['name'] for m in members), members, 'trigger_conflict',
                                  keep['id'] if keep and keep in members else None)
            g.update({'example': j['example'], 'keep_can_do_it': j.get('keep_can_do_it'), 'judgment_reason': j.get('reason')})
            groups.append(g)
        return groups

    # ------------------------------------------------------------ 收拢

    def _consolidate_groups(self):
        if self.scope != 'all' or any(self.u['listings'][a]['from'] == 'none' for a in ('claude', 'codex')):
            return []  # 另一边加载情况未知时，只给当前 agent 的去重建议
        sides = {}
        for x in self.entries.values():
            if x['status'] in USED + ('duplicate_path',) and x['kind'] in ('personal', 'shared') and not x['via']:
                bucket = sides.setdefault(_base(x['name']), {'claude': {}, 'codex': {}})[x['agent']]
                cur = bucket.get(x['entity'])
                if cur is None or (cur['status'] == 'duplicate_path' and x['status'] != 'duplicate_path'):
                    bucket[x['entity']] = x  # 同一实体有几个入口时，用清单里列出的那个
        portable_override = self.j.get('portable') or {}
        groups = []
        for name, side in sorted(sides.items()):
            claude, codex = list(side['claude'].values()), list(side['codex'].values())
            if not claude or not codex:
                continue  # 只收拢两边都装了的
            members = claude + codex
            if set(side['claude']) & set(side['codex']):
                groups.append({'agent': None, 'name': name, 'kind': 'shared', 'content': 'identical',
                               'members': [m['id'] for m in members], 'default_keep': None, 'process_default': False,
                               'level': 'green', 'options': {}, 'portability': []})
                continue
            flags = sorted({f for m in members for f in m['portability']})
            portable = portable_override.get(name, not flags)
            content = _content(members)
            options = dict((m['id'], self._cons_option(m, claude, codex, content, portable)) for m in members)
            keep = None
            if content == 'identical':  # 内容不同时由用户选以哪份为准；相同时选不被挡住、步骤最少的
                keep = min(members, key=lambda m: (bool(options[m['id']]['blocked']), len(options[m['id']]['steps'])))['id']
            level = 'red' if not portable else ('green' if content == 'identical' else 'yellow')
            groups.append({
                'agent': None, 'name': name, 'kind': 'pair', 'content': content,
                'members': [m['id'] for m in members], 'default_keep': keep, 'level': level,
                'process_default': bool(keep) and portable and not options[keep]['blocked'],
                'portable': portable, 'portability': flags, 'options': options, 'diff': _diff(members, self.s),
            })
        return groups

    def _cons_option(self, src, claude, codex, content, portable):
        """以 src 为准收拢：实体放到 ~/.agents/skills/<名字>，Claude Code 用相对软链接指过去。"""
        in_place = src['kind'] == 'shared' and src['root'] == self.shared_root and src['level'] == 1
        target = src['path'] if in_place else os.path.join(self.shared_root, os.path.basename(src['path']))
        steps, blocked = [], None
        if not in_place:
            occupant = next((m for m in claude + codex if m['path'] == target), None)
            if occupant:
                steps.append(_step('trash', occupant, removes=[occupant['path']]))
            elif os.path.lexists(target):
                blocked = '目标位置 %s 已经被别的东西占着' % target
            steps.append(_step('move', src, target=target, needs=[src['path']]))
        for c in claude:
            rel = os.path.relpath(target, os.path.dirname(c['path']))
            steps.append(_step('link', c, target=rel, replace=c is not src))
        if src['agent'] == 'claude':  # Codex 本来就读共享目录，原来那份要移走
            for x in codex:
                if x['path'] != target:
                    steps.append(self._removal('codex', x, [src['path']]))
        level = 'red' if not portable else ('green' if content == 'identical' else 'yellow')
        return {'keep': src['id'], 'target': target, 'keeps': [src['path']], 'steps': steps, 'blocked': blocked,
                'savings': 0, 'level': level}


def _step(kind, m, **extra):
    st = {'type': kind, 'agent': m['agent'], 'entry': m['id'], 'path': m['path'], 'needs': [], 'removes': []}
    st.update(extra)
    return st


def _content(members):
    if not all(m['complete'] for m in members):
        return 'unverified'
    return 'identical' if len({m['content'] for m in members}) == 1 else 'different'


def _diff(members, s):
    """两份不同时，列出只在一边有的和内容不同的文件，给页面看。"""
    ents = []
    for m in members:
        if m['entity'] not in [e for e, _ in ents]:
            ents.append((m['entity'], m['id']))
    if len(ents) != 2:
        return None
    rows = [dict((r[0], tuple(r[1:])) for r in s['entities'][e]['fingerprint']['manifest']) for e, _ in ents]
    a, b = rows
    return {'a': ents[0][1], 'b': ents[1][1], 'only_a': _missing(a, b), 'only_b': _missing(b, a),
            'changed': sorted(p for p in a if p in b and a[p] != b[p])[:20]}


def _missing(x, y):
    """x 里有、y 里没有的，只列 y 缺的最浅那一层，例如 scripts/。"""
    out = set()
    for p in x:
        if p in y:
            continue
        parts = p.split('/')
        for i in range(len(parts)):
            q = '/'.join(parts[:i + 1])
            if q not in y:
                out.add(q + '/' if x[q][0] == 'd' else q)
                break
    return sorted(out)[:20]


# ---------------------------------------------------------------- 选择与步骤

def default_selections(plan):
    return dict((g['id'], {'process': g['process_default'], 'keep': g['default_keep']})
                for g in plan['dedup'] + plan['consolidate'] if g['options'])


def compute_steps(plan, selections=None):
    """每改一次选择都从头算：先去重，再收拢。页面上的 JavaScript 是同一套算法。"""
    sel = default_selections(plan) if selections is None else selections
    chosen = []
    for g in plan['dedup'] + plan['consolidate']:
        s = sel.get(g['id']) or {}
        opt = g['options'].get(s.get('keep')) if s.get('process') else None
        if opt:
            chosen.append((g, opt))
    steps, conflicts, removed, runnable = [], [], set(), []
    for g, opt in chosen:
        if opt['blocked']:  # 不可选的方案只报冲突，它的步骤都不做
            conflicts.append({'type': 'blocked', 'group': g['id'], 'reason': opt['blocked']})
            continue
        runnable.append((g, opt))
        for st in opt['steps']:
            if st['type'] == 'trash' and st['path'] in removed:
                continue  # 去重已经移走了，收拢不用再移
            removed.update(st['removes'])
            steps.append(dict(st, group=g['id']))
    for g, opt in runnable:
        for st in opt['steps']:
            for r in st['removes']:
                for g2, opt2 in runnable:
                    if g2 is g:
                        continue
                    for p in sorted(set(opt2['keeps']) | {n for st2 in opt2['steps'] for n in st2['needs']}):
                        if _inside(p, r):
                            conflicts.append({'type': 'removed_by_other', 'group': g2['id'], 'path': p,
                                              'other_group': g['id'], 'other_step': st['id']})
    return {'steps': steps, 'conflicts': conflicts}


def _load(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description='合成 plan.json。只读。')
    ap.add_argument('--scan', required=True)
    ap.add_argument('--usage', required=True)
    ap.add_argument('--judgments', help='agent 写的判断，可选')
    ap.add_argument('--out', help='输出文件，不写就打印到标准输出')
    a = ap.parse_args(argv)
    result = build(_load(a.scan), _load(a.usage), _load(a.judgments) if a.judgments else None)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
    else:
        sys.stdout.write(text + '\n')


if __name__ == '__main__':
    main()
