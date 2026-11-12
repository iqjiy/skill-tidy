"""第一阶段验收：在临时目录里准备几种情况，页面应显示对应的结论。见 DESIGN.md "验收"。"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

from logs import LogHome, c_listing, x_listing, x_meta
from util import SCRIPTS, link, skill, snapshot, write
import plan
import scan
import serve
import usage

PAGE = os.path.join(os.path.dirname(SCRIPTS), 'assets', 'page.html')


def iso(offset):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() + offset))


def card(page, name):
    m = re.search(r'<details[^>]*data-name="%s".*?</details>' % re.escape(name), page, re.S)
    return m.group(0) if m else ''


def diags(page, kind):
    return re.findall(r'<li class="diag" data-type="%s">(.*?)</li>' % kind, page, re.S)


class AcceptanceHome(LogHome):
    """五种情况放在同一个主目录里，当前是 Claude Code，两边都整理。"""

    def setUp(self):
        super().setUp()
        h = self.h
        # 两个入口指向同一个实体
        write(h('.agents/skills/grilling/SKILL.md'), skill('grilling', 'Grill the plan.'))
        link('../../.agents/skills/grilling', h('.claude/skills/grilling'))
        # 一个死链接
        link('../../global-skills/gone', h('.claude/skills/gone'))
        # 一个不在清单里的 skill
        write(h('.agents/skills/debug-pro-1.0.0/SKILL.md'), '# debug-pro\n\nSystematic debugging.\n')
        # 两份 SKILL.md 相同但脚本不同
        text = skill('cam', 'Remove watermarks.')
        write(h('.claude/skills/cam/SKILL.md'), text)
        write(h('.claude/skills/cam/scripts/run.py'), 'print(1)\n')
        write(h('.codex/skills/cam/SKILL.md'), text)
        write(h('.codex/skills/cam/scripts/run.py'), 'print(2)\n')
        # 一个目录里套着另一个同名 skill
        write(h('.codex/skills/last30days/SKILL.md'), skill('last30days', 'Deep research.'))
        write(h('.codex/skills/last30days/variants/open/SKILL.md'), skill('last30days', 'Research topics.'))
        self.claude_names = ['grilling', 'cam']
        self.claude_log('-work/s.jsonl', [c_listing(self.claude_names, iso(3600))])
        roots = [('r0', h('.codex/skills')), ('r1', h('.agents/skills'))]
        items = [('grilling', 'Grill the plan.', 'r1/grilling/SKILL.md'), ('cam', 'Remove watermarks.', 'r0/cam/SKILL.md'),
                 ('last30days:last30days', 'Deep research.', 'r0/last30days/SKILL.md'),
                 ('last30days:last30days', 'Research topics.', 'r0/last30days/variants/open/SKILL.md')]
        start = time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime(time.time() + 3600))
        self.codex_log(start, [x_meta(iso(3600)), x_listing(roots, items)])
        self.listing_text = ''.join('- %s: x\n' % n for n in self.claude_names)

    def make_plan(self, scope='all'):
        u = usage.collect(self.home, 'claude', scope, self.listing_text)
        s = scan.scan(self.home, project=self.home, listings=u['listings'])
        return plan.build(s, u)


class PageTest(AcceptanceHome):
    def test_page_shows_each_prepared_case(self):
        p = self.make_plan()
        page = serve.render(p)
        self.assertIn('已共享', card(page, 'grilling'))
        self.assertIn('内容不同', card(page, 'cam'))
        self.assertIn('包内变体', card(page, 'last30days'))
        [dead] = diags(page, 'dead_link')
        self.assertIn('死链接', dead)
        self.assertIn(self.h('.claude/skills/gone'), dead)
        [skipped] = diags(page, 'skipped')
        self.assertIn('被跳过', skipped)
        self.assertIn(self.h('.agents/skills/debug-pro-1.0.0'), skipped)
        self.assertIn('没有头部', skipped)

    def test_other_side_without_logs_still_gives_a_report(self):
        shutil.rmtree(self.h('.codex/sessions'))
        page = serve.render(self.make_plan())
        [unknown] = diags(page, 'listing_unknown')
        self.assertIn('加载情况未知', unknown)
        self.assertIn('Codex', unknown)
        self.assertTrue(diags(page, 'dead_link'))  # 当前 agent 的诊断照样有

    def test_important_diagnostics_come_first_and_stale_records_are_grouped(self):
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {'lark-a': {}, 'lark-b': {}}}))
        page = serve.render(self.make_plan())
        types = re.findall(r'<li class="diag" data-type="([a-z_]+)">', page)
        self.assertLess(types.index('skipped'), types.index('stale_lock'))
        self.assertLess(types.index('dead_link'), types.index('stale_lock'))
        [stale] = diags(page, 'stale_lock')
        self.assertIn('lark-a', stale)
        self.assertIn('lark-b', stale)

    def test_trigger_conflict_without_keep_makes_no_claim_about_the_kept_one(self):
        write(self.h('.codex/skills/plans/SKILL.md'), skill('plans', 'Write implementation plans.'))
        write(self.h('.agents/skills/roadmap/SKILL.md'), skill('roadmap', 'Write step-by-step roadmaps.'))
        u = usage.collect(self.home, 'codex', 'current', '\n'.join([
            '### Skill roots', '- `r0` = `%s`' % self.h('.codex/skills'), '- `r1` = `%s`' % self.h('.agents/skills'),
            '### Available skills', '- plans: x (file: r0/plans/SKILL.md)', '- roadmap: y (file: r1/roadmap/SKILL.md)']))
        s = scan.scan(self.home, project=self.home, listings=u['listings'])
        j = {'trigger_conflicts': [{'agent': 'codex', 'members': [self.h('.codex/skills/plans'), self.h('.agents/skills/roadmap')],
                                    'example': '帮我把需求拆成实施计划', 'keep_can_do_it': True}]}
        page = serve.render(plan.build(s, u, j))
        self.assertIn('帮我把需求拆成实施计划', page)
        self.assertFalse('保留的那一个' in page, '没定留哪一份时，不该说它能不能独立完成')

    def test_page_has_no_way_to_execute(self):
        page = serve.render(self.make_plan())
        for needle in ('fetch(', 'XMLHttpRequest', '<form', 'sendBeacon', 'WebSocket'):
            self.assertNotIn(needle, page)

    def test_paths_are_escaped(self):
        write(self.h('.claude/skills/a<b>/SKILL.md'), '# no frontmatter\n')
        page = serve.render(self.make_plan())
        self.assertNotIn('a<b>', page.split('<script')[0])
        self.assertIn('a&lt;b&gt;', page)

    def test_placeholder_in_a_skill_name_stays_text(self):
        # 名字里写上模板的占位符，也只当文字显示：不能把整份数据填进标签，拼出可执行的属性
        name = 'x__PLAN_JSON__y'
        listing = ['### Skill roots', '- `r0` = `%s`' % self.h('.codex/skills'), '- `r1` = `%s`' % self.h('.agents/skills'),
                   '### Available skills']
        for key, root in (('r0', '.codex/skills'), ('r1', '.agents/skills')):
            write(self.h(root, 'evil', 'SKILL.md'), skill(name, 'a" ontoggle="alert(1)'))
            listing.append('- %s: x (file: %s/evil/SKILL.md)' % (name, key))
        u = usage.collect(self.home, 'codex', 'current', '\n'.join(listing))
        page = serve.render(plan.build(scan.scan(self.home, project=self.home, listings=u['listings']), u))
        self.assertIn('data-name="%s"' % name, page)
        self.assertNotIn('ontoggle', page.split('<script id="plan-data"')[0])

    def test_session_time_from_a_log_is_escaped(self):
        start = time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime(time.time() + 7200))
        self.codex_log(start, [x_meta('<b>x</b>'), x_listing([('r1', self.h('.agents/skills'))],
                                                            [('grilling', 'Grill.', 'r1/grilling/SKILL.md')])])
        page = serve.render(self.make_plan())
        self.assertNotIn('<b>x</b>', page)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', page)

    def test_imported_period_and_gaps_are_displayed_as_text(self):
        p = self.make_plan()
        u = p['usage']['claude']
        u['period'] = {'from': '<i>start</i>', 'to': '<b>end</b>'}
        u['gaps'] = [{'kind': '<img src=x>'}, {'kind': 'archives_skipped', 'count': '<b>9</b>'}]
        page = serve.render(p)
        for tag in ('<i>start</i>', '<b>end</b>', '<img src=x>', '<b>9</b>'):
            self.assertNotIn(tag, page)
            self.assertIn(serve.e(tag), page)

    def test_unknown_gap_is_never_a_format_string(self):
        p = self.make_plan()
        p['usage']['claude']['gaps'] = [{'kind': '{unknown}'}]
        self.assertIn('{unknown}', serve.render(p))

    def test_old_scan_and_plan_sources_are_cleaned_on_import(self):
        u = usage.collect(self.home, 'claude', 'all', self.listing_text)
        s = scan.scan(self.home, listings=u['listings'])
        ent = s['entities'][self.h('.claude/skills/cam')]
        legacy = {'homepage': 'https://reader:LEGACY_MARKER@example.test/cam?sig=LEGACY_MARKER'}
        ent['source']['frontmatter'] = legacy
        ent['portability'] = [{'kind': 'claude_tool', 'file': '.env', 'line': 1, 'text': 'LEGACY_MARKER'}]
        p = plan.build(s, u)
        self.assertNotIn('LEGACY_MARKER', json.dumps(p))
        x = next(x for x in p['entries'].values() if x['path'] == self.h('.claude/skills/cam'))
        x['source']['frontmatter'] = legacy
        page = serve.render(p)
        self.assertNotIn('LEGACY_MARKER', page)
        self.assertIn('https://example.test/cam', page)

    def test_page_embeds_only_what_its_script_uses(self):
        # 页面脚本只用到去重、收拢和等级文字；入口、来源、诊断、日志路径都不嵌进页面
        page = serve.render(self.make_plan())
        blob = re.search(r'<script id="plan-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(sorted(json.loads(blob)), ['consolidate', 'dedup', 'texts'])


@unittest.skipUnless(shutil.which('node'), '需要 node 才能跑页面里的 JavaScript')
class PageScriptTest(AcceptanceHome):
    """页面上改选后由 JavaScript 重算步骤，必须和 plan.compute_steps 算得一样。"""

    def run_js(self, p, selections):
        with open(PAGE, encoding='utf-8') as fh:
            src = fh.read()
        js = src[src.index('/* BEGIN computeSteps */'):src.index('/* END computeSteps */')]
        files = []
        for name, obj in (('plan.json', p), ('sel.json', selections)):
            path = os.path.join(self.tmp, name)
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh, ensure_ascii=False)
            files.append(path)
        prog = js + ('\nconst fs = require("fs");'
                     '\nconst r = computeSteps(JSON.parse(fs.readFileSync(process.argv[1], "utf8")),'
                     ' JSON.parse(fs.readFileSync(process.argv[2], "utf8")));'
                     '\nprocess.stdout.write(JSON.stringify(r));')
        out = subprocess.run(['node', '-e', prog] + files, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_script_matches_python_for_default_changed_and_all_skipped(self):
        write(self.h('.agents/skills/cam/notes.txt'), 'taken')  # 让收拢 cam 的方案不可选，也要两边一致
        p = self.make_plan()
        defaults = plan.default_selections(p)
        changed = dict(defaults)
        cam = [g for g in p['consolidate'] if g['name'] == 'cam'][0]
        changed[cam['id']] = {'process': True, 'keep': cam['members'][0]}
        skipped = dict((g, {'process': False, 'keep': s['keep']}) for g, s in defaults.items())
        self.assertIn('blocked', [c['type'] for c in plan.compute_steps(p, changed)['conflicts']])  # 不可选的分支也比到了
        for label, sel in (('defaults', defaults), ('changed', changed), ('all skipped', skipped)):
            with self.subTest(label):
                self.assertEqual(self.run_js(p, sel), plan.compute_steps(p, sel))
        self.assertEqual(self.run_js(p, skipped), {'steps': [], 'conflicts': []})  # 验收：全部跳过时，步骤为空


class ReadOnlyTest(AcceptanceHome):
    def test_whole_pipeline_leaves_home_untouched(self):
        # 验收：生成页面之后，两边的安装目录、配置和回收站都和运行前一样
        write(self.h('.claude/settings.json'), '{"enabledPlugins": {}}\n')
        write(self.h('.codex/config.toml'), '[features]\n')
        out = os.path.join(self.tmp, 'out')
        os.makedirs(out)
        listing = write(os.path.join(out, 'listing.txt'), self.listing_text)
        before = snapshot(self.home)
        py = sys.executable
        commands = [
            [py, os.path.join(SCRIPTS, 'usage.py'), '--home', self.home, '--current', 'claude', '--scope', 'all',
             '--current-listing', listing, '--out', os.path.join(out, 'usage.json')],
            [py, os.path.join(SCRIPTS, 'scan.py'), '--home', self.home, '--project', self.home,
             '--listings', os.path.join(out, 'usage.json'), '--out', os.path.join(out, 'scan.json')],
            [py, os.path.join(SCRIPTS, 'plan.py'), '--scan', os.path.join(out, 'scan.json'),
             '--usage', os.path.join(out, 'usage.json'), '--out', os.path.join(out, 'plan.json')],
            [py, os.path.join(SCRIPTS, 'serve.py'), '--static', '--no-open', '--plan', os.path.join(out, 'plan.json'),
             '--out', os.path.join(out, 'skill-tidy.html')],
        ]
        for cmd in commands:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=self.tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(os.path.exists(self.h('.skill-tidy')))
        self.assertTrue(os.path.isfile(os.path.join(out, 'skill-tidy.html')))

    def test_without_static_flag_nothing_is_served_yet(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'serve.py'), '--plan', 'x.json'],
                              capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('--static', proc.stderr)


if __name__ == '__main__':
    unittest.main()
