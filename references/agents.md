# Claude Code 与 Codex 的加载、日志与配置差异

给写 `scan.py`、`usage.py` 的人看。以下格式均为 2026-11-07 到 11-08 在本机实测，对应版本：Claude Code 2.1.319，Codex 桌面版 0.166 到 0.167、命令行版 0.162.1。版本变了要重新核对。本文的路径把主目录写成 `~`，日志原文里的绝对路径换成了 `/Users/you`。

## 1. 两边对照

| | Claude Code | Codex |
|---|---|---|
| 个人 skill 目录 | `~/.claude/skills`，只扫一层 | `~/.codex/skills` 和 `~/.agents/skills`，递归扫描 |
| 内置 skill | 没有对应的磁盘目录 | `~/.codex/skills/.system/` |
| 插件 skill | `~/.claude/plugins/cache/<市场>/<插件>/<版本>/skills/` | `~/.codex/plugins/cache/<市场>/<插件>/<版本>/skills/` |
| 已装插件记录 | `~/.claude/plugins/installed_plugins.json` | `~/.codex/config.toml` 的 `[plugins."名字@市场"]` |
| 加载清单在日志里的位置 | `type: attachment` 记录，`attachment.type == "skill_listing"` | 第一条 `role: developer` 的消息正文，`## Skills` 一节 |
| 会话日志 | `~/.claude/projects/<编码后的 cwd>/*.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，旧会话压缩成 `.jsonl.zst` |
| 关闭单个 skill | `settings.json` 的 `skillOverrides`，按名字 | `config.toml` 的 `[[skills.config]]`，按路径或名字 |
| 识别身份的环境变量 | `CLAUDECODE=1`、`AI_AGENT=claude-code_<版本>_agent` | `CODEX_SANDBOX`，只在开沙箱时才设置 |

## 2. 本机根目录

| 根目录 | 谁读 | 深度 | 只读 |
|---|---|---|---|
| `~/.claude/skills` | Claude Code | 1 | 否 |
| `~/.claude/plugins/cache` | Claude Code | 按 `installed_plugins.json` 里的 `installPath` 找 | 是，插件 |
| `~/.agents/skills` | Codex 直接读，Claude Code 只经软链接 | 4 | 否 |
| `~/.codex/skills` | Codex | 4 | 否 |
| `~/.codex/skills/.system` | Codex | 1 | 是，内置 |
| `~/.codex/plugins/cache` | Codex | 以加载清单的 Skill roots 为准 | 是，插件 |
| `<项目>/.claude/skills` | Claude Code | 1 | 否，可能是团队共用 |
| `<项目>/.agents/skills` | Codex | 4 | 否，可能是团队共用 |

注意：
- `~/.agents/skills/superpowers` 是指向 `~/.codex/superpowers/skills` 的软链接，Codex 经它加载第二层的 14 个 skill。
- Codex 的 Skill roots 里有一条 `r4` 直接指向 `~/.codex/plugins/cache/openai-curated-remote`，条目写作 `r4/plugin-management/0.1.0/skills/...`。所以插件根目录不能写死，以清单为准。
- 插件升级后目录名的版本号会变，例如 pdf 插件出现过 `26.909.12148`、`26.1026.10807`、`26.1104.22356` 三个版本。统计使用次数时，要把版本号这一段去掉再合并。

## 3. Claude Code 的加载清单

位置：会话日志里 `type == "attachment"` 且 `attachment.type == "skill_listing"` 的记录。一次会话里可能有多条，`isInitial: true` 的是开场那份。

```json
{
  "type": "attachment",
  "timestamp": "2026-11-07T03:36:03.527Z",
  "cwd": "/Users/you",
  "sessionId": "…",
  "version": "2.1.319",
  "attachment": {
    "type": "skill_listing",
    "isInitial": true,
    "skillCount": 34,
    "names": ["agent-reach", "camscanner-watermark-remover", "superpowers:brainstorming", "…"],
    "digests": ["eec9ac472b99597c", "…"],
    "content": "- agent-reach: 只读读取各平台内容…\n- …"
  }
}
```

要点：
- `names` 和 `digests` 一一对应，各 34 条。其中 14 条带 `插件名:` 前缀。
- `content` 每行一个 skill，写法是 `- 名字: 描述`，没有文件路径。名字要靠扫描目录才能对应到文件。
- 清单里有内置 skill（如 `simplify`、`loop`），磁盘上找不到对应文件，归为"内置"，不报"未定位"。
- 当前会话的清单由调用方 agent 从上下文直接提供，不必读日志。只有"整理全部"、又在 Codex 里运行时，才需要从日志里取 Claude Code 的清单。

## 4. Codex 的加载清单

位置：每个会话日志前几行里，`type == "response_item"`、`payload.type == "message"`、`payload.role == "developer"` 的那条记录。`payload.content` 是一个数组，其中一项的 `text` 含有 `## Skills`。

