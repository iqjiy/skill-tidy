"""scan.py：扫描、入口与实体、链接、嵌套、目录指纹、来源线索、是否通用。见 DESIGN.md 第 3、7 节。"""
import json
import os
import subprocess
import sys
import time
import unittest

from util import SCRIPTS, TempHome, link, skill, snapshot, write
import scan

SCAN_PY = os.path.join(SCRIPTS, 'scan.py')


def paths(entries):
    return sorted(e['path'] for e in entries)


def issues(result, kind):
    return sorted((i['path'], i.get('target')) for i in result['issues'] if i['type'] == kind)


def _retarget(path, target):
    os.remove(path)
    os.symlink(target, path)


class EntryTest(TempHome):
    def test_links_to_one_skill_are_separate_entries_of_one_entity(self):
        # 验收：两个入口指向同一个实体 -> 已共享
        real = self.h('.agents/skills/grill-me')
        write(real + '/SKILL.md', skill('grill-me', 'A relentless interview.'))
        link('../../.agents/skills/grill-me', self.h('.claude/skills/grill-me'))
        link('../../.agents/skills/grill-me', self.h('.codex/skills/grill-me'))
        r = scan.scan(self.home)
        self.assertEqual(paths(r['entries']), [real, self.h('.claude/skills/grill-me'), self.h('.codex/skills/grill-me')])
        self.assertEqual({e['entity'] for e in r['entries']}, {real})
        self.assertEqual(list(r['entities']), [real])
        by_path = {e['path']: e for e in r['entries']}
        self.assertEqual(by_path[self.h('.claude/skills/grill-me')]['agents'], ['claude'])
        self.assertEqual(by_path[self.h('.claude/skills/grill-me')]['link'], '../../.agents/skills/grill-me')
        self.assertEqual(by_path[self.h('.codex/skills/grill-me')]['agents'], ['codex'])
        self.assertIsNone(by_path[real]['link'])
        self.assertEqual(by_path[real]['kind'], 'shared')

    def test_dead_link_is_reported_and_not_an_entry(self):
        # 验收：一个死链接 -> 死链接
        link('../../global-skills/gone', self.h('.claude/skills/gone'))
        r = scan.scan(self.home)
        self.assertEqual(r['entries'], [])
        self.assertEqual([(i['type'], i['path'], i['target']) for i in r['issues']],
                         [('dead_link', self.h('.claude/skills/gone'), '../../global-skills/gone')])

    def test_dead_skill_md_link_is_reported_once(self):
        link('../../nowhere/SKILL.md', self.h('.codex/skills/broken/SKILL.md'))
        r = scan.scan(self.home)
        self.assertEqual(r['entries'], [])
        self.assertEqual([(i['type'], i['path']) for i in r['issues']],
                         [('dead_link', self.h('.codex/skills/broken/SKILL.md'))])

    def test_symlink_loop_is_reported_and_walk_ends(self):
        write(self.h('.agents/skills/pack/a/SKILL.md'), skill('a', 'x'))
        link('..', self.h('.agents/skills/pack/a/back'))
        r = scan.scan(self.home)
        self.assertEqual(paths(r['entries']), [self.h('.agents/skills/pack/a')])
        self.assertEqual(issues(r, 'loop'), [(self.h('.agents/skills/pack/a/back'), '..')])

    def test_entry_reached_through_a_linked_directory_records_the_link(self):
        write(self.h('.codex/superpowers/skills/brainstorming/SKILL.md'), skill('brainstorming', 'x'))
        link(self.h('.codex/superpowers/skills'), self.h('.agents/skills/superpowers'))
        r = scan.scan(self.home)
        [e] = r['entries']
        self.assertEqual(e['path'], self.h('.agents/skills/superpowers/brainstorming'))
        self.assertEqual(e['via'], self.h('.agents/skills/superpowers'))
        self.assertEqual(e['entity'], self.h('.codex/superpowers/skills/brainstorming'))
        self.assertEqual(e['level'], 2)

    def test_install_time_is_recorded(self):
        before = time.time()
        write(self.h('.claude/skills/foo/SKILL.md'), skill('foo', 'x'))
        [e] = scan.scan(self.home)['entries']
        self.assertGreaterEqual(e['installed_at'], before - 2)
        self.assertLessEqual(e['installed_at'], time.time() + 2)


