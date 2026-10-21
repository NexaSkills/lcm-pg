#!/usr/bin/env python3
"""Install the reentrancy guard into the fork's backends/postgres.py.

Idempotent: adds a module-level loader + a call inside connect() that
execs scripts/pg_reentrancy_patch.py (if present) once and applies its
install(). Best-effort by design: any failure leaves the original borrow
path in place. Usage: install_reentrancy_guard.sh runs this, then the
end-to-end pool 1/1 verify lives in the shell script's second half.
"""
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "backends" / "postgres.py"

LOADER = '''

_LCM_PG_REENTRANCY_MOD = None


def _lcm_pg_load_reentrancy():
    """Exec scripts/pg_reentrancy_patch.py once (module-level cache)."""
    global _LCM_PG_REENTRANCY_MOD
    if _LCM_PG_REENTRANCY_MOD is False:
        return None
    if _LCM_PG_REENTRANCY_MOD is not None:
        return _LCM_PG_REENTRANCY_MOD
    try:
        import importlib.util as _iu
        from pathlib import Path as _P
        _sp = _P(__file__).resolve().parent.parent / "scripts" / "pg_reentrancy_patch.py"
        if not _sp.exists():
            _LCM_PG_REENTRANCY_MOD = False
            return None
        _spec = _iu.spec_from_file_location("_lcm_pg_reentrancy", _sp)
        _mod = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _LCM_PG_REENTRANCY_MOD = _mod
        return _mod
    except Exception:
        _LCM_PG_REENTRANCY_MOD = False
        return None
'''

CALL = '''    _guard = _lcm_pg_load_reentrancy()
    if _guard is not None:
        try:
            import sys as _s
            _guard.install(_s.modules.get(__package__ + "." + __name__, None))
        except Exception:
            pass
'''


def main() -> int:
    src = P.read_text()
    if "_lcm_pg_load_reentrancy" in src:
        print("already installed")
        return 0
    # module loader after the imports block: anchor on the first top-level def
    # (keep it above connect()). Place it right before `def connect(`.
    i = src.index("def connect(")
    src = src[:i] + LOADER + "\n\n" + src[i:]
    # call inside connect(): after the opening docstring's first line-block,
    # i.e. right after the first `"""`-terminated docstring in connect.
    i = src.index("def connect(")
    j = src.index('"""', src.index('"""', i + 1) + 3)  # end of docstring
    src = src[:j + 3] + "\n" + CALL + src[j + 3:]
    P.write_text(src)
    import py_compile
    py_compile.compile(str(P), doraise=True)
    print("connect() hook added + compiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
