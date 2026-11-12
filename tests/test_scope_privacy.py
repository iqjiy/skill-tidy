"""单端扫描的范围校验、另一端引用保护和旧扫描的兼容。"""
import json
import os
import subprocess
import sys

from logs import x_listing_text
from util import TempHome, SCRIPTS, link, skill, write
import plan
import scan
import serve
import usage


class ScopePrivacyTest(TempHome):
    def setUp(self):
        super().setUp()
        self.copy = self.h('.codex/skills/local')
        self.keep = self.h('.agents/skills/keep')
        for path in (self.copy, self.keep):
            write(os.path.join(path, 'SKILL.md'), skill('foo', 'Same description.'))
        write(self.h('.agents/.skill-lock.json'), json.dumps({'skills': {'keep': {'source': 'org/public'}}}))
        listing = x_listing_text([], [('foo', 'Test.', path + '/SKILL.md') for path in (self.copy, self.keep)])
        self.usage = usage.collect(self.home, 'codex', 'current', listing)

    def add_private_alias(self):
        link(self.copy, self.h('.claude/skills/PRIVATE_ALIAS'))
        write(self.h('.claude/skills/.skills_store_lock.json'), json.dumps({'skills': {
            'PRIVATE_ALIAS': {'source': 'org/PRIVATE_REPO'}}}))

    def test_alias_and_lock_stay_private(self):
        self.add_private_alias()
        s = scan.scan(self.home, listings=self.usage['listings'])
        p = plan.build(s, self.usage)
        for result in (json.dumps(s), json.dumps(p), serve.render(p)):
            for private in ('PRIVATE_ALIAS', 'PRIVATE_REPO', '.claude/skills'):
                self.assertNotIn(private, result)
        e = next(x for x in p['entries'].values() if x['path'] == self.copy)
        self.assertTrue(e['documented'])

    def test_installed_claude_plugin_protects_entity_without_exporting_install(self):
        install = self.h('PRIVATE_INSTALL')
        link(self.copy, os.path.join(install, 'skills', 'PRIVATE_PLUGIN_ALIAS'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps({'plugins': {
            'private@market': [{'installPath': install}]}}))
        s = scan.scan(self.home, listings=self.usage['listings'])
        p = plan.build(s, self.usage)
        self.assertNotIn('PRIVATE_', json.dumps(s) + json.dumps(p))
        steps = plan.compute_steps(p)['steps']
        self.assertEqual([(st['type'], st['path']) for st in steps], [('disable', self.copy + '/SKILL.md')])

    def test_rendering_an_old_single_agent_plan_filters_private_locks(self):
        s = scan.scan(self.home, listings=self.usage['listings'])
        p = plan.build(s, self.usage)
        e = next(x for x in p['entries'].values() if x['path'] == self.copy)
        e['source']['locks'].append({'name': 'private', 'file': self.h('.claude/skills/.skills_store_lock.json'),
                                     'record': {'source': 'org/PRIVATE_REPO'}})
        page = serve.render(p)
        self.assertNotIn('PRIVATE_REPO', page)
        self.assertIn('org/public', page)

    def test_independent_personal_symlink_can_still_be_unlinked(self):
        physical = self.h('outside/physical')
        os.makedirs(os.path.dirname(physical))
        os.rename(self.copy, physical)
        link(physical, self.copy)
        link(physical, self.h('.claude/skills/alias'))
        s = scan.scan(self.home, listings=self.usage['listings'])
        steps = plan.compute_steps(plan.build(s, self.usage))['steps']
        self.assertEqual([(st['type'], st['path']) for st in steps], [('trash', self.copy)])

    def make_personal_alias(self):
        physical = self.h('packages/foo')
        os.makedirs(os.path.dirname(physical))
        os.rename(self.copy, physical)
        link(physical, self.copy)
        return physical

    def check_alias_dependency(self):
        s = scan.scan(self.home, listings=self.usage['listings'])
        steps = plan.compute_steps(plan.build(s, self.usage))['steps']
        self.assertEqual([(st['type'], st['path']) for st in steps], [('disable', self.copy + '/SKILL.md')])

    def test_shared_root_uses_the_same_personal_symlink(self):
        self.make_personal_alias()
        link(os.path.dirname(self.copy), self.h('.claude/skills'))
        self.check_alias_dependency()

    def test_registered_plugin_install_path_uses_personal_symlink(self):
        physical = self.make_personal_alias()
        write(os.path.join(physical, 'skills', 'plugin-skill', 'SKILL.md'), skill('plugin-skill', 'x'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps({'plugins': {
            'test@market': [{'installPath': self.copy}]}}))
        self.check_alias_dependency()

    def test_external_skill_file_link_protects_target_without_loading_it(self):
        self.make_personal_alias()
        link(self.copy + '/SKILL.md', self.h('.claude/skills/file-link/SKILL.md'))
        self.check_alias_dependency()
        full = scan.scan(self.home)
        steps = plan.compute_steps(plan.build(full, self.usage))['steps']
        self.assertEqual([(st['type'], st['path']) for st in steps], [('disable', self.copy + '/SKILL.md')])
        other = scan.scan(self.home, listings={'claude': {'entries': []}})
        self.assertEqual(other['entities'], {})
        self.assertEqual(other['issues'][0]['type'], 'unreadable')

    def test_plugin_link_to_nested_skill_protects_parent(self):
        nested = os.path.join(self.copy, 'nested')
        write(os.path.join(nested, 'SKILL.md'), skill('nested', 'Nested.'))
        install = self.h('PRIVATE_INSTALL')
        link(nested, os.path.join(install, 'skills', 'PRIVATE_PLUGIN_ALIAS'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps({'plugins': {
            'private@market': [{'installPath': install}]}}))
        s = scan.scan(self.home, listings=self.usage['listings'])
        steps = plan.compute_steps(plan.build(s, self.usage))['steps']
        self.assertEqual([(st['type'], st['path']) for st in steps], [('disable', self.copy + '/SKILL.md')])

    def test_older_full_scan_with_current_usage_filters_private_metadata(self):
        self.add_private_alias()
        link('/missing', self.h('.claude/skills/PRIVATE_DEAD'))
        s = scan.scan(self.home)
        self.assertIn('PRIVATE_ALIAS', json.dumps(s))
        p = plan.build(s, self.usage)
        for value in (json.dumps(p), serve.render(p)):
            for marker in ('PRIVATE_ALIAS', 'PRIVATE_REPO', 'PRIVATE_DEAD'):
                self.assertNotIn(marker, value)
        group = p['dedup'][0]
        kept = next(x for x in p['entries'].values() if x['path'] == self.keep)
        self.assertEqual(group['options'][kept['id']]['steps'][0]['type'], 'disable')

    def test_explicit_invalid_listings_never_write_an_unscoped_scan(self):
        inp = os.path.join(self.tmp, 'usage.json')
        out = os.path.join(self.tmp, 'scan.json')
        cmd = [sys.executable, os.path.join(SCRIPTS, 'scan.py'), '--home', self.home,
               '--project', self.home, '--listings', inp, '--out', out]
        for body in (None, '{', '{}', '{"listings":{}}', '{"listings":{"unknown":{"entries":[]}}}',
                     '{"listings":{"codex":null}}', '{"listings":{"codex":{"entries":"bad"}}}',
                     '{"scope":"current","current":"codex","listings":{"codex":{"entries":[]},"claude":{"entries":[]}}}'):
            with self.subTest(body=body):
                if body is not None:
                    write(inp, body)
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(os.path.exists(out))
