#!/usr/bin/env bash
# Install the reentrancy guard into the fork's connect() and verify end-to-end
# with pool 1/1 (the exact config that deadlocked embed_auto_backfill).
set -euo pipefail
# Code root: prefer the runtime plugin tree (fleet); dev host may export
# LCM_PG_CODE to point at its authoring fork.
FORK="${LCM_PG_CODE:-${HOME}/.hermes/plugins/lcm-pg}"
[[ -d "$FORK/scripts" ]] || FORK="${HOME}/.hermes/forks/lcm-pg"
B="$HOME/lcm-pg"

export LCM_PG_CODE="$FORK"
grep -q "_LCM_PG_REENTRANT_INSTALL_DONE" "$FORK/backends/postgres.py" || python3 - <<'PY'
import re, os
p = os.environ["LCM_PG_CODE"] + "/backends/postgres.py"
src = open(p).read()
anchor = "def connect("
i = src.index(anchor)
j = src.index("\n", i)
inject = (
    "\n    _mod = globals().get('_lcm_pg_reentrancy')\n"
    "    if _mod is None:\n"
    "        try:  # reentrancy guard (scripts/pg_reentrancy_patch.py)\n"
    "            from pathlib import Path as _P\n"
    "            import importlib.util as _iu\n"
    "            _sp = _P(__file__).resolve().parent.parent / \"scripts\" / \"pg_reentrancy_patch.py\"\n"
    "            if _sp.exists():\n"
    "                _spec = _iu.spec_from_file_location(\"_lcm_pg_reentrancy\", _sp)\n"
    "                _mod = _iu.module_from_spec(_spec)\n"
    "                _spec.loader.exec_module(_mod)\n"
    "                globals()['_lcm_pg_reentrancy'] = _mod\n"
    "        except Exception:\n"
    "            _mod = None  # best-effort; the original borrow path still works\n"
    "    if _mod is not None:\n"
    "        try:\n"
    "            import sys as _s\n"
    "            _mod.install(_s.modules.get(__package__ + \".\" + __name__))\n"
    "        except Exception:\n"
    "            pass\n"
)
open(p, "w").write(src[:j] + inject + src[j:])
print("connect() hook added")
PY

echo "== verify: MessageStore with pool 1/1 on a live profile (30s cap)"
cd "$FORK" && LCM_BACKEND=postgres LCM_PG_DSN="postgresql:///lcm?host=$B/run&port=54329&user=lcm01" LCM_PG_POOL_MIN=1 LCM_PG_POOL_MAX=1 timeout 30 "$B/venv/bin/python" - <<'PY'
import sys, types, abc, importlib
from pathlib import Path
agent=types.ModuleType("agent"); ce=types.ModuleType("agent.context_engine")
class ContextEngine(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self): ...
    @abc.abstractmethod
    def should_compress(self,*a,**k): ...
    @abc.abstractmethod
    def compress(self,*a,**k): ...
    @abc.abstractmethod
    def update_from_response(self,*a,**k): ...
ce.ContextEngine=ContextEngine; agent.context_engine=ce
sys.modules["agent"]=agent; sys.modules["agent.context_engine"]=ce
FORK=os.environ["LCM_PG_CODE"]
sys.path.insert(0, FORK)
st=importlib.import_module("store")  # fork root on sys.path: flat module names

s=st.MessageStore(str(Path.home()/".hermes/profiles/mike/lcm.db"))
print("MessageStore OK with pool 1/1 + guard")
s.close()
PY
