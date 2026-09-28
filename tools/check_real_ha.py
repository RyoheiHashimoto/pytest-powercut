"""本物の Home Assistant の保存処理を電断させて、壊れるかどうかを見る。

pytest は使わない（HA を入れた環境に余計なものを足さないため）。
実行: ./.venv-ha/bin/python tools/check_real_ha.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pytest_powercut.core import explore, mark  # noqa: E402

import homeassistant.util.file as ha_file  # noqa: E402
from homeassistant.util.file import write_utf8_file, write_utf8_file_atomic  # noqa: E402

NAME = "core.restore_state"
V1 = '{"trip_km": 12.3}'
V2 = '{"trip_km": 45.6}'


def describe(got: bytes | None) -> str:
    if got is None:
        return "ファイルが無い"
    if not got:
        return "中身が空"
    if set(got) == {0}:
        return f"{len(got)} バイトすべてゼロ"
    return f"{got[:24]!r} になっている"


def check(disk: Path, marks: list[str]) -> str | None:
    p = disk / NAME
    got = p.read_bytes() if p.exists() else None
    want1, want2 = V1.encode(), V2.encode()
    if "v2" in marks:
        if got != want2:
            return f"v2 を保存したのに {describe(got)}"
    elif "v1" in marks:
        if got not in (want1, want2):
            return f"v1 を保存したのに {describe(got)}"
    elif got is not None and got not in (want1, want2):
        return f"まだ何も保存していないのに {describe(got)}"
    return None


def run(writer):
    def workload(root: Path):
        p = root / NAME
        writer(str(p), V1)
        mark("v1")
        writer(str(p), V2)
        mark("v2")

    return explore(workload, check)


def main() -> int:
    import homeassistant.const as const

    print(f"Home Assistant {const.__version__}（{Path(ha_file.__file__)}）\n")

    targets = [
        ("write_utf8_file（既定。.storage はこちら）", write_utf8_file, None),
        ("write_utf8_file_atomic（atomicwrites 経由）", write_utf8_file_atomic, 0),
    ]
    rows = []
    for name, writer, expect in targets:
        res = run(writer)
        rows.append((name, res.states, res.failures))
        if expect is not None and len(res.failures) != expect:
            print(f"※ {name} は不合格 {expect} 件の見込みだったが {len(res.failures)} 件")

    print(f"{'保存の書き方':<44}{'試した状態':>10}{'壊れた':>8}")
    for name, states, failures in rows:
        print(f"{name:<44}{states:>10}{len(failures):>8}")
    print()
    for name, _, failures in rows:
        if not failures:
            continue
        kinds: dict[str, int] = {}
        for f in failures:
            for k in ("ファイルが無い", "中身が空", "すべてゼロ", "になっている"):
                if k in f.message:
                    kinds[k] = kinds.get(k, 0) + 1
                    break
        promised = sum(1 for f in failures if "を保存したのに" in f.message)
        print(f"■ {name}")
        print(f"   壊れ方の内訳: " + "、".join(f"{k} {n}件" for k, n in kinds.items()))
        print(f"   うち「保存した」と宣言した後に壊れたもの: {promised}件")
        f = failures[0]
        print(f"   例: {f.message}")
        print(f"       （{f.cut_after} の直後に電断）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
