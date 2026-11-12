import os,re,subprocess
H=os.path.expanduser('~'); D=f'{H}/.codex/sessions'
def text(p):
    if p.endswith('.zst'): return subprocess.run(['zstd','-dcq',p],capture_output=True).stdout.decode('utf-8','replace')
    return open(p,encoding='utf-8',errors='replace').read()
rows=[]
for root,_,fs in os.walk(D):
    for f in fs:
        if not f.endswith(('.jsonl','.jsonl.zst')): continue
        p=os.path.join(root,f)
        if os.path.getsize(p)>60_000_000: continue
        t=text(p).replace('\\n','\n').replace('\\"','"')
        i=t.find('### Skill roots')
        if i<0: continue
        j=t.find('### Available skills',i); end=t.find('\n###',j+10)
        roots=dict(re.findall(r'- `(r\d+)` = `([^`]+)`',t[i:j]))
        body=t[j:end]
        files=re.findall(r'\(file: (r\d+)/([^)]+)\)',body)
        fs_=[roots.get(r,'?').replace(H,'~')+'/'+rel for r,rel in files]
        gm=[x for x in fs_ if '/grill-me/' in x or '/find-skills/' in x]
        rows.append((f[8:18],len(fs_),len(body),gm))
rows.sort()
for r in rows: print(r[0],'skills',r[1],'listing chars',r[2],r[3])
