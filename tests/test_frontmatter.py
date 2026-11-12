"""头部检查器：只认本机实际用到的写法，其余标"未验证"。见 DESIGN.md 第 4 节。"""
import unittest

import util  # noqa: F401  把 scripts/ 加进 sys.path
import scan

# 仿 feishu-drive-1.0.0：头部没写结束的 ---，第 8 行起的正文被当成了头部，直到第 11 行的分隔线。
FEISHU_LIKE = '\n'.join([
    '---',
    'name: feishu-drive',
    'description: 飞书云空间文件管理 Skill。',
    'required_permissions:',
    '  - drive:file:upload',
    '  - drive:file:download',
    '',
    '# 🚀 快速启动：三步打通云空间 (必读)',
    '为避免机器人文件进入“私有黑盒”，请在首次使用前完成以下配置：',
    '1. **创建锚点**：在您的飞书云空间创建一个文件夹。',
    '---',
    '',
    '正文',
]) + '\n'

PLANNING_LIKE_HOOKS = (
    'hooks:\n'
    '  UserPromptSubmit:\n'
    '    - hooks:\n'
    '        - type: command\n'
    '          command: "if [ -f task_plan.md ]; then echo \'[plan] active\'; fi"\n'
    '  PreToolUse:\n'
    '    - matcher: "Write|Edit|Bash"\n'
)


def fm(*lines):
    return '---\n' + ''.join(l + '\n' for l in lines) + '---\n\n# body\n'


class StatusTest(unittest.TestCase):
    CASES = [
        # (说明, 文本, 结论, name, description)
        ('plain', fm('name: pdf', 'description: Read PDFs.'), 'ok', 'pdf', 'Read PDFs.'),
        ('double quoted', fm('name: "a"', r'description: "Use when: you say \"hi\""'), 'ok', 'a', 'Use when: you say "hi"'),
        ('single quoted', fm('name: a', "description: 'it''s fine: really'"), 'ok', 'a', "it's fine: really"),
        ('folded', fm('name: a', 'description: >', '  line one', '  line two'), 'ok', 'a', 'line one line two\n'),
        ('folded strip', fm('name: a', 'description: >-', '  line one', '  line two'), 'ok', 'a', 'line one line two'),
        ('folded paragraphs', fm('name: a', 'description: >', '  a', '  b', '', '  c'), 'ok', 'a', 'a b\nc\n'),
        ('literal', fm('name: a', 'description: |', '  line one', '  line two'), 'ok', 'a', 'line one\nline two\n'),
        ('nested map', fm('name: a', 'description: b', 'metadata:', '  author: x', '  version: "1"'), 'ok', 'a', 'b'),
        ('deep nesting', fm('name: a', 'description: b', *PLANNING_LIKE_HOOKS.splitlines()), 'ok', 'a', 'b'),
        ('flow list', fm('name: a', 'description: b', 'allowed-tools: [Bash, Read]'), 'ok', 'a', 'b'),
        ('flow map', fm('name: a', 'description: b', 'metadata: {"clawdbot":{"emoji":"🧠","os":["linux"]}}'), 'ok', 'a', 'b'),
        ('block list', fm('name: a', 'description: b', 'triggers:', '  - security', '  - OWASP'), 'ok', 'a', 'b'),
        ('crlf', '---\r\nname: a\r\ndescription: b\r\n---\r\nbody\r\n', 'ok', 'a', 'b'),
        ('no frontmatter', '# debug-pro\n\nSystematic debugging.\n', 'no_frontmatter', None, None),
        ('empty file', '', 'no_frontmatter', None, None),
        ('fake symlink file', '../../SKILL.md', 'no_frontmatter', None, None),
        ('unclosed', '---\nname: a\ndescription: b\n', 'unclosed', 'a', 'b'),
        ('missing name', fm('description: b'), 'missing_name', None, 'b'),
        ('empty name', fm('name:', 'description: b'), 'missing_name', None, 'b'),
        ('name is a list', fm('name:', '  - a', 'description: b'), 'missing_name', None, 'b'),
        ('missing description', fm('name: a'), 'missing_description', 'a', None),
        ('empty quoted description', fm('name: a', 'description: ""'), 'missing_description', 'a', None),
    ]

    def test_status_name_and_description(self):
        for label, text, status, name, desc in self.CASES:
            with self.subTest(label):
                r = scan.check_frontmatter(text)
                self.assertEqual(r['status'], status)
                self.assertEqual(r['name'], name)
                self.assertEqual(r['description'], desc)

    def test_body_inside_frontmatter_points_at_first_body_line(self):
        r = scan.check_frontmatter(FEISHU_LIKE)
        self.assertEqual(r['status'], 'body_in_frontmatter')
        self.assertEqual(r['line'], 8)  # 从标题那一行算起，不是从第一句散文算起
        self.assertEqual(r['name'], 'feishu-drive')

    def test_body_line_without_heading(self):
        r = scan.check_frontmatter(fm('name: a', 'description: b', 'This line is body text.'))
        self.assertEqual(r['status'], 'body_in_frontmatter')
        self.assertEqual(r['line'], 4)


