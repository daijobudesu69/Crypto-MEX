"""Import-order shim. MUST be imported before pandas anywhere in this project.

pyarrow's _compute DLL is blocked by a Windows Application Control policy on this
machine, which makes `import pandas` fail outright (pandas 3.x imports
pyarrow.compute eagerly in its arrow accessors). Poisoning sys.modules['pyarrow']
turns that into a plain ImportError, which pandas' compat layer catches, and
pandas then runs in its no-pyarrow mode.

Consequence: no parquet I/O. Klines are cached as gzipped CSV instead
(documented deviation from spec section 11).
"""
import sys as _sys

_sys.modules.setdefault("pyarrow", None)

import os as _os
_os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
