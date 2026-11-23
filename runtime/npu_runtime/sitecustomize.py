"""Auto-loaded Python startup hook in ~/.opsb/runtime/sitecustomize.py."""
import os

if os.environ.get("OPSB_ARENA_ACCEL") == "1" or os.environ.get("OPSB_ARENA_PHASE") == "record":
    try:
        import arena_accel
        arena_accel.install_hook()
    except Exception:
        pass
