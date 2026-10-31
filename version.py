"""This apworld's own world_version, and the client-vs-seed version check.

Why this exists (player report 2026-10-12, Shinryu): a 0.1.0 client connected
to a seed generated with 0.2.x. 0.1.0 predates traps, so the first trap id fell
through to the inventory grant as "cat 3328", the grant loop parked the save
counter on it, and 75 items -- the Mystic Key among them -- queued behind it
for hours with nothing on screen saying why.

The world stamps its world_version into slot_data (fill_slot_data); the client
compares it with its own on Connected. Pure functions, no network: the only
question asked is "can this client read what this seed contains", and the seed
itself answers it.

Player-facing strings here follow the no-dash rule (no em/en dash, no "--").
"""
import json
import os
import pkgutil

RELEASES_HINT = "the FF1PSP-Archipelago releases page on GitHub"


def _read_world_version():
    # pkgutil.get_data goes through the package's loader, so it reads the
    # manifest out of the installed .apworld zip as well as from a source
    # checkout (and from the public repo, where the package IS the root).
    raw = None
    try:
        raw = pkgutil.get_data(__package__ or "ff1psp", "archipelago.json")
    except Exception:
        raw = None
    if raw is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "archipelago.json"), "rb") as f:
                raw = f.read()
        except Exception:
            return None
    try:
        return str(json.loads(raw.decode("utf-8"))["world_version"])
    except Exception:
        return None


WORLD_VERSION = _read_world_version()


def parse(v):
    """'0.2.4' -> (0, 2, 4). None for anything that is not dotted integers, so a
    missing or malformed version can never produce a false alarm."""
    if not isinstance(v, str):
        return None
    try:
        parts = tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return None
    return parts if parts else None


def seed_mismatch(client_v, seed_v):
    """Compare this client's world_version with the one the seed was generated
    with. Returns None when there is nothing to say, else (level, message):

      "error"   client OLDER than the generator. A newer generator may place
                items this client has never heard of (exactly the 0.1.0 trap
                case), and one unreadable item blocks every item after it.
      "warning" client on a NEWER major.minor line than the seed. Usually fine
                (ids are append-only), but it is the first thing to rule out.

    Same line with a newer patch is the normal, healthy case: silent. A seed
    from before world_version was stamped (or any unparseable value) is
    silent too: unknown is not evidence of a mismatch."""
    c, s = parse(client_v), parse(seed_v)
    if c is None or s is None:
        return None
    if c < s:
        return ("error",
                f"This multiworld was made with FF1 PSP version {seed_v}, but "
                f"your FF1 PSP is version {client_v}. Some of your items may "
                f"get stuck and never arrive. Close the client and PPSSPP, "
                f"install version {seed_v} or newer from {RELEASES_HINT}, then "
                f"connect again. Your progress is safe.")
    if c[:2] > s[:2]:
        return ("warning",
                f"This multiworld was made with FF1 PSP version {seed_v}, and "
                f"your FF1 PSP is version {client_v}. That usually works fine. "
                f"If items stop arriving, install version {seed_v} from "
                f"{RELEASES_HINT}.")
    return None