class UnverifiedTest(unittest.TestCase):
    CASES = [
        ('anchor', fm('name: a', 'description: &d some text'), 'anchor'),
        ('alias', fm('name: a', 'description: b', 'summary: *d'), 'alias'),
        ('tag', fm('name: a', 'description: !!str b'), 'tag'),
        ('comment line', fm('# a comment', 'name: a', 'description: b'), 'comment'),
        ('inline comment', fm('name: a', 'description: b # note'), 'comment'),
        ('multiline plain', fm('name: a', 'description: first line', '  second line'), 'multiline_plain'),
        ('multiline quoted', fm('name: a', 'description: "first', '  second"'), 'multiline_quoted'),
        ('multiline flow', fm('name: a', 'description: b', 'tags: [one,', '  two]'), 'multiline_flow'),
    ]

    def test_other_constructs_are_marked_unverified(self):
        for label, text, kind in self.CASES:
            with self.subTest(label):
                r = scan.check_frontmatter(text)
                self.assertEqual(r['status'], 'unverified')
                self.assertIn(kind, r['unverified'])

    def test_supported_constructs_are_not_marked(self):
        for label, text, status, _, _ in StatusTest.CASES:
            if status == 'ok':
                with self.subTest(label):
                    self.assertEqual(scan.check_frontmatter(text)['unverified'], [])

    def test_inline_comment_is_cut_from_value(self):
        self.assertEqual(scan.check_frontmatter(fm('name: a', 'description: b # note'))['description'], 'b')

    def test_multiline_plain_value_is_joined(self):
        r = scan.check_frontmatter(fm('name: a', 'description: first line', '  second line'))
        self.assertEqual(r['description'], 'first line second line')


class HintTest(unittest.TestCase):
    def test_unquoted_colon_is_only_a_hint(self):
        # 仿 a-stock-analysis-1.0.0：严格 YAML 不合格，但 Codex 照样加载
        r = scan.check_frontmatter(fm('name: a-stock-analysis', 'description: A股分析。Use when: (1) 查询行情'))
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['description'], 'A股分析。Use when: (1) 查询行情')
        self.assertEqual(r['hints'], [{'kind': 'unquoted_colon', 'key': 'description', 'line': 3}])

    def test_quoted_colon_is_not_a_hint(self):
        r = scan.check_frontmatter(fm('name: a', 'description: "Use when: (1) x"'))
        self.assertEqual(r['hints'], [])


class FieldsTest(unittest.TestCase):
    def test_top_level_scalars_are_kept(self):
        r = scan.check_frontmatter(fm('name: grill-me', 'description: x', 'disable-model-invocation: true',
                                      'homepage: "https://example.com/grill"'))
        self.assertEqual(r['fields']['disable-model-invocation'], 'true')
        self.assertEqual(r['fields']['homepage'], 'https://example.com/grill')

    def test_nested_and_list_values_are_not_kept_as_fields(self):
        r = scan.check_frontmatter(fm('name: a', 'description: b', 'metadata:', '  source: x', 'triggers:', '  - y'))
        self.assertNotIn('metadata', r['fields'])
        self.assertNotIn('triggers', r['fields'])
        self.assertNotIn('source', r['fields'])  # 嵌套的 source 不当成顶层字段


if __name__ == '__main__':
    unittest.main()
