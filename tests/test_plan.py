"""plan.py：把 scan.json、usage.json 和 agent 的判断合成 plan.json，并按用户的选择重算步骤。

见 DESIGN.md 第 2、5、6、7、9、10 节。测试走真实的 usage -> scan -> plan 流程。
"""
import json
import os
import time
import unittest

from logs import LogHome, c_listing, c_skill, x_exec, x_listing, x_listing_text, x_meta
from util import link, skill, write
import plan
import scan
import usage


def iso(offset):
    """相对现在的 UTC 时间。会话默认在造好文件之后才开始，这样造的 skill 都算"会话之前装的"。"""
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() + offset))


LATER, EARLIER = 3600, -3600


class PlanCase(LogHome):
    project = None

    def roots(self):
        return [('r0', self.h('.codex/skills')), ('r1', self.h('.agents/skills')),
                ('r2', self.h('.codex/skills/.system')), ('r3', self.h('.codex/plugins/cache'))]

    def codex_items(self, items):
        """[(名字, 描述, SKILL.md 绝对路径)] -> 清单里的 (名字, 描述, rN/相对路径)。"""
        out = []
        for name, desc, path in items:
            key, root = max(((k, r) for k, r in self.roots() if path.startswith(r + '/')), key=lambda kr: len(kr[1]))
            out.append((name, desc, '%s/%s' % (key, path[len(root) + 1:])))
        return out

    def claude_session(self, names, offset=LATER, extra=()):
        self.claude_log('-work/s.jsonl', [c_listing(names, iso(offset))] + list(extra))

    def codex_session(self, items, offset=LATER, extra=()):
        start = time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime(time.time() + offset))
        self.codex_log(start, [x_meta(iso(offset)), x_listing(self.roots(), self.codex_items(items))] + list(extra))

    def codex_text(self, items):
        return x_listing_text(self.roots(), self.codex_items(items))

    def make(self, current, scope, listing_text, judgments=None):
        u = usage.collect(self.home, current, scope, listing_text)
        s = scan.scan(self.home, project=self.project, listings=u['listings'])
        return plan.build(s, u, judgments)


def entry(p, path):
    return next(e for e in p['entries'].values() if e['path'] == path)


def group(groups, name):
    [g] = [g for g in groups if g['name'] == name]
    return g


def steps(option):
    return [(s['type'], s.get('path'), s.get('target') or s.get('name')) for s in option['steps']]


def md(path):
    return os.path.join(path, 'SKILL.md')