正文格式（JSON 解码后）：

```
## Skills
A skill is a set of local instructions … using the skill roots table.
### Skill roots
- `r0` = `/Users/you/.codex/skills`
- `r1` = `/Users/you/.agents/skills`
- `r2` = `/Users/you/.codex/skills/.system`
- `r3` = `/Users/you/.codex/plugins/cache/openai-curated-remote/pages/0.1.26/skills`
…
### Available skills
- imagegen: Generate or edit raster images … (file: r2/imagegen/SKILL.md)
- pdf:pdf: Read, create, inspect … (file: r7/pdf/26.1104.22356/skills/pdf/SKILL.md)
- writing-plans: Use when you have a spec … (file: r1/writing-plans-0.1.0/SKILL.md)
</skills_instructions>
```

要点：
- 每行一个 skill：`- 名字: 描述 (file: rN/相对路径)`。描述里可能有冒号，所以名字取第一个 `: ` 之前，路径从行尾的 `(file: …)` 取。
- 名字也可能带前缀，例如 `pdf:pdf`、`pages:write-page`，11-06 的清单里有 33 条。所以不能靠前缀判断是哪个 agent。
- 清单原样注入，没有截断。11-06 的清单有 94 条、30,100 字符，描述与磁盘上一致。
- 先用 `json.loads` 解析整行再取 `text`。如果直接对原始行跑正则，换行是 `\n` 两个字符，引号是 `\"`。
- 会话的元信息在第一行 `type == "session_meta"`：`payload.timestamp`、`payload.cwd`、`payload.originator`（如 `Codex Desktop`）、`payload.cli_version`。报告注明"清单取自哪次会话"时就用这些字段。
- Codex 只对最新会话可信。11-06 的清单里没有 grill-me，11-07 23:07 的清单里有，因为它是 11-07 才装的。

## 5. 使用次数

### Claude Code

| 信号 | 日志里的样子 | 怎么算 |
|---|---|---|
| 模型调用 skill | `message.content[]` 里 `{"type":"tool_use","name":"Skill","input":{"skill":"agent-reach","args":"…"}}` | 一次算一次，名字取 `input.skill` |
| 用户输入斜杠命令 | 用户消息里 `<command-name>/grill-me</command-name>` | 一次算一次，但要排除 `/model`、`/effort`、`/plugin` 这类内置命令 |

插件 skill 的名字带前缀，比如 `superpowers:writing-skills`。

### Codex

日志里没有"调用 skill"这种事件。模型是自己去读 SKILL.md 的，读取记录在 `payload.type` 为 `custom_tool_call` 或 `function_call` 的记录里，SKILL.md 的路径出现在 `payload.input` 或 `payload.arguments` 中。实例：

```
payload.type = custom_tool_call, payload.name = exec
payload.input = await Promise.allSettled([tools.exec_command({cmd:"cat ~/.agents/skills/agent-reach/SKILL.md"}), …])
```

规则：
- 一次会话里读过同一个 SKILL.md，只算一次。同一次读取在日志里会出现两遍：一遍在 `custom_tool_call`，一遍在 `event_msg` 的 `item_completed`。按会话去重后不会多算。
- 含 `### Skill roots` 的那条是开场注入的清单，要跳过，否则每个 skill 都会多算一次。
- 同一个 skill 可能从不同路径读到。例如 superpowers 的 skill，有时经 `~/.codex/superpowers/skills/…` 读，有时经 `~/.agents/skills/superpowers/…` 读。按真实路径合并。
- 日志里有已删除 skill 的旧路径，例如 `~/.agents/skills/brainstorming/`、`~/.agents/skills/pdf/`。对不上现存 skill 的丢弃，并记下丢弃条数。

### 日志规模

| | 文件数 | 体积 | 最早 |
|---|---|---|---|
| Claude Code | 70 个 `.jsonl` | 53 MB | 2026-09-29 |
| Codex | 26 个 `.jsonl` 和 114 个 `.jsonl.zst` | 87 MB 和 504 MB（压缩后） | 2026-03-15 |

Codex 最大的一个归档压缩后 398 MB，解压后 552 MB，所以要边解压边读，不能整个读进内存。

## 6. 读日志的写法

先按字节筛选，再对筛出的行跑正则。第一版脚本直接对每一行跑带懒惰匹配的正则，在超长行上回溯，三分钟都没跑完。照下面的写法，两边全部日志约 1.3 秒读完（系统自带 python3 3.9）。

