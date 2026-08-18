"""Route-2 core: plaintext-ELF code patcher for FF1 PSP.

Facts proven during feasibility test (2026-06-30):
  * PSP_GAME/SYSDIR/BOOT.BIN is a PLAINTEXT MIPS32-LE ELF (magic 7f 45 4c 46).
    EBOOT.BIN is the ~PSP (KIRK) encrypted copy. No decryption needed.
  * Single PT_LOAD, ET_EXEC: RAM<->file map is linear:
        file = 0x54 + (ram - 0x8804000)   for ram in [0x8804000, 0x89cacc0)
  * Segment is RWX; memsz(0x32ce00) > filesz(0x1c6cc0): 1.4MB BSS reserved.
    Code caves append at ram >= 0x8b30e00 by growing filesz & memsz.
  * Plaintext ELF (0x1c7234) < encrypted EBOOT slot (0x1c7390): a no-cave
    patched ELF drops into the EBOOT.BIN slot IN-PLACE (no LBA shift).

PPSSPP loads a \x7fELF placed in the EBOOT.BIN slot directly (KIRK decrypt is
only triggered by ~PSP magic). Cave-bearing (larger) builds go through
build_iso -> _relocate_eboot (the grow-by-append repacker below), which moves
a grown ELF to a fresh extent and rewrites the directory record + PVD.
"""
import struct, os, shutil

ISO_DEFAULT = r"C:\ff1 psp ap main\PPSSPP\PSP\GAME\Final Fantasy Original - 20th Anniversary Edition (USA) (En,Ja) (FW3.03).iso"
SECTOR = 2048
VADDR = 0x8804000
SEG_FILE_OFF = 0x54          # PH0 p_offset within the ELF
FILESZ = 0x1c6cc0
MEMSZ = 0x32ce00

# ISO layout (from iso_extract walker)
EBOOT_ISO_OFF = 0x10000      # SYSDIR/EBOOT.BIN, LBA 32
EBOOT_SLOT = 0x1c7390
BOOT_ISO_OFF = 0x29e0000     # SYSDIR/BOOT.BIN (plaintext ELF)
BOOT_SIZE = 0x1c7234
# Sanity ceiling for a cave-grown ELF. Caves may now push the ELF past EBOOT_SLOT
# because build_iso relocates a grown ELF to a fresh extent (grow-by-append). This
# is only a runaway backstop, far above any real bake.
MAX_ELF = 0x400000


def ram2file(ram):
    """Map a runtime RAM address to a byte offset inside the ELF image."""
    if not (VADDR <= ram < VADDR + FILESZ):
        raise ValueError(f"{ram:#x} not in in-file code range "
                         f"[{VADDR:#x},{VADDR+FILESZ:#x})")
    return SEG_FILE_OFF + (ram - VADDR)


