# 原型脚本

设计阶段在本机做核查时用的一次性脚本，留作参考，不是正式代码。正式实现以 `references/agents.md` 里的写法为准。

| 文件 | 做什么 | 得出的结论 |
|---|---|---|
| `frontmatter_survey.py` | 扫描所有 SKILL.md，统计头部用到的写法，并用 PyYAML 严格解析 | 146 个里有 2 个严格解析不合格，只有 1 个真的被 Codex 跳过。见 DESIGN.md 第 4 节 |
| `codex_listing_timeline.py` | 按时间列出每次 Codex 会话加载了多少个 skill、清单有多长 | 清单没有被截断；装上不久的 skill 要到下一次会话才会出现 |
| `codex_usage_by_path.py` | 读全部 Codex 日志（包括 `.zst` 归档），统计每条 SKILL.md 路径被读过几次 | 证明边解压边读、先按字节筛选再跑正则的写法够快 |

运行方式：`frontmatter_survey.py` 需要 PyYAML（用 conda 的 `dl` 环境），另外两个用系统自带的 python3 就能跑。三个脚本都只读，会读取本机的 skill 目录和会话日志。
