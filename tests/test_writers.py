"""よくある「状態ファイルの保存」7通りを電断させ、壊れるかどうかを見る。

判定は1つだけ。「読み手が見るのは、書き切ったどれかの版か、何も無いかのどちらか。
いったん『保存した』と言った版は、電断後も必ず読めること」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pytest_powercut.core import explore, mark  # noqa: E402

V1 = b'{"trip_km": 12.3}'
V2 = b'{"trip_km": 45.6}'
NAME = "state.json"


def sync_dir(d: Path):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ 保存のしかた

def naive(p: Path, data: bytes):
    """いちばん素朴。open して書くだけ。"""
    with open(p, "wb") as f:
        f.write(data)


def pathlib_write(p: Path, data: bytes):
    """Path.write_bytes。中身は素朴な書き込みと同じ。"""
    p.write_bytes(data)


def inplace_fsync(p: Path, data: bytes):
    """同じファイルを上書きして fsync する。"""
    with open(p, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    sync_dir(p.parent)


def rename_no_file_sync(p: Path, data: bytes):
    """一時ファイルに書いて rename。一時ファイルを fsync していない。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    sync_dir(p.parent)


def rename_no_dir_sync(p: Path, data: bytes):
    """一時ファイルを fsync して rename。ディレクトリを fsync していない。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_full(p: Path, data: bytes):
    """一時ファイル → fsync → rename → ディレクトリを fsync（pi-obd-meter と同じ手順）。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    sync_dir(p.parent)


def atomicwrites_lib(p: Path, data: bytes):
    """PyPI の atomicwrites（月504万ダウンロード、2022年からアーカイブ）。"""
    from atomicwrites import atomic_write

    with atomic_write(str(p), mode="wb", overwrite=True) as f:
        f.write(data)


def ha_write_utf8_file(p: Path, data: bytes):
    """Home Assistant の既定の書き方（homeassistant/util/file.py を写したもの）。

    一時ファイルに書いて os.replace する。fsync はしない。
    """
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data.decode("utf-8"))
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                os.remove(tmp)
            except OSError:
                pass


WRITERS = [
    ("素の open（fsync なし）", naive, False),
    ("Path.write_bytes", pathlib_write, False),
    ("その場で上書き + fsync", inplace_fsync, False),
    ("rename（一時ファイルを fsync せず）", rename_no_file_sync, False),
    ("rename（ディレクトリを fsync せず）", rename_no_dir_sync, False),
    ("完全なアトミック書き込み", atomic_full, True),
    ("atomicwrites", atomicwrites_lib, None),
    ("Home Assistant の既定", ha_write_utf8_file, None),
]


# ------------------------------------------------------------------ 判定

def check(disk: Path, marks: list[str]) -> str | None:
    p = disk / NAME
    got = p.read_bytes() if p.exists() else None
    if "v2" in marks:
        if got != V2:
            return f"v2 を保存したのに {describe(got)}"
    elif "v1" in marks:
        if got not in (V1, V2):
            return f"v1 を保存したのに {describe(got)}"
    else:
        if got is not None and got not in (V1, V2):
            return f"まだ何も保存していないのに {describe(got)}"
    return None


def describe(got: bytes | None) -> str:
    if got is None:
        return "ファイルが無い"
    if not got:
        return "中身が空"
    if set(got) == {0}:
        return f"{len(got)} バイトすべてゼロ"
    return f"{got[:24]!r} になっている"


def run(writer):
    def workload(root: Path):
        p = root / NAME
        writer(p, V1)
        mark("v1")
        writer(p, V2)
        mark("v2")

    return explore(workload, check)


def test_report():
    """全部の書き方を試して表にする。安全と分かっているものは合格を要求する。"""
    rows = []
    for name, writer, safe in WRITERS:
        res = run(writer)
        rows.append((name, res.states, len(res.failures), res.failures[:1]))
        if safe is True:
            assert not res.failures, f"{name} が壊れた: {res.failures[0]}"
        if safe is False:
            assert res.failures, f"{name} は壊れるはずが、壊れなかった"

    print("\n\n電断後にディスクに残りうる状態を、書き方ごとに試した結果\n")
    print(f"{'書き方':<36}{'試した状態':>10}{'壊れた':>8}")
    for name, states, bad, _ in rows:
        print(f"{name:<36}{states:>10}{bad:>8}")
    print()
    for name, _, bad, sample in rows:
        if sample:
            f = sample[0]
            print(f"■ {name}")
            print(f"   {f.message}")
            print(f"   （{f.cut_after} の直後に電断。{f.state}）")