class RootTest(TempHome):
    def test_depth_follows_each_root(self):
        write(self.h('.claude/skills/outer/inner/SKILL.md'), skill('inner', 'x'))  # Claude Code 只扫一层
        write(self.h('.agents/skills/a/b/c/d/SKILL.md'), skill('d', 'x'))  # 第 4 层
        write(self.h('.agents/skills/a/b/c/d/e/SKILL.md'), skill('e', 'x'))  # 第 5 层
        r = scan.scan(self.home)
        [e] = r['entries']
        self.assertEqual((e['path'], e['level']), (self.h('.agents/skills/a/b/c/d'), 4))

    def test_system_skills_belong_to_the_builtin_root_only(self):
        write(self.h('.codex/skills/.system/imagegen/SKILL.md'), skill('imagegen', 'x'))
        [e] = scan.scan(self.home)['entries']
        self.assertEqual((e['kind'], e['root'], e['agents']), ('builtin', self.h('.codex/skills/.system'), ['codex']))

    def test_hidden_and_dependency_directories_are_skipped(self):
        write(self.h('.agents/skills/pack/SKILL.md'), skill('pack', 'x'))
        write(self.h('.agents/skills/pack/node_modules/dep/SKILL.md'), skill('dep', 'x'))
        write(self.h('.agents/skills/pack/.git/x/SKILL.md'), skill('x', 'x'))
        self.assertEqual(paths(scan.scan(self.home)['entries']), [self.h('.agents/skills/pack')])

    def test_directory_without_any_skill_is_reported(self):
        os.makedirs(self.h('.codex/skills/codex-primary-runtime'))
        write(self.h('.codex/skills/notes/README.md'), 'x')
        write(self.h('.agents/skills/bundle/inner/SKILL.md'), skill('inner', 'x'))
        r = scan.scan(self.home)
        self.assertEqual(issues(r, 'no_skill_md'), [(self.h('.codex/skills/codex-primary-runtime'), None),
                                                    (self.h('.codex/skills/notes'), None)])

    def test_project_roots_are_scanned_and_not_duplicated(self):
        proj = os.path.join(self.tmp, 'proj')
        write(proj + '/.claude/skills/team/SKILL.md', skill('team', 'x'))
        write(self.h('.claude/skills/mine/SKILL.md'), skill('mine', 'x'))
        r = scan.scan(self.home, project=proj)
        self.assertEqual(sorted((e['path'], e['kind']) for e in r['entries']),
                         [(self.h('.claude/skills/mine'), 'personal'), (proj + '/.claude/skills/team', 'project')])
        r = scan.scan(self.home, project=self.home)  # 在主目录里运行时，项目目录就是个人目录
        self.assertEqual([(e['path'], e['kind']) for e in r['entries']], [(self.h('.claude/skills/mine'), 'personal')])