```python
import os, re, shutil, subprocess

HOME = os.path.expanduser('~').encode()
SKILL_PATH = re.compile(rb'((?:/Users/|~/)[^"\\\s]{1,200}?/SKILL\.md)')  # 长度有上限，避免回溯

def iter_lines(path):
    """逐行读取 .jsonl 或 .jsonl.zst，不整个读进内存。"""
    if path.endswith('.zst'):
        if shutil.which('zstd'):
            proc = subprocess.Popen(['zstd', '-dcq', path], stdout=subprocess.PIPE)
            yield from proc.stdout
            proc.wait()
            return
        try:
            from compression import zstd  # Python 3.14 起自带
        except ImportError:
            return  # 两种都没有：跳过，由调用方记入覆盖缺口
        with zstd.open(path, 'rb') as fh:
            yield from fh
        return
    with open(path, 'rb') as fh:
        yield from fh

def codex_skill_reads(path):
    """返回一次 Codex 会话里读过的 SKILL.md 路径集合。"""
    seen = set()
    for line in iter_lines(path):
        if b'SKILL.md' not in line:
            continue
        if b'### Skill roots' in line:  # 开场注入的清单，不算使用
            continue
        if b'custom_tool_call' not in line and b'function_call' not in line and b'item_completed' not in line:
            continue
        for m in SKILL_PATH.finditer(line):
            raw = m.group(1)
            if raw.startswith(b'~/'):
                raw = HOME + raw[1:]
            seen.add(raw.decode('utf-8', 'replace'))
    return seen

CLAUDE_SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')

def claude_skill_calls(path):
    """返回 Claude Code 会话里 Skill 工具的调用名列表。"""
    names = []
    with open(path, 'rb') as fh:
        for line in fh:
            if b'"Skill"' not in line:
                continue
            names += CLAUDE_SKILL_CALL.findall(line.decode('utf-8', 'replace'))
    return names
```

注意：
- 路径有两种写法：绝对路径和 `~/` 开头。本机全部 Codex 日志里，只以 `~/` 形式出现的读取有 4 次，比如 `~/.codex/skills/last30days/SKILL.md`。上面已统一展开成绝对路径。
- `SKILL_PATH` 写死了 macOS 的 `/Users/`，Linux 要改成 `/home/`。
- 读到的路径要先 `os.path.realpath`，再对应到扫描结果。

## 7. 解析加载清单