class LoadStatusTest(PlanCase):
    def test_claude_entries_against_the_listing(self):
        write(md(self.h('.claude/skills/alpha')), skill('alpha', 'Alpha.'))
        write(md(self.h('.claude/skills/broken')), '# broken\n\nno frontmatter\n')
        write(md(self.h('.claude/skills/manual')), skill('manual', 'Only by /manual.', 'disable-model-invocation: true\n'))
        self.claude_session(['alpha', 'simplify'])
        p = self.make('claude', 'current', '- alpha: Alpha.\n- simplify: Review changed code.')
        self.assertEqual(entry(p, self.h('.claude/skills/alpha'))['status'], 'loaded')
        broken = entry(p, self.h('.claude/skills/broken'))
        self.assertEqual((broken['status'], broken['reason']), ('skipped', 'no_frontmatter'))  # 验收：被跳过
        self.assertEqual(entry(p, self.h('.claude/skills/manual'))['status'], 'manual_only')
        self.assertEqual(p['builtin']['claude'], ['simplify'])
        self.assertIn({'type': 'skipped', 'entry': broken['id'], 'reason': 'no_frontmatter'}, p['diagnostics'])

    def test_skipped_skill_keeps_the_line_where_body_starts(self):
        write(md(self.h('.claude/skills/drive')), '---\nname: drive\ndescription: Files.\n\n# 快速启动\n为避免……\n---\n')
        self.claude_session([])
        x = entry(self.make('claude', 'current', ''), self.h('.claude/skills/drive'))
        self.assertEqual((x['status'], x['reason'], x['reason_line']), ('skipped', 'body_in_frontmatter', 5))

    def test_skill_installed_after_the_session_is_not_called_skipped(self):
        self.claude_session(['alpha'], offset=EARLIER)
        write(md(self.h('.claude/skills/alpha')), skill('alpha', 'Alpha.'))
        write(md(self.h('.claude/skills/fresh')), skill('fresh', 'Fresh.'))
        p = self.make('claude', 'current', '- alpha: Alpha.')
        self.assertEqual(entry(p, self.h('.claude/skills/fresh'))['status'], 'installed_after')

    def test_one_name_in_two_places_is_unlocated(self):
        self.project = os.path.join(self.tmp, 'proj')
        write(md(self.h('.claude/skills/same')), skill('same', 'Mine.'))
        write(md(self.project + '/.claude/skills/same'), skill('same', 'Team.'))
        self.claude_session(['same'])
        p = self.make('claude', 'current', '- same: Mine.')
        a, b = entry(p, self.h('.claude/skills/same')), entry(p, self.project + '/.claude/skills/same')
        self.assertEqual((a['status'], b['status']), ('unlocated', 'unlocated'))
        self.assertIn({'type': 'unlocated', 'agent': 'claude', 'name': 'same', 'entries': sorted([a['id'], b['id']])},
                      p['diagnostics'])
        self.assertEqual(p['dedup'], [])

    def test_codex_lists_one_path_per_entity(self):
        write(md(self.h('.agents/skills/grill-me')), skill('grill-me', 'Interview.'))
        link('../../.agents/skills/grill-me', self.h('.codex/skills/grill-me'))
        gone = md(self.h('.codex/skills/removed'))
        items = [('grill-me', 'Interview.', md(self.h('.codex/skills/grill-me'))), ('removed', 'Gone.', gone)]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        self.assertEqual(entry(p, self.h('.codex/skills/grill-me'))['status'], 'loaded')
        self.assertEqual(entry(p, self.h('.agents/skills/grill-me'))['status'], 'duplicate_path')
        self.assertIn({'type': 'listed_missing', 'agent': 'codex', 'name': 'removed', 'path': gone}, p['diagnostics'])

    def test_other_side_without_listing_is_unknown(self):
        # 验收：另一边没有日志时，照样生成当前 agent 的报告
        write(md(self.h('.claude/skills/tma')), skill('tma', 'Applications.'))
        write(md(self.h('.codex/skills/tma')), skill('tma', 'Applications.'))
        self.claude_session(['tma'])
        p = self.make('claude', 'all', '- tma: Applications.')
        self.assertEqual(entry(p, self.h('.claude/skills/tma'))['status'], 'loaded')
        self.assertEqual(entry(p, self.h('.codex/skills/tma'))['status'], 'unknown')
        self.assertIn({'type': 'listing_unknown', 'agent': 'codex'}, p['diagnostics'])
        self.assertEqual(p['consolidate'], [])

    def test_disabled_plugin_is_not_called_skipped(self):
        install = self.h('.claude/plugins/cache/m/sp/1.0')
        write(md(install + '/skills/brainstorming'), skill('brainstorming', 'Explore.'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps(
            {'version': 2, 'plugins': {'sp@m': [{'installPath': install, 'version': '1.0'}]}}))
        write(self.h('.claude/settings.json'), json.dumps({'enabledPlugins': {'sp@m': False}}))
        self.claude_session([])
        p = self.make('claude', 'current', '')
        self.assertEqual(entry(p, install + '/skills/brainstorming')['status'], 'plugin_disabled')


class DedupTest(PlanCase):
    def test_nested_same_name_skill_is_a_package_variant(self):
        # 验收：一个目录里套着另一个同名 skill -> 包内变体
        top = self.h('.codex/skills/last30days')
        write(md(top), skill('last30days', 'Deep research.'))
        write(md(top + '/variants/open'), skill('last30days', 'Research topics.'))
        items = [('last30days:last30days', 'Deep research.', md(top)),
                 ('last30days:last30days', 'Research topics.', md(top + '/variants/open'))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'last30days')
        self.assertEqual((g['kind'], g['level'], g['process_default']), ('package_variant', 'red', False))
        variant = entry(p, top + '/variants/open')['id']
        self.assertIn(top, g['options'][variant]['blocked'])  # 留变体时，不能移走外层目录

    def test_plugin_copy_is_kept(self):
        mine = self.h('.codex/skills/pdf')
        write(md(mine), skill('pdf', 'Use when tasks involve PDFs.'))
        plugin_md = write(md(self.h('.codex/plugins/cache/primary/pdf/26.1/skills/pdf')), skill('pdf', 'Read PDFs.'))
        items = [('pdf', 'Use when tasks involve PDFs.', md(mine)), ('pdf:pdf', 'Read PDFs.', plugin_md)]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'pdf')
        self.assertEqual(g['default_keep'], entry(p, os.path.dirname(plugin_md))['id'])
        self.assertEqual(steps(g['options'][g['default_keep']]), [('trash', mine, None)])
        self.assertEqual((g['content'], g['level']), ('different', 'yellow'))

    def test_documented_copy_beats_suspected_copy(self):
        clawhub = self.h('.agents/skills/brainstorming-0.1.0')
        write(md(clawhub), skill('brainstorming', 'Explore intent first.'))
        write(clawhub + '/_meta.json', json.dumps({'ownerId': 'kn7', 'slug': 'brainstorming', 'version': '0.1.0'}))
        repo = self.h('.codex/superpowers')
        write(repo + '/.git/config', '[remote "origin"]\n\turl = https://github.com/obra/superpowers.git\n')
        write(md(repo + '/skills/brainstorming'), skill('brainstorming', 'Explore intent first.') + '\nMuch longer body.\n' * 20)
        link(repo + '/skills', self.h('.agents/skills/superpowers'))
        via = self.h('.agents/skills/superpowers/brainstorming')
        items = [('brainstorming', 'Explore intent first.', md(clawhub)), ('brainstorming', 'Explore intent first.', md(via))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'brainstorming')
        keep, other = entry(p, via)['id'], entry(p, clawhub)['id']
        self.assertEqual(g['default_keep'], keep)
        self.assertEqual(g['evidence'], {keep: 'documented', other: 'suspected_copy'})
        self.assertEqual(g['level'], 'yellow')
        # 只整理当前 agent：共享目录里的不动文件，只在 Codex 配置里按路径关闭
        self.assertEqual(steps(g['options'][keep]), [('disable', md(clawhub), None)])

    def test_documented_beats_unknown_and_identical_copies_are_green(self):
        text = skill('tool', 'A tool.')
        write(md(self.h('.codex/skills/tool')), text)
        write(md(self.h('.agents/skills/tool-1.0')), text)
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {
            'tool-1.0': {'source': 'a/tools', 'sourceUrl': 'https://github.com/a/tools.git'}}}))
        items = [('tool', 'A tool.', md(self.h('.codex/skills/tool'))), ('tool', 'A tool.', md(self.h('.agents/skills/tool-1.0')))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'tool')
        self.assertEqual(g['default_keep'], entry(p, self.h('.agents/skills/tool-1.0'))['id'])
        self.assertEqual((g['content'], g['level'], g['process_default']), ('identical', 'green', True))
        self.assertEqual(steps(g['options'][g['default_keep']]), [('trash', self.h('.codex/skills/tool'), None)])

    def test_copy_another_agent_links_to_is_disabled_not_trashed(self):
        # 只整理 Codex 时也要看 Claude Code 的入口：它链到这份实体，移走会让那边失效
        text = skill('tool', 'A tool.')
        write(md(self.h('.codex/skills/tool')), text)
        write(md(self.h('.agents/skills/tool-1.0')), text)
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {
            'tool-1.0': {'source': 'a/tools', 'sourceUrl': 'https://github.com/a/tools.git'}}}))
        link('../../.codex/skills/tool', self.h('.claude/skills/tool'))
        items = [('tool', 'A tool.', md(self.h('.codex/skills/tool'))), ('tool', 'A tool.', md(self.h('.agents/skills/tool-1.0')))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'tool')
        self.assertEqual(steps(g['options'][g['default_keep']]), [('disable', md(self.h('.codex/skills/tool')), None)])
        self.assertEqual({x['agent'] for x in p['entries'].values()}, {'codex'})  # 那个链接只用来保护，不写进 plan.json

    def test_unknown_sources_are_left_to_the_user(self):
        write(md(self.h('.codex/skills/a')), skill('dup', 'One.'))
        write(md(self.h('.agents/skills/b')), skill('dup', 'Two.'))
        items = [('dup', 'One.', md(self.h('.codex/skills/a'))), ('dup', 'Two.', md(self.h('.agents/skills/b')))]
        self.codex_session(items)
        g = group(self.make('codex', 'current', self.codex_text(items))['dedup'], 'dup')
        self.assertEqual((g['default_keep'], g['level'], g['process_default']), (None, 'yellow', False))

    def test_equal_evidence_goes_to_the_newest(self):
        old, new = self.h('.codex/skills/old'), self.h('.codex/skills/new')
        write(md(old), skill('twin', 'Old.'))
        write(md(new), skill('twin', 'New.'))
        os.utime(md(old), (1000000000, 1000000000))
        write(self.h('.codex/skills/.skills_store_lock.json'), json.dumps({'version': 1, 'skills': {
            'old': {'zip_url': 'https://x/old.zip'}, 'new': {'zip_url': 'https://x/new.zip'}}}))
        items = [('twin', 'Old.', md(old)), ('twin', 'New.', md(new))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        self.assertEqual(group(p['dedup'], 'twin')['default_keep'], entry(p, new)['id'])

    def test_linked_directory_entries_are_disabled_not_moved(self):
        # 经软链接目录进来的入口，移走它会动到链接另一头的文件
        write(md(self.h('.codex/skills/doc')), skill('doc', 'Docs.'))
        write(self.h('.codex/skills/.skills_store_lock.json'), json.dumps({'version': 1, 'skills': {'doc': {'zip_url': 'https://x'}}}))
        write(md(self.h('stuff/pack/doc')), skill('doc', 'Other docs.'))
        link(self.h('stuff/pack'), self.h('.agents/skills/pack'))
        items = [('doc', 'Docs.', md(self.h('.codex/skills/doc'))), ('doc', 'Other docs.', md(self.h('.agents/skills/pack/doc')))]
        self.codex_session(items)
        self.claude_session([])
        p = self.make('codex', 'all', self.codex_text(items))
        g = group(p['dedup'], 'doc')
        self.assertEqual(steps(g['options'][g['default_keep']]), [('disable', md(self.h('.agents/skills/pack/doc')), None)])

    def test_claude_name_switch_that_would_hit_a_kept_skill_is_impossible_for_project_skills(self):
        self.project = os.path.join(self.tmp, 'proj')
        team = self.project + '/.claude/skills/x'
        write(md(team), skill('x', 'Team copy.'))
        install = self.h('.claude/plugins/cache/m/sp/1.0')
        write(md(install + '/skills/x'), skill('x', 'Plugin copy.'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps(
            {'version': 2, 'plugins': {'sp@m': [{'installPath': install, 'version': '1.0'}]}}))
        self.claude_session(['x', 'sp:x'])
        p = self.make('claude', 'current', '- x: Team copy.\n- sp:x: Plugin copy.')
        g = group(p['dedup'], 'x')
        self.assertEqual(g['default_keep'], entry(p, install + '/skills/x')['id'])  # 插件不进删除候选
        [st] = g['options'][g['default_keep']]['steps']
        self.assertEqual((st['type'], st['path']), ('impossible', team))  # 项目目录不移动，按名字关闭又会关掉插件那份
        self.assertEqual(g['level'], 'red')


class ConsolidateTest(PlanCase):
    def both_sides(self, names, codex_items):
        self.claude_session(names)
        self.codex_session(codex_items)
        return self.make('claude', 'all', ''.join('- %s: x\n' % n for n in names))

    def test_links_to_one_entity_are_already_shared(self):
        # 验收：两个入口指向同一个实体 -> 已共享
        write(md(self.h('.agents/skills/grilling')), skill('grilling', 'Grill.'))
        link('../../.agents/skills/grilling', self.h('.claude/skills/grilling'))
        p = self.both_sides(['grilling'], [('grilling', 'Grill.', md(self.h('.agents/skills/grilling')))])
        g = group(p['consolidate'], 'grilling')
        self.assertEqual((g['kind'], g['level'], g['options']), ('shared', 'green', {}))

    def test_shared_group_shows_the_entry_codex_loaded(self):
        write(md(self.h('.agents/skills/grill-me')), skill('grill-me', 'Interview.'))
        link('../../.agents/skills/grill-me', self.h('.codex/skills/grill-me'))
        link('../../.agents/skills/grill-me', self.h('.claude/skills/grill-me'))
        p = self.both_sides(['grill-me'], [('grill-me', 'Interview.', md(self.h('.codex/skills/grill-me')))])
        g = group(p['consolidate'], 'grill-me')
        self.assertEqual(sorted(p['entries'][m]['path'] for m in g['members']),
                         [self.h('.claude/skills/grill-me'), self.h('.codex/skills/grill-me')])

    def test_extra_directories_are_summarised_at_the_top_level(self):
        text = skill('cam', 'Remove watermarks.')
        claude, codex = self.h('.claude/skills/cam'), self.h('.codex/skills/cam')
        write(md(claude), text)
        write(md(codex), text + 'Codex notes.\n')
        write(codex + '/scripts/remove.py', 'x')
        write(codex + '/scripts/lib/util.py', 'x')
        write(codex + '/agents/openai.yaml', 'x')
        p = self.both_sides(['cam'], [('cam', 'Remove watermarks.', md(codex))])
        d = group(p['consolidate'], 'cam')['diff']
        self.assertEqual((p['entries'][d['b']]['path'], d['only_b'], d['changed']), (codex, ['agents/', 'scripts/'], ['SKILL.md']))

    def test_identical_copies_move_to_the_shared_dir_and_get_linked(self):
        text = skill('tma', 'Applications.')
        write(md(self.h('.claude/skills/tma')), text)
        write(md(self.h('.codex/skills/tma')), text)
        p = self.both_sides(['tma'], [('tma', 'Applications.', md(self.h('.codex/skills/tma')))])
        g = group(p['consolidate'], 'tma')
        self.assertEqual((g['content'], g['level'], g['process_default']), ('identical', 'green', True))
        self.assertEqual(g['default_keep'], entry(p, self.h('.codex/skills/tma'))['id'])
        opt = g['options'][g['default_keep']]
        self.assertEqual(steps(opt), [('move', self.h('.codex/skills/tma'), self.h('.agents/skills/tma')),
                                      ('link', self.h('.claude/skills/tma'), '../../.agents/skills/tma')])
        self.assertEqual(opt['savings'], 0)

    def test_copy_already_in_the_shared_dir_only_needs_the_link(self):
        text = skill('agent-reach', 'Read platforms.')
        write(md(self.h('.claude/skills/agent-reach')), text)
        write(md(self.h('.agents/skills/agent-reach')), text)
        p = self.both_sides(['agent-reach'], [('agent-reach', 'Read platforms.', md(self.h('.agents/skills/agent-reach')))])
        g = group(p['consolidate'], 'agent-reach')
        self.assertEqual(steps(g['options'][g['default_keep']]),
                         [('link', self.h('.claude/skills/agent-reach'), '../../.agents/skills/agent-reach')])

    def test_same_skill_md_with_different_scripts_is_different(self):
        # 验收：两份 SKILL.md 相同但脚本不同 -> 内容不同
        text = skill('cam', 'Remove watermarks.')
        claude, codex = self.h('.claude/skills/cam'), self.h('.codex/skills/cam')
        write(md(claude), text)
        write(claude + '/scripts/run.py', 'print(1)\n')
        write(md(codex), text)
        write(codex + '/scripts/run.py', 'print(2)\n')
        p = self.both_sides(['cam'], [('cam', 'Remove watermarks.', md(codex))])
        g = group(p['consolidate'], 'cam')
        self.assertEqual((g['content'], g['default_keep'], g['process_default'], g['level']), ('different', None, False, 'yellow'))
        self.assertEqual(steps(g['options'][entry(p, claude)['id']]),
                         [('move', claude, self.h('.agents/skills/cam')), ('link', claude, '../../.agents/skills/cam'),
                          ('trash', codex, None)])

    def test_agent_specific_skill_stays_put_until_the_agent_confirms(self):
        text = skill('pwf', 'Plan with files.') + 'Run ${CLAUDE_PLUGIN_ROOT}/scripts/init.sh\n'
        write(md(self.h('.claude/skills/pwf')), text)
        write(md(self.h('.agents/skills/pwf')), text)
        self.claude_session(['pwf'])
        self.codex_session([('pwf', 'Plan with files.', md(self.h('.agents/skills/pwf')))])
        p = self.make('claude', 'all', '- pwf: x')
        g = group(p['consolidate'], 'pwf')
        self.assertEqual((g['level'], g['process_default']), ('red', False))
        hits = entry(p, self.h('.claude/skills/pwf'))['portability_hits']  # agent 确认时要看的那几行
        self.assertEqual([(h['kind'], h['file'], h['line']) for h in hits], [('claude_plugin_root', 'SKILL.md', 7)])
        confirmed = self.make('claude', 'all', '- pwf: x', {'portable': {'pwf': True}})
        g = group(confirmed['consolidate'], 'pwf')
        self.assertEqual((g['level'], g['process_default']), ('green', True))

    def test_target_taken_by_a_third_directory_blocks(self):
        text = skill('cam', 'Remove watermarks.')
        claude, codex = self.h('.claude/skills/cam'), self.h('.codex/skills/cam')
        write(md(claude), text)
        write(md(codex), text + 'different\n')
        write(self.h('.agents/skills/cam/notes.txt'), 'not a skill')
        p = self.both_sides(['cam'], [('cam', 'Remove watermarks.', md(codex))])
        g = group(p['consolidate'], 'cam')
        self.assertIn(self.h('.agents/skills/cam'), g['options'][entry(p, claude)['id']]['blocked'])
        # 不可选的方案只报冲突，不列它的步骤
        r = plan.compute_steps(p, {g['id']: {'process': True, 'keep': entry(p, claude)['id']}})
        self.assertEqual((r['steps'], [c['type'] for c in r['conflicts']]), ([], ['blocked']))

    def test_identical_copies_prefer_a_target_that_is_free(self):
        text = skill('tma', 'Applications.')
        write(md(self.h('.claude/skills/tma-claude')), text)
        write(md(self.h('.codex/skills/tma')), text)
        write(self.h('.agents/skills/tma/notes.txt'), 'taken')
        p = self.both_sides(['tma'], [('tma', 'Applications.', md(self.h('.codex/skills/tma')))])
        g = group(p['consolidate'], 'tma')
        self.assertEqual((g['default_keep'], g['process_default']), (entry(p, self.h('.claude/skills/tma-claude'))['id'], True))


class StepsTest(PlanCase):
    def setUp(self):
        super().setUp()
        text = skill('tma', 'Applications.')
        write(md(self.h('.claude/skills/tma')), text)
        write(md(self.h('.codex/skills/tma')), text)
        write(md(self.h('.agents/skills/tma-1.0')), skill('tma', 'Applications, newer.'))
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {'tma-1.0': {'sourceUrl': 'https://g/t.git'}}}))
        self.claude_session(['tma'])
        self.codex_session([('tma', 'Applications.', md(self.h('.codex/skills/tma'))),
                            ('tma', 'Applications, newer.', md(self.h('.agents/skills/tma-1.0')))])
        self.plan = self.make('claude', 'all', '- tma: Applications.')
        self.dedup, self.cons = group(self.plan['dedup'], 'tma'), group(self.plan['consolidate'], 'tma')

    def test_all_skipped_means_no_steps(self):
        # 验收：全部跳过时，步骤为空
        r = plan.compute_steps(self.plan, {g['id']: {'process': False, 'keep': g['default_keep']}
                                           for g in self.plan['dedup'] + self.plan['consolidate']})
        self.assertEqual(r, {'steps': [], 'conflicts': []})

    def test_defaults_give_dedup_steps_before_consolidation_steps(self):
        sel = plan.default_selections(self.plan)
        sel[self.cons['id']] = {'process': True, 'keep': entry(self.plan, self.h('.agents/skills/tma-1.0'))['id']}
        r = plan.compute_steps(self.plan, sel)
        self.assertEqual([(s['group'], s['type']) for s in r['steps']],
                         [(self.dedup['id'], 'trash'), (self.cons['id'], 'link')])
        self.assertEqual(r['conflicts'], [])

    def test_consolidating_a_copy_that_dedup_removes_is_a_conflict(self):
        sel = plan.default_selections(self.plan)  # 去重默认留有据可查的 tma-1.0，移走 ~/.codex/skills/tma
        sel[self.cons['id']] = {'process': True, 'keep': entry(self.plan, self.h('.codex/skills/tma'))['id']}
        [c] = plan.compute_steps(self.plan, sel)['conflicts']
        self.assertEqual((c['group'], c['path'], c['other_group']), (self.cons['id'], self.h('.codex/skills/tma'), self.dedup['id']))

    def test_keeping_a_variant_while_removing_its_package_is_a_conflict(self):
        top = self.h('.codex/skills/pack')
        write(md(top), skill('pack', 'Main.'))
        write(md(top + '/variants/open'), skill('pack', 'Open.'))
        items = [('pack:pack', 'Main.', md(top)), ('pack:pack', 'Open.', md(top + '/variants/open'))]
        self.codex_session(items, offset=LATER + 60)
        p = self.make('codex', 'current', self.codex_text(items))
        g = group(p['dedup'], 'pack')
        r = plan.compute_steps(p, {g['id']: {'process': True, 'keep': entry(p, top + '/variants/open')['id']}})
        self.assertEqual([c['group'] for c in r['conflicts']], [g['id']])