class PluginTest(TempHome):
    def test_claude_plugin_skills_come_from_installed_plugins(self):
        install = self.h('.claude/plugins/cache/claude-plugins-official/superpowers/6.3.0')
        write(install + '/skills/brainstorming/SKILL.md', skill('brainstorming', 'Explore intent.'))
        write(self.h('.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/brainstorming/SKILL.md'),
              skill('brainstorming', 'old version'))
        write(self.h('.claude/plugins/installed_plugins.json'), json.dumps({'version': 2, 'plugins': {
            'superpowers@claude-plugins-official': [
                {'scope': 'user', 'installPath': install, 'version': '6.3.0', 'gitCommitSha': '7e51'}]}}))
        write(self.h('.claude/settings.json'), json.dumps({'enabledPlugins': {'superpowers@claude-plugins-official': True}}))
        [e] = scan.scan(self.home)['entries']
        self.assertEqual((e['path'], e['kind'], e['agents']), (install + '/skills/brainstorming', 'plugin', ['claude']))
        self.assertEqual(e['plugin'], {'id': 'superpowers@claude-plugins-official', 'name': 'superpowers',
                                       'version': '6.3.0', 'git_sha': '7e51', 'enabled': True})

    def test_codex_plugin_skills_come_only_from_the_listing(self):
        cache = self.h('.codex/plugins/cache')
        pdf = write(cache + '/openai-primary-runtime/pdf/26.1104.22356/skills/pdf/SKILL.md', skill('pdf', 'PDF tools.'))
        write(cache + '/openai-curated-remote/openai-templates/0.1.1/skills/artifact-x/SKILL.md', skill('artifact-x', 'never loaded'))
        self.assertEqual(scan.scan(self.home)['entries'], [])
        r = scan.scan(self.home, listings={'codex': {'entries': [{'name': 'pdf:pdf', 'path': pdf}]}})
        [e] = r['entries']
        self.assertEqual((e['path'], e['kind'], e['agents']), (os.path.dirname(pdf), 'plugin', ['codex']))
        self.assertEqual(e['plugin'], {'id': 'pdf@openai-primary-runtime', 'name': 'pdf',
                                       'version': '26.1104.22356', 'git_sha': None, 'enabled': None})

    def test_listed_skill_outside_known_roots_is_added(self):
        other = write(self.tmp + '/proj/.agents/skills/team-skill/SKILL.md', skill('team-skill', 'x'))
        missing = self.tmp + '/proj/.agents/skills/gone/SKILL.md'
        r = scan.scan(self.home, listings={'codex': {'entries': [{'name': 'team-skill', 'path': other},
                                                                 {'name': 'gone', 'path': missing}]}})
        [e] = r['entries']
        self.assertEqual((e['path'], e['kind'], e['agents']), (os.path.dirname(other), 'listed', ['codex']))

    def test_listed_path_must_be_a_skill_md(self):
        # 清单里的路径指向别的文件时，不把它所在的目录当成 skill 去读
        notes = write(self.tmp + '/private/notes.txt', 'n')
        write(self.tmp + '/private/secret.env', 'X=~/.codex SECRET\n')
        r = scan.scan(self.home, listings={'codex': {'entries': [{'name': 'notes', 'path': notes}]}})
        self.assertEqual(r['entries'], [])
        self.assertNotIn(self.tmp + '/private', json.dumps(r))

    def test_listed_skill_already_found_is_not_duplicated(self):
        p = write(self.h('.codex/skills/pdf/SKILL.md'), skill('pdf', 'x'))
        r = scan.scan(self.home, listings={'codex': {'entries': [{'name': 'pdf', 'path': p}]},
                                           'claude': {'entries': [{'name': 'pdf'}]}})
        self.assertEqual([(e['path'], e['kind']) for e in r['entries']], [(os.path.dirname(p), 'personal')])


class EntityTest(TempHome):
    def test_frontmatter_result_is_attached_to_entity(self):
        d = self.h('.agents/skills/debug-pro-1.0.0')
        write(d + '/SKILL.md', '# debug-pro\n\nSystematic debugging.\n')
        ent = scan.scan(self.home)['entities'][d]
        self.assertEqual(ent['frontmatter']['status'], 'no_frontmatter')
        self.assertEqual(ent['skill_md']['bytes'], len('# debug-pro\n\nSystematic debugging.\n'))

    def test_same_skill_md_but_different_script(self):
        # 验收：两份 SKILL.md 相同但脚本不同 -> 内容不同
        md = skill('cam', 'Remove watermarks.')
        write(self.h('.claude/skills/cam/SKILL.md'), md)
        write(self.h('.claude/skills/cam/scripts/run.py'), 'print(1)\n')
        write(self.h('.codex/skills/cam/SKILL.md'), md)
        write(self.h('.codex/skills/cam/scripts/run.py'), 'print(2)\n')
        ents = scan.scan(self.home)['entities']
        a, b = ents[self.h('.claude/skills/cam')], ents[self.h('.codex/skills/cam')]
        self.assertEqual(a['skill_md']['sha256'], b['skill_md']['sha256'])
        self.assertNotEqual(a['fingerprint']['sha256'], b['fingerprint']['sha256'])

    def test_nested_skills_are_recorded_under_their_package(self):
        # 验收：一个目录里套着另一个同名 skill -> 包内变体
        top = self.h('.codex/skills/last30days')
        write(top + '/SKILL.md', skill('last30days', 'Deep research.'))
        write(top + '/variants/open/SKILL.md', skill('last30days', 'Research topics.'))
        write(top + '/skills/last30days/SKILL.md', '../../SKILL.md')
        ents = scan.scan(self.home)['entities']
        self.assertEqual(ents[top]['nested'], [top + '/skills/last30days', top + '/variants/open'])
        self.assertEqual(ents[top + '/variants/open']['nested'], [])
        self.assertEqual(ents[top + '/variants/open']['frontmatter']['name'], 'last30days')
        self.assertEqual(ents[top + '/skills/last30days']['frontmatter']['status'], 'no_frontmatter')