def _iso_find_boot_bins(f):
    """Every BOOT.BIN on the disc, as [(path, file_offset, size), ...].

    BOOT_ISO_OFF is the offset on the ISO this project was RE'd against
    ("(USA) (En,Ja) (FW3.03)"). Nothing guarantees another dump puts the file
    at the same LBA, so seeking the constant can read unrelated bytes -- which
    is how a player's dump reported "BOOT.BIN not a plaintext ELF" on
    2026-08-08.

    ALL of them, not the first: a disc that bundles a firmware updater carries
    a SECOND BOOT.BIN under PSP_GAME/SYSDIR/UPDATE/ (that is what the "(FW6.00)"
    in a Redump name refers to), and directory order does not promise the game's
    own copy comes first. The caller picks the one that is actually a game ELF.

    NOTE: this only fixes WHERE we read the executable from. Every patch RAM
    address in this project is statically RE'd against one build, so
    load_boot_elf still gates on the build fingerprint below."""
    out = []
    try:
        f.seek(_PVD_SECTOR * SECTOR)
        pvd = f.read(SECTOR)
        if len(pvd) < SECTOR or pvd[0] != 1 or pvd[1:6] != _ISO9660_ID:
            return out
        root = pvd[156:156 + 34]
        lba = struct.unpack_from("<I", root, 2)[0]
        size = struct.unpack_from("<I", root, 10)[0]

        def walk(lba, size, depth, path):
            f.seek(lba * SECTOR)
            data = f.read(size)
            p = 0
            while p + 33 <= len(data):
                rl = data[p]
                if rl == 0:
                    p = (p // SECTOR + 1) * SECTOR
                    continue
                elba = struct.unpack_from("<I", data, p + 2)[0]
                elen = struct.unpack_from("<I", data, p + 10)[0]
                flags = data[p + 25]
                nlen = data[p + 32]
                name = bytes(data[p + 33:p + 33 + nlen])
                p += rl
                if name in (b"\x00", b"\x01"):
                    continue
                nm = name.split(b";")[0].decode("latin1", "replace")
                if flags & 2:
                    if depth < 4:
                        walk(elba, elen, depth + 1, f"{path}/{nm}")
                elif nm.upper() == "BOOT.BIN":
                    out.append((f"{path}/{nm}", elba * SECTOR, elen))

        walk(lba, size, 0, "")
    except (OSError, struct.error):
        pass
    return out


# Words that identify the build every patch address in this project was RE'd
# against, sampled from the plaintext BOOT.BIN at (elf_offset, expected bytes).
# A same-size ELF from a different revision would silently take every patch at
# the wrong address; these make that a clean refusal instead.
_BUILD_SIG = (
    (0x18, b"\x28\x65\x81\x08"),                  # e_entry  = 0x08816528
    (0x1c, struct.pack("<I", 0x34)),              # e_phoff  = 0x34
    (0x34 + 0x04, struct.pack("<I", SEG_FILE_OFF)),   # PH0 p_offset = 0x54
    (0x34 + 0x08, struct.pack("<I", VADDR)),          # PH0 p_vaddr
    (0x34 + 0x10, struct.pack("<I", FILESZ)),         # PH0 p_filesz
    (0x34 + 0x14, struct.pack("<I", MEMSZ)),          # PH0 p_memsz
)


def _build_matches(elf):
    return len(elf) == BOOT_SIZE and all(
        bytes(elf[o:o + len(b)]) == b for o, b in _BUILD_SIG)


CHECKISO_HINT = ("  Type /checkiso in the client for a plain-English "
                 "report on the ISO you are already using, and what to do "
                 "about it.")


class UnsupportedIsoRevision(Exception):
    """The disc image cannot be patched (wrong dump, blank or missing ELF).

    __str__ appends CHECKISO_HINT so the self-diagnosis pointer travels with
    the message no matter where it surfaces -- the launcher banner, the remote
    path, or a bare traceback -- instead of only where a caller remembered to
    print it."""

    def __str__(self):
        return super().__str__() + CHECKISO_HINT


def _boot_bin_complaint(off, size, elf):
    """Explain, in terms a player can act on, why this BOOT.BIN is unusable.

    The common case is NOT a different disc layout. Some ULUS10251 dumps ship
    PSP_GAME/SYSDIR/BOOT.BIN as a correctly-sized but ZERO-FILLED placeholder
    and carry the real executable only in the KIRK-encrypted EBOOT.BIN --
    that is how a player's "(FW6.00)"-named dump failed on 2026-08-08, at the
    SAME extent 0x29e0000 our own dump uses, so the layout was identical and
    only the file's contents differed. Decrypting EBOOT.BIN is out of scope
    (it needs the console's KIRK keys), so the only fix is a dump whose
    BOOT.BIN is the plaintext ELF."""
    if not any(elf):
        return (f"this ISO's BOOT.BIN ({size:#x} bytes at {off:#x}) is "
                "entirely ZEROS. The randomizer patches that plaintext ELF; "
                "it cannot read the encrypted EBOOT.BIN (that needs PSP "
                "console keys). Two things produce a blank BOOT.BIN: (a) a "
                "mastering that ships it as a placeholder, or (b) a bad "
                "CSO/CHD -> ISO conversion that wrote zero blocks. If you "
                "converted this file, re-convert it (PPSSPP's own converter, "
                "or maxcso) and try again; otherwise you need a dump whose "
                "BOOT.BIN is a real ELF. The one this randomizer is built "
                "against is 'Final Fantasy - 20th Anniversary Edition (USA) "
                f"(En,Ja) (FW3.03)': BOOT.BIN is {BOOT_SIZE:#x} bytes and "
                "starts with 7f 45 4c 46.")
    return (f"BOOT.BIN at {off:#x} is not a plaintext ELF "
            f"(magic {bytes(elf[:4])!r}) -- this is not the FF1 PSP dump this "
            "randomizer is built against.")


def load_boot_elf(iso=ISO_DEFAULT):
    with open(iso, "rb") as f:
        f.seek(BOOT_ISO_OFF)
        elf = bytearray(f.read(BOOT_SIZE))
        if elf[:4] != b"\x7fELF":
            # Not the layout we were RE'd against -- find the file properly.
            cands = _iso_find_boot_bins(f)
            if not cands:
                raise UnsupportedIsoRevision(
                    "no PSP_GAME/SYSDIR/BOOT.BIN in this ISO -- it is not a "
                    "plain (uncompressed) FF1 PSP disc image. CSO/CHD/ZSO "
                    "images must be decompressed to .iso first.")
            best = None
            for path, off, size in cands:
                f.seek(off)
                blob = bytearray(f.read(size))
                if blob[:4] == b"\x7fELF":
                    elf = blob
                    break
                # Prefer the GAME's copy over a bundled firmware updater's for
                # the error message -- "UPDATE/BOOT.BIN is zeros" would send a
                # player chasing the wrong file.
                if best is None or ("UPDATE" in best[0].upper()
                                    and "UPDATE" not in path.upper()):
                    best = (path, off, size, blob)
            else:
                # Nothing on the disc is a plaintext ELF -- report the game's
                # own copy (or the only one) rather than the updater's.
                path, off, size, blob = best
                raise UnsupportedIsoRevision(
                    _boot_bin_complaint(off, size, blob)
                    + f" (checked {len(cands)}: "
                    + ", ".join(p for p, _, _ in cands) + ")")
        # build_iso installs the patched ELF over the EBOOT.BIN slot at the
        # fixed EBOOT_ISO_OFF, so a disc whose BOOT.BIN moved is only usable
        # if the EBOOT slot did not.
        f.seek(EBOOT_ISO_OFF)
        if f.read(4) not in (b"~PSP", b"\x7fELF"):
            raise UnsupportedIsoRevision(
                f"no boot executable at the expected EBOOT.BIN offset "
                f"{EBOOT_ISO_OFF:#x} -- this ISO's layout differs from the "
                "dump this patcher was built for.")
    if not _build_matches(elf):
        raise UnsupportedIsoRevision(
            f"BOOT.BIN is {len(elf):#x} bytes; this patcher's code addresses "
            f"were reverse-engineered against the {BOOT_SIZE:#x}-byte build "
            "from the Redump dump 'Final Fantasy - 20th Anniversary Edition "
            "(USA) (En,Ja) (FW3.03)'. Patching a different revision would "
            "write every hook to the wrong address, so it is refused. Use the "
            "FW3.03 dump.")
    return elf


def apply_patches(elf, patches):
    """patches: list of (ram_addr, bytes) little-endian machine code/data."""
    for ram, blob in patches:
        fo = ram2file(ram)
        elf[fo:fo + len(blob)] = blob
    return elf


# --- code caves & detours -------------------------------------------------
# (The old cave_alloc grew PH0's p_filesz to squat the section-header tail;
# it was replaced by add_segment_cave below and removed -- see next comment
# for why an in-image cave is unsafe.)
# A cave inside the loaded image is unsafe: [filesz_end..memsz_end] =
# 0x89cacc0..0x8b30e00 is the game's runtime data/heap (0x89cacc4 is a live
# vtable ptr). Safe cave = a NEW PT_LOAD segment at/above memsz_end so the
# game's heap (allocated after the module) starts above it. We add the 2nd
# phdr by relocating a 2-entry program-header table into the non-loaded
# section-header tail (0x520 bytes @ file 0x1c6d14) and appending the cave
# there too -- no file growth, stays in the EBOOT slot, no ISO repacker.
SAFE_CAVE_VADDR = 0x08B30E00         # == VADDR + MEMSZ (contiguous past module)
SHDR_TAIL_OFF = SEG_FILE_OFF + FILESZ  # 0x1c6d14, start of non-loaded shdr area


def add_segment_cave(elf, cave_bytes, vaddr=SAFE_CAVE_VADDR, align=0x40):
    """Add cave_bytes as a loaded PT_LOAD segment; return the cave's RAM address.

    First call: relocates a 2-entry phdr table + the cave into the non-loaded
    section-header tail and adds the 2nd (cave) segment at `vaddr`. Subsequent
    calls APPEND their cave to that existing segment (growing p_filesz/p_memsz),
    so multiple on-disc code features can coexist without clobbering each other.
    Caves start at SHDR_TAIL_OFF and may grow the ELF past EBOOT_SLOT -- build_iso
    then relocates the whole ELF to a fresh extent (grow-by-append repacker)."""
    assert len(cave_bytes) % 4 == 0
    e_phnum = struct.unpack_from("<H", elf, 0x2c)[0]
    if e_phnum >= 2:
        # a cave segment already exists -> append this cave to it (phdr entry #1)
        e_phoff = struct.unpack_from("<I", elf, 0x1c)[0]
        ph1 = e_phoff + 32
        p_offset = struct.unpack_from("<I", elf, ph1 + 4)[0]
        p_vaddr = struct.unpack_from("<I", elf, ph1 + 8)[0]
        p_filesz = struct.unpack_from("<I", elf, ph1 + 16)[0]
        p_memsz = struct.unpack_from("<I", elf, ph1 + 20)[0]
        if p_memsz != p_filesz:
            # a zero-init tail (cave_bss_tail) is already reserved at the end of
            # the segment; appending file bytes would land inside it. The
            # bss-tail feature must be applied LAST (see iso_patcher.FEATURES).
            raise ValueError("cave segment has a bss tail; add_segment_cave "
                             "after cave_bss_tail would overlap it")
        new_off = p_offset + p_filesz            # caves are word-multiples -> stays aligned
        new_vaddr = p_vaddr + p_filesz
        end = new_off + len(cave_bytes)
        if end > MAX_ELF:
            raise ValueError(f"appended cave end {end:#x} exceeds sanity cap {MAX_ELF:#x}")
        if end > len(elf):
            elf.extend(b"\0" * (end - len(elf)))
        elf[new_off:new_off + len(cave_bytes)] = cave_bytes
        struct.pack_into("<I", elf, ph1 + 16, p_filesz + len(cave_bytes))  # p_filesz
        struct.pack_into("<I", elf, ph1 + 20, p_filesz + len(cave_bytes))  # p_memsz
        return new_vaddr
    e_phoff = struct.unpack_from("<I", elf, 0x1c)[0]
    ph0 = bytes(elf[e_phoff:e_phoff + 32])          # copy original phdr
    new_phoff = SHDR_TAIL_OFF
    # place cave after the 2-entry table, aligned so (off % align)==(vaddr % align)
    # -- PT_LOAD requires file offset and vaddr CONGRUENT modulo the segment
    # alignment (equal residues, not equal alignment), so the cave slides
    # forward in word steps until the residues match.
    cave_off = new_phoff + 64
    if align > 1:
        while (cave_off % align) != (vaddr % align):
            cave_off += 4
    end = cave_off + len(cave_bytes)
    if end > MAX_ELF:
        raise ValueError(f"segment cave end {end:#x} exceeds sanity cap {MAX_ELF:#x}")
    if end > len(elf):
        elf.extend(b"\0" * (end - len(elf)))
    # phdr1 for the cave: PT_LOAD, RWX
    ph1 = struct.pack("<8I", 1, cave_off, vaddr, vaddr,
                      len(cave_bytes), len(cave_bytes), 7, align)
    elf[new_phoff:new_phoff + 32] = ph0
    elf[new_phoff + 32:new_phoff + 64] = ph1
    elf[cave_off:cave_off + len(cave_bytes)] = cave_bytes
    # point ELF header at the relocated phdr table; drop section headers
    struct.pack_into("<I", elf, 0x1c, new_phoff)     # e_phoff
    struct.pack_into("<H", elf, 0x2c, 2)             # e_phnum
    struct.pack_into("<I", elf, 0x20, 0)             # e_shoff = 0
    struct.pack_into("<H", elf, 0x30, 0)             # e_shnum = 0
    struct.pack_into("<H", elf, 0x32, 0)             # e_shstrndx = 0
    return vaddr


def cave_bss_tail(elf, size, align=0x10):
    """Reserve `size` bytes of ZERO-initialized RAM at the end of the cave
    segment by growing p_memsz only (the loader zero-fills [filesz, memsz)).
    Costs no file bytes, so large runtime tables fit even though the in-place
    EBOOT slot only has ~0x67c spare file bytes. Returns the tail's RAM address.

    Must be the LAST cave operation: add_segment_cave refuses to append after
    this (file bytes would land inside the zero tail)."""
    e_phnum = struct.unpack_from("<H", elf, 0x2c)[0]
    if e_phnum < 2:
        raise ValueError("no cave segment yet; add_segment_cave first")
    e_phoff = struct.unpack_from("<I", elf, 0x1c)[0]
    ph1 = e_phoff + 32
    p_vaddr = struct.unpack_from("<I", elf, ph1 + 8)[0]
    p_memsz = struct.unpack_from("<I", elf, ph1 + 20)[0]
    start = p_vaddr + p_memsz
    if align > 1:
        start = (start + align - 1) & ~(align - 1)
    struct.pack_into("<I", elf, ph1 + 20, (start - p_vaddr) + size)
    return start


def cave_write(elf, ram, blob):
    """Overwrite bytes inside the ADDED cave segment (ram2file only maps the
    main segment). Used to re-assemble a cave in place once addresses that
    depend on its own placement (e.g. a bss-tail table) are known."""
    e_phoff = struct.unpack_from("<I", elf, 0x1c)[0]
    ph1 = e_phoff + 32
    p_offset = struct.unpack_from("<I", elf, ph1 + 4)[0]
    p_vaddr = struct.unpack_from("<I", elf, ph1 + 8)[0]
    p_filesz = struct.unpack_from("<I", elf, ph1 + 16)[0]
    if not (p_vaddr <= ram and ram + len(blob) <= p_vaddr + p_filesz):
        raise ValueError(f"{ram:#x}+{len(blob):#x} outside cave file range")
    fo = p_offset + (ram - p_vaddr)
    elf[fo:fo + len(blob)] = blob


def get_entry(elf):
    """ELF e_entry (module entry point RAM address)."""
    return struct.unpack_from("<I", elf, 0x18)[0]


def install_detour(elf, hook_ram, cave_ram):
    """Overwrite two instructions at hook_ram with `j cave_ram; nop`. The two
    displaced originals must already be at the head of the cave (in order), so
    the cave runs them, then its body, then jumps back to hook_ram+8. The nop is
    the branch-delay slot of the jump."""
    try:
        from . import mips_asm as A
    except ImportError:
        import mips_asm as A
    fo = ram2file(hook_ram)
    elf[fo:fo + 8] = A.j(cave_ram) + A.nop()


_COPY_CHUNK = 8 << 20        # 8MB: beats shutil's default on spinning disks


def _copy_with_progress(src, dst, progress):
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(_COPY_CHUNK)
            if not buf:
                break
            fo.write(buf)
            done += len(buf)
            try:
                progress(done, total)
            except Exception:      # a reporting bug must never fail the bake
                progress = lambda *_: None      # noqa: E731


def build_iso(elf, out_iso, src_iso=ISO_DEFAULT, progress=None):
    """Copy src ISO, install the (patched) ELF as the boot EBOOT.BIN.

    If the ELF fits the original EBOOT slot it is written IN PLACE (byte-identical
    layout, no ISO9660 changes). If a cave grew it past the slot, we relocate it:
    the grown ELF is APPENDED at the end of the ISO and the EBOOT.BIN directory
    record is repointed to the new extent (grow-by-append repacker), so the whole
    image never has to shift. See _relocate_eboot.

    progress (optional) = callable(done_bytes, total_bytes) invoked during the
    copy. This copy IS the bake's wall clock (~1.4GB), so it is the only stage
    worth reporting; the caller formats/throttles."""
    if progress is None:
        shutil.copyfile(src_iso, out_iso)
    else:
        _copy_with_progress(src_iso, out_iso, progress)
    if len(elf) <= EBOOT_SLOT:
        with open(out_iso, "r+b") as f:
            f.seek(EBOOT_ISO_OFF)
            f.write(elf)
            pad = EBOOT_SLOT - len(elf)
            if pad:
                f.write(b"\0" * pad)
        return out_iso
    _relocate_eboot(out_iso, elf)
    return out_iso


# --- ISO9660 grow-by-append repacker -----------------------------------------
# The disc is a single-PVD ISO9660 (no Joliet SVD). The boot loader is the file
# /PSP_GAME/SYSDIR/EBOOT.BIN, whose directory record extent = LBA 32 (EBOOT_ISO_OFF)
# / size EBOOT_SLOT. PPSSPP resolves it through the directory tree, so relocating
# its extent only needs that one record updated (files, unlike dirs, are not in the
# path table). We append the grown ELF at the end of the image (sector-aligned),
# repoint the record's extent LBA + data length, and bump the PVD volume-space
# size. The old in-slot extent is left as harmless dead space.
_PVD_SECTOR = 16
_ISO9660_ID = b"CD001"


def _find_boot_eboot_record(buf):
    """Return the byte offset of the boot EBOOT.BIN directory record in `buf`
    (the one whose current extent == LBA 32 / size EBOOT_SLOT). Walks the PVD
    directory tree; raises if not found or ambiguous."""
    pvd_off = _PVD_SECTOR * SECTOR
    if buf[pvd_off] != 1 or buf[pvd_off + 1:pvd_off + 6] != _ISO9660_ID:
        raise ValueError("PVD not found at sector 16")
    root = buf[pvd_off + 156:pvd_off + 156 + 34]
    root_lba = struct.unpack_from("<I", root, 2)[0]
    root_len = struct.unpack_from("<I", root, 10)[0]
    want_lba = EBOOT_ISO_OFF // SECTOR
    hits = []

    def walk(lba, size, depth):
        base = lba * SECTOR
        p = 0
        while p < size:
            rl = buf[base + p]
            if rl == 0:
                p = (p // SECTOR + 1) * SECTOR
                continue
            rec = base + p
            elba = struct.unpack_from("<I", buf, rec + 2)[0]
            elen = struct.unpack_from("<I", buf, rec + 10)[0]
            flags = buf[rec + 25]
            nlen = buf[rec + 32]
            name = bytes(buf[rec + 33:rec + 33 + nlen])
            if name not in (b"\x00", b"\x01"):
                if flags & 2:
                    if depth < 4:
                        walk(elba, elen, depth + 1)
                elif name.split(b";")[0] == b"EBOOT.BIN" and \
                        elba == want_lba and elen == EBOOT_SLOT:
                    hits.append(rec)
            p += rl

    walk(root_lba, root_len, 0)
    if len(hits) != 1:
        raise ValueError(f"expected exactly one boot EBOOT.BIN record "
                         f"(lba {want_lba}, size {EBOOT_SLOT:#x}); found {len(hits)}")
    return hits[0]


def _relocate_eboot(out_iso, elf):
    """Append `elf` at the end of out_iso (sector-aligned) and repoint the boot
    EBOOT.BIN directory record + PVD volume size to the new extent."""
    with open(out_iso, "r+b") as f:
        data = bytearray(f.read())
        rec = _find_boot_eboot_record(data)
        # append point: current end, padded up to a sector boundary
        if len(data) % SECTOR:
            data.extend(b"\0" * (SECTOR - len(data) % SECTOR))
        new_lba = len(data) // SECTOR
        data.extend(elf)
        if len(data) % SECTOR:
            data.extend(b"\0" * (SECTOR - len(data) % SECTOR))
        new_total = len(data) // SECTOR
        # directory record: extent LBA (LE @+2, BE @+6), data length (LE @+10, BE @+14)
        # ISO9660 stores these as both-endian pairs (LE copy then BE copy);
        # BOTH copies must be rewritten -- a picky reader that trusts the BE
        # half would see a torn record. Same for the PVD pair below.
        struct.pack_into("<I", data, rec + 2, new_lba)
        struct.pack_into(">I", data, rec + 6, new_lba)
        struct.pack_into("<I", data, rec + 10, len(elf))
        struct.pack_into(">I", data, rec + 14, len(elf))
        # PVD volume-space size (LE @+80, BE @+84)
        pvd = _PVD_SECTOR * SECTOR
        struct.pack_into("<I", data, pvd + 80, new_total)
        struct.pack_into(">I", data, pvd + 84, new_total)
        f.seek(0)
        f.write(data)
        f.truncate()
    return out_iso


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "test_identity.iso"
    elf = load_boot_elf()
    # Feasibility test #5(a): IDENTITY swap -- plaintext ELF, no code change.
    # Byte-identical logic; if it boots, PPSSPP accepts plaintext-ELF-as-EBOOT.
    build_iso(elf, out)
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")
    print(f"EBOOT slot now holds plaintext ELF (magic {bytes(elf[:4])!r})")
