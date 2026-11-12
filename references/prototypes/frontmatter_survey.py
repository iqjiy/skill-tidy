import os,re,yaml,collections
H=os.path.expanduser('~')
roots=[f'{H}/.claude/skills',f'{H}/.agents/skills',f'{H}/.codex/skills',f'{H}/.claude/plugins/cache',f'{H}/.codex/plugins/cache',f'{H}/.codex/superpowers/skills']
seen=set(); files=[]
for r in roots:
    for root,dirs,fs in os.walk(r,followlinks=True):
        if root[len(r):].count(os.sep)>5: dirs[:]=[]; continue
        dirs[:]=[d for d in dirs if d not in('node_modules','.git','dist','dependency','vendor')]
        if 'SKILL.md' in fs:
            p=os.path.realpath(os.path.join(root,'SKILL.md'))
            if p not in seen: seen.add(p); files.append(p)
feat=collections.Counter(); fails=[]; nofm=[]
for p in files:
    t=open(p,encoding='utf-8',errors='replace').read()
    m=re.match(r'^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)',t,re.S)
    if not m: nofm.append(p); continue
    fm=m.group(1)
    for name,rx in [('block > or |',r':\s*[>|][-+]?\s*$'),('list - item',r'^\s+- '),('nested map',r'^\s+[\w-]+:\s'),('double-quoted',r':\s*"'),('single-quoted',r":\s*'"),('flow [..] or {..}',r':\s*[\[{]'),('anchor/alias',r'[&*]\w'),('comment #',r'^\s*#'),('tab indent',r'^\t')]:
        if re.search(rx,fm,re.M): feat[name]+=1
    try:
        d=yaml.safe_load(fm)
        if not isinstance(d,dict) or not isinstance(d.get('name'),str) or not isinstance(d.get('description'),str): fails.append((p,'name/description missing or not string'))
    except Exception as e: fails.append((p,str(e).split('\n')[0][:80]))
print('SKILL.md files (unique real paths):',len(files),'| no frontmatter:',len(nofm))
print('constructs used:',dict(feat))
print('PyYAML failures:',len(fails))
for p,e in fails: print('  ',p.replace(H,'~'),'|',e)
for p in nofm: print('  no-fm',p.replace(H,'~'))
