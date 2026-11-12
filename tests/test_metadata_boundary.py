"""目录外的 Codex skill、文档链接和两种加载清单的读取边界。"""
import json
import os
import subprocess
import sys

from logs import LogHome, x_listing, x_meta
from util import SCRIPTS, link, skill, write
import scan
import usage


class ListingBoundaryTest(LogHome):
    def test_claude_descriptions_cannot_select_directories(self):
        other = write(self.tmp + '/other/SKILL.md', skill('outside', 'private description'))
        u = usage.collect(self.home, 'claude', 'current', '- demo: 普通描述 (file: %s)' % other)
        self.assertEqual(u['listings']['claude']['entries'], [{'name': 'demo', 'path': None}])
        # 旧 usage.json 可能已经把描述后缀误解析成路径。
        u['listings']['claude']['entries'][0]['path'] = other
        self.assertEqual(scan.scan(self.home, listings=u['listings'])['entities'], {})

    def test_codex_can_load_external_skills_from_context_and_developer_log(self):
        d = self.tmp + '/outside/team (release)'
        write(d + '/SKILL.md', skill('team', 'real external skill'))
        roots = [('r0', os.path.dirname(d))]
        items = [('team', 'Team.', 'r0/team (release)/SKILL.md')]
        self.codex_log('2026-11-07T10-00-00', [x_meta('2026-11-07T10:00:00Z'), x_listing(roots, items)])
        for agent, text in [('claude', '- demo: Demo.'), ('codex', '- team: Team. (file: %s/SKILL.md)' % d)]:
            with self.subTest(agent=agent):
                u = usage.collect(self.home, agent, 'all', text)
                result = scan.scan(self.home, listings=u['listings'])
                self.assertEqual(result['entities'][d]['frontmatter']['name'], 'team')


class MetadataBoundaryTest(LogHome):
    def test_project_manifest_cannot_link_to_an_outside_document(self):
        target = write(self.tmp + '/outside/private.yaml', skill('not-a-skill', 'OUTSIDE_DESCRIPTION'))
        project = self.tmp + '/project'
        directory = project + '/.agents/skills/linked'
        link(target, directory + '/SKILL.md')
        listings = {'codex': {'entries': [{'name': 'linked', 'path': directory + '/SKILL.md'}]}}
        result = scan.scan(self.home, project=project, listings=listings)
        self.assertNotIn('OUTSIDE_DESCRIPTION', json.dumps(result))
        self.assertNotIn(directory, result['entities'])

    def test_external_skill_with_internal_manifest_link_is_supported(self):
        d = self.tmp + '/outside/team'
        write(d + '/docs/manifest.md', skill('team', 'internal link'))
        link('docs/manifest.md', d + '/SKILL.md')
        r = scan.scan(self.home, listings={'codex': {'entries': [{'name': 'team', 'path': d + '/SKILL.md'}]}})
        self.assertEqual(r['entities'][d]['frontmatter']['description'], 'internal link')

    def test_clawhub_metadata_is_bounded_to_skill_directory(self):
        d = self.h('.agents/skills/demo')
        write(d + '/SKILL.md', skill('demo', 'sample'))
        target = write(self.tmp + '/outside/private.json', json.dumps({'ownerId': 'OUTSIDE_OWNER'}))
        link(target, d + '/_meta.json')
        r = scan.scan(self.home)
        self.assertNotIn('OUTSIDE_OWNER', json.dumps(r))
        self.assertIsNone(r['entities'][d]['source']['clawhub'])

    def test_non_regular_metadata_does_not_block_scan(self):
        d = self.h('.agents/skills/demo')
        write(d + '/SKILL.md', skill('demo', 'sample'))
        os.mkfifo(d + '/_meta.json')
        cmd = [sys.executable, os.path.join(SCRIPTS, 'scan.py'), '--home', self.home, '--project', self.home]
        run = subprocess.run(cmd, timeout=3, capture_output=True, check=True)
        self.assertIsNone(json.loads(run.stdout)['entities'][d]['source']['clawhub'])
