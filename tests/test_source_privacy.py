"""临时安装目录、真实 Git 配置，以及扫描、计划、页面三个输出的边界。"""
import json
import os
import signal
import shutil
import subprocess
import sys

from util import TempHome, skill, write
import plan
import scan
import serve
import usage


class SourcePrivacyTest(TempHome):
    def reports(self, extra=''):
        paths = []
        for root in ('.agents/skills', '.codex/skills'):
            paths.append(write(self.h(root, 'demo', 'SKILL.md'), skill('demo', 'x', extra)))
        listing = '\n'.join('- demo: x (file: %s)' % p for p in paths)
        u = usage.collect(self.home, 'codex', listing_text=listing)
        s = scan.scan(self.home, listings=u['listings'])
        p = plan.Planner(s, u, {}).run()
        self.assertTrue(p['dedup'])  # 有卡片时来源才会显示在 HTML 的表格里
        return json.dumps(s), json.dumps(p), serve.render(p)

    def test_frontmatter_only_exports_required_fields_and_cleans_sources(self):
        urls = {'repository': 'https://acct:REPO_CANARY@example.test/org/project?sig=QUERY_CANARY',
                'homepage': 'https://acct:HOME_CANARY@example.test/page#QUERY_CANARY',
                'source': 'https://acct:SOURCE_CANARY@example.test/package'}
        extra = ''.join('%s: "%s"\n' % item for item in urls.items())
        extra += 'api_token: EXTRA_CANARY\n'
        for report in self.reports(extra):
            self.assertNotIn('_CANARY', report)
            for path in ('org/project', 'page', 'package'):
                self.assertIn('https://example.test/' + path, report)

    def test_git_remote_added_by_git_is_cleaned_in_all_outputs(self):
        if not shutil.which('git'):
            self.skipTest('git 未安装')
        repo = self.h('.agents/skills/demo')
        os.makedirs(repo)
        subprocess.run(['git', 'init', '-q', repo], check=True)
        url = 'https://acct:GIT_CANARY@example.test/org/project.git?sig=SIG_CANARY#ref'
        subprocess.run(['git', '-C', repo, 'remote', 'add', 'origin', url], check=True)
        with open(os.path.join(repo, '.git', 'config'), encoding='utf-8') as fh:
            self.assertIn('"' + url + '"', fh.read())
        for report in self.reports():
            self.assertNotIn('_CANARY', report)
            self.assertIn('https://example.test/org/project.git', report)

    def test_scp_remote_hides_account_and_query(self):
        for report in self.reports('repository: ACCT_CANARY@example.test:org/project.git?sig=SIG_CANARY\n'):
            self.assertIn('example.test:org/project.git', report)
            self.assertNotIn('_CANARY', report)

    def test_git_config_may_not_include_or_link_to_private_config(self):
        private = write(os.path.join(self.tmp, 'private.gitconfig'),
                        '[remote "origin"]\nurl = https://example.test/PRIVATE_CANARY/repo\n')
        write(self.h('.agents/skills/demo/.git/config'), '[include]\npath = %s\n' % private)
        os.makedirs(self.h('.codex/skills/demo/.git'))
        os.symlink(private, self.h('.codex/skills/demo/.git/config'))
        for report in self.reports():
            self.assertNotIn('_CANARY', report)

    def test_git_parser_ignores_ambient_repository_and_user_config(self):
        if not shutil.which('git'):
            self.skipTest('git 未安装')
        repo = os.path.join(self.tmp, 'working-repo')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        os.unlink(os.path.join(repo, '.git', 'config'))
        os.mkfifo(os.path.join(repo, '.git', 'config'))
        global_config = self.h('.gitconfig')
        os.mkfifo(global_config)
        target = self.h('.codex/skills/demo')
        write(target + '/SKILL.md', skill('demo', 'x'))
        write(target + '/.git/config', '[remote "origin"]\nurl = https://example.test/demo.git\n')
        process = subprocess.Popen([sys.executable, scan.__file__, '--home', self.home],
                                   cwd=repo, env=dict(os.environ, GIT_CONFIG_GLOBAL=global_config),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail('git 打开了调用目录或用户配置中的管道')
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn(b'https://example.test/demo.git', stdout)

    def test_clawhub_drops_nested_values(self):
        write(self.h('.agents/skills/demo/_meta.json'), json.dumps({
            'slug': 'demo', 'ownerId': {'token': 'NESTED_CANARY'}, 'version': ['NESTED_CANARY']}))
        for report in self.reports():
            self.assertNotIn('_CANARY', report)

    def test_old_source_record_can_be_safely_imported(self):
        url = '"https://name:OLD_CANARY@example.test/org/repo.git?sig=OLD_CANARY"'
        old = {'git': {'root': self.home, 'remote': url, 'unused': 'OLD_CANARY'},
               'clawhub': {'slug': 'demo', 'version': {'token': 'OLD_CANARY'}},
               'locks': [{'name': 'demo', 'file': 'skills.lock',
                          'record': {'sourceUrl': url, 'apiToken': 'OLD_CANARY'}}],
               'frontmatter': {'repository': url, 'api_token': 'OLD_CANARY'}}
        safe = scan.sanitize_source(old)
        self.assertNotIn('_CANARY', json.dumps(safe))
        self.assertEqual(safe['git']['remote'], 'https://example.test/org/repo.git')
        self.assertEqual(safe, scan.sanitize_source(safe))

    def test_unsupported_url_and_non_string_values_are_omitted(self):
        record = {'source': 'owner/project', 'sourceUrl': 'https://[malformed/LOCK_CANARY',
                  'zip_url': {'unexpected': 'LOCK_CANARY'}}
        write(self.h('.agents/.skill-lock.json'), json.dumps({'skills': {'demo': record}}))
        reports = self.reports('homepage: "bad source META_CANARY"\n')
        for report in reports:
            self.assertIn('owner/project', report)
            self.assertNotIn('_CANARY', report)
        source = json.loads(reports[0])['entities'][self.h('.agents/skills/demo')]['source']
        self.assertEqual(source['locks'][0]['record'], {'source': 'owner/project'})
        self.assertEqual(source['frontmatter'], {})

    def test_ambiguous_colon_separated_value_is_not_treated_as_a_repository(self):
        for report in self.reports('source: "account:VALUE_CANARY"\n'):
            self.assertNotIn('_CANARY', report)

    def test_long_run_of_yaml_tags_is_unverified(self):
        result = scan.check_frontmatter(skill('demo', '!!str &anchor ' * 1200 + 'valid'))
        self.assertEqual(result['description'], 'valid')
        self.assertEqual(result['status'], 'unverified')
        self.assertCountEqual(result['unverified'], ['tag', 'anchor'])