class FingerprintTest(TempHome):
    def make(self, name):
        d = os.path.join(self.tmp, name)
        write(d + '/SKILL.md', skill('cam', 'Remove watermarks.'))
        write(d + '/scripts/run.py', 'print(1)\n')
        write(d + '/a.md', 'a')
        write(d + '/b.md', 'b')
        link('a.md', d + '/ref.md')
        return d

    CASES = [
        ('DS_Store added', lambda d: write(d + '/.DS_Store', 'junk'), True),
        ('nested DS_Store added', lambda d: write(d + '/scripts/.DS_Store', 'junk'), True),
        ('pycache added', lambda d: write(d + '/scripts/__pycache__/run.cpython-39.pyc', 'x'), True),
        ('mtime changed', lambda d: os.utime(d + '/scripts/run.py', (1, 1)), True),
        ('script changed', lambda d: write(d + '/scripts/run.py', 'print(2)\n'), False),
        ('exec bit set', lambda d: os.chmod(d + '/scripts/run.py', 0o755), False),
        ('symlink retargeted', lambda d: _retarget(d + '/ref.md', 'b.md'), False),
        ('empty dir added', lambda d: os.makedirs(d + '/agents'), False),
        ('file renamed', lambda d: os.rename(d + '/a.md', d + '/c.md'), False),
    ]

    def test_whole_directory_comparison(self):
        for label, mutate, same in self.CASES:
            with self.subTest(label):
                a, b = self.make(label + '-a'), self.make(label + '-b')
                mutate(b)
                self.assertEqual(scan.fingerprint(a)['sha256'] == scan.fingerprint(b)['sha256'], same)

    def test_manifest_lists_relative_paths(self):
        f = scan.fingerprint(self.make('m'))
        self.assertEqual({row[0] for row in f['manifest']}, {'SKILL.md', 'a.md', 'b.md', 'ref.md', 'scripts', 'scripts/run.py'})
        self.assertTrue(f['complete'])

    @unittest.skipIf(os.geteuid() == 0, 'root 能读任何文件')
    def test_unreadable_file_makes_fingerprint_incomplete(self):
        d = self.make('locked')
        os.chmod(d + '/scripts/run.py', 0)
        self.addCleanup(os.chmod, d + '/scripts/run.py', 0o644)
        self.assertFalse(scan.fingerprint(d)['complete'])


