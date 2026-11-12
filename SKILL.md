---
name: skill-tidy
description: Use when 用户想整理本机 Claude Code 或 Codex 的 skill：说 skill 太多、有重复、两边各装了一份、想收拢成一份，或想知道哪些 skill 没加载、坏了、链接断了。只支持 Claude Code 和 Codex。
---

# skill-tidy（第一阶段：只读体检）

扫描两边的 skill 目录和会话日志，生成一个本地页面：去重、收拢、"如果执行会做哪几步"的预览、诊断。**第一阶段只读**：不移动、不删除、不改任何配置，页面上也没有执行入口。

脚本在本 skill 目录的 `scripts/` 下（下面写作 `$S`），使用系统自带的 `python3`。本机装有 Git 时，扫描还会读取仓库的来源地址。

## 1. 说明自己是谁，确定范围

- 先告诉用户你是 Claude Code 还是 Codex。拿不准时看环境变量：Claude Code 有 `CLAUDECODE=1`，Codex 可能有 `CODEX_SANDBOX`。还对不上就问用户。不要靠 `插件名:` 前缀判断，两边都有。
- 用户说了"只整理这里"还是"两边都整理"，就照着做；没说就问一句，默认只整理当前 agent（`--scope current`）。两边都整理是 `--scope all`。

## 2. 写下当前会话的加载清单

建输出目录：`OUT=$(mktemp -d -t skill-tidy)`。不要放进任何 skill 目录。

把你上下文里的 skill 清单**原样**写进 `$OUT/listing.txt`，一个都不能漏，漏掉的会被误报成"被跳过"：
- Claude Code：可用 skill 列表，一行一个 `- 名字: 描述`。名字必须完整（含 `superpowers:` 这类前缀），描述可以截短。
- Codex：`## Skills` 里的 `### Skill roots` 表和 `### Available skills` 列表。每行末尾的 `(file: rN/…)` 必须保留。

## 3. 按顺序跑脚本

在用户的项目目录里运行：`scan.py` 的 `--project` 默认取当前目录，会顺带扫项目里的 `.claude/skills`、`.agents/skills`。

```bash
python3 $S/usage.py --current claude --scope all --current-listing $OUT/listing.txt --out $OUT/usage.json
python3 $S/scan.py --listings $OUT/usage.json --out $OUT/scan.json
python3 $S/plan.py --scan $OUT/scan.json --usage $OUT/usage.json --out $OUT/plan.json
```

`--current` 填你自己（`claude` 或 `codex`），`--scope` 填第 1 步定下的范围。

## 4. 补上需要判断的部分

读 `$OUT/plan.json`，只做这两件事。有内容就写进 `$OUT/judgments.json`，然后带上 `--judgments $OUT/judgments.json` 重跑 `plan.py`：

- **触发冲突**：在同一个 agent 已加载的 skill 里（`entries` 中 `status` 为 `loaded`），读描述，找出同一句用户请求会同时命中的两个 skill。必须写出这句请求，并说明保留的那一个能否独立完成它。写不出例句就不算。按描述判断，不要按关键词或相似度。以下都不算：同一来源的一组（同一仓库或同一 ClawHub 发布者）、有意设置的别名（如 grill-me 转到 grilling）、两边各装一份（那是收拢）、同一领域但功能互补。A 和 B、B 和 C 各有例句，也不能把 A、B、C 并成一组。`members` 和 `keep` 填 `entries` 里的 `path`；`keep` 填能独立完成那句请求的那份，`keep_can_do_it` 说的就是它。拿不准就两个都不写，由 `plan.py` 按来源证据选。
- **是否通用**：`consolidate` 里 `portability` 不为空时，用报告里的规则、相对文件名和行号判断。命中可能来自含凭据的配置文件，保留原文在本机。仅在现有信息足够时填写 `portable`，无法确定就沿用默认的谨慎预览。

```json
{"trigger_conflicts": [{"agent": "codex", "members": ["入口路径", "入口路径"], "example": "那句用户请求",
                        "keep": "要保留的入口路径", "keep_can_do_it": true, "reason": "一句话"}],
 "portable": {"skill 名字": false}}
```

## 5. 生成页面，交给用户

```bash
python3 $S/serve.py --static --plan $OUT/plan.json
```

它会生成页面并用浏览器打开。然后按这个顺序告诉用户：

1. 页面路径。
2. 去重：几组（触发冲突也算在里面）；每个 🔴 组一句话说明原因（例如包内变体）。
3. 收拢：可以直接处理的几组；内容不同、要用户先选以哪份为准的几组；已共享的几组（不用处理）；不通用的几组。
4. 诊断里最要紧的几条，例如被跳过的原因、死链接。
5. 保留哪一份、处理哪几组由用户在页面上选，你没有执行任何步骤。

## 常见错误

| 错误 | 后果 |
|---|---|
| 清单抄漏、名字抄错 | 已加载的 skill 被报成"被跳过" |
| 输出目录放进 skill 目录 | 改动了安装目录，指纹也跟着变 |
| 把诊断里的"被跳过"当成删除建议 | 诊断只说明情况，不据此建议删除 |
| 按名字相似把一串 skill 并成一组 | 只有写得出例句的才算触发冲突 |
| 替用户执行页面上的步骤 | 第一阶段只读，执行是第二阶段的事 |
