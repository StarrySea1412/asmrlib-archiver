"""信任边界的基础异常类型。

这个模块刻意不依赖包内任何其它模块，这样 ``guards`` 和 ``sites`` 都能安全地
引用同一组异常类，而不会形成双向导入环。``guards`` 会原样再导出这些名字，
所以 ``from asmrlib_archiver.guards import BlockedUrl`` 依旧有效，且与
``from asmrlib_archiver.errors import BlockedUrl`` 是同一个对象。
"""

from __future__ import annotations


class GuardError(RuntimeError):
    pass


class BlockedUrl(GuardError):
    pass


class BlockedRedirect(GuardError):
    pass
