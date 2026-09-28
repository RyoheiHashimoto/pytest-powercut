"""電断の後にディスクに残りうる状態を作り、保存処理が壊れないかを試す（試作）。

使う側のコードは変えない。指定したフォルダの中への書き込みだけを横取りして、
「何をどこに書いたか」「いつ fsync したか」「いつ rename したか」を記録する。
そのあと、電源が落ちた時点ごとに、ディスクに残りうる状態を作り直す。

模擬する壊れ方（実機の ext4 で観測した形に合わせてある）:
  - fsync していない書き込みは、消える / 残る / 途中まで残る
  - ファイルの大きさだけが残り、中身がゼロになる（Raspberry Pi の電断後に実測した形）
  - ディレクトリの変更（作成・rename・削除）は、ディレクトリを fsync するまで確定しない。
    確定していない分は、後ろから順に失われる（途中だけ失われることはない）
"""

from __future__ import annotations

import builtins
import io
import os
import stat as _stat_mod
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl as _fcntl   # macOS の atomicwrites は fcntl(F_FULLFSYNC) を使う
except ImportError:   # Windows
    _fcntl = None

# 横取りする前の本物。偽物の中からはこちらを呼ぶ（偽物が偽物を呼んで無限再帰になるのを防ぐ）
_real_stat = os.stat
_real_realpath = os.path.realpath


def _mode_flags(mode: str) -> int:
    """io.open が opener に渡すフラグを、mode から組み立てる。"""
    if "+" in mode:
        flags = os.O_RDWR
    elif any(c in mode for c in "wax"):
        flags = os.O_WRONLY
    else:
        flags = os.O_RDONLY
    if "w" in mode:
        flags |= os.O_CREAT | os.O_TRUNC
    if "a" in mode:
        flags |= os.O_CREAT | os.O_APPEND
    if "x" in mode:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _is_real_dir(path) -> bool:
    """本物の stat だけでディレクトリか判定する（os.path.isdir は偽物の os.stat を呼ぶので使えない）。"""
    try:
        return _stat_mod.S_ISDIR(_real_stat(path).st_mode)
    except (OSError, ValueError):
        return False

CREATE, DATA, FSYNC, DIRSYNC, RENAME, REMOVE, MARK = (
    "create", "data", "fsync", "dirsync", "rename", "remove", "mark",
)


@dataclass
class Event:
    kind: str
    name: str = ""
    new_name: str = ""
    ino: int = -1
    data: bytes = b""
    label: str = ""

    def describe(self) -> str:
        if self.kind == CREATE:
            return f"{self.name} を作る（inode {self.ino}）"
        if self.kind == DATA:
            return f"inode {self.ino} に書く（{len(self.data)} バイトになった）"
        if self.kind == FSYNC:
            return f"inode {self.ino} を fsync"
        if self.kind == DIRSYNC:
            return "ディレクトリを fsync"
        if self.kind == RENAME:
            return f"{self.name} を {self.new_name} へ rename"
        if self.kind == REMOVE:
            return f"{self.name} を削除"
        return f"「{self.label}」を約束"


