"""Scroll-gated custom party sprites (JobScrollBoosts) -- battle + pause menu.

When a job's scroll is owned, the client overwrites that job's resident battle
sheets (JOBxx.GIM copies in PSP RAM) with custom art shipped in the apworld.
Live-proven 2026-08-12 (see the job-sprite-surfaces-live-vs-baked memory):

  * Battle party sprites AND pause-menu portraits draw LIVE from those resident
    GIMs -- a pixel+palette write shows instantly, no reload needed.
  * The engine restores/reloads the packs from disc constantly (menu open,
    field streaming), so ApClient._jobsprite_loop PINS the art: cheap signature
    check per tick, rewrite whenever vanilla bytes reappear. At 0.25s the pin
    showed zero visible flicker.
  * The FIELD walking sprite is different art entirely (MEV_CMN.PCK ->
    PC<job><0|1>.PCK) baked at map load; RAM writes are inert there. Field art
    is deliberately OUT OF SCOPE for this module (deferred 2026-08-12).
  * Palette-only writes never repaint -- the engine's refresh keys on the
    PIXEL bytes changing. Always write pixels and palette together.

Art files: ff1psp/client/job_sprites/job<XX>.bin, XX = class id in lowercase
hex (job00..job0b) -- zlib of exactly PAL_LEN + PIX_LEN bytes (256-entry
RGBA8888 palette, then 0x7000 GE-swizzled index-8 pixels, 256x112). Built by
re_only/job_sprite_tool.py `build` from an artist PNG; a missing file simply
means that class keeps vanilla art. Shadows (JOBxx_S) are not replaced --
several vanilla shadows are shared between classes, so per-class shadow swaps
would misattribute; silhouette changes ride the main sheet only.

The 64-byte signature window sits at pixel offset +0x80 (NOT +0: the first
rows are transparent padding shared by several sheets). Vanilla signatures
below were extracted from the retail dpk; they identify a resident sheet
without reading the whole 28KB. A custom sheet's own signature is the
"already applied" marker, so the pin never rewrites unnecessarily -- and the
loop recognises its own handiwork after a rescan (the class_names anchor
lesson: never key identification solely on vanilla bytes you overwrite).
"""
import zlib

GIM_MAGIC = b"MIG.00.1PSP"
PAL_OFF, PIX_OFF = 0x80, 0x4d0          # offsets inside a resident JOBxx.GIM
PAL_LEN, PIX_LEN = 0x400, 0x7000
SIG_OFF, SIG_LEN = 0x80, 0x40           # signature window inside the pixels
CLASS_COUNT = 12                        # 0..5 base, 6..11 promoted

# pixels[0x80:0xC0] of each vanilla sheet -- unique across all 12 (verified
# against the retail dpk 2026-08-12; test_job_sprites re-checks when the dpk
# is present).
VANILLA_SIG = {
    0: bytes.fromhex(
        "00000000020202020202020202020200020000021a181716161718191b020000"
        "020002181615151617191a1b020202001a0218161515161718181819191a1b02"),
    1: bytes.fromhex(
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0002020202020202020202000000000002131211101010111213140202000000"),
    2: bytes.fromhex(
        "0000000000000000000000000000000000000002020000000000000000000000"
        "020202180202020202020200000200001b1917181b1a181718191a02021a0200"),
    3: bytes.fromhex(
        "0000000000000000000000000000000000020202020202020202020000000000"
        "021513141413131213141602020202000211101011121213140d090808090b02"),
    4: bytes.fromhex(
        "000000020202020202020202020202020202020f0e0d0c0c0b0a09090a0b0c0d"
        "0d0d1211101008080808080808080809121211101008080808090a0908080808"),
    5: bytes.fromhex(
        "0000000000000000000000000000020200000000000000000000000002021b19"
        "0000000000000000000002021a1817180000000000000000020202181717181a"),
    6: bytes.fromhex(
        "02020000020202020202020200000000180200021a18171617191b0202020000"
        "19020218161515171a1a19190a0202001902181615151718171618090e021b02"),
    7: bytes.fromhex(
        "0000000000000000000000000000000002020202020202020202020000000000"
        "1a191818191a1a19191a1b02020000001616171717181a1a19191a1b1b020000"),
    8: bytes.fromhex(
        "0002020000020202020002000002020202191802021a19191b021902021a1918"
        "1716191a1817191a1b191819171617181617191616181a1b1a1719171617181b"),
    9: bytes.fromhex(
        "00000000000000000000020002020200000202020202020202020d020c090200"
        "02141413121212140d020908080c02000212101011111213090b080a0c020202"),
    10: bytes.fromhex(
        "0000000000000000000000000000000000000000000000000000000000000000"
        "020202020202020202020200000000001b1a1918171616161718190202000000"),
    11: bytes.fromhex(
        "0000000000000000000000000000020200000000000000000000000002021b19"
        "0000000000000000000002021a181718000000000000000002021b181717181c"),
}

_cache = {}          # cls -> (pal, pix) | None, resolved once per process


def sheet(cls):
    """(palette bytes, pixel bytes) of the custom art for class `cls`, or None
    if no file ships for it. Corrupt/mis-sized files log-and-None rather than
    raise: one bad sheet must not kill the whole loop."""
    if cls in _cache:
        return _cache[cls]
    out = None
    try:
        import pkgutil
        raw = pkgutil.get_data(__package__, "job_sprites/job%02x.bin" % cls)
        if raw is not None:
            blob = zlib.decompress(raw)
            if len(blob) == PAL_LEN + PIX_LEN:
                out = (blob[:PAL_LEN], blob[PAL_LEN:])
    except Exception:
        out = None
    _cache[cls] = out
    return out


def custom_sig(cls):
    """Signature window of the custom art (the 'already applied' marker)."""
    got = sheet(cls)
    return got[1][SIG_OFF:SIG_OFF + SIG_LEN] if got else None


def targets_for(owned_jobs):
    """{class id: (pal, pix)} for every scrolled class with shipped art.
    A scroll gates BOTH its base and promoted class (fj and fj+6)."""
    out = {}
    for fj in owned_jobs:
        for cls in (fj, fj + 6):
            got = sheet(cls)
            if got:
                out[cls] = got
    return out
