"""skill-tidy：生成体检页面。

第一阶段只有 --static：读 plan.json，生成可以直接用浏览器打开的静态页面。页面上没有执行入口，
改选只影响页面上"如果执行会做哪几步"的预览。第二阶段再加本地服务，见 DESIGN.md 第 10 节。

只用标准库，兼容 macOS 自带的 python3 3.9。

用法：
    python3 serve.py --static --plan plan.json [--out skill-tidy.html] [--no-open]
"""
import argparse
import copy
import html
import json
import os
import re
import sys
import webbrowser

from scan import lock_paths, sanitize_source

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.normpath(os.path.join(HERE, '..', 'assets', 'page.html'))

AGENT = {'claude': 'Claude Code', 'codex': 'Codex'}
LEVEL = {'green': '🟢 可放心处理', 'yellow': '🟡 需要你判断', 'red': '🔴 谨慎'}
KIND = {'same_name': '同名', 'package_variant': '包内变体', 'trigger_conflict': '触发冲突'}
CONTENT = {'identical': '内容完全相同', 'different': '内容不同', 'unverified': '未验证（有文件读不了）'}
ENTRY_KIND = {'personal': '个人目录', 'shared': '共享目录', 'builtin': '内置', 'plugin': '插件',
              'project': '项目目录', 'listed': '清单里的路径'}
STATUS = {'loaded': '已加载', 'manual_only': '仅手动调用（/名字），不在模型清单里',
          'duplicate_path': '同一实体，已从别的入口加载', 'installed_after': '装在最近一次会话之后，还没加载过',
          'skipped': '被跳过', 'unlocated': '未定位', 'plugin_disabled': '插件已停用', 'unknown': '加载情况未知'}
REASON = {'no_frontmatter': '没有头部', 'body_in_frontmatter': '头部混进了正文', 'unclosed': '头部没有结束的 ---',
          'missing_name': '头部缺 name', 'missing_description': '头部缺 description',
          'unverified': '头部用了未验证的写法', 'ok': '头部检查没发现问题', 'unreadable': 'SKILL.md 读不了'}
EVIDENCE = {'documented': '有据可查', 'unknown': '不知道来源', 'suspected_copy': '疑似转载'}
KEEP_REASON = {'package_variant': '同一个包里的变体，默认不处理', 'fixed': '内置或插件自带的不进删除候选',
               'agent': 'agent 判断这一份能独立完成', 'evidence': '来源证据更好，请复核',
               'usage': '用得更多', 'unknown_sources': '几份都不知道来源，需要你判断',
               'newest': '来源证据相同，选更新的那份'}
PORTABILITY = {'claude_plugin_root': '用了 ${CLAUDE_PLUGIN_ROOT}', 'claude_tool': '写了 Claude 专有的工具名',
               'codex_path': '写死了 ~/.codex 路径'}
HINTS = {'unquoted_colon': '值里有没加引号的 ": "', 'comment': '用了注释', 'anchor': '用了 &', 'alias': '用了 *',
         'tag': '用了 !', 'indented': '有多余的缩进', 'multiline_plain': '多行普通文本', 'multiline_quoted': '多行引号文本',
         'multiline_flow': '多行 [...] 或 {...}', 'trailing_text': '引号后面还有文字', 'block_header': '多行文本的开头不认识'}
IMPOSSIBLE = {'fixed': '内置或插件自带的不能动',
              'name_shared': '项目目录不移动，按名字关闭又会关掉要保留的同名 skill'}
GAPS = {'no_logs': '没有日志', 'archives_skipped': '跳过了 {count} 个压缩归档（没有 zstd）'}


def e(s):
    return html.escape('' if s is None else str(s))


def code(s):
    return '<code>%s</code>' % e(s)


def step_text(st):
    t = st['type']
    if t == 'trash':
        return '移到回收站：%s' % st['path']
    if t == 'disable' and st['agent'] == 'codex':
        return '在 ~/.codex/config.toml 里按路径关闭：%s' % st['path']
    if t == 'disable':
        return '在 ~/.claude/settings.json 的 skillOverrides 里关闭：%s' % st['name']
    if t == 'move':
        return '移动：%s → %s' % (st['path'], st['target'])
    if t == 'link':
        return '%s换成软链接：%s → %s' % ('先移到回收站，再' if st.get('replace') else '', st['path'], st['target'])
    return '做不到：%s（%s）' % (st['path'], IMPOSSIBLE.get(st.get('reason'), st.get('reason')))


def _when(ts):
    return (ts or '').replace('T', ' ')[:16]


