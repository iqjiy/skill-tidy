"""测试用：造两边会话日志里的几种记录。格式见 references/agents.md 第 3–5 节。"""
import calendar
import json
import os

from util import TempHome, write


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(',', ':'))


# ------------------------------------------------------------ Claude Code 日志里的几种记录

def c_listing(names, ts, cwd='/work'):
    return dumps({'type': 'attachment', 'timestamp': ts, 'cwd': cwd, 'sessionId': 's', 'version': '2.1.319',
                  'attachment': {'type': 'skill_listing', 'isInitial': True, 'skillCount': len(names),
                                 'names': names, 'digests': [], 'content': ''}})


def c_skill(name, ts='2026-11-07T04:00:00.000Z'):
    return dumps({'type': 'assistant', 'timestamp': ts, 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Skill', 'input': {'skill': name}, 'caller': {'type': 'direct'}}]}})


def c_command(name, ts='2026-11-07T04:00:00.000Z'):
    return dumps({'type': 'user', 'timestamp': ts, 'message': {
        'role': 'user', 'content': '<command-name>/%s</command-name>\n<command-message>%s</command-message>' % (name, name)}})


def c_tool_result(text, ts='2026-11-07T04:00:00.000Z'):
    return dumps({'type': 'user', 'timestamp': ts, 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': text}]}})


# ------------------------------------------------------------ Codex 日志里的几种记录

def x_meta(ts, cwd='/work'):
    return dumps({'timestamp': ts, 'type': 'session_meta', 'payload': {
        'id': 'x', 'timestamp': ts, 'cwd': cwd, 'originator': 'Codex Desktop', 'cli_version': '0.166.0'}})


def x_listing_text(roots, items):
    return ('<skills_instructions>\n## Skills\nA skill is a set of local instructions.\n### Skill roots\n' +
            ''.join('- `%s` = `%s`\n' % kv for kv in roots) + '### Available skills\n' +
            ''.join('- %s: %s (file: %s)\n' % it for it in items) +
            '### How to use skills\n- Discovery: The list above is the skills available.\n</skills_instructions>')


def x_listing(roots, items, role='developer'):
    return dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                                                       'content': [{'type': 'input_text', 'text': x_listing_text(roots, items)}]}})


def x_exec(cmd):
    return dumps({'type': 'response_item', 'payload': {
        'type': 'custom_tool_call', 'name': 'exec', 'input': 'await tools.exec_command({cmd:%s})' % json.dumps(cmd)}})


def x_function(cmd):
    return dumps({'type': 'response_item', 'payload': {
        'type': 'function_call', 'name': 'shell', 'arguments': dumps({'command': ['bash', '-lc', cmd]})}})


def x_output(text):
    return dumps({'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'output': text}})


def x_completed(cmd):
    return dumps({'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'command': cmd}}})


class LogHome(TempHome):
    def claude_log(self, rel, lines, mtime=None):
        p = write(self.h('.claude/projects', rel), ''.join(l + '\n' for l in lines))
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def codex_log(self, start, lines):
        """start 形如 2026-11-07T23-07-44，和真实文件名一样。"""
        day = start[:10].replace('-', '/')
        return write(self.h('.codex/sessions', day, 'rollout-%s-01a1b68d.jsonl' % start), ''.join(l + '\n' for l in lines))


def utc(*parts):
    return calendar.timegm(parts + (0,) * (6 - len(parts)))