class SourceTest(TempHome):
    def test_clawhub_meta_is_recorded(self):
        d = self.h('.agents/skills/brainstorming-0.1.0')
        write(d + '/SKILL.md', skill('brainstorming', 'x'))
        meta = {'ownerId': 'kn7b', 'slug': 'brainstorming', 'version': '0.1.0', 'publishedAt': 1770691893078}
        write(d + '/_meta.json', json.dumps(meta))
        self.assertEqual(scan.scan(self.home)['entities'][d]['source']['clawhub'], meta)

    def test_lock_records_attach_to_skill_or_are_reported_stale(self):
        write(self.h('.agents/skills/grill-me/SKILL.md'), skill('grill-me', 'x'))
        grill = {'source': 'mattpocock/skills', 'sourceUrl': 'https://github.com/mattpocock/skills.git'}
        lock = write(self.h('.agents/.skill-lock.json'), json.dumps(
            {'version': 3, 'skills': {'grill-me': grill, 'lark-approval': {'source': 'larksuite/cli'}}}))
        store = write(self.h('.claude/skills/.skills_store_lock.json'), json.dumps(
            {'version': 1, 'skills': {'youtube-watcher': {'source': 'skillhub'}}}))
        r = scan.scan(self.home)
        self.assertEqual(r['entities'][self.h('.agents/skills/grill-me')]['source']['locks'],
                         [{'file': lock, 'name': 'grill-me', 'record': grill}])
        self.assertEqual(sorted((i['path'], i['name']) for i in r['issues'] if i['type'] == 'stale_lock'),
                         sorted([(lock, 'lark-approval'), (store, 'youtube-watcher')]))

    def test_git_remote_is_found_above_the_skill(self):
        repo = self.h('.codex/superpowers')
        write(repo + '/.git/config', '[core]\n\tbare = false\n[remote "origin"]\n'
                                     '\turl = https://github.com/obra/superpowers.git\n'
                                     '\tfetch = +refs/heads/main:refs/remotes/origin/main\n')
        write(repo + '/skills/brainstorming/SKILL.md', skill('brainstorming', 'x'))
        link(repo + '/skills', self.h('.agents/skills/superpowers'))
        ent = scan.scan(self.home)['entities'][repo + '/skills/brainstorming']
        self.assertEqual(ent['source']['git'], {'root': repo, 'remote': 'https://github.com/obra/superpowers.git'})

    def test_git_remote_keeps_no_credentials(self):
        repo = self.h('.codex/superpowers')
        write(repo + '/.git/config', '[remote "origin"]\n\turl = https://user:SECRET@github.com/obra/superpowers.git?sig=S2\n')
        write(repo + '/skills/brainstorming/SKILL.md', skill('brainstorming', 'x'))
        link(repo + '/skills', self.h('.agents/skills/superpowers'))
        ent = scan.scan(self.home)['entities'][repo + '/skills/brainstorming']
        self.assertEqual(ent['source']['git']['remote'], 'https://github.com/obra/superpowers.git')

    def test_lock_records_keep_only_source_fields(self):
        write(self.h('.agents/skills/grill-me/SKILL.md'), skill('grill-me', 'x'))
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {'grill-me': {
            'source': 'mattpocock/skills', 'sourceUrl': 'https://tok@github.com/mattpocock/skills.git',
            'zip_url': 'https://cdn.example.com/g.zip?X-Amz-Signature=SECRET', 'apiToken': 'SECRET'}}}))
        [lock] = scan.scan(self.home)['entities'][self.h('.agents/skills/grill-me')]['source']['locks']
        self.assertEqual(lock['record'], {'source': 'mattpocock/skills', 'sourceUrl': 'https://github.com/mattpocock/skills.git',
                                          'zip_url': 'https://cdn.example.com/g.zip'})

    def test_git_repo_at_home_is_not_a_source(self):
        write(self.h('.git/config'), '[remote "origin"]\n\turl = https://example.com/dotfiles.git\n')
        write(self.h('.claude/skills/foo/SKILL.md'), skill('foo', 'x'))
        self.assertIsNone(scan.scan(self.home)['entities'][self.h('.claude/skills/foo')]['source']['git'])

    def test_frontmatter_source_fields_are_recorded(self):
        d = self.h('.claude/skills/foo')
        write(d + '/SKILL.md', skill('foo', 'x', 'homepage: https://example.com/foo\nrepository: "https://github.com/a/foo"\n'))
        self.assertEqual(scan.scan(self.home)['entities'][d]['source']['frontmatter'],
                         {'homepage': 'https://example.com/foo', 'repository': 'https://github.com/a/foo'})


class PortabilityTest(TempHome):
    def test_agent_specific_references_are_flagged(self):
        d = self.h('.claude/skills/planning-with-files')
        write(d + '/SKILL.md', skill('planning-with-files', 'x') + 'Run ${CLAUDE_PLUGIN_ROOT}/scripts/init.sh\n')
        write(d + '/scripts/ask.md', 'Use AskUserQuestion to confirm.\n')
        write(d + '/scripts/paths.sh', 'echo start\ncat ~/.codex/config.toml\n')
        with open(d + '/scripts/blob.bin', 'wb') as fh:
            fh.write(b'\x00\xff AskUserQuestion')
        hits = scan.scan(self.home)['entities'][d]['portability']
        self.assertEqual(sorted((h['kind'], h['file'], h['line']) for h in hits),
                         [('claude_plugin_root', 'SKILL.md', 7), ('claude_tool', 'scripts/ask.md', 1),
                          ('codex_path', 'scripts/paths.sh', 2)])

    def test_hits_do_not_copy_the_line(self):
        # 命中的那一行可能是 .env 里的密钥，只记规则、文件和行号，agent 需要时自己打开看
        d = self.h('.claude/skills/env')
        write(d + '/SKILL.md', skill('env', 'x'))
        write(d + '/.env', 'CONFIG=~/.codex/config.toml TOKEN=SECRET\n')
        [hit] = scan.scan(self.home)['entities'][d]['portability']
        self.assertEqual(hit, {'kind': 'codex_path', 'file': '.env', 'line': 1})

    def test_plain_skill_has_no_flags(self):
        d = self.h('.agents/skills/plain')
        write(d + '/SKILL.md', skill('plain', 'Read the file, then write a summary.'))
        self.assertEqual(scan.scan(self.home)['entities'][d]['portability'], [])