class _FakeFile:
    """横取りしたファイル。書き込みは記録され、fsync するまで確定しない。"""

    def __init__(self, disk: "FakeDisk", ino: int, mode: str, name: str = "", encoding: str = "utf-8"):
        self.disk = disk
        self.ino = ino
        self.name = name
        self.encoding = encoding or "utf-8"
        self.binary = "b" in mode
        self.append = "a" in mode
        self.closed = False
        self.pos = len(disk.files[ino]) if self.append else 0
        self._fd = disk._alloc_fd(("file", ino))

    # --- 書き込み
    def write(self, s):
        chars = len(s) if isinstance(s, str) else None
        if isinstance(s, str):
            s = s.encode(self.encoding)
        cur = self.disk.files[self.ino]
        off = len(cur) if self.append else self.pos
        new = bytearray(cur)
        if off > len(new):
            new.extend(b"\0" * (off - len(new)))
        new[off:off + len(s)] = s
        self.disk._set_data(self.ino, bytes(new))
        self.pos = off + len(s)
        return len(s) if chars is None else chars

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    # --- 読み取り
    def read(self, n=-1):
        data = self.disk.files[self.ino][self.pos:]
        if self.binary:
            if n is not None and n >= 0:
                data = data[:n]
            self.pos += len(data)
            return data
        text = data.decode(self.encoding)
        if n is not None and n >= 0:
            text = text[:n]       # テキストでは n は文字数（バイト数ではない）
        self.pos += len(text.encode(self.encoding))
        return text

    def readline(self):
        data = self.disk.files[self.ino][self.pos:]
        idx = data.find(b"\n")
        chunk = data if idx < 0 else data[:idx + 1]
        self.pos += len(chunk)
        return chunk if self.binary else chunk.decode(self.encoding)

    def seek(self, pos, whence=0):
        size = len(self.disk.files[self.ino])
        self.pos = pos if whence == 0 else (self.pos + pos if whence == 1 else size + pos)
        return self.pos

    def tell(self):
        return self.pos

    def truncate(self, size=None):
        size = self.pos if size is None else size
        cur = self.disk.files[self.ino]
        data = cur[:size] if size <= len(cur) else cur + b"\0" * (size - len(cur))
        self.disk._set_data(self.ino, data)
        return size

    # --- その他
    def flush(self):
        pass

    def fileno(self):
        return self._fd

    def close(self):
        if not self.closed:
            self.closed = True
            self.disk.fds.pop(self._fd, None)
            getattr(self.disk, "_open_files", {}).pop(self._fd, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


class FakeDisk:
    """root 以下だけを横取りするディスク。"""

    def __init__(self, root: str):
        self.root = _real_realpath(root)
        self.entries: dict[str, int] = {}
        self.files: list[bytes] = []
        self.events: list[Event] = []
        self.fds: dict[int, tuple[str, int]] = {}
        self.unsupported: list[str] = []
        self._next_fd = 900001

    # --- 内部
    def _alloc_fd(self, what):
        fd = self._next_fd
        self._next_fd += 1
        self.fds[fd] = what
        return fd

    def _norm(self, path) -> str:
        return _real_realpath(os.fspath(path))

    def inside(self, path) -> bool:
        try:
            p = self._norm(path)
        except (TypeError, ValueError):
            return False
        return p == self.root or p.startswith(self.root + os.sep)

    def _set_data(self, ino: int, data: bytes):
        self.files[ino] = data
        self.events.append(Event(DATA, ino=ino, data=data))

    def _create(self, name: str) -> int:
        ino = len(self.files)
        self.files.append(b"")
        self.entries[name] = ino
        self.events.append(Event(CREATE, name=name, ino=ino))
        return ino

    # --- 横取りする操作
    def open(self, path, mode="r", *args, **kwargs):
        enc = kwargs.get("encoding") or (args[1] if len(args) > 1 else None) or "utf-8"
        name = self._norm(path)
        exists = name in self.entries
        if not exists:
            if any(c in mode for c in "wxa+"):
                ino = self._create(name)
            else:
                raise FileNotFoundError(2, "No such file or directory", str(path))
        else:
            ino = self.entries[name]
            if "x" in mode:
                raise FileExistsError(17, "File exists", str(path))
        if "w" in mode and self.files[ino]:
            self._set_data(ino, b"")
        return _FakeFile(self, ino, mode, name, enc)

    def os_open(self, path, flags, mode=0o777):
        name = self._norm(path)
        if name == self.root or _is_real_dir(name):
            return self._alloc_fd(("dir", -1))
        if name not in self.entries:
            if flags & os.O_CREAT:
                self._create(name)
            else:
                raise FileNotFoundError(2, "No such file or directory", str(path))
        elif flags & os.O_EXCL:
            raise FileExistsError(17, "File exists", str(path))
        ino = self.entries[name]
        if flags & os.O_TRUNC and self.files[ino]:
            self._set_data(ino, b"")
        f = _FakeFile(self, ino, "a+b" if flags & os.O_APPEND else "r+b", name)
        self._open_files[f.fileno()] = f
        return f.fileno()

    def rename(self, src, dst):
        s, d = self._norm(src), self._norm(dst)
        if s not in self.entries:
            raise FileNotFoundError(2, "No such file or directory", str(src))
        self.entries[d] = self.entries.pop(s)
        self.events.append(Event(RENAME, name=s, new_name=d))

    def remove(self, path):
        p = self._norm(path)
        if p not in self.entries:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        del self.entries[p]
        self.events.append(Event(REMOVE, name=p))

    def fsync(self, fd):
        what = self.fds.get(fd)
        if what is None:
            raise OSError(9, "Bad file descriptor")
        kind, ino = what
        self.events.append(Event(DIRSYNC) if kind == "dir" else Event(FSYNC, ino=ino))

    def mark(self, label: str):
        self.events.append(Event(MARK, label=label))


class _Patcher:
    """root 以下への操作だけを FakeDisk へ回す。ほかは本物へそのまま通す。"""

    def __init__(self, disk: FakeDisk):
        self.disk = disk
        self.saved: list[tuple[object, str, object]] = []

    def _patch(self, obj, attr, new):
        self.saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, new)

    def __enter__(self):
        disk = self.disk
        disk._open_files = {}
        real_open, real_io_open = builtins.open, io.open
        real = {n: getattr(os, n) for n in
                ("open", "close", "fsync", "replace", "rename", "remove", "unlink", "fdopen", "stat", "write", "fstat")
                if hasattr(os, n)}
        if hasattr(os, "fdatasync"):
            real["fdatasync"] = os.fdatasync

        def fake_open(file, mode="r", *a, **kw):
            opener = kw.get("opener")
            if opener is not None:
                # io.open は opener にファイルの作成を任せ、渡されたパスは使わない。
                # tempfile.NamedTemporaryFile はこの形で「フォルダ＋opener」を渡してくる
                fd = opener(file, _mode_flags(mode))
                f = disk._open_files.get(fd)
                if f is not None:
                    f.binary = "b" in mode
                    return f
                kw = {k: v for k, v in kw.items() if k != "opener"}
                return real_open(fd, mode, *a, **kw)
            if isinstance(file, int):   # tempfile は番号（fd）で開き直す
                f = disk._open_files.get(file)
                if f is not None:
                    f.binary = "b" in mode
                    return f
                return real_open(file, mode, *a, **kw)
            if disk.inside(file):
                return disk.open(file, mode, *a, **kw)
            return real_open(file, mode, *a, **kw)

        def fake_os_open(path, flags, mode=0o777, **kw):
            if disk.inside(path):
                return disk.os_open(path, flags, mode)
            return real["open"](path, flags, mode, **kw)

        def fake_close(fd):
            if fd in disk.fds or fd in disk._open_files:
                f = disk._open_files.pop(fd, None)
                if f is not None:
                    f.close()
                disk.fds.pop(fd, None)
                return
            return real["close"](fd)

        def fake_fsync(fd):
            if fd in disk.fds:
                return disk.fsync(fd)
            return real["fsync"](fd)

        def fake_rename(src, dst, **kw):
            if disk.inside(src) or disk.inside(dst):
                return disk.rename(src, dst)
            return real["rename"](src, dst, **kw)

        def fake_replace(src, dst, **kw):
            if disk.inside(src) or disk.inside(dst):
                return disk.rename(src, dst)
            return real["replace"](src, dst, **kw)

        def fake_remove(path, **kw):
            if disk.inside(path):
                return disk.remove(path)
            return real["remove"](path, **kw)

        def fake_fdopen(fd, mode="r", *a, **kw):
            f = disk._open_files.get(fd)
            if f is not None:
                f.binary = "b" in mode
                return f
            return real["fdopen"](fd, mode, *a, **kw)

        def _fake_stat_result(size):
            st = list(_real_stat(__file__))
            st[6] = size
            return os.stat_result(st)

        def fake_fstat(fd):
            what = disk.fds.get(fd)
            if what is not None:
                kind, ino = what
                return _real_stat(disk.root) if kind == "dir" else _fake_stat_result(len(disk.files[ino]))
            return real["fstat"](fd)

        def fake_stat(path, **kw):
            if kw.get("dir_fd") is None and not isinstance(path, int) and disk.inside(path):
                name = disk._norm(path)
                if name in disk.entries:
                    return _fake_stat_result(len(disk.files[disk.entries[name]]))
                if name == disk.root or _is_real_dir(name):
                    return _real_stat(name)
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real["stat"](path, **kw)

        def fake_write(fd, data):
            f = disk._open_files.get(fd)
            if f is not None:
                return f.write(data)
            return real["write"](fd, data)

        self._patch(builtins, "open", fake_open)
        self._patch(io, "open", fake_open)
        self._patch(os, "open", fake_os_open)
        self._patch(os, "close", fake_close)
        self._patch(os, "fsync", fake_fsync)
        if "fdatasync" in real:
            self._patch(os, "fdatasync", fake_fsync)
        self._patch(os, "rename", fake_rename)
        self._patch(os, "replace", fake_replace)
        self._patch(os, "remove", fake_remove)
        self._patch(os, "unlink", fake_remove)
        self._patch(os, "fdopen", fake_fdopen)
        self._patch(os, "stat", fake_stat)
        self._patch(os, "fstat", fake_fstat)
        self._patch(os, "write", fake_write)

        if hasattr(os, "fchmod"):
            real_fchmod = os.fchmod

            def fake_fchmod(fd, mode, **kw):
                if fd in disk.fds:
                    return None
                return real_fchmod(fd, mode, **kw)

            self._patch(os, "fchmod", fake_fchmod)

        real_chmod = os.chmod

        def fake_chmod(path, mode, **kw):
            if not isinstance(path, int) and disk.inside(path):
                return None
            return real_chmod(path, mode, **kw)

        self._patch(os, "chmod", fake_chmod)

        if _fcntl is not None:
            real_fcntl = _fcntl.fcntl
            syncs = tuple(c for c in (getattr(_fcntl, "F_FULLFSYNC", None),
                                      getattr(_fcntl, "F_BARRIERFSYNC", None)) if c is not None)

            def fake_fcntl(fd, cmd, *a):
                if fd in disk.fds:
                    if cmd in syncs:
                        disk.fsync(fd)
                    return 0
                return real_fcntl(fd, cmd, *a)

            self._patch(_fcntl, "fcntl", fake_fcntl)
        return disk

    def __exit__(self, *exc):
        for obj, attr, old in reversed(self.saved):
            setattr(obj, attr, old)
        self.saved.clear()
        return False


