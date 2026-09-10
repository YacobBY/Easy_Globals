"""EasyGlobals — share Python objects between processes at shared-memory speed.

    from EasyGlobals import Globals
    g = Globals()
    g.myvar = 42          # this process becomes the writer of 'myvar'
    print(g.myvar)        # every process can read it, lock-free

No server, no dependencies. See README for semantics (single writer per
variable, ephemeral lifetime) and the migration notes from the 0.1.x
memcached-based releases.
"""
# Explicit re-export (not a star-import): the submodule also defines an
# `EasyGlobals = Globals` compat alias, and star-importing that would shadow
# the submodule on the package — breaking the historical
# `from EasyGlobals import EasyGlobals; g = EasyGlobals.Globals()` style.
from .EasyGlobals import Globals, OwnershipError

__version__ = "0.2.1"
__all__ = ["Globals", "OwnershipError"]
