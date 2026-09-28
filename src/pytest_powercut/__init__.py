"""pytest-powercut: 電断のあとにファイルが壊れないかを試す。

使い方は README を参照。pytest の中では `powercut` という仕掛け（fixture）が使える。
"""

from .core import Failure, Result, explore, mark

__all__ = ["Failure", "Result", "explore", "mark", "__version__"]
__version__ = "0.1.0"