```python
import json, re

def codex_listing(path):
    """从一次 Codex 会话日志里取出加载清单。返回 (会话信息, [(名字, 绝对路径)])。"""
    meta, entries = {}, []
    for raw in iter_lines(path):
        if b'"session_meta"' in raw and not meta:
            p = json.loads(raw)['payload']
            meta = {k: p.get(k) for k in ('timestamp', 'cwd', 'originator', 'cli_version')}
        if b'### Skill roots' not in raw:
            continue
        payload = json.loads(raw).get('payload', {})
        for part in payload.get('content', []):
            text = part.get('text', '') if isinstance(part, dict) else ''
            if '### Skill roots' not in text:
                continue
            i = text.find('### Skill roots')
            j = text.find('### Available skills', i)
            roots = dict(re.findall(r'- `(r\d+)` = `([^`]+)`', text[i:j]))
            for line in text[j:].split('\n')[1:]:
                if not line.startswith('- '):
                    break
                m = re.match(r'- (.+?): .* \(file: (r\d+)/([^)]+)\)$', line)
                if m and m.group(2) in roots:
                    entries.append((m.group(1), os.path.join(roots[m.group(2)], m.group(3))))
            return meta, entries
    return meta, entries

def claude_listing(path):
    """从一次 Claude Code 会话日志里取出开场的加载清单。返回 (会话信息, [名字])。"""
    with open(path, 'rb') as fh:
        for raw in fh:
            if b'"skill_listing"' not in raw:
                continue
            o = json.loads(raw)
            a = o.get('attachment', {})
            if a.get('type') == 'skill_listing' and a.get('isInitial'):
                meta = {k: o.get(k) for k in ('timestamp', 'cwd', 'version')}
                return meta, a.get('names', [])
    return {}, []
```

取"最近一次有效清单"：按会话的开始时间倒序遍历，返回第一个非空结果。Codex 的会话文件名里带开始时间，例如 `rollout-2026-11-07T23-07-44-…jsonl`。

## 8. 来源记录

| 文件 | 在哪 | 关键字段 | 说明 |
|---|---|---|---|
| `_meta.json` | ClawHub 安装的 skill 目录里 | `ownerId`、`slug`、`version`、`publishedAt` | `~/.agents/skills` 的 54 个里有 39 个带它。ClawHub 上同一个 slug 常有多个发布者，要靠 `ownerId` 区分 |
| `~/.agents/.skill-lock.json` | 共享目录的上一层 | `version: 3`。`skills.<名字>` 下有 `source`、`sourceUrl`、`skillPath`、`skillFolderHash`、`installedAt` | 记录了 24 个，其中 23 个 `lark-*` 已不在磁盘上，报为残留记录 |
| `~/.claude/skills/.skills_store_lock.json` | Claude Code 个人目录里 | `skills.<名字>` 下有 `source: skillhub`、`zip_url`、`version` | 记录的 2 个都已不在，报为残留记录 |
| `~/.claude/plugins/installed_plugins.json` | Claude Code 插件目录 | `plugins."名字@市场"[]` 下有 `installPath`、`version`、`gitCommitSha` | 插件 skill 来源"有据可查"的依据 |
| SKILL.md 头部 | skill 自身 | `source`、`repository`、`homepage` | 借鉴自 skill-manager |
| skill 目录所在的 git 仓库 | 向上找 `.git` | `remote.origin.url` | 例如 `~/.codex/superpowers` 的 remote 是 `github.com/obra/superpowers` |

`_meta.json` 样例：

```json
{"ownerId": "kn722nva0z7svbapne80p8e8jd7zwmk7", "slug": "blog-writer", "version": "0.1.0", "publishedAt": 1769361436760}
```

## 9. 关闭单个 skill

**Claude Code**：`~/.claude/settings.json` 里的 `skillOverrides`，按 skill 名字设置。程序内的说明原文："Per-skill listing overrides keyed by skill name. "name-only" lists the skill without its description; "user-invocable-only" hides it from the model but keeps /name; "off" hides it from both. Absent = on."

```json
{"skillOverrides": {"camscanner-watermark-remover": "off"}}
```

插件 skill 的键是 `brainstorming` 还是 `superpowers:brainstorming`，还没验证。整个插件的开关是 `enabledPlugins`，本机为 `{"superpowers@claude-plugins-official": true}`。

**Codex**：`~/.codex/config.toml`，写法出自官方文档 learn.chatgpt.com/docs/build-skills（11-07 查阅）。

```toml
[[skills.config]]
path = "/Users/you/.agents/skills/brainstorming-0.1.0/SKILL.md"
enabled = false
```

每条只能用 `path` 或 `name` 其中一个。`path` 只匹配一个文件，`name` 匹配所有同名的 skill。多条同时命中时，以最后一条为准。

## 10. 识别当前 agent

| 信号 | Claude Code | Codex |
|---|---|---|
| 调用方自报 | SKILL.md 要求 agent 先说明自己是谁 | 同左 |
| 环境变量 | `CLAUDECODE=1`、`AI_AGENT=claude-code_2-1-319_agent`、`CLAUDE_CODE_ENTRYPOINT=cli`，均为本机实测 | `CODEX_SANDBOX`，源码里只在开沙箱时设置。不开沙箱时很可能没有这个变量，要实测 |
| 不能用的 | `插件名:` 前缀，两边都有 | 同左 |

## 11. 本机的测试素材

写测试时可以直接拿这些作对照：

| 情况 | 本机实例 |
|---|---|
| 三个入口指向同一实体 | `~/.claude/skills/grill-me`、`~/.codex/skills/grill-me`、`~/.agents/skills/grill-me`，都指向 `~/.agents/skills/grill-me` |
| 两份整个目录相同 | `~/.claude/skills/agent-reach` 与 `~/.agents/skills/agent-reach` |
| SKILL.md 不同，文件也不同 | `camscanner-watermark-remover`：Codex 那份多了 `scripts/`、`agents/` |
| 一个 skill 目录里套着同名 skill | `~/.codex/skills/last30days/`，内有 `variants/open/` 和 `skills/last30days/`（14 字节的假文件） |
| 没有头部，被 Codex 跳过 | `debug-pro-1.0.0`、`test-runner-1.0.0` |
| 头部混进正文，被 Codex 跳过 | `feishu-drive-1.0.0`（第 12 行起是正文） |
| 严格 YAML 不合格，但被加载 | `a-stock-analysis-1.0.0`（描述里有未加引号的 `Use when: (1)`） |
| 同名冲突 | Codex 里的 brainstorming、executing-plans、writing-plans、skill-creator、pdf、last30days |
| 用了 Claude 专有变量，不通用 | `planning-with-files` 用了 `${CLAUDE_PLUGIN_ROOT}` |
| 空目录 | `~/.codex/skills/codex-primary-runtime` |
| 会干扰整目录比较的系统文件 | 本机 skill 目录里有 12 个 `.DS_Store` |

本机情况会变，测试用例要放在临时目录里自己造，以上实例只用于核对行为是否一致。