def _header(plan):
    scope = '只整理当前 agent' if plan['scope'] == 'current' else '两边都整理'
    rows = []
    for a in plan['agents']:
        l, u = plan['listings'][a], plan['usage'][a]
        if l['from'] == 'context':
            src = '清单取自当前会话（%d 条）' % l['count']
        elif l['from'] == 'log':
            s = l['session'] or {}
            src = '清单取自 %s 的会话（工作目录 %s，%d 条）' % (e(_when(s.get('timestamp'))), code(s.get('cwd')), l['count'])
        else:
            src = '取不到加载清单，加载情况未知'
        period = u.get('period')
        use = '使用次数统计区间 %s 至 %s' % (_when(period['from']), _when(period['to'])) if period else '没有使用记录'
        gaps = [GAPS[g['kind']].format(**g) if g['kind'] in GAPS else g['kind'] for g in u.get('gaps', [])]
        dropped = (u.get('dropped') or {}).get('reads')
        if dropped:
            gaps.append('%d 次读取对不上现存 skill，已丢弃' % dropped)
        rows.append('<div><b>%s</b>：%s；%s%s</div>' % (AGENT[a], src, e(use), '；缺口：' + e('、'.join(gaps)) if gaps else ''))
    return ('<header><h1>skill 体检</h1><div class="meta">生成于 %s · 当前 agent：%s · 范围：%s</div></header>'
            '<div class="overview">%s</div>'
            '<div class="banner">这是只读体检：页面上没有执行入口，改选只影响下面"如果执行会做哪几步"的预览。</div>'
            % (e(_when(plan['generated_at'])), AGENT[plan['current']], scope, ''.join(rows)))


def _uses(x):
    if x['uses'] is not None:
        return '%d 次' % x['uses']
    return {'ambiguous_name': '无法确定（同名调用）', 'no_logs': '没有日志'}.get(x['uses_note'], '—')


def _source(x, allowed_locks):
    out, src = [], sanitize_source(x['source'])
    if x['plugin']:
        out.append('插件 %s %s' % (x['plugin']['id'], x['plugin'].get('version') or ''))
    if src['git'] and src['git'].get('remote'):
        out.append('git：%s' % src['git']['remote'])
    if src['clawhub']:
        c = src['clawhub']
        out.append('ClawHub：%s %s（发布者 %s）' % (c.get('slug'), c.get('version') or '', c.get('ownerId')))
    for lock in src['locks']:
        if allowed_locks is not None and lock['file'] not in allowed_locks:
            continue  # 已有单端 plan.json 可能带有另一端的锁记录
        r = lock['record'] or {}
        out.append('安装记录：%s' % (r.get('sourceUrl') or r.get('zip_url') or r.get('source') or os.path.basename(lock['file'])))
    for k, v in sorted(src['frontmatter'].items()):
        out.append('%s：%s' % (k, v))
    return '；'.join(out) or '没有来源记录'


def _members(plan, g, pick_label, evidence=True):
    E = plan['entries']
    allowed_locks = None
    if plan['scope'] == 'current':
        allowed_locks = lock_paths({x['root'] for x in E.values() if x['agent'] == plan['current'] and x['root']})
    head = '<tr><th></th><th>%s</th><th>入口</th><th>位置</th>%s<th>来源</th><th>使用次数</th><th>状态</th></tr>' % (
        pick_label, '<th>来源证据</th>' if evidence else '')
    rows = []
    for mid in g['members']:
        x = E[mid]
        pick = ''
        if g['options']:
            pick = '<input type="radio" name="keep-%s" value="%s"%s>' % (
                e(g['id']), e(mid), ' checked' if mid == g['default_keep'] else '')
        rows.append('<tr><td class="pick">%s</td><td class="nowrap">%s</td><td class="path">%s</td><td class="nowrap">%s</td>%s'
                    '<td class="small">%s</td><td class="nowrap">%s</td><td class="status">%s</td></tr>' % (
                        pick, e(AGENT[x['agent']]), code(x['path']), e(ENTRY_KIND.get(x['kind'], x['kind'])),
                        '<td class="nowrap">%s</td>' % e(EVIDENCE[g['evidence'][mid]]) if evidence else '',
                        e(_source(x, allowed_locks)), e(_uses(x)), e(STATUS.get(x['status'], x['status']))))
    return '<table>%s%s</table>' % (head, ''.join(rows))


def _diff(plan, g):
    d = g.get('diff')
    if not d or g['content'] != 'different':
        return ''
    E = plan['entries']
    parts = []
    for key, other in (('only_a', 'a'), ('only_b', 'b')):
        if d[key]:
            parts.append('只在 %s 里有：%s' % (code(E[d[other]]['path']), '、'.join(code(p) for p in d[key])))
    if d['changed']:
        parts.append('两边都有但内容不同：%s' % '、'.join(code(p) for p in d['changed']))
    return '<div class="diff">%s</div>' % '<br>'.join(parts) if parts else ''


