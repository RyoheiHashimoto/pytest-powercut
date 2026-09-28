"""pytest の仕掛け（fixture）。テストの中で `powercut` として受け取る。"""

from __future__ import annotations

from typing import Callable, List, Optional

import pytest

from .core import Result, explore, mark


def _report(res: Result, limit: int = 5) -> str:
    lines = [
        f"電断のあとに壊れた状態が {len(res.failures)} 件ありました"
        f"（試した状態 {res.states} 件）。",
        "",
    ]
    seen = set()
    for f in res.failures:
        if f.message in seen:
            continue
        seen.add(f.message)
        lines.append(f"  - {f.message}")
        lines.append(f"      {f.cut_after} の直後に電断")
        lines.append(f"      {f.state}")
        if len(seen) >= limit:
            break
    if len(seen) < len({f.message for f in res.failures}):
        lines.append("  （ほかの壊れ方は省略）")
    if res.capped:
        lines.append(
            f"  ※ 状態が多すぎて {res.capped} 箇所で打ち切りました。"
            "limit_per_cut を上げると、さらに調べられます"
        )
    return "\n".join(lines)


class PowerCut:
    """1つのテストの中で使う入口。"""

    def __init__(self) -> None:
        self.result: Optional[Result] = None

    def saved(self, label: str) -> None:
        """「ここまでは確実に保存した」と宣言する。電断後の確認で使う。"""
        mark(label)

    def run(
        self,
        workload: Callable[..., None],
        check: Callable[..., Optional[str]],
        *,
        limit_per_cut: int = 200,
    ) -> Result:
        """workload を1回動かし、あらゆる時点での電断を再現して check にかける。

        workload(root: Path) — ファイルを書く処理。root の中だけが横取りされる。
        check(disk: Path, saved: list[str]) — 電断後のディスクを見て、
            壊れていれば理由の文字列を返すか、assert で落とす。
        壊れた状態が1つでもあれば、このテストは失敗する。
        """

        def _check(disk, saved: List[str]) -> Optional[str]:
            try:
                return check(disk, saved)
            except AssertionError as err:
                return str(err) or "assert が失敗した"

        self.result = explore(workload, _check, limit_per_cut=limit_per_cut)
        if self.result.failures:
            pytest.fail(_report(self.result), pytrace=False)
        return self.result


@pytest.fixture
def powercut() -> PowerCut:
    """電断のあとにファイルが壊れないかを試す。"""
    return PowerCut()
