"""Watch the installed .apworld for a mid-session rebuild.

Why this exists (2026-08-08 incident): the player launched the client at
20:03, the apworld was rebuilt+installed at 20:38, and the connect at 20:39
died with

    zipimport.ZipImportError: bad local file header: '...ff1psp.apworld'

zipimport reads the zip's central directory ONCE, at first import, and caches
every entry offset. Replacing the file under a live process leaves those
offsets pointing into the new bytes, so the next LAZY import (here:
`_start_bridge`'s `from .launcher import ensure_ppsspp`) reads garbage. The
bridge task died, nothing launched PPSSPP, and the client just sat there.

Nothing can recover this in-process -- zipimport has no supported way to drop
its cache and re-read. The only fix is a fresh process. So: notice the swap,
and while it is still safe (bridge not up yet) relaunch ourselves and
reconnect to the same server/slot. Once the game IS running we do not yank the
session out from under the player; we warn instead.
"""
import asyncio
import os
import subprocess
import sys

try:
    from CommonClient import logger
except Exception:                       # offline unit tests: no AP on sys.path
    import logging
    logger = logging.getLogger("Client")

POLL_SECONDS = 3.0


def apworld_path():
    """Absolute path of the .apworld zip we were imported from, or None.

    When installed, __file__ is
    ``...\\custom_worlds\\ff1psp.apworld\\ff1psp\\client\\apworld_watch.py``:
    a path whose middle component is a real FILE. Running from a source
    checkout there is no such component, and the watcher stays off (a source
    tree is not swapped atomically and imports come from real files).
    """
    p = os.path.abspath(__file__)
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
        if os.path.isfile(p):
            return p


def _stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _launcher_argv(ctx):
    """Command that reopens THIS client, pre-filled to reconnect.

    Mirrors the documented entry point in ff1psp/__init__.py:
        ArchipelagoLauncher.exe "Final Fantasy 1 PSP Client" -- <url> <slot>
    The password is never put on the command line (it would show up in the
    process list); it rides an env var the child reads once.
    """
    import Utils
    name = "Final Fantasy 1 PSP Client"
    url = ctx.server_address or ""
    slot = ctx.auth or ""
    exe = None
    for cand in ("ArchipelagoLauncher.exe", "ArchipelagoLauncher"):
        try:
            path = Utils.local_path(cand)
        except Exception:
            continue
        if os.path.isfile(path):
            exe = path
            break
    if exe:
        argv = [exe, name, "--"]
    else:
        # Source checkout / non-frozen: drive the same component via Launcher.py.
        launcher = None
        try:
            launcher = Utils.local_path("Launcher.py")
        except Exception:
            pass
        if not launcher or not os.path.isfile(launcher):
            return None
        argv = [sys.executable, launcher, name, "--"]
    if url:
        argv.append(url)
        if slot:
            argv.append(slot)
    return argv


def _respawn(ctx):
    argv = _launcher_argv(ctx)
    if not argv:
        logger.error("  [apworld] cannot find the Archipelago Launcher to "
                     "restart with -- close and reopen the client yourself.")
        return False
    env = dict(os.environ)
    if ctx.password:
        env["FF1PSP_RESTART_PASSWORD"] = ctx.password
    # Detach: the child must outlive us, and must not inherit our console.
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8) |
                               getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                                       0x200))
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, env=env, close_fds=True, **kw)
    except Exception as e:
        logger.error(f"  [apworld] restart failed to spawn ({e}) -- close and "
                     "reopen the client yourself.")
        return False
    return True


async def watch(ctx):
    """Poll the apworld's mtime+size; react once if it changes."""
    path = apworld_path()
    if not path:
        return                      # source checkout -- nothing to watch
    baseline = _stamp(path)
    if baseline is None:
        return
    warned = False
    while not ctx.exit_event.is_set():
        try:
            await asyncio.wait_for(ctx.exit_event.wait(), POLL_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        now = _stamp(path)
        if now is None or now == baseline:
            continue
        # The zip we were imported from is gone/replaced. Every import we have
        # not already done is now poisoned.
        if getattr(ctx, "_bridge_started", False):
            if not warned:
                warned = True
                baseline = now
                logger.error(
                    "  [apworld] ff1psp.apworld was REBUILT on disk while this "
                    "client is running. The game is already up, so I will NOT "
                    "restart and interrupt your session -- but this process is "
                    "still running the OLD code and any not-yet-loaded part of "
                    "it will fail. Finish what you are doing, then close and "
                    "reopen the client.")
            continue
        logger.error(
            "  [apworld] ff1psp.apworld was REBUILT on disk while this client "
            "is running. Python cannot reload a swapped zip, so the bridge "
            "would crash with 'bad local file header'. Restarting the client "
            "now and reconnecting to the same server/slot…")
        if _respawn(ctx):
            ctx.exit_event.set()
        return