class ScopeTest(TempHome):
    def test_listings_limit_the_report_to_those_agents(self):
        # 只整理 Codex 时，Claude Code 独占的 skill 不进 scan.json，内容也不读
        write(self.h('.claude/skills/c-only/SKILL.md'), skill('c-only', 'x'))
        write(self.h('.claude/skills/c-only/.env'), 'X=~/.codex SECRET\n')
        link('../../nowhere', self.h('.claude/skills/dead'))
        write(self.h('.codex/skills/k/SKILL.md'), skill('k', 'x'))
        r = scan.scan(self.home, listings={'codex': {'entries': []}})
        self.assertEqual(paths(r['entries']), [self.h('.codex/skills/k')])
        self.assertEqual(r['issues'], [])
        self.assertNotIn('SECRET', json.dumps(r))

    def test_other_agent_aliases_only_supply_protection_and_source_evidence(self):
        write(self.h('.agents/skills/grilling/SKILL.md'), skill('grilling', 'x'))
        link('../../.agents/skills/grilling', self.h('.claude/skills/grilling'))
        write(self.h('.agents/skills/codex-only/SKILL.md'), skill('codex-only', 'x'))
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {
            'grilling': {'source': 'a/b'}, 'lark-x': {'source': 'c/d'}}}))
        r = scan.scan(self.home, listings={'claude': {'entries': []}})
        self.assertEqual(paths(r['entries']), [self.h('.claude/skills/grilling')])
        entity = r['entities'][self.h('.agents/skills/grilling')]
        self.assertTrue(entity['other_agent_has_source'])
        self.assertEqual(entity['source']['locks'], [])
        self.assertEqual(r['issues'], [])  # 范围外的残留记录、空目录都不报


class CliTest(TempHome):
    def test_cli_writes_only_the_output_file(self):
        # 验收：运行前后，安装目录和配置都不变
        write(self.h('.agents/skills/grill-me/SKILL.md'), skill('grill-me', 'x'))
        link('../../.agents/skills/grill-me', self.h('.claude/skills/grill-me'))
        link('../../global-skills/gone', self.h('.claude/skills/gone'))
        write(self.h('.agents/skills/pack/a/SKILL.md'), skill('a', 'x'))
        link('..', self.h('.agents/skills/pack/a/back'))
        write(self.h('.agents/.skill-lock.json'), json.dumps({'version': 3, 'skills': {'lark-x': {}}}))
        write(self.h('.claude/settings.json'), '{}')
        write(self.h('.codex/config.toml'), '[features]\n')
        pdf = write(self.h('.codex/plugins/cache/m/pdf/1.0/skills/pdf/SKILL.md'), skill('pdf', 'x'))
        listings = write(os.path.join(self.tmp, 'usage.json'),
                         json.dumps({'listings': {'codex': {'entries': [{'name': 'pdf:pdf', 'path': pdf}]}}}))
        out = os.path.join(self.tmp, 'scan.json')
        before = snapshot(self.home)
        proc = subprocess.run([sys.executable, SCAN_PY, '--home', self.home, '--project', self.home,
                               '--listings', listings, '--out', out], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(self.home), before)
        with open(out, encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertEqual(paths(data['entries']), [self.h('.agents/skills/grill-me'), self.h('.agents/skills/pack/a'),
                                                  os.path.dirname(pdf)])


if __name__ == '__main__':
    unittest.main()