class UsageTest(PlanCase):
    def test_counts_and_periods_attach_to_entries(self):
        write(md(self.h('.claude/skills/alpha')), skill('alpha', 'Alpha.'))
        write(md(self.h('.codex/skills/pdf')), skill('pdf', 'PDFs.'))
        self.claude_session(['alpha'], extra=[c_skill('alpha'), c_skill('alpha')])
        self.codex_session([('pdf', 'PDFs.', md(self.h('.codex/skills/pdf')))], extra=[x_exec('cat ~/.codex/skills/pdf/SKILL.md')])
        p = self.make('claude', 'all', '- alpha: Alpha.')
        self.assertEqual(entry(p, self.h('.claude/skills/alpha'))['uses'], 2)
        self.assertEqual(entry(p, self.h('.codex/skills/pdf'))['uses'], 1)
        self.assertEqual(sorted(p['usage']), ['claude', 'codex'])
        self.assertIsNotNone(p['usage']['claude']['period'])

    def test_calls_by_a_name_two_entries_share_are_not_attributed(self):
        self.project = os.path.join(self.tmp, 'proj')
        write(md(self.h('.claude/skills/same')), skill('same', 'Mine.'))
        write(md(self.project + '/.claude/skills/same'), skill('same', 'Team.'))
        self.claude_session(['same'], extra=[c_skill('same')])
        p = self.make('claude', 'current', '- same: Mine.')
        e = entry(p, self.h('.claude/skills/same'))
        self.assertEqual((e['uses'], e['uses_note']), (None, 'ambiguous_name'))


