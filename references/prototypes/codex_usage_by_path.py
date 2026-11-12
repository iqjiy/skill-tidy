import os,re,time,collections,subprocess,shutil
H=os.path.expanduser('~'); D=f'{H}/.codex/sessions'
rx=re.compile(rb'(/Users/[^"\\\s]{1,200}?/SKILL\.md)')
def lines(p):
    if p.endswith('.zst'):
        pr=subprocess.Popen(['zstd','-dcq',p],stdout=subprocess.PIPE); yield from pr.stdout; pr.wait()
    else:
        with open(p,'rb') as fh: yield from fh
t=time.time(); per=collections.Counter(); files=0; earliest=None
for root,_,fs in os.walk(D):
    for f in fs:
        if not (f.endswith('.jsonl') or f.endswith('.jsonl.zst')): continue
        files+=1; seen=set()
        for line in lines(os.path.join(root,f)):
            if b'SKILL.md' in line and (b'exec_command' in line or b'function_call' in line):
                if b'### Skill roots' in line: continue
                for m in rx.finditer(line): seen.add(m.group(1).decode('utf-8','replace'))
        per.update(seen)
print(f'codex {files} files (incl .zst) in {time.time()-t:.1f}s, zstd={bool(shutil.which("zstd"))}')
for k in ['brainstorming','executing-plans','writing-plans','skill-creator','/pdf/','last30days']:
    hits=[(p.replace(H,'~'),c) for p,c in per.items() if k in p]
    print(k, hits if hits else 'never read')
