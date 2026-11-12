"""测试用的小工具：在临时目录里造 skill 目录，并给目录拍快照。"""
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'scripts')
sys.path.insert(0, SCRIPTS)


def write(path, text='', mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def link(target, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)
    return path


def skill(name, description, extra=''):
    return '---\nname: %s\ndescription: %s\n%s---\n\n# %s\n' % (name, description, extra, name)


def snapshot(top):
    """记下 top 下每一项的类型、权限、修改时间和内容，用来证明运行前后没有变化。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[os.path.relpath(p, top)] = ('l', os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[os.path.relpath(p, top)] = ('d', st.st_mode, st.st_mtime_ns)
            else:
                with open(p, 'rb') as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                out[os.path.relpath(p, top)] = ('f', st.st_mode, st.st_mtime_ns, digest)
    return out


class TempHome(unittest.TestCase):
    """每个测试一个全新的主目录。"""

    def setUp(self):
        # macOS 的临时目录在 /var 下，/var 是软链接；先取真实路径，免得断言里混进两种写法。
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='skill-tidy-test-'))
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def h(self, *parts):
        return os.path.join(self.home, *parts)