class SavingsTest(PlanCase):
    def test_removing_a_listed_skill_saves_its_listing_line(self):
        mine = self.h('.codex/skills/pdf')
        write(md(mine), skill('pdf', 'Use when tasks involve PDFs.'))
        plugin_md = write(md(self.h('.codex/plugins/cache/primary/pdf/26.1/skills/pdf')), skill('pdf', 'Read PDFs.'))
        items = [('pdf', 'Use when tasks involve PDFs.', md(mine)), ('pdf:pdf', 'Read PDFs.', plugin_md)]
        self.codex_session(items)
        g = group(self.make('codex', 'current', self.codex_text(items))['dedup'], 'pdf')
        self.assertEqual(g['options'][g['default_keep']]['savings'],
                         len('- pdf: Use when tasks involve PDFs. (file: r0/pdf/SKILL.md)'))


class DiagnosticsTest(PlanCase):
    def test_scan_issues_and_format_hints_are_listed(self):
        link('../../global-skills/gone', self.h('.claude/skills/gone'))  # 验收：死链接
        write(md(self.h('.claude/skills/stock')), skill('stock', 'A股分析。Use when: (1) 查询'))
        self.claude_session(['stock'])
        p = self.make('claude', 'current', '- stock: A股分析。Use when: (1) 查询')
        self.assertIn({'type': 'dead_link', 'path': self.h('.claude/skills/gone'), 'target': '../../global-skills/gone'},
                      p['diagnostics'])
        hint = [d for d in p['diagnostics'] if d['type'] == 'format_hint']
        self.assertEqual([(d['entry'], d['hints']) for d in hint],
                         [(entry(p, self.h('.claude/skills/stock'))['id'], ['unquoted_colon'])])

    def test_current_scope_reports_only_that_agent(self):
        # 只整理 Codex 时，plan.json 里不出现 Claude Code 的入口和诊断
        write(md(self.h('.claude/skills/c-only')), skill('c-only', 'Claude only.'))
        link('../../global-skills/gone', self.h('.claude/skills/gone'))
        write(md(self.h('.codex/skills/k')), skill('k', 'K.'))
        link('../../nowhere', self.h('.codex/skills/dead'))
        items = [('k', 'K.', md(self.h('.codex/skills/k')))]
        self.codex_session(items)
        p = self.make('codex', 'current', self.codex_text(items))
        self.assertEqual({x['agent'] for x in p['entries'].values()}, {'codex'})
        self.assertEqual([d['path'] for d in p['diagnostics'] if d['type'] == 'dead_link'], [self.h('.codex/skills/dead')])