def _card(g, tags, body):
    level = g['level']
    opt = g['options'].get(g['default_keep']) if g['default_keep'] else None
    savings = '约省 %d 字符' % opt['savings'] if opt and opt['savings'] else ''
    return ('<details class="item %s" data-group="%s" data-name="%s" open><summary><span class="item-name">%s</span>%s'
            '<span class="lvl">%s</span><span class="savings">%s</span></summary><div class="item-body">%s</div></details>'
            % (level, e(g['id']), e(g['name']), e(g['name']), ''.join(tags), LEVEL[level], savings, body))


def _process(g):
    return ('<div class="field"><label><input type="checkbox" class="process" data-group="%s"%s> 处理这一组</label></div>'
            % (e(g['id']), ' checked' if g['process_default'] else ''))


def _dedup(plan):
    cards = []
    for g in plan['dedup']:
        E = plan['entries']
        tags = ['<span class="tag">%s</span>' % e(AGENT[g['agent']]),
                '<span class="tag%s">%s</span>' % (' bad' if g['kind'] == 'package_variant' else '', e(KIND[g['kind']])),
                '<span class="tag%s">%s</span>' % (' ok' if g['content'] == 'identical' else ' warn', e(CONTENT[g['content']]))]
        if g['default_keep']:
            suggest = '建议留 %s。理由：%s' % (code(E[g['default_keep']]['path']), e(KEEP_REASON.get(g['keep_reason'])))
        else:
            suggest = '还没定留哪一个：%s' % e(KEEP_REASON.get(g['keep_reason']))
        body = _process(g) + '<div class="suggest">%s</div>' % suggest
        if g['kind'] == 'trigger_conflict':
            can = ''
            if g['default_keep']:  # 没定留哪一份时，不说它能不能独立完成
                can = '保留的那一个%s独立完成它。' % ('能' if g.get('keep_can_do_it') else '不能确定能否')
            body += '<div class="field">会同时命中的请求：「%s」。%s%s</div>' % (
                e(g['example']), can, e(g.get('judgment_reason') or ''))
        cards.append(_card(g, tags, body + _members(plan, g, '留这一份') + _diff(plan, g)))
    return ''.join(cards) or '<div class="empty-sec">没有发现重复。</div>'


def _consolidate(plan):
    cards = []
    for g in plan['consolidate']:
        if g['kind'] == 'shared':
            tags = ['<span class="tag ok">已共享</span>']
            body = '<div class="suggest">两边的入口已经指向同一个实体，不用处理。</div>' + _members(plan, g, '', evidence=False)
            cards.append(_card(g, tags, body))
            continue
        tags = ['<span class="tag%s">%s</span>' % (' ok' if g['content'] == 'identical' else ' warn', e(CONTENT[g['content']]))]
        body = _process(g)
        if g['portability'] and not g['portable']:
            tags.append('<span class="tag bad">不通用</span>')
            body += '<div class="risk">不通用：%s。脚本标出来的，等 agent 确认；确认之前留在原处。</div>' % '、'.join(
                e(PORTABILITY.get(f, f)) for f in g['portability'])
        elif g['portability']:
            body += '<div class="suggest">脚本标出过 %s，agent 已确认通用。</div>' % '、'.join(
                e(PORTABILITY.get(f, f)) for f in g['portability'])
        if not g['default_keep']:
            body += '<div class="suggest">内容不同，先选以哪份为准，再收拢。</div>'
        cards.append(_card(g, tags, body + _members(plan, g, '以这份为准', evidence=False) + _diff(plan, g)))
    if not cards:
        if plan['scope'] == 'current':
            return '<div class="empty-sec">只整理当前 agent 时不做收拢。</div>'
        return '<div class="empty-sec">没有可以收拢的 skill。</div>'
    return ''.join(cards)