# ------------------------------------------------------------------ 電断後の状態

@dataclass
class CrashState:
    entries: dict[str, bytes]
    desc: str
    marks: list[str] = field(default_factory=list)


def _common_prefix(a: bytes, b: bytes) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _candidates(synced: bytes, since: list[bytes]) -> list[tuple[bytes, str]]:
    """電断のあとに、そのファイルが持ちうる中身を並べる。"""
    out = [(synced, "最後に fsync したまま")]
    seen = {synced}
    intact = len(synced)
    prev = synced
    for i, v in enumerate(since, 1):
        intact = min(intact, _common_prefix(synced, v))
        label = f"{i}/{len(since)} 回目の未 fsync の書き込み"
        if v not in seen:
            seen.add(v)
            out.append((v, f"{label}が残った"))
        k = _common_prefix(prev, v)
        cut = k + max(1, (max(len(prev), len(v)) - k) // 2)
        torn = v[:cut] + prev[cut:]     # 前半は新しい内容、後半は古い内容のまま
        if torn not in seen:
            seen.add(torn)
            out.append((torn, f"{label}が途中まで書かれた"))
        zero = bytearray(len(v))
        zero[:intact] = synced[:intact]
        z = bytes(zero)
        if z not in seen:
            seen.add(z)
            out.append((z, f"{label}の大きさだけ残り、中身がゼロ"))
        prev = v
    return out


def crash_states(events: list[Event], limit: int = 200) -> list[CrashState]:
    """events の直後に電源が落ちたとして、ディスクに残りうる状態を並べる。"""
    meta: list[Event] = []
    durable = 0
    synced: dict[int, bytes] = {}
    since: dict[int, list[bytes]] = {}
    marks: list[str] = []
    for e in events:
        if e.kind == CREATE:
            meta.append(e)
            synced[e.ino] = b""
            since[e.ino] = []
        elif e.kind in (RENAME, REMOVE):
            meta.append(e)
        elif e.kind == DIRSYNC:
            durable = len(meta)
        elif e.kind == DATA:
            since.setdefault(e.ino, []).append(e.data)
        elif e.kind == FSYNC:
            vs = since.get(e.ino) or []
            if vs:
                synced[e.ino] = vs[-1]
            since[e.ino] = []
        elif e.kind == MARK:
            marks.append(e.label)

    out: list[CrashState] = []
    for keep in range(durable, len(meta) + 1):
        entries: dict[str, int] = {}
        for e in meta[:keep]:
            if e.kind == CREATE:
                entries[e.name] = e.ino
            elif e.kind == RENAME:
                if e.name in entries:
                    entries[e.new_name] = entries.pop(e.name)
            elif e.kind == REMOVE:
                entries.pop(e.name, None)
        inos = sorted(set(entries.values()))
        options = [_candidates(synced.get(i, b""), since.get(i, [])) for i in inos]
        total = 1
        for o in options:
            total *= len(o)
        pick = [0] * len(inos)
        while True:
            files = {}
            desc = []
            for i, ino in enumerate(inos):
                data, why = options[i][pick[i]]
                files[ino] = data
                desc.append(f"inode {ino} は{why}")
            out.append(CrashState(
                entries={name: files[ino] for name, ino in entries.items()},
                desc=f"ディレクトリの変更を {keep}/{len(meta)} だけ残す。" + "、".join(desc),
                marks=list(marks),
            ))
            if len(out) >= limit:
                return out
            i = 0
            while i < len(pick):
                pick[i] += 1
                if pick[i] < len(options[i]):
                    break
                pick[i] = 0
                i += 1
            if i == len(pick):
                break
    return out


# ------------------------------------------------------------------ 使う側の入口

@dataclass
class Failure:
    cut_after: str
    state: str
    message: str


@dataclass
class Result:
    states: int = 0
    failures: list[Failure] = field(default_factory=list)
    capped: int = 0   # 状態が多すぎて打ち切った電断の時点の数


_current: FakeDisk | None = None


def mark(label: str):
    """「ここまでは確実に保存した」と宣言する。電断後の確認で使う。"""
    if _current is not None:
        _current.mark(label)


def explore(workload, check, *, limit_per_cut: int = 200) -> Result:
    """workload を1回動かし、あらゆる時点での電断を再現して check にかける。

    workload(root: Path) — ファイルを書く処理。root の中だけが横取りされる。
    check(disk: Path, marks: list[str]) -> str | None — 壊れていれば理由を返す。
    """
    global _current
    res = Result()
    with tempfile.TemporaryDirectory() as run_root:
        disk = FakeDisk(run_root)
        _current = disk
        try:
            with _Patcher(disk):
                workload(Path(run_root))
        finally:
            _current = None
        events = disk.events

    for cut in range(len(events) + 1):
        last = "開始前" if cut == 0 else events[cut - 1].describe()
        states = crash_states(events[:cut], limit_per_cut)
        if len(states) >= limit_per_cut:
            res.capped += 1
        for st in states:
            res.states += 1
            with tempfile.TemporaryDirectory() as disk_root:
                for name, data in st.entries.items():
                    dest = Path(disk_root) / os.path.basename(name)
                    dest.write_bytes(data)
                msg = check(Path(disk_root), st.marks)
            if msg:
                res.failures.append(Failure(last, st.desc, msg))
    return res
