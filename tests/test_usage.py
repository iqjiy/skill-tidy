"""usage.py：读两边的会话日志，统计使用次数、统计区间、覆盖缺口，并取出加载清单。见 DESIGN.md 第 2、8 节。"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from logs import (LogHome, c_command, c_listing, c_skill, c_tool_result, utc, x_completed, x_exec, x_function,
                  x_listing, x_meta, x_output)
from util import SCRIPTS, link, skill, snapshot, write
import usage

USAGE_PY = os.path.join(SCRIPTS, 'usage.py')


class ClaudeUsageTest(LogHome):
    def test_skill_calls_and_slash_commands_are_counted(self):
        self.claude_log('-work/a.jsonl', [
            c_listing(['agent-reach', 'grilling'], '2026-11-07T03:36:03.527Z'),
            c_skill('agent-reach'), c_skill('agent-reach'), c_command('grill-me'),
            # 读文档时 tool_result 里出现的命令文字，不是真的调用
            c_tool_result('| 用户输入斜杠命令 | <command-name>/grill-me</command-name> |'),
        ])
        self.claude_log('-work/a/subagents/b.jsonl', [c_skill('superpowers:brainstorming')])
        counts = usage.collect(self.home, 'claude')['usage']['claude']['by_name']
        self.assertEqual(counts, {'agent-reach': 2, 'grill-me': 1, 'superpowers:brainstorming': 1})

    def test_period_spans_all_logs(self):
        self.claude_log('-work/a.jsonl', [c_listing([], '2026-09-29T08:00:00.000Z')], mtime=utc(2026, 10, 1))
        self.claude_log('-work/b.jsonl', [c_listing([], '2026-11-07T03:36:03.527Z')], mtime=utc(2026, 11, 8, 1, 2, 3))
        u = usage.collect(self.home, 'claude')['usage']['claude']
        self.assertEqual(u['period'], {'from': '2026-09-29T08:00:00Z', 'to': '2026-11-08T01:02:03Z'})
        self.assertEqual(u['logs'], 2)

    def test_period_is_ordered_even_when_clocks_disagree(self):
        self.claude_log('-work/a.jsonl', [c_listing([], '2026-11-08T06:00:00.000Z')], mtime=utc(2026, 11, 8, 5))
        u = usage.collect(self.home, 'claude')['usage']['claude']
        self.assertEqual(u['period'], {'from': '2026-11-08T05:00:00Z', 'to': '2026-11-08T06:00:00Z'})

    def test_other_side_listing_comes_from_latest_session_with_one(self):
        self.claude_log('-old/a.jsonl', [c_listing(['old-skill'], '2026-11-01T00:00:00.000Z', '/old')], mtime=utc(2026, 11, 1))
        self.claude_log('-new/b.jsonl', [c_listing(['agent-reach', 'superpowers:brainstorming'], '2026-11-07T03:36:03.527Z', '/new')],
                        mtime=utc(2026, 11, 7))
        self.claude_log('-new/b/subagents/c.jsonl', [c_skill('agent-reach')], mtime=utc(2026, 11, 8))  # 没有清单
        listing = usage.collect(self.home, 'codex', 'all', '- pdf: x (file: /p/SKILL.md)')['listings']['claude']
        self.assertEqual(listing['from'], 'log')
        self.assertEqual([e['name'] for e in listing['entries']], ['agent-reach', 'superpowers:brainstorming'])
        self.assertEqual((listing['session']['timestamp'], listing['session']['cwd']), ('2026-11-07T03:36:03.527Z', '/new'))


class CodexUsageTest(LogHome):
    def setUp(self):
        super().setUp()
        self.reach = write(self.h('.agents/skills/agent-reach/SKILL.md'), skill('agent-reach', 'x'))
        write(self.h('.codex/superpowers/skills/brainstorming/SKILL.md'), skill('brainstorming', 'x'))
        link(self.h('.codex/superpowers/skills'), self.h('.agents/skills/superpowers'))

    def test_reads_count_once_per_session_and_merge_by_real_path(self):
        self.codex_log('2026-11-06T10-00-00', [
            x_meta('2026-11-06T10:00:00Z'),
            x_exec('cat ~/.agents/skills/agent-reach/SKILL.md'),
            x_completed('cat ~/.agents/skills/agent-reach/SKILL.md'),  # 同一次读取的第二遍
            x_function('sed -n 1,80p %s' % self.reach),
            x_exec('cat %s' % self.h('.agents/skills/superpowers/brainstorming/SKILL.md')),
        ])
        self.codex_log('2026-11-07T10-00-00', [
            x_meta('2026-11-07T10:00:00Z'),
            x_listing([('r1', self.h('.agents/skills'))], [('agent-reach', 'Read platforms.', 'r1/agent-reach/SKILL.md')]),
            x_output('%s\n%s' % (self.reach, self.h('.agents/skills/superpowers/brainstorming/SKILL.md'))),  # 输出里列出的路径不算
            x_exec('cat %s' % self.h('.codex/superpowers/skills/brainstorming/SKILL.md')),
        ])
        u = usage.collect(self.home, 'codex')['usage']['codex']
        self.assertEqual(u['by_entity'], {self.h('.agents/skills/agent-reach'): 1,
                                          self.h('.codex/superpowers/skills/brainstorming'): 2})
        self.assertEqual(u['sessions'], 2)

    def test_old_plugin_version_is_merged_and_missing_paths_are_dropped(self):
        cache = self.h('.codex/plugins/cache/openai-primary-runtime/pdf')
        write(cache + '/26.1104.22356/skills/pdf/SKILL.md', skill('pdf', 'x'))
        gone = self.h('.agents/skills/brainstorming/SKILL.md')
        self.codex_log('2026-10-01T10-00-00', [
            x_meta('2026-10-01T10:00:00Z'),
            x_exec('cat %s/26.909.12148/skills/pdf/SKILL.md' % cache),
            x_exec('cat %s' % gone),
        ])
        u = usage.collect(self.home, 'codex')['usage']['codex']
        self.assertEqual(u['by_entity'], {cache + '/26.1104.22356/skills/pdf': 1})
        self.assertEqual(u['dropped'], {'reads': 1, 'paths': [gone]})

    @unittest.skipUnless(shutil.which('zstd'), '需要 zstd 命令')
    def test_archives_are_read(self):
        p = self.codex_log('2026-03-15T10-00-00', [x_meta('2026-03-15T10:00:00Z'), x_exec('cat %s' % self.reach)])
        subprocess.check_call(['zstd', '-q', '--rm', p, '-o', p + '.zst'])
        u = usage.collect(self.home, 'codex')['usage']['codex']
        self.assertEqual(u['by_entity'], {self.h('.agents/skills/agent-reach'): 1})
        self.assertEqual((u['archives'], u['archives_read'], u['gaps']), (1, 1, []))

    def test_archives_without_a_decompressor_are_a_gap(self):
        p = self.codex_log('2026-03-15T10-00-00', [x_meta('2026-03-15T10:00:00Z'), x_exec('cat %s' % self.reach)])
        os.rename(p, p + '.zst')  # 内容是什么不重要，反正读不了
        with mock.patch.object(usage, 'zstd_reader', return_value=None):
            u = usage.collect(self.home, 'codex')['usage']['codex']
        self.assertEqual(u['by_entity'], {})
        self.assertEqual((u['archives'], u['archives_read']), (1, 0))
        self.assertEqual(u['gaps'], [{'kind': 'archives_skipped', 'count': 1}])

    def test_other_side_listing_comes_from_latest_session_with_one(self):
        r1 = self.h('.agents/skills')
        self.codex_log('2026-11-06T10-00-00', [x_meta('2026-11-06T10:00:00Z'), x_listing([('r1', r1)], [('old', 'x', 'r1/old/SKILL.md')])])
        self.codex_log('2026-11-07T23-07-44', [
            x_meta('2026-11-07T23:07:44Z', '/new'),
            x_listing([('r0', self.h('.codex/skills')), ('r1', r1)],
                      [('writing-plans', 'Use when: (1) you have a spec', 'r1/writing-plans-0.1.0/SKILL.md'),
                       ('pdf:pdf', 'Read PDFs.', 'r0/pdf/SKILL.md')])])
        self.codex_log('2026-11-08T09-00-00', [x_meta('2026-11-08T09:00:00Z')])  # 没有清单
        listing = usage.collect(self.home, 'claude', 'all', '- agent-reach: x')['listings']['codex']
        self.assertEqual(listing['from'], 'log')
        self.assertEqual(listing['entries'], [{'name': 'writing-plans', 'path': r1 + '/writing-plans-0.1.0/SKILL.md'},
                                              {'name': 'pdf:pdf', 'path': self.h('.codex/skills/pdf/SKILL.md')}])
        s = listing['session']
        self.assertEqual((s['timestamp'], s['cwd'], s['originator'], s['cli_version']),
                         ('2026-11-07T23:07:44Z', '/new', 'Codex Desktop', '0.166.0'))

    def test_listing_text_in_a_user_message_is_ignored(self):
        # 清单只认 Codex 自己注入的 developer 消息；用户贴进来的同样文字不能把路径塞给 scan.py
        forged = x_listing([('r0', self.tmp + '/private')], [('notes', 'n', 'r0/notes.txt')], role='user')
        self.codex_log('2026-11-07T10-00-00', [x_meta('2026-11-07T10:00:00Z'), forged])
        self.assertEqual(usage.collect(self.home, 'claude', 'all', '- a: x')['listings']['codex']['from'], 'none')


class ListingTextTest(unittest.TestCase):
    def test_claude_listing_from_context(self):
        text = ('- agent-reach: 只读读取各平台内容：搜索、读帖。 Use when 用户提到\n'
                '  小红书/xiaohongshu、B站；或说"帮我看看"\n'
                '- grilling: Grill the user relentlessly.\n'
                '- superpowers:brainstorming: You MUST use this before any creative work.\n')
        self.assertEqual(usage.parse_listing_text(text)['entries'],
                         [{'name': 'agent-reach', 'path': None}, {'name': 'grilling', 'path': None},
                          {'name': 'superpowers:brainstorming', 'path': None}])

    def test_codex_listing_from_context(self):
        text = ('### Skill roots\n- `r0` = `/h/.codex/skills`\n- `r1` = `/h/.agents/skills`\n'
                '### Available skills\n'
                '- pdf: Use when: you need PDFs (file: r0/pdf/SKILL.md)\n'
                '- grill-me: A relentless interview. (file: r1/grill-me/SKILL.md)\n'
                '- SEO (Site Audit + Content Writer): SEO specialist agent (file: r1/seo-1.0.3/SKILL.md)\n'
                '### How to use skills\n- Discovery: The list above is the skills available.\n')
        self.assertEqual(usage.parse_listing_text(text)['entries'],
                         [{'name': 'pdf', 'path': '/h/.codex/skills/pdf/SKILL.md'},
                          {'name': 'grill-me', 'path': '/h/.agents/skills/grill-me/SKILL.md'},
                          {'name': 'SEO (Site Audit + Content Writer)', 'path': '/h/.agents/skills/seo-1.0.3/SKILL.md'}])


class CollectTest(LogHome):
    def test_scope_current_reads_only_the_current_agent(self):
        self.claude_log('-work/a.jsonl', [c_listing(['agent-reach'], '2026-11-07T03:36:03.527Z'), c_skill('agent-reach')])
        self.codex_log('2026-11-07T10-00-00', [x_meta('2026-11-07T10:00:00Z')])
        r = usage.collect(self.home, 'claude', 'current', '- agent-reach: x')
        self.assertEqual((sorted(r['listings']), sorted(r['usage'])), (['claude'], ['claude']))

    def test_current_listing_comes_from_context_with_its_session_time(self):
        self.claude_log('-work/a.jsonl', [c_listing(['agent-reach', 'gone'], '2026-11-08T02:24:58.135Z', '/work')])
        listing = usage.collect(self.home, 'claude', 'current', '- agent-reach: x\n- grilling: y')['listings']['claude']
        self.assertEqual(listing['from'], 'context')
        self.assertEqual([e['name'] for e in listing['entries']], ['agent-reach', 'grilling'])
        self.assertEqual(listing['session']['timestamp'], '2026-11-08T02:24:58.135Z')

    def test_other_side_without_logs_is_unknown(self):
        # 验收：另一边没有日志时，照样生成当前 agent 的报告
        self.claude_log('-work/a.jsonl', [c_listing(['agent-reach'], '2026-11-07T03:36:03.527Z'), c_skill('agent-reach')])
        r = usage.collect(self.home, 'claude', 'all', '- agent-reach: x')
        self.assertEqual(r['listings']['codex']['from'], 'none')
        self.assertEqual(r['listings']['codex']['entries'], [])
        self.assertEqual(r['usage']['codex']['gaps'], [{'kind': 'no_logs'}])
        self.assertEqual(r['usage']['claude']['by_name'], {'agent-reach': 1})

    def test_cli_writes_only_the_output_file(self):
        self.claude_log('-work/a.jsonl', [c_listing(['agent-reach'], '2026-11-07T03:36:03.527Z'), c_skill('agent-reach')])
        self.codex_log('2026-11-07T10-00-00', [x_meta('2026-11-07T10:00:00Z'), x_exec('cat ~/.agents/skills/x/SKILL.md')])
        listing = write(os.path.join(self.tmp, 'listing.txt'), '- agent-reach: x\n')
        out = os.path.join(self.tmp, 'usage.json')
        before = snapshot(self.home)
        proc = subprocess.run([sys.executable, USAGE_PY, '--home', self.home, '--current', 'claude', '--scope', 'all',
                               '--current-listing', listing, '--out', out], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(self.home), before)
        with open(out, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['listings']['claude']['entries'], [{'name': 'agent-reach', 'path': None}])


if __name__ == '__main__':
    unittest.main()