def _diag_text(plan, d):
    E = plan['entries']
    t = d['type']
    x = E.get(d.get('entry')) if d.get('entry') else None
    if t == 'dead_link':
        return '死链接：%s → %s' % (code(d['path']), code(d['target']))
    if t == 'loop':
        return '循环链接：%s → %s' % (code(d['path']), code(d.get('target')))
    if t == 'no_skill_md':
        return '目录里没有 SKILL.md：%s' % code(d['path'])
    if t == 'stale_lock':
        return '残留记录：%s 里记着 %s，磁盘上已经没有了' % (code(d['path']), e(d['name']))
    if t == 'unreadable':
        return '读不了：%s（%s）' % (code(d['path']), e(d.get('error')))
    if t == 'skipped':
        reason = REASON.get(d['reason'], d['reason'])
        if d['reason'] in ('body_in_frontmatter', 'unclosed') and x.get('reason_line'):
            reason += '，第 %d 行起是正文' % x['reason_line']
        return '被跳过：%s（%s，%s%s）' % (code(x['path']), AGENT[x['agent']], e(reason),
                                       '，内置' if x['kind'] == 'builtin' else '')
    if t == 'installed_after':
        return '装在最近一次会话之后，还没加载过：%s（%s）' % (code(x['path']), AGENT[x['agent']])
    if t == 'unlocated':
        return '未定位：%s 清单里的 %s 对应多个入口：%s' % (AGENT[d['agent']], e(d['name']), '、'.join(
            code(E[i]['path']) for i in d['entries']))
    if t == 'listing_unknown':
        return '%s 加载情况未知：取不到它的加载清单，只给当前 agent 的去重建议。' % AGENT[d['agent']]
    if t == 'listed_missing':
        return '%s 清单里有、磁盘上找不到：%s %s' % (AGENT[d['agent']], e(d['name']), code(d['path']))
    if t == 'format_hint':
        return '格式提示（已加载，不算坏）：%s：%s' % (code(x['path']), e('、'.join(HINTS.get(h, h) for h in d['hints'])))
    return e(json.dumps(d, ensure_ascii=False))


DIAG_ORDER = ['listing_unknown', 'skipped', 'installed_after', 'unlocated', 'listed_missing', 'dead_link', 'loop',
              'unreadable', 'no_skill_md', 'format_hint', 'stale_lock']


def _rank(d):
    return DIAG_ORDER.index(d['type']) if d['type'] in DIAG_ORDER else len(DIAG_ORDER)


def _diagnostics(plan):
    items, stale = [], {}
    for d in sorted(plan['diagnostics'], key=_rank):
        if d['type'] == 'stale_lock':  # 同一个锁文件里的残留记录合成一条
            stale.setdefault(d['path'], []).append(d['name'])
        else:
            items.append('<li class="diag" data-type="%s">%s</li>' % (e(d['type']), _diag_text(plan, d)))
    for path, names in stale.items():
        items.append('<li class="diag" data-type="stale_lock">残留记录：%s 里记着 %d 个 skill，磁盘上已经没有了：%s</li>'
                     % (code(path), len(names), e('、'.join(names))))
    for a, names in sorted(plan['builtin'].items()):
        if names:
            items.append('<li class="diag" data-type="builtin">%s 清单里有 %d 个 skill 在磁盘上没找到对应文件，'
                         '多半是内置的（也可能在这次没扫的项目目录里）：%s</li>' % (AGENT[a], len(names), e('、'.join(names))))
    return '<ul class="diags">%s</ul>' % ''.join(items) if items else '<div class="empty-sec">没有发现问题。</div>'


def render(plan):
    with open(TEMPLATE, encoding='utf-8') as fh:
        page = fh.read()
    # 页面脚本只用到这几项；入口、来源、诊断、日志路径都不嵌进页面
    data = copy.deepcopy({'dedup': plan['dedup'], 'consolidate': plan['consolidate']})
    for g in data['dedup'] + data['consolidate']:
        for opt in g['options'].values():
            for st in opt['steps']:
                st['text'] = step_text(st)
    data['texts'] = {'level': LEVEL}
    blob = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')  # 不让数据提前结束 <script>
    parts = {'<!--HEADER-->': _header(plan), '<!--DEDUP-->': _dedup(plan), '<!--CONSOLIDATE-->': _consolidate(plan),
             '<!--DIAGNOSTICS-->': _diagnostics(plan), '__PLAN_JSON__': blob}
    # 一次替换完：填进去的内容里即使写着占位符，也不会再被替换
    return re.sub('|'.join(re.escape(k) for k in parts), lambda m: parts[m.group(0)], page)


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成 skill-tidy 的体检页面。')
    ap.add_argument('--static', action='store_true', help='生成静态页面（第一阶段只有这个）')
    ap.add_argument('--plan', required=True, help='plan.py 生成的 plan.json')
    ap.add_argument('--out', help='页面写到哪里，默认和 plan.json 放在一起')
    ap.add_argument('--no-open', action='store_true', help='生成后不自动打开浏览器')
    a = ap.parse_args(argv)
    if not a.static:
        ap.error('第一阶段只能用 --static 生成静态页面；本地服务是第二阶段的内容')
    with open(a.plan, encoding='utf-8') as fh:
        plan = json.load(fh)
    out = os.path.abspath(a.out or os.path.join(os.path.dirname(os.path.abspath(a.plan)), 'skill-tidy.html'))
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(render(plan))
    print('页面已生成：%s' % out)
    if not a.no_open:
        webbrowser.open('file://' + out)


if __name__ == '__main__':
    main()
