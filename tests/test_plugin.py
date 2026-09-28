"""仕掛け（fixture）そのものの自己確認。

安全な書き方は通り、危ない書き方はきちんと捕まることを確かめる。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

V1 = b'{"v": 1}'
V2 = b'{"v": 2}'
NAME = "state.json"


def atomic_write(p: Path, data: bytes) -> None:
    """安全な書き方。一時ファイル → fsync → rename → ディレクトリを fsync。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    fd = os.open(p.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def naive_write(p: Path, data: bytes) -> None:
    """危ない書き方。fsync も rename もしない。"""
    with open(p, "wb") as f:
        f.write(data)


def make_workload(powercut, writer):
    def workload(root: Path) -> None:
        p = root / NAME
        writer(p, V1)
        powercut.saved("v1")
        writer(p, V2)
        powercut.saved("v2")

    return workload


def check(disk: Path, saved: list[str]):
    p = disk / NAME
    got = p.read_bytes() if p.exists() else None
    if "v2" in saved:
        assert got == V2, f"v2 を保存したのに {got!r}"
    elif "v1" in saved:
        assert got in (V1, V2), f"v1 を保存したのに {got!r}"
    else:
        assert got is None or got in (V1, V2), f"何も保存していないのに {got!r}"


def test_safe_writer_passes(powercut):
    res = powercut.run(make_workload(powercut, atomic_write), check)
    assert res.states > 0
    assert not res.failures


def test_unsafe_writer_is_caught(powercut):
    with pytest.raises(pytest.fail.Exception) as err:
        powercut.run(make_workload(powercut, naive_write), check)
    assert "電断のあとに壊れた状態が" in str(err.value)


def test_saved_marks_reach_the_check(powercut):
    seen = []

    def workload(root: Path) -> None:
        atomic_write(root / NAME, V1)
        powercut.saved("v1")

    def spy(disk: Path, saved: list[str]):
        seen.append(tuple(saved))
        return None

    powercut.run(workload, spy)
    assert () in seen and ("v1",) in seen