class JudgmentsTest(PlanCase):
    def setUp(self):
        super().setUp()
        write(md(self.h('.codex/skills/plans')), skill('plans', 'Write plans.'))
        write(md(self.h('.agents/skills/roadmap')), skill('roadmap', 'Write roadmaps.'))
        self.items = [('plans', 'Write plans.', md(self.h('.codex/skills/plans'))),
                      ('roadmap', 'Write roadmaps.', md(self.h('.agents/skills/roadmap')))]
        self.codex_session(self.items)

    def judged(self, **extra):
        j = {'agent': 'codex', 'members': [self.h('.codex/skills/plans'), self.h('.agents/skills/roadmap')],
             'keep': self.h('.codex/skills/plans'), 'keep_can_do_it': True, 'reason': '两份都写计划'}
        j.update(extra)
        return self.make('codex', 'current', self.codex_text(self.items), {'trigger_conflicts': [j]})

    def test_trigger_conflict_needs_an_example_request(self):
        p = self.judged(example='帮我把这个需求拆成实施计划')
        g = [g for g in p['dedup'] if g['kind'] == 'trigger_conflict'][0]
        self.assertEqual(g['example'], '帮我把这个需求拆成实施计划')
        self.assertEqual(g['default_keep'], entry(p, self.h('.codex/skills/plans'))['id'])
        self.assertEqual(g['level'], 'yellow')

    def test_trigger_conflict_without_example_is_rejected(self):
        p = self.judged(example='')
        self.assertEqual([g for g in p['dedup'] if g['kind'] == 'trigger_conflict'], [])
        self.assertEqual([r['reason'] for r in p['rejected_judgments']], ['no_example'])


if __name__ == '__main__':
    unittest.main()
