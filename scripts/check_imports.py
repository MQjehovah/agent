import ast, os, glob

src = 'E:/ai/agent/src'
for f in sorted(glob.glob(f'{src}/**/*.py', recursive=True)):
    rel = os.path.relpath(f, src)
    with open(f, encoding='utf-8') as fh:
        for line in fh.readlines():
            s = line.strip()
            if 'from worktree import' in s or 'import worktree' in s:
                print(f'{rel}: {s}')
            if 'from config import' in s or 'from settings import' in s:
                print(f'{rel}: {s}')
