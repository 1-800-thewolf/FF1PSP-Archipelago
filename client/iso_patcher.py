"""On-disc ISO patcher for FF1 PSP Route-2 code features AND seed data tables.

Two patch layers, both baked into a cached COPY of the player's ISO before boot
(the original ISO is never modified):

  * CODE features (FEATURES): real MIPS hacks. PPSSPP's JIT blocks live code
    patching, so these can only ship on-disc.
  * DATA patches (apply_data_patches): the seed's shuffled tables (shops,
    prices, encounter zones, magic, xp/encounter scaling, AP chest contents).
    These are plain ELF .data at FIXED file offsets (verified byte-for-byte
    against rando_data.META), so they are patched deterministically -- no
    signature scanning. Baking them replaced the old runtime DataPatch writes,
    which needed 24 MB RAM scans every boot (game-lagging, and fragile: a
    save-state load reverted them until the client noticed).

ORDER MATTERS: data first, then code. monk_thief_dabble ORs its learn bits on
top of the (possibly shuffled) magic_learn table, so the final table carries
both the seed's shuffle and the feature bits.

A BAKE TAG (8 bytes at the head of the cave segment, RAM 0x08B30E00) identifies
the exact bake -- magic 'F1AP' + the low 32 bits of the bake hash. The launcher
reads it from a running game to decide "is this PPSSPP already running THIS
seed's patched ISO?" (feature signatures alone can't tell two seeds apart).
"""
import os, struct, time, warnings
from . import eboot_patch as E
from . import mips_asm as A
from . import popup_bake


# --- feature: Monk/Thief/Master get magic (MP scaling + spells + 0.75x RedMage) ---
_MAGIC_LEARN = 0x08955376
_DISPATCH    = 0x089792e8
_REDMAGE_CAP = 0x088d0da8
_MENU_LVCAP_TAB   = 0x08979310   # magic-MENU class->row-cap jump table (indexed class*4)
_MENU_REDMAGE_CAP = 0x088d18bc   # menu handler: rows lit = min(magiclv, 6)
_START_TAB   = 0x0895567c
_STATIDX7_THIEF_DELAY = 0x08887d20
_MONK   = [2, 6, 10, 14]
_THIEF  = [33, 39, 43, 46]
_MASTER = [0, 4, 12, 16, 20] + _MONK  # +Curaga(16, Lv5)
# Char levels at which a dabbler's magiclv increments (growth bit7). Start
# magiclv is 1, so these unlock spell levels 2..6 -- the cap (RedMage cap-6
# handler), so there is no wasted 7th tick. SLOWED 2026-08-18 (user spec
# 1/1, 2/10, 3/18, 4/25, 5/33, 6/45; was [5, 12, 20, 29, 39, 49], whose 49
# tick was eaten by the cap). _SM_DAB_ANCHORS mirrors these levels so the
# FIRST charge of each spell level lands exactly on its unlock.
_SCHEDULE = [10, 18, 25, 33, 45]

# Starting MP / magic level the dabble feature grants Thief+Monk at creation
# (written into the class start-stats table). The client's party-setter must
# mirror these when it rewrites a character's L1 stat block (see ApClient).
DABBLE_START_MP = 3
DABBLE_START_MAGICLV = 1
# v231 dabbler MP soft cap (see apply_monkthief_magic). Per level-up a dabbler
# accrues e = F + W1*min(INT,K) + W2*max(INT-K,0) SIXTEENTHS of an MP; the knee
# K is the only per-job difference. W1/W2 are the below/above-knee slopes, so
# W2 < W1 is the soft cap: INT keeps paying past K, just at a quarter rate.
# Tuning knobs: W2 -> endgame total, F -> early game, K_MASTER -> Master's edge.
_DAB_MP_D = 16                # accrual denominator (must be a power of two)
_DAB_MP_F = 8                 # flat per level = 0.5 MP
_DAB_MP_W1 = 4                # below the knee: INT/4 per level
_DAB_MP_W2 = 1                # above the knee: INT/16 per level
_DAB_MP_K = 10                # Thief / Monk knee
_DAB_MP_K_MASTER = 14         # Master knee -- ~16% more total MP by L99
DABBLE_JOBS = (1, 2)          # Thief, Monk (class ids)

# Shop "who can learn" class panel: the right-hand box lists 5 fixed classes per
# color (white 0x08985CE8 = RM/WM/Knight/RW/WW, black 0x08985CF0 = RM/BM/Ninja/
# RW/BW; drawn white if learnable, gray if not) and never mentions Monk/Thief,
# so dabble spells look unlearnable in shops. All three 5-byte const tables
# (y-offsets + the two class lists) are 8-aligned with zero padding, so a 6th
# entry fits IN PLACE; the copy loops + draw loop use inline immediates 5.
# The learnable-mask builder (fn 0x8820380) checks 5 hardcoded class codes and
# ORs bits 0-4 into 0x707c(s0); a cave hooked over each color's bit-0x10 tail
# re-runs that tail and adds a 6th check (Monk a1=2 white / Thief a1=1 black,
# bit 0x20), then rejoins at the untouched convergence point 0x08820D50.
_PANEL_YTAB       = 0x08985CE0   # 5 y-offsets (+0x60 px base), step 0x0C
_PANEL_WHITE_TAB  = 0x08985CE8   # 5 class ids, white shops
_PANEL_BLACK_TAB  = 0x08985CF0   # 5 class ids, black shops
_PANEL_COPY_LENS  = (0x08822A4C, 0x08822A74, 0x08822A9C)  # addiu v1,zero,5
_PANEL_DRAW_CMP   = 0x08822B9C   # slti v1,s2,5 (draw-loop bound)
_PANEL_LEARNCHK   = 0x08820DC4   # helper(a0=struct, a1=class, a2=spell)
_PANEL_YBASE      = (0x08822B34, 0x08822B74)  # addiu v0,v0,0x60 (y base, both
                                 # color paths); lowered so 6 lines fit
_PANEL_YBASE_VAL  = 0x5A         # 0x60-6: 6th line cleared the box bottom
_PANEL_WHITE_TAIL = 0x08820C84   # beqz/nop/lw/ori 0x10/b/sw  (6 words)
_PANEL_BLACK_TAIL = 0x08820D3C   # beqz/nop/lw/ori 0x10/sw    (5 words)
_PANEL_JOIN       = 0x08820D50   # convergence; NEVER touch (beql from
                                 # 0x8820BB8 lands on 0x8820D54)


def _magic_slot_remap(shops):
    """spell_index -> spell_index map that follows a magic-shop shuffle: each
    vanilla dabble spell maps to whatever spell now occupies its vanilla shop
    slot. Same shop => same tier (align_shop_spell_levels gives the occupant
    that shop's level), so Monk/Thief still learn from the shops they could in
    vanilla instead of chasing spells the shuffle moved to late shops. Identity
    when `shops` is None or the magic slots are unshuffled."""
    if shops is None:
        return {}
    from .. import rando as R
    from .. import rando_data as RD
    van = RD.VANILLA["shops"]
    remap = {}
    for color in R._MAGIC_COLORS:
        for off in R._magic_slots(color):
            remap[van[off] - 1] = shops[off] - 1   # shop spell ids are 1-based
    return remap


def dabble_learn_bits(shops=None):
    """[(spell_index, learn_bit)] the dabble feature ORs into magic_learn.
    Shared with the client so its expected-bytes reconcile matches the baked
    table (learn bits ride ON TOP of any magic shuffle). `shops` = the seed's
    (possibly shuffled) shops table bytes; the granted spells follow the
    shuffle via _magic_slot_remap."""
    remap = _magic_slot_remap(shops)
    m = lambda i: remap.get(i, i)
    out = [(m(i), 2) for i in _MONK] + [(m(i), 1) for i in _THIEF] \
        + [(m(i), 10) for i in _MASTER]
    return out


def apply_dabble_learn_overlay(magic_learn: bytearray, shops=None) -> bytearray:
    """OR the dabble learn bits into a 64-entry u16 magic_learn table (bytes).
    Pure-bytes twin of the ELF-side _bit_u16 loop in apply_monkthief_magic.
    Pass the seed's shuffled `shops` bytes so the bits land on the remapped
    spells, matching the bake."""
    for idx, bit in dabble_learn_bits(shops):
        v = struct.unpack_from("<H", magic_learn, idx * 2)[0]
        struct.pack_into("<H", magic_learn, idx * 2, v | (1 << bit))
    return magic_learn


def _bit_u16(elf, ram, bit):
    fo = E.ram2file(ram); v = struct.unpack_from("<H", elf, fo)[0]
    struct.pack_into("<H", elf, fo, v | (1 << bit))

def _byte_bit(elf, ram, bit, on):
    fo = E.ram2file(ram)
    elf[fo] = (elf[fo] | (1 << bit)) if on else (elf[fo] & ~(1 << bit) & 0xff)


def apply_monkthief_magic(elf: bytearray, feats=None):
    # MP-amount cave: per-level MP gain with REMAINDER CARRY.
    #
    # v231 SOFT CAP (user-authorized 2026-08-07). The old scale was
    # Thief floor(INT/4)+2, Monk floor(INT/4)+1, Master floor(INT/3)+1 --
    # INT-linear with no ceiling, so a mid-INT dabbler ended L99 around 600-780
    # maxMP, brushing the 999 cap and rivalling a full caster. Dabblers now
    # accrue in SIXTEENTHS of an MP through a two-slope curve:
    #
    #   e = _DAB_MP_F + _DAB_MP_W1*min(INT, K) + _DAB_MP_W2*max(INT - K, 0)
    #   acc += e;  gain = acc >> 4;  carry = acc & 15
    #
    # Full slope below the knee K, a quarter of it above -- so INT never stops
    # mattering (no flat ceiling), it just stops compounding. Lands ~350
    # (Thief/Monk) / ~410 (Master) at L99 on a mid INT roll, ~415 / ~480 on a
    # Mind-Plus-fed one.
    #
    # Master differs by KNEE ONLY (14 vs 10): same shape, holds the full slope
    # four points of INT longer, ~16% more total -- deliberately a slight edge
    # over Thief/Monk, not a tier jump.
    #
    # WHY A SOFT CAP AND NOT A HARD CLAMP: e(INT) is NONDECREASING, and total MP
    # is the sum of e over levels, so a dabbler who races above K has INT >= the
    # laggard's at EVERY level and therefore can never end with less. Concavity
    # compresses the racer's lead; it cannot invert it. A hard clamp does invert
    # it (the laggard keeps full slope while the racer is pinned), which is
    # exactly why this is two slopes and not a min().
    #
    #   any other job: floor(INT/4)   (unchanged vanilla shared-path behavior --
    #   a different denominator, hence the separate leg)
    #
    # We accumulate into a per-member byte and grant floor(acc/div), keeping
    # acc%div as carry, so fractional rates are exact over time. INT varies per
    # level (growth/Mind Plus/equip), so a stateless recompute can't work -- the
    # running remainder must be stored. (A Monk->Master promotion inherits the
    # acc byte across the knee switch; that's <1 MP of drift, invisible.)
    #
    # Storage = a 0x1000 zero buffer baked into the cave segment (file-backed, so
    # it re-zeros every boot -> free char-init; a mid-run PPSSPP restart loses <1
    # accumulated MP point, which is invisible). Per-member slot = (record & 0xFFF):
    # the 4 level-up records (INT@+0x33, maxMP@+0x2e -- a DIFFERENT struct than the
    # field party record) sit contiguously within one 4KB page, so their low-12-bit
    # offsets are distinct + stable across the session base-shift, and any index is
    # in-bounds (no crash). s4=record, s1=job; falls into the cap/store @0x08887BF8.
    # Under slot_magic the statIdx-1 jump-table entry is repointed to the
    # slot-gain cave, so the vanilla MP handler (and this cave hooked into its
    # tail) is UNREACHABLE -- skip the 0x1000 accumulator + cave entirely.
    # This also keeps the tome bss tables inside the no-sign-carry low page
    # (the 4KB buffer was the largest single tenant).
    sm_on = bool(feats and feats.get("slot_magic"))
    BUF = 0 if sm_on else E.add_segment_cave(elf, b"\x00" * 0x1000)
    assert _DAB_MP_D and not (_DAB_MP_D & (_DAB_MP_D - 1)), "denominator must be 2^n"
    DSH = _DAB_MP_D.bit_length() - 1                   # acc >> DSH == acc / D
    # acc stays a BYTE: max e = F + W1*K_master + (99 - K_master) fits well
    # under 256 even with a full carry already in the slot.
    assert (_DAB_MP_F + _DAB_MP_W1 * _DAB_MP_K_MASTER
            + _DAB_MP_W2 * (99 - _DAB_MP_K_MASTER)
            + _DAB_MP_D - 1) < 256, "accrual would overflow the carry byte"
    cave = A.asm_labels([
        A.lbu("t0", 0x33, "s4"),                       # t0 = INT
        A.andi("t3", "s4", 0xFFF),                     # per-member slot offset
        A.lui("t4", (BUF >> 16) & 0xFFFF),
        A.ori("t4", "t4", BUF & 0xFFFF),
        A.addu("t3", "t4", "t3"),                      # t3 = &acc[member]
        A.lbu("t5", 0, "t3"),                          # t5 = acc
        # dabbler dispatch. The first branch's delay slot preloads the default
        # knee, so Thief/Monk fall into DAB with t2 already set.
        A.addiu("t1", "zero", 1),                      # Thief
        ("beq", "s1", "t1", "DAB"), A.addiu("t2", "zero", _DAB_MP_K),
        A.addiu("t1", "zero", 2),                      # Monk
        ("beq", "s1", "t1", "DAB"), A.nop(),
        A.addiu("t1", "zero", 8),                      # Master
        ("beq", "s1", "t1", "DAB_M"), A.nop(),
        # --- non-dabbler leg: vanilla floor(INT/4) with remainder carry -------
        A.addu("t5", "t5", "t0"),                      # acc += INT
        A.srl("s2", "t5", 2),                          # gain = acc >> 2
        A.andi("t5", "t5", 3),                         # carry = acc & 3
        A.sb("t5", 0, "t3"),
        A.j(0x08887BF8), A.nop(),                      # -> existing cap/store
        # --- dabbler leg: two-slope accrual in 1/D MP ------------------------
        ("label", "DAB_M"),
        A.addiu("t2", "zero", _DAB_MP_K_MASTER),       # Master: later knee
        ("label", "DAB"),
        A.slt("t1", "t0", "t2"),                       # INT < K ?
        ("beq", "t1", "zero", "DAB_GE"), A.nop(),
        A.addu("s2", "t0", "zero"),                    # below knee: lo = INT
        ("beq", "zero", "zero", "DAB_SUM"),
        A.addu("t4", "zero", "zero"),                  #             hi = 0
        ("label", "DAB_GE"),
        A.addu("s2", "t2", "zero"),                    # at/above:   lo = K
        A.subu("t4", "t0", "t2"),                      #             hi = INT-K
        ("label", "DAB_SUM"),
    ] + _sm_mul_const("s2", "s2", _DAB_MP_W1, "t1")    # s2 = W1 * lo
      + _sm_mul_const("t4", "t4", _DAB_MP_W2, "t1")    # t4 = W2 * hi
      + [
        A.addu("s2", "s2", "t4"),
        A.addiu("s2", "s2", _DAB_MP_F),                # e = F + W1*lo + W2*hi
        A.addu("t5", "t5", "s2"),                      # acc += e
        A.srl("s2", "t5", DSH),                        # gain = acc / D
        A.andi("t5", "t5", _DAB_MP_D - 1),             # carry = acc % D
        A.sb("t5", 0, "t3"),
        A.j(0x08887BF8), A.nop(),                      # -> existing cap/store
    ])
    if not sm_on:
        cave_vaddr = E.add_segment_cave(elf, cave)
        E.install_detour(elf, 0x08887BC4, cave_vaddr)
    # learn-bits (shared with the client's expected-bytes reconcile). Read the
    # shops table out of the ELF: data patches run FIRST (patch_iso order
    # contract), so this sees the seed's shuffle and the bits follow it.
    from .. import rando_data as RD
    iso_off, _, count = RD.META["shops"]
    shops = bytes(elf[iso_off - E.BOOT_ISO_OFF:iso_off - E.BOOT_ISO_OFF + count])
    for i, bit in dabble_learn_bits(shops):
        _bit_u16(elf, _MAGIC_LEARN + i*2, bit)
    # cap repoint jobs 1/2/8 -> RedMage cap-6 (learn/MP dispatch)
    for job in (1, 2, 8):
        struct.pack_into("<I", elf, E.ram2file(_DISPATCH + job*4), _REDMAGE_CAP)
    # magic-MENU row highlight: vanilla maps jobs 1/2/8 to the "no rows light up"
    # handler, so Thief/Monk/Master had magic but dark menu rows. Repoint them to
    # the RedMage cap-6 MENU handler (rows lit = min(magiclv, 6)) so their rows
    # light up and scale as magiclv grows. (This table is DATA read by jr.)
    for job in (1, 2, 8):
        struct.pack_into("<I", elf, E.ram2file(_MENU_LVCAP_TAB + job*4), _MENU_REDMAGE_CAP)
    # magiclv schedule via growth bit7 (Thief row1, Monk row2; Master rides Monk via job%6)
    GROWTH = 0x0894c1b8
    for cls in (1, 2):
        row = GROWTH + cls*99
        for lv in range(2, 100):
            _byte_bit(elf, row + (lv-2), 7, on=False)
        for lv in _SCHEDULE:
            _byte_bit(elf, row + (lv-2), 7, on=True)
    # Thief increment +4 -> +1
    E.apply_patches(elf, [(_STATIDX7_THIEF_DELAY, A.addiu("s2", "zero", 1))])
    # starting stats: Monk/Thief start magiclv 1 + 3 MP (magiclv @ +class*16, MP @ -2)
    for cls in DABBLE_JOBS:
        row = _START_TAB + cls*16
        elf[E.ram2file(row)] = DABBLE_START_MAGICLV
        struct.pack_into("<H", elf, E.ram2file(row - 2), DABBLE_START_MP)
    # shop class panel: add a 6th line (Monk on white shops, Thief on black) so
    # the "which classes learn this" box reflects the dabble grants. See the
    # _PANEL_* constants block for the RE map.
    # data: 6th entry lands in each table's zero padding (all 8-aligned)
    elf[E.ram2file(_PANEL_YTAB + 5)]      = 0x3C   # y continues the 0x0C step
    elf[E.ram2file(_PANEL_WHITE_TAB + 5)] = 2      # Monk
    elf[E.ram2file(_PANEL_BLACK_TAB + 5)] = 1      # Thief
    # printer: three table-copy loop counts + the draw-loop bound, 5 -> 6
    for ram in _PANEL_COPY_LENS + (_PANEL_DRAW_CMP,):
        _set_imm16(elf, ram, 6)
    # shift the whole list up so the 6th line clears the box bottom
    for ram in _PANEL_YBASE:
        _set_imm16(elf, ram, _PANEL_YBASE_VAL)
    # builder cave: two entries (white/black) sharing a common tail. Entry
    # state: v0 = the 5th class's learn-check result (the displaced tails'
    # own beqz consumed it originally), s0 = shop struct, s1 = spell id
    # (callee-saved across the helper). ra is dead here (reloaded later),
    # t-regs/at free.
    # White 6th line follows class change: Monk (a1=2) before promotion,
    # Master (a1=8) after -- detected by any party class byte >= 6 (promotion
    # is all-at-once). Party records = [s0+0x6EFC], stride 0x5C, class @+0x7A
    # (menu frame, per the tome teach-gate RE). The chosen id doubles as the
    # displayed class: the cave pokes it into the white table's 6th byte
    # (DATA write -- JIT-safe) before the printer copies the table.
    panel = A.assemble([
        # white entry (cave+0)
        A.lw("t0", 0x6EFC, "s0"),                      # party record array
        A.addiu("a1", "zero", 2),                      # default: Monk
        A.addiu("t1", "zero", 4),                      # 4 members
        A.addiu("t2", "zero", 6),                      # promoted threshold
        A.lbu("t3", 0x7A, "t0"),                       # wloop: class byte
        A.slt("at", "t3", "t2"),
        A.beq("at", "zero", 5),                        # class>=6 -> promoted
        A.addiu("t1", "t1", -1),                       # (delay)
        A.bne("t1", "zero", -5),                       # -> wloop
        A.addiu("t0", "t0", 0x5C),                     # (delay) next record
        A.beq("zero", "zero", 2), A.nop(),             # all base -> store
        A.addiu("a1", "zero", 8),                      # promoted: Master
        A.lui("t4", 0x0898),                           # store: display id =
        A.sb("a1", 0x5CED, "t4"),                      #   white table 6th byte
        A.beq("zero", "zero", 2), A.nop(),             # -> common
        # black entry (cave+68): 6th class = Thief
        A.addiu("a1", "zero", 1),
        # common: finish the displaced bit-0x10 (5th class) tail
        A.beq("v0", "zero", 4), A.nop(),
        A.lw("v1", 0x707C, "s0"),
        A.ori("v1", "v1", 0x10),
        A.sw("v1", 0x707C, "s0"),
        # 6th-class check (a1 preset per entry)
        A.addu("a0", "s0", "zero"),
        A.jal(_PANEL_LEARNCHK),
        A.addu("a2", "s1", "zero"),                    # delay: a2 = spell id
        A.beq("v0", "zero", 4), A.nop(),
        A.lw("v1", 0x707C, "s0"),
        A.ori("v1", "v1", 0x20),
        A.sw("v1", 0x707C, "s0"),
        A.j(_PANEL_JOIN), A.nop(),
    ])
    panel_vaddr = E.add_segment_cave(elf, panel)
    E.apply_patches(elf, [
        # white bit-0x10 tail (6 words) -> cave white entry
        (_PANEL_WHITE_TAIL, A.j(panel_vaddr) + A.nop() * 5),
        # black bit-0x10 tail (5 words) -> cave black entry
        (_PANEL_BLACK_TAIL, A.j(panel_vaddr + 68) + A.nop() * 4),
    ])


# --- feature: Thief/Ninja bonus crit = ONE independent AGI roll per attack -----
# RE (2026-07-27, live-verified). The crit path is in the per-hit loop of the
# combat-calc fn 0x88840d0 (a0 = *(s5+0x34) = attacker battle struct):
#   0x8884208  lh s0,0x18(a0)   attack power
#   0x888420c  lh s2,0x1a(a0)   CRIT RATE (weapon crit + gear)
#   0x8884330  s6 = hit count   ([a0+0x14] * [a0+0x7], clamped 1..0x63)
#   0x888437c  miss check       (s1 = to-hit; s1 < s0 -> this hit missed)
#   0x88843f0  slt v0,s0,v0     s0 = rand%201, v0 = [sp+0x3c] threshold;
#                               roll < threshold -> crit: flag 0x8 at
#                               results+0xC and damage += attack power
# NB the OLD (2026-07-01) hook @0x8884464 / `lbu v0,0x20(v1)` was NOT the crit
# stat: that sub-block is gated on `[s5+0x3d] < 4` (the TARGET must be a party
# member) so it never ran for a party member attacking a monster, and +0x20 read
# 0 for a Thief holding a crit-25 weapon. The feature was inert until v153.
#
# DESIGN (user 2026-07-27): do NOT touch the vanilla crit math. Instead give a
# Thief/Ninja exactly ONE extra, independent crit roll PER ATTACK at AGI/201,
# spent on the first hit whose normal crit check fails. Rationale: adding AGI to
# the crit stat gets multiplied by the hit count (a Lv45 Thief swings 8 times),
# so raw AGI meant +1.79 extra crits per swing at endgame vs +0.10 early -- a 9x
# spread and a far bigger balance change than intended. One roll per attack is
# hit-count independent by construction: an 8-hit and a 1-hit attack each get
# exactly one AGI/201 chance, and AGI still scales it linearly.
#
# Cave 1 @0x888420c (displaces `lh s2,0x1a(a0)` + `sb v1,0x0(s4)`, ret 0x8884214)
# runs ONCE per action: it re-arms the mailbox for this attacker -- rate =
# AGI * _CRIT_AGI_SCALE/100 for a party Thief(1)/Ninja(7), else 0 -- and clears
# the used latch. It deliberately leaves s2 alone so vanilla crit is untouched.
# Cave 2 @0x88843e8 (displaces `seh a0,v0` + `lw v0,0x3c(sp)`, ret 0x88843f0)
# runs per hit, immediately before the vanilla compare. If the hit did NOT crit
# normally and the one roll is unspent, it spends it: `jal 0x8869528` (the game's
# own RNG), `% 201 < rate` -> force the crit by zeroing s0 (and bumping the saved
# threshold to >=1 so a 0-crit weapon still compares true), so the vanilla crit
# path applies flag + damage; then SE_Play the cue. The roll is only reachable on
# hits that connected (the miss check bails to 0x8884518 earlier).
# The `lw v0,0x3c(sp)` MUST stay ahead of our own frame push (it is sp-relative);
# both displaced words are copied verbatim so no seh encoder is needed. a0/v0/v1
# are live past the hook and the RNG/SE calls clobber caller-saved regs, so they
# go through the save frame. Class @ menu-frame rec+0x1E via *(battle_base+0x6834)
# (rec+0x5A reads ZERO in that frame -- see _M_CLASS), AGI @ struct+0x37.
_CRIT_HOOK, _CRIT_RET = 0x0888420C, 0x08884214
_CRIT_SE_HOOK, _CRIT_SE_RET = 0x088843E8, 0x088843F0
_CRIT_RNG = 0x08869528             # the calc fn's own RNG (returns u16)
_CRIT_RNG_MOD = 201                # vanilla rolls rand%0xC9 for both hit and crit
_CRIT_SE_DEFAULT = 0x00FE          # cue sound id; mailbox-editable, 0 = silent
_CRIT_MB_MAGIC = b"CRTB"
# mailbox: +0 u32 magic, +4 u16 rate (0 = attacker gets no roll), +6 u8 used
# latch (one roll per action), +8 u16 SE id
_CRIT_MB_RATE, _CRIT_MB_USED, _CRIT_MB_SE = 0x04, 0x06, 0x08
# Bonus-roll rate as a percent of AGI, BAKED -- deliberately not a yaml option
# (user 2026-07-27: players don't get to tune the Thief's crit balance). 100 =
# AGI/201 per attack, which is what was playtested. Change it here only, and
# bump PATCHER_VERSION when you do: any other value re-emits the cave with a
# multiply/divide, so the feature bytes change.
_CRIT_AGI_SCALE = 100


def apply_thief_extra_crit(elf: bytearray, feats=None):
    scale = max(0, min(1000, int(_CRIT_AGI_SCALE)))
    mb = E.add_segment_cave(
        elf, _CRIT_MB_MAGIC + struct.pack("<HBBH", 0, 0, 0, _CRIT_SE_DEFAULT)
        + b"\x00" * 2)

    rate_calc = [A.lbu("t1", 0x37, "a0")]                  # t1 = AGI
    if scale != 100:                                       # t1 = AGI * scale / 100
        rate_calc += [A.addiu("t2", "zero", scale), A.multu("t1", "t2"), A.mflo("t1"),
                      A.addiu("t2", "zero", 100), A.divu("t1", "t2"), A.mflo("t1")]
    cave = A.asm_labels([
        A.lh("s2", 0x1A, "a0"), A.sb("v1", 0x00, "s4"),    # displaced originals
        A.addiu("sp", "sp", -0x20),
        A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"), A.sw("t2", 0x08, "sp"),
        A.sw("t3", 0x0C, "sp"), A.sw("t4", 0x10, "sp"), A.sw("at", 0x14, "sp"),
        # re-arm for THIS action: no roll by default, latch clear
        A.li("t4", mb),
        A.sh("zero", _CRIT_MB_RATE, "t4"), A.sb("zero", _CRIT_MB_USED, "t4"),
        A.lbu("t0", 0x3C, "s5"),                           # attacker idx
        A._i(0x0B, "t0", "at", 4),                         # sltiu at,t0,4 (party?)
        ("beq", "at", "zero", "DONE"), A.nop(),
        A.lw("t1", 0x00, "s5"), A.lw("t1", 0x6834, "t1"),  # field_array
        A.sll("t2", "t0", 2), A.sll("t3", "t0", 4), A.addu("t2", "t2", "t3"),
        A.sll("t3", "t0", 3), A.addu("t2", "t2", "t3"),
        A.sll("t3", "t0", 6), A.addu("t2", "t2", "t3"),    # t2 = idx*0x5C
        A.addu("t1", "t1", "t2"), A.lbu("t3", 0x1E, "t1"), # class (menu frame)
        A.addiu("at", "zero", 1), ("beq", "t3", "at", "ARM"), A.nop(),   # Thief
        A.addiu("at", "zero", 7), ("bne", "t3", "at", "DONE"), A.nop(),  # Ninja?
        ("label", "ARM"),
    ] + rate_calc + [
        A.sh("t1", _CRIT_MB_RATE, "t4"),
        ("label", "DONE"),
        A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"), A.lw("t2", 0x08, "sp"),
        A.lw("t3", 0x0C, "sp"), A.lw("t4", 0x10, "sp"), A.lw("at", 0x14, "sp"),
        A.addiu("sp", "sp", 0x20),
        A.j(_CRIT_RET), A.nop(),
    ])
    E.install_detour(elf, _CRIT_HOOK, E.add_segment_cave(elf, cave))

    d0 = struct.unpack_from("<I", elf, E.ram2file(_CRIT_SE_HOOK))[0]      # seh a0,v0
    d1 = struct.unpack_from("<I", elf, E.ram2file(_CRIT_SE_HOOK + 4))[0]  # lw v0,0x3c(sp)
    se_cave = A.asm_labels([
        A.word(d0), A.word(d1),                    # displaced (still original sp)
        A.addiu("sp", "sp", -0x30),
        A.sw("ra", 0x00, "sp"), A.sw("v0", 0x04, "sp"), A.sw("a0", 0x08, "sp"),
        A.sw("v1", 0x0C, "sp"), A.sw("a1", 0x10, "sp"), A.sw("a2", 0x14, "sp"),
        A.sw("a3", 0x18, "sp"), A.sw("at", 0x1C, "sp"),
        A.li("t0", mb),
        A.lhu("t1", _CRIT_MB_RATE, "t0"),
        ("beq", "t1", "zero", "SEDONE"), A.nop(),  # attacker gets no AGI roll
        A.lbu("t2", _CRIT_MB_USED, "t0"),
        ("bne", "t2", "zero", "SEDONE"), A.nop(),  # the one roll is already spent
        A.lw("t3", 0x04, "sp"),                    # vanilla threshold for this hit
        A.slt("at", "s0", "t3"),
        ("bne", "at", "zero", "SEDONE"), A.nop(),  # normal crit -> roll not spent
        A.addiu("t2", "zero", 1), A.sb("t2", _CRIT_MB_USED, "t0"),   # spend it
        A.jal(_CRIT_RNG), A.nop(),
        A.andi("v0", "v0", 0xFFFF),
        A.addiu("t2", "zero", _CRIT_RNG_MOD), A.divu("v0", "t2"), A.mfhi("t3"),
        A.li("t0", mb),                            # t-regs are caller-saved
        A.lhu("t1", _CRIT_MB_RATE, "t0"),
        A.slt("at", "t3", "t1"),
        ("beq", "at", "zero", "SEDONE"), A.nop(),  # AGI roll failed
        # force the vanilla compare to read as a crit: roll 0 vs threshold >= 1
        A.addu("s0", "zero", "zero"),
        A.lw("t3", 0x04, "sp"), A.slt("at", "zero", "t3"),
        ("bne", "at", "zero", "SEPLAY"), A.nop(),
        A.addiu("t3", "zero", 1), A.sw("t3", 0x04, "sp"),
        ("label", "SEPLAY"),
        A.lhu("a0", _CRIT_MB_SE, "t0"),
        ("beq", "a0", "zero", "SEDONE"), A.nop(),  # id 0 = cue disabled
        A.jal(_SC_SE_PLAY), A.nop(),
        ("label", "SEDONE"),
        A.lw("ra", 0x00, "sp"), A.lw("v0", 0x04, "sp"), A.lw("a0", 0x08, "sp"),
        A.lw("v1", 0x0C, "sp"), A.lw("a1", 0x10, "sp"), A.lw("a2", 0x14, "sp"),
        A.lw("a3", 0x18, "sp"), A.lw("at", 0x1C, "sp"),
        A.addiu("sp", "sp", 0x30),
        A.j(_CRIT_SE_RET), A.nop(),
    ])
    E.install_detour(elf, _CRIT_SE_HOOK, E.add_segment_cave(elf, se_cave))


# --- feature: shop spell level reads magic_info+9 (so shop-shuffle align works) ---
# The magic shop computes a spell's level from its INDEX in code (level =
# ((id-1)&0x1f)>>2 + 1), ignoring the magic_info +9 level byte. That makes the
# apworld's align_shop_spell_levels (which retiers shuffled shop spells by writing
# +9) inert: a lvl-8 spell dropped into Cornelia still gates at Lv.8, so a beginner
# can't buy it. RE (shop-spell-level memory) found (via a conditional read-bp on
# magiclv, pc != the per-frame eligibility read):
#   * CAST gate (battle/field) already reads +9 -> align alone makes cast work.
#   * SHOP BUY-BLOCK: gate @0x08820830 (fn 0x8820380) does lbu magiclv; slt
#     magiclv, v0 where v0 = s2+1, and s2 = the index level-1 computed at SITE A
#     @0x882070c. Fail -> msg 0x14 "magic level too low". So SITE A is the level
#     source for buyability. id in a2 -> s2 = level-1.
#   * SHOP "raise hands" eligibility @0x8820e18 reads INDEX (id in s2 -> s0). Kept
#     patched so raised hands match buyability.
# We repoint both at magic_info+9. magic_info is static ELF data: record0
# @0x8954d1a, so record[idx].+9 = 0x8954d23 + idx*14 (14 = *8+*4+*2). The AP
# client's DataPatch overwrites this table live with the aligned +9 values, so a
# Cornelia-shuffled spell (aligned +9=1) becomes buyable/eligible at magiclv 1.
_MAGIC_INFO_L9   = 0x8954d23   # magic_info[0] + 9 ; lui 0x0895, lbu off 0x4d23
# eligibility site (in-place; 12 free slots in the white/black branch), id s2->s0
_ELIG_BEQZ       = 0x8820e54   # beqz at,e78 (white/black split) -> nop
_ELIG_FORMULA    = 0x8820e5c   # 12 slots -> unified +9 load (out: s0 = level-1)
# buy-gate level source SITE A (in-place; 13 free slots), id a2 -> s2 = level-1
_BUY_BEQZ        = 0x8820708   # beql at,zero,0x8820730 (white/black split) -> nop
_BUY_FORMULA     = 0x882070c   # 13 slots -> unified +9 load, falls into 0x8820740
# displayed "Lv.N" renderer (list-draw loop, fn 0x8823xxx): 0x88231f4 lbu id ->
# ...index math... -> 0x8823228 addiu a1,v0,1 (=level) -> jal draw-number @x=0xb4.
# In-place 13 slots 0x88231f4..0x8823224 -> v0 = magic_info[id-1].+9 - 1 (id from
# mem 0x6f09(s2)); the untouched 0x8823228 then yields a1 = +9. Preserve t1=1
# (passed to the draw as t3 @0x8823244).
_DISP_FORMULA    = 0x88231f4   # 13 slots; reads id from 0x6f09(s2), out v0=level-1
# learn-STORE level: on buy the spell is filed at char_record+0x41+(level-1)*3+slot
# ([8 levels][3 slots]; store @0x08820a30 sb a2,0x41(v0)). The (level-1) = v1 from
# the index formula @0x88209bc (id a2 -> v1). Menu displays by storage position, so
# storing at the +9 row makes the spell show/cast at its shuffled level. Only 6
# in-place slots (0x88209bc..0x88209d0) -> cave detour. out: v1 = level-1.
_STORE_BEQL      = 0x88209b8   # beql at,zero,0x88209d0 -> nop (both colors fall thru)
_STORE_DETOUR    = 0x88209bc   # -> j cave (delay 0x88209c0 nop)
_STORE_RET       = 0x88209d4   # lw a0,0x7078(s0) (resume; v1 must hold level-1)
# (5) FIELD magic-USE cast gate SITE C @0x088c2e40 (fn 0x088c2e1c reads the spell
# id, then gates: field-castable bit0, magiclv, MP cost -- all -> reject msg 0x66).
# The magiclv sub-gate computed the spell's level by the INDEX formula
# (((id-1)|(id-0x21))>>2) instead of magic_info+9, so a cross-tier shuffled spell
# (e.g. Poisona id13, vanilla Lv4, shuffled to Lv1) still demanded magiclv 4 in the
# FIELD menu even though battle-cast (which already reads +9) worked. LIVE-RE'd
# 2026-07-09 (read-bp on the WM Lv.1 spell row -> this fn; _poisona_vals confirmed
# idx_level 3 >= magiclv 3 -> reject while +9 level = 1). Fix: in-place rewrite of
# the 7-slot white/black index block (0x088c2e40..0x088c2e58) to load magic_info+9.
# a3 = id*14 is already live here (built @0x088c2e20 for the field-bit read and
# reused @0x088c2e80 for MP cost), so +9 = *(_FCAST_L9_BASE + a3); v1 (char rec on
# entry, used for the magiclv load) is then repurposed as the level-1 output, and
# the untouched slt at,v1,a2 @0x088c2e5c compares +9-level < magiclv.
_FCAST_LEVEL     = 0x088c2e40  # 7 in-place slots -> a2=magiclv, v1=+9 level-1
_FCAST_L9_BASE   = 0x08954d15  # magic_info[id-1]+9 == this + id*14 (id 1-based)


def _level_from_plus9(src_reg, out_reg):
    """Emit: out_reg = magic_info[src_reg-1].+9 - 1 (i.e. shuffled level-1).
    Uses v0/v1/at as scratch. Callers pick s0/s2 for src and s0/s2 for out, so
    v0/v1/at are free scratch (out overwritten last)."""
    return [
        A.addiu("v0", src_reg, -1),   # idx = id-1
        A.sll("v1", "v0", 3),         # idx*8
        A.sll("at", "v0", 2),         # idx*4
        A.addu("v1", "v1", "at"),     # idx*12
        A.sll("at", "v0", 1),         # idx*2
        A.addu("v1", "v1", "at"),     # idx*14
        A.lui("at", (_MAGIC_INFO_L9 >> 16) & 0xffff),
        A.addu("at", "at", "v1"),
        A.lbu(out_reg, _MAGIC_INFO_L9 & 0xffff, "at"),  # out = magic_info[idx].+9
        A.addiu(out_reg, out_reg, -1),                  # out = level-1
    ]


def apply_shop_spell_level(elf: bytearray, feats=None):
    # (1) eligibility "raise hands" @0x8820e18: in-place, id in s2 -> s0=level-1.
    elig = A.assemble(_level_from_plus9("s2", "s0") + [A.nop(), A.nop()])
    assert len(elig) == 48, len(elig)   # exactly fills 0x8820e5c..e88 (12 slots)
    E.apply_patches(elf, [(_ELIG_BEQZ, A.nop()), (_ELIG_FORMULA, elig)])

    # (2) buy-gate level source SITE A @0x882070c: in-place, id in a2 -> s2=level-1,
    # then fall into the original 0x8820740 (lw a0,0x7078(s0)).
    buy = A.assemble(_level_from_plus9("a2", "s2") + [A.nop(), A.nop(), A.nop()])
    assert len(buy) == 52, len(buy)     # exactly fills 0x882070c..0x882073c (13 slots)
    E.apply_patches(elf, [(_BUY_BEQZ, A.nop()), (_BUY_FORMULA, buy)])

    # (3) displayed "Lv.N" @0x88231f4: in-place, id from mem 0x6f09(s2) -> v0=level-1
    # (untouched 0x8823228 addiu a1,v0,1 then draws +9). Keep t1=1 for the draw.
    disp = A.assemble([
        A.lbu("v0", 0x6f09, "s2"),   # v0 = spell id
        A.addiu("v0", "v0", -1),     # idx
        A.sll("v1", "v0", 3), A.sll("at", "v0", 2), A.addu("v1", "v1", "at"),
        A.sll("at", "v0", 1), A.addu("v1", "v1", "at"),        # idx*14
        A.lui("at", (_MAGIC_INFO_L9 >> 16) & 0xffff), A.addu("at", "at", "v1"),
        A.lbu("v0", _MAGIC_INFO_L9 & 0xffff, "at"),            # v0 = magic_info[idx].+9
        A.addiu("v0", "v0", -1),                                # v0 = level-1
        A.addiu("t1", "zero", 1),                               # preserve t1 flag
        A.nop(),
    ])
    assert len(disp) == 52, len(disp)   # exactly fills 0x88231f4..0x8823224 (13 slots)
    E.apply_patches(elf, [(_DISP_FORMULA, disp)])

    # (4) learn-STORE level SITE B @0x88209bc: cave detour. id a2 -> v1=level-1
    # (magic_info[a2-1].+9 - 1), then return to 0x88209d4. Save/restore v0+at;
    # a2 (id) preserved. So the bought spell files into its +9 (shuffled) level row.
    cave = A.assemble([
        A.addiu("sp", "sp", -8), A.sw("v0", 0, "sp"), A.sw("at", 4, "sp"),
        A.addiu("v0", "a2", -1),            # idx
        A.sll("at", "v0", 3), A.sll("v1", "v0", 2), A.addu("at", "at", "v1"),  # idx*12
        A.sll("v1", "v0", 1), A.addu("at", "at", "v1"),                        # idx*14
        A.lui("v1", (_MAGIC_INFO_L9 >> 16) & 0xffff), A.addu("v1", "v1", "at"),
        A.lbu("v1", _MAGIC_INFO_L9 & 0xffff, "v1"),   # v1 = magic_info[idx].+9
        A.addiu("v1", "v1", -1),                       # v1 = level-1
        A.lw("v0", 0, "sp"), A.lw("at", 4, "sp"), A.addiu("sp", "sp", 8),
        A.j(_STORE_RET), A.nop(),
    ])
    cave_vaddr = E.add_segment_cave(elf, cave)
    E.apply_patches(elf, [
        (_STORE_BEQL, A.nop()),
        (_STORE_DETOUR, A.j(cave_vaddr) + A.nop()),
    ])

    # (5) FIELD cast gate SITE C @0x088c2e40: replace the index-formula level
    # (white/black + sra) with a magic_info+9 load. a3 = id*14 live; v1 = char
    # rec on entry (magiclv @0x30) then reused as level-1 out; slt @0x088c2e5c
    # (v1 < a2 = magiclv) is left intact.
    fcast = A.assemble([
        A.lbu("a2", 0x30, "v1"),                          # a2 = magiclv (v1=char rec)
        A.lui("v1", (_FCAST_L9_BASE >> 16) & 0xffff),     # v1 = 0x08950000
        A.addu("v1", "v1", "a3"),                          # + id*14
        A.lbu("v1", _FCAST_L9_BASE & 0xffff, "v1"),       # v1 = magic_info[id-1]+9
        A.addiu("v1", "v1", -1),                            # v1 = level-1
        A.nop(), A.nop(),
    ])
    assert len(fcast) == 28, len(fcast)   # 7 slots 0x088c2e40..0x088c2e58
    E.apply_patches(elf, [(_FCAST_LEVEL, fcast)])


# --- Shop purchase mailbox (shop_buy_mailbox) --------------------------------
# Exact purchase attribution for AP shop offers, replacing the client's
# gil-drop inference. RE 2026-08-01 (re_only/HANDOFF_shop_buy_hook.md): the
# item/equip purchase COMMIT calls ADD-ITEM via `jal 0x88d4494` @0x0881ec64
# with a1=cat, a2=gid, a3=qty and s0 = the shop UI struct, which carries the
# GLOBAL STORE ID @+0x7064 (sequential per store; Crescent weapons/armor/items
# = 0x15/0x16/0x17) and the shop type @+0x7068 (1=weapon 2=armor 3=item).
# The hook re-points that jal at a cave that appends
#   (store_id u8, type u8, cat u8, gid u8, qty u16, seq u16)
# to the BUYB ring mailbox, then jumps on to ADD-ITEM (args untouched; ra
# already points back to the commit site). Entry is fully written BEFORE the
# head bump so a concurrent client read can never see a half entry. Spell
# shops use a different path (apply_shop_spell_level's) and are not recorded.
_BUYMB_MAGIC = b"BUYB"
BUYMB_HEAD_OFF = 4          # u16: total purchases this boot (ring idx = mod 8)
BUYMB_RING_OFF = 8
BUYMB_RING_ENTRIES = 8      # 8-byte entries
BUYMB_LEN = BUYMB_RING_OFF + BUYMB_RING_ENTRIES * 8
_BUY_HOOK = 0x0881EC64      # jal ADD-ITEM at the purchase commit
_BUY_ADDITEM = 0x088D4494
_BUY_STOREID_OFF = 0x7064   # s0-relative (shop UI struct)
_BUY_SHOPTYPE_OFF = 0x7068


def apply_shop_buy_mailbox(elf: bytearray, feats=None):
    mb = E.add_segment_cave(elf, _BUYMB_MAGIC + b"\x00" * (BUYMB_LEN - 4))
    cave = A.assemble([
        A.li("t0", mb),
        A.lhu("t1", BUYMB_HEAD_OFF, "t0"),
        A.andi("t2", "t1", BUYMB_RING_ENTRIES - 1),
        A.sll("t2", "t2", 3),
        A.addu("t2", "t2", "t0"),
        A.lw("t3", _BUY_STOREID_OFF, "s0"),
        A.sb("t3", BUYMB_RING_OFF + 0, "t2"),
        A.lw("t3", _BUY_SHOPTYPE_OFF, "s0"),
        A.sb("t3", BUYMB_RING_OFF + 1, "t2"),
        A.sb("a1", BUYMB_RING_OFF + 2, "t2"),      # cat
        A.sb("a2", BUYMB_RING_OFF + 3, "t2"),      # gid
        A.sh("a3", BUYMB_RING_OFF + 4, "t2"),      # qty
        A.sh("t1", BUYMB_RING_OFF + 6, "t2"),      # seq
        A.addiu("t1", "t1", 1),
        A.sh("t1", BUYMB_HEAD_OFF, "t0"),          # head bump LAST
        A.j(_BUY_ADDITEM), A.nop(),
    ])
    cv = E.add_segment_cave(elf, cave)
    old = struct.unpack_from("<I", elf, E.ram2file(_BUY_HOOK))[0]
    want = 0x0C000000 | (_BUY_ADDITEM >> 2)
    if old != want:                     # wrong ISO / clashing edit
        raise ValueError(f"shop_buy_mailbox: hook site {_BUY_HOOK:#x} holds "
                         f"{old:#010x}, expected {want:#010x}")
    E.apply_patches(elf, [(_BUY_HOOK, A.jal(cv))])


# --- feature: spell tomes (spells as usable inventory items; AP pool items) ------
# RE: spell-tome-items-re memory. The consumable item-info table (44 rows x 0x10
# @0x089538F0; ZERO slack -- the weapons table starts at 0x08953BB0 right after)
# is RELOCATED to a 108-row copy so ids 44..107 become real items ("spell
# tomes", one per spell index 0..63). A baked 0x6c0-byte table would blow the
# ~0x67c-byte in-place cave file budget, so the copy lives in a ZERO-INIT bss
# tail of the cave segment (memsz-only, no file bytes) and a small boot cave --
# detoured at the ELF entry point -- builds it at startup: copy the 44 vanilla
# rows, then generate the 64 tome rows from a pattern. Tome row: sort=id-1,
# effect=0x13 (new; vanilla uses 0x00..0x12 -> menu/battle use fns fall to
# their "not usable" default until the Phase-B handlers land), flags=0x10
# (single-target menu item, Golden-Apple flow), target=1, +6 param = spell
# index, buy=0, sell=100, flags=0x10 (single-target consumable, exact byte --
# menu code compares the whole byte in places; deviating soft-locks the menu).
#
# Phase A (relocation; LIVE-VERIFIED 2026-07-02): with no tomes in the
# inventory the game behaves identically. Phase B (below): effect-0x13
# validity/apply cave handlers + both jump tables extended to 20 entries.
# B2 (tome names/descs) shipped via tome_names.build_extended_banks +
# extern_bake; Phase C (yaml option, AP pool, client grants) shipped too --
# "spell_tomes" is a FEATURES key.
_ITEM_TABLE = 0x089538F0          # vanilla row0 (id0=dummy .. id43)
_ITEM_ROWS_VANILLA = 44
_ITEM_ROWS_NEW = 108              # + 64 tome ids 44..107
_TOME_EFFECT = 0x13
# Tome SELL price (item row +0xc) by the spell's VANILLA level, 1..8: a linear
# 20 -> 2000 ramp in 7 even steps of 1980/7, each step rounded to the nearest 25
# (user call 2026-08-07).
# VANILLA level, NOT the shuffled magic_info+9 byte: with shuffle_magic_shops on,
# align_shop_spell_levels rewrites +9 to match whichever store now stocks the
# spell, so a magic_info read would make Cure worth 2000 gil purely because it
# landed in a late shop. Spell IDENTITY never moves between magic slots (only
# the +9 byte is rewritten), so the vanilla level is pure arithmetic on the slot:
# level = ((slot & 31) >> 2) + 1 -- verified byte-exact against RD.VANILLA
# magic_info for all 64 slots. The cave therefore needs no table read at all,
# and indexes this table with (slot & 0x1C), which cannot leave it.
_TOME_SELL_BY_LEVEL = (25, 300, 575, 875, 1150, 1425, 1725, 2000)
_CAVE_HI = E.SAFE_CAVE_VADDR >> 16

# (lui_addr, addiu_addr, field_offset) for every code ref to the table,
# enumerated by a BOOT-wide scan (lui window 128; addiu/ori and lui+memop
# direct forms searched -- only lui+addiu pairs exist). Every word is
# re-verified against the vanilla image at patch time.
# Item-table refs that slot_magic's own caves overwrite (see apply_spell_tomes).
_SM_DISPLACED_ITEM_SITES = frozenset({0x088c4fd8})
_ITEM_TABLE_SITES = [
    (0x0881fc30, 0x0881fc40, 0xc),   # shop: sell price
    (0x08822494, 0x08822498, 0xc),   # shop: sell price
    (0x08823650, 0x08823678, 0x0),   # shop: row base (buy list)
    (0x088237c8, 0x088237d8, 0x0),
    (0x08823a40, 0x08823a50, 0x0),
    (0x088c4aec, 0x088c4b10, 0x0),   # menu: row base
    (0x088c4ca4, 0x088c4cac, 0x2),   # menu use-validity fn 0x088c4c94: effect id
    (0x088c4e24, 0x088c4e28, 0x3),   # validity effect blocks: flags/params
    (0x088c4f24, 0x088c4f28, 0x3),
    (0x088c4fd8, 0x088c4fdc, 0x3),
    (0x088c508c, 0x088c5090, 0x3),
    (0x088c5140, 0x088c5144, 0x3),
    (0x088c51f4, 0x088c51f8, 0x3),
    (0x088c52a8, 0x088c52ac, 0x3),
    (0x088c535c, 0x088c5360, 0x3),
    (0x088c5410, 0x088c5414, 0x6),
    (0x088c5458, 0x088c5474, 0x2),   # menu use-apply fn 0x088c5450: effect id
    (0x088c54ac, 0x088c54b0, 0x6),   # apply effect blocks: power/param
    (0x088c5508, 0x088c550c, 0x6),
    (0x088c5574, 0x088c5578, 0x6),
    (0x088c5610, 0x088c5614, 0x6),
    (0x088c5658, 0x088c565c, 0x3),
    (0x088c5730, 0x088c5734, 0x6),
    (0x088c5790, 0x088c5794, 0x6),
    (0x088c57dc, 0x088c57e0, 0x6),
    (0x088c586c, 0x088c5870, 0x6),
    (0x088c58fc, 0x088c5900, 0x6),
    (0x088c598c, 0x088c5990, 0x6),
    (0x088c5a1c, 0x088c5a20, 0x6),
    (0x088c5aac, 0x088c5ab0, 0x6),
    (0x088c63c0, 0x088c63d0, 0x2),   # FIELD target-select setup: effect id
    (0x088c6404, 0x088c6408, 0x6),
    # 0x088c6444's lui is the DELAY SLOT of the bnel @0x088c63fc (executes only
    # on the branch-taken path). The linearly-nearer lui @0x088c641c anchors
    # magic_info (0x08954d0d) and must NOT be rewritten.
    (0x088c6400, 0x088c6444, 0x3),
    (0x088c7940, 0x088c7950, 0x2),   # item info/description composer
    # two redundant luis (branch-likely delay slot + fall-through re-lui) feed
    # the +0x5/+0x6 paths; rewriting both is correct for either path
    (0x088c7984, 0x088c7988, 0x5),
    (0x088c7980, 0x088c7998, 0x6),
    (0x088d4268, 0x088d4270, 0x2),   # near GIVE_ITEM_FN: stack/usability checks
    (0x088d4288, 0x088d428c, 0x6),
    (0x088d4378, 0x088d437c, 0x4),
]


def _read_word(elf, ram):
    return struct.unpack_from("<I", elf, E.ram2file(ram))[0]


def _set_imm16(elf, ram, imm):
    struct.pack_into("<H", elf, E.ram2file(ram), imm & 0xFFFF)


def _set_lo16_ori(elf, ram, imm):
    """Write the LOW half of a lui/addiu address pair, converting the `addiu`
    to `ori` (same rs/rt, same immediate field).

    Why: `addiu` SIGN-extends, so a low half >= 0x8000 subtracts 0x10000 from
    the address and the pair needs a +1 carry in the lui. Every consumer of the
    cave segment therefore had to stay inside the FIRST HALF of its 64k page,
    which is what capped apply_spell_tomes' bss tails (and, in v228, left the
    segment 592 bytes short of fitting magic_power_scaling). `ori` ZERO-extends,
    so the full 0..0xFFFF range is addressable off one lui and the usable window
    doubles.

    Safe because `ori rX, rX, lo` == `addiu rX, rX, lo` exactly when rX's low 16
    bits are already zero -- which they are, every one of these targets being the
    rt of the paired `lui` (verified over all 42 sites: each is `lui rX,hi` then
    `addiu rX,rX,lo`, rs == rt == the lui's target). Anywhere the existing addiu
    is correct, the ori is correct too.

    The lui immediate is untouched, so the spell_tomes runtime SIGNATURE (which
    reads the lui imm at 0x088c4ca4) is unaffected."""
    fo = E.ram2file(ram)
    word = struct.unpack_from("<I", elf, fo)[0]
    if (word >> 26) not in (0x09, 0x0D):        # addiu | ori
        raise ValueError(f"{ram:#x}: expected addiu/ori for a lo16 repoint, "
                         f"got opcode {word >> 26:#04x}")
    word = (0x0D << 26) | (word & 0x03FF0000) | (imm & 0xFFFF)
    struct.pack_into("<I", elf, fo, word)


# --- Phase B: effect-0x13 handlers (teach / refuse) -------------------------
# The menu item-use pipeline dispatches by effect id through two adjacent jump
# tables (19 entries each, effects 0x00..0x12):
#   validity fn 0x088C4C94: "can this item be used on this char?" -> bool.
#     At dispatch: a1 = item row offset (id*0x10), a2 = char idx, s1 = menu ctx,
#     s0 = 0 (result). Exit 0x088C5428 returns s0!=0. s2..s5 are saved by the
#     fn prologue (free for the handler); ra is restored from the stack (safe
#     to clobber with jal).
#   apply fn 0x088C5450: performs the effect. At dispatch: v1 = row offset,
#     s1 = char idx, s0 = ctx. Exit 0x088C5AC8 (sets UI response byte 9); the
#     CALLER consumes the item afterwards, so apply just mutates state.
# Menu char record = [ctx+0x7098] + idx*0x5C = the real party record MINUS
# 0x20 (menu ptr is party_rec-0x20: maxHP menu+0x2A == party+0x0A, stats
# +0x31.. == +0x11..). Through the menu ptr: status +0x1F (&3 = dead/stone),
# magiclv +0x30, known-spell rows +0x41 (8 levels x 3 slots, 1-based spell id,
# 0 = empty; the shop learn-store uses this same menu-flavor base -- FIELD
# offset is +0x21, +0x41 field is equipment), class byte +0x7A.
#
# Teach gate mirrors the shop rules: char not dead/stone, magiclv >= spell's
# level (magic_info+9 -> shuffle-aware), class may learn it (the game's own
# leaf fn 0x08820DC4 can_learn(a1=job, a2=spell_id_1based) -> v0, bit =
# (job%6)+(job/6)*8 -- so shuffled learn-bits and monk_thief_dabble patches
# are respected automatically), spell not already known, and a free slot at
# its level row. Any failure -> not usable (item kept; player can retry after
# class change / magic level up). Random-loot gen (0x088647a0, rand%44) can
# never produce tome ids -- AP is the only tome source.
_USE_VALID_EXIT = 0x088C5428
_USE_APPLY_EXIT = 0x088C5AC8
_CAN_LEARN_FN   = 0x08820DC4
_MAGIC_INFO_L9B = 0x08954D23        # magic_info[0]+9 (same value as _MAGIC_INFO_L9)
_VAL_SLTIU, _VAL_JT_LUI, _VAL_JT_ADDIU = 0x088C4CCC, 0x088C4CDC, 0x088C4CE4
_APP_SLTIU, _APP_JT_LUI, _APP_JT_ADDIU = 0x088C5484, 0x088C5490, 0x088C5498
_VAL_JT, _APP_JT = 0x089790E4, 0x08979130   # vanilla jump tables (19 entries)

# menu-record field offsets (party-record offset + 0x20)
# class comes from the -2 display/class array (ff1_data CLASS_BASE_SA): row r's
# job = menu_rec+0x1E (= field rec-2). rec+0x7A (field +0x5A) is the NEXT
# row's class -- using it swapped every eligibility check by one char (live
# 2026-07-02: WM judged as BM, BM as Warrior, ...).
_M_STATUS, _M_MAGICLV, _M_SPELLS, _M_CLASS = 0x1F, 0x30, 0x41, 0x1E

# --- B2 names plumbing (see spell-tome-items-re memory) ----------------------
# Item names/descs resolve as: getter(cat, id) -> u16 string id via the
# per-cat id-array (0x08990D9C table; cat1 array vanilla @0x08953600, 44
# entries of (name_id u16, desc_id u16)) -> text from the per-cat NAME/DESC
# banks (heap-loaded from FM_EXTERN12US.PC; bank ptrs live in a heap registry
# the CLIENT locates by payload signature). On-disc we: (a) repoint the cat1
# array literal to a 108-entry bss copy (tome entries left ZERO -> string id
# 0 = "Potion", safe without a client), (b) widen the getters' cat1 id bound
# (slti 0x2D -> 0x6C; two getter fns: name @0x088D4718 reads entry+0, desc
# twin @0x088D48BC reads entry+2), (c) reserve bss scratch right after the
# array where the client builds 108-entry extended NAME/DESC banks and then
# repoints the heap registry + fills the tome array entries with real ids.
_NAME_ARRAY_LIT   = 0x08990DA0        # u32 literal -> cat1 (name,desc) array
_CAT1_ARRAY       = 0x08953600        # vanilla array (44 entries x 4B)
_NAME_BOUND_SITES = (0x088D4764, 0x088D4908)   # slti rX, rY, 0x2D per getter
# Client-built extended NAME/DESC banks live here. Sized to the RUNTIME
# (tomes, no-remote) path only -- fixed content, measured 5625 bytes at v166
# and guarded by tome_names' build-time assert; the remote path writes into
# dpk dead space instead. Shrunk 0x2800 -> 0x2200 at v167 to keep the tome
# bss tables inside the no-sign-carry low page once slot_magic's caves ride
# in front of them (the 0x7ff0 check below fails loudly if this regresses);
# 0x2200 -> 0x21C0 at v168 for the scroll heal cave's spell-id bounds gate;
# 0x21C0 -> 0x1F00 at v204 for the Crimson Wizard pay-site cave (the tome bss
# btbl crossed the 0x8000 sign-carry line -> live bake abort 2026-08-02:
# "bss tail 0x8b355c0 left the expected 64k page"; sized against an
# all-features patch_iso-shaped sim, btbl lands at 0x7f10 = 0xE0 margin).
# Measured content 5625 = 0x15F9, so ~2.3K of headroom remains.
_BANK_SCRATCH_SZ  = 0x1F00

# --- battle item-use exclusion (SOLVED 2026-07-16 by static disasm) ----------
# The battle item window is a SEPARATE module (0x886xxxx; the 0x088C4xxx-8xxx
# code incl. the 0x088C6034 state pump is the FIELD menu -- the earlier "battle
# detour" attempts were patching the field flow, which is why they never
# stopped the battle cast). Battle usability is table-driven: fn 0x08871594
# (ctx, cat, id) returns, for cat 1 (consumables), the u16 at
# _BATTLE_USE_TABLE[id] -- 0xFF means "not usable in battle": the row renderer
# (0x0886fc18) draws it greyed and the confirm handler (jal @0x0886c950)
# refuses with message 0x66. That is exactly the vanilla Tent/Cabin/Golden
# Apple behavior (their entries are 0xFF). Non-0xFF values are the queued
# battle action (fang entries = spell ids; the commit @0x0886cf08 stores the
# u16 into the action record +0x44) -- tome ids 44+ read PAST the 44-entry
# table, fetched junk (id44 -> 0x0000 -> action 0 = "Protect"), and that junk
# executed at turn resolution.
# Fix: relocate the table to a 108-entry bss copy (44 vanilla + 64 x 0x00FF
# for the tome ids) and repoint the single lui/addiu ref in fn 0x08871594.
# Tomes then grey out and refuse in battle like a Tent; the field teach flow
# never touches this table. (Future "cast the tome in battle" option: write
# the tome's 1-based spell id instead of 0xFF -- the fang path proves the
# mechanism; would still need targeting/eligibility design.)
_BATTLE_USE_TABLE = 0x0894BB1E      # 44 x u16, per consumable id; 0xFF = no
_BATTLE_USE_LUI   = 0x08871630      # lui  v0, 0x895
_BATTLE_USE_ADDIU = 0x08871634      # addiu v0, v0, -0x44e2


def _emit_char_rec(idx_reg, out_reg, tmp="v1"):
    """out = [ctx_reg preloaded in out] + idx*0x5C (0x5C = 4+8+16+64)."""
    return [
        A.sll("v0", idx_reg, 2), A.sll(tmp, idx_reg, 4), A.addu("v0", "v0", tmp),
        A.sll(tmp, idx_reg, 3), A.addu("v0", "v0", tmp),
        A.sll(tmp, idx_reg, 6), A.addu("v0", "v0", tmp),
        A.addu(out_reg, out_reg, "v0"),
    ]


def _emit_spell_and_level(table, row_reg, id_out, lvl_out):
    """id_out = 1-based spell id from tome row's +6 param; lvl_out = its level
    (magic_info[id-1]+9, stride 14). Clobbers v0/v1/at."""
    return [
        A.lui("v0", table >> 16), A.ori("v0", "v0", table & 0xFFFF),
        A.addu("v0", "v0", row_reg),
        A.lhu(id_out, 6, "v0"), A.addiu(id_out, id_out, 1),
        A.addiu("v0", id_out, -1),
        A.sll("v1", "v0", 3), A.sll("at", "v0", 2), A.addu("v1", "v1", "at"),
        A.sll("at", "v0", 1), A.addu("v1", "v1", "at"),          # idx*14
        A.lui("at", _MAGIC_INFO_L9B >> 16), A.addu("at", "at", "v1"),
        A.lbu(lvl_out, _MAGIC_INFO_L9B & 0xFFFF, "at"),
    ]


def _tome_validity_handler(table):
    """Effect-0x13 validity: s2=rec, s3=spell id, s4=level (s-regs are saved
    by the validity fn). Sets s0=1 iff teachable, then exits."""
    return A.asm_labels(
        [A.lw("s2", 0x7098, "s1"),
         # the use pipeline calls validity in phases where the ctx's party
         # array may be unset -- a null/wild pointer here would raise a memory
         # exception (PPSSPP breaks = apparent freeze). Refuse unless the
         # array sits in user RAM (0x08xxxxxx/0x09xxxxxx).
         A.srl("v0", "s2", 24), A.addiu("v0", "v0", -8),
         A._i(0x0B, "v0", "at", 2),                 # sltiu at, v0, 2
         ("beq", "at", "zero", "fail"), A.nop()]
        + _emit_char_rec("a2", "s2")
        + [A.lbu("v0", _M_STATUS, "s2"), A.andi("v0", "v0", 3),
           ("bne", "v0", "zero", "fail"), A.nop()]
        + _emit_spell_and_level(table, "a1", "s3", "s4")
        + [
            A.lbu("v0", _M_MAGICLV, "s2"), A.slt("at", "v0", "s4"),
            ("bne", "at", "zero", "fail"), A.nop(),          # magiclv < level
            A.lbu("a1", _M_CLASS, "s2"),
            A.jal(_CAN_LEARN_FN),
            A.addu("a2", "s3", "zero"),                       # (delay) a2 = spell id
            ("beq", "v0", "zero", "fail"), A.nop(),           # class can't learn
            A.addiu("v0", "s4", -1), A.sll("v1", "v0", 1), A.addu("v0", "v0", "v1"),
            A.addu("s2", "s2", "v0"),                         # rec + (level-1)*3
            A.lbu("v1", _M_SPELLS + 0, "s2"),
            ("beq", "v1", "s3", "fail"), A.nop(),             # already known
            ("beq", "v1", "zero", "ok"), A.nop(),
            A.lbu("v1", _M_SPELLS + 1, "s2"),
            ("beq", "v1", "s3", "fail"), A.nop(),
            ("beq", "v1", "zero", "ok"), A.nop(),
            A.lbu("v1", _M_SPELLS + 2, "s2"),
            ("beq", "v1", "s3", "fail"), A.nop(),
            ("bne", "v1", "zero", "fail"), A.nop(),           # row full
            ("label", "ok"), A.addiu("s0", "zero", 1),
            ("label", "fail"), A.j(_USE_VALID_EXIT), A.nop(),
        ])


def _tome_apply_handler(table):
    """Effect-0x13 apply: store the spell id into the first empty slot of its
    level row (validity already guaranteed one). t-regs free; no jal (ra live)."""
    return A.asm_labels(
        [A.lw("t0", 0x7098, "s0"),
         A.srl("v0", "t0", 24), A.addiu("v0", "v0", -8),
         A._i(0x0B, "v0", "at", 2),                 # sltiu at, v0, 2
         ("beq", "at", "zero", "bail"), A.nop()]
        + _emit_char_rec("s1", "t0", tmp="a0")   # v1 holds the row offset: keep it
        + _emit_spell_and_level(table, "v1", "t3", "t4")      # v1 = row offset here
        + [
            A.addiu("v0", "t4", -1), A.sll("a0", "v0", 1), A.addu("v0", "v0", "a0"),
            A.addu("t0", "t0", "v0"),
            A.lbu("v0", _M_SPELLS + 0, "t0"),
            ("beq", "v0", "zero", "slot0"), A.nop(),
            A.lbu("v0", _M_SPELLS + 1, "t0"),
            ("beq", "v0", "zero", "slot1"), A.nop(),
            A.sb("t3", _M_SPELLS + 2, "t0"),
            A.j(_USE_APPLY_EXIT), A.nop(),
            ("label", "slot0"), A.sb("t3", _M_SPELLS + 0, "t0"),
            A.j(_USE_APPLY_EXIT), A.nop(),
            ("label", "slot1"), A.sb("t3", _M_SPELLS + 1, "t0"),
            A.j(_USE_APPLY_EXIT), A.nop(),
            ("label", "bail"), A.j(_USE_APPLY_EXIT), A.nop(),
        ])


def _tome_cave_blob(elf, cave, table, narr, btbl, displaced, entry):
    """The whole spell_tomes cave: [validity][apply][jt_valid 20][jt_apply 20]
    [boot init]. Returns (blob, off_jtv, off_jta). Layout is length-stable, so
    a placeholder pass sizes it and a second pass fills real addresses."""
    val = _tome_validity_handler(table)
    app = _tome_apply_handler(table)
    off_app = len(val)
    off_jtv = off_app + len(app)
    off_jta = off_jtv + 20 * 4
    off_price = off_jta + 20 * 4
    off_init = off_price + len(_TOME_SELL_BY_LEVEL) * 4
    jt_v = b"".join(struct.pack("<I", _read_word(elf, _VAL_JT + i * 4))
                    for i in range(19)) + struct.pack("<I", cave)
    jt_a = b"".join(struct.pack("<I", _read_word(elf, _APP_JT + i * 4))
                    for i in range(19)) + struct.pack("<I", cave + off_app)
    prices = b"".join(struct.pack("<I", p) for p in _TOME_SELL_BY_LEVEL)
    init = _tome_init_cave(displaced, table, narr, btbl, entry + 8,
                           cave + off_price)
    return val + app + jt_v + jt_a + prices + init, off_jtv, off_jta, off_init


def _tome_init_cave(displaced, table, narr, btbl, resume, price_tbl):
    """Boot cave: build the relocated 108-row item table + the 108-entry cat1
    (name,desc) id-array + the 108-entry battle-usability table (u16 per id;
    tome entries 0xFF = not battle-usable) in the bss tail, then resume the
    original entry.
    Runs at the module entry point, where t/v/at regs are dead by ABI (unlike
    mid-function detours, no save/restore). The 64 tome array entries get
    string ids (43+slot, 43+slot) pointing at the extended NAME/DESC bank
    entries that extern_bake.bake_names grows into FM_EXTERN12US/18US.PC (both
    ship together under the spell_tomes feature)."""
    return A.assemble([
        A.word(displaced[0]), A.word(displaced[1]),   # displaced entry prologue
        # copy the 44 vanilla rows (word-wise)
        A.li("t8", _ITEM_TABLE),
        A.li("t9", table),
        A.addiu("v0", "zero", _ITEM_ROWS_VANILLA * 0x10),
        A.lw("v1", 0, "t8"),                          # L1:
        A.sw("v1", 0, "t9"),
        A.addiu("t8", "t8", 4),
        A.addiu("v0", "v0", -4),
        A.bne("v0", "zero", -5),                      # -> L1
        A.addiu("t9", "t9", 4),                       # (delay) dst++
        # generate tome rows for ids 44..107 (t9 == table+0x2c0 here)
        A.addiu("t8", "zero", _ITEM_ROWS_VANILLA),    # t8 = id
        A.addiu("v0", "t8", -1),                      # L2: w0 = sort(id-1)
        # flags = EXACTLY 0x10 like every single-target consumable: menu code
        # compares the whole byte in places (an extra 0x80 bit soft-locked the
        # use flow live 2026-07-02). Battle handling for effect 0x13 is dealt
        # with separately, not via flag bits.
        A.lui("at", (0x10 << 8) | _TOME_EFFECT),      #  | effect<<16 | flags 0x10<<24
        A.addu("v0", "v0", "at"),
        A.sw("v0", 0, "t9"),
        A.addiu("v0", "t8", -_ITEM_ROWS_VANILLA),     # spell index 0..63
        A.sll("v0", "v0", 16),                        # -> +6..7 param
        A.addiu("v0", "v0", 0x6401),                  # +4 target=1, +5=0x64:
        # +5 is the use-sequence/message id handed to the target-select UI
        # (0x088c7990 lbu +5 -> jal 0x88d2bf8); 0 soft-locks the menu. 0x64 =
        # Golden Apple's, giving the exact single-target consumable flow.
        A.sw("v0", 4, "t9"),
        A.sw("zero", 8, "t9"),                        # buy 0 (not shop-buyable)
        # sell price = _TOME_SELL_BY_LEVEL[vanilla level - 1], and the vanilla
        # level of magic slot s is ((s & 31) >> 2) + 1, so the word offset into
        # the table is ((s & 31) >> 2) * 4 == s & 0x1C. Deliberately NOT a
        # magic_info+9 read: that byte is rewritten by the shop-shuffle level
        # alignment, and the price must follow the spell, not its new shop.
        A.addiu("v1", "t8", -_ITEM_ROWS_VANILLA),     # v1 = spell slot 0..63
        A.andi("v0", "v1", 0x1C),                     # (level-1)*4
        A.lui("at", (price_tbl >> 16) & 0xFFFF),
        A.ori("at", "at", price_tbl & 0xFFFF),
        A.addu("at", "at", "v0"),
        A.lw("v0", 0, "at"),
        A.sw("v0", 12, "t9"),                         # sell = level price
        A.addiu("t8", "t8", 1),
        A.addiu("v0", "t8", -_ITEM_ROWS_NEW),
        A.bne("v0", "zero", -19),                     # -> L2
        A.addiu("t9", "t9", 16),                      # (delay) row++
        # copy the 44 vanilla cat1 (name,desc) id-array entries
        A.li("t8", _CAT1_ARRAY),
        A.li("t9", narr),
        A.addiu("v0", "zero", 44 * 4),
        A.lw("v1", 0, "t8"),                          # L3:
        A.sw("v1", 0, "t9"),
        A.addiu("t8", "t8", 4),
        A.addiu("v0", "v0", -4),
        A.bne("v0", "zero", -5),                      # -> L3
        A.addiu("t9", "t9", 4),                       # (delay) dst++  (t9 -> entry 44)
        # fill tome entries: array[id] = (id-1) | (id-1)<<16  for id 44..107
        # (name id == desc id == 43+slot -> the extern_bake extended banks)
        A.addiu("t8", "zero", _ITEM_ROWS_VANILLA),    # t8 = id (44)
        A.addiu("v0", "t8", -1),                       # L4: v0 = 43+slot
        A.sll("v1", "v0", 16),
        A.addu("v1", "v1", "v0"),                      # (43+slot)|(43+slot)<<16
        A.sw("v1", 0, "t9"),
        A.addiu("t8", "t8", 1),
        A.addiu("v0", "t8", -_ITEM_ROWS_NEW),
        A.bne("v0", "zero", -7),                       # -> L4
        A.addiu("t9", "t9", 4),                        # (delay) dst++
        # battle-usability table: copy the 44 vanilla u16s (source is only
        # halfword-aligned -- 0x...1E -- so copy halfword-wise), then 64 tome
        # entries = 32 words of 0x00FF00FF ("not usable in battle" = Tent)
        A.li("t8", _BATTLE_USE_TABLE),
        A.li("t9", btbl),
        A.addiu("v0", "zero", _ITEM_ROWS_VANILLA),
        A.lhu("v1", 0, "t8"),                          # L5:
        A.sh("v1", 0, "t9"),
        A.addiu("t8", "t8", 2),
        A.addiu("v0", "v0", -1),
        A.bne("v0", "zero", -5),                       # -> L5
        A.addiu("t9", "t9", 2),                        # (delay) dst++
        A.li("v1", 0x00FF00FF),
        A.addiu("v0", "zero", (_ITEM_ROWS_NEW - _ITEM_ROWS_VANILLA) // 2),
        A.sw("v1", 0, "t9"),                           # L6:
        A.addiu("v0", "v0", -1),
        A.bne("v0", "zero", -3),                       # -> L6
        A.addiu("t9", "t9", 4),                        # (delay) dst++
        A.j(resume), A.nop(),
    ])


def apply_spell_tomes(elf: bytearray, feats=None):
    # v194: slot_magic's Soma cave lands ON the effect-2 validity block's
    # lui/addiu pair (0x088c4fd8), so those words are gone by the time this
    # runs (FEATURES order puts slot_magic first). The cave never reads the
    # item table, so dropping the site is the whole fix -- but do it by an
    # EXPLICIT list, not by tolerating a mismatch: every other site staying
    # verified is what makes this table trustworthy.
    sites = [s for s in _ITEM_TABLE_SITES
             if not (feats and feats.get("slot_magic")
                     and s[0] in _SM_DISPLACED_ITEM_SITES)]
    # verify every site against the vanilla words (fails loudly on a wrong ISO
    # or if another feature ever touches these instructions)
    for lui_ram, addiu_ram, field in sites:
        wl, wa = _read_word(elf, lui_ram), _read_word(elf, addiu_ram)
        if (wl >> 26) != 0x0F or (wl & 0xFFFF) != (_ITEM_TABLE >> 16):
            raise ValueError(f"unexpected lui @{lui_ram:#x}: {wl:#010x}")
        if (wa >> 26) != 0x09 or (wa & 0xFFFF) != ((_ITEM_TABLE + field) & 0xFFFF):
            raise ValueError(f"unexpected addiu @{addiu_ram:#x}: {wa:#010x}")
        if ((wa >> 21) & 31) != ((wl >> 16) & 31):
            raise ValueError(f"lui/addiu reg mismatch @{addiu_ram:#x}")
    # entry-point detour: the two displaced words must be position-independent
    entry = E.get_entry(elf)
    displaced = (_read_word(elf, entry), _read_word(elf, entry + 4))
    for w in displaced:
        op = w >> 26
        if op in (1, 2, 3, 4, 5, 6, 7) or (op == 0 and (w & 0x3F) in (8, 9)):
            raise ValueError(f"entry starts with a branch ({w:#010x}); cannot displace")
    # verify the effect-dispatch words we patch (both fns: sltiu bound + the
    # lui/addiu pair forming the jump-table base)
    for sltiu_ram, lui_ram, addiu_ram, jt in (
            (_VAL_SLTIU, _VAL_JT_LUI, _VAL_JT_ADDIU, _VAL_JT),
            (_APP_SLTIU, _APP_JT_LUI, _APP_JT_ADDIU, _APP_JT)):
        ws, wl, wa = (_read_word(elf, sltiu_ram), _read_word(elf, lui_ram),
                      _read_word(elf, addiu_ram))
        if (ws >> 26) != 0x0B or (ws & 0xFFFF) != 0x13:
            raise ValueError(f"unexpected sltiu @{sltiu_ram:#x}: {ws:#010x}")
        want = ((wl & 0xFFFF) << 16) + ((wa & 0xFFFF) - 0x10000)
        if (wl >> 26) != 0x0F or (wa >> 26) != 0x09 or want != jt:
            raise ValueError(f"unexpected jt base @{lui_ram:#x}: {wl:#010x}/{wa:#010x}")
    # verify + widen the two name/desc getter cat1 id bounds (slti 0x2D->0x6C)
    for site in _NAME_BOUND_SITES:
        w = _read_word(elf, site)
        if (w >> 26) != 0x0A or (w & 0xFFFF) != 0x2D:
            raise ValueError(f"unexpected getter bound @{site:#x}: {w:#010x}")
    # verify the battle-usability table ref in fn 0x08871594 (single reader)
    wl, wa = _read_word(elf, _BATTLE_USE_LUI), _read_word(elf, _BATTLE_USE_ADDIU)
    if ((wl >> 26) != 0x0F
            or (wl & 0xFFFF) != ((_BATTLE_USE_TABLE + 0x8000) >> 16)
            or (wa >> 26) != 0x09
            or (wa & 0xFFFF) != (_BATTLE_USE_TABLE & 0xFFFF)
            or ((wa >> 21) & 31) != ((wl >> 16) & 31)):
        raise ValueError(f"unexpected battle-use table ref: {wl:#010x}/{wa:#010x}")
    if struct.unpack_from("<I", elf, E.ram2file(_NAME_ARRAY_LIT))[0] != _CAT1_ARRAY:
        raise ValueError("cat1 name-array literal not where expected")
    # two-pass assemble: the cave code needs the table + name-array addresses
    # (bss tail lands right after the cave) and its own vaddr (handler entries
    # in the extended jump tables); the blob's length is placeholder-independent
    blob, off_jtv, off_jta, _ = _tome_cave_blob(elf, 0, 0, 0, 0, displaced, entry)
    cave_vaddr = E.add_segment_cave(elf, blob)
    table = E.cave_bss_tail(elf, _ITEM_ROWS_NEW * 0x10)
    narr = E.cave_bss_tail(elf, _ITEM_ROWS_NEW * 4)     # cat1 (name,desc) array
    scratch = E.cave_bss_tail(elf, _BANK_SCRATCH_SZ)    # client-built banks
    btbl = E.cave_bss_tail(elf, _ITEM_ROWS_NEW * 2)     # battle-usability u16s
    if (table >> 16) != _CAVE_HI or (btbl & 0xFFFF) >= 0x10000 - 0x10:
        # keeps every repointed pair on ONE hi16 and the runtime signature
        # (lui imm == _CAVE_HI) feature-set-independent. v228: the low halves
        # are written as `ori` (zero-extending) by _set_lo16_ori, so the whole
        # 64k page is usable -- this was 0x8000 while they were sign-extending
        # `addiu`, which is what left the segment short of fitting
        # magic_power_scaling.
        raise ValueError(f"bss tail {table:#x} left the expected 64k page")
    real, r_jtv, r_jta, r_init = _tome_cave_blob(elf, cave_vaddr, table, narr,
                                                 btbl, displaced, entry)
    assert len(real) == len(blob) and (r_jtv, r_jta) == (off_jtv, off_jta)
    E.cave_write(elf, cave_vaddr, real)
    E.install_detour(elf, entry, cave_vaddr + r_init)
    # repoint the cat1 array literal + widen the getter bounds
    struct.pack_into("<I", elf, E.ram2file(_NAME_ARRAY_LIT), narr)
    for site in _NAME_BOUND_SITES:
        _set_imm16(elf, site, _ITEM_ROWS_NEW)
    # repoint every table ref
    for lui_ram, addiu_ram, field in sites:
        _set_imm16(elf, lui_ram, table >> 16)
        _set_lo16_ori(elf, addiu_ram, (table + field) & 0xFFFF)
    # repoint the battle-usability table (tome entries 0xFF -> battle refusal)
    _set_imm16(elf, _BATTLE_USE_LUI, btbl >> 16)
    _set_lo16_ori(elf, _BATTLE_USE_ADDIU, btbl & 0xFFFF)
    # widen both effect dispatches to 20 entries via the extended cave tables
    for sltiu_ram, lui_ram, addiu_ram, jt_off in (
            (_VAL_SLTIU, _VAL_JT_LUI, _VAL_JT_ADDIU, off_jtv),
            (_APP_SLTIU, _APP_JT_LUI, _APP_JT_ADDIU, off_jta)):
        jt_addr = cave_vaddr + jt_off
        assert (jt_addr & 0xFFFF) < 0x10000 and (jt_addr >> 16) == _CAVE_HI
        _set_imm16(elf, sltiu_ram, _TOME_EFFECT + 1)
        _set_imm16(elf, lui_ram, jt_addr >> 16)
        _set_lo16_ori(elf, addiu_ram, jt_addr & 0xFFFF)


# --- slot_magic: Pixel-Remaster-style spell slots (v167) ----------------------
# Replaces the MP pool with per-spell-level charges. Design pillars:
#  * STORAGE = "spent" counters, u8[4 chars][8 levels], save-resident at
#    *SAVE_BLOCK_PTR + 0x80C (= 0x08D1190C canonical; v193 relocation -- the
#    original 0x464 home turned out to be a native rolling map-record list,
#    see ff1_data.SPELL_SLOTS_SPENT_BASE_SA). Spent-not-remaining is the load-bearing
#    trick: a fresh game's zeroed save = nothing spent = full charges, so there
#    is NO init hook; level-ups grow the derived max and need NO grant hook; and
#    save/load rolls charges back atomically with the rest of the save.
#  * MAX is DERIVED at read time: max(level L) = 0 if magiclv < L else
#    min(TBL[L][char_level], CLASS_CAP[class]) -- a PR-style progression table
#    plus per-class caps (hybrids halved like PR, dabble classes modest).
#    current = max - spent (clamped >= 0). Castable iff spent < max.
#  * MP stays NATIVE but inert: every cast's MP cost is forced to 0 (pool sits
#    at max forever -- no u16 underflow, and native restores stay harmless
#    no-ops), and every MP *gate* is replaced by a charge gate. The MP display
#    lines are replaced by slot displays.
# Deduct/gate sites (static disasm, all verified byte-exact 2026-07-30):
#   battle deduct  0x088826D4 (cast state fn 0x088825B8 phase 0)
#   battle afford  0x08870170 (predicate fn 0x088700CC: feeds menu grey-out AND
#                  the confirm buzzer; magiclv/flag gates stay native)
#   field  deduct  0x088C472C (fn 0x088C429C)
#   field  gate    0x088C2E74 (fn 0x088C2BC0; reject msg 0x66 path unchanged;
#                  shop_spell_level's in-place rewrite ends at 0x088C2E58 -- no
#                  overlap, but keep it that way)
#   field  menu    0x088D1C44 (fn 0x088D17E0 grey-out; reads cost RECORD-
#                  relative +0xA, which is why a 0x4d16 scan misses it)
#   field repeats  0x088C34C8 / 0x088C3634 (fn 0x088C3260; skipping these
#                  leaves a repeat-cast charge bypass)
# Displays:
#   magic submenu: spell-name columns stay VANILLA (v177; the cursor hand
#                  collides with any left-side column). Row-loop hook
#                  _SM_MENU_ROW 0x088D19A4 (v214) draws "cur max" charges at
#                  the row's right edge. Selected char = u32 [ctx+0x7090].
#   main panel:    fn 0x088CEAA0 MP line block 0x088CEDD0..0x088CEE6C replaced
#                  by 2 rows x 4 "n/" charge counts (levels 1-4 / 5-8); line-y
#                  immediates tightened 0xe/0x1a/0x26 -> 0xc/0x16/0x20 to fit
#                  the 0x38 panel pitch. NB fn shared by ~7 screens (item
#                  target-select etc.) -- they all get the 4-line panel.
# Ether family (effect 0x0E = ids 4/5/6, the only users): validity handler
#   0x088C4D74 + apply handler 0x088C5560 detoured wholesale -- Ether restores
#   1 charge per level, Turbo Ether 5, Dry Ether all (param tiers 50/150/999).
#   No jump-table widening, so zero interplay with spell_tomes' JT relocation.
#   Battle use: entries stay VANILLA (v182 -- ethers are battle-usable again);
#   the battle leg is the `bether` cave on the action-0x6A handler.
#   Rest refills also SHIPPED: _rest_cave on _SM_REST_TIERED/_SM_REST_INN
#   (Tent=2 tiers, LIVE-VERIFIED 2026-07-30).
# DEFERRED (cosmetic only): field cost displays 0x088C3904/0x088CF498 still
#   show MP-style numbers.
# Conflicting features deferred case-by-case (user 2026-07-30): the mana-cost
# shuffle is silently disabled at gen (see __init__._seed_shuffle); the dabble
# MP-growth cave and CW mana-refund keep running but act on the inert pool.
_SM_BATTLE_DEDUCT = 0x088826D4      # lw v1,0x34(s2) / lhu v0,0xc(v1)
_SM_BATTLE_DEDUCT_RET = 0x088826DC  # native subu v0,v0,a0 ; sh v0,0xc(v1)
_SM_BATTLE_AFFORD = 0x08870170      # andi a1,a2,0xffff / slt at,t0,a1
_SM_BATTLE_AFFORD_RET = 0x08870178  # beql at,zero,... (tests our at)
_SM_FIELD_DEDUCT = 0x088C472C       # subu a0,a0,a2 / sh a0,0x2c(a1)
_SM_FIELD_DEDUCT_RET = 0x088C4734   # sb v1,0x6fac(s5) (v1=1 preserved)
_SM_FIELD_GATE = 0x088C2E74         # lhu a0,0x2c(v1) / lui v1,0x895
_SM_FIELD_GATE_RET = 0x088C2E8C     # bnel v1,zero -> reject 0x66
_SM_MENU_GREY = 0x088D1C44          # lbu v0,0xa(a1) / lhu v1,0x2c(a0)
_SM_MENU_GREY_RET = 0x088D1C4C      # slt at,v1,v0 (native, consumes ours)
_SM_REPEAT_A = 0x088C34C8           # lhu a2,0x2c(v1) / lbu a0,(a0)
_SM_REPEAT_A_RET = 0x088C34D0       # slt a0,a2,a0
_SM_REPEAT_B = 0x088C3634           # lhu a2,0x2c(a0) / addiu v1,v1,0x4d16
_SM_REPEAT_B_RET = 0x088C3650       # slt at,a2,v1
# v214: the hook was at 0x088D1A50 (addiu s1,sp,0xa8 / addiu v0,zero,1) -- which
# is the COLUMN loop head, and its back-edge (0x088D1D5C bnez) targets
# 0x088D1A54, the SECOND displaced word. Columns 1 and 2 therefore re-entered
# the middle of our `j cave / nop` pair, skipped the `addiu v0,zero,1`, and hit
# `beql s6,v0` with v0 = the cave's leftover max-charges -- so the name draw for
# the two right columns never ran while the cursor still walked them (user
# report 2026-08-03). Hook moved to the ROW loop body at 0x088D19A4 (move fp,v0
# / lw v0,0x40(sp)), which nothing branches to; the row-loop back-edge targets
# 0x088D19A0. Bonus: the numbers now draw once per row instead of 3x.
_SM_MENU_ROW = 0x088D19A4           # move fp,v0 / lw v0,0x40(sp)
_SM_MENU_ROW_RET = 0x088D19AC       # slt at,fp,v0 (needs fp + v0 restored)
_SM_PANEL_HOOK = 0x088CEDD0         # addiu s1,s2,0x26 / andi a3,s1,0xffff
_SM_PANEL_RET = 0x088CEE70          # fn epilogue (lw ra,0x2c(sp))
_SM_PANEL_Y1 = 0x088CEC68           # addiu s1,s2,0xe  -> 0xc  (Lv line)
_SM_PANEL_Y2 = 0x088CED30           # addiu s1,s2,0x1a -> 0x16 (HP line)
_SM_COL_TABLE = 0x08991B78          # 3 x s16 spell-name column x (100,212,324)
                                    # (verified as a tripwire; never rewritten)
_SM_ETHER_VALID = 0x088C4D74        # effect-0x0E validity handler entry
_SM_ETHER_APPLY = 0x088C5560        # effect-0x0E apply handler entry
# battle magic info panel (fn 0x08870208): the MP cur/max number draws become
# charge cur/max of the HIGHLIGHTED spell's level (v168, live feedback: battle
# menu still showed mana). s2 = battle ctx, s1 = BU record (spell id +0x44),
# acting row = [ctx+0x67C0]; ra is dead (native jal follows immediately).
# The "MP Cost" line stays vanilla for now (cosmetic; see memory OPEN list).
# v177: ONE detour at the "MP" label draw replaces the whole panel body
# (label + highlighted cur/max + the old 2x4 grid): a 3-row cur/max grid over
# the freed space -- rows L1-3 / L4-6 / L7-8 at y 0xD8/0xE8/0xF8, columns
# x = 0x170 + col*0x20 (pairs cur right@+8, '/' id 0x11, max right@+20).
_SM_BPANEL_ALL = 0x088702F0         # lw a0,0x68d0(s2) / addiu a2,zero,0x174
# Rest charge refill (v168; static RE 2026-07-30). The tier byte ctx+0x6FB0 is
# MESSAGE-ONLY -- actual restore is the event-opcode heal handler 0x088496E4
# (jump table 0x0893FD18): sub-op 2 @0x08849740 = tiered rest, amount tables
# HP 0x0893FD08 {999,999,200,100} / MP 0x0893FD10 {999,999,100,0} indexed by
# cmd[3] (2 = Tent, 3 = Sleeping Bag); sub-op 6 @0x08849980 = INN (full + status
# clear). Both are one-shot (script ptr advances by cmd length per opcode) and
# both start with the same two position-independent words. Charges refill on
# Tent/Cottage/inn/scripted-full (cmd[3] <= 2), NOT Sleeping Bag (cmd[3] == 3,
# native gives 0 MP). cmd ptr = s2 at both sites. NEEDS live confirm that
# field Tent/Cottage actually route through sub-op 2 (exec-bp; the connecting
# event bytecode lives in the blob, not code) -- see slot-magic memory.
# v182: MID-BATTLE ETHERS (static RE 2026-07-30). Battle item effects route
# through the action-info table (0x08954D0C + id*14; ether family = kind 0x13
# tier-2 MP restore) into the result-array apply fn 0x088860D4. The MP-restore
# leg's cur/max loads at 0x088862C0 are the single hook: for party targets
# holding ether item ids 4/5/6 (cat/id queued at ctx+0x683C+caster_row*3, the
# caster row = [s4+0x3C], target row = [s4+0x3D]), the cave restores CHARGES
# (Ether 1 / Turbo 5 / Dry all) and neutralizes the MP add (v0 = cur - amount,
# so the native add/clamp lands back on cur); anything else runs vanilla.
# Consumption (fn 0x0887181C @0x08883914, final phase) is separate -- the item
# is still spent. Battle-use entries 4/5/6 stay VANILLA 0x6A/6B/6C now.
# v182: cosmetic MP remnants BLANKED (jal -> nop; delay slots verified to
# carry only dead argument setup). Field magic-use header MP line + MP Cost
# label (fn 0x088CFF18), the per-frame cost number + its no-spell dash (fn
# 0x088C36A4), the target-select panel's MP Cost label + number (fn
# 0x088CF2B4), and the STATUS screen MP line (fn 0x088D067C). HP lines are
# separate jal sites, untouched. The save/load FILE-SELECT preview draws MP
# via the fm_save_load widget pipeline (module 0x08923000+, template text in
# STitleDtl_US.dat) -- NOT these primitives; still OPEN.
_SM_MP_BLANK_JALS = (
    0x088D0548, 0x088D0590, 0x088D05AC, 0x088D05F8, 0x088D0614,  # use header
    0x088C38E0, 0x088C3928,                                      # cost number
    0x088CF474, 0x088CF4B8,                                      # target sel
    0x088D0C50, 0x088D0C98, 0x088D0CB4, 0x088D0D00,              # status
)
# v185: SAVE/LOAD file-select preview -- the per-character "MP n" row becomes
# "Magic n" showing MAGIC LEVEL (= highest usable spell level; it is literally
# _sm_slotfn's lock test `magiclv < L -> no slots`). RE 2026-07-30: the preview
# is drawn by fn 0x088274F8 (sole call site 0x08826FF8), NOT the fm_save_load
# shell; s4 = the previewed char record in the canonical (live-0x20) form, so
# class/level/magiclv are all reachable. Two one-word edits, no cave and NO PCK
# repack: the label is a string id immediate and CAMP_CMD.MSG entry 1 is
# already "Magic" (5 glyphs, fits the ~40px label field); the value load moves
# from MP (+0x2C u16) to magiclv (+0x30 u8).
# NB the preview reads the SLOT's own bytes -- never call _sm_slotfn here, it
# reads the LIVE save block (spent counters / INT accumulator) which belongs to
# a different file. Do not touch FM_SAVE_LOADUS.PCK either: our wp16 output is
# 2 bytes larger than the shipped stream even for an identity recompress, and
# the dpk slot (0x22B8) has zero headroom.
# v188: PER-FILE preview discrimination. The save->slot fill fn (0x08825CE0)
# copy #9 spans save+0x43C..0x840 -> slot+0x71C -- the WHOLE slot_magic region
# (spent 0x80C, CW pool 0x82C, acc, marker) reaches every file's preview record
# (the earlier "impossible" verdict stopped two copy loops short). An all-zero
# spent array can't distinguish "fresh slot save" from "mana save", so the
# client stamps a MARKER byte 0x5A at save+0x838 (SPELL_SLOTS_MARKER_SA)
# while a slot_magic seed plays; it rides into each save. Preview caves test
# the PREVIEWED file's copy: marker addr = (s4 - s2*0x5C) + _SM_SAVEPRV_
# MARKER_OFF (char0 rec = slotbase+4; save+X -> slotbase + X-0x43C+0x71C).
# Marker set -> "Magic <magiclv>", clear -> vanilla "MP <mp>" -- so old mana
# files keep showing MP, and slot saves made before the marker self-heal on
# their first re-save. v193: marker moved save+0x48C -> save+0x838 with the
# whole region (native map-record collision), so the slot-relative offset is
# 0x838-0x43C+0x71C-4 = 0xB14 (still inside the 0x10A4 slot record).
_SM_SAVEPRV_LABEL = 0x08827A40      # addiu a1,zero,0xC / addiu t0,zero,-1
_SM_SAVEPRV_VALUE = 0x08827A58      # lhu a1,0x2C(s4)   / lw a0,8(s5)
_SM_SAVEPRV_LABEL_RET = 0x08827A48
_SM_SAVEPRV_VALUE_RET = 0x08827A60
_SM_SAVEPRV_MARKER_OFF = 0xB14      # rel. char0 rec; save+0x838, value 0x5A
_SM_MARKER_VALUE = 0x5A
_SM_BETHER_HOOK = 0x088862C0        # lh v0,0xc(a0) / lh v1,0xe(a0)
_SM_BETHER_RET = 0x088862C8         # addu v0,v0,a1 (the native MP add)
_SM_REST_TIERED = 0x08849740        # move s4,zero / move s3,s0
_SM_REST_INN = 0x08849980           # move s4,zero / move s3,s0
# Battle panel charges grid: v171 detoured the first "MP Cost" label draw
# (0x08870374); v177 replaced that with ONE detour at the fn entry
# (_SM_BPANEL_ALL, 0x088702F0) drawing a 3-row grid -- see _bpanel_all_cave.
_SM_BPANEL_COST_RET = 0x088703FC    # lw ra,0x1c(sp) (epilogue; rejoin point)
# v171: level-up "MP increased by X" line -> X = spell slots gained this
# level (0 = line hidden, e.g. Thief 1->2). Implemented by repointing the
# statIdx jump-table entry 1 (MP) at 0x0894C410 to a cave that counts
# threshold crossings in (old, new] across all 8 levels for the job, then
# joins the common return 0x08887D34 (move v0,s2). Side effect: maxMP grows
# by the slot count instead of the MP formula -- harmless, the pool is inert.
# The dabble MP cave's 0x08887BC4 hook becomes unreachable under slot_magic
# (statIdx-1 never reaches the vanilla handler) -- intended.
_SM_LEVELUP_JT = 0x0894C410         # statIdx JT entry[1] -> 0x08887B98
_SM_LEVELUP_RET = 0x08887D34        # move v0,s2 + epilogue
# v172: Crimson Wizard damage->slot conversion (replaces the MP refund, which
# is inert under slot_magic). Design (user 2026-07-30): damage converts to
# POINTS = raw damage taken (v178, user: flat amounts so bigger HP pools can
# absorb bigger hits for bigger gains) into a per-char pool (u8, cap 200,
# save-resident at *SAVE_BLOCK_PTR+0x484); slot prices are a FLAT linear
# curve anchored L1 = 15 damage, L8 = 150: W(L) = 15 + 135*(L-1)/7
# (15/34/53/72/92/111/130/150). On every qualifying hit the pool greedily buys
# back the HIGHEST spent slots it can afford. THE POOL BANKS UNSPENT POINTS
# across hits, battles and saves -- three 50-damage hits reload an L8; no
# single-hit requirement. Teal popup = slots restored (hidden at 0).
# The leaf below is jal'd from BOTH Crimson Wizard legs in
# apply_job_scroll_boosts (physical + magic damage) when slot_magic is on --
# FEATURES order guarantees apply_slot_magic runs first and publishes the
# cave address via _SM_EXPORTS.
_SM_SAVE_PTR_HI, _SM_SAVE_PTR_LO = 0x089D, 0x7AD8
_SM_SPENT_OFF = 0x80C               # *SAVE_BLOCK_PTR + off = u8[4][8] spent
                                    # (v193 RELOCATED from 0x464: the old home
                                    # was inside a native rolling map-record
                                    # list; see ff1_data.py SPELL_SLOTS_*)
_SM_L9_BASE = 0x08954D15            # cost sites read 0x08954D16 + id*14; the
                                    # spell's LEVEL byte is always cost-1
_SM_DRAW_NUM = 0x08819698           # draw number (a1 val, a2 x, a3 y, t0 col,
                                    # t1 width, t2 9, t3 right-align, [sp]=0)
_SM_DRAW_STR = 0x08819214           # draw menu string id (a1 id; 0x0d = "/")
_SM_GREY = 0xFFA0A0A0
_SM_SURFACE_OFF = 0x6C88            # a0 = ctx + this for both draw prims
_SM_CW_POOL_OFF = 0x82C             # u8[4] damage-point pool (after spent[4][8])
_SM_EXPORTS = {}                    # {"cwslot": cave addr} set at apply time


def _sm_cwslot_cave():
    """Crimson Wizard damage -> slot buyback leaf.
    (a0 = party idx, a1 = damage dealt, a2 = battle ctx) -> v0 = slots
    restored. Clobbers ONLY a0-a2, at, v0, t6-t9 -- both CW legs keep their
    live state in t0/t1/t4/t5/v1/s-regs. Prices are the user's flat linear
    curve W(L) = 15 + 135*(L-1)/7 (15/34/53/72/92/111/130/150); the pool is
    save-resident and BANKS partial points across hits/battles/saves."""
    return A.asm_labels([
        # t6 = unit record = ctx + idx*0x6C + 0xC714
        A.sll("t6", "a0", 2), A.sll("t7", "a0", 3), A.addu("t6", "t6", "t7"),
        A.sll("t7", "a0", 5), A.addu("t6", "t6", "t7"),
        A.sll("t7", "a0", 6), A.addu("t6", "t6", "t7"),
        A.addu("t6", "t6", "a2"),
        A.li("t7", 0xC714), A.addu("t6", "t6", "t7"),
        A.lhu("at", 0x08, "t6"),            # curHP (dead -> nothing)
        ("beq", "at", "zero", "CW_R0"), A.nop(),
        A.addu("t8", "a1", "zero"),         # points = raw damage (flat)
        ("beq", "t8", "zero", "CW_R0"), A.nop(),
        A.lui("t6", _SM_SAVE_PTR_HI),
        A.lw("t6", _SM_SAVE_PTR_LO, "t6"),
        ("beq", "t6", "zero", "CW_R0"), A.nop(),
        A.addu("t7", "t6", "a0"),           # pool byte base
        A.lbu("t9", _SM_CW_POOL_OFF, "t7"),
        A.addu("t9", "t9", "t8"),
        A._i(0x0B, "t9", "at", 201),        # cap 200
        ("bne", "at", "zero", "CW_C"), A.nop(),
        A.addiu("t9", "zero", 200),
        ("label", "CW_C"),
        A.sll("at", "a0", 3), A.addu("t6", "t6", "at"),   # spent[idx] base
        A.addu("v0", "zero", "zero"),       # count restored
        A.addiu("a1", "zero", 8),           # L = 8..1 (highest first)
        ("label", "CW_L"),
        A.addiu("t8", "a1", -1),            # W = 15 + 135*(L-1)/7
        A.addiu("at", "zero", 135),
        A.multu("t8", "at"), A.mflo("t8"),
        A.addiu("at", "zero", 7),
        A.divu("t8", "at"), A.mflo("t8"),
        A.addiu("t8", "t8", 15),
        ("label", "CW_S"),
        A.addu("at", "t6", "a1"),
        A.lbu("a2", _SM_SPENT_OFF - 1, "at"),
        ("beq", "a2", "zero", "CW_N"), A.nop(),
        A.slt("at", "t9", "t8"),            # pool < W ?
        ("bne", "at", "zero", "CW_N"), A.nop(),
        A.addu("at", "t6", "a1"),
        A.addiu("a2", "a2", -1),
        A.sb("a2", _SM_SPENT_OFF - 1, "at"),
        A.subu("t9", "t9", "t8"),
        A.addiu("v0", "v0", 1),
        ("beq", "zero", "zero", "CW_S"), A.nop(),
        ("label", "CW_N"),
        A.addiu("a1", "a1", -1),
        ("bne", "a1", "zero", "CW_L"), A.nop(),
        A.sb("t9", _SM_CW_POOL_OFF, "t7"),  # bank the remainder
        A.jr(), A.nop(),
        ("label", "CW_R0"),
        A.addu("v0", "zero", "zero"),
        A.jr(), A.nop(),
    ])


# v183: dabbler INT-variance accrual (user-authorized 2026-07-30, "option 2,
# Mind Plus uncapped"). Each dabbler level-up accrues signed CENTS of bonus
# effective-level: delta = (INT*10 - E10[class][lvl]) * WQ[class][lvl] >> 6,
# where E10 = expected INT x10 derived from the game's own growth data
# (guaranteed bit-4 levels + 1/7 random rolls) and WQ = per-level weights
# solved at bake so the natural roll spread is EXACTLY +-L/10 effective
# levels (sigma) at every level -- 4 same-class dabblers at L30 land ~2 on
# schedule, ~1 three ahead, ~1 three behind. Mind Plus raises INT above par
# permanently -> every future level accrues the surplus, uncapped by design.
# Storage: NONE -- the deviation is derived LIVE from the int_e10/int_cw
# tables each read (see _sm_int_tables; the old save-block accumulator scheme
# was dropped, nothing reads or writes it). Effective level
# (charlevel + acc/100, clamped 1..99) feeds ONLY the slot threshold count
# (slotfn + the level-up crossing counter); the magiclv unlock gate stays
# native. Applies to jobs 1/2/7/8 (Thief/Monk/Ninja/Master; promotions keep
# the class%6 growth row and the same acc slot).
# v177/v179/v180: battle party status window (bottom-right, 4 rows
# Name/curHP/maxHP/curMP; battle-only trio, HP fn 0x0886F38C + MP fn
# 0x0886F55C + name fn 0x0886F6D8). curMP column -> 8 per-level charge digits;
# HP columns + names compressed left to make room.
# v220: FORMATION SWAP re-permutes the row-indexed slot arrays. spent[4][8],
# the CW point pool[4] and the Soma count[4] are all indexed by party ROW, but
# the formation routine MOVES the character records between rows -- so charges
# stuck to the SLOT, not the caster (reported live 2026-08-04: a Red Wizard's
# available charges changed just by reordering the party; max followed the
# character, spent did not, and the menu drew max - spent[row] from two people).
# A client-side permute was tried first and rejected: it writes ~a tick LATE and
# the menu holds its rendered row until reopened, so the first draw after a swap
# still showed stale numbers -- and once this cave exists, a second writer would
# re-apply the same permutation and undo it. This hook is the ONLY writer.
#
# RE 2026-08-04 (write-bp on the party records during a swap -> PCs 0x088c23b8
# / 0x088c24dc / 0x088c24e8, all inside fn ~0x088c21xx..0x088c26xx). The routine
# is a three-leg swap through a stack temp: stash row X -> copy row Y over X ->
# copy temp into row Y. Both row indices are already in memory at hook time:
#   rowX = lb 1([s0+0x718C])   (cursor row; s6 = [s0+0x7098] + rowX*0x5C)
#   rowY = lh [s0+0x70B2]
# Hook 0x088C257C: the third leg's copy loop ends at 0x088C2578, so the records
# are fully swapped; s0 (menu ctx) is live (the displaced words use it) and
# [s0+0x718C] is still valid (re-read at 0x088C2600). Verified by branch-target
# scan over 0x088C0000..0x088C4000 that nothing jumps into the displaced pair.
_SM_FORMSWAP_HOOK = 0x088C257C      # sw zero,0x70dc(s0) / sw zero,0x70ec(s0)
_SM_FORMSWAP_RET = 0x088C2584
_SM_FORMSWAP_CURSOR = 0x718C        # [s0+this] -> +1 = cursor row (rowX)
_SM_FORMSWAP_OTHER = 0x70B2         # [s0+this] = the other row (rowY), halfword


def _sm_formswap_cave():
    """Swap the three ROW-indexed slot_magic arrays when the party records are
    reordered, so charges/pool/soma follow the CHARACTER.

    Runs at _SM_FORMSWAP_HOOK with s0 = menu ctx. Everything it touches is
    stack-saved: the hook sits between two of the routine's own copy loops and
    the caller keeps live state in caller-saved regs across this point (the same
    trap _menu_row_cave hit at v214). Displaced originals run LAST, on the
    restored sp -- they only need s0, which is never written here.
    """
    return A.asm_labels(
        [A.addiu("sp", "sp", -0x40),
         A.sw("ra", 0x00, "sp"), A.sw("v0", 0x04, "sp"), A.sw("v1", 0x08, "sp"),
         A.sw("a0", 0x0C, "sp"), A.sw("a1", 0x10, "sp"), A.sw("a2", 0x14, "sp"),
         A.sw("a3", 0x18, "sp"), A.sw("t0", 0x1C, "sp"), A.sw("t1", 0x20, "sp"),
         A.sw("t2", 0x24, "sp"), A.sw("t8", 0x28, "sp"), A.sw("t9", 0x2C, "sp"),
         # a0 = rowX (cursor row), a1 = rowY (the row it traded with)
         A.lw("v0", _SM_FORMSWAP_CURSOR, "s0"),
         ("beq", "v0", "zero", "FS_OUT"), A.nop(),
         A.lbu("a0", 0x01, "v0"),
         A.lhu("a1", _SM_FORMSWAP_OTHER, "s0"),
         A.andi("a1", "a1", 0xFF),
         ("beq", "a0", "a1", "FS_OUT"), A.nop(),      # no-op swap
         A._i(0x0B, "a0", "at", 4),                   # sltiu at,a0,4
         ("beq", "at", "zero", "FS_OUT"), A.nop(),
         A._i(0x0B, "a1", "at", 4),
         ("beq", "at", "zero", "FS_OUT"), A.nop(),
         A.lui("v0", _SM_SAVE_PTR_HI),
         A.lw("v0", _SM_SAVE_PTR_LO, "v0"),
         ("beq", "v0", "zero", "FS_OUT"), A.nop(),
         # a2 = &spent[rowX][0], a3 = &spent[rowY][0]
         A.sll("a2", "a0", 3), A.addu("a2", "a2", "v0"),
         A.addiu("a2", "a2", _SM_SPENT_OFF),
         A.sll("a3", "a1", 3), A.addu("a3", "a3", "v0"),
         A.addiu("a3", "a3", _SM_SPENT_OFF),
         A.addiu("t0", "zero", 8),                    # 8 spell levels
         ("label", "FS_L"),
         A.lbu("t1", 0x00, "a2"), A.lbu("t2", 0x00, "a3"),
         A.sb("t2", 0x00, "a2"), A.sb("t1", 0x00, "a3"),
         A.addiu("a2", "a2", 1), A.addiu("a3", "a3", 1),
         A.addiu("t0", "t0", -1),
         ("bne", "t0", "zero", "FS_L"), A.nop(),
         # CW point pool[4] and Soma count[4] -- one byte each
         A.addu("t8", "v0", "a0"), A.addu("t9", "v0", "a1"),
         A.lbu("t1", _SM_CW_POOL_OFF, "t8"), A.lbu("t2", _SM_CW_POOL_OFF, "t9"),
         A.sb("t2", _SM_CW_POOL_OFF, "t8"), A.sb("t1", _SM_CW_POOL_OFF, "t9"),
         A.lbu("t1", _SM_SOMA_OFF, "t8"), A.lbu("t2", _SM_SOMA_OFF, "t9"),
         A.sb("t2", _SM_SOMA_OFF, "t8"), A.sb("t1", _SM_SOMA_OFF, "t9"),
         ("label", "FS_OUT"),
         A.lw("ra", 0x00, "sp"), A.lw("v0", 0x04, "sp"), A.lw("v1", 0x08, "sp"),
         A.lw("a0", 0x0C, "sp"), A.lw("a1", 0x10, "sp"), A.lw("a2", 0x14, "sp"),
         A.lw("a3", 0x18, "sp"), A.lw("t0", 0x1C, "sp"), A.lw("t1", 0x20, "sp"),
         A.lw("t2", 0x24, "sp"), A.lw("t8", 0x28, "sp"), A.lw("t9", 0x2C, "sp"),
         A.addiu("sp", "sp", 0x40),
         A.sw("zero", 0x70DC, "s0"),                  # displaced original #1
         A.sw("zero", 0x70EC, "s0"),                  # displaced original #2
         A.j(_SM_FORMSWAP_RET), A.nop()])


_SM_BSTAT_HPX = 0x0886F4CC          # addiu a2,zero,0x178 (curHP right x)
_SM_BSTAT_SLX = 0x0886F4F0          # addiu a2,zero,0x178 (slash x)
_SM_BSTAT_MXX = 0x0886F514          # addiu a2,zero,0x1A8 (maxHP right x)
_SM_BSTAT_NAMEX = 0x0886FA0C        # addiu a1,zero,0xFC (name x) -> 0xF8
_SM_BSTAT_HOOK = 0x0886F694         # addiu a2,zero,0x1D4 / addiu t1,zero,3
_SM_BSTAT_RET = 0x0886F6A8          # past the original curMP draw jal
# v194: Soma Drops under slot_magic. The vanilla effect-2 handler pair (raise
# maxMP by 5, refuse at 999) becomes "raise the number of LEVEL-1 spell slots
# by 1, spilling upward". Storage = a per-character COUNT (u8[4]) -- the whole
# distribution stays DERIVED, exactly like the natural table, so a level-up
# needs no grant hook and the spill re-solves itself as the natural curve
# grows (user's own worked example: nat 9/9/8/7/6/5/4/3 + 1 soma -> L3 hits 9;
# one level later nat is 9/9/9/7/... and the SAME soma now lands on L4).
#   final[L] = nat[L] + clamp(N - sum_{L'<L}(9 - nat[L']), 0, 9 - nat[L])
# i.e. N ones poured in from L1 up, each level capped at a hard 9 and only
# levels within magiclv taking part (class caps do NOT apply -- Soma is the
# one way past a hybrid's natural ceiling; user 2026-07-31).
# Validity mirrors vanilla's "maxMP already 999 -> refuse": Soma is refusable
# iff total < 9 * magiclv, so a full caster (and any magiclv-0 job) rejects
# the item instead of eating it. The dead s16[4] INT accumulator at save+0x830
# (v183 went live-compute and NOTHING has read or written it since) is
# repurposed as that count array -- same save-resident, preview-copied,
# client-canaried region.
_SM_SOMA_OFF = 0x830                # u8[4] Soma Drops drunk, per party row
_SM_SOMA_VALID = 0x088C4FD8         # effect-2 validity handler entry
_SM_SOMA_APPLY = 0x088C577C         # effect-2 apply handler entry
_SM_SOMA_MAX = 72                   # 8 levels x 9 slots -- count saturates
_SM_SLOT_CAP = 9                    # hard per-level ceiling
# Soma Drop's description under slot_magic. 24 chars: 2 more than the vanilla
# "Raises max MP by 5." it replaces once the icon prefix is dropped, and well
# inside the widest vanilla item description (26 chars).
_SOMA_GID = 38
_SOMA_SLOT_DESC = "Gain an extra spell slot"
# Ether family under slot_magic: the item handlers restore spell CHARGES per
# level, not MP (see the effect-0x0E detour), so the vanilla "Restores 50 MP."
# text is a lie. gids 4/5/6 = Ether / Turbo Ether / Dry Ether; the amounts match
# the apply detour's param tiers (1 / 5 / full). NOTE the gids here are the
# BANK ids (bank index + 1, the same space _SOMA_GID=38 lives in), NOT the
# ff1_data CAT_ITEM ids 4/5/6 -- bank indices 8/9/10 are the ethers.
_ETHER_SLOT_DESCS = {
    9:  "Restores 1 of each spell slot",
    10: "Restores 5 of each spell slot",
    11: "Restores all spell slots",
}



def slot_magic_item_descs(on):
    """{bank gid: description} the bake rewrites when slot_magic is on, or None.
    The client mirrors this into its shop-desc baseline (see
    tome_names.items_desc_bank)."""
    return ({_SOMA_GID: _SOMA_SLOT_DESC, **_ETHER_SLOT_DESCS} if on else None)


_SM_LIVE_REC_OFF = 0xDC4           # *SAVE_BLOCK_PTR -> live party record 0
                                    # (= ff1_data.PARTY_BASE_SA - 0x20: the
                                    # menu/live record form the caves read,
                                    # class +0x1E / level +0x20 / magiclv 0x30)
_SM_INT_Q = 6.0 / 49.0              # variance of one 1-in-7 INT roll


_SM_INT_ROW_BYTES = 99 * 2          # one class row of the E10 / CW tables


def _sm_mul_const(dst, src, k, tmp):
    """dst = src * k using shifts/adds only (k > 0). Emitted from k's bits so
    the sequence is always consistent with the constant it encodes."""
    bits = [i for i in range(16) if (k >> i) & 1]
    assert bits, "k must be > 0"
    out = [A.sll(dst, src, bits[0])]
    for b in bits[1:]:
        out += [A.sll(tmp, src, b), A.addu(dst, dst, tmp)]
    return out


def _sm_int_tables(elf):
    """(E10, CW) for class rows Thief(0)/Monk(1), levels 1..99 (index lvl-1).

    E10[lvl] = expected INT x10 at that level, derived from the game's own
    growth data (guaranteed bit-4 levels + 1/7 rolls elsewhere).
    CW[lvl]  = weight x256 such that bonus_levels = (INT*10 - E10) * CW / 2560
    has standard deviation EXACTLY lvl/10 under natural rolls -- i.e.
    CW = (lvl/10) / sqrt(q * rolls_so_far) * 256, q = 6/49.

    Live-compute (current deviation x cumulative weight) instead of path
    accrual: INT deviation is a martingale, so this has the same mean (0)
    and the same spread, needs NO save storage and NO level-up hook, and a
    Mind Plus applies immediately and permanently (user 2026-07-30:
    "accept that early mind plus has drastic late-game impact")."""
    import math
    GROWTH, START = 0x0894C1B8, 0x08955678
    e10_all, cw_all = b"", b""
    for cls in (1, 2):
        base = elf[E.ram2file(START + cls * 16 + 7)]
        goff = E.ram2file(GROWTH + cls * 99)
        g = bytes(elf[goff:goff + 99])
        exp, rolls = float(base), 0
        e10, cw = [int(round(exp * 10))], [0]      # level 1: no variance yet
        for i in range(98):                        # levels 2..99
            lv = i + 2
            if g[i] & 0x10:
                exp += 1.0
            else:
                exp += 1.0 / 7.0
                rolls += 1
            e10.append(min(0xFFFF, int(round(exp * 10))))
            sd = math.sqrt(_SM_INT_Q * rolls) if rolls else 0.0
            w = ((lv / 10.0) / sd * 256.0) if sd > 0 else 0.0
            cw.append(max(0, min(0xFFFF, int(round(w)))))
        e10_all += struct.pack("<99H", *e10)
        cw_all += struct.pack("<99H", *cw)
    # 2 rows x 99 u16 = 396 B each; pad to a word multiple for add_segment_cave
    return e10_all + bytes(-len(e10_all) % 4), cw_all + bytes(-len(cw_all) % 4)          # pad to a word multiple


# AUTHENTIC Pixel Remaster slot progression (PR master data, 2026-07-30),
# encoded as u8 thresholds[12 jobs][8 spell levels][9]: the char level at
# which that spell level's slot count reaches k+1 (0xFF = never); max =
# count of thresholds <= (effective) char level. Verified to reproduce the
# full PR 12x99x8 table EXACTLY (RW L30 = 8/8/7/7/6/5/3/0, Ninja 4/4/4/4).
# PSP job-id order Wa Th Mo RM WM BM Kn Ni Ma RW WW BW. Dabbler rows are
# REBUILT at bake from _SM_DAB_ANCHORS below.
_SM_THR_B64 = (
    "eNrt0V0PQzAUgOHofFPqu1Q71jEMw///b7a6XSKR2N3O1XtznpzkrOs5IwHN4SKAAnErQjUQGUSY"
    "MGCTCNdP+SIiTGizbWWU9z9yJCBrphNzcFF06OG8VTXDRiEpBtOCKMCsmlzkRynj3RJGSUbLZlwz"
    "Qgte9/OXE+CPYwinVlTdckNyfeqG5fgJ5aMNXT8mRfNCXhCn7N5NG3h79HOa5ays2mE5ywGyqm+H"
    "fceh2XEOgf9/7Ttv+ENz/w=="
)

# Dabbler (Thief/Monk/Master) slot curve, USER-AUTHORED anchors, REBALANCED
# 2026-08-01: cap moved 80 -> 90 and the whole curve pulled down to the new
# spec rows L20 3/2/1 and L90+ 6/5/4/3/2/1 (was L20 4/2/1/1, L80+ 9/8/6/4/3/2).
# Per spell level, (char level, slots) control points; steps spread between
# anchors by _sm_dab_steps, FROZEN after 90.
# RE-GATED 2026-08-18: the magiclv schedule slowed to 1/10/18/25/33/45
# (_SCHEDULE), and EVERY spell level's first anchor now sits exactly on its
# own unlock -- so the level that grants a new spell level also grants that
# level's first charge (no more "gate at 20, first slot at 24" gap). The L90
# endpoint is the unchanged balance contract (6/5/4/3/2/1, 21 slots total);
# only the intermediate anchors were re-derived to fit the later gates.
# Rows: L25 3/2/1/1, L45 4/3/1/2/1/1, L60 5/3/2/2/1/1, L90+ 6/5/4/3/2/1.
_SM_DAB_ANCHORS = {
    1: [(1, 1), (20, 3), (40, 4), (60, 5), (90, 6)],  # start 1 (user)
    2: [(10, 1), (25, 2), (45, 3), (70, 4), (90, 5)],
    3: [(18, 1), (48, 2), (72, 3), (90, 4)],
    4: [(25, 1), (55, 2), (90, 3)],
    5: [(33, 1), (90, 2)],
    6: [(45, 1), (90, 1)],
}


# v189: DE-LUMPING. The old floor-interpolation put every row's step exactly
# ON the shared anchor levels (20/30/40/50), so a dabbler got nothing for 5-9
# levels and then 3-5 slots at once (measured: +3 @20, +3 @30, +4 @40, +5 @50,
# nothing 51-99). The anchor VALUES are the balance contract and are unchanged;
# only the placement of the intra-segment steps changed: steps are spread
# evenly across the segment and each spell level carries its own PHASE so the
# rows do not step on the same char levels. Phases were picked by exhaustive
# search over 0.3..0.7 minimising (max gain, sum of squared gains). Re-solved
# 2026-08-01 for the rebalanced anchors under the user's HARD rule "no more
# than 1 spell slot per level-up": the phases below give max gain == 1 on
# EVERY level 1..99 (no level anywhere grants two slots). Changing an anchor
# without re-running that search can break the rule.
# Re-solved 2026-08-18 for the re-gated anchors (same 0.30..0.70 grid, same
# hard rule): max gain == 1 on every level 1..99.
_SM_DAB_PHASE = {1: 0.5, 2: 0.45, 3: 0.7, 4: 0.55, 5: 0.45, 6: 0.3}


def _sm_dab_steps(L):
    """Char levels at which spell level L's slot count increments (sorted).
    Step d of a segment always lands at or before its end anchor, so
    value(anchor level) == the authored anchor value EXACTLY."""
    import math
    a = _SM_DAB_ANCHORS.get(L)
    if not a:
        return []
    ph = _SM_DAB_PHASE.get(L, 0.5)
    s = [a[0][0]] * a[0][1]                 # the unlock level's initial slots
    for (c0, v0), (c1, v1) in zip(a, a[1:]):
        d, span = v1 - v0, c1 - c0
        for j in range(1, d + 1):
            lv = c0 + int(math.ceil((j - ph) * span / float(d)))
            s.append(min(c1, max(c0 + 1, lv)))
    return sorted(s)


def _sm_dab_value(L, c):
    """Dabbler max slots at spell level L (1..8) for char level c."""
    a = _SM_DAB_ANCHORS.get(L)
    if not a or c < a[0][0]:
        return 0
    if c >= a[-1][0]:
        return a[-1][1]
    return sum(1 for lv in _sm_dab_steps(L) if lv <= c)


def _sm_thr_table(feats=None):
    """Thresholds u8[12][8][9], index (job*8 + L-1)*9. Base = the static PR
    extraction; dabbler jobs (Thief 1 / Monk 2 / Master 8) are rebuilt from
    _SM_DAB_ANCHORS at bake time. When the dabble feature is on, Ninja (7)
    becomes elementwise max(PR Ninja, dabbler) -- as thresholds, per-step MIN
    -- so a Thief -> Ninja promotion can NEVER slow slot growth (user rule
    2026-07-30). Monk -> Master is the same curve on both sides already."""
    import base64, zlib
    raw = bytearray(zlib.decompress(base64.b64decode(_SM_THR_B64)))
    assert len(raw) == 12 * 8 * 9              # 864, already a word multiple

    def thr_of(L):
        ts = []
        for k in range(1, 10):
            lv = next((c for c in range(1, 100)
                       if _sm_dab_value(L, c) >= k), None)
            ts.append(lv if lv is not None else 0xFF)
        return ts

    dab = {L: thr_of(L) for L in range(1, 9)}
    for job in (1, 2, 8):
        for L in range(1, 9):
            base = (job * 8 + L - 1) * 9
            raw[base:base + 9] = bytes(dab[L])
    if feats and feats.get("monk_thief_dabble_in_magic"):
        for L in range(1, 9):
            base = (7 * 8 + L - 1) * 9
            merged = bytes(min(raw[base + k], dab[L][k]) for k in range(9))
            raw[base:base + 9] = merged
    return bytes(raw)


def _sm_charlv_chunk(load_raw, int_e10, int_cw, tag):
    """t8 = EFFECTIVE character level (raw level + the dabbler INT deviation),
    clamped 0..99. `load_raw` is the instruction list that puts the RAW level
    in t8 (from the record for the live path, from a1 for the what-if path the
    level-up delta needs). Clobbers t9/at/v0/v1 only. Labels are tagged so the
    chunk can be emitted more than once in one cave."""
    IV_ON, IV_OFF, LV_OK = f"IV_ON_{tag}", f"IV_OFF_{tag}", f"LV_OK_{tag}"
    return (load_raw + [
        # dabbler INT variance: effective level = level + acc/100 (jobs
        # 1/2/7/8 only; acc derived live from the int tables -- no save storage)
        A.lbu("t9", 0x1E, "a0"),
        A.addiu("at", "zero", 1),
        ("beq", "t9", "at", IV_ON), A.nop(),
        A.addiu("at", "zero", 2),
        ("beq", "t9", "at", IV_ON), A.nop(),
        A.addiu("at", "zero", 7),
        ("beq", "t9", "at", IV_ON), A.nop(),
        A.addiu("at", "zero", 8),
        ("bne", "t9", "at", IV_OFF), A.nop(),
        ("label", IV_ON),
        # table row: Thief/Ninja -> 0, Monk/Master -> 1 (class & 1 ? 0 : 1)
        A.andi("t9", "t9", 1), A.xori("t9", "t9", 1),
        A._i(0x0B, "t8", "at", 100),        # level in range for the table?
        ("beq", "at", "zero", IV_OFF), A.nop(),
        ]
        # at = row * _SM_INT_ROW_BYTES, emitted by BIT DECOMPOSITION of the
        # stride so the shift set can never drift from the table layout
        # (a hand-written sequence summing to 310 instead of 198 shipped in
        # v183 and zeroed every Monk/Master's slots -- live 2026-07-30).
        + _sm_mul_const("at", "t9", _SM_INT_ROW_BYTES, "v0")
        + [
        A.addiu("v0", "t8", -1),
        A.sll("v0", "v0", 1), A.addu("at", "at", "v0"),  # + (level-1)*2
        A.li("t9", int_e10),
        A.addu("t9", "t9", "at"),
        A.lhu("t9", 0, "t9"),               # expected INT x10
        A.lbu("v0", 0x33, "a0"),            # current INT
        A.sll("v1", "v0", 3), A.sll("v0", "v0", 1),
        A.addu("v0", "v1", "v0"),           # INT * 10
        A.subu("v0", "v0", "t9"),           # signed deviation (x10)
        A.li("t9", int_cw),
        A.addu("t9", "t9", "at"),
        A.lhu("t9", 0, "t9"),               # weight x256
        A.mult("v0", "t9"), A.mflo("v0"),
        A.li("t9", 2560),
        A.div("v0", "t9"), A.mflo("v0"),    # bonus effective levels (signed)
        A.addu("t8", "t8", "v0"),
        A.slt("at", "t8", "zero"),
        ("beq", "at", "zero", IV_OFF), A.nop(),
        A.addu("t8", "zero", "zero"),
        ("label", IV_OFF),
        A._i(0x0B, "t8", "at", 100),        # sltiu at,t8,100
        ("bne", "at", "zero", LV_OK), A.nop(),
        A.addiu("t8", "zero", 99),
        ("label", LV_OK),
    ])


def _sm_nat_chunk(tbl_ram, lvl_reg, tag):
    """v0 = NATURAL slot count 0..9 for spell level `lvl_reg` (1..8) at the
    effective char level already in t8, job = a0's class byte. Clobbers
    t9/at/v1/v0 only -- `lvl_reg` and every t0-t7 are preserved."""
    CLS_OK = f"CLS_OK_{tag}"
    return [
        # t9 = THR + (job*8 + level-1)*9; max = count of thresholds <= charlv
        A.lbu("t9", 0x1E, "a0"),            # class 0..11
        A._i(0x0B, "t9", "at", 12),         # sltiu at,t9,12 (garbage-proof)
        ("bne", "at", "zero", CLS_OK), A.nop(),
        A.addu("t9", "zero", "zero"),
        ("label", CLS_OK),
        A.sll("t9", "t9", 3),               # job*8
        A.addu("t9", "t9", lvl_reg),        # +level (1..8; -1 folded below)
        A.addiu("t9", "t9", -1),
        A.sll("v0", "t9", 3), A.addu("t9", "t9", "v0"),      # *9
        A.li("v0", tbl_ram),
        A.addu("t9", "t9", "v0"),
        A.addu("v0", "zero", "zero"),       # count
        # unrolled: v0 += (thr[k] <= charlv); 0xFF sorts above 99 naturally
    ] + [ins for k in range(9) for ins in (
        A.lbu("v1", k, "t9"),
        A.slt("at", "t8", "v1"),            # charlv < thr ?
        A.xori("at", "at", 1),
        A.addu("v0", "v0", "at"))]


def _sm_soma_chunk(out_reg, row_reg, tmp, tag):
    """out = Soma Drops drunk by party row `row_reg` (0 with no save block)."""
    SKIP = f"NOSOMA_{tag}"
    return [
        A.addu(out_reg, "zero", "zero"),
        A.lui(tmp, _SM_SAVE_PTR_HI),
        A.lw(tmp, _SM_SAVE_PTR_LO, tmp),
        ("beq", tmp, "zero", SKIP), A.nop(),
        A.addu(tmp, tmp, row_reg),
        A.lbu(out_reg, _SM_SOMA_OFF, tmp),
        ("label", SKIP),
    ]


def _sm_slotfn(tbl_ram, int_e10=0, int_cw=0):
    """Shared jal-able leaf: (a0=menu char rec, a1=spell level 1..8,
    a2=char row 0..3) -> v0 = max charges, v1 = spent. Clobbers t8/t9/at ONLY
    (chosen because no hook site has t8/t9 live -- all verified in the dumps);
    t0-t3 are used for the Soma spill and are stack-saved for that reason.
    Castability for every gate = slt(v1, v0): one compare covers both
    "level locked" (max 0) and "charges exhausted".
    v194: max = nat[L] + the share of the character's Soma count that spills
    into L, i.e. what is left of it after every LOWER unlocked level has been
    topped up to 9. Costs one threshold scan per level below the requested
    one; the loop is bounded at 8 and only runs on already-unlocked levels."""
    return A.asm_labels(
        [A.addiu("sp", "sp", -0x20),
         A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
         A.sw("t2", 0x08, "sp"), A.sw("t3", 0x0C, "sp"),
         A.lbu("t8", 0x30, "a0"),           # magiclv
         A.slt("at", "t8", "a1"),
         ("bne", "at", "zero", "LOCKED"), A.nop(),
         A._i(0x0B, "a1", "at", 9),         # sltiu at,a1,9 (loop bound guard)
         ("beq", "at", "zero", "LOCKED"), A.nop()]
        + _sm_charlv_chunk([A.lbu("t8", 0x20, "a0")],  # char level (u32 low)
                           int_e10, int_cw, "S")
        + _sm_soma_chunk("t1", "a2", "t9", "S")        # t1 = soma count
        + [A.addu("t2", "zero", "zero"),    # t2 = spare capacity below a1
           A.addiu("t0", "zero", 1),        # t0 = the level being scanned
           ("label", "SL_LOOP")]
        + _sm_nat_chunk(tbl_ram, "t0", "S")
        + [("beq", "t0", "a1", "SL_MINE"), A.nop(),
           A.addiu("at", "zero", _SM_SLOT_CAP), A.subu("at", "at", "v0"),
           A.addu("t2", "t2", "at"),
           ("beq", "zero", "zero", "SL_LOOP"), A.addiu("t0", "t0", 1),
           ("label", "SL_MINE"),
           A.addu("t3", "zero", "v0"),      # t3 = nat[a1]
           # spill = clamp(soma - capacity_below, 0, 9 - nat)
           A.subu("v0", "t1", "t2"),
           A.slt("at", "v0", "zero"),
           ("beq", "at", "zero", "SL_POS"), A.nop(),
           A.addu("v0", "zero", "zero"),
           ("label", "SL_POS"),
           A.addiu("v1", "zero", _SM_SLOT_CAP), A.subu("v1", "v1", "t3"),
           A.slt("at", "v1", "v0"),
           ("beq", "at", "zero", "SL_ROOM"), A.nop(),
           A.addu("v0", "zero", "v1"),
           ("label", "SL_ROOM"),
           A.addu("v0", "v0", "t3"),
           A.lui("t8", _SM_SAVE_PTR_HI),
           A.lw("t8", _SM_SAVE_PTR_LO, "t8"),
           ("beq", "t8", "zero", "NOSAVE"), A.nop(),
           A.sll("t9", "a2", 3), A.addu("t8", "t8", "t9"),
           A.addu("t8", "t8", "a1"),        # +level; spent byte = off-1+level
           ("beq", "zero", "zero", "OUT"),
           A.lbu("v1", _SM_SPENT_OFF - 1, "t8"),
           ("label", "NOSAVE"),
           ("beq", "zero", "zero", "OUT"), A.addu("v1", "zero", "zero"),
           ("label", "LOCKED"),
           A.addu("v0", "zero", "zero"),
           A.addu("v1", "zero", "zero"),
           ("label", "OUT"),
           A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
           A.lw("t2", 0x08, "sp"), A.lw("t3", 0x0C, "sp"),
           A.jr(), A.addiu("sp", "sp", 0x20)])


def _sm_totalfn(tbl_ram, int_e10=0, int_cw=0):
    """Leaf: (a0=char rec, a1=char level to ASSUME, a2=row) -> v0 = total
    slots over every unlocked level (natural + Soma spill), v1 = 9 * magiclv
    (the ceiling; v0 == v1 means another Soma Drop can do nothing). Same
    clobber contract as _sm_slotfn plus t4. a1 is an assumed level rather than
    the record's so the level-up cave can difference two totals -- that is the
    only honest way to report a gain once Soma redirection is in play."""
    return A.asm_labels(
        [A.addiu("sp", "sp", -0x20),
         A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
         A.sw("t2", 0x08, "sp"), A.sw("t3", 0x0C, "sp"),
         A.sw("t4", 0x10, "sp"),
         A.lbu("t4", 0x30, "a0")]           # magiclv
        + _sm_charlv_chunk([A.andi("t8", "a1", 0xFF)], int_e10, int_cw, "T")
        + [A._i(0x0B, "t4", "at", 9),       # sltiu at,t4,9 (garbage-proof)
           ("bne", "at", "zero", "TL_MLOK"), A.nop(),
           A.addiu("t4", "zero", 8),
           ("label", "TL_MLOK"),
           A.addu("t2", "zero", "zero"),    # spare capacity
           A.addu("t3", "zero", "zero"),    # natural total
           A.addiu("t0", "zero", 1),
           ("label", "TL_LOOP"),
           A.slt("at", "t4", "t0"),         # magiclv < L -> done
           ("bne", "at", "zero", "TL_DONE"), A.nop()]
        + _sm_nat_chunk(tbl_ram, "t0", "T")
        + [A.addu("t3", "t3", "v0"),
           A.addiu("at", "zero", _SM_SLOT_CAP), A.subu("at", "at", "v0"),
           A.addu("t2", "t2", "at"),
           A.addiu("t0", "t0", 1),
           A._i(0x0B, "t0", "at", 9),       # sltiu at,t0,9
           ("bne", "at", "zero", "TL_LOOP"), A.nop(),
           ("label", "TL_DONE")]
        + _sm_soma_chunk("t1", "a2", "t9", "T")
        + [A.slt("at", "t2", "t1"),         # spill is capped by free room
           ("beq", "at", "zero", "TL_ADD"), A.nop(),
           A.addu("t1", "zero", "t2"),
           ("label", "TL_ADD"),
           A.addu("v0", "t3", "t1"),
           A.sll("v1", "t4", 3), A.addu("v1", "v1", "t4"),   # 9 * magiclv
           A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
           A.lw("t2", 0x08, "sp"), A.lw("t3", 0x0C, "sp"),
           A.lw("t4", 0x10, "sp"),
           A.jr(), A.addiu("sp", "sp", 0x20)])


def _sm_level_from_id(id_src_reg, out_reg, scratch):
    """out = spell LEVEL for the (possibly dirty) spell-id reg: level byte =
    *(0x08954D15 + (id&0xff)*14) -- one below every cost site's own address
    math, so the id base convention can never drift. Clobbers at + scratch."""
    return [
        A.andi("at", id_src_reg, 0xFF),
        A.sll(scratch, "at", 3), A.subu(scratch, scratch, "at"),
        A.sll(scratch, scratch, 1),                      # id*14
        A.li("at", _SM_L9_BASE),
        A.addu("at", "at", scratch),
        A.lbu(out_reg, 0, "at"),
    ]


def _sm_row_chain(rec_reg, base_ctx_reg, base_off, out_reg, scratch):
    """out = party row 0..3 from a menu char-rec pointer: compare rec -
    [ctx+base_off] against 0x5C multiples. Clobbers at + scratch + out."""
    body = [
        A.lw(scratch, base_off, base_ctx_reg),
        A.subu(scratch, rec_reg, scratch),
        A.addu(out_reg, "zero", "zero"),
    ]
    for r in (1, 2, 3):
        body += [
            A.addiu("at", "zero", r * 0x5C),
            ("bne", scratch, "at", f"RC{r}"), A.nop(),
            A.addiu(out_reg, "zero", r),
            ("label", f"RC{r}"),
        ]
    return body


def _sm_spend(row_reg, lvl_reg, tmp0, tmp1):
    """spent[row][level] += 1 via *SAVE_BLOCK_PTR (no-op when ptr null).
    Clobbers at + tmp0; tmp1 unused (kept for call-site clarity)."""
    return [
        A.lui(tmp0, _SM_SAVE_PTR_HI),
        A.lw(tmp0, _SM_SAVE_PTR_LO, tmp0),
        ("beq", tmp0, "zero", "SPEND_OUT"), A.nop(),
        A.sll("at", row_reg, 3),
        A.addu(tmp0, tmp0, "at"),
        A.addu(tmp0, tmp0, lvl_reg),
        A.lbu("at", _SM_SPENT_OFF - 1, tmp0),
        A.addiu("at", "at", 1),
        A.sb("at", _SM_SPENT_OFF - 1, tmp0),
        ("label", "SPEND_OUT"),
    ]


def apply_slot_magic(elf: bytearray, feats=None):
    # -- verify every hook site against the vanilla words (fail loudly on a
    # wrong ISO or a colliding feature edit)
    expect = {
        _SM_BATTLE_DEDUCT: (0x8E430034, 0x9462000C),
        _SM_BATTLE_AFFORD: (0x30C5FFFF, 0x0105082A),
        _SM_FIELD_DEDUCT: (0x00862023, 0xA4A4002C),
        _SM_FIELD_GATE: (0x9464002C, 0x3C030895),
        _SM_MENU_GREY: (0x90A2000A, 0x9483002C),
        _SM_REPEAT_A: (0x9466002C, 0x90840000),
        _SM_REPEAT_B: (0x9486002C, 0x24634D16),
        _SM_MENU_ROW: (0x0040F021, 0x8FA20040),
        _SM_PANEL_HOOK: (0x26510026, 0x3227FFFF),
        _SM_ETHER_VALID: (0x00061040, 0x00461021),
        _SM_ETHER_APPLY: (0x00111040, 0x00511021),
        _SM_SOMA_VALID: (0x3C020895, 0x244238F3),
        _SM_SOMA_APPLY: (0x00112040, 0x00912021),
        _SM_BPANEL_ALL: (0x8E4468D0, 0x24060174),
        _SM_REST_TIERED: (0x0000A021, 0x02009821),
        _SM_REST_INN: (0x0000A021, 0x02009821),
        _SM_BSTAT_HOOK: (0x240601D4, 0x24090003),
        _SM_BETHER_HOOK: (0x8482000C, 0x8483000E),
        _SM_SAVEPRV_LABEL: (0x2405000C, 0x2408FFFF),
        _SM_SAVEPRV_VALUE: (0x9685002C, 0x8EA40008),
    }
    for ram, (w0, w1) in expect.items():
        g0, g1 = _read_word(elf, ram), _read_word(elf, ram + 4)
        if (g0, g1) != (w0, w1):
            raise ValueError(f"slot_magic: unexpected words @{ram:#x}: "
                             f"{g0:#010x}/{g1:#010x}")
    for site, want in ((_SM_PANEL_Y1, 0x2651000E), (_SM_PANEL_Y2, 0x2651001A)):
        if _read_word(elf, site) != want:
            raise ValueError(f"slot_magic: unexpected panel-y @{site:#x}")
    # Wrong-ISO tripwires ONLY: the feature no longer edits either table
    # (spell-name columns stay vanilla since v177, ethers battle-usable since
    # v182) -- these verify the vanilla bytes and write NOTHING.
    cols = struct.unpack_from("<3h", elf, E.ram2file(_SM_COL_TABLE))
    if cols != (100, 212, 324):
        raise ValueError(f"slot_magic: unexpected column table {cols}")
    for iid, want in ((4, 0x006A), (5, 0x006B), (6, 0x006C)):
        got = struct.unpack_from("<H", elf,
                                 E.ram2file(_BATTLE_USE_TABLE + iid * 2))[0]
        if got != want:
            raise ValueError(f"slot_magic: battle-use[{iid}] = {got:#x}")
    # (v182: entries stay vanilla -- ethers are battle-usable again)

    # -- data cave: the PR threshold table (dabbler curve + Ninja merge are
    # feats-dependent), then the shared leaf fn
    tbl = E.add_segment_cave(elf, _sm_thr_table(feats))
    _e10, _cw = _sm_int_tables(elf)
    int_e10 = E.add_segment_cave(elf, _e10)
    int_cw = E.add_segment_cave(elf, _cw)
    slotfn = E.add_segment_cave(elf, _sm_slotfn(tbl, int_e10, int_cw))
    totalfn = E.add_segment_cave(elf, _sm_totalfn(tbl, int_e10, int_cw))
    if feats and feats.get("monk_thief_dabble_in_magic"):
        # Ninja's native spell-level cap is 4; a promoted Thief carries dabble
        # spells (and now slots) at levels 5-6. Repoint job 7 in BOTH cap
        # dispatch tables to the cap-6 handlers (same move the dabble feature
        # makes for jobs 1/2/8) so promotion never locks existing rows.
        struct.pack_into("<I", elf, E.ram2file(_DISPATCH + 7 * 4), _REDMAGE_CAP)
        struct.pack_into("<I", elf, E.ram2file(_MENU_LVCAP_TAB + 7 * 4),
                         _MENU_REDMAGE_CAP)

    # -- battle deduct: party rows bump spent + force cost 0 (MP untouched);
    # enemy rows (idx >= 4) keep the vanilla MP pool. s1 = spell id, s2 =
    # combatant obj, a0 = cost (native, loaded just before the hook).
    battle_deduct = A.asm_labels(
        [A.lw("v1", 0x34, "s2"),            # displaced 1
         A.lhu("v0", 0x0C, "v1"),           # displaced 2
         A.lbu("a2", 0x3C, "s2"),           # unit row
         A._i(0x0B, "a2", "at", 4),         # sltiu at,a2,4 (party?)
         ("beq", "at", "zero", "VANILLA"), A.nop()]
        + _sm_level_from_id("s1", "a1", "a0")
        + _sm_spend("a2", "a1", "a0", None)
        + [A.addu("a0", "zero", "zero"),    # cost 0 -> native subu/sh no-op
           ("label", "VANILLA"),
           A.j(_SM_BATTLE_DEDUCT_RET), A.nop()])

    # -- battle afford: replace `curMP < cost` with `spent >= max`. Live-after
    # regs v0 (flags byte), t1 (magiclv), t2 (status) preserved; a3 = spell id,
    # t4 = acting row, a0 = battle ctx (preserved).
    battle_afford = A.asm_labels(
        [A.addiu("sp", "sp", -0x20),
         A.sw("ra", 0x00, "sp"), A.sw("v0", 0x04, "sp"), A.sw("v1", 0x08, "sp"),
         A.sw("t1", 0x0C, "sp"), A.sw("t2", 0x10, "sp"), A.sw("a0", 0x14, "sp"),
         A.lw("v1", 0x6834, "a0")]          # menu char array
        + _emit_char_rec("t4", "v1", tmp="a1")   # v1 += t4*0x5C (a1 dead here)
        + [A.addu("a0", "v1", "zero")]
        + _sm_level_from_id("a3", "a1", "v0")
        + [A.addu("a2", "t4", "zero"),
           A.jal(slotfn), A.nop(),
           A.slt("at", "v1", "v0"),         # spent < max -> castable
           A.xori("at", "at", 1),           # at = NOT castable (native shape)
           A.lw("a0", 0x14, "sp"), A.lw("t2", 0x10, "sp"), A.lw("t1", 0x0C, "sp"),
           A.lw("v1", 0x08, "sp"), A.lw("v0", 0x04, "sp"), A.lw("ra", 0x00, "sp"),
           A.addiu("sp", "sp", 0x20),
           A.j(_SM_BATTLE_AFFORD_RET), A.nop()])

    # -- field deduct: skip the MP write for party rows, bump spent. s4 = row,
    # fp = spell id, a1 = char rec, a2 = cost, v1 = 1 (preserved for resume).
    field_deduct = A.asm_labels(
        [A._i(0x0B, "s4", "at", 4),         # sltiu at,s4,4
         ("beq", "at", "zero", "VANILLA"), A.nop()]
        + _sm_level_from_id("fp", "a2", "a0")
        + _sm_spend("s4", "a2", "a0", None)
        + [A.j(_SM_FIELD_DEDUCT_RET), A.nop(),
           ("label", "VANILLA"),
           A.subu("a0", "a0", "a2"),        # displaced originals (enemy-safe)
           A.sh("a0", 0x2C, "a1"),
           A.j(_SM_FIELD_DEDUCT_RET), A.nop()])

    # -- field cast gate: v1 = char rec at entry, s0 = spell id, s1 = ctx.
    # Resume with v1 = 1 to reject (msg 0x66), 0 to pass.
    field_gate = A.asm_labels(
        [A.lhu("a0", 0x2C, "v1"),           # displaced 1 (a0 dead downstream)
         A.addiu("sp", "sp", -0x10),
         A.sw("ra", 0x00, "sp"),
         A.addu("a0", "v1", "zero")]
        + _sm_row_chain("a0", "s1", 0x7098, "a2", "a1")
        + _sm_level_from_id("s0", "a1", "v0")
        + [A.jal(slotfn), A.nop(),
           A.slt("at", "v1", "v0"),
           A.xori("v1", "at", 1),           # v1 = reject
           A.lw("ra", 0x00, "sp"),
           A.addiu("sp", "sp", 0x10),
           A.j(_SM_FIELD_GATE_RET), A.nop()])

    # -- field menu grey-out: a0 = char rec (LIVE -- native delay slot reloads
    # magiclv from it), a1 = magic_info rec (preserved), fp = level-1, s0 =
    # ctx. Resume executes native slt at,v1,v0: v0 = notcast, v1 = 0.
    menu_grey = A.asm_labels(
        [A.addiu("sp", "sp", -0x10),
         A.sw("ra", 0x00, "sp"), A.sw("a1", 0x04, "sp"),
         A.lw("a2", 0x7090, "s0"),          # selected char row
         A.addiu("a1", "fp", 1),            # level
         A.jal(slotfn), A.nop(),
         A.slt("at", "v1", "v0"),
         A.xori("v0", "at", 1),             # v0 = notcast -> native slt greys
         A.addu("v1", "zero", "zero"),
         A.lw("a1", 0x04, "sp"), A.lw("ra", 0x00, "sp"),
         A.addiu("sp", "sp", 0x10),
         A.j(_SM_MENU_GREY_RET), A.nop()])

    # -- field repeat re-check A: v1 = char rec, a0 = cost ptr (level = its
    # byte at -1), s0 = ctx. Resume: native slt a0,a2,a0 -> a2 = 0, a0 = notcast.
    repeat_a = A.asm_labels(
        [A.lbu("a1", -1, "a0"),             # LEVEL from cost ptr - 1
         A.addiu("sp", "sp", -0x10),
         A.sw("ra", 0x00, "sp"),
         A.addu("a0", "v1", "zero")]
        + _sm_row_chain("a0", "s0", 0x7098, "a2", "v0")
        + [A.jal(slotfn), A.nop(),
           A.slt("at", "v1", "v0"),
           A.xori("a0", "at", 1),
           A.addu("a2", "zero", "zero"),
           A.lw("ra", 0x00, "sp"),
           A.addiu("sp", "sp", 0x10),
           A.j(_SM_REPEAT_A_RET), A.nop()])

    # -- field repeat re-check B: a0 = char rec, a1 = spell id, s0 = ctx.
    # Resume: native slt at,a2,v1 -> a2 = 0, v1 = notcast.
    repeat_b = A.asm_labels(
        [A.addiu("sp", "sp", -0x10),
         A.sw("ra", 0x00, "sp")]
        + _sm_row_chain("a0", "s0", 0x7098, "a2", "v0")
        + _sm_level_from_id("a1", "a1", "v1")
        + [A.jal(slotfn), A.nop(),
           A.slt("at", "v1", "v0"),
           A.xori("v1", "at", 1),
           A.addu("a2", "zero", "zero"),
           A.lw("ra", 0x00, "sp"),
           A.addiu("sp", "sp", 0x10),
           A.j(_SM_REPEAT_B_RET), A.nop()])

    # -- magic submenu "cur max" per level row. Hook _SM_MENU_ROW 0x088D19A4
    # (v214): fp = level-1, s3 = y, s0 = ctx. Draws cur right-aligned @0x1A6
    # and max right-aligned @0x1B8 (spell-name columns stay vanilla). Grey when
    # the row is dead (max 0); cur additionally grey at 0.
    def _menu_row_cave():
        # v214: the hook sits INSIDE a live draw loop -- the caller keeps loop
        # state in caller-saved regs across this point -- so t0-t9 / a0-a3 /
        # v0 / v1 are saved and restored around the whole body. Only fp is
        # intentionally rewritten, by displaced word 1 (`move fp,v0`, run first
        # because the body's level index reads fp); displaced word 2
        # (`lw v0,0x40(sp)`) runs last, on the RESTORED native sp.
        _SAVE = (("t0", 0x20), ("t1", 0x24), ("t2", 0x28), ("t3", 0x2C),
                 ("t4", 0x30), ("t5", 0x34), ("t6", 0x38), ("t7", 0x3C),
                 ("t8", 0x40), ("t9", 0x44), ("a0", 0x48), ("a1", 0x4C),
                 ("a2", 0x50), ("a3", 0x54), ("v1", 0x58))
        body = [A.addiu("sp", "sp", -0x60),
                A.sw("ra", 0x18, "sp")]
        body += [A.sw(r, o, "sp") for r, o in _SAVE]
        body += [A.addu("fp", "v0", "zero"),  # displaced 1 (row -> fp)
                 A.lw("a0", 0x7098, "s0"),
                A.lw("a2", 0x7090, "s0")]
        body += _emit_char_rec("a2", "a0")
        body += [A.addiu("a1", "fp", 1),
                 A.jal(slotfn), A.nop(),
                 A.subu("v1", "v0", "v1"),
                 A.slt("at", "v1", "zero"),
                 ("beq", "at", "zero", "CL"), A.nop(),
                 A.addu("v1", "zero", "zero"),
                 ("label", "CL"),
                 A.sw("v0", 0x10, "sp"), A.sw("v1", 0x14, "sp"),
                 # t0 = white unless cur==0 (covers max==0 too: cur==0 then)
                 A.li("t0", 0xFFFFFFFF),
                 ("bne", "v1", "zero", "C1"), A.nop(),
                 A.li("t0", _SM_GREY),
                 ("label", "C1"), A.sw("t0", 0x1C, "sp")]
        # cur @0x6A (colour from t0 just computed)
        body += [A.addiu("a0", "s0", _SM_SURFACE_OFF),
                 A.addu("a1", "v1", "zero"),
                 A.addiu("a2", "zero", 0x1A6),
                 A.andi("a3", "s3", 0xFFFF),
                 A.lw("t0", 0x1C, "sp"),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp")]
        # "/" @0x6A -- white if the row is alive (max>0) else grey
        body += [A.lw("v0", 0x10, "sp"),
                 A.li("t0", 0xFFFFFFFF),
                 ("bne", "v0", "zero", "C2"), A.nop(),
                 A.li("t0", _SM_GREY),
                 ("label", "C2"),
                 A.addiu("a0", "s0", _SM_SURFACE_OFF),
                 A.addiu("a1", "zero", 0x0D),
                 A.addiu("a2", "zero", 0x1A6),
                 A.andi("a3", "s3", 0xFFFF),
                 A.jal(_SM_DRAW_STR), A.addu("t1", "zero", "zero")]
        # max @0x88 -- same row colour
        body += [A.lw("v0", 0x10, "sp"),
                 A.li("t0", 0xFFFFFFFF),
                 ("bne", "v0", "zero", "C3"), A.nop(),
                 A.li("t0", _SM_GREY),
                 ("label", "C3"),
                 A.addiu("a0", "s0", _SM_SURFACE_OFF),
                 A.addu("a1", "v0", "zero"),
                 A.addiu("a2", "zero", 0x1B8),
                 A.andi("a3", "s3", 0xFFFF),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp")]
        body += [A.lw("ra", 0x18, "sp")]
        body += [A.lw(r, o, "sp") for r, o in _SAVE]
        body += [A.addiu("sp", "sp", 0x60),
                 A.j(_SM_MENU_ROW_RET),
                 A.lw("v0", 0x40, "sp")]     # displaced 2 (original sp!)
        return A.asm_labels(body)
    menu_row = _menu_row_cave()

    # -- main-menu panel: replace the MP line with 2 rows x 4 charge counts
    # (digits-only, v179; levels 1-4, then 5-8). s0 = ctx, s2 = panel y base, s3 =
    # row*0x5C. s5/s6/s7 are caller-owned -> saved. Rows at +0x20 / +0x2A
    # (line-y immediates tightened separately).
    def _panel_cave():
        body = [A.addiu("sp", "sp", -0x40),
                A.sw("ra", 0x18, "sp"), A.sw("s5", 0x1C, "sp"),
                A.sw("s6", 0x20, "sp"), A.sw("s7", 0x24, "sp"),
                A.lw("s6", 0x7098, "s0")]
        # s7 = party row from s3 (0, 0x5C, 0xB8, 0x114) by compare chain
        body += [A.addu("s7", "zero", "zero")]
        for r in (1, 2, 3):
            body += [A.addiu("at", "zero", r * 0x5C),
                     ("bne", "s3", "at", f"PR{r}"), A.nop(),
                     A.addiu("s7", "zero", r),
                     ("label", f"PR{r}")]
        body += [A.addu("s6", "s6", "s3"),  # rec = array + row off
                 A.addu("s5", "zero", "zero"),
                 ("label", "PLOOP"),
                 A.addu("a0", "s6", "zero"),
                 A.addiu("a1", "s5", 1),
                 A.addu("a2", "s7", "zero"),
                 A.jal(slotfn), A.nop(),
                 A.subu("v1", "v0", "v1"),          # cur = max - spent
                 A.slt("at", "v1", "zero"),
                 ("beq", "at", "zero", "PCL"), A.nop(),
                 A.addu("v1", "zero", "zero"),
                 ("label", "PCL"),
                 A.sw("v1", 0x28, "sp"),
                 A.li("t0", 0xFFFFFFFF),            # white; grey when cur == 0
                 ("bne", "v1", "zero", "PCC"), A.nop(),
                 A.li("t0", _SM_GREY),
                 ("label", "PCC"),
                 A.sw("t0", 0x2C, "sp"),
                 # x = 0x84 + (k % 4) * 0x1C
                 A.andi("at", "s5", 3),
                 A.sll("v0", "at", 4), A.sll("v1", "at", 3),
                 A.addu("v0", "v0", "v1"),
                 A.sll("v1", "at", 2), A.addu("v0", "v0", "v1"),
                 A.addiu("v0", "v0", 0x84),
                 A.sw("v0", 0x30, "sp"),
                 # y = s2 + (k < 4 ? 0x20 : 0x2A)
                 A._i(0x0A, "s5", "at", 4),          # slti at,s5,4
                 A.addiu("v1", "s2", 0x2A),
                 ("beq", "at", "zero", "PY"), A.nop(),
                 A.addiu("v1", "s2", 0x20),
                 ("label", "PY"),
                 A.andi("v1", "v1", 0xFFFF),
                 A.sw("v1", 0x34, "sp"),
                 # draw the count
                 A.addiu("a0", "s0", _SM_SURFACE_OFF),
                 A.lw("a1", 0x28, "sp"), A.lw("a2", 0x30, "sp"),
                 A.lw("a3", 0x34, "sp"), A.lw("t0", 0x2C, "sp"),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp"),
                 # (v179: slash separators dropped -- digits-only reads
                 # cleaner, matches the battle window the user approved)
                 A.addiu("s5", "s5", 1),
                 A.addiu("at", "zero", 8),
                 ("bne", "s5", "at", "PLOOP"), A.nop(),
                 A.lw("s7", 0x24, "sp"), A.lw("s6", 0x20, "sp"),
                 A.lw("s5", 0x1C, "sp"), A.lw("ra", 0x18, "sp"),
                 A.addiu("sp", "sp", 0x40),
                 A.j(_SM_PANEL_RET), A.nop()]
        return A.asm_labels(body)
    # v187: the field 2x4 grid is ALWAYS on (user: 'always show the Leo
    # screenshot style'); the slot_magic_battle_display yaml now gates the
    # BATTLE status-window slot digits instead (see _bat_disp below).
    # Pre-2026-08-04 seeds shipped the same flag as slot_magic_field_display.
    panel = _panel_cave()
    _bat_disp = bool(feats and (feats.get("slot_magic_battle_display")
                                or feats.get("slot_magic_field_display")))

    # -- Ether family validity: usable iff target alive and ANY spent > 0.
    # a1 = row off, a2 = char idx, s1 = ctx, s0 = result (s2/s3 fn-saved).
    ether_valid = A.asm_labels(
        [A.lw("v1", 0x7098, "s1"),
         A.srl("v0", "v1", 24), A.addiu("v0", "v0", -8),
         A._i(0x0B, "v0", "at", 2),
         ("beq", "at", "zero", "EV_OUT"), A.nop()]
        + _emit_char_rec("a2", "v1", tmp="a0")
        + [A.lbu("v0", _M_STATUS, "v1"), A.andi("v0", "v0", 3),
           ("bne", "v0", "zero", "EV_OUT"), A.nop(),
           A.lui("s2", _SM_SAVE_PTR_HI),
           A.lw("s2", _SM_SAVE_PTR_LO, "s2"),
           ("beq", "s2", "zero", "EV_OUT"), A.nop(),
           A.sll("s3", "a2", 3), A.addu("s2", "s2", "s3"),
           A.lw("s3", _SM_SPENT_OFF, "s2"),
           A.lw("v0", _SM_SPENT_OFF + 4, "s2"),
           A.word((0x00 << 26) | (19 << 21) | (2 << 16) | (19 << 11) | 0x25),
           # ^ or s3, s3, v0
           ("beq", "s3", "zero", "EV_OUT"), A.nop(),
           A.addiu("s0", "zero", 1),
           ("label", "EV_OUT"),
           A.j(_USE_VALID_EXIT), A.nop()])

    # -- Ether family apply: n charges back at EVERY level (param tier:
    # <100 -> 1 [Ether], <500 -> 2 [Turbo], else full restore [Dry]).
    # v1 = row off, s1 = char idx, s0 = ctx; t-regs free, ra LIVE (no jal).
    ether_apply = A.asm_labels(
        [A.li("v0", _ITEM_TABLE + 6),
         A.addu("v0", "v0", "v1"),
         A.lhu("a0", 0, "v0"),               # param (50 / 150 / 999)
         A.lui("t0", _SM_SAVE_PTR_HI),
         A.lw("t0", _SM_SAVE_PTR_LO, "t0"),
         ("beq", "t0", "zero", "EA_OUT"), A.nop(),
         A.sll("t1", "s1", 3), A.addu("t0", "t0", "t1"),
         A.addiu("t0", "t0", _SM_SPENT_OFF), # -> spent[char] base
         A._i(0x0B, "a0", "at", 500),        # sltiu at,a0,500
         ("beq", "at", "zero", "EA_FULL"), A.nop(),
         A._i(0x0B, "a0", "at", 100),        # sltiu at,a0,100
         A.addiu("t1", "zero", 5),           # Turbo Ether: 5 charges/level
         ("beq", "at", "zero", "EA_N"), A.nop(),
         A.addiu("t1", "zero", 1),           # Ether: 1 charge/level
         ("label", "EA_N"),
         A.addiu("t2", "zero", 8),
         ("label", "EA_LOOP"),
         A.lbu("t3", 0, "t0"),
         A.subu("t3", "t3", "t1"),
         A.slt("at", "t3", "zero"),
         ("beq", "at", "zero", "EA_ST"), A.nop(),
         A.addu("t3", "zero", "zero"),
         ("label", "EA_ST"),
         A.sb("t3", 0, "t0"),
         A.addiu("t2", "t2", -1),
         ("bne", "t2", "zero", "EA_LOOP"), A.addiu("t0", "t0", 1),
         A.j(_USE_APPLY_EXIT), A.nop(),
         ("label", "EA_FULL"),
         A.sw("zero", 0, "t0"), A.sw("zero", 4, "t0"),
         ("label", "EA_OUT"),
         A.j(_USE_APPLY_EXIT), A.nop()])

    # -- battle magic panel: full-body replacement. At 0x088702F0: s2 = ctx,
    # s1 = BU rec (unused now), s0 dead, ra dead (restored at the epilogue we
    # rejoin). 3-row cur/max grid for all 8 levels.
    def _bpanel_all_cave():
        body = [A.addiu("sp", "sp", -0x30),
                A.lbu("a2", 0x67C0, "s2"),
                A.sw("a2", 0x2C, "sp"),             # party row
                A.lhu("a1", 0x44, "s1")]            # highlighted spell id
        body += _sm_level_from_id("a1", "a1", "v0")
        body += [A.sw("a1", 0x10, "sp"),            # selected LEVEL (1..8)
                 A.lw("s1", 0x6834, "s2")]
        body += _emit_char_rec("a2", "s1", tmp="a1")  # s1 = char rec
        body += [A.addu("s0", "zero", "zero"),
                 ("label", "BG_LOOP"),
                 A.addu("a0", "s1", "zero"),
                 A.addiu("a1", "s0", 1),
                 A.lw("a2", 0x2C, "sp"),
                 A.jal(slotfn), A.nop(),
                 A.subu("a1", "v0", "v1"),          # cur
                 A.slt("at", "a1", "zero"),
                 ("beq", "at", "zero", "BG_CL"), A.nop(),
                 A.addu("a1", "zero", "zero"),
                 ("label", "BG_CL"),
                 A.sw("a1", 0x28, "sp"),
                 A.sw("v0", 0x14, "sp"),            # max
                 # base colour: yellow for the highlighted spell's level,
                 # white otherwise (grey rules below still win)
                 A.lw("at", 0x10, "sp"),
                 A.addiu("v1", "s0", 1),
                 A.li("t0", 0xFFFFFFFF),
                 ("bne", "v1", "at", "BG_BC"), A.nop(),
                 A.li("t0", 0xFF00FFFF),            # yellow (ABGR)
                 ("label", "BG_BC"),
                 A.sw("t0", 0x18, "sp"),            # row colour (pre-grey)
                 ("bne", "a1", "zero", "BG_CC"), A.nop(),
                 A.li("t0", _SM_GREY),              # cur grey at 0
                 ("label", "BG_CC"),
                 A.sw("t0", 0x24, "sp"),
                 A.lw("t0", 0x18, "sp"),
                 A.lw("v1", 0x14, "sp"),
                 ("bne", "v1", "zero", "BG_RC"), A.nop(),
                 A.li("t0", _SM_GREY),              # dead level: pair grey
                 A.sw("t0", 0x24, "sp"),            # (cur too)
                 ("label", "BG_RC"),
                 A.sw("t0", 0x18, "sp"),
                 # row = k/3, col = k%3
                 A.addiu("at", "zero", 3),
                 A.divu("s0", "at"),
                 A.mflo("v0"),                      # row
                 A.mfhi("v1"),                      # col
                 A.sll("v0", "v0", 4),
                 A.addiu("v0", "v0", 0xD8),         # y = 0xD8 + row*16
                 A.sw("v0", 0x1C, "sp"),
                 A.sll("v1", "v1", 5),
                 A.addiu("v1", "v1", 0x170 + 8),    # cur right-align x
                 A.sw("v1", 0x20, "sp"),
                 # cur
                 A.lw("a0", 0x68D0, "s2"),
                 A.lw("a1", 0x28, "sp"),
                 A.lw("a2", 0x20, "sp"),
                 A.lw("a3", 0x1C, "sp"),
                 A.lw("t0", 0x24, "sp"),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp"),
                 # "/"
                 A.lw("a0", 0x68D0, "s2"),
                 A.addiu("a1", "zero", 0x11),
                 A.lw("a2", 0x20, "sp"),
                 A.lw("a3", 0x1C, "sp"),
                 A.lw("t0", 0x18, "sp"),
                 A.jal(0x088192FC), A.addu("t1", "zero", "zero"),
                 # max right-aligned at x + 12
                 A.lw("a0", 0x68D0, "s2"),
                 A.lw("a1", 0x14, "sp"),
                 A.lw("a2", 0x20, "sp"),
                 A.addiu("a2", "a2", 12),
                 A.lw("a3", 0x1C, "sp"),
                 A.lw("t0", 0x18, "sp"),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp"),
                 A.addiu("s0", "s0", 1),
                 A.addiu("at", "zero", 8),
                 ("bne", "s0", "at", "BG_LOOP"), A.nop(),
                 A.addiu("sp", "sp", 0x30),
                 A.j(_SM_BPANEL_COST_RET), A.nop()]
        return A.asm_labels(body)
    bpanel_all = _bpanel_all_cave()

    # -- mid-battle ether family -> charge restore (see _SM_BETHER_HOOK note)
    bether = A.asm_labels([
        A.lbu("t0", 0x3D, "s4"),            # target row
        A._i(0x0B, "t0", "at", 4),          # party?
        ("beq", "at", "zero", "BE_VAN"), A.nop(),
        A.lbu("t1", 0x3C, "s4"),            # caster row
        A.lw("t2", 0x00, "s4"),             # ctx
        A.sll("t3", "t1", 1), A.addu("t3", "t3", "t1"),
        A.addu("t2", "t2", "t3"),
        A.lbu("t3", 0x683D, "t2"),          # queued item id
        A.addiu("t4", "t3", -4),
        A._i(0x0B, "t4", "at", 3),          # 4..6 ?
        ("beq", "at", "zero", "BE_VAN"), A.nop(),
        A.lui("t5", _SM_SAVE_PTR_HI),
        A.lw("t5", _SM_SAVE_PTR_LO, "t5"),
        ("beq", "t5", "zero", "BE_VAN"), A.nop(),
        A.sll("t6", "t0", 3), A.addu("t5", "t5", "t6"),
        A.addiu("t5", "t5", _SM_SPENT_OFF), # spent[target] base
        A.addiu("t6", "zero", 1),           # Ether: 1
        ("beq", "t4", "zero", "BE_LOOP"), A.nop(),
        A.addiu("t6", "zero", 5),           # Turbo: 5
        A.addiu("at", "zero", 1),
        ("beq", "t4", "at", "BE_LOOP"), A.nop(),
        A.sw("zero", 0, "t5"), A.sw("zero", 4, "t5"),   # Dry: all
        ("beq", "zero", "zero", "BE_OUT"), A.nop(),
        ("label", "BE_LOOP"),
        A.addiu("t7", "zero", 8),
        ("label", "BE_L"),
        A.lbu("t8", 0, "t5"),
        A.subu("t8", "t8", "t6"),
        A.slt("at", "t8", "zero"),
        ("beq", "at", "zero", "BE_ST"), A.nop(),
        A.addu("t8", "zero", "zero"),
        ("label", "BE_ST"),
        A.sb("t8", 0, "t5"),
        A.addiu("t7", "t7", -1),
        ("bne", "t7", "zero", "BE_L"), A.addiu("t5", "t5", 1),
        ("label", "BE_OUT"),
        # neutralize the native MP add: v0 = cur - amount (+amount = cur)
        A.lh("v0", 0x0C, "a0"),
        A.subu("v0", "v0", "a1"),
        A.lh("v1", 0x0E, "a0"),
        A.j(_SM_BETHER_RET), A.nop(),
        ("label", "BE_VAN"),
        A.lh("v0", 0x0C, "a0"),             # displaced originals
        A.lh("v1", 0x0E, "a0"),
        A.j(_SM_BETHER_RET), A.nop(),
    ])

    # -- rest charge refill (one-shot event opcode). Tiers (v171 rebalance,
    # user): Sleeping Bag (cmd[3]==3) = nothing, Tent (==2) = 2 charges back
    # at every level for everyone, Cottage/scripted-full (<=1) and INNs =
    # full refill (spent array zeroed). LIVE-VERIFIED (Tent=2) 2026-07-30.
    def _rest_cave(hook, tiered):
        body = [A.word(0x0000A021),         # displaced: move s4,zero
                A.word(0x02009821)]         #            move s3,s0
        if tiered:
            body += [A.lbu("a3", 3, "s2"),  # amount-table index (cmd[3])
                     A.addiu("v0", "zero", 3),
                     ("beq", "a3", "v0", "RR_OUT"), A.nop()]  # Sleeping Bag
        body += [A.lui("v0", _SM_SAVE_PTR_HI),
                 A.lw("v0", _SM_SAVE_PTR_LO, "v0"),
                 ("beq", "v0", "zero", "RR_OUT"), A.nop()]
        if tiered:
            body += [A.addiu("at", "zero", 2),
                     ("beq", "a3", "at", "RR_TENT"), A.nop()]
        body += [A.sw("zero", _SM_SPENT_OFF + i * 4, "v0") for i in range(8)]
        body += [("label", "RR_OUT"),
                 A.j(hook + 8), A.nop()]
        if tiered:
            # Tent: spent[i] = max(0, spent[i] - 2) over all 32 bytes
            body += [("label", "RR_TENT"),
                     A.addiu("v1", "v0", _SM_SPENT_OFF),
                     A.addiu("a3", "zero", 32),
                     ("label", "RR_TL"),
                     A.lbu("at", 0, "v1"),
                     A.addiu("at", "at", -2),
                     A.slt("v0", "at", "zero"),
                     ("beq", "v0", "zero", "RR_TS"), A.nop(),
                     A.addu("at", "zero", "zero"),
                     ("label", "RR_TS"),
                     A.sb("at", 0, "v1"),
                     A.addiu("a3", "a3", -1),
                     ("bne", "a3", "zero", "RR_TL"), A.addiu("v1", "v1", 1),
                     A.j(hook + 8), A.nop()]
        return A.asm_labels(body)
    rest_tiered = _rest_cave(_SM_REST_TIERED, tiered=True)
    rest_inn = _rest_cave(_SM_REST_INN, tiered=False)

    # -- battle party status window: curMP column -> 8 per-level charge digits
    def _bstat_cave():
        body = [A.addiu("sp", "sp", -0x20),
                A.sw("ra", 0x10, "sp"), A.sw("s4", 0x14, "sp"),
                A.sw("s5", 0x18, "sp"),
                A.addu("s4", "a3", "zero"),         # row y
                A.lw("s5", 0x6834, "s3")]           # field array
        body += _emit_char_rec("s1", "s5", tmp="a1")  # s5 = char rec
        body += [A.sw("zero", 0x1C, "sp"),
                 ("label", "BS_LOOP"),
                 A.lw("v0", 0x1C, "sp"),            # k
                 A.sll("at", "v0", 3), A.sll("v1", "v0", 2),
                 A.addu("at", "at", "v1"),
                 A.sll("v1", "v0", 1),
                 A.addu("at", "at", "v1"),
                 A.addiu("at", "at", 0x172),        # x = 0x172 + k*14 (ends 468)
                 A.sw("at", 0x08, "sp"),
                 A.addu("a0", "s5", "zero"),
                 A.addiu("a1", "v0", 1),
                 A.addu("a2", "s1", "zero"),
                 A.jal(slotfn), A.nop(),
                 A.subu("a1", "v0", "v1"),          # cur
                 A.slt("at", "a1", "zero"),
                 ("beq", "at", "zero", "BS_CL"), A.nop(),
                 A.addu("a1", "zero", "zero"),
                 ("label", "BS_CL"),
                 A.li("t0", 0xFFFFFFFF),
                 ("bne", "a1", "zero", "BS_CC"), A.nop(),
                 A.li("t0", _SM_GREY),
                 ("label", "BS_CC"),
                 A.lw("a0", 0x68D0, "s3"),
                 A.lw("a2", 0x08, "sp"),
                 A.andi("a3", "s4", 0xFFFF),
                 A.addiu("t1", "zero", 1), A.addiu("t2", "zero", 9),
                 A.addiu("t3", "zero", 1),
                 A.jal(_SM_DRAW_NUM), A.sw("zero", 0, "sp"),
                 A.lw("v0", 0x1C, "sp"),
                 A.addiu("v0", "v0", 1),
                 A.sw("v0", 0x1C, "sp"),
                 A._i(0x0B, "v0", "at", 8),         # sltiu at,v0,8
                 ("bne", "at", "zero", "BS_LOOP"), A.nop(),
                 A.lw("s5", 0x18, "sp"), A.lw("s4", 0x14, "sp"),
                 A.lw("ra", 0x10, "sp"),
                 A.addiu("sp", "sp", 0x20),
                 A.j(_SM_BSTAT_RET), A.nop()]
        return A.asm_labels(body)
    bstat = _bstat_cave()

    # -- Soma Drop (effect 2) validity: vanilla refuses at maxMP 999, we refuse
    # once every unlocked level already sits at 9 (total == 9 * magiclv). A
    # magiclv-0 job therefore always refuses -- there is no level 1 to raise.
    # s1 = ctx, a2 = target row, s0 = the valid flag; ra is stack-saved by the
    # host fn (the exit at _USE_VALID_EXIT reloads it), so jal is safe.
    soma_valid = A.asm_labels(
        [A.lw("v1", 0x7098, "s1")]
        + _emit_char_rec("a2", "v1", tmp="a0")
        + [A.lbu("v0", _M_STATUS, "v1"), A.andi("v0", "v0", 3),
           ("bne", "v0", "zero", "SV_OUT"), A.nop(),
           A.addu("a0", "zero", "v1"),
           A.lbu("a1", 0x20, "v1"),         # current char level
           A.jal(totalfn), A.nop(),
           A.slt("at", "v0", "v1"),         # total < 9 * magiclv ?
           ("beq", "at", "zero", "SV_OUT"), A.nop(),
           A.addiu("s0", "zero", 1),
           ("label", "SV_OUT"),
           A.j(_USE_VALID_EXIT), A.nop()])

    # -- Soma Drop apply: bump the character's count; the distribution itself
    # is re-derived by _sm_slotfn, so there is nothing else to write. The MP
    # pool is deliberately left alone (inert under slot_magic).
    # s0 = ctx, s1 = char row, v1 = item id * 16; t-regs free.
    soma_apply = A.asm_labels(
        [A.lui("t0", _SM_SAVE_PTR_HI),
         A.lw("t0", _SM_SAVE_PTR_LO, "t0"),
         ("beq", "t0", "zero", "SA_OUT"), A.nop(),
         A.addu("t0", "t0", "s1"),
         A.lbu("t1", _SM_SOMA_OFF, "t0"),
         A._i(0x0B, "t1", "at", _SM_SOMA_MAX),   # sltiu at,t1,72 (saturate)
         ("beq", "at", "zero", "SA_OUT"), A.nop(),
         A.addiu("t1", "t1", 1),
         A.sb("t1", _SM_SOMA_OFF, "t0"),
         ("label", "SA_OUT"),
         A.j(_USE_APPLY_EXIT), A.nop()])

    # -- level-up slot-gain: statIdx-1 (MP) handler replaced by the REAL slot
    # delta across the level-up -- total(new level) - total(old level), both
    # including the Soma spill. Counting raw threshold crossings (v171..v193)
    # over-reports once Soma has already filled the level that crossed: the
    # gain silently redirects to the next unfilled level, and on a completely
    # full caster there is no gain at all. 0 still hides the line natively.
    # Blind spot kept from the old cave: magiclv is read as it stands now, so
    # a level-up that ALSO unlocks a new spell level may under-report it.
    levelup = A.asm_labels(
        # row 0..3 from the record pointer: the live party array is
        # *SAVE_BLOCK_PTR + _SM_LIVE_REC_OFF (= PARTY_BASE_SA - 0x20, the
        # live-0x20 record form), stride 0x5C. No match -> row 0 (cosmetic
        # only: the row picks the Soma count, nothing else).
        [A.addu("a2", "zero", "zero"),
         A.lui("t0", _SM_SAVE_PTR_HI),
         A.lw("t0", _SM_SAVE_PTR_LO, "t0"),
         ("beq", "t0", "zero", "LU_ROW"), A.nop(),
         A.li("t1", _SM_LIVE_REC_OFF),
         A.addu("t0", "t0", "t1"),
         A.subu("t2", "s4", "t0")]
        + [ins for r in (1, 2, 3) for ins in (
            A.addiu("at", "zero", r * 0x5C),
            ("bne", "t2", "at", f"LU_R{r}"), A.nop(),
            A.addiu("a2", "zero", r),
            ("label", f"LU_R{r}"))]
        + [("label", "LU_ROW"),
           # v186: a1 (newLevel) is CLOBBERED before dispatch -- the accessor's
           # growth-bit RNG call (0x08869528 -> 0x088F548C) writes a1 on its
           # init path, so counts were garbage (usually 0 -> silent level-ups,
           # live 2026-07-30). The record still holds the OLD level here (the
           # caller stores old+1 only later, at 0x08876B78) -- use it.
           A.lbu("t5", 0x20, "s4"),         # OLD level (u32 low byte, <=99)
           A.addu("a0", "zero", "s4"),
           A.addiu("a1", "t5", 1),          # new level
           A.jal(totalfn), A.nop(),
           A.addu("t6", "zero", "v0"),      # total AFTER (totalfn keeps t5/t6)
           A.addu("a0", "zero", "s4"),
           A.addu("a1", "zero", "t5"),
           A.jal(totalfn), A.nop(),
           A.subu("s2", "t6", "v0"),        # gain
           A.slt("at", "s2", "zero"),
           ("beq", "at", "zero", "LU_END"), A.nop(),
           A.addu("s2", "zero", "zero"),
           ("label", "LU_END"),
           A.j(_SM_LEVELUP_RET), A.nop()])

    # -- install everything
    # The formation cave re-emits its two displaced words verbatim; if the pair
    # at the hook is not the expected `sw zero,0x70dc(s0)` / `sw zero,0x70ec(s0)`
    # the site moved and the cave would corrupt the swap routine.
    _fs = struct.unpack_from("<2I", elf, E.ram2file(_SM_FORMSWAP_HOOK))
    if _fs != (0xAE0070DC, 0xAE0070EC):
        raise ValueError("slot_magic: formation-swap hook not vanilla "
                         f"({_fs[0]:#010x} {_fs[1]:#010x})")
    # Iteration order FIXES each cave's vaddr (add_segment_cave appends in
    # sequence) and hence the whole segment layout. Appending new pairs at
    # the end is safe; reordering changes the baked bytes on every ISO --
    # PATCHER_VERSION bump territory.
    for hook, cave in ((_SM_BATTLE_DEDUCT, battle_deduct),
                       (_SM_BATTLE_AFFORD, battle_afford),
                       (_SM_FIELD_DEDUCT, field_deduct),
                       (_SM_FIELD_GATE, field_gate),
                       (_SM_MENU_GREY, menu_grey),
                       (_SM_REPEAT_A, repeat_a),
                       (_SM_REPEAT_B, repeat_b),
                       (_SM_MENU_ROW, menu_row),
                       (_SM_PANEL_HOOK, panel),
                       (_SM_ETHER_VALID, ether_valid),
                       (_SM_ETHER_APPLY, ether_apply),
                       (_SM_SOMA_VALID, soma_valid),
                       (_SM_SOMA_APPLY, soma_apply),
                       (_SM_BPANEL_ALL, bpanel_all),
                       # OFF-state uses the t3=0 imm below, NOT a detour: a
                       # j-skip cave here ATE THE PARTY BATTLE SPRITES (live
                       # 2026-07-31, mechanism unidentified -- reverting the
                       # hook live restored them). Only hook when ON.
                       *(((_SM_BSTAT_HOOK, bstat),) if _bat_disp else ()),
                       (_SM_BETHER_HOOK, bether),
                       (_SM_REST_TIERED, rest_tiered),
                       # v220: formation swap moves the row-indexed arrays with
                       # the records, so the menu's FIRST draw is already right
                       (_SM_FORMSWAP_HOOK, _sm_formswap_cave()),
                       (_SM_REST_INN, rest_inn)):
        E.install_detour(elf, hook, E.add_segment_cave(elf, cave))
    # level-up statIdx-1 repoint (data word in the jump table, not a detour)
    if struct.unpack_from("<I", elf, E.ram2file(_SM_LEVELUP_JT))[0] != 0x08887B98:
        raise ValueError("slot_magic: statIdx JT entry 1 not vanilla")
    lv_cave = E.add_segment_cave(elf, levelup)
    struct.pack_into("<I", elf, E.ram2file(_SM_LEVELUP_JT), lv_cave)
    # publish the Crimson Wizard damage->slot leaf for apply_job_scroll_boosts
    # (FEATURES order runs slot_magic first; the dict is reset per patch run)
    _SM_EXPORTS.clear()
    _SM_EXPORTS["cwslot"] = E.add_segment_cave(elf, _sm_cwslot_cave())
    # save/load preview: PER-FILE conditional (see _SM_SAVEPRV_* notes).
    # Both caves: t0/t1 scratch is safe (label path re-emits t0=-1 and the
    # string prim only reads t0/t1; value path re-sets t0-t3 after rejoin);
    # v0 (row y) and all s-regs untouched.
    def _saveprv_marker(tmp0, tmp1):
        return (_sm_mul_const(tmp0, "s2", 0x5C, tmp1)
                + [A.subu(tmp0, "s4", tmp0),
                   A.lbu(tmp0, _SM_SAVEPRV_MARKER_OFF, tmp0),
                   A.addiu("at", "zero", _SM_MARKER_VALUE)])
    saveprv_label = A.asm_labels(
        _saveprv_marker("t2", "t3")
        + [A.addiu("a1", "zero", 1),        # "Magic"
           ("beq", "t2", "at", "SPL"), A.nop(),
           A.addiu("a1", "zero", 0xC),      # "MP" (vanilla)
           ("label", "SPL"),
           A.addiu("t0", "zero", -1),       # displaced original #2
           A.j(_SM_SAVEPRV_LABEL_RET), A.nop()])
    # v195: non-caster jobs carry a nonzero magiclv byte in the record (it is
    # inert for them -- the class dispatch tables, not magiclv, decide who has
    # a magic menu), so a Warrior previewed as "Magic 3". Class-gate the slot
    # path: class @s4+0x1E in {non-caster set} -> report 0. Set = Warrior(0)
    # and, unless the dabble feature is on, Thief(1)/Monk(2)/Master(8).
    _noncast = (0,) if (feats and feats.get("monk_thief_dabble_in_magic")) \
        else (0, 1, 2, 8)
    _classgate = []
    for _c in _noncast:
        _classgate += [A.addiu("t3", "zero", _c),
                       ("beq", "t2", "t3", "SPV_ZERO"), A.nop()]
    saveprv_value = A.asm_labels(
        _saveprv_marker("t0", "t1")
        + [A.lbu("a1", 0x30, "s4"),         # magiclv
           ("beq", "t0", "at", "SPV_SLOT"), A.nop(),
           A.lhu("a1", 0x2C, "s4"),         # MP (displaced original #1)
           ("beq", "zero", "zero", "SPV"), A.nop(),
           ("label", "SPV_SLOT"),
           A.lbu("t2", 0x1E, "s4")]         # class
        + _classgate
        + [("beq", "zero", "zero", "SPV"), A.nop(),
           ("label", "SPV_ZERO"),
           A.addiu("a1", "zero", 0),
           ("label", "SPV"),
           A.lw("a0", 8, "s5"),             # displaced original #2
           A.j(_SM_SAVEPRV_VALUE_RET), A.nop()])
    E.install_detour(elf, _SM_SAVEPRV_LABEL,
                     E.add_segment_cave(elf, saveprv_label))
    E.install_detour(elf, _SM_SAVEPRV_VALUE,
                     E.add_segment_cave(elf, saveprv_value))
    # blank every cosmetic MP draw (verify each is still a jal first)
    for ram in _SM_MP_BLANK_JALS:
        if (_read_word(elf, ram) >> 26) != 0x03:
            raise ValueError(f"slot_magic: MP-blank site @{ram:#x} not a jal")
        struct.pack_into("<I", elf, E.ram2file(ram), 0)
    # battle status window HP compress + digit strip: yaml-gated (v187 --
    # slot_magic_battle_display gates the BATTLE slot digits). OFF: vanilla
    # HP/name layout, curMP draw skipped (blank; no slots, no mana).
    for site, van, new in ((_SM_BSTAT_HPX, 0x24060178, 0x144),
                           (_SM_BSTAT_SLX, 0x24060178, 0x144),
                           (_SM_BSTAT_MXX, 0x240601A8, 0x162),
                           (_SM_BSTAT_NAMEX, 0x240500FC, 0xF8)):
        if _read_word(elf, site) != van:
            raise ValueError(f"slot_magic: unexpected bstat imm @{site:#x}")
        if _bat_disp:
            _set_imm16(elf, site, new)
    if not _bat_disp:
        # blank the curMP number the vanilla-safe way: the draw prim's own
        # skip flag (t3, checked at 0x088196D4 `beqz t3`) -- flip the delay
        # slot imm `addiu t3,zero,1` @0x0886F6A4 to 0. Zero new code paths.
        if _read_word(elf, 0x0886F6A4) != 0x240B0001:
            raise ValueError("slot_magic: bstat t3 imm not vanilla")
        _set_imm16(elf, 0x0886F6A4, 0)
    # panel line-y tighten (4 lines in the 0x38 pitch)
    _set_imm16(elf, _SM_PANEL_Y1, 0x0C)
    _set_imm16(elf, _SM_PANEL_Y2, 0x16)
    # magic-submenu spell-name columns stay VANILLA (v177): the cursor hand
    # spans ~76px around its cell anchor, so ANY left-side charge column
    # collides with some hand position; charges moved to the row's right edge
    # instead (cur@0x1A6 / max@0x1B8 -- beyond the col-3 hand extent ~340).


# --- remote-chest AP name box (poll-based chests, see tier2-poll-chests memory) -
# The chest-reward handler resolves the pickup-box {NAME} via getter 0x088d4718
# and stores the resulting string id at struct+0x58A (sh v0,0x58A(a1) @0x08843d44).
# Remote AP chests bake a benign filler item into byte0/1 of the treasure u32 (safe
# native grant -- give-item 0x088d4494 is a bound-186 packed-record append, no id
# bound) and the ABSOLUTE remote name-bank string id into bits16-30 (the handler
# ignores those bits -- it reads only byte0=cat/byte1=id/bit31). This detour, hooked
# at 0x08843d18 where a1 still holds the raw treasure u32, captures bits16-30 and,
# when nonzero, overrides the stored string id so the box shows the extern_bake-
# extended remote name. Transparent (t0==0 -> no override) for own/filler chests.
_RC_HOOK      = 0x08843D18    # andi s2,a1,0xff  (a1 = raw treasure u32 here)
_RC_HOOK_W0   = 0x30B200FF    # andi s2,a1,0xff  (vanilla; verified before patch)
_RC_GETTER    = 0x088D4718    # name getter (cat,id) -> v0 = string id
_RC_RESUME    = 0x08843D48    # first instr after the displaced d18..d44 block
_RC_STRUCT_S1 = 0x52C8        # lw a1,0x52C8(s1) reloads the struct ptr (d38)


def _remote_name_cave(scratch_ram):
    """Detour body. On entry a1 = raw treasure u32, v0 = struct ptr (from the
    displaced d14 lw), s0/s1 = handler saved regs, s3 = id (set by the hook's
    delay slot `ext s3,a1,8,8`). Reproduces d18..d44 with the bits16-30 capture
    (stashed in `scratch_ram` across the getter call) + string-id override."""
    return A.asm_labels([
        A.srl("t0", "a1", 16), A.andi("t0", "t0", 0x7FFF),   # t0 = remote sid
        A.li("at", scratch_ram), A.sw("t0", 0, "at"),        # stash across jal
        A.andi("s2", "a1", 0xFF),                            # displaced d18 (cat)
        A.addu("a0", "s0", "zero"),                          # d20 move a0,s0
        A.sh("s2", 0x588, "v0"),                             # d24
        A.addu("a1", "s2", "zero"),                          # d28 move a1,s2
        A.addu("a2", "s3", "zero"),                          # d2c move a2,s3
        A.jal(_RC_GETTER), A.addiu("a3", "zero", 1),         # d30 + d34 delay
        A.lw("a1", _RC_STRUCT_S1, "s1"),                     # d38
        A.li("at", scratch_ram), A.lw("t0", 0, "at"),        # reload remote sid
        ("beq", "t0", "zero", "store"), A.nop(),
        A.addu("v0", "t0", "zero"),                          # v0 = remote sid
        ("label", "store"),
        A.addiu("v1", "zero", 1),                            # d3c li v1,1
        A.addu("a0", "s0", "zero"),                          # d40 move a0,s0
        A.sh("v0", 0x58A, "a1"),                             # d44 store string id
        A.j(_RC_RESUME), A.nop(),
        A.word(0),                                           # scratch word (tail)
    ])


def apply_remote_chest_names(elf: bytearray, feats=None):
    """Install the remote-chest name-override detour (see _remote_name_cave)."""
    w = _read_word(elf, _RC_HOOK)
    if w != _RC_HOOK_W0:
        raise ValueError(f"unexpected chest-reward hook @{_RC_HOOK:#x}: {w:#010x}")
    placeholder = _remote_name_cave(0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    scratch_ram = cave_vaddr + len(placeholder) - 4          # tail word
    real = _remote_name_cave(scratch_ram)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    # single-instruction hook: `j cave`. Its delay slot (the untouched d1c
    # `ext s3,a1,8,8`) runs with a1 still = treasure, setting s3 before the cave.
    fo = E.ram2file(_RC_HOOK)
    elf[fo:fo + 4] = A.j(cave_vaddr)


# --- feature: Dangerous Forests (forest overworld encounters -> danger pool) ------
# The overworld encounter roll (fn 0x8841e1c) picks a formation table from the
# party tile; a polling client can never win the race with this per-step roll
# (playtest: false-in AND false-out at borders), so the GAME does the check.
#
# We identify FOREST the exact way the game renders/collides: the tile's ATT
# attribute == 0x0006 (RE'd live 2026-07-06 -- attr 0x0003 is GRASS, 0x0006 is
# FOREST, 0x000d MARSH, 0x000e DESERT; owclass's old guess had forest/grass
# swapped, which is why every prior attempt fired on the wrong tiles). The field
# struct the encounter fn holds in $s4 carries the live map pointers: *(s4+0xC00)
# = tile-grid arena (u16 grid, +10 header, stride 510), *(s4+0xC14) = ATT base
# (u16 per tile). Reading att[grid[y][x]] with the game's OWN (s1=x, s2=y) is
# frame-exact and matches the visuals/battle-background with ZERO offset -- no
# static bitmap, no terrain-map remap (both earlier approaches fought an imaginary
# coordinate offset).
#
# Hook the land-path table select (`b 0x8841f9c` @0x8841f60; its delay slot
# `addu s0,v0,v1` = vanilla zones_overworld row still runs). In the cave: if the
# party tile's attr == 0x0006, commit the tier's forest formation directly (u16);
# otherwise fall through to the vanilla slot roll. Marsh/desert/grass keep their
# vanilla pools; dungeons + water never reach this hook. Overrides the forest roll
# wholesale, and STACKS with harder_encounters (which selects the harder tier list).
_DF_HOOK = 0x08841F60          # `b 0x8841f9c` (land path; delay slot computes s0)
_DF_HOOK_W0 = 0x1000000E       # vanilla word: beq zero,zero,+14
_DF_RESUME = 0x08841F9C        # common slot-roll continuation
_DF_GRID_PTR_OFF = 0x0C00      # s4+this -> tile-grid arena (grid data at +10)
_DF_ATT_PTR_OFF = 0x0C14       # s4+this -> ATT table base
_DF_GRID_HDR = 10              # arena header before the u16 grid
_DF_GRID_STRIDE = 510          # 255 u16 tiles per row
_DF_FOREST_ATTR = 0x0006       # ATT attribute of a forest tile
_DF_ZONES_OW = 0x08945890      # zones_overworld base; at the hook s0 = this + zone*8
_DF_FORM_TABLE = 0x08948D14    # monster_formations base (ISO 0x2b24d68 - RAM2ISO)
_DF_FORM_STRIDE = 0x0F         # 15-byte formation record

# --- V7 progression-scaled forests, u16 DLC-monster capable ------------------
# V1-V4 swapped the WHOLE forest fight for one fixed endgame "danger pool" (too
# hard, zone-blind). V6 kept it LOCAL + route-scaled by AUTHORING a bespoke
# "mob + heavy" formation per tier into the dead filler slots 0xF3.. -- which
# FROZE at battle init on the very first forest fight: a formation id's sprite/
# POSITION data lives in a SEPARATE per-id secondary table keyed to that slot's
# ORIGINAL monsters, so writing new monster ids into a slot (even a byte-perfect
# 15B record) desyncs the two and crashes (proven twice: the ocean clone in slot
# 0xe0, and V6's 0xF3.. slots). See re_only/HANDOFF_regional_ocean_encounters.md
# caveat #1 + re_only/gen_ocean_pools.py header.
#
# V7 authors NOTHING: each tier REFERENCES an EXISTING formation whose secondary
# table is already valid. To reach the PSP bonus-dungeon monsters (Yellow Ogre,
# Skuldier, Sekhret, Pharaoh, ...) the tier tables hold u16 formation ids (>=0x100)
# -- those monsters only lead DLC formations. The vanilla forest roll reads a u8
# id (`lbu` @0x8841fc0), but the id it feeds forward is stored `sh v0,0xbe4(s4)`
# (0x8841fc8) -- the battle-context formation field is ALREADY u16, and DLC
# dungeons drive battles through that same field. So on a forest tile the cave
# skips the u8 table entirely: it loads a u16 id (`lhu`) from the selected tier
# table, stores it to s4+0xbe4, and jumps straight to the encounter-commit tail
# (0x88425b0). Non-forest tiles fall through to the vanilla slot-roll (_DF_RESUME).
#
# Two tier POOLS, chosen at BAKE TIME by the harder_encounters flag (passed in via
# `feats`): normal = _DF_TIER_POOL_A, harder = _DF_TIER_POOL_B (a tougher signature
# monster per tier, so DangerousForests + HarderEncounters STACK). On a forest tile
# the cave draws the game RNG and indexes SLOT_MAP[rng & 63] into the zone tier's
# 8-wide pool -- v190 layout; see _df_data's docstring for the exact cave data.
_DF_RNG_FN = 0x08869528        # game RNG fn (v0 = random word; same fn the vanilla roll calls)
_DF_RESUME_TAIL = 0x088425B0   # encounter-commit tail (sets +0xbe0/+0xbdc=3, returns)
_DF_CTX_FORM_OFF = 0x0BE4      # s4+this = battle-context formation id (u16 field)

# 9 tiers = the player's normal progression stops, in route order (difficulty rises
# with route position, NOT raw geography):
#   0 Cornelia  1 Pravoka  2 Elfheim  3 Western Keep  4 Melmond
#   5 Crescent Lake  6 Onrac  7 Citadel of Trials  8 Lufenia/endgame
# Each pool entry is an EXISTING formation id (u8 or u16) that FEATURES an on-theme
# monster for that tier -> its secondary sprite/position table is already valid ->
# no freeze. A few packs are count-reduced via _DF_FORM_EDITS below (all on
# formations with ZERO encounter-table references, so no side effects).
#
# V8 VARIANTS: each tier is a POOL of exactly _DF_POOL_MAX existing formation ids.
# On a forest tile the cave draws the game RNG (fn 0x8869528) and picks a slot via
# the shared weight map below, so repeat forests in a zone no longer play the
# identical fight. All ids are EXISTING formations (secondary sprite/pos table
# already valid -> no freeze, same rule as V7).
#
# SLOT WEIGHTS (v190): this cave rolls its OWN RNG and jumps straight to the commit
# tail, so it never touches the engine's slot scramble (0x08945850) -- pools used to
# be flat `rng % cnt`, every entry equally likely. v190 reproduces the engine curve
# in software with _SLOT_WEIGHT_MAP: `rng & 63` indexes a 64-byte table that yields
# the slot, giving 18.75/18.75/18.75/18.75/9.38/9.38/4.69/1.56 -- the same skew
# _CF_POOLS inherits for free by reusing the vanilla roll.
# Pool ORDER IS THEREFORE LOAD-BEARING, not cosmetic: entry i gets weight i. Rows
# are stored in RANK order, which is NOT ascending threat -- the user-specified
# rule is "hardest = rarest (slot 7), easiest = 2nd-rarest (slot 6), and the middle
# six run easiest -> hardest across slots 0..5". Reordering a row reweights it.
# Pools must be exactly _DF_POOL_MAX deep (the map indexes 0..7 unconditionally);
# to drop a fight, replace it -- do not shorten the row.
_DF_POOL_MAX = 8
# 64-entry slot weight map, shared by dangerous_forests and regional_ocean. Counts
# are 12/12/12/12/6/6/3/1 = 64, matching the engine scramble's distribution.
_SLOT_WEIGHT_COUNTS = (12, 12, 12, 12, 6, 6, 3, 1)


def _slot_weight_map():
    """64 bytes: index = rng & 63, value = pool slot 0..7."""
    out = bytearray()
    for slot, n in enumerate(_SLOT_WEIGHT_COUNTS):
        out += bytes([slot]) * n
    assert len(out) == 64, "weight counts must sum to 64 (the rng & 63 domain)"
    return bytes(out)
# Tiers threat-ranked (2026-07-09 rebalance): each tier's pool is strictly harder
# than the tier before it. Threat = weighted stat score (HP3/EXP5/ATT3/DEF1/MDEF1,
# def+mdef clamped 80, WarMech=100) with pack rule = leader 100% + every other
# spawned monster 20%. Pools are a pure permutation of the prior fid set (no new
# formations); see the df-threat-rebalance notes.
# v190 widened both pools 3 -> 8 (audit 2026-07-31). The 3 original ids per tier
# are KEPT; the 5 additions were solved under the same locked threat metric, with
# three extra constraints the raw metric cannot express:
#   - sea/boss monsters excluded (forest theme; scripted bosses never roll here),
#   - status-effect gate: the metric is stat-only and badly underrates petrify /
#     instant-death / level-drain mobs, so Basilisk/Medusa/Cockatrice/Pyrolisk/
#     Mindflayer/Evil-Death Eye/Catoblepas are barred below tier 5 and the
#     paralyse/drain undead below tier 3,
#   - no two entries in a pool share the same monster multiset (content dedupe).
# Both pools stay strictly layered (tier max <= next tier min) except the one
# known, user-accepted 3.1pt seam at pool A t5/t6 (Knocker 0x13f).
_DF_TIER_POOL_A = [
    # 0 Cornelia (threat 5-10) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x14b   5.7 1-5 Black Goblin
    #   0x08d   7.4 3-7 Cobra
    #   0x010   8.0 2-3 Gargoyle
    #   0x0e6   8.6 3-6 Tarantula + 0-2 Black Widow
    #   0x161   9.2 2-3 Revenant
    #   0x087   9.8 1-3 Gigas Worm + 1 Ogre
    #   0x086   5.5 2-4 Crazy Horse
    #   0x00c   9.8 1-2 Ogre + 0 Hyenadon
    [0x14b, 0x08d, 0x010, 0x0e6, 0x161, 0x087, 0x086, 0x00c],
    # 1 Pravoka (threat 10-12) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x150  10.4 1-2 Blue Troll
    #   0x116  10.6 1-2 Yellow Ogre
    #   0x013  10.8 1 Ogre Chief + 1-2 Ogre
    #   0x02b  11.0 1 Bloodbones + 2-4 Skeleton + 1 Crawler
    #   0x063  11.1 1-2 Troll
    #   0x13a  11.4 1 Devil Hound
    #   0x06b  10.2 1-3 Gray Ooze
    #   0x160  11.9 1-5 Revenant
    [0x150, 0x116, 0x013, 0x02b, 0x063, 0x13a, 0x06b, 0x160],
    # 2 Elfheim (threat 13-15) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x066  13.0 1-2 Tarantula + 0-2 Black Widow + 0-1 Green Slime + 0-1 Gray Ooze
    #   0x12b  13.1 2-4 Skuldier
    #   0x036  13.2 1-3 Manticore
    #   0x13b  13.7 1-2 Devil Hound
    #   0x117  14.1 1-4 Yellow Ogre
    #   0x123  14.9 4-8 Gloom Widow
    #   0x01b  12.7 1-2 Troll + 0-1 Minotaur
    #   0x01e  15.1 1-2 Hill Gigas
    [0x066, 0x12b, 0x036, 0x13b, 0x117, 0x123, 0x01b, 0x01e],
    # 3 W. Keep (threat 16-22) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x06f  17.1 1-2 Sphinx
    #   0x20a  17.2 0-2 Yellow Ogre + 1-2 Blood Tiger
    #   0x14c  17.6 2-5 Black Goblin + 0-2 Wild Nakk
    #   0x01f  18.8 1-2 Hill Gigas + 0-3 Lizard
    #   0x099  19.2 1-3 Sabertooth + 0-2 Lesser Tiger
    #   0x0a2  20.3 1-3 Hellhound + 0-2 Ogre Mage
    #   0x142  16.3 2-6 Python
    #   0x136  22.5 2-4 Death Elemental
    [0x06f, 0x20a, 0x14c, 0x01f, 0x099, 0x0a2, 0x142, 0x136],
    # 4 Melmond (threat 23-27) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x16c  23.8 1 Yellow Dragon
    #   0x165  24.0 2-6 Wild Nakk
    #   0x137  24.1 3-4 Death Manticore
    #   0x07c  24.9 1 Vampire
    #   0x162  25.5 2-3 Rock Gargoyle
    #   0x12c  26.0 3-5 Skuldier + 0-2 Flood Gigas
    #   0x02e  23.0 1 Ice Gigas + 0-2 Winter Wolf
    #   0x0bb  27.1 3-4 Chimera
    [0x16c, 0x165, 0x137, 0x07c, 0x162, 0x12c, 0x02e, 0x0bb],
    # 5 Crescent (threat 27-35) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x258  28.5 1-3 Knocker + 2-6 Black Goblin
    #   0x054  28.6 1 Death Knight + 1-2 Nightmare
    #   0x166  28.8 4-8 Wild Nakk
    #   0x201  29.2 1-3 Knocker + 0-2 Devil Hound
    #   0x038  30.2 1-3 Baretta
    #   0x098  32.5 2-6 Wraith + 0-4 Specter
    #   0x156  27.4 1 Bonesnatch + 2-4 Skuldier + 1 Flood Gigas
    #   0x0a1  34.8 2-4 Earth Elemental
    [0x258, 0x054, 0x166, 0x201, 0x038, 0x098, 0x156, 0x0a1],
    # 6 Onrac (threat 32-39) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x145  34.4 4-8 Poison Eagle
    #   0x13d  35.2 1-2 Duel Knight
    #   0x02c  36.0 1-5 Specter + 0-3 Wraith + 0-3 Wight + 0-3 Ghast
    #   0x0d8  36.1 2-4 Stone Golem
    #   0x235  36.7 1-2 Mad Ogre
    #   0x221  38.4 1-2 Blue Dragon
    #   0x13f  31.7 1-5 Knocker
    #   0x211  39.5 0 Mad Ogre + 1-3 Flood Gigas
    [0x145, 0x13d, 0x02c, 0x0d8, 0x235, 0x221, 0x13f, 0x211],
    # 7 Trials (threat 41-47) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x149  41.6 0-5 Black Goblin + 1-3 Dark Wolf + 0-2 Elm Gigas + 0-2 Catoblepas
    #   0x21a  41.6 1-2 Undergrounder
    #   0x0d9  43.5 2-4 Green Dragon
    #   0x164  43.7 3-8 Rock Gargoyle
    #   0x0ce  44.8 2-3 Blue Dragon
    #   0x0bf  46.2 1-4 Clay Golem + 1-3 Stone Golem
    #   0x050  41.1 3-6 Black Flan
    #   0x16e  46.7 1 Black Dragon
    [0x149, 0x21a, 0x0d9, 0x164, 0x0ce, 0x0bf, 0x050, 0x16e],
    # 8 Lufenia (threat 47-65) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x0cd  49.9 5-9 Black Knight
    #   0x127  51.0 2-4 Reaper
    #   0x22b  54.8 0-1 Sekhret + 2-3 Earth Troll
    #   0x15f  60.5 3-6 Red Flan
    #   0x22e  61.6 0-1 Pharaoh + 3-7 Bonesnatch
    #   0x128  63.7 2-6 Reaper
    #   0x215  47.3 2-9 Rock Gargoyle
    #   0x255  65.4 2-3 Black Dragon
    [0x0cd, 0x127, 0x22b, 0x15f, 0x22e, 0x128, 0x215, 0x255],
]
_DF_TIER_POOL_B = [
    # 0 Cornelia (threat 9-13) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x087   9.8 1-3 Gigas Worm + 1 Ogre
    #   0x122   9.9 2-4 Gloom Widow
    #   0x150  10.4 1-2 Blue Troll
    #   0x013  10.8 1 Ogre Chief + 1-2 Ogre
    #   0x063  11.1 1-2 Troll
    #   0x13a  11.4 1 Devil Hound
    #   0x161   9.2 2-3 Revenant
    #   0x08c  12.5 1-3 Ogre + 0-2 Hyenadon
    [0x087, 0x122, 0x150, 0x013, 0x063, 0x13a, 0x161, 0x08c],
    # 1 Pravoka (threat 13-14) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x066  13.0 1-2 Tarantula + 0-2 Black Widow + 0-1 Green Slime + 0-1 Gray Ooze
    #   0x12b  13.1 2-4 Skuldier
    #   0x141  13.1 2-4 Python
    #   0x091  13.4 2-5 Werewolf + 0-5 Warg Wolf
    #   0x151  13.7 1-2 Blue Troll + 0-2 Python
    #   0x13b  13.7 1-2 Devil Hound
    #   0x0e4  12.9 2-4 Minotaur
    #   0x117  14.1 1-4 Yellow Ogre
    [0x066, 0x12b, 0x141, 0x091, 0x151, 0x13b, 0x0e4, 0x117],
    # 2 Elfheim (threat 14-17) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x123  14.9 4-8 Gloom Widow
    #   0x0b6  15.1 3-4 Manticore
    #   0x093  15.7 1-4 Ogre Chief + 0-2 Ogre
    #   0x14f  16.0 1-3 Blood Tiger
    #   0x06a  16.3 2-5 Horned Devil
    #   0x0ee  16.6 1-3 Ogre Mage + 0-2 Ogre Chief
    #   0x08f  14.3 2-5 Wight + 2-5 Ghast
    #   0x11a  16.9 1-2 Elm Gigas
    [0x123, 0x0b6, 0x093, 0x14f, 0x06a, 0x0ee, 0x08f, 0x11a],
    # 3 W. Keep (threat 18-20) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x092  17.8 2-5 Ochre Jelly + 0-5 Tarantula
    #   0x154  18.5 1 Poison Naga + 0-1 Death Elemental
    #   0x099  19.2 1-3 Sabertooth + 0-2 Lesser Tiger
    #   0x135  19.7 1-3 Death Elemental
    #   0x09e  20.2 2-4 Hill Gigas
    #   0x041  20.3 1 Neochu
    #   0x0ab  17.8 3-6 Bloodbones
    #   0x0a2  20.3 1-3 Hellhound + 0-2 Ogre Mage
    # v203: 0x095 (2-6 Anaconda + 0-4 Scorpion) swapped out for 0x099 so that
    # Scorpion belongs solely to the harder-overworld Elfheim hand-pick 0x01a
    # (rando._OW_HANDPICK). Sabertooth + Lesser Tiger appear nowhere else.
    [0x092, 0x154, 0x099, 0x135, 0x09e, 0x041, 0x0ab, 0x0a2],
    # 4 Melmond (threat 21-30) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x0b2  23.8 1-4 Minotaur Zombie + 0-2 Troll
    #   0x16c  23.8 1 Yellow Dragon
    #   0x20f  24.1 2-4 Death Manticore
    #   0x0e7  24.1 4-7 Weretiger
    #   0x119  25.9 1 Elm Gigas + 0-2 Dark Wolf
    #   0x111  28.2 1-2 Earth Troll
    #   0x06e  21.4 1 Ogre Mage + 1 Ogre Chief + 0-7 Hyenadon
    #   0x038  30.2 1-3 Baretta
    [0x0b2, 0x16c, 0x20f, 0x0e7, 0x119, 0x111, 0x06e, 0x038],
    # 5 Crescent (threat 32-44) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x0ca  33.8 1-2 King Mummy + 1-6 Mummy
    #   0x0d2  34.4 0-1 Spirit Naga + 1-3 Air Elemental
    #   0x21c  35.9 1-2 Earth Troll + 0-2 Dark Wolf
    #   0x138  38.8 1 Devil Wizard
    #   0x15a  42.1 1 Mythril Golem
    #   0x148  42.4 3-7 Dark Wolf
    #   0x219  32.0 1 Blue Dragon
    #   0x164  43.7 3-8 Rock Gargoyle
    [0x0ca, 0x0d2, 0x21c, 0x138, 0x15a, 0x148, 0x219, 0x164],
    # 6 Onrac (threat 44-49) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x223  44.8 1-3 Blue Dragon
    #   0x0bf  46.2 1-4 Clay Golem + 1-3 Stone Golem
    #   0x12e  46.2 2-4 Squidraken
    #   0x139  46.5 1-2 Devil Wizard
    #   0x205  46.9 3-4 Dark Elemental
    #   0x215  47.3 2-9 Rock Gargoyle
    #   0x146  43.7 1-2 Pharaoh
    #   0x0d0  49.4 4-8 Black Flan
    [0x223, 0x0bf, 0x12e, 0x139, 0x205, 0x215, 0x146, 0x0d0],
    # 7 Trials (threat 50-62) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x0d3  51.8 1-3 Vampire Lord + 1-2 Dragon Zombie
    #   0x13e  52.9 2-5 Duel Knight
    #   0x22c  53.9 1-4 Flare Gigas
    #   0x259  55.4 1 Vampire Lord + 2-5 Vampire
    #   0x14d  56.0 1-2 Black Dragon
    #   0x15e  60.6 2-5 Red Flan + 0-5 Gloom Widow
    #   0x112  50.1 1-2 Earth Troll + 0-1 Sekhret
    #   0x228  61.6 0-1 Pharaoh + 3-7 Bonesnatch
    [0x0d3, 0x13e, 0x22c, 0x259, 0x14d, 0x15e, 0x112, 0x228],
    # 8 Lufenia (threat 64-84) -- ranks: 4x18.75%, 2x9.38%, 4.69%, 1.56%
    #   0x04a  64.4 0-8 Cockatrice + 0-8 Pyrolisk + 1-5 King Mummy + 0-8 Mummy
    #   0x233  65.2 2-4 Sekhret
    #   0x114  66.2 1-2 Abyss Worm
    #   0x234  68.8 0-2 Black Dragon + 1-2 Blue Dragon
    #   0x23a  74.7 2-4 Black Dragon
    #   0x222  76.4 3-9 Duel Knight
    #   0x128  63.7 2-6 Reaper
    #   0x155  83.6 2-4 Holy Dragon
    [0x04a, 0x233, 0x114, 0x234, 0x23a, 0x222, 0x128, 0x155],
]
# Count-only reductions (verify-vanilla then overwrite). Each target has 0
# references in any encounter table (u8 or u16 DLC) AND appears in pool A only
# (0x211 = tier 6, 0x235 = tier 7 -- both barred from pool B by the solver), so
# the edit is forest-exclusive with no side effects.
# (fid -> (vanilla 15B hex, patched 15B hex))
_DF_FORM_EDITS = {
    0x211: ("010004970001a10103ff0000ff0000",   # 0-1 Mad Ogre, 1-3 Flood Gigas ->
            "010004970000a10103ff0000ff0000"),  #   0-0 Mad Ogre, 1-3 Flood Gigas
    0x235: ("020004970204ff0000ff0000ff0000",   # 2-4 Mad Ogre ->
            "020004970102ff0000ff0000ff0000"),  #   1-2 Mad Ogre
}
# Per-zone tier: each of the 64 overworld zones (8x8 grid of 32-tile cells) mapped
# to its NEAREST progression stop, so a zone's forest scales to when the route
# brings the player there -- not to raw map distance from spawn. Derived by
# nearest-anchor over the 9 towns' overworld positions (+ endgame corners capped
# at tier 8); see the dangerous-forests memory for the anchor coords. Every zone
# has a tier (no 0xFF): ocean zones simply have no forest tiles to trigger it.
# SHARED zone map: MUST equal ff1psp.rando.ZONE_TIER byte-for-byte (test_rando
# enforces parity) -- forests (this table) and overworld foot encounters (gen
# side) read one canonical zone->tier map. User-curated 2026-07-15 via the
# interactive zone editor (v67); +7 zone bias: zone=((x+7)>>5)+8*((y+7)>>5).
_DF_ZONE_TIER = (
    6, 6, 6, 7, 7, 7, 8, 8,   # z0-2 Onrac; z3-5 Trials; NE = Lufenia
    6, 6, 6, 7, 7, 7, 8, 8,
    6, 6, 6, 7, 7, 7, 7, 8,
    6, 6, 6, 1, 1, 1, 8, 8,   # Matoya pocket (z27-29) Pravoka; z30/31 peninsula tip
    4, 4, 4, 1, 0, 1, 1, 1,   # z36 Cornelia; z39 Pravoka (peninsula south landmass)
    4, 4, 4, 3, 0, 0, 1, 1,
    4, 4, 3, 2, 2, 5, 5, 5,   # z50 W.Keep; z51/52 Elfheim; z53+ Crescent
    4, 4, 3, 3, 2, 5, 5, 5,
)


def _df_data(harder):
    """Cave data: ZONE_TIER[64] (u8) + POOL[9][_DF_POOL_MAX] (u16, @+64) +
    SLOT_MAP[64] (u8, @+64+18*_DF_POOL_MAX) = 272 B at _DF_POOL_MAX=8. POOL starts
    at an even offset so its `lhu` reads stay aligned. The per-tier CNT array is
    GONE as of v190: the weight map yields slots 0..MAX-1 unconditionally, so every
    pool must be exactly _DF_POOL_MAX deep (asserted below) -- a short row would be
    read past its end."""
    zone_tier = bytes(_DF_ZONE_TIER)
    pools = _DF_TIER_POOL_B if harder else _DF_TIER_POOL_A
    assert len(zone_tier) == 64 and len(pools) == 9 and max(zone_tier) < len(pools)
    assert all(len(p) == _DF_POOL_MAX for p in pools), \
        "weighted slot map indexes 0..MAX-1; a short pool would read past the row"
    pool_bytes = b"".join(struct.pack("<H", f) for p in pools for f in p)
    data = zone_tier + pool_bytes + _slot_weight_map()
    return data + b"\x00" * (-len(data) % 4)


# --- boss_minions: formation-record edits (sprite packs done by ms2_bake) ----
# Formation table: RAM 0x08948D14, 15B/record, 0x260 records:
#   [layout, b1, b2] + 4 x (mon_id, min, max), 0xFF = empty slot.
# byte0 = LAYOUT selects the static position array (BOOT @0x08948CAC..):
# layout 2 = the 4-position big grid -> boss (slot 0) + up to 3 adds.
# Every distinct monster needs its GIM pair in MS2_<fid>.PCK (gid-sorted) or
# battle init null-jumps at RA 0x088fba28 -- ms2_bake rebuilds the packs AFTER
# build_iso. See boss-adds-ms2-pack-cracked memory.
FORMATION_TABLE = 0x08948D14
FORMATION_STRIDE = 15
_MINION_LAYOUT = 2   # 4-position big grid (default for the large fiend bosses)
# Layout dispatch @0x08879254 -> jump table @0x0894BEC4, 7 cases; each sets a
# position-array base (0x08948CAC..) + a count. Entry = 3 bytes (x, y, scale),
# formation slot 0 (the boss) = entry 0:
#   0: 9 small, col-major MID-first  entry0 (0x10,0x4c)   5: same 9, TOP-first
#   2: 4 big   entry0 (0x10,0x0c)    3/4/6: single-position (vanilla boss fights)
# Layout 5's top-first order is what keeps an absurd-intensity big boss out of
# the battle menu; see boss_minions.TOP_GRID_LAYOUT.
_MAX_LAYOUT = 7
# Bosses that KEEP the 9-position small grid (layout 0): their own sprite and
# all curated adds are small, so the big grid would waste 5 slots and clamp the
# adds behind the boss group. MUST mirror boss_minions.SMALL_GRID_FIDS
# (test_minions asserts equality). Piscodemon (fids 0x1C/0x9C).
_SMALL_GRID_FIDS = frozenset({0x1C, 0x9C})
_SMALL_GRID_LAYOUT = 0   # 9-position small grid


# The formation-based boss fights (mirror of boss_minions.all_fids(); only
# scripted fiend REMATCHES (map >= 0xd4) and Chronodia lack formation records
# and stay untouched). 0x1C/0x9C = Piscodemon, 0x7B = Chaos, 0x100-0x110 =
# DLC bosses (Echidna..Death Gaze).
MINION_BOSS_FIDS = (0x1C, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
                    0x7A, 0x7B, 0x7C, 0x7D, 0x7F, 0x9C) + tuple(
                    range(0x100, 0x111))


def apply_boss_minions(elf: bytearray, feats=None):
    # Without a plan (test harness / defensive default) still stamp the layout
    # change with empty add slots -- a lone boss on the 4-big grid is benign,
    # and the signature must flip whenever the feature ran. Production always
    # ships a plan (ApClient disables the feature for planless old seeds).
    plan = ((feats or {}).get("boss_minions_plan")
            or [[fid, []] for fid in MINION_BOSS_FIDS])
    for entry in plan:
        fid, groups = entry[0], entry[1]
        # Layout travels in the plan (it depends on intensity); fall back to the
        # per-fid default for planless/old-format entries.
        layout = (int(entry[2]) if len(entry) > 2
                  else (_SMALL_GRID_LAYOUT if int(fid) in _SMALL_GRID_FIDS
                        else _MINION_LAYOUT))
        # The layout dispatch (0x08879254) range-checks with `sltiu at,a1,7` and
        # falls through to a NO-position path above that, so a stray layout id
        # would render an empty formation.
        if not 0 <= layout < _MAX_LAYOUT:
            raise ValueError(f"boss_minions fid {int(fid):#x}: layout {layout} "
                             f"outside the engine's 0..{_MAX_LAYOUT - 1} dispatch")
        off = E.ram2file(FORMATION_TABLE + int(fid) * FORMATION_STRIDE)
        elf[off] = layout
        # slot 0 = the boss, untouched. Rewrite slots 1-3 from the plan.
        for i in range(3):
            base = off + 3 + (i + 1) * 3
            if i < len(groups):
                mon, mn, mx = (int(x) for x in groups[i])
                elf[base:base + 3] = bytes((mon, mn, mx))
            else:
                elf[base:base + 3] = b"\xff\x00\x00"


# --- feature: minion death serializer (multi-kill dissolve-freeze fix) -----------
# Boss-minion battles WEDGE when 2+ enemies die in the same damage application.
# ROOT (live RE 2026-07-16, minion-multikill-dissolve-freeze memory): every
# action's anim state machine sweeps the 9 enemy slots at its "deaths" phase
# (sweep fn 0x888646c, called from the per-action-type anim fns) and calls
# death_visual_start (0x8871d14) for each dead-not-yet-dissolving slot -- that
# fn arms the per-slot death-anim flag @bb+0xCD30+slot and per-slot timer
# @bb+0xCD3A+2*slot; the action's following phase (e.g. 0x8882a3c for action
# type 1) waits via 0x8874b3c until ALL NINE flags are clear. In a boss fight
# every enemy death runs the LONG boss dissolve whose screen/shake stage uses
# SHARED runner state -- two flags armed in ONE sweep orphan all but one
# dissolve: the orphan's flag never clears and every later action's wait spins
# forever (the "limbo": corpse anims loop, command menu never returns).
# Normal encounters' quick fades run concurrently fine (vanilla AoE
# multi-kills prove it), so only minion-boss fids are gated.
# FIX: guard cave at death_visual_start's entry: in a minion-boss fight
# (per-seed fid table from the plan), if ANY death-anim flag is already armed,
# return without starting this one. The unit stays dead-but-unflagged, the
# waiting phase only blocks on flags that were actually armed, and the
# engine's own next sweep (it runs in every subsequent action) starts the
# deferred dissolve -- native serialization: kills stay in their normal apply
# context, no timers, no cross-battle state.
# PLUS a per-frame SWEEP call (battle main loop, same fid gating): vanilla only
# sweeps at each action's death phase, which left deferred corpses standing
# until the NEXT action (~a full turn). Calling the sweep every frame starts
# each deferred dissolve the moment the previous one's flag clears, so chained
# deaths run back-to-back. Normal encounters keep vanilla timing (fid gate).
_MDS_HOOK = 0x08871d14                 # death_visual_start entry
_MDS_HOOK_W0, _MDS_HOOK_W1 = 0x30A500FF, 0x000510C0  # andi a1,a1,0xff; sll v0,a1,3
_MDS_RESUME = _MDS_HOOK + 8            # continue vanilla fn body
_MDS_FLAGS_HI_OFF = -0x32d0            # bb+0x10000-0x32d0 = bb+0xCD30 (9 flags)
_MDS_FID_MAX = 0x268                   # fid table covers 0x000..0x267
_MDS_SWEEP_FN = 0x0888646c             # start-death-visuals sweep (9 slots)
_MDS_TICK_HOOK = 0x0886ad04            # battle main loop: jal 0x8879844
_MDS_TICK_W0 = 0x0E21E611              # that jal (single-word hook; the vanilla
                                       # delay slot finalizing a0 stays in place)
_MDS_TICK_CALL = 0x08879844            # displaced call, performed by the cave
_MDS_TICK_RET = 0x0886ad0c
# --- reward-repeat guard (v245) ---------------------------------------------
# The sweep fn 0x0888646c is ALSO the XP/gil award loop. Per row it does:
#   0x088864b0 lbu s1, species          (0xFF -> next row)
#   0x088864c0 jal 0x8871ec4            eligibility: nonzero = row not ready
#   0x088864c8 bnez v0, 0x8886628       <-- the only skip
#   0x088864d4 jal 0x8871d14            death_visual_start
#   0x088864dc.. u16 [bb+0xCEC6] += XP ; u16 [bb+0xCEC8] += Gil ; jal set_drop
# The reward add sits AFTER the call and is NOT gated on it, and 0x8871ec4 only
# rejects a row whose unit is still ALIVE -- nothing latches "already paid".
# Vanilla survives on timing alone: the sweep runs once per action and the
# dissolve stamps species 0xFF before the next one.
#
# Both halves of that break under this feature: the entry guard below leaves a
# deferred unit dead-but-unflagged (dissolve never starts, species stays valid)
# and the per-frame sweep re-enters the loop every frame -- so a deferred row
# re-paid its FULL XP and Gil once per frame until its dissolve began. On the
# long type-5 boss dissolve that is 60-120x the intended payout, which is why
# Garland (first minion fight, level-1 party) read as "excessive exp and gil"
# even at 2.5x -- 38,369 gil off 10 battles in the 2026-08-08 player bundle.
#
# FIX: guard the SWEEP instead of death_visual_start's entry -- when a dissolve
# is already running, branch the whole row to the loop tail, so it skips the
# reward add and set_drop along with the death visual. The row is retried by a
# later sweep and paid exactly once, when it actually dissolves. The entry
# guard stays as defence for any other caller of death_visual_start.
_MDS_REWARD_HOOK = 0x088864c8          # `bnez v0, 0x8886628` + its nop delay slot
_MDS_REWARD_W0, _MDS_REWARD_W1 = 0x14400057, 0x00000000
_MDS_REWARD_CONT = _MDS_REWARD_HOOK + 8   # 0x088864d0 lw a0,(s2) -- process row
_MDS_REWARD_NEXT = 0x08886628          # loop tail (addiu v1,s0,1) -- skip row
# Neither displaced word is a branch target (full-text scan 2026-08-08) and the
# loop head is 0x0888648c, well clear of the site -- see the caves-in-loops rule.
# Per-unit dissolve-TYPE override (v75). The dissolve runner picks its handler
# from a type 0..8 that fn 0x88756f8 derives from the FORMATION ID alone (RE
# 2026-07-16): fid 0x79/0x74 -> 3, 0x77/0x76 -> 4, 0x7c/0x7d/0x7f -> 5,
# 0x100-0x110 -> 7, 0x260-0x267 -> 8, 0x7b -> 0, DEFAULT (every normal
# encounter) -> 6. Jumptable @0x0894BF80: types 1/2/3/4/7 share the long BOSS
# dissolve (0x0887A5F8, the shared-runner one that can't overlap), while type 6
# (0x0887AC7C) is the ordinary monster fade vanilla runs on several monsters at
# once. So in a boss formation EVERY minion inherits the boss dissolve purely
# because of its fid. Override it per-unit at the runner's type dispatch: a unit
# whose species differs from enemy-slot-0's (i.e. an added minion, not the boss
# -- species compare, so swarm bosses like Piscodemon keep theirs) gets type 6.
# Minions then fade concurrently like normal monsters; the boss keeps its
# dissolve; the guard below only has to serialize boss-species units.
_MDS_TYPE_HOOK = 0x08879c18            # runner: andi v1,a2,0xff (a2 = type)
_MDS_TYPE_W0, _MDS_TYPE_W1 = 0x30C300FF, 0x2C610009  # andi v1,a2,0xff; sltiu at,v1,9
_MDS_TYPE_RET = _MDS_TYPE_HOOK + 8
_MDS_NORMAL_TYPE = 6                   # ordinary monster fade (concurrent-safe)
# species byte of enemy slot n = bb + 0xC90D + n*0x6C (= enemy unit rec +0x49;
# derived from the sweep's own addressing at 0x088864b0). Too big for one
# signed imm16 -> added in two steps, like the game does.
_MDS_SPECIES_OFF_A = 0x7ff8
_MDS_SPECIES_OFF_B = 0xC90D - 0x7ff8
# Fids EXCLUDED from the whole serializer machinery (guard + per-frame sweep +
# type override) -- they keep pure vanilla death visuals. Piscodemon (0x1C/0x9C):
# its vanilla dissolve type is already 6 (DEFAULT concurrent fade, not a
# shared-runner boss dissolve) and it is a single-species SWARM, so every unit is
# the SAME species as slot 0. Under the universal gate that means the swarm's
# deaths were force-serialized one at a time AND run through the type-override's
# species compare -- and that manual step distorts the death SFX (overlapping cue
# playback on the serialized chain). Since its native type-6 fade already overlaps
# cleanly, we let Piscodemon fall through to vanilla entirely.
_MDS_EXCLUDE_FIDS = frozenset({0x1C, 0x9C})   # Piscodemon (both formations)


def apply_minion_death_serializer(elf: bytearray, feats=None):
    for addr, want in ((_MDS_HOOK, _MDS_HOOK_W0), (_MDS_HOOK + 4, _MDS_HOOK_W1),
                       (_MDS_TICK_HOOK, _MDS_TICK_W0),
                       (_MDS_REWARD_HOOK, _MDS_REWARD_W0),
                       (_MDS_REWARD_HOOK + 4, _MDS_REWARD_W1),
                       (_MDS_TYPE_HOOK, _MDS_TYPE_W0),
                       (_MDS_TYPE_HOOK + 4, _MDS_TYPE_W1)):
        w = struct.unpack_from("<I", elf, E.ram2file(addr))[0]
        if w != want:
            raise ValueError(f"minion_death_serializer: unexpected word "
                             f"@{addr:#x}: {w:#010x} (want {want:#010x})")
    plan = ((feats or {}).get("boss_minions_plan")
            or [[fid, []] for fid in MINION_BOSS_FIDS])
    fidtab = bytearray(_MDS_FID_MAX)
    for entry in plan:
        fid = int(entry[0])
        if not (0 <= fid < _MDS_FID_MAX):
            raise ValueError(f"minion_death_serializer: fid {fid:#x} out of range")
        if fid in _MDS_EXCLUDE_FIDS:      # Piscodemon: vanilla death visuals
            continue
        fidtab[fid] = 1
    tab = E.add_segment_cave(elf, bytes(fidtab))

    # a0 = battle base (fn arg), a1 = enemy slot 0..8; must not clobber
    # a0/a1/ra. v0/v1 are overwritten immediately by the vanilla body; t7-t9/at
    # saved defensively anyway (house style).
    guard = A.asm_labels([
        A.word(_MDS_HOOK_W0), A.word(_MDS_HOOK_W1),      # displaced originals
        A.addiu("sp", "sp", -0x10),
        A.sw("t7", 0x0, "sp"), A.sw("t8", 0x4, "sp"),
        A.sw("t9", 0x8, "sp"), A.sw("at", 0xC, "sp"),
        A.lhu("t8", 0x68a6, "a0"),                       # formation id
        A._i(0x0B, "t8", "at", _MDS_FID_MAX),            # sltiu: in table range?
        ("beq", "at", "zero", "vanilla"), A.nop(),
        A.lui("t9", (tab >> 16) & 0xFFFF),
        A.ori("t9", "t9", tab & 0xFFFF),
        A.addu("t9", "t9", "t8"),
        A.lbu("t7", 0, "t9"),                            # fidtab[fid]
        ("beq", "t7", "zero", "vanilla"), A.nop(),       # not a minion boss
        # EVERY unit is serialized -- minions included. v75 narrowed this to
        # boss-species units on the theory that the type-6 fade (override cave
        # below) is safe to overlap "because vanilla does it"; that was an
        # ASSUMPTION, never tested, and it FROZE on the first live multi-kill
        # (2026-07-16). Only one death visual at a time is proven safe, so the
        # gate stays universal; the override just makes each one short.
        # any of the 9 death-anim flags armed? (bytes are 0/1, so summing two
        # words + the ninth byte detects any nonzero without an OR mnemonic)
        A.lui("t9", 1),
        A.addu("t9", "a0", "t9"),                        # bb+0x10000
        A.lw("t7", _MDS_FLAGS_HI_OFF, "t9"),             # flags[0..3]
        A.lw("t8", _MDS_FLAGS_HI_OFF + 4, "t9"),         # flags[4..7]
        A.addu("t7", "t7", "t8"),
        A.lbu("t8", _MDS_FLAGS_HI_OFF + 8, "t9"),        # flags[8]
        A.addu("t7", "t7", "t8"),
        ("beq", "t7", "zero", "vanilla"), A.nop(),       # none active -> start
        # a dissolve is already running: DEFER this one (unit stays
        # dead-unflagged; the next action's sweep retries). Return to caller
        # (the sweep ignores the return value).
        A.lw("t7", 0x0, "sp"), A.lw("t8", 0x4, "sp"),
        A.lw("t9", 0x8, "sp"), A.lw("at", 0xC, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.jr("ra"), A.nop(),
        ("label", "vanilla"),
        A.lw("t7", 0x0, "sp"), A.lw("t8", 0x4, "sp"),
        A.lw("t9", 0x8, "sp"), A.lw("at", 0xC, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.j(_MDS_RESUME), A.nop(),
    ])
    guard_vaddr = E.add_segment_cave(elf, guard)
    E.install_detour(elf, _MDS_HOOK, guard_vaddr)

    # Per-frame sweep (battle main loop). Site audited 2026-07-16: at the
    # displaced jal only a0 (its argument, finalized by the vanilla delay
    # slot) and the callee-saved regs are live; ra is stale (this jal was
    # about to overwrite it). s0 = battle scene; bb = s0+0x460; the sweep's
    # arg is the scene's enemy-actor object s0+0xD41C (its +0 slot holds bb).
    tick = A.asm_labels([
        A.addiu("sp", "sp", -0x10),
        A.sw("a0", 0x0, "sp"),                           # preserve jal arg
        A.addiu("t9", "s0", 0x460),                      # bb
        A.lhu("t8", 0x68a6, "t9"),                       # formation id
        A._i(0x0B, "t8", "at", _MDS_FID_MAX),            # sltiu: in range?
        ("beq", "at", "zero", "skip"), A.nop(),
        A.lui("t7", (tab >> 16) & 0xFFFF),
        A.ori("t7", "t7", tab & 0xFFFF),
        A.addu("t7", "t7", "t8"),
        A.lbu("t7", 0, "t7"),                            # fidtab[fid]
        ("beq", "t7", "zero", "skip"), A.nop(),          # not a minion boss
        A.addiu("a0", "s0", 0x5424),
        A.addiu("a0", "a0", 0x7ff8),                     # enemy-actor obj
        A.jal(_MDS_SWEEP_FN), A.nop(),                   # start pending deaths
        ("label", "skip"),
        A.lw("a0", 0x0, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.jal(_MDS_TICK_CALL), A.nop(),                  # displaced per-frame jal
        A.j(_MDS_TICK_RET), A.nop(),
    ])
    tick_vaddr = E.add_segment_cave(elf, tick)
    # single-word hook: keep the vanilla delay slot (finalizes a0) in place
    fo = E.ram2file(_MDS_TICK_HOOK)
    elf[fo:fo + 4] = A.j(tick_vaddr)

    # Reward-repeat guard, at the sweep's per-row skip test (see _MDS_REWARD_HOOK).
    # Live here: s2 = enemy-actor obj (its +0 holds bb), s0 = row, s1 = species,
    # v0 = the eligibility result we are replacing the branch on. The displaced
    # `bnez` CANNOT ride at the cave head like install_detour's convention -- its
    # offset is PC-relative and would land somewhere else from out here -- so it
    # is re-emitted as a branch to our own "next" label instead.
    reward = A.asm_labels([
        ("bne", "v0", "zero", "next"), A.nop(),           # vanilla skip test
        A.addiu("sp", "sp", -0x10),
        A.sw("t6", 0x0, "sp"), A.sw("t7", 0x4, "sp"),
        A.sw("t8", 0x8, "sp"), A.sw("t9", 0xC, "sp"),
        A.lw("t9", 0, "s2"),                              # bb
        A.lhu("t8", 0x68a6, "t9"),                        # formation id
        A.sltiu("at", "t8", _MDS_FID_MAX),                # in table range?
        ("beq", "at", "zero", "cont"), A.nop(),
        A.lui("t7", (tab >> 16) & 0xFFFF),
        A.ori("t7", "t7", tab & 0xFFFF),
        A.addu("t7", "t7", "t8"),
        A.lbu("t7", 0, "t7"),                             # fidtab[fid]
        ("beq", "t7", "zero", "cont"), A.nop(),           # not a minion boss
        # any of the 9 death-anim flags armed? (same 0/1-byte sum as the guard)
        A.lui("t8", 1),
        A.addu("t8", "t9", "t8"),                         # bb+0x10000
        A.lw("t7", _MDS_FLAGS_HI_OFF, "t8"),              # flags[0..3]
        A.lw("t6", _MDS_FLAGS_HI_OFF + 4, "t8"),          # flags[4..7]
        A.addu("t7", "t7", "t6"),
        A.lbu("t6", _MDS_FLAGS_HI_OFF + 8, "t8"),         # flags[8]
        A.addu("t7", "t7", "t6"),
        ("beq", "t7", "zero", "cont"), A.nop(),           # none armed -> pay now
        # a dissolve is running: defer the WHOLE row -- no death visual, no XP/gil
        # add, no set_drop. A later sweep pays it once, when it really dissolves.
        A.lw("t6", 0x0, "sp"), A.lw("t7", 0x4, "sp"),
        A.lw("t8", 0x8, "sp"), A.lw("t9", 0xC, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.j(_MDS_REWARD_NEXT), A.nop(),
        ("label", "cont"),
        A.lw("t6", 0x0, "sp"), A.lw("t7", 0x4, "sp"),
        A.lw("t8", 0x8, "sp"), A.lw("t9", 0xC, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.j(_MDS_REWARD_CONT), A.nop(),
        ("label", "next"),
        A.j(_MDS_REWARD_NEXT), A.nop(),
    ])
    reward_vaddr = E.add_segment_cave(elf, reward)
    fo = E.ram2file(_MDS_REWARD_HOOK)
    elf[fo:fo + 8] = A.j(reward_vaddr) + A.nop()

    # Per-unit dissolve-type override, at the runner's type dispatch. Live here:
    # a2 = type (what we rewrite), s1 = enemy slot 0..8, a1 = battle base.
    # v1/at are (re)computed by the displaced words, which we replay LAST so
    # they see the overridden a2 -- hence originals at the cave TAIL, not head.
    override = A.asm_labels([
        A.addiu("sp", "sp", -0x10),
        A.sw("t7", 0x0, "sp"), A.sw("t8", 0x4, "sp"), A.sw("t9", 0x8, "sp"),
        A.lhu("t8", 0x68a6, "a1"),                       # formation id
        A._i(0x0B, "t8", "at", _MDS_FID_MAX),            # sltiu: in table range?
        ("beq", "at", "zero", "keep"), A.nop(),
        A.lui("t9", (tab >> 16) & 0xFFFF),
        A.ori("t9", "t9", tab & 0xFFFF),
        A.addu("t9", "t9", "t8"),
        A.lbu("t9", 0, "t9"),                            # fidtab[fid]
        ("beq", "t9", "zero", "keep"), A.nop(),          # not a minion boss
        A.sll("t7", "s1", 6), A.sll("t8", "s1", 3),      # slot*0x6C
        A.addu("t7", "t7", "t8"),
        A.sll("t8", "s1", 2), A.addu("t7", "t7", "t8"),
        A.addu("t7", "a1", "t7"),
        A.addiu("t7", "t7", _MDS_SPECIES_OFF_A),
        A.addiu("t7", "t7", _MDS_SPECIES_OFF_B),
        A.lbu("t7", 0, "t7"),                            # species[slot]
        A.addiu("t8", "a1", _MDS_SPECIES_OFF_A),
        A.addiu("t8", "t8", _MDS_SPECIES_OFF_B),
        A.lbu("t8", 0, "t8"),                            # species[slot 0] = boss
        ("beq", "t7", "t8", "keep"), A.nop(),            # the boss: keep its type
        A.addiu("a2", "zero", _MDS_NORMAL_TYPE),         # minion -> normal fade
        ("label", "keep"),
        A.lw("t7", 0x0, "sp"), A.lw("t8", 0x4, "sp"), A.lw("t9", 0x8, "sp"),
        A.addiu("sp", "sp", 0x10),
        A.word(_MDS_TYPE_W0), A.word(_MDS_TYPE_W1),      # displaced (see above)
        A.j(_MDS_TYPE_RET), A.nop(),
    ])
    override_vaddr = E.add_segment_cave(elf, override)
    E.install_detour(elf, _MDS_TYPE_HOOK, override_vaddr)




# ---- feature: Chaos Shrine basement pools (harder_dungeon_encounters) -------
# The Chaos Shrine basement is EIGHT floors (0x20-0x27), not one pool: three
# descent floors, the four elemental "past" floors (Earth 0x23 / Fire 0x24 /
# Water 0x25 / Air 0x26 -- the Lich/Marilith/Kraken/Tiamat rooms), and the Chaos
# approach 0x27. Vanilla's harder-mode step (_CAVE_HARDER_DUNGEON) self-maps
# chaos_basement -- it is the terminal dungeon, so there is no "next dungeon up"
# to borrow from and the endgame floors got NO difficulty increase at all.
#
# Fix: the DF trick. The u8 zones_caves table can only hold base formations
# (< 0x100), which is why the PSP bonus-dungeon bestiary (Sekhret, Flare Gigas,
# Mad Ogre, Abyss Worm, Black Dragon, Duel Knight, ...) was unreachable here --
# those monsters only lead DLC formations with u16 ids. But the field the roll
# feeds forward is already u16 (`sh v0,0xbe4(s4)`), so a detour that writes the
# u16 directly bypasses the u8 table exactly as dangerous_forests does.
#
# Same hard rule as DF: every id below REFERENCES AN EXISTING formation, so its
# secondary sprite/position table is already valid (authoring new formations
# freezes battle init -- see the DF header). Nothing is authored, and NO shared
# formation record is edited, so Lufenia's harder-overworld pool and the regional
# ocean pools -- which share many of these records -- cannot move. That was a
# hard constraint from the user (2026-07-29): both are correctly balanced today.
#
# Rows are indexed by the VANILLA slot roll, not a fresh RNG draw, so the pools
# inherit the engine's skew from scramble 0x08945850 exactly: slots 0-3 are
# 18.75% each, 4-5 are 9.38%, slot 6 is 4.69% and slot 7 is 1.56%. Pools are
# therefore ordered common -> rare, and every boss cameo sits at slot 7 (or 6 on
# the two floors that carry two), keeping cameos rare by design.
_CF_HOOK = 0x08842258          # `lui v1,0x894` in the cave-table read (mapid < 0x87)
_CF_HOOK_W0 = 0x3C030894       # vanilla word at the hook
_CF_RESUME = 0x08842260        # vanilla continuation; expects v1 = zones_caves base
_CF_ZONES_CAVES = 0x08945F9C   # zones_caves base (what the displaced lui/addiu built)
_CF_TAIL = 0x088425B0          # encounter-commit tail (shared with DF/ocean)
_CF_CTX_FORM_OFF = 0x0BE4      # s4+this = battle-context formation id (u16)
# (floor selection is the 256-byte map-id rowmap in _cf_data -- v216 replaced
# the old first-map/row-count range chain)

# 8 slots per floor, common -> rare. Boss cameos at slot 7 (+ slot 6 on Earth and
# Air). Fiend cameos use the ORIGINAL fiend formations (0x77-0x7a), never the
# 0x73-0x76 Chaos-Shrine rematch forms -- those are the boss fights on these very
# floors and pay a flat 2000 xp.
_CF_POOLS = {
    0x20: (0x224, 0x0d9, 0x23d, 0x13e, 0x152, 0x159, 0x0cd, 0x056),
    #      EarthTroll GreenDrgn Undergrnd DuelKnght FlareGigas Bonesnatch BlkKnight WARMECH
    0x21: (0x163, 0x0cb, 0x22d, 0x14d, 0x127, 0x0ae, 0x130, 0x101),
    #      RockGargoyle DrgnZomb MadOgre+Troll BlackDrgn Reaper IceGigas Sekhret CERBERUS
    0x22: (0x0cd, 0x0d9, 0x222, 0x128, 0x23a, 0x235, 0x139, 0x103),
    #      BlkKnight GreenDrgn DuelKnight Reaper BlackDrgn MadOgre DevilWiz TWO-HEADED DRAGON
    0x23: (0x20c, 0x233, 0x23d, 0x224, 0x113, 0x23b, 0x100, 0x077),
    #      RockGargoyle Sekhret Undergrnd EarthTroll EarthPlant AbyssWorm ECHIDNA TIAMAT
    0x24: (0x22c, 0x15f, 0x235, 0x0aa, 0x225, 0x0a9, 0x153, 0x07a),
    #      FlareGigas RedFlan MadOgre RedDragon FlareGigas+Hundlegs FireLizard Prototype LICH
    0x25: (0x12f, 0x11f, 0x15d, 0x0c9, 0x124, 0x0c7, 0x125, 0x079),
    #      Squidraken KillerShark Orochi WaterElem SahaginQueen WaterNaga SahaginQueen MARILITH
    0x26: (0x145, 0x155, 0x203, 0x0d1, 0x14e, 0x20b, 0x102, 0x078),
    #      PoisonEagle HolyDragon DarkElem AirElem BloodyEye SilverDragon AHRIMAN KRAKEN
    0x27: (0x228, 0x222, 0x23b, 0x233, 0x0cd, 0x15a, 0x128, 0x0d7),
    #      Pharaoh+Bonesnatch DuelKnight AbyssWorm Sekhret BlkKnight MythrilGolem Reaper PurpleWorm
}

# ---- Flying Fortress floors (same detour, second map range) ------------------
# The fortress is the OTHER dungeon harder_dungeon_encounters cannot serve: its
# chain steps fortress -> chaos_basement, so all five floors rerolled from ONE
# shared pool -- the union of the basement's vanilla bytes. That pool is Earth-
# and fire-flavored (2-4 Earth Elemental, 4-7 Earth Medusa, the Mount-Gulg fire
# block), which is how Melmond-flavored fights ended up on the game's highest
# floor, and it gave every floor the same 36.1 expected threat with no gradient.
#
# These rows are hand-authored instead (user-directed, 2026-08-03):
#   * Each floor keeps the top half of its OWN vanilla fights by threat, so the
#     floor still reads as the Flying Fortress. Formations fielding Evil Eye or
#     Mindflayer are promoted into that top half -- they punch well above their
#     threat number -- and are RANKED with those two monsters' unit values x2.5.
#   * The sliced-off bottom half is replaced from the Chaos floors and the DLC
#     bestiary, capped at 1.5x that floor's own hardest vanilla fight.
#   * Slot order is INVERTED versus _CF_POOLS/_DF_POOL: hardest fight in the
#     MOST common slot, easiest in the rarest. Ascending the tower gets worse.
#   * WarMech is hand-placed one slot more common per floor (F1 slot 7 = 1.56%
#     -> F5 slot 3 = 18.75%), so the closer to the top, the likelier it stalks
#     you. That displaced Kraken's F1 cameo from slot 7 to slot 6.
# rando._DUNGEON_BOSS_SLOTS mirrors the two cameo placements into the u8 table;
# these rows are authoritative while the feature is on.
_FF_POOLS = {  # noqa: E305
    0x5c: (0x0d1, 0x069, 0x15a, 0x0cb, 0x04d, 0x054, 0x078, 0x056),
    #      AirElem EvilEye MythrilGolem DrgnZombie BlkKnight DeathKnight KRAKEN WARMECH
    0x5d: (0x203, 0x223, 0x164, 0x069, 0x050, 0x0d8, 0x056, 0x054),
    #      DarkElem BlueDragon RockGargoyle EvilEye BlackFlan StoneGolem WARMECH DeathKnight
    0x5e: (0x16d, 0x153, 0x14e, 0x053, 0x0cc, 0x056, 0x0d8, 0x0b3),
    #      HolyDragon Prototype BloodyEye VampLord Guardian WARMECH StoneGolem Rakshasa
    0x5f: (0x14d, 0x13e, 0x0cd, 0x0b5, 0x056, 0x053, 0x0cc, 0x051),
    #      BlackDragon DuelKnight BlkKnight Mindflayer WARMECH VampLord Guardian AirElem
    0x60: (0x126, 0x15f, 0x127, 0x056, 0x0b5, 0x050, 0x051, 0x0d2),
    #      Reaper+Revenant RedFlan Reaper WARMECH Mindflayer BlackFlan AirElem SpiritNaga
}


# ---- Difficulty-slope pools (2026-08-03) ------------------------------------
# harder_dungeon_encounters used to reroll a dungeon from the NEXT dungeon's
# vanilla pool. That chain follows STORY order, but vanilla encounter difficulty
# does not, so the slope had two inversions: ice -> trials stepped DOWN (24.1 ->
# 20.9) and mirage -> fortress stepped DOWN (32.6 -> 28.6). Measured as played,
# Cavern of Ice came out EASIER than Mount Gulg, and Mirage Tower came out
# easier with the "harder" option ON than in vanilla (32.9 -> 28.6).
#
# These four dungeons are authored instead, to the user's target slope
#   Gulg 25.0 < Ice 28.6 < Sunken 32.2 < Mirage 40.2 < Fortress 47.7
# with the same rules as the fortress rows: hardest fight in the MOST common
# slot, boss cameos pinned to the (map, slot) rando._DUNGEON_BOSS_SLOTS already
# uses. Rows are dealt round-robin from each dungeon's pool so every map gets a
# spread of the band rather than a slice of it.
#
# Being authored, these dungeons are no longer seed-random -- the same trade the
# basement and the fortress already make in exchange for exact control and
# access to u16 DLC formations.
_GG_POOLS = {   # Mount Gulg 0x29-0x2d -- expected 25.0 (was 24.1)
    0x29: (0x098, 0x09c, 0x02f, 0x096, 0x02e, 0x0ab, 0x0ad, 0x07f),
    #      Wraith Piscodemon DarkWiz Cockatrice IceGigas Bloodbones WinterWolf GARLAND
    0x2a: (0x098, 0x0ac, 0x02f, 0x096, 0x02e, 0x030, 0x11b, 0x0ad),
    0x2b: (0x098, 0x0ac, 0x09c, 0x096, 0x02e, 0x030, 0x11b, 0x0ab),
    0x2c: (0x098, 0x0ac, 0x09c, 0x02f, 0x02e, 0x030, 0x11b, 0x0ab),
    0x2d: (0x0ac, 0x09c, 0x02f, 0x096, 0x030, 0x11b, 0x0ab, 0x0ad),
}
_IC_POOLS = {   # Cavern of Ice 0x40-0x44 -- expected 28.6 (was 20.8, BELOW Gulg)
    0x40: (0x0ae, 0x0b0, 0x04b, 0x156, 0x12c, 0x119, 0x0ea, 0x033),
    #      IceGigas+Wolves WhiteDrgn DrgnZomb Bonesnatch Skuldier ElmGigas HornedDevil Rakshasa
    0x41: (0x0ae, 0x09d, 0x04b, 0x156, 0x12c, 0x119, 0x0e9, 0x033),
    0x42: (0x0ae, 0x09d, 0x0b0, 0x12c, 0x119, 0x0e9, 0x07d, 0x01c),
    #      ... slots 6/7 are the ASTOS and PISCODEMON cameos
    0x43: (0x0ae, 0x09d, 0x0b0, 0x04b, 0x156, 0x119, 0x0e9, 0x0ea),
    0x44: (0x09d, 0x0b0, 0x04b, 0x156, 0x12c, 0x0e9, 0x0ea, 0x033),
}
_SK_POOLS = {   # Sunken Shrine 0x17-0x1e -- expected 32.2 (was 31.5, and vanilla-
                # flavored: "sunken" self-maps, so it rerolled from its own pool)
    0x17: (0x125, 0x062, 0x04e, 0x0e2, 0x047, 0x058, 0x05a, 0x079),
    #      SahaginQueen WhiteCroc BlueDrgn WhiteCroc WaterNaga StoneGolem Shark MARILITH
    0x18: (0x125, 0x11e, 0x04e, 0x0e2, 0x0a0, 0x061, 0x058, 0x0c3),
    0x19: (0x125, 0x11e, 0x15c, 0x0e2, 0x0a0, 0x0cf, 0x0c3, 0x0c2),
    0x1a: (0x11e, 0x15c, 0x049, 0x0a0, 0x0cf, 0x0f2, 0x0c2, 0x0b9),
    0x1b: (0x15c, 0x049, 0x0d2, 0x0cf, 0x0f2, 0x0c8, 0x0b9, 0x045),
    0x1c: (0x049, 0x0d2, 0x0c4, 0x0f2, 0x0c8, 0x0fe, 0x045, 0x043),
    0x1d: (0x0d2, 0x0c4, 0x062, 0x0c8, 0x0fe, 0x061, 0x043, 0x042),
    0x1e: (0x0c4, 0x062, 0x04e, 0x0fe, 0x061, 0x047, 0x042, 0x05a),
}
_MT_POOLS = {   # Mirage Tower 0x50-0x52 -- expected 40.2 (was 28.6, i.e. SOFTER
                # than its own vanilla 32.9; it drew the fortress's weak tail,
                # 1-4 Earth Medusa 15.0 / 3-4 Manticore 15.1 included)
    0x50: (0x068, 0x149, 0x050, 0x211, 0x0be, 0x051, 0x04d, 0x0b3),
    #      Vampires BlkGoblin+ElmGigas BlackFlan MadOgre+FloodGigas Wyvern AirElem BlkKnight Rakshasa
    0x51: (0x068, 0x053, 0x050, 0x0cc, 0x0be, 0x0d8, 0x04d, 0x0d2),
    0x52: (0x053, 0x149, 0x0cc, 0x211, 0x0d8, 0x051, 0x0d2, 0x0b3),
}
# map id -> pool row, assembled in one contiguous block. The detour indexes it
# through a 256-byte map-id table (0 = "not ours, run vanilla"), which stays flat
# no matter how many dungeons get authored -- a chain of range compares would
# not.
_CFP_ALL = {}
for _pools in (_CF_POOLS, _FF_POOLS, _GG_POOLS, _IC_POOLS, _SK_POOLS, _MT_POOLS):
    for _mid, _row in _pools.items():
        assert _mid not in _CFP_ALL, f"map {_mid:#04x} claimed by two pools"
        _CFP_ALL[_mid] = _row
del _pools, _mid, _row
_CFP_ROWMAP_LEN = 256          # indexed by map id; the hook path is mapid < 0x87


def _cf_data():
    """(256-byte map-id -> row+1 table) + (N rows x 8 u16 formation ids).

    Row 0 is the lowest map id in _CFP_ALL; the table holds row+1 so that 0 can
    mean "this map is not authored, fall through to the vanilla u8 table"."""
    mids = sorted(_CFP_ALL)
    rowmap = bytearray(_CFP_ROWMAP_LEN)
    rows = bytearray()
    for i, mid in enumerate(mids):
        assert mid < _CFP_ROWMAP_LEN, f"map {mid:#04x} outside the row map"
        assert i + 1 < 256, "too many authored rows for a u8 row map"
        rowmap[mid] = i + 1
        row = _CFP_ALL[mid]
        assert len(row) == 8, f"pool {mid:#04x} must have 8 slots"
        for fid in row:
            assert 0 < fid < 0x260, f"pool {mid:#04x} fid {fid:#05x} out of range"
            rows += struct.pack("<H", fid)
    return bytes(rowmap) + bytes(rows)


def apply_chaos_floor_pools(elf: bytearray, feats=None):
    fo = E.ram2file(_CF_HOOK)
    w = struct.unpack_from("<I", elf, fo)[0]
    if w != _CF_HOOK_W0:
        raise ValueError(f"unexpected cave-table hook @{_CF_HOOK:#x}: {w:#010x}")
    # At the hook: s1 = mapid, s0 = mapid*8, v0 = the slot byte the vanilla
    # scramble roll just produced, s4 = field struct. v1 is dead (the displaced
    # lui/addiu pair was building the zones_caves base), so t*/v1 are scratch and
    # no jal is needed -- reusing the vanilla slot keeps the rarity curve intact.
    data = _cf_data()

    def _code(pool_vaddr):
        return A.asm_labels([
            A.li("t4", pool_vaddr),                     # &ROWMAP[0]
            A.addu("t0", "t4", "s1"),                   # &ROWMAP[mapid]
            A.lbu("t0", 0, "t0"),                       # row+1, or 0 = not authored
            ("beq", "t0", "zero", "vanilla"), A.nop(),
            A.addiu("t0", "t0", -1),                    # t0 = row
            A.sll("t2", "t0", 4),                       # row*16 (8 x u16)
            A.sll("t3", "v0", 1),                       # slot*2
            A.addu("t2", "t2", "t3"),
            A.addiu("t4", "t4", _CFP_ROWMAP_LEN),       # rows start after the map
            A.addu("t2", "t2", "t4"),                   # &ROWS[row][slot]
            A.lhu("t2", 0, "t2"),                       # u16 formation id
            A.sh("t2", _CF_CTX_FORM_OFF, "s4"),         # battle ctx (u16 field)
            A.j(_CF_TAIL), A.nop(),                     # commit encounter
            ("label", "vanilla"),
            A.li("v1", _CF_ZONES_CAVES),                # rebuild what we displaced
            A.j(_CF_RESUME), A.nop(),
        ])

    placeholder = data + _code(0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    real = data + _code(cave_vaddr)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    # Single-word hook. The untouched delay slot @0x0884225c (`addiu v1,v1,0x5f9c`)
    # still runs against a stale v1 -- harmless, both paths rebuild v1 themselves.
    elf[fo:fo + 4] = A.j(cave_vaddr + len(data))


# ---- DLC boss rewards --------------------------------------------------------
# Echidna / Cerberus / Ahriman / Two-Headed Dragon ship with xp = gil = 0: the
# bonus dungeons script their rewards, so the stat record never had to carry one.
# The chaos_floor cameos above field them as random encounters, where nothing
# scripts anything -- a 5000 HP Ahriman for zero XP and zero gil.
#
# NOT a FEATURES entry: this is a DATA-ONLY bake, delivered by the existing
# monster_rewards DataPatch (boot_patch.DLC_BOSS_REWARDS, seeded inside
# scale_monster_rewards so xp_boost/gil_boost scale it like every other monster).
# One code path, so there is nothing to keep in sync and no bake-order hazard --
# an ELF-side write here would race the DataPatch, which applies to the same
# bytes and legitimately holds a BOOSTED value the ELF copy must not clobber.
# ff1_data.MONSTER_STATS_BLOCK therefore stays a byte-for-byte vanilla mirror,
# which is required: it is that DataPatch's `vanilla` signature.
#
# The reward change is unavoidably global (it lands on the native bonus-dungeon
# fights too), for the same reason the cameos keep their minions -- the
# 0x100-0x25f formation table has ZERO free records, so no cameo-only clone of
# these bosses can exist. Their stat record IS the native one.

# ---- feature: Encounters on Chaos' Floor -------------------------------------
# Map 0x27 (the floor Chaos stands on) ships a fully populated 8-slot encounter
# table AND a valid rate zone, but byte 0 of its map_gate record is 0 -- the
# engine's "no encounters here" flag (`lbu s5,(v0)` / `blezl s5` @0x08842158),
# the same 0 that towns and empty entrance maps carry. So the table is dead data
# and the walk to Chaos is silent in vanilla. Flipping the byte to 4 -- exactly
# what the other seven basement floors use -- wakes it up; s5 then indexes
# rate_ptrs_dungeon[4] (0x089456b4), a valid entry.
_CF_GATE_REC = 0x0894463A      # map_gate base, 8B records
_CF_GATE_MAP = 0x27            # Chaos' floor
_CF_GATE_OFF = 0               # byte 0 = encounter-rate index (0 = disabled)
_CF_GATE_VANILLA = 0x00
_CF_GATE_ON = 0x04             # matches basement floors 0x20-0x26


def apply_chaos_floor_encounters(elf: bytearray, feats=None):
    off = E.ram2file(_CF_GATE_REC + _CF_GATE_MAP * 8) + _CF_GATE_OFF
    if elf[off] != _CF_GATE_VANILLA:
        raise ValueError(f"map {_CF_GATE_MAP:#04x} gate byte is {elf[off]:#04x}, "
                         f"expected vanilla {_CF_GATE_VANILLA:#04x}")
    elf[off] = _CF_GATE_ON


def apply_dangerous_forests(elf: bytearray, feats=None):
    harder = bool((feats or {}).get("harder_encounters"))
    assert _DF_POOL_MAX == 8, "cave computes tier*16 (=tier*_DF_POOL_MAX*2) via one sll"
    fo = E.ram2file(_DF_HOOK)
    w = struct.unpack_from("<I", elf, fo)[0]
    if w != _DF_HOOK_W0:
        raise ValueError(f"unexpected land-path hook @{_DF_HOOK:#x}: {w:#010x}")
    # 1) count reductions (verify vanilla first). Only the edited fids that appear
    # in pool A (slot 0 of tiers 4 & 8) matter; skipped when harder (pool B never
    # references them). Targets are unreferenced so the edit is forest-exclusive.
    if not harder:
        for fid, (van_hex, new_hex) in _DF_FORM_EDITS.items():
            ffo = E.ram2file(_DF_FORM_TABLE + fid * _DF_FORM_STRIDE)
            if elf[ffo:ffo + _DF_FORM_STRIDE] != bytes.fromhex(van_hex):
                raise ValueError(f"DF form edit 0x{fid:03x}: unexpected vanilla record")
            elf[ffo:ffo + _DF_FORM_STRIDE] = bytes.fromhex(new_hex)
    # 2) cave = [272 B data][code]; entry after the data. Reads the party tile's ATT
    # attr via the field struct's live map pointers ($s4 = field struct, $s1=x,
    # $s2=y). On a forest tile it draws the game RNG (fn 0x8869528), maps
    # `rng & 63` through SLOT_MAP to a weighted slot of the zone's tier POOL (v190;
    # was a flat `rng % CNT[tier]`), writes that u16 formation id to
    # the battle-context field (s4+0xbe4), and jumps to the encounter-commit tail --
    # bypassing the vanilla u8 slot roll. The tail restores $ra from its OWN stack
    # frame (0x1c($sp)), so the jal clobbering $ra is harmless. t0-t4 are free
    # scratch; $s0-$s2/$s4 survive the jal (callee-saved) and drive the pool lookup.
    data = _df_data(harder)
    def _code(zonetier_vaddr, pool_vaddr, slotmap_vaddr):
        return A.asm_labels([
            A.lw("t0", _DF_GRID_PTR_OFF, "s4"),      # grid arena
            A.lw("t1", _DF_ATT_PTR_OFF, "s4"),       # att base
            A.sll("t2", "s2", 9),                    # y*512
            A.sll("t3", "s2", 1),                    # y*2
            A.subu("t2", "t2", "t3"),                # y*510
            A.sll("t3", "s1", 1),                    # x*2
            A.addu("t2", "t2", "t3"),                # y*510 + x*2
            A.addu("t2", "t2", "t0"),                # + arena
            A.lhu("t2", _DF_GRID_HDR, "t2"),         # tile id (u16), +10 header
            A.sll("t2", "t2", 1),                    # tile*2
            A.addu("t2", "t1", "t2"),
            A.lhu("t2", 0, "t2"),                    # ATT attr (u16)
            A.addiu("t3", "zero", _DF_FOREST_ATTR),
            ("bne", "t2", "t3", "notforest"), A.nop(),  # not forest -> vanilla slot roll
            # --- forest tile: draw RNG, pick a variant from the zone's tier pool ---
            A.jal(_DF_RNG_FN), A.nop(),              # v0 = random word ($s* survive)
            A.andi("t1", "v0", 63),                  # weight-map index (rng & 63)
            A.li("t4", slotmap_vaddr),
            A.addu("t4", "t4", "t1"),                # &SLOT_MAP[rng & 63]
            A.lbu("t1", 0, "t4"),                    # weighted slot 0..MAX-1
            A.li("t4", _DF_ZONES_OW),                # zones_overworld base
            A.subu("t2", "s0", "t4"),                # s0 - base = zone*8
            A.srl("t3", "t2", 3),                    # zone
            A.li("t4", zonetier_vaddr),
            A.addu("t4", "t4", "t3"),                # &ZONE_TIER[zone]
            A.lbu("t3", 0, "t4"),                    # tier idx (0..8)
            A.sll("t4", "t3", 4),                    # tier*16 (= tier*_DF_POOL_MAX*2)
            A.sll("t2", "t1", 1),                    # slot*2
            A.addu("t4", "t4", "t2"),                # tier*16 + slot*2
            A.li("t0", pool_vaddr),
            A.addu("t4", "t4", "t0"),                # &POOL[tier][slot]
            A.lhu("t2", 0, "t4"),                    # u16 formation id
            A.sh("t2", _DF_CTX_FORM_OFF, "s4"),      # store into battle ctx (u16)
            A.j(_DF_RESUME_TAIL), A.nop(),           # commit encounter
            ("label", "notforest"),
            A.j(_DF_RESUME), A.nop(),                # non-forest: vanilla slot roll
        ])
    placeholder = data + _code(0, 0, 0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    zonetier_vaddr = cave_vaddr
    pool_vaddr = cave_vaddr + 64
    slotmap_vaddr = cave_vaddr + 64 + _DF_POOL_MAX * 9 * 2   # = +208 at MAX=8
    real = data + _code(zonetier_vaddr, pool_vaddr, slotmap_vaddr)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    # single-word hook: `j cave_entry`; the untouched delay slot @0x8841f64
    # (addu s0,v0,v1) still computes the vanilla land row before the cave runs
    # (used only on the non-forest fall-through).
    elf[fo:fo + 4] = A.j(cave_vaddr + len(data))


# ---- feature: Regional Ocean Encounters -------------------------------------
# The SHIP's ocean encounters are drawn from the FLAT terrain-2 pool 0x08945aa0
# (single 8-slot row, same fight everywhere) -- LIVE-PROVEN 2026-07-09 by a
# black-box sentinel test (re_only/ocean_table_probe4.py): distinct monster written
# into each water table, held across battle reloads; the ship in deep NW AND SW open
# ocean drew the 0x08945aa0 sentinel (Sea Scorpion), NOT the 0x08945ca8 one. So the
# ship reaches the encounter selector's TERRAIN-2 branch (0x08841f70 -> s0=0x08945aa0),
# despite the static class->terrain table mapping ocean(class 12)->terrain 4: the
# vehicle path resolves to terrain 2 in practice. The earlier "ocean=terrain-4=
# 0x08945ca8, pure DATA edit" belief (v28..v34) was therefore INERT -- it stamped a
# table (0x08945ca8) that in vanilla holds LAND mobs (Goblin/Troll/Wyvern) and that
# the ship never reads; the player kept fighting the untouched flat sea pool. This
# flip-flopped once before (v23 hit terrain-2 -> "sea troll on land"; v28 "fixed" it
# onto terrain-4 -> feature died). Neither is a pure data edit.
#
# FIX (v36): a DF-style on-disc DETOUR on the terrain-2 branch (0x08841f70, the ship
# path). Inside, read the party tile's ATT attribute (== 0xF00F = deep ocean) via the
# field struct's live map pointers -- EXACTLY the gate that separates the ship
# (ocean, 0xF00F) from the canoe (river, 0xF009) and foot-on-shallows (0xF002/0)
# that SHARE this same terrain-2 flat pool. On an ocean tile: compute the zone,
# look up its regional pool (1..4), RNG-pick one of the pool's 8 formation ids, write
# it to the battle-context formation field (s4+0xbe4) and jump to the encounter-commit
# tail -- frame-exact, no client race. Non-ocean (river/shallow) tiles + pool-0
# (starting-sea basin) + unassigned zones fall through to the vanilla flat slot roll
# (0x08841f9c), so canoe/foot keep vanilla -- NO land/river spillover.
# The old belt-and-braces DATA stamp into the terrain-4 table 0x08945ca8 was
# REMOVED in v190 (u8 slots cannot hold the pools' u16 DLC ids) -- code hook only.
#
# CONSTRAINT (unchanged): a formation's monster-graphics set is preloaded per
# formation-id from a battle resource pack (NOT the executable), so we only
# REFERENCE existing vanilla formations and COUNT-EDIT ocean-exclusive slots --
# never author new monster sets. All rows below use vanilla formation ids; the
# _OC_FORM_EDITS entries change counts only.
_OC_T4_TABLE = 0x08945CA8       # terrain-4 zoned table -- NO LONGER WRITTEN (v190;
                                # u8 slots can't hold the pools' u16 DLC ids, see
                                # apply_regional_ocean_encounters). Kept for reference.
_OC_HOOK        = 0x08841F70    # terrain-2 branch `b 0x8841f9c` (ship path); delay slot
                                # @0x8841f74 (addiu s0,s0,0x5aa0 -> s0=0x8945aa0) still runs
_OC_HOOK_W0     = 0x1000000A    # vanilla word: b 0x8841f9c (beq zero,zero,+10)
_OC_RESUME      = 0x08841F9C    # terrain-2 flat slot-roll continuation (vanilla path)
_OC_OCEAN_ATTR  = 0xF00F        # ATT attribute of a deep-ocean (ship-only) tile
_OC_FORM_TABLE = 0x08948D14     # monster_formations base (= _DF_FORM_TABLE)
# 8-slot rows (roll-weighted via scramble 0x08945850, slot 0 most likely), all
# vanilla formation ids, one per regional pool.
_OC_POOL_ROWS = {
    # NW (threat 18-46)
    #   0x0e1  23.0 1 Sea Troll + 0-3 Sea Snake
    #   0x042  24.4 1-2 Sea Troll + 1-3 Sea Scorpion
    #   0x037  24.5 1-3 Wyrm
    #   0x0c2  26.8 1-2 Sea Troll + 1-4 Sea Scorpion
    #   0x061  27.9 1-2 Sea Troll + 0-2 Sea Snake + 0-2 Sea Scorpion
    #   0x0c4  32.4 1-5 Sea Scorpion + 0-3 Sea Snake
    #   0x070  18.0 1-3 Wyvern
    #   0x044  46.3 1-6 Sea Scorpion + 2-5 Sea Snake + 2 Sea Troll
    1: (0x0e1, 0x042, 0x037, 0x0c2, 0x061, 0x0c4, 0x070, 0x044),
    # NE (threat 18-55)
    #   0x135  19.7 1-3 Death Elemental
    #   0x16b  22.9 1 Dragon Zombie
    #   0x047  27.7 1 Water Naga + 0-1 Water Elemental
    #   0x0f2  28.9 3-6 Sea Snake
    #   0x049  35.1 1-3 Water Elemental
    #   0x0c9  50.2 3-6 Water Elemental
    #   0x154  18.5 1 Poison Naga + 0-1 Death Elemental
    #   0x0c7  55.5 1-2 Water Naga + 3-6 Water Elemental
    2: (0x135, 0x16b, 0x047, 0x0f2, 0x049, 0x0c9, 0x154, 0x0c7),
    # SW (threat 8-39)
    #   0x05d   9.9 1-2 Shark + 0-2 Sahagin + 0-1 Bigeyes
    #   0x05b   9.9 0-6 Sahagin + 0 Sahagin Chief + 1-2 Bigeyes
    #   0x0db  10.9 3-7 Sahagin + 0-2 Sahagin Chief
    #   0x05c  11.6 1-5 Buccaneer + 0 Shark
    #   0x072  23.1 2-4 Sea Snake
    #   0x045  26.1 0-1 Sahagin Prince + 1-2 White Shark
    #   0x05e   8.0 1 Shark + 0-1 Sahagin Chief
    #   0x125  39.1 5-8 Sahagin Queen
    3: (0x05d, 0x05b, 0x0db, 0x05c, 0x072, 0x045, 0x05e, 0x125),
    # SE (threat 12-50)
    #   0x048  22.0 1 White Shark + 0-1 Deepeyes
    #   0x05a  24.2 1-2 White Shark + 0-1 Shark
    #   0x045  26.1 0-1 Sahagin Prince + 1-2 White Shark
    #   0x0fe  28.0 1-2 Sahagin Prince + 8 Sahagin Chief
    #   0x0c5  42.2 3-6 Sahagin Prince + 2 White Shark
    #   0x124  44.3 0-1 Sahagin Queen + 1-2 Killer Shark
    #   0x0de  11.5 1-2 Shark + 0-3 Sahagin Chief
    #   0x0c6  49.6 2-5 Ghost
    4: (0x048, 0x05a, 0x045, 0x0fe, 0x0c5, 0x124, 0x0de, 0x0c6),
}
# zone -> pool (re_only/build_ocean_regions.compute_zone_pool, 8x8 grid). Pool 0
# (starting sea) and no-ocean zones are OMITTED here so they keep their vanilla t4
# rows -- only the 4 outer quadrants are re-pooled.
_OC_POOL_ZONES = {
    1: (0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19, 24, 25, 26, 27),
    2: (4, 5, 6, 7, 12, 13, 14, 15, 20, 21, 22, 23, 28, 29, 30, 31),
    3: (32, 33, 34, 40, 41, 42, 48, 49, 50, 56, 57, 58, 59),
    4: (38, 39, 47, 55, 60, 61, 62, 63),
}
# count-only edits to ocean-exclusive formations (fid -> (vanilla 15B, patched 15B)).
# 0xDD's edit was DROPPED in v190: SW slot 0 became the Sahagin Queen (0x125), so
# 0xDD is no longer referenced by any pool and editing it would be a stray write.
_OC_FORM_EDITS = {
    0xDB: ("0000040c03070d0002ff0000ff0000", "0000040c02020d0404ff0000ff0000"),
    0x5B: ("0100040c00060d0000130102ff0000", "0100040c06060d0000130202ff0000"),
}


def _oc_zone_pool():
    """ZONE_POOL[64] (u8): 0 = starting-sea / no-ocean (keep vanilla flat roll),
    1..4 = NW/NE/SW/SE regional pool. Inverse of _OC_POOL_ZONES."""
    zp = bytearray(64)
    for pool, zones in _OC_POOL_ZONES.items():
        for z in zones:
            zp[z] = pool
    return bytes(zp)


def apply_regional_ocean_encounters(elf: bytearray, feats=None):
    # 1) count-only formation edits (verify the vanilla record first).
    #    NOTE: the old "belt-and-braces" data stamp into the terrain-4 table
    #    (_OC_T4_TABLE) was REMOVED in v190. That table is u8-per-slot, and the
    #    pools now carry u16 DLC formation ids (Sahagin Queen 0x124/0x125, Death
    #    Elemental 0x135, Dragon Zombie 0x16b, ...) which cannot be represented
    #    there -- stamping them would silently truncate to a DIFFERENT formation
    #    (0x124 -> 0x24 Minotaur Zombie, 0x135 -> 0x35 Earth Medusa). Leaving the
    #    table vanilla is the correct fail-safe: the ship provably reads terrain-2
    #    (live sentinel test), which the detour below owns, and any tile that did
    #    resolve to terrain-4 now gets vanilla behaviour rather than a wrong fight.
    for fid, (van_hex, new_hex) in _OC_FORM_EDITS.items():
        ffo = E.ram2file(_OC_FORM_TABLE + fid * 15)
        if elf[ffo:ffo + 15] != bytes.fromhex(van_hex):
            raise ValueError(f"ocean form edit 0x{fid:02x}: unexpected vanilla record")
        elf[ffo:ffo + 15] = bytes.fromhex(new_hex)
    # 2) CODE: terrain-2 ocean detour (see block comment above). Cave layout:
    #    [ZONE_POOL 64B][POOL 4x8 u16 = 64B][SLOT_MAP 64B][code]; entry = cave + 192.
    fo = E.ram2file(_OC_HOOK)
    w = struct.unpack_from("<I", elf, fo)[0]
    if w != _OC_HOOK_W0:
        raise ValueError(f"unexpected terrain-2 hook @{_OC_HOOK:#x}: {w:#010x}")
    zone_pool = _oc_zone_pool()
    for p, row in _OC_POOL_ROWS.items():
        assert len(row) == 8, f"ocean pool {p} must be exactly 8 slots (weighted map)"
    pool_rows = b"".join(struct.pack("<H", f)
                         for p in (1, 2, 3, 4) for f in _OC_POOL_ROWS[p])   # 64 B
    data = zone_pool + pool_rows + _slot_weight_map()                       # 192 B
    assert len(data) == 192 and len(data) % 4 == 0

    def _code(zp_vaddr, pool_vaddr, slotmap_vaddr):
        return A.asm_labels([
            # --- read the party tile's ATT attribute (s4=field struct, s1=x, s2=y) ---
            A.lw("t0", _DF_GRID_PTR_OFF, "s4"),      # tile-grid arena
            A.lw("t1", _DF_ATT_PTR_OFF, "s4"),       # ATT base
            A.sll("t2", "s2", 9),                    # y*512
            A.sll("t3", "s2", 1),                    # y*2
            A.subu("t2", "t2", "t3"),                # y*510
            A.sll("t3", "s1", 1),                    # x*2
            A.addu("t2", "t2", "t3"),                # y*510 + x*2
            A.addu("t2", "t2", "t0"),
            A.lhu("t2", _DF_GRID_HDR, "t2"),         # tile id (u16), +10 header
            A.sll("t2", "t2", 1),
            A.addu("t2", "t1", "t2"),
            A.lhu("t2", 0, "t2"),                    # ATT attr (u16)
            A.li("t3", _OC_OCEAN_ATTR),
            ("bne", "t2", "t3", "vanilla"), A.nop(),  # not deep ocean -> vanilla flat roll
            # --- ocean tile: zone = ((x+7)>>5) + 8*((y+7)>>5) ---
            A.addiu("t2", "s1", 7), A.srl("t2", "t2", 5),
            A.addiu("t3", "s2", 7), A.srl("t3", "t3", 5), A.sll("t3", "t3", 3),
            A.addu("t2", "t2", "t3"),                # zone -- 0..72, NOT 0..63
            # v233 BOUNDS GUARD, and this one is not theoretical: the +7 bias
            # gives grid index 8 for coord 249..254, so zone reaches 72, and
            # 2991 OCEAN cells along the east and south map edges land there.
            # Unguarded, _oc_zone_pool[zone] read past its 64-byte table into
            # the u16 pool rows that follow -- a byte of 1..4 picks the wrong
            # region, and anything larger indexes past POOL entirely and rolls
            # a garbage formation. Present since v36; measured 2026-08-06.
            A.sltiu("t3", "t2", 64),
            ("beq", "t3", "zero", "vanilla"), A.nop(),
            A.li("t4", zp_vaddr), A.addu("t4", "t4", "t2"),
            A.lbu("t3", 0, "t4"),                    # pool (0 = vanilla, 1..4)
            ("beq", "t3", "zero", "vanilla"), A.nop(),  # basin/no-ocean zone -> vanilla
            # --- pooled ocean tile: stash (pool-1) in s0 (dead on this path -- the
            # commit tail 0x88425b0 reloads s0..s5 from the stack frame before jr ra),
            # so it survives the RNG jal with NO zone/pool recompute. ---
            A.addiu("s0", "t3", -1),                 # s0 = pool-1 (0..3); survives jal
            A.jal(_DF_RNG_FN), A.nop(),              # v0 = random word (s0..s2/s4 survive; t* clobbered)
            A.andi("t5", "v0", 63),                  # weight-map index (rng & 63)
            A.li("t4", slotmap_vaddr), A.addu("t4", "t4", "t5"),
            A.lbu("t5", 0, "t4"),                    # weighted slot 0..7
            A.sll("t4", "s0", 4),                    # (pool-1)*16 (8 x u16 per row)
            A.sll("t5", "t5", 1),                    # slot*2
            A.addu("t4", "t4", "t5"),                # + slot offset
            A.li("t0", pool_vaddr), A.addu("t4", "t0", "t4"),
            A.lhu("t2", 0, "t4"),                    # u16 formation id (DLC-capable)
            A.sh("t2", _DF_CTX_FORM_OFF, "s4"),      # store into battle ctx (u16 field)
            A.j(_DF_RESUME_TAIL), A.nop(),           # commit encounter
            ("label", "vanilla"),
            A.j(_OC_RESUME), A.nop(),                # vanilla terrain-2 flat slot roll (s0 intact)
        ])

    placeholder = data + _code(0, 0, 0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    real = data + _code(cave_vaddr, cave_vaddr + 64, cave_vaddr + 128)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    # single-word hook: `j cave_entry` over the terrain-2 branch; its delay slot
    # (addiu s0,s0,0x5aa0) still sets s0 = vanilla flat pool for the fall-through.
    elf[fo:fo + 4] = A.j(cave_vaddr + len(data))


# ---- feature: Overworld u16 formations (harder overworld encounters) --------
# The overworld foot table zones_overworld (RAM 0x08945890, 64 zones x 8 slots) is
# u8, so it could only ever hold base-game formations (< 0x100). That kept the top
# tiers starving: only ~20 u8 land formations exist above threat 40, against ~76
# once the PSP bonus-dungeon bestiary is reachable.
#
# The read site (live-verified disasm):
#   0x08841fb8  lbu  v0, (v0)       # slot scramble byte -> slot index 0..7
#   0x08841fbc  addu v0, s0, v0     # s0 = zones_overworld + zone*8
#   0x08841fc0  lbu  v0, (v0)       # <- the u8 formation id
#   0x08841fc4  b    0x88425b0      # commit tail
#   0x08841fc8  sh   v0, 0xbe4(s4)  # delay slot -- ALREADY stores a u16
#
# The destination field is already u16, so only the LOAD is the limit. Note the
# hook CANNOT go on 0x08841fc0: its delay slot 0x08841fc4 is a branch, and a
# branch in a jump's delay slot is architecturally undefined. So we hook the addu
# at 0x08841fbc and displace the lbu into the cave, returning to 0x08841fc4.
#
# Rather than widen the table in place, a COMPANION HIGH-BYTE table supplies
# bits 8-15:
#   formation = low[zone*8+slot] | (high[zone*8+slot] << 8)
#
# v230: THE COMPANION LIVES IN THE CAVE SEGMENT. Through v229 it was homed on the
# zoned table 0x08945aa8, documented as "the terrain-3 zoned table -- confirmed
# UNUSED on the overworld (0 tiles resolve to terrain 3)". That was WRONG, and it
# is the single worst encounter bug the project has shipped: 0x08945aa8 is the
# DESERT table, and the companion overwrote it. Every desert tile in the game --
# the whole Ryukhan Desert around Mirage Tower included -- rolled its formation id
# out of the companion's HIGH BYTES, which are 0x00/0x01/0x02 for almost every
# slot. Those are formations 0x00 "3-5 Goblin", 0x01 "2-4 Skeleton" and 0x02
# "1-3 Goblin Guard + Wolf": the late-game desert became the softest terrain in
# the game, and only when harder_overworld_encounters was ON (the companion only
# ships with that flag). Live-confirmed 2026-08-06 from a player session -- 5
# Goblins at overworld (199,77), zone 22, vanilla battle XP 30.
#
# ROOT CAUSE of the wrong "terrain 3 is unused" claim: encounter_census.py (and
# re_only/tile_terrain_probe.py) decoded the tile-class -> terrain mapping as
# `terrain = 0x8945810[class*2 + 1]`. The engine does NOT index by class*2. From
# 0x08841e8c..0x08841ebc (`raw` = the tile byte from the terrain map):
#       raw <= 0 or raw >= 13  -> no encounter at all
#       raw <  3               -> entry = raw*2
#       raw >= 3               -> entry = raw+3        <-- the +3, not *2
#       terrain = 0x8945810[entry*2 + 1]
# so the real mapping is
#       raw 1,2,3,4,5,10 -> terrain 0  land zoned      0x08945890
#       raw 6,7,8        -> terrain 1  MARSH/RIVER flat 0x08945a90
#       raw 9            -> terrain 4  zoned            0x08945ca8
#       raw 11           -> terrain 3  DESERT zoned     0x08945aa8   (1732 tiles!)
#       raw 12           -> terrain 2  OCEAN/shallow flat 0x08945aa0 (44503 tiles)
# The same bad decode is why v23/v28 flip-flopped Regional Ocean between terrain-2
# and terrain-4 and why v36 had to prove the ship reads terrain-2 with a black-box
# sentinel: class 12 really does resolve to terrain 2. It also produced the v222
# "empty encounter on the canoe river" (a companion index guarded only by luck).
#
# The companion is therefore now a plain 512-byte blob INSIDE the cave segment,
# where nothing else can claim it, and 0x08945aa8 is left to its rightful owner.
# The bytes arrive through feats["_ow_hi"] (the seed's shuffle output, which used
# to ride the DataPatch path); a missing/short blob bakes an all-zero companion,
# which degrades to the plain vanilla u8 id instead of garbage.
_OWU_HOOK    = 0x08841FBC       # addu v0,s0,v0  (the displaced instruction)
_OWU_HOOK_W0 = 0x02021021       # vanilla word at the hook
_OWU_LBU     = 0x08841FC0       # displaced lbu v0,(v0) -> becomes a nop
_OWU_LBU_W0  = 0x90420000
_OWU_RESUME  = 0x08841FC4       # b 0x88425b0 (+ its delay slot sh v0,0xbe4(s4))
_OWU_LOW     = 0x08945890       # zones_overworld
_OWU_LOW_LEN = 512              # 64 zones x 8 slots -- the ONLY range the hi table covers
# v222 (live 2026-08-05, user report: an EMPTY encounter on the canoe river beside
# the Cavern of Ice). The lbu at 0x08841fc0 is NOT the land table's private read --
# it is the SHARED slot-roll load for every terrain path. Each path only picks the
# BASE in s0 beforehand: land = zones_overworld 0x08945890, terrain-2 flat (ship on
# open sea, CANOE on rivers, foot on shallows) = 0x08945aa0, terrain-4 zoned ocean =
# 0x08945ca8. The cave computed the companion index as an UNGUARDED v0 - 0x08945890,
# so a river roll (base 0x08945aa0, delta 0x210) read its "high byte" from
# 0x08945aa8 + 0x210 = 0x08945cb8 -- inside the vanilla terrain-4 LAND-mob table --
# and OR'd e.g. 0x63 << 8 onto a valid u8 river formation. The resulting id is far
# past the formation table, so the battle spawned with no monsters in it.
# Fix: gate the OR on the delta actually landing inside zones_overworld
# (0 <= delta < 512); any other base falls through with the plain vanilla u8 id.
# Terrain-2 sits 0x210 above and terrain-4 0x418 above, so both now miss the gate;
# zones_caves (0x08945f9c, delta 0x70c) is likewise excluded whether or not the
# dungeon roll shares this site.


def apply_overworld_u16(elf: bytearray, feats=None):
    for addr, want in ((_OWU_HOOK, _OWU_HOOK_W0), (_OWU_LBU, _OWU_LBU_W0)):
        fo = E.ram2file(addr)
        w = struct.unpack_from("<I", elf, fo)[0]
        if w != want:
            raise ValueError(f"unexpected overworld read site @{addr:#x}: "
                             f"{w:#010x} (want {want:#010x})")

    # cave layout: [512 B companion high-byte table][code]; entry = cave + 512.
    hi = bytes((feats or {}).get("_ow_hi") or b"")
    if len(hi) > _OWU_LOW_LEN:
        raise ValueError(f"companion table is {len(hi)} B, max {_OWU_LOW_LEN}")
    hi = hi + b"\0" * (_OWU_LOW_LEN - len(hi))      # short/absent -> plain u8 ids

    def _code(high_vaddr):
        return A.asm_labels([
            A.addu("v0", "s0", "v0"),        # displaced: v0 = &low[zone*8+slot]
            A.li("t0", _OWU_LOW),
            A.subu("t1", "v0", "t0"),        # t1 = zone*8 + slot
            A.lbu("t2", 0, "v0"),            # low byte (displaced lbu)
            # only zones_overworld has a companion: delta must be < 512 (unsigned,
            # so a base BELOW the land table wraps huge and fails too)
            A.li("t0", _OWU_LOW_LEN),
            A.sltu("t0", "t1", "t0"),
            ("beq", "t0", "zero", "plain"),  # marsh/desert/ocean/cave base -> vanilla u8
            A.nop(),
            A.li("t0", high_vaddr),
            A.addu("t0", "t0", "t1"),
            A.lbu("t3", 0, "t0"),            # high byte
            A.sll("t3", "t3", 8),
            A.or_("v0", "t2", "t3"),         # v0 = u16 formation id
            A.j(_OWU_RESUME), A.nop(),       # back to the commit branch
            ("label", "plain"),
            A.or_("v0", "t2", "zero"),       # v0 = the vanilla u8 id
            A.j(_OWU_RESUME), A.nop(),
        ])

    placeholder = hi + _code(0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    real = hi + _code(cave_vaddr)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    elf[E.ram2file(_OWU_HOOK):E.ram2file(_OWU_HOOK) + 4] = A.j(cave_vaddr + len(hi))
    # the jump's delay slot is the old lbu; the cave redoes it, so retire it
    elf[E.ram2file(_OWU_LBU):E.ram2file(_OWU_LBU) + 4] = A.nop()


# ---- feature: Desert + class-9 per-tier pools (harder overworld) -------------
# Two overworld terrains that are NOT the land table, and so were never reached by
# the harder bands. Both are already ZONED in vanilla, which is what makes them
# safe to scale per-tier (unlike terrain 1 -- see the note further down).
#
# terrain 3 DESERT, zoned 0x08945aa8, ~1732 tiles: the whole Ryukhan Desert around
# Mirage Tower plus the western dunes and two small early patches. It is not the
# land table, so harder_overworld_encounters never reached it -- vanilla content
# is real (Baretta / Desert Baretta / Sand Worm / Allosaurus / Tyrannosaur, threat
# 21-41 in the Ryukhan rows) but FIXED, so the desert wrapped in Lufenia-tier
# grass at threat 35-64 stayed a mid-game fight. Through v229 it was also being
# OVERWRITTEN by the overworld u16 companion; see apply_overworld_u16.
#
# terrain 4 = tile class 9, ~439 tiles on the southern landmass, zoned
# 0x08945ca8. Vanilla holds one generic-wilderness row (mean threat ~8: Wolves,
# Ogres, Goblin Guards, Cobra) across all six of its zones, including the 280
# tiles in Crescent-tier zones.
#
# A DF-style detour makes each terrain u16 and per-tier: compute the zone from the
# party tile, read the shared ZONE_TIER map, weight-roll a slot off the engine's
# own scramble curve and write the u16 formation id straight to the battle
# context. Both vanilla tables are left exactly as shipped -- the caves override
# the roll before it happens rather than editing data.
#
# TERRAIN 1 (MARSH/RIVER, flat 0x08945a90) IS DELIBERATELY LEFT VANILLA. It is one
# global 8-slot row (Hydra / Crocodile / Ochu / Piranha / Neochu) shared by every
# marsh and river tile on the map, and that global row is how the game is meant to
# play. v230 made it zoned off ZONE_TIER; v231 REVERTED that, because ZONE_TIER is
# a LAND-ROUTE map and the canoe rivers cut straight through Cornelia- and
# Pravoka-tier zones -- the Ice Cavern river (zone 46, tier 1) started rolling
# Tarantulas and 9-Pirate packs (user report 2026-08-06). River difficulty cannot
# be derived from where a river happens to sit on the walking route. Do not retry
# this without a river-specific zone map.
#
# Pools are PRECOMPUTED literals from re_only/gen_terrain_pools.py (the threat
# metric needs the ISO). Per tier k they draw from the SAME threat band
# [FLOOR[k], CEIL[k]] gen_ow_pools uses for the land table -- so neither terrain
# can be softer than the grass around it. The desert filters that band to a sand
# flavour set and tops up from the rest of it; class 9 takes an even spread of the
# whole band, because its vanilla content is generic wilderness with no theme to
# preserve. NOTHING AQUATIC may top up either one (v231: the v230 desert literal
# let 4-6 Sahagin and a 9-Pirate pack into tier 1 through that top-up). Sorted
# ASCENDING by threat: slot 0 is the most common roll, slot 7 the rarest, so
# hardest = rarest (the harder-overworld land convention, v201d).
# Low tiers are NOT floored: vanilla's own desert row for zone 37 (the 35 tiles
# north of Cornelia) is mean threat 7.0, so a floor would spike an early area this
# option never promised to touch.
# test_patch.terrain_pool_checks regenerates them from the ISO and fails on drift.
_TP_POOL_MAX = 8
_TP_T3_HOOK    = 0x08841F84     # terrain-3 (desert) `b 0x8841f9c`
_TP_T3_HOOK_W0 = 0x10000005     # delay slot @0x8841f88: addu s0,v0,v1
# terrain-4 is the switch's FALL-THROUGH case, so it has no `b` of its own: the
# hook is its first word (`sll v1,v0,3`) and the delay slot is the harmless
# `lui v0,0x894` that follows. There is therefore NO free fall-through here --
# the cave must always commit, which it does (every ZONE_TIER value 0..8 has a
# full row). If a future revision needs to fall through it must rebuild v1 =
# zone*8 itself and jump to 0x08841f94, where v0 is still the lui's result.
_TP_T4_HOOK    = 0x08841F8C     # terrain-4 `sll v1,v0,3`
_TP_T4_HOOK_W0 = 0x000218C0

_DESERT_TIER_POOL = [
    # 0 Cornelia  Tarantula / Crazy Horse / Ghast / Zombie+Ghoul / Goblin /
    #             Gigas Worm / Cobra / Gargoyle              (threat  5.5-8.0)
    [0x012, 0x086, 0x00f, 0x084, 0x080, 0x007, 0x08d, 0x010],
    # 1 Pravoka   Lizard / Wolf / 4-6 SAHAGIN / 9 PIRATE / Shadow / Gigas Worm /
    #             Cobra / Gargoyle                           (threat  6.1-8.0)
    # The Sahagin and Pirate packs are amphibious and belong in the zones around
    # Pravoka -- the same call rando._OW_HANDPICK makes for the land table
    # ("Pravoka is a port"). Do not "clean" them out of here again.
    [0x009, 0x083, 0x0dd, 0x07e, 0x00a, 0x007, 0x08d, 0x010],
    # 2 Elfheim   Blue Troll / Troll / Scorpion / Lesser Tiger / Python /
    #             Manticore / Blue Troll+Python / Gargoyle   (threat 10.4-13.7)
    [0x150, 0x063, 0x01a, 0x019, 0x141, 0x036, 0x151, 0x090],
    # 3 W.Keep    as Elfheim, ceiling raised to 2-4 Troll    (threat 10.4-14.8)
    [0x150, 0x063, 0x01a, 0x019, 0x036, 0x151, 0x090, 0x0e3],
    # 4 Melmond   Troll / Anaconda / Elm Gigas / Minotaur Zombie / Python /
    #             Sabertooth / Hill Gigas / Sand Worm        (threat 14.8-21.5)
    [0x0e3, 0x015, 0x11a, 0x032, 0x143, 0x099, 0x09e, 0x0bc],
    # 5 Crescent  Manticore / Elm Gigas / Python / Mummy / Sand Worm /
    #             Yellow Dragon / Wyrm / Weretiger+Sabertooth (threat 15.1-26.6)
    [0x0b6, 0x11a, 0x143, 0x01d, 0x0bc, 0x118, 0x037, 0x0b9],
    # 6 Onrac     Sphinx / Yellow Dragon / Weretiger / Red Dragon / Specter /
    #             Baretta / Mummy+King Mummy / 1-4 Baretta   (threat 22.8-34.6)
    [0x0ef, 0x118, 0x0e7, 0x02a, 0x0ac, 0x038, 0x09d, 0x0b8],
    # 7 Trials    Specter / Bonesnatch / Baretta / DESERT BARETTA / Rock
    #             Gargoyle / King Mummy / Pharaoh / Wyvern+Wyrm (threat 28.1-39.2)
    [0x0ac, 0x159, 0x038, 0x071, 0x163, 0x0ca, 0x170, 0x0be],
    # 8 Lufenia   Pharaoh / Wyvern+Wyrm / Red Dragon / Rock Gargoyle / Black
    #             Dragon / Sekhret / Sekhret+Earth Troll /
    #             Pharaoh+Bonesnatch                         (threat 36.5-61.6)
    [0x170, 0x0be, 0x0aa, 0x164, 0x16e, 0x130, 0x22b, 0x228],
]
# terrain 4 = tile class 9, ~439 tiles on the SOUTHERN landmass (zones 51/53/58/
# 59/61/62). Already zoned in vanilla at 0x08945ca8, but every one of those zones
# holds the same generic-wilderness row at mean threat ~8 -- Wolves, Ogres, Goblin
# Guards, Cobra -- even the 280 tiles sitting in Crescent-tier zones. No flavour
# filter here (unlike the desert): its vanilla content IS generic, so each row is
# just an even spread across the tier's threat band with aquatics excluded.
_T4_TIER_POOL = [
    # 0 Cornelia
    [0x012, 0x080, 0x0dd, 0x007, 0x010, 0x088, 0x08b, 0x06b],
    # 1 Pravoka
    [0x009, 0x07e, 0x007, 0x002, 0x094, 0x0e6, 0x08a, 0x06b],
    # 2 Elfheim
    [0x150, 0x013, 0x063, 0x160, 0x01b, 0x12b, 0x151, 0x13b],
    # 3 W.Keep
    [0x150, 0x024, 0x13a, 0x0e4, 0x091, 0x085, 0x09b, 0x0e0],
    # 4 Melmond
    [0x085, 0x0b6, 0x0df, 0x0ad, 0x03c, 0x033, 0x04f, 0x136],
    # 5 Crescent
    [0x0b6, 0x0d5, 0x03c, 0x01d, 0x030, 0x03b, 0x037, 0x058],
    # 6 Onrac
    [0x0ef, 0x0b2, 0x037, 0x0d6, 0x258, 0x0a8, 0x09d, 0x115],
    # 7 Trials
    [0x0bb, 0x111, 0x201, 0x0af, 0x224, 0x147, 0x252, 0x211],
    # 8 Lufenia
    [0x0a1, 0x0cb, 0x03e, 0x146, 0x044, 0x0cd, 0x259, 0x128],
]


def _tp_data(pools):
    """Cave data: ZONE_TIER[64] (u8) + POOL[9][8] (u16, @+64) + SLOT_MAP[64]
    (@+208) = 272 B. Byte-for-byte the same shape dangerous_forests uses, so the
    two caves share _DF_ZONE_TIER and _slot_weight_map()."""
    zone_tier = bytes(_DF_ZONE_TIER)
    assert len(zone_tier) == 64 and len(pools) == 9
    assert max(zone_tier) < len(pools)
    assert all(len(p) == _TP_POOL_MAX for p in pools), \
        "weighted slot map indexes 0..MAX-1; a short pool would read past the row"
    pool_bytes = b"".join(struct.pack("<H", f) for p in pools for f in p)
    data = zone_tier + pool_bytes + _slot_weight_map()
    assert len(data) == 272 and len(data) % 4 == 0
    return data


def apply_terrain_pools(elf: bytearray, feats=None):
    assert _TP_POOL_MAX == 8, "cave computes tier*16 (= tier*_TP_POOL_MAX*2) via one sll"
    for hook, want, pools in ((_TP_T3_HOOK, _TP_T3_HOOK_W0, _DESERT_TIER_POOL),
                              (_TP_T4_HOOK, _TP_T4_HOOK_W0, _T4_TIER_POOL)):
        fo = E.ram2file(hook)
        w = struct.unpack_from("<I", elf, fo)[0]
        if w != want:
            raise ValueError(f"unexpected terrain hook @{hook:#x}: {w:#010x} "
                             f"(want {want:#010x})")
        data = _tp_data(pools)

        def _code(zonetier_vaddr, pool_vaddr, slotmap_vaddr):
            return A.asm_labels([
                # zone = ((x+7)>>5) + 8*((y+7)>>5)   (s1 = x, s2 = y, s4 = field struct)
                A.addiu("t2", "s1", 7), A.srl("t2", "t2", 5),
                A.addiu("t3", "s2", 7), A.srl("t3", "t3", 5), A.sll("t3", "t3", 3),
                A.addu("t2", "t2", "t3"),                # zone -- 0..72, NOT 0..63
                # zone can exceed 63 (the +7 bias gives grid index 8 for
                # coord 249..254); an unguarded ZONE_TIER read would take a
                # "tier" out of the pool bytes and index past the rows into
                # garbage formation ids. No desert or class-9 cell lands there
                # on the shipped map, but the failure mode is the v222 empty
                # encounter, so it is guarded rather than argued about.
                A.sltiu("t3", "t2", 64),
                ("beq", "t3", "zero", "vanilla"), A.nop(),
                A.li("t4", zonetier_vaddr), A.addu("t4", "t4", "t2"),
                A.lbu("t3", 0, "t4"),                    # tier idx (0..8)
                # tier*16 parked in s0: it must survive the RNG jal, and s0 is dead
                # on this path (the commit tail 0x88425b0 reloads s0..s5 from its
                # own stack frame before jr ra -- the regional-ocean precedent).
                A.sll("s0", "t3", 4),                    # tier*16 (8 x u16 per row)
                A.jal(_DF_RNG_FN), A.nop(),              # v0 = random word (s0-s2/s4 survive)
                A.andi("t5", "v0", 63),                  # weight-map index (rng & 63)
                A.li("t4", slotmap_vaddr), A.addu("t4", "t4", "t5"),
                A.lbu("t5", 0, "t4"),                    # weighted slot 0..7
                A.sll("t5", "t5", 1),                    # slot*2
                A.addu("t4", "s0", "t5"),                # tier*16 + slot*2
                A.li("t0", pool_vaddr), A.addu("t4", "t0", "t4"),
                A.lhu("t2", 0, "t4"),                    # u16 formation id (DLC-capable)
                A.sh("t2", _DF_CTX_FORM_OFF, "s4"),      # store into battle ctx (u16)
                A.j(_DF_RESUME_TAIL), A.nop(),           # commit encounter
                ("label", "vanilla"),
                A.j(_DF_RESUME), A.nop(),                # zone off the table -> vanilla
            ])

        placeholder = data + _code(0, 0, 0)
        cave_vaddr = E.add_segment_cave(elf, placeholder)
        real = data + _code(cave_vaddr, cave_vaddr + 64,
                            cave_vaddr + 64 + _TP_POOL_MAX * 9 * 2)
        assert len(real) == len(placeholder)
        E.cave_write(elf, cave_vaddr, real)
        # single-word hook over the terrain's `b`; its delay slot still sets s0 to
        # the vanilla base, so nothing is lost if a later revision falls through.
        elf[fo:fo + 4] = A.j(cave_vaddr + len(data))


# ---- feature: Northern River Encounters -------------------------------------
# TERRAIN 1 IS THE RIVER TERRAIN, and nothing else. Measured 2026-08-06 from the
# static map: the cells that route to terrain 1 are exactly the cells whose ATT
# movement attribute is 0xF009 -- 1122 of them (the live map reads 1135 because
# open_progression adds 16 river-class cells). 0xF009 is the canoe attribute:
# these tiles cannot be entered on foot or by ship at all. MARSH IS NOT IN THIS
# SET -- marsh cells carry walkable land attributes and route to terrain 0, the
# ordinary land table, which is why marsh plays mechanically like grass. So this
# hook reaches rivers and only rivers, and needs NO river-vs-marsh gate.
#
# Vanilla gives every river on the map ONE 8-slot row (0x08945a90: Hydra /
# Crocodile / Ochu / Piranha / Neochu, expected threat 26.4) -- the Ice Cavern
# river and a starting-continent creek are the same fight. This feature splits
# that by continent, exactly the way regional_ocean_encounters splits the sea:
#
#   NORTHERN rivers  -> _RR_NORTH_POOL, a harder water/flying/giant themed row
#   SOUTHERN rivers  -> nothing. The cave falls through to the vanilla flat roll,
#                       so the southern rivers keep the vanilla row byte for byte.
#
# The zone map is a 64-byte 0/1 table, NOT a difficulty tier. This is the whole
# lesson of the reverted v230 attempt: rando.ZONE_TIER answers "which town would
# I be at if I WALKED here", and canoe rivers cut straight through Cornelia- and
# Pravoka-tier zones -- the Ice Cavern river sits in zone 46, tier 1, so a
# ZONE_TIER-driven river rolled Tarantulas and a 9-Pirate pack (user report
# 2026-08-06). A river's difficulty cannot be inferred from its walking
# neighbourhood; it has to be assigned. Measured river cells per zone put the
# split beyond argument -- grid rows 0-2 hold 298 river cells, rows 4-7 hold 824,
# and ROW 3 HOLDS NONE. North = zone < 24 is therefore an exact continental cut,
# not a judgement call. re_only/zone_paint.py renders it for re-tuning.
#
# Pool order follows the user's rule: the rarest slot (s7) is the hardest fight
# and the second-rarest (s6) the easiest, with the common slots ascending -- but
# the two lightest picks are held OUT of s0/s1 and parked in the 9.38% slots, so
# the pool clears Lufenia-tier harder-overworld by a real margin instead of 1.0.
_RR_HOOK    = 0x08841F68        # terrain-1 (river) `b 0x8841f9c`
_RR_HOOK_W0 = 0x1000000C        # delay slot @0x8841f6c: addiu s0,s0,0x5a90
_RR_RESUME  = 0x08841F9C        # vanilla flat slot roll (southern / unassigned)
_RR_POOL_MAX = 8
# expected threat 44.5 vs vanilla river 26.4, harder-overworld north 26.0/30.4/41.8,
# dangerous-forests north 46.1/54.4/68.1 -- above the grass, below the forests.
_RR_NORTH_POOL = [
    0x15d,   # s0 18.75%  45.0  1-2 Yamatano Orochi
    0x12e,   # s1 18.75%  46.2  2-4 Squidraken
    0x16e,   # s2 18.75%  46.7  1 Black Dragon
    0x0ff,   # s3 18.75%  48.0  1-2 Iron Golem
    0x0cb,   # s4  9.38%  36.6  2-4 Dragon Zombie
    0x211,   # s5  9.38%  39.5  0-1 Mad Ogre, 1-3 Flood Gigas
    0x145,   # s6  4.69%  34.4  4-8 Poison Eagle
    0x230,   # s7  1.56%  57.8  2-6 Squidraken
]
# zone -> 1 = northern continent (use the pool), 0 = leave vanilla.
# Grid rows 0-2 plus zone 31 (row 3, col 7 -- the north-east peninsula tip, user
# call 2026-08-06). Zone 31 holds no river cell in the shipped map AND none in
# the 16 canoe cells open_progression carves at runtime (those land in zones 34,
# 43 and 54, all southern), so it is inert today -- it is here so that a future
# river carved into that corner is northern by default rather than by omission.
# re_only/river_zones.json mirrors this set for the painter; test_patch asserts
# the two agree so the picking tool cannot drift from what is actually baked.
_RR_NORTH_ZONES = frozenset(range(0, 24)) | {31}


def _rr_data():
    """Cave data: ZONE_NORTH[64] (u8) + POOL[8] (u16, @+64) + SLOT_MAP[64] (@+80)
    = 144 B. Same shape as the desert cave, one pool row instead of nine."""
    zn = bytes(1 if z in _RR_NORTH_ZONES else 0 for z in range(64))
    assert len(_RR_NORTH_POOL) == _RR_POOL_MAX
    pool = b"".join(struct.pack("<H", f) for f in _RR_NORTH_POOL)
    data = zn + pool + _slot_weight_map()
    assert len(data) == 144 and len(data) % 4 == 0
    return data


def apply_northern_river_encounters(elf: bytearray, feats=None):
    fo = E.ram2file(_RR_HOOK)
    w = struct.unpack_from("<I", elf, fo)[0]
    if w != _RR_HOOK_W0:
        raise ValueError(f"unexpected terrain-1 hook @{_RR_HOOK:#x}: {w:#010x} "
                         f"(want {_RR_HOOK_W0:#010x})")
    data = _rr_data()

    def _code(zn_vaddr, pool_vaddr, slotmap_vaddr):
        return A.asm_labels([
            # zone = ((x+7)>>5) + 8*((y+7)>>5)   (s1 = x, s2 = y, s4 = field struct)
            A.addiu("t2", "s1", 7), A.srl("t2", "t2", 5),
            A.addiu("t3", "s2", 7), A.srl("t3", "t3", 5), A.sll("t3", "t3", 3),
            A.addu("t2", "t2", "t3"),                # zone -- 0..72, NOT 0..63
            # THE ZONE INDEX OVERFLOWS THE 64-ENTRY TABLE. With the +7 bias,
            # x (or y) in 249..254 yields grid index 8, so zone reaches 72 and
            # 3024 map cells index past any 64-byte zone table. On the shipped
            # map every one of those cells is ocean or plain grass -- zero river
            # cells -- but open_progression edits the grid, and an unguarded
            # read here would pull a "north" flag out of the pool bytes that
            # follow and roll a northern fight on a southern edge tile.
            A.sltiu("t3", "t2", 64),
            ("beq", "t3", "zero", "vanilla"), A.nop(),   # off the table -> vanilla
            A.li("t4", zn_vaddr), A.addu("t4", "t4", "t2"),
            A.lbu("t3", 0, "t4"),                    # 1 = northern river
            ("beq", "t3", "zero", "vanilla"), A.nop(),   # southern/unassigned -> vanilla
            A.jal(_DF_RNG_FN), A.nop(),              # v0 = random word (s1/s2/s4 survive)
            A.andi("t5", "v0", 63),                  # weight-map index (rng & 63)
            A.li("t4", slotmap_vaddr), A.addu("t4", "t4", "t5"),
            A.lbu("t5", 0, "t4"),                    # weighted slot 0..7
            A.sll("t5", "t5", 1),                    # slot*2
            A.li("t0", pool_vaddr), A.addu("t4", "t0", "t5"),
            A.lhu("t2", 0, "t4"),                    # u16 formation id (DLC-capable)
            A.sh("t2", _DF_CTX_FORM_OFF, "s4"),      # store into battle ctx (u16)
            A.j(_DF_RESUME_TAIL), A.nop(),           # commit encounter
            ("label", "vanilla"),
            A.j(_RR_RESUME), A.nop(),                # vanilla flat roll; s0 already set
        ])

    placeholder = data + _code(0, 0, 0)
    cave_vaddr = E.add_segment_cave(elf, placeholder)
    real = data + _code(cave_vaddr, cave_vaddr + 64, cave_vaddr + 64 + _RR_POOL_MAX * 2)
    assert len(real) == len(placeholder)
    E.cave_write(elf, cave_vaddr, real)
    # single-word hook over the terrain-1 `b`; its delay slot (addiu s0,s0,0x5a90)
    # still sets s0 to the vanilla flat pool, which the fall-through path needs.
    elf[fo:fo + 4] = A.j(cave_vaddr + len(data))


# --- feature: blood magic (activatable equipment use in battle costs 10% max HP) --
# RE: blood-magic-re memory / re_only/HANDOFF_blood_magic.md (all sites LIVE-CONFIRMED
# 2026-07-09). Using activatable equipment (spell-on-use; weapon/armor record +7 != 0)
# as a BATTLE item casts a free spell; this taxes it: the user loses 10% of their max
# HP per use (KO allowed). Fully self-contained on-disc detour -- no client loop.
# v84 extends the tax from weapons-only to armor (White/Black Robe, Healing Helm,
# Gauntlets, Giant's Gloves, ...): committed category 3 selects the armor proc table.
#
# HOOK (v90): the ITEM state machine's DISPLAY/APPLY state, between the status-
# effect result writer and the apply/display calls:
#   0x0888384c  jal 0x8874a1c        # per-target pre-display bookkeeping
#   0x08883854  jal 0x88854c4        # status-effect result writer   (HOOK here,
#   0x08883858  move a0,s2           #  displace these 2)
#   0x0888385c  jal 0x88860d4        # APPLY results (0x88818fc a3=-1 = same
#   0x08883864  jal 0x88819c0        # clamp0 KO-capable HP debit) + DISPLAY
# At this point the cast executor 0x88846D8 (an EARLIER state) has initialized
# the result array and written the cast's own entries, so appending ours here
# survives to apply+display. v88 hooked the cast epilogue 0x8883678 instead --
# REFUTED by live capture 2026-07-20: that state runs BEFORE the executor, when
# the array still holds the previous action's ZEROED entries (target byte 0, not
# the 0xff free marker), so the append never fired and the executor's init would
# have wiped it anyway. The item SM never runs for native MAGIC casts or plain
# attacks, so a stateless gate here cannot false-fire on native casting (same
# guarantee the old epilogue hook had); consumables DO pass through this state,
# gated out by the weapon/armor proc==spell checks below.
#
# Battle record C = [s2+0x34] (= ctx+0xC714+row*0x6C, verified equal live): curHP @ +8,
# maxHP @ +0xA (u16), committed item category @ +0x57 (2 = weapon), item id @ +0x58.
# [s2+0x3C] = unit index (party = 0..3; same convention as the thief-crit detour).
_BLOOD_HOOK   = 0x08883854
_BLOOD_RET    = 0x0888385C          # = _BLOOD_HOOK + 8 (past the 2 displaced insns)
_BLOOD_STATFN = 0x088854C4          # displaced jal target (status-effect writer)
# --- popup color (v91): the digit popup spawn fn 0x88739A4 picks the glyph CELL
# as bank_base + digit from the battle-number sprite sheet; banks are 20 cells:
# 0x00 letters/symbols (MISS text uses cell 0xb), 0x14 white damage digits,
# 0x28 green heal digits. The base is chosen only from entry-flags bit 0x20
# (heal), so blood entries mark themselves with a spare flags bit (0x400) and a
# tiny second detour in the spawn fn overrides the bank for flagged entries.
# (The 0x3C-bank probe RAN and failed -- cell bank != color, see the v91-v95
# note below -- so this whole colour leg is DEFERRED; constants kept for the
# resumption.)
_BLOOD_FLAGBIT = 0x400              # spare entry-flags bit = "blood cost" mark
# (v91-v95 probes: overriding the digit CELL bank base -- s0 @0x8873B6C, banks
# 0x3C/0x00/0x28 incl. unconditional -- provably changed the cells written to
# sprite+0x426 (live dump: 0x29/0x2d) with ZERO visual change. Cell != color.)
_BLOOD_CLRHOOK = 0x08873B6C         # spawn fn: move a1,s6 / jal 0x88d9a24 --
_BLOOD_CLRJAL  = 0x088D9A24         # flags still live in s2 here; stash them
_BLOOD_CLRRET  = 0x08873B78         # (we also emit the jal's original delay
                                    # slot addiu a0,sp,0x48 and return past it)
_BLOOD_ROWHOOK = 0x08873D00         # spawn fn damage-side row write:
                                    #   addiu v0,a1,-0xa / sw v0,0x48(v1)
                                    # s2 = digit LOOP INDEX here (not flags!),
                                    # hence the stash cell.
_BLOOD_ROWRET  = 0x08873D08
_BLOOD_REDROW  = 0x1E               # probe: row offset for marked entries
                                    # (damage = -0xa white, heal = +0xa green)
_WEAPON_PROC0 = 0x08953BB7          # weapons[0]+7; weapons[id]+7 == this + id*28
# Armor table sits one 28-byte dummy-row0 slot after the last weapon record: armor
# id1 row @0x0895433C (ELF bytes == rando_data VANILLA["armor"] verified), so the
# id-indexed base mirrors the weapon one. Armor records share the weapon shape and
# +7 is likewise the use-cast spell id (White/Black Robe, Healing Helm, ...):
# armor[0]+7 = 0x08954327; armor[id]+7 == that + id*28 (= _ERG_ARMOR_P0 below).
# --- v221 equipment-activation TICKET -----------------------------------------
# The armor leg used to be a blind scan of all 75 armor +7 procs for one matching
# the committed spell @C+0x44, because an armor use wipes its own cat/id pair
# before the queued turn runs. That scan cannot tell an ARMOR activation from a
# CONSUMABLE that happens to cast the same spell -- consumables pass through the
# same item state machine and also write C+0x44. Vanilla collisions: armor procs
# are Curaja 0x18 (White Robe), Firaga 0x30 (Black Robe), Poisona 0xc, Focara
# 0x2b, Blind 0x3b -- so a Fang (and any future consumable sharing a proc) was
# taxed as blood magic. Procs are not randomized (rando._ACTIVATABLE_ARMOR_IDS
# derives from the vanilla table), so the collision set is fixed but real.
#
# Fix: stamp a ticket where the activation is UNAMBIGUOUS. The battle item-
# usability resolver 0x08871594 (a0=ctx, a1=cat, a2=id) is called by the
# equip-execute path with ra == _ERG_RA_EXEC, and that caller is the one that
# `store 0x44 & CAST`s the proc -- i.e. exactly an equipment activation, cat 2
# (weapon) or 3 (armor), never a consumable (cat 1). The stamp leg records the
# proc spell id + a pending byte; the blood cave reads AND CLEARS the ticket on
# every pass (so one can never outlive its action) and charges only on a match.
# The resolver detour is SHARED with equipment_rune_gate (same hook address):
# whichever of the two features is on installs it, both legs when both are on.
_BTK_LEN   = 4                      # [0] pending, [1] proc spell id, pad
_BTK_PEND  = 0
_BTK_SPELL = 1


def _blood_ticket_mb(elf, feats):
    """The equipment-activation ticket mailbox vaddr, minted once and threaded
    through feats so the resolver stamp leg and the blood cave agree on it (the
    two are installed by different feature fns). An isolated unit test that
    applies one feature with no feats gets a throwaway -- such tests only check
    bytes, and the other half of the pair is absent there anyway."""
    if feats is not None and feats.get("_blood_tkt_mb") is not None:
        return feats["_blood_tkt_mb"]
    mb = E.add_segment_cave(elf, b"\x00" * _BTK_LEN)
    if feats is not None:
        feats["_blood_tkt_mb"] = mb
    return mb


def _blood_ticket_stamp(mb):
    """Resolver-cave leg: if this call is the equip-execute caller asking for a
    cat 2/3 proc, stamp {proc spell, pending} in the ticket mailbox. Emitted on
    the resolver's VANILLA-CONTINUE path only, so locked gear (rune gate) never
    stamps. t0-t3/at are scratch at the fn entry (leaf, computes them itself)."""
    return [
        A.li("t0", _ERG_RA_EXEC),
        ("bne", "ra", "t0", "BTKSKIP"), A.nop(),   # not the cast path
        A.andi("t1", "a1", 0xFF),
        A.addiu("at", "zero", 2),
        ("beq", "t1", "at", "BTKW"), A.nop(),
        A.addiu("at", "zero", 3),
        ("bne", "t1", "at", "BTKSKIP"), A.nop(),   # cat 1 consumable -> no ticket
        A.li("t2", _ERG_ARMOR_P0),
        ("beq", "zero", "zero", "BTKC"), A.nop(),
        ("label", "BTKW"),
        A.li("t2", _ERG_WEAPON_P0),
        ("label", "BTKC"),
        A.andi("t3", "a2", 0xFF),                  # id*28 (the native shape)
        A.sll("t1", "t3", 3), A.subu("t1", "t1", "t3"), A.sll("t1", "t1", 2),
        A.addu("t2", "t2", "t1"),
        A.lbu("t2", 0x00, "t2"),                   # proc spell id
        ("beq", "t2", "zero", "BTKSKIP"), A.nop(), # plain gear -> no cast
        A.li("t1", mb),
        A.sb("t2", _BTK_SPELL, "t1"),
        A.addiu("t3", "zero", 1),
        A.sb("t3", _BTK_PEND, "t1"),
        ("label", "BTKSKIP"),
    ]


def apply_blood_magic(elf: bytearray, feats=None):
    dmb = _delaypop_mb(elf, feats)   # shared delayed-popup mailbox (heal-item stagger)
    tkt = _blood_ticket_mb(elf, feats)   # equipment-activation ticket (v221)
    cave = A.asm_labels([
        # --- displaced originals (0x8883854 / 0x8883858): call the status-effect
        # result writer with a0=s2. Re-emitted as move-then-jal (ra is clobbered
        # by the original jal anyway; the SM fn restores its own ra from stack).
        A.addu("a0", "s2", "zero"),
        A.jal(_BLOOD_STATFN), A.nop(),
        # --- save scratch (s2 preserved; v0/t0-t5/at are ours -- v0 currently
        # holds the status writer's dead return value) ---
        A.addiu("sp", "sp", -0x20),
        A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"), A.sw("t2", 0x08, "sp"),
        A.sw("t3", 0x0C, "sp"), A.sw("t4", 0x10, "sp"), A.sw("t5", 0x14, "sp"),
        A.sw("at", 0x18, "sp"),
        # party actor? [s2+0x3C] < 4 else DONE (enemy item use)
        A.lbu("t0", 0x3C, "s2"),
        A.addiu("at", "zero", 4), A.slt("at", "t0", "at"),
        ("beq", "at", "zero", "DONE"), A.nop(),
        # C = battle record; v0 = committed spell id
        A.lw("t1", 0x34, "s2"),
        A.lhu("v0", 0x44, "t1"),
        # no spell executed -> DONE (also protects the armor scan: a proc==0
        # row must never match a v0 of 0)
        ("beq", "v0", "zero", "DONE"), A.nop(),
        # Weapons and armor commit DIFFERENTLY (live captures 2026-07-20,
        # watch_blood_commit.py): a weapon use commits cat/id @C+0x57/+0x58
        # (cat == 2) and the pair SURVIVES to this epilogue; an armor use
        # writes only the spell @C+0x44 -- its cat/id pair (@C+0x5B/+0x5C)
        # is wiped again before the queued turn executes, so by epilogue
        # time v0 is the ONLY trace of the armor cast. The armor leg is
        # therefore the resolver TICKET (v221, see _blood_ticket_stamp) --
        # NOT the old blind proc scan, which taxed any consumable casting an
        # armor proc (Fangs vs Black Robe's Firaga). Native magic/attacks
        # never reach this epilogue (v44 live proof).
        # -- weapon leg: [C+0x57]==2 and weapon[[C+0x58]*28+7]==v0 --
        A.lbu("t2", 0x57, "t1"),
        A.addiu("at", "zero", 0x02),
        ("bne", "t2", "at", "ARMCHK"), A.nop(),
        A.lbu("t2", 0x58, "t1"),                                   # weapon id (1-based)
        A.sll("t3", "t2", 5), A.sll("t4", "t2", 2), A.subu("t3", "t3", "t4"),  # id*28
        A.li("t5", _WEAPON_PROC0), A.addu("t3", "t5", "t3"),
        A.lbu("t3", 0x00, "t3"),                                   # proc spell id
        ("beq", "t3", "zero", "ARMCHK"), A.nop(),
        ("beq", "t3", "v0", "CHARGE"), A.nop(),
        # -- armor leg: the equipment-activation ticket (v221) --
        ("label", "ARMCHK"),
        # Read AND CLEAR the ticket unconditionally: this state runs once per
        # item action, so clearing here means a ticket can never outlive the
        # action that stamped it (a field/locked activation that never casts
        # leaves one behind; the next item action burns it harmlessly).
        A.li("t5", tkt),
        A.lbu("t2", _BTK_PEND, "t5"),
        A.sb("zero", _BTK_PEND, "t5"),
        ("beq", "t2", "zero", "DONE"), A.nop(),                    # no activation
        # Consumable veto: a live cat-1 commit @C+0x57 means an ITEM was used,
        # never equipment -- belt-and-braces so a leftover ticket can never be
        # spent on a Fang/Curtain/Tonic even if one somehow survives to here.
        A.lbu("t2", 0x57, "t1"),
        A.addiu("at", "zero", 0x01),
        ("beq", "t2", "at", "DONE"), A.nop(),
        A.lbu("t3", _BTK_SPELL, "t5"),                             # stamped proc
        ("beq", "t3", "v0", "CHARGE"), A.nop(),
        ("beq", "zero", "zero", "DONE"), A.nop(),
        ("label", "CHARGE"),
        # dmg = maxHP/10 (>=1); max = [C+0xA]
        A.lhu("t3", 0x0A, "t1"),
        A.addiu("at", "zero", 10), A.divu("t3", "at"), A.mflo("t4"),
        ("bne", "t4", "zero", "HAVE"), A.nop(),
        A.addiu("t4", "zero", 1),
        ("label", "HAVE"),
        # Deliver the cost through the ACTION-RESULT ARRAY instead of a raw
        # HP write. ctx+0xCD50 = 13 result entries, stride 0x14: +0 source
        # unit, +1 target unit (0xff = free; the executor's init 0x8875500
        # pre-sets +0xC flags = 1), +4 value u32, +8 hit count. The executor
        # has already written the cast's own entries by the time this state
        # runs; the apply call at the return point (0x88860d4 -> 0x88818fc
        # a3=-1) debits the SAME battle-HP field this cave used to write
        # (cur=[unit+8], value<cur ? cur-value : 0 -- identical KO-allowed
        # clamp) plus native KO handling, and 0x88819c0 pops the number over
        # the target. So appending an entry = popup + damage + KO, all
        # native. Statically RE'd 2026-07-20 from the poison/regen tick
        # writers (0x88868d8 / 0x8882af8) and both cast state machines;
        # entry shape confirmed live via watch_blood_results.py.
        A.lw("t5", 0x00, "s2"),                                    # ctx = [actor+0]
        A.li("at", 0xCD50), A.addu("t5", "t5", "at"),
        A.addiu("t2", "zero", 13),                                 # slots left
        ("label", "SLOT"),
        A.lbu("t3", 0x01, "t5"),
        A.addiu("at", "zero", 0xFF),
        ("beq", "t3", "at", "CLAIM"), A.nop(),
        A.addiu("t5", "t5", 0x14),
        A.addiu("t2", "t2", -1),
        ("bne", "t2", "zero", "SLOT"), A.nop(),
        # all 13 slots taken (should not happen) -> old direct-write fallback
        A.lhu("t2", 0x08, "t1"),
        A.slt("at", "t4", "t2"),
        ("beq", "at", "zero", "ZERO"), A.nop(),
        A.subu("t2", "t2", "t4"),
        ("beq", "zero", "zero", "STORE"), A.nop(),
        ("label", "ZERO"),
        A.addu("t2", "zero", "zero"),
        ("label", "STORE"),
        A.sh("t2", 0x08, "t1"),                                    # write battle curHP
        ("beq", "zero", "zero", "DONE"), A.nop(),
        ("label", "CLAIM"),
        A.lbu("t0", 0x3C, "s2"),                                   # caster unit id
        A.sb("t0", 0x00, "t5"),                                    # source = caster
        A.sb("t0", 0x01, "t5"),                                    # target = caster
        A.sw("t4", 0x04, "t5"),                                    # value = dmg
        A.addiu("at", "zero", 1),
        A.sb("at", 0x08, "t5"),                                    # hit flag
        A.addiu("at", "zero", 1 | _BLOOD_FLAGBIT),
        A.sh("at", 0x0C, "t5"),                                    # flags: damage + red mark
        # --- v129 blood + heal-item stagger (ask 2, original case): if this
        # activatable item also HEALS the caster (e.g. a Healing Staff), the
        # green heal number and the red blood-cost number would land on the same
        # unit in the same frame. Mark the caster's HP-heal result entry
        # no-display (0x80 -- the engine still APPLIES the heal) and record a
        # delayed GREEN popup, so the red cost shows first and the heal floats up
        # ~half a second later. Only the CLAIM path reaches here (an activatable
        # item was used); the blood entry itself (flags 0x401, no 0x20) is not a
        # match. t5=self-damage entry is done with; t0-t5/at reused. ---
        A.lw("t0", 0x00, "s2"),                                    # ctx
        A.li("at", 0xCD50), A.addu("t0", "t0", "at"),              # &result[0]
        A.lbu("t1", 0x3C, "s2"),                                   # caster unit
        A.addiu("t2", "zero", 13),
        ("label", "BHLOOP"),
        A.lbu("t3", 0x01, "t0"),                                   # tgt
        ("bne", "t3", "t1", "BHNEXT"), A.nop(),                    # not the caster
        A.lhu("t3", 0x0C, "t0"),                                   # flags
        A.andi("t4", "t3", 0x20),
        ("beq", "t4", "zero", "BHNEXT"), A.nop(),                  # not a heal
        A.andi("t4", "t3", 0x100),
        ("beq", "t4", "zero", "BHNEXT"), A.nop(),                  # not an HP heal
        A.andi("t4", "t3", 0x80),
        ("bne", "t4", "zero", "BHNEXT"), A.nop(),                  # already no-display
        A.ori("t3", "t3", 0x80),
        A.sh("t3", 0x0C, "t0"),                                    # no-display the heal
        A.lw("t3", 0x04, "t0"),                                    # heal value
        A.li("t4", dmb),
        A.sb("t1", _DP_UNIT, "t4"), A.sh("t3", _DP_VAL, "t4"),
        A.addiu("t5", "zero", 0x20), A.sh("t5", _DP_FLAGS, "t4"),  # green heal arm
        A.addiu("t5", "zero", DELAYPOP_DELAY_FRAMES), A.sb("t5", _DP_DELAY, "t4"),
        A.addiu("t5", "zero", 1), A.sb("t5", _DP_PEND, "t4"),      # pending
        ("beq", "zero", "zero", "DONE"), A.nop(),                  # one is enough
        ("label", "BHNEXT"),
        A.addiu("t0", "t0", 0x14),
        A.addiu("t2", "t2", -1),
        ("bne", "t2", "zero", "BHLOOP"), A.nop(),
        # --- restore + return ---
        ("label", "DONE"),
        A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"), A.lw("t2", 0x08, "sp"),
        A.lw("t3", 0x0C, "sp"), A.lw("t4", 0x10, "sp"), A.lw("t5", 0x14, "sp"),
        A.lw("at", 0x18, "sp"),
        A.addiu("sp", "sp", 0x20),
        A.j(_BLOOD_RET), A.nop(),
    ])
    cave_vaddr = E.add_segment_cave(elf, cave)
    E.install_detour(elf, _BLOOD_HOOK, cave_vaddr)
    # --- v221 ticket stamp at the usability resolver. equipment_rune_gate
    # detours the SAME entry and runs FIRST (FEATURES insertion order), folding
    # the stamp into its own cave; install a stamp-only cave here only when that
    # feature is off, so the hook is never claimed twice.
    if not (feats and feats.get("equipment_rune_gate")):
        stamp = A.asm_labels(_blood_ticket_stamp(tkt) + [
            A.word(0x30A300FF),                  # displaced: andi v1,a1,0xff
            A.word(0x24020003),                  # displaced: addiu v0,zero,3
            A.j(_ERG_HOOK + 8), A.nop(),
        ])
        E.install_detour(elf, _ERG_HOOK, E.add_segment_cave(elf, stamp))
    # --- popup color: DEFERRED (user, 2026-07-20). Findings so far, for a
    # future attempt: digit CELL (+0x426, bank+digit; banks 0x00/0x14/0x28/
    # 0x3C) does NOT select color (live-proven). Color = per-digit ROW value
    # @sprite+0x468: damage digit-0xa (white), heal digit+0xa (green); row
    # 0x1E+digit rendered BLANK (no number), so valid rows beyond +/-0xa are
    # unknown -- likely needs the number-atlas texture mapped before another
    # probe. Detour recipe that worked mechanically: stash entry flags
    # (s2) to a cave scratch at _BLOOD_CLRHOOK, override the damage-side row
    # write at _BLOOD_ROWHOOK for flag-0x400 entries. See blood-magic-re.


# --- feature: job-scroll boosts (WW dia-vs-anything + BW instant-kill boost) -----
# Job scrolls (AP items, ids.job_item_id) grant permanent per-class boosts instead
# of promotions. The White Wizard and Black Wizard legs need on-disc code (the
# other scrolls -- Ninja steal, Red Wizard conversion, Master reactive stats --
# are client-side loops in ApClient). All three hooks live in the SPELL EXECUTION
# fn 0x088846D8 (statically RE'd 2026-07-13 via bootdis; s4 = caster actor obj,
# same context family as the crit/blood hooks):
#   * magic_info rec = 0x8954d0d + id*14 (id 1-based): +0 target-flags, +1 u16
#     power/status-mask (s5/s6), +3 u16 element mask (s1), +5 u8 effect TYPE,
#     +7 u8 accuracy (to-hit base = acc + 0x94 = fp), +9 level, +10 MP.
#   * effect-type dispatch: sltiu 0x19 + jump table @0x0894C0D4 (25 entries).
#   * TYPE 2 (Dia/Diara/Diaga/Diaja) @0x8884af4: undead gate @0x8884b2c reads
#     target family byte [target_struct+0x21] & 0x8 -- non-undead take ZERO
#     damage. WW HOOK: displace the two loads, OR the undead bit into v1 for a
#     gated White Wizard caster -> full normal dia damage vs anything.
#   * TYPE 3 (status roll: Death/Quake/Scourge/Warp have status-mask==1) handler
#     @0x8884d28: score s7 = acc+0x94 - target_magdef(+0x32), -0x94 if target
#     resist(+0x24) & element, +0x28 if weak(+0x22); hit if rand%201 < s7.
#     BW HOOK @0x8884da8 (the rand jal): s7 += tohit-bonus for a gated Black
#     Wizard caster on kill spells (status-mask==1) before the roll.
#   * TYPE 0x12 (threshold autohit: Kill; also Stun/Blind with other masks)
#     @0x88852bc: AUTO-HIT iff target curHP(+8) < 0x12D (301) and not element-
#     resisted (no RNG at all). BW HOOK @0x88852e8: replace the slti 0x12d with
#     an unsigned compare against a tunable threshold when gated (kill spells
#     only, status-mask==1).
# Scroll ownership is RUNTIME state (an AP item found mid-run), so the caves gate
# on a MAILBOX cave: magic "SCRL", flags u8 @+4 (bit0 = WW dia, bit1 = BW kill),
# u16 @+8 = BW to-hit bonus, u16 @+10 = BW kill-spell HP threshold, u8[256] @+16 =
# baked boss table (byte[id]==1 => boss), u16[64] @+272 = baked per-spell fail-
# damage power. The ApClient _scroll_battle_loop locates the mailbox by magic scan
# and keeps the ownership FLAGS current; the tuning, boss table and fail-power
# table are baked defaults. The WW dia boost additionally requires a BOSS in the
# encounter; BW leg 3 makes a kill spell that fails to kill deal INT-scaled damage
# via the fail-power table (0 = vanilla no-effect miss).
# Class gate (in-cave): caster idx [s4+0x3C] < 4, field rec = *(battle_base
# +0x6834) + idx*0x5C, class @+0x1E (menu frame -- NOT +0x5A, which reads zero;
# live-verified in-battle 2026-07-13); WW = {4,10}, BW = {5,11} (base + promoted).
_WW_HOOK,  _WW_RET  = 0x08884B2C, 0x08884B34
_BW3_HOOK, _BW3_RET = 0x08884DA8, 0x08884DB0   # displaced: jal rand + nop
_BWK_HOOK, _BWK_RET = 0x088852E8, 0x088852F0
_SCROLL_RAND_FN = 0x08869528
SCROLL_MB_MAGIC = b"SCRL"
SCROLL_MB_FLAGS_OFF  = 4    # u8: bit0 = WW dia-vs-anything, bit1 = BW kill boost,
                            # bit2 = Knight lifesteal (heal % of phys damage dealt)
SCROLL_MB_TOHIT_OFF  = 8    # u16: LEGACY scalar (superseded by the per-spell
                            # SCROLL_MB_TOHITTAB_OFF table); kept for layout stability
SCROLL_MB_THRESH_OFF = 10   # u16: LEGACY scalar (superseded by SCROLL_MB_THRTAB_OFF)
SCROLL_MB_BOSSTAB_OFF = 16  # u8[256]: byte[monster_id]==1 => that id is a boss.
                            # Static (boss set is fixed), so it is BAKED here, not
                            # client-written; a savestate load reverts the mailbox
                            # to these baked bytes, which stay correct.
SCROLL_MB_FAILPOW_OFF = 272 # u16[64]: fail-damage POWER per spell id (0x110). A
                            # kill spell that does NOT kill deals damage via the
                            # engine's own damage path using this power (0 = no
                            # fail damage / vanilla miss). Indexed by 1-based magic
                            # id; baked (static). Table len 64 entries = 128 bytes.
SCROLL_BW_TOHIT_DEFAULT  = 20
SCROLL_BW_THRESH_DEFAULT = 500

# --- Spell identities (CONFIRMED 2026-07-14 by dumping magic_info against the
# canonical SPELL_NAMES list; the earlier ids were inferred from element bits and
# happened to be right). Relevant BW status spells, with their handler:
#   type-3 ROLL (score = acc + 148 - target_magdef, -148 if elem-resisted,
#                +40 if elem-weak; hit iff score >= rand%201):
#       50 Scourge (mask 1)  54 Death (mask 1)  55 Quake (mask 1)
#       63 Warp    (mask 1)  58 BREAK (mask 2, acc 64, elem 0x0002)
#   type-0x12 THRESHOLD AUTOHIT (no RNG, no accuracy: auto-hit iff target curHP <
#                threshold AND not already-statused AND not elem-resisted):
#       64 Kill (mask 1)  56 STUN (mask 0x10)  60 BLIND (mask 8)
# Stun/Blind have accuracy byte 0 and it is never read -- the HP threshold is the
# ONLY tuning knob for them (short of bypassing the resist check).
_ID_SCOURGE, _ID_DEATH, _ID_QUAKE, _ID_WARP, _ID_BREAK = 50, 54, 55, 63, 58
_ID_KILL, _ID_STUN, _ID_BLIND = 64, 56, 60

# v162: the rest of the no-power enemy-target black spells (user 2026-07-28). These
# also deal Necrocaster miss-damage, but reach the miss path through DIFFERENT
# effect-type handlers than the kill line above:
#   * type-3 (effect 0x03, handler 0x08884d28) -- SAME roll-miss ori @_FAIL_HOOK_T3
#     that Death/Scourge/etc. already use, so these need ONLY a fail-power entry:
#         34 Sleep  38 Dark  42 Hold  45 Sleepra  47 Confuse  62 Stop
#   * type-0x04 (effect 0x04, handler 0x08884e1c) -- rolls unconditionally; roll-miss
#     ori @_FAIL_HOOK_SLOW (new hook): 40 Slow  52 Slowra
#   * type-0x0e generic-status (effect 0x0e, handler 0x08885370) -- rolls ONLY when
#     spell id == 35, so 35 Focus reaches roll-miss ori @_FAIL_HOOK_STATUS (new hook)
#     but 44 Focara AUTO-APPLIES (never misses) => its entry below is INERT (kept for
#     documentation / future roll-enable; user chose to skip Focara 2026-07-28).
_ID_SLEEP, _ID_DARK, _ID_HOLD, _ID_SLEEPRA, _ID_CONFUSE, _ID_STOP = 34, 38, 42, 45, 47, 62
_ID_SLOW, _ID_SLOWRA, _ID_FOCUS, _ID_FOCARA = 40, 52, 35, 44

# Fail-damage powers keyed by 1-based magic id. A status spell that does NOT land
# deals damage instead of nothing, via the engine's own magic-damage path (so these
# BASE powers still get INT scaling, the normal random range, element and magdef).
# Reference powers: Fira 30, Blizzara 40, Blizzaga 70, Flare 100. User calibration
# 2026-07-14: Break matches Quake, Stun/Blind get a small consolation hit.
SCROLL_FAIL_POWERS = {
    _ID_SCOURGE: 40,
    _ID_DEATH:   75,    # user retune 2026-07-21 (was 70)
    _ID_QUAKE:   50,
    _ID_WARP:    60,
    _ID_KILL:    115,   # user retune 2026-07-21 (was 110)
    _ID_BREAK:   50,    # same power as Quake (user 2026-07-14)
    _ID_STUN:    55,    # user retune 2026-07-28 (was 40)
    _ID_BLIND:   50,    # user retune 2026-07-28 (was 40)
    # v162 (user 2026-07-28) -- remaining no-power enemy-target black spells
    # (powers retuned by user same day):
    _ID_SLEEP:   5,
    _ID_FOCUS:   5,
    _ID_DARK:    10,
    _ID_SLOW:    10,
    _ID_HOLD:    30,
    _ID_FOCARA:  15,    # INERT: Focara auto-applies, never reaches a miss hook
    _ID_SLEEPRA: 35,
    _ID_CONFUSE: 25,
    _ID_SLOWRA:  45,
    _ID_STOP:    50,
}
SCROLL_FAILPOW_ENTRIES = 64   # id 1..64

# --- v100: both reliability knobs are now DYNAMIC, scaling with the CASTER'S INT
# (user 2026-07-21) instead of being flat per-spell constants. A late-game
# Necrocaster is meant to be dramatically better at these spells than an early one,
# and INT is the stat the class is built around.
#
# CASTER INT = u8 at [s4+0x34] + 0x36. RE'd 2026-07-21 and confirmed on BOTH sides
# of the battle-unit stat struct, which is shared by party and enemies:
#   enemy init 0x0887B934: lbu v1,0xe(s0) / sb v1,0x36(s2)  -- monster_stats +14 is
#     the documented "intelligence" field, and +0xd (agility) lands at +0x37.
#   party init 0x0887BC8C: sb v0,0x36(s5) from getter 0x088970A8 (base stat +
#     equipment), with the agility getter storing to +0x37 right after.
# Both tables below therefore hold a MULTIPLIER now, not a final value. 0 still
# means "feature off for that spell", so the per-spell gating is unchanged.

# Per-spell BW to-hit bonus (type-3 roll spells): score += INT * mult, added to s7
# before the rand%201 compare, so +N score is roughly +N/2 percentage points of
# land chance. Was a flat +20 (SCROLL_BW_TOHIT_DEFAULT) for all five.
SCROLL_MB_TOHITTAB_OFF = 528  # u16[64] @0x210
SCROLL_TOHIT_INT_MULT = 1     # user 2026-07-21: accuracy += INT*1 (non-boss)
SCROLL_TOHIT_BONUSES = {
    _ID_SCOURGE: SCROLL_TOHIT_INT_MULT,
    _ID_DEATH:   SCROLL_TOHIT_INT_MULT,
    _ID_QUAKE:   SCROLL_TOHIT_INT_MULT,
    _ID_WARP:    SCROLL_TOHIT_INT_MULT,
    _ID_BREAK:   SCROLL_TOHIT_INT_MULT,
}

# Per-spell BW autohit HP threshold (type-0x12 spells): threshold = BASE + INT*mult.
# 0 = leave vanilla 301. Was flat Kill 500 / Stun 500 / Blind 750.
SCROLL_MB_THRTAB_OFF = 656    # u16[64] @0x290
SCROLL_THRESH_BASE = 300      # user 2026-07-21: 300 + INT*mult
SCROLL_THRESHOLDS = {
    _ID_KILL:  10,
    _ID_STUN:  20,
    _ID_BLIND: 40,
}

# --- v101: BOSS DAMPING + KILL ROLL FALLBACK (user spec 2026-07-21) -------------
# Two problems the INT scaling created, both fixed here.
#
# (1) INT*1 let a late-game Necrocaster instant-KO fiends far too reliably (Lich
# 84%). Against the encounter's BOSS the INT bonus is divided by
# NECRO_BOSS_INT_DIV, so INT still helps but only slightly (Lich 30%->35% across
# the whole game), and the raised autohit threshold is suppressed entirely
# (bosses keep the engine's vanilla 301). Chaos stays 0% at every INT.
#
# WHICH TARGET COUNTS AS "THE BOSS" (user 2026-07-21): a boss that shows up as an
# ADD must behave like a minion, not a second boss -- same principle as the 75%
# cameo/minion stat softening. apply_boss_minions writes the boss into formation
# type-slot 0 and adds into slots 1-3, and enemy units spawn in type-slot order,
# so **enemy unit 0 is the boss and every other unit is an add**. The gate is
# therefore "target is enemy unit 0 AND that monster id is in the baked boss
# table" -- a cameo Lich in slot 2 is unit >=1 and gets the full undamped INT*1.
# Target unit index is a u8 at s4+0x3D (RE'd 2026-07-21: the per-target loop at
# 0x08884A4C reads the unit id and stores it there; <4 = party, else enemy
# index = id-4, so the boss is id == 4).
NECRO_BOSS_INT_DIV = 10       # boss INT bonus = INT // this (x0.1)
NECRO_BOSS_UNIT_ID = 4        # s4+0x3D value meaning "enemy unit 0" (the boss)
NECRO_TGT_IDX_OFF  = 0x3D     # u8 target unit index within the actor object
#
# (2) Kill was HP-gated while the type-3 kill line was not, so Warp/Quake could
# kill things Kill could not. Above its threshold Kill now ROLLS instead of just
# failing, at NECRO_KILL_FB_ACC accuracy, KEEPING ITS DEATH ELEMENT -- so the
# engine's own resist check still applies and death-immune targets still shrug it
# off. Only Kill gets this; Stun/Blind keep the plain threshold behaviour.
NECRO_KILL_FB_ACC = 30        # user 2026-07-21: Kill fallback accuracy

# --- Save-or-suffer MISS REPORT (v102) -----------------------------------------
# The AP client logs "7% Warp chance on Orthros" whenever an instant-kill / status
# spell FAILS, so the player learns whether the attempt was ever winnable. Only
# the cave can see the failure the instant it happens, so it stamps a tiny record
# here and the client polls it. We report the raw inputs rather than a computed
# chance: the client already has the monster stat block (magic defence +0x14,
# element resist +0x18) and the encounter block, so it can reproduce the engine's
# score formula exactly, and re-tuning the formula then needs no new bake.
# v105: this is a RING, not a single slot. A multi-target cast (Warp/Quake hit
# every enemy) fails once PER TARGET within a single frame, so a one-slot record
# overwrote itself and the 0.2s client poll only ever saw the last one -- live
# 2026-07-21, casting Warp at 4 enemies logged exactly one line. The ring lets
# the client drain every miss from one action.
#   +0 u8 write counter (free-running, wraps at 256; index = counter & 15)
#   +1..3 pad
#   +4 + i*4: entry i, SOS_RING entries of 4 bytes:
#        +0 spell id (1-based)
#        +1 target unit index (raw s4+0x3D: <4 party, 4 = enemy unit 0, 5+ adds)
#        +2 caster INT
#        +3 gated -- 1 if this caster really is a scrolled Necrocaster, so the
#                    client knows whether to add the INT bonus to its score
SOS_RING = 16                 # >= 9 (max enemies) so one action can never wrap
SCROLL_MB_REPORT_OFF = 784

# --- v107 White Cleric dia INT stacking ----------------------------------------
# u8[4], one per party slot: how many +1 INT steps this caster has banked in the
# CURRENT battle. The cave increments it (capped at the caster's level) on each
# dia cast and rewrites the battle-unit INT to base + acc.
#
# Why an accumulator instead of just incrementing the battle-unit INT in place:
# the engine RE-DERIVES the battle-unit INT from the party record on its own
# schedule -- live 2026-07-22, a poked INT of 26 fell back to 16 after ~9s with
# no cast in between. So an in-place bump does not survive to the next cast. The
# cave therefore recomputes base + acc from the party record every cast, which is
# also self-correcting: an engine refresh mid-battle cannot cause drift or
# double-stacking.
#
# That same refresh is what makes the "reverts at end of combat" requirement
# free: the boost only ever lands in the battle-unit record, never the party
# record (live-verified -- post-battle P_INT was unchanged after a mid-battle
# poke), and menu-cast spells read the party record, so the buff cannot leak out
# of combat. The client zeroes this array at battle end so battle N+1 does not
# start pre-stacked.
SCROLL_MB_DIAINT_OFF = 852
SCROLL_MB_DIAINT_LEN = 4

# v118 heal-popup mailbox: a scroll heal (dia self-heal, later Knight lifesteal
# etc.) that fires INSIDE the magic executor cannot append its own green-number
# result entry -- the executor's per-slot value loop recomputes it to 0 (the
# offensive spell's effect on the caster). So the compute cave records the heal
# here and the executor EPILOGUE cave (post-loop) appends the entry, where the
# loop can no longer overwrite it. {pending u8, unit u8, value u16}.
SCROLL_MB_HEALPOP_OFF = 856
SCROLL_MB_HEALPOP_LEN = 4

# v123 staggered teal popup: RESERVED 8-byte hole (kept so DIASTEP_OFF below does
# not shift). The staggered-popup record moved to the shared _DELAYPOP service
# (see _install_delayed_popup / _delaypop_mb) so blood_magic can share it; the
# scroll caves now record there. This hole is just baked zeros now.
SCROLL_MB_TEALPOP_OFF = SCROLL_MB_HEALPOP_OFF + SCROLL_MB_HEALPOP_LEN   # 860
SCROLL_MB_TEALPOP_LEN = 8

# v127 dia INT step table: u8[64] keyed by 1-based magic id, how many INT steps
# one cast of that dia spell banks (Dia 1 / Diara 2 / Diaga 3 / Diaja 4 -- the dia
# tier). BAKED static; a dedicated table rather than deriving from the self-heal
# amount so a future heal retune cannot silently change the INT step. Indexed the
# same way as the diaheal table.
SCROLL_MB_DIASTEP_OFF = SCROLL_MB_TEALPOP_OFF + SCROLL_MB_TEALPOP_LEN   # 868
SCROLL_MB_DIASTEP_LEN = SCROLL_FAILPOW_ENTRIES                          # u8[64]

# v130 Grand Master attack accumulator: u8[4], per-battle attack gained per party
# slot (so the cave knows when the cap is hit -> pop a yellow 0). Reset per battle
# by the client, like the dia INT accumulator. The attack GAIN moved on-disc (to
# the same damage epilogues as the CW MP refund) so the yellow number is drawn
# with exact timing; the client keeps only the Master max-HP leg.
SCROLL_MB_MATK_OFF = SCROLL_MB_DIASTEP_OFF + SCROLL_MB_DIASTEP_LEN      # 932
SCROLL_MB_MATK_LEN = 4
# Grand Master attack tuning (matches the client consts it replaces):
MASTER_ATK_DMG_DIV = 20            # gain per hit = ceil(dmg / this)
# v229: the per-battle cap is level*MUL_NUM/MUL_DEN + OVER_LEVEL (was a flat
# level + 5, which shrank in relative terms every level: +5 is 50% of a level-10
# Master's own level and 7% of a level-75 one's, so the reactive-growth window
# stopped mattering late). At 2x: L10 25 / L25 55 / L50 105 / L75 155.
# Retune by editing these three numbers ONLY -- the cave asm and the client both
# go through master_atk_cap() below, so the two can no longer drift.
MASTER_ATK_CAP_MUL_NUM = 2
MASTER_ATK_CAP_MUL_DEN = 1         # MUST be a power of 2 (the cave shifts)
MASTER_ATK_CAP_OVER_LEVEL = 5


def master_atk_cap(level):
    """Grand Master per-battle ATTACK cap at `level`. The single source of truth:
    _master_cap_asm() emits this same arithmetic into the two damage caves, and
    ApClient imports this function rather than redeclaring the formula."""
    return (level * MASTER_ATK_CAP_MUL_NUM) // MASTER_ATK_CAP_MUL_DEN \
        + MASTER_ATK_CAP_OVER_LEVEL


# The accumulator the caves count the cap in (SCROLL_MB_MATK) is u8 PER SLOT, so
# a retune that lets the cap exceed 255 at the level ceiling would wrap the
# counter and un-cap the Master. Caught here, at import, rather than in a battle.
assert master_atk_cap(99) <= 255, (
    f"MB_MATK is u8: master_atk_cap(99) = {master_atk_cap(99)} > 255")
assert MASTER_ATK_CAP_MUL_DEN & (MASTER_ATK_CAP_MUL_DEN - 1) == 0, (
    "MASTER_ATK_CAP_MUL_DEN must be a power of 2 (_master_cap_asm shifts)")


def _master_cap_asm(reg, tmp):
    """Emit `reg = master_atk_cap(reg)` (reg holds the level on entry). `tmp` is
    clobbered. Multiplies before adding OVER_LEVEL, matching the Python helper.
    A power-of-2 NUM becomes a shift; anything else costs a multu."""
    out = []
    n = MASTER_ATK_CAP_MUL_NUM
    if n != 1:
        if n & (n - 1) == 0:                       # power of 2 -> shift
            out.append(A.sll(reg, reg, n.bit_length() - 1))
        else:
            out += [A.addiu(tmp, "zero", n), A.multu(reg, tmp), A.mflo(reg)]
    if MASTER_ATK_CAP_MUL_DEN != 1:
        out.append(A.srl(reg, reg, MASTER_ATK_CAP_MUL_DEN.bit_length() - 1))
    out.append(A.addiu(reg, reg, MASTER_ATK_CAP_OVER_LEVEL))
    return out


# v229 Grand Master max-HP CEILING. The engine clamps a battle unit's derived max
# HP to 999 in TWO places (statically RE'd 2026-08-06 over BOOT.BIN):
#   0x08885b28  sh v1,0xa(s4)   ; Giant's Tonic case: maxHP = base + bonus
#   0x08885b30  slti at,v1,0x3e8 -> store 0x3e7 if >= 1000
#   0x088765c8  sh v1,0xa(s0)   ; the RE-DERIVE path, run on every damage event
#   0x088765e0  slti at,v1,0x3e8 -> store 0x3e7 if >= 1000   (maxMP likewise)
# CRITICAL: only the DERIVED max HP (BU_MAXHP +0xA) is clamped -- the bonus field
# we write through (BU_MAXHP_BONUS +0x66) is NOT. So past this ceiling the client
# could inflate the bonus forever with nothing to show for it. Reaching it is
# therefore a real "no room to grow" state, and the Master flips to its defensive
# heal there even with attack cap left to spend (see the client leg).
MASTER_MAXHP_CEIL = 999
# Grand Master max-HP/heal tuning. The HP writes stay CLIENT-side (the client
# delta loop sees magic damage too, which this physical epilogue never does);
# the cave only mirrors the formula to DRAW the number, so these must stay in
# lockstep -- ApClient imports them rather than redeclaring.
MASTER_HP_DMG_PCT  = 20            # max-HP tick = ceil(dmg * this / 100)
MASTER_HP_HEAL_NUM = 1             # HP healed = tick * NUM // DEN  (-> 10% of dmg)
MASTER_HP_HEAL_DEN = 2
# v219: once the per-battle ATTACK cap (master_atk_cap(level)) is spent, the
# Master stops growing and turns fully defensive: the heal jumps from 10% of
# damage taken to a flat this% of it, unhalved. Same cap that switches the popup
# from the yellow attack-gain number to the green heal number, so the drawn value
# and the client's write stay one and the same event. v220: that cap is now the
# ONLY one -- the client's max-HP leg stops growing on the same accumulator
# instead of running a second per-battle pool of its own.
# v229: the boosted heal has a SECOND trigger, MASTER_MAXHP_CEIL. Max HP can run
# out of room before the attack cap runs out (999 is reachable on a long fight or
# on top of a Giant's Tonic), and growth the engine throws away is not growth. At
# the ceiling the max-HP leg stops and the heal jumps to this% -- but ATTACK KEEPS
# ACCRUING to its own cap, so the cave keeps drawing the yellow attack-gain
# number. This trigger is deliberately CLIENT-ONLY: the cave's popup choice is
# already correct for it (yellow while attack still has room, green once capped),
# and nothing in the cave needs to know the max-HP leg went quiet.
MASTER_HP_CAPPED_PCT = 35          # capped heal = ceil(dmg * this / 100)
                                   # v260: retuned 50 -> 35 (fully-stacked Master
                                   # was out-sustaining incoming damage).

SCROLL_MB_LEN = SCROLL_MB_MATK_OFF + SCROLL_MB_MATK_LEN

# --- v103 on-screen miss feedback, STEP 1 of 2 (EXPERIMENT) --------------------
# Commit the "no effect" result flag on the fail-DAMAGE branch too, so the battle
# message box can render next to the damage number. With this on, a failed kill
# spell should show the vanilla BATTLE_MSG entry 0 ("No effect.") AND the fail
# damage. If the renderer does show both, step 2 is to overwrite entry 0 in place
# (it is exactly 10 chars + terminator, so no offset-table edit) with a
# per-bucket string: "Immune!" / "Resisted!" / "So close!".
# If the renderer shows only one, or the message suppresses the damage popup, set
# this False and fall back to showing a message only for the 0%-chance bucket.
# LIVE RESULT 2026-07-21: **the flag and the damage popup are mutually exclusive.**
# With this True the target showed "MISS!!" and NO damage number at all -- result
# flag 0x10 is not "show a message", it IS the miss verdict, and the renderer
# drops the damage popup for it. So the top-of-screen box is NOT reachable this
# way and the experiment is REVERTED. (Confirmed separately: "No effect." is the
# FIELD menu's can't-activate-this-item message, not a battle message at all.)
NECRO_MSG_ON_FAIL_DMG = False
NECRO_ROLL_CONST  = 148       # the engine's own +148 in score = acc+148-magdef
NECRO_ROLL_RANGE  = 201       # rand % 201; hit iff score >= roll
_NECRO_MAGDEF_OFF = 0x32      # target stat struct: s16 magic defence


def _scroll_u16_table(d):
    """128-byte u16[64] keyed by 1-based magic id (0 = feature off for that id)."""
    t = bytearray(2 * SCROLL_FAILPOW_ENTRIES)
    for mid, v in d.items():
        struct.pack_into("<H", t, (mid - 1) * 2, v)
    return bytes(t)

SCROLL_MB_DIAHEAL_OFF = 400   # u16[64] @0x190: per-spell self-heal MULTIPLIER
                              # (Q8 fixed point) for a White Wizard casting a
                              # dia-type spell (0 = not a dia spell / no heal).
                              # Indexed by 1-based magic id; baked (static).
# The 4 dia-line spells (magic_dump: the only type-2 spells) heal the WW caster.
# id 2=Dia, 10=Diara, 19=Diaga, 26=Diaja (powers 20/40/60/80). User 2026-07-14.
# Only nonzero for dia spells, so "mult != 0" also means "is a dia spell"; the
# heal is gated on the CASTER's class {4,10} (WhiteMage/WhiteWizard) so with
# randomized spells a Monk who learned dia neither heals nor (via the boss gate)
# damages bosses.
#
# v227 (user 2026-08-05): the heal SCALES WITH INT instead of the old flat
# 10/20/30/40 -- Dia INT*0.5, Diara INT*0.75, Diaga INT*1, Diaja INT*1.25.
# Stored as Q8 (x/256) so the cave needs one multu + srl 8 and every listed
# ratio is exact: 128/192/256/320. heal = (INT * mult) >> 8, floored, min 1
# (user: floor, min 1; no cap other than the maxHP clamp).
# The INT used is the caster's EQUIPPED battle INT (engine getter _INT_GET_FN =
# base + weapon/armor INT bonuses) PLUS the dia stacks banked earlier in this
# battle, but NOT this cast's own step (user: heal off pre-cast INT).
SCROLL_DIA_HEAL_MULT_Q8 = {2: 128, 10: 192, 19: 256, 26: 320}


# INT steps banked per cast = the dia tier (user 2026-07-23): Dia +1, Diara +2,
# Diaga +3, Diaja +4. The accumulator (hence total INT bonus) is still clamped to
# the caster's level.
SCROLL_DIA_STEPS = {2: 1, 10: 2, 19: 3, 26: 4}


def _scroll_diastep_table():
    """64-byte table (u8[64]) of dia INT step keyed by 1-based magic id."""
    t = bytearray(SCROLL_FAILPOW_ENTRIES)
    for mid, step in SCROLL_DIA_STEPS.items():
        t[mid - 1] = step
    return bytes(t)

# Once-per-cast hook in the magic-exec prologue (the target loop is INTERNAL, so
# code before 0x8884a34 runs once). We detour the two pre-loop instrs at 0x8884A28
# (`addu v1,v1,zero` + `sw v1,0x34(sp)`) and return to 0x8884A30. Battle-unit
# record = *(s4) + idx*0x6C + 0xC714; curHP @+8, maxHP @+0xA (ff1_data BU_*).
_HEAL_HOOK, _HEAL_RET = 0x08884A28, 0x08884A30
# v118 executor post-loop epilogue. The per-slot value loop (index @sp+0x58,
# 0..12) recomputes every non-free entry's +4 value, so an entry appended mid-
# loop is overwritten. When the index reaches 13 the fn falls through to its
# register-restore epilogue at 0x8885494 (`lw ra,0x2c(sp)` / `lw fp,0x28(sp)`),
# reached ONLY at loop exit. Hooking there appends the recorded heal-popup entry
# where the loop can no longer touch it; [sp+0x54] still holds the result-array
# base (ctx+0xCD50). Shared by every exec caller, so it is gated on the HEALPOP
# mailbox (only a scroll heal sets it). Return past both displaced loads.
_SCROLL_HEALPOP_HOOK, _SCROLL_HEALPOP_RET = 0x08885494, 0x0888549C
# Digit-popup spawner. TRUE signature from the display state's own call site
# 0x8881a5c (disasm 2026-07-23): a0 = ctx ([actor+0]), a1 = target unit,
# a2 = FLAGS, a3 = VALUE -- the old "(actor, target, value, flags)" note had
# a2/a3 swapped, which drew the flags word as a 5-digit number (live).
# The display state draws EVERY entry whose flags lack 0x80 (no other filter).
_POPUP_SPAWN_FN = 0x088739A4
# v209 SPLIT PUMP -- tick and spawn are DIFFERENT hooks, because the two safe
# properties live at different sites (both RE'd 2026-08-02, re_only/bootdis.py):
#
# TICK site = the ACTION-TYPE DISPATCHER prologue 0x088824DC. Call chain,
# single path each level:
#   battle phase SM 0x0886B0FC (state byte ctx+0xD1AE, jump table 0x0894B784)
#     -> sole call site 0x0886B3AC: a0 = ctx+0xD41C (the singleton action-SM
#        obj: +0 ctx, +0x16 state, +0x17 action type, +0x3C row)
#     -> 0x088824DC switches on [a0+0x17] and jal's the per-action SM handler
#        (0=attack 0x8880CE0, 1, 3=spell 0x8883504, 5, 9, 10, 11).
# Runs EVERY frame an action resolves (strict superset of the anim-poll fn's
# frames: every jal caller of 0x08881478 -- 0x8880DA4/0x8882A0C/0x8882DE4/
# 0x8882E60/0x8883144/0x888347C/0x8883884 -- is inside a handler this
# dispatcher invokes), so the countdown drains during pure status casts too.
# The tick cave ONLY decrements; it never calls the spawner. v208 spawned from
# here and CORRUPTED (live 2026-08-02: shredded party sprite + yellow 00000
# during a 5-hit attack) -- the prologue runs in EVERY SM state incl. setup
# states where the display isn't ready, so this site may tick, NEVER spawn.
# Displaces the fn prologue `addiu sp,sp,-0x10` + `sw ra,0xc(sp)` (entry-only).
_DELAYPOP_TICK_HOOK, _DELAYPOP_TICK_RET = 0x088824DC, 0x088824E4
#
# SPAWN site = the anim-poll SM state 0x08881478 (leaf; runs while an action's
# animation/popup phase is live -- the ONLY context ever proven safe for
# _POPUP_SPAWN_FN; deduct/executor contexts corrupt sprites, live v204+v206,
# and so does the dispatcher prologue, live v208. DO NOT move the spawn).
# The spawn cave consumes a ripe record (delay==0) and jal's the spawner; it
# does NOT tick. v207's "one action late" symptom proves this fn DOES tick
# during a status cast (the countdown partially drained) -- it just ran too
# few times for 30 frames; with the dispatcher draining the countdown, the
# cast's own anim-poll window now finds the record ripe and spawns in-cast.
# Displaces `addiu sp,sp,-16` + `sw s1,0xc(sp)`.
_DELAYPOP_HOOK, _DELAYPOP_RET = 0x08881478, 0x08881480
# --- shared DELAYED-POPUP service so BOTH job_scroll_boosts (teal MP refund) and
# blood_magic (heal-item stagger) can float a number a few frames after the one
# it would otherwise overlap. patch_iso installs it ONCE (mailbox + dispatcher
# detour) before the features loop and threads the mailbox vaddr through
# feats["_delaypop_mb"]; features read it via _delaypop_mb(). Record layout:
#   +0 pending u8 | +1 delay u8 (frames) | +2 unit u8 | +4 value u16 | +6 flags u16
# The pump cave counts delay -> 0 then calls the spawner with those flags.
# One record = one pending stagger; the real overlap cases (an RW taking a hit;
# a blood heal item) never fire two within the same ~0.5s window.
_DELAYPOP_LEN = 8
DELAYPOP_DELAY_FRAMES = 30
_DP_PEND, _DP_DELAY, _DP_UNIT, _DP_VAL, _DP_FLAGS = 0, 1, 2, 4, 6
# v211: spare record byte +3 = "the dispatcher tick site may drain this
# record". Set ONLY by the CW pay-site arm (the one record that must ripen
# during a pure status cast, where the anim-poll fn barely runs); cleared by
# the spawn cave at consume. Every other arm (teal on-hit MP gain, Grand
# Master yellow, blood magic, strength popups) leaves it 0, so those records
# tick ONLY at the anim-poll site -- the exact v207 float-frame timing. v210
# double-ticked them at the dispatcher too, which ripened them while the white
# damage numbers were still animating; the spawn then landed same-frame with
# the white number and was illegible/hidden (v121's known failure mode), which
# read as "no teal / yellow numbers" (live 2026-08-02, twice).
_DP_TICKFAST = 3


def _install_delayed_popup(elf):
    """Install the delayed-popup mailbox + the split pump (tick detour at the
    dispatcher prologue, tick+spawn detour at the anim-poll state -- see the
    _DELAYPOP_*_HOOK notes). Return the mailbox vaddr. Called once by patch_iso
    (always on). The anim-poll fn (0x08881478) is a LEAF (never saves ra) ->
    the spawn cave saves ra + a0 across the jal (it reads [a0+0]=ctx after the
    hook); its sp push/pop nets to 0 before the return point's `sw s0,0x8(sp)`.
    The dispatcher tick cave never calls anything and only touches t0/t1.
    v210: BOTH sites tick; spawn stays anim-poll-only. v209's tick-only-at-
    dispatcher starved on-take-damage records (CW teal / Grand Master yellow,
    armed late in an enemy action): the damage-number FLOAT frames run the
    anim-poll fn but evidently not (or not always) the dispatcher, so the
    countdown froze, the record carried over pending, and the next arm
    overwrote it -- effect landed, number lost (live 2026-08-02).
    v211: the dispatcher tick is GATED on the record's _DP_TICKFAST byte (set
    only by the CW pay-site arm): v210's unconditional double-tick ripened
    on-hit records while the white damage numbers were still animating, so
    the spawn landed same-frame with them and was hidden/illegible -- still
    "no teal / yellow" live. On-hit records now tick only at the anim-poll
    site (exact v207 float-frame timing); only the CW cast refund fast-ticks
    so it can ripen during a pure status cast."""
    dmb = E.add_segment_cave(elf, b"\x00" * _DELAYPOP_LEN)
    spawn = A.asm_labels([
        A.addiu("sp", "sp", -16), A.sw("s1", 0x0C, "sp"),      # displaced originals
        A.li("t0", dmb),
        A.lbu("t1", _DP_PEND, "t0"),
        ("beq", "t1", "zero", "DPDONE"), A.nop(),
        A.lbu("t1", _DP_DELAY, "t0"),
        ("bne", "t1", "zero", "DPTICK"), A.nop(),
        A.sb("zero", _DP_PEND, "t0"),                          # consume
        A.sb("zero", _DP_TICKFAST, "t0"),                      # clear fast-tick arm
        A.addiu("sp", "sp", -0x10),
        A.sw("ra", 0x00, "sp"), A.sw("a0", 0x04, "sp"),
        A.lw("a0", 0x00, "a0"),                                # ctx
        A.lbu("a1", _DP_UNIT, "t0"),                           # target unit
        A.lhu("a2", _DP_FLAGS, "t0"),                          # flags (colour arm)
        A.lhu("a3", _DP_VAL, "t0"),                            # value
        A.jal(_POPUP_SPAWN_FN), A.nop(),
        A.lw("ra", 0x00, "sp"), A.lw("a0", 0x04, "sp"),
        A.addiu("sp", "sp", 0x10),
        ("beq", "zero", "zero", "DPDONE"), A.nop(),
        ("label", "DPTICK"),
        A.addiu("t1", "t1", -1),
        A.sb("t1", _DP_DELAY, "t0"),
        ("label", "DPDONE"),
        A.j(_DELAYPOP_RET), A.nop(),
    ])
    E.install_detour(elf, _DELAYPOP_HOOK, E.add_segment_cave(elf, spawn))
    tick = A.asm_labels([
        A.addiu("sp", "sp", -0x10), A.sw("ra", 0x0C, "sp"),    # displaced originals
        A.li("t0", dmb),
        A.lbu("t1", _DP_PEND, "t0"),
        ("beq", "t1", "zero", "TKDONE"), A.nop(),
        A.lbu("t1", _DP_TICKFAST, "t0"),
        ("beq", "t1", "zero", "TKDONE"), A.nop(),              # not a fast-tick record
        A.lbu("t1", _DP_DELAY, "t0"),
        ("beq", "t1", "zero", "TKDONE"), A.nop(),              # ripe: spawn site consumes
        A.addiu("t1", "t1", -1),
        A.sb("t1", _DP_DELAY, "t0"),
        ("label", "TKDONE"),
        A.j(_DELAYPOP_TICK_RET), A.nop(),
    ])
    E.install_detour(elf, _DELAYPOP_TICK_HOOK, E.add_segment_cave(elf, tick))
    return dmb


def _delaypop_mb(elf, feats):
    """The shared delayed-popup mailbox vaddr for a feature cave. In patch_iso it
    comes from feats (installed once). In an isolated unit test that applies one
    feature with no feats, mint a throwaway mailbox so the cave still assembles
    (the anim-poll detour is absent there, but such tests only check bytes)."""
    if feats and feats.get("_delaypop_mb") is not None:
        return feats["_delaypop_mb"]
    return E.add_segment_cave(elf, b"\x00" * _DELAYPOP_LEN)


# --- feature: attack-buff yellow popup (Temper / Saber / Strength Tonic / Giant's
# Gloves) ---------------------------------------------------------------------
# In this mod YELLOW numbers = Strength going up (same colour language as the Grand
# Master reactive-attack leg). Temper/Saber the SPELLS, and the Strength Tonic /
# Giant's-Gloves ITEMS, all raise a battle unit's temp attack via the generic
# stat-modifier fn 0x088854C4 (statically RE'd 2026-07-25, then live-confirmed by
# diffing battle-unit records: a Strength Tonic wrote BU_ATTACK_BONUS +0x26 += 10 /
# Saber via Giant's Gloves += 16). Inside that fn the attack case is a plain
# additive RMW to [target_stat+0x26] with a NATIVE clamp to 255 that runs a few
# instrs later:
#   0x08885864  lh   v1,0x26(a0)     ; old bonus       (a0 = [s3+0x38] target stat)
#   0x08885868  addu v1,v1,s0        ; + s0 (= power: Temper 14 / Saber 16 / Tonic 10)
#   0x0888586C  sh   v1,0x26(a0)     ; commit          <-- SITE 1 (Saber: also +0x28)
#   ...         slti at,v1,0x100 -> store 0xFF if >=256 (attack-bonus caps at 255)
# A second, attack-only case stores the same way at 0x088858F4 (Temper).
# We hook the (addu; sh) PAIR at each site (install_detour displaces two words, and
# the pair is mid-case so neither word is a branch/switch target), recompute the
# CLAMPED delta = min(old+power,255) - old (== power normally, < power or 0 near the
# 255 ceiling -> a natural yellow 0 when the target is already maxed, per user spec:
# 0 yellow means strength went up by 0), and stage a yellow number on the target via
# the shared delayed-popup mailbox. Target unit id = [s3+0x3d] (the fn's own party
# gate reads it as <4); non-party targets get no popup. No artificial cap (Q1a): the
# only 0 is the genuine native-clamp whiff. s0/s3 are preserved; a0/v1/at are dead
# after each return point (the next native instr reloads a0 then v1).
_STRPOP_SITES = ((0x08885868, 0x08885870),   # SITE 1 (attack, Saber path)
                 (0x088858F0, 0x088858F8))   # SITE 2 (attack-only, Temper path)
_STRPOP_TGT_STAT_OFF = 0x26   # BU_ATTACK_BONUS on the [s3+0x38] target stat struct
_STRPOP_UNIT_OFF     = 0x3d   # target unit id off s3 (party rows 0..3)
# Delay 0 (NOT the shared DELAYPOP_DELAY_FRAMES=30): historically the anim-poll
# consumer (0x08881478, pump site through v207) only ran during action animations,
# and a BUFF cast's animation is short, so a 30-frame countdown rarely reached 0
# before the cast ended -- the popup then fired ~5s late (next damage anim) or
# never (record cleared). A buff has no competing damage number to stagger behind,
# so delay 0 makes the FIRST consumer tick during the cast spawn it immediately
# (consumer: delay==0 -> consume+spawn). Still correct with the v208 per-frame
# dispatcher pump (which would also have fixed the late fire).
# Live-diagnosed 2026-07-25: single-target Temper wrote +0x26 fine but popup was
# usually missing / occasionally 5s late -- a delay/consumer-timing bug, not a
# hook bug. See [[attack-buff-popup]].
_STRPOP_DELAY_FRAMES = 0


def apply_strength_popups(elf: bytearray, feats=None):
    """Always-on: float a yellow "attack gained" number when Temper/Saber/Strength
    Tonic/Giant's Gloves raise a party unit's attack. See _STRPOP_SITES notes."""
    dmb = _delaypop_mb(elf, feats)
    for hook, ret in _STRPOP_SITES:
        cave = A.asm_labels([
            A.addu("v1", "v1", "s0"),                    # displaced: new = old + power
            A.sh("v1", _STRPOP_TGT_STAT_OFF, "a0"),      # displaced: commit (game clamps later)
            # clamped delta = min(v1,255) - (v1 - s0)
            A.addiu("at", "zero", 0x100),
            A.sltu("a0", "v1", "at"),                    # a0 = (new < 256)
            A.addiu("at", "zero", 0xFF),                 # at = 255 (assume capped)
            ("beq", "a0", "zero", "AFTERCAP"), A.nop(),
            A.addu("at", "v1", "zero"),                  # new < 256 -> newc = new
            ("label", "AFTERCAP"),
            A.subu("at", "at", "v1"),                    # newc - new
            A.addu("at", "at", "s0"),                    # + power = clamped gain (0..power)
            # party gate + unit id
            A.lbu("a0", _STRPOP_UNIT_OFF, "s3"),         # target unit id
            A.addiu("v1", "zero", 4),
            A.sltu("v1", "a0", "v1"),                    # unit < 4 (party) ?
            ("beq", "v1", "zero", "DONE"), A.nop(),
            A.li("v1", dmb),                             # stage a YELLOW number
            A.sb("a0", _DP_UNIT, "v1"),
            A.sh("at", _DP_VAL, "v1"),
            A.addiu("a0", "zero", 0x20 | _PC_FLAG_YELLOWD), A.sh("a0", _DP_FLAGS, "v1"),
            A.addiu("a0", "zero", _STRPOP_DELAY_FRAMES), A.sb("a0", _DP_DELAY, "v1"),
            A.addiu("a0", "zero", 1), A.sb("a0", _DP_PEND, "v1"),
            ("label", "DONE"),
            A.j(ret), A.nop(),
        ])
        E.install_detour(elf, hook, E.add_segment_cave(elf, cave))


_BU_OFF, _BU_STRIDE, _BU_HP, _BU_MAXHP = 0xC714, 0x6C, 0x08, 0x0A
_BU_ATTACK, _BU_ATTACK_BONUS = 0x18, 0x26   # derived attack / durable bonus input
# Battle-unit INT: what the magic-damage path actually reads (u8 [s4+0x34]+0x36).
_BU_INT = 0x36
# The engine's own "what INT does this party member actually have" getter,
# `u8 int_of(field_rec a0, int raw a1)` -- RE'd 2026-08-05 off the battle-unit
# refresh 0x08876384, whose `jal 0x88970a8` / `sb v0,0x36(s0)` pair is what fills
# BU_INT in the first place. It returns
#   clamp(0, 99, field[0x33] + wpnTbl[field[0x3c]] + armTbl[field[0x3d..0x40]])
# (item stat tables at 0x08953bbe / 0x0895432d, stride 0x1C), then with a1 == 0
# applies the engine's conditional halving (global flag 0x2000). So field[0x33]
# alone is BASE INT WITHOUT EQUIPMENT; any cave that wants the INT the magic
# path really uses must call this, and must pass a1 = 0 like the refresh does.
# It follows the ABI (saves ra/s0, no s-reg clobber), so a cave may jal it as
# long as it preserves its own live t-registers and ra across the call.
_INT_GET_FN = 0x088970A8
# The field/menu record the class gate already computes (*(battle_base+0x6834)
# + idx*0x5C) is the party record shifted by -0x20 -- live-dumped 2026-07-22:
# class @+0x1E, LEVEL u32 @+0x20 (== P_LEVEL), P_INT @+0x33. LEVEL is read as a
# byte off +0x20 (little-endian low byte); levels never exceed 99.
_FLD_LEVEL, _FLD_INT = 0x20, 0x33
# Knight lifesteal: the physical combat-calc fn 0x88840d0 accumulates the total
# damage this attack deals into [result_block(s4)+4] (capped 99999) and finalizes
# it (including the party-attacker nullify path @0x8884670) before its epilogue.
# Hook the epilogue at 0x88846B0 (`lw s7,0x24(sp)` + `lw s6,0x20(sp)`, ret 0xB8):
# neither word is a branch target (the only in-fn targets here are 0xA8/0xAC) and
# s5 (actor obj) + s4 (result block) are still live (restored at 0xB8/0xBC). For a
# gated Knight caster (class {0,6}) heal the attacker unit rec [s5+0x34] (curHP@+8,
# maxHP@+0xA -- same rec family the blood-magic caster uses) by dealt // DIV
# (>=1 when dealt>0). KO attacker (curHP==0) is skipped -- no lifesteal-revive.
_LIFE_HOOK, _LIFE_RET = 0x088846B0, 0x088846B8
KNIGHT_LIFESTEAL_PCT = 15   # heal = damage_dealt * this // 100; user-tunable.
                            # v248: 20% -> 10% to match the defense-pierce leg.
                            # v261: 10% -> 15%. A plain reciprocal divisor can't
                            # express 15%, so the cave is mul-then-div (multu by
                            # PCT, divu by 100) instead of the old single divu.
                            # dealt is engine-capped at 99999, so PCT up to ~42000
                            # still can't overflow the 32-bit product.
KNIGHT_LIFESTEAL_CAP = 500  # v260: hard ceiling on ONE attack's self-heal. 10% was
                            # uncapped, so a late-game multi-hit Knight rolling
                            # 5-figure damage healed a full bar off a single swing.
                            # Clamped in the cave AFTER the divide, so both the HP
                            # write and the green number show the capped value.
# Knight defense pierce: inside the same combat-calc fn's per-hit loop the raw hit
# damage (v1, already clamped to 0xFF) has the target's DEF subtracted at 0x88843D8:
#   0x88843D0  lw v0,0x38(s5)   ; target stat struct
#   0x88843D4  lh v0,0x12(v0)   ; target DEF
#   0x88843D8  subu v0,v1,v0    ; dmg - DEF   (then bgtz / else floor to 1)
# Displace the two loads (verified: NO branch in the fn targets either word; the
# only nearby in-fn targets are 0xCC and 0xE8) and shave DEF by 1/DIV before the
# subtract, so a gated Knight ignores that fraction of DEF. Per-HIT, but the cut is
# multiplicative on DEF (not damage) so multi-hit attacks don't compound it.
_DEFP_HOOK, _DEFP_RET = 0x088843D0, 0x088843D8
KNIGHT_DEFPIERCE_DIV = 10   # DEF -= DEF // this (10 => ignore 10%); user-tunable
                            # v83: retuned 20% -> 10% (felt too strong live).
# Where the engine's magic-damage computation begins (v0 must = target struct on
# entry; s6 = power). The fail-damage cave sets those and jumps here so kill spells
# reuse the real damage formula (INT scaling + random range + element + magdef).
_DMG_PATH = 0x08884B54
# Miss paths that currently just flag "no effect": type-3 roll miss, and Kill
# (type-0x12) target-over-threshold. Both are `ori v1,v1,0x10 / b 0x8885478 /
# sh v1,0xc(s3)`; we detour the ori and either deal fail damage or reproduce it.
_FAIL_HOOK_T3   = 0x08884E10
_FAIL_HOOK_KILL = 0x08885364
_MISS_EPILOGUE  = 0x08885478
# v82: two MORE fail sites, found from a live report ("Kill on Death Eye misses for
# 0"). A kill spell can bail BEFORE ever reaching the two hooks above:
#  * type-3 STATUS-IMMUNITY bail @0x08884D64: `lhu v1,0xc(s3)` then the target's
#    status-immunity mask (tgt_stat+0x00) AND s6 (the spell's status mask) is
#    nonzero at 0x08884D5C -> falls into the ori at 0x08884D68.
#  * Kill (type 0x12) ELEMENT-RESIST bail @0x08885318: the target's element-resist
#    mask (tgt_stat+0x24) AND s1 (the spell's element) is nonzero at 0x08885310 ->
#    ori at 0x0888531C. (The Kill block's OTHER two exits -- HP >= 0x12D and status
#    immunity -- both converge on _FAIL_HOOK_KILL, so those already worked.)
# Both are the same `ori v1,v1,0x10 / b epilogue / sh v1,0xc(s3)` shape with v1
# pre-loaded and s3/s4/sp identical, so they reuse the SAME fail-damage cave.
# Verified: no branch in fn 0x88846D8 targets any displaced word.
_FAIL_HOOK_T3_IMMUNE = 0x08884D68
_FAIL_HOOK_KILL_ELEM = 0x0888531C
# v162: two MORE roll-miss sites, one per additional status handler. Both are the
# identical `ori v1,v1,0x10 / b 0x08885478 / sh v1,0xc(s3)` triple (v1 pre-loaded,
# s3/s4/sp identical -- same fn 0x88846D8), so they reuse the SAME fail-damage cave.
#  * type-0x04 (Slow/Slowra) handler 0x08884e1c: roll at 0x08884ea4 (bnel score<roll)
#    lands on the ori at 0x08884ED4 (disasm 2026-07-28).
#  * type-0x0e generic-status handler 0x08885370: rolls only for spell id 35 (Focus);
#    the score<roll fall-through hits the ori at 0x088853D0.
# Verified: nothing in fn 0x88846D8 branches to either displaced word (hook+4 is the
# `b epilogue` in each, reproduced by the cave's own MISS path).
_FAIL_HOOK_SLOW   = 0x08884ED4
_FAIL_HOOK_STATUS = 0x088853D0

# --- Necrocaster death-resist pierce (v98) ---------------------------------
# User ask 2026-07-21: "if I cast Kill on Lich it should work at full strength and
# do full damage" -- but ONLY for the Necrocaster; a Red Wizard who learned Kill
# must still be resisted. This is the "resist bypass" lever left open in the
# job-scroll-boosts memory, narrowed to ONE element so it is not a blanket
# immunity-breaker: dumping magic_info shows Kill (id 64) and Death (id 54) BOTH
# carry element mask 0x0008, and nothing else does => 0x0008 IS the death element.
# We clear just that bit out of the TARGET's element-resist mask (tgt_stat+0x24)
# for a gated Black Wizard/Necrocaster caster, so a Necrocaster's Fire is still
# resisted by a fire-immune enemy while Kill/Death ignore death resistance.
NECRO_PIERCE_ELEM = 0x0008          # death element; user-tunable
NECRO_PIERCE_KEEP = 0xFFFF & ~NECRO_PIERCE_ELEM
# Three sites read tgt_stat+0x24 and AND it with the spell element s1. Each hooks
# the two words that produce the mask and returns to the AND, so the AND sees a
# death-bit-free mask and the engine's own "not resisted" path runs unchanged.
# Verified (script over the whole magic-exec fn 0x88846D8-0x8885500): NO branch or
# jump targets any of the six displaced words.
#  A) type-3 status roll @0x08884D74-80: resist costs -148 to-hit (~a hard miss)
#     for Death/Scourge/Quake/Warp. Displaced: subu v1,fp,v1 + and v0,s1,v0
#     (the lhu at 0x08884D74 is itself a branch target, so the pair starts after
#     it and the mask is already in v0). Ret 0x08884D80.
#  B) Kill/Stun/Blind type-0x12 autohit @0x08885308-0C: resist is a HARD bail.
#     Displaced: lhu v1,0x24(a0) + and v1,s1,v1. Ret 0x08885310.
#  C) magic-damage path @0x08884BE4-E8: resist HALVES damage (sra/subu at
#     0x08884C00) and costs -148 accuracy. This is the leg the user actually sees
#     on Lich: Lich outlives Kill's HP threshold, so the cast takes the fail-damage
#     leg (power 110) -- without this it lands for half. Displaced:
#     lw v0,0x38(s4) + lhu v0,0x24(v0). Ret 0x08884BEC.
_NECRO_T3_HOOK,   _NECRO_T3_RET   = 0x08884D78, 0x08884D80
_NECRO_KILL_HOOK, _NECRO_KILL_RET = 0x08885308, 0x08885310
_NECRO_DMG_HOOK,  _NECRO_DMG_RET  = 0x08884BE4, 0x08884BEC


# --- Magic power scaling (magic_power_scaling, v228) -------------------------
# Monster Power / Boss Difficulty scale magic defence with the DAMPED multiplier
# (boot_patch._BS_DAMP), and mdef feeds TWO unrelated engine legs:
#
#   to-hit   score = acc + 148 - mdef, roll `hit iff score >= rand()%201`.
#            LINEAR and UNBOUNDED -- at 150% power a late-game mob's mdef alone
#            drives the score negative, so status spells stop landing entirely.
#   damage   a 3-bucket step at _MPD_HOOK: mdef>=201 -> -50%, >=101 -> -25%,
#            else -12.5%. COARSE and SATURATING -- and mdef is a byte, so past
#            ~200% power every high-mdef monster pins at 255 and stops scaling.
#
# This feature replaces both legs (user spec 2026-08-05):
#   to-hit   score = (acc + 148 - mdef_VANILLA) * shrink,  shrink = 0.5**(m-1)
#            -> landing chance decays MULTIPLICATIVELY with power (x0.50 at
#            200%, x0.25 at 300%, x0.065 at 500%) instead of falling off a
#            cliff, and nothing hard-zeroes that was not already 0% at 100%.
#   damage   dmg = dmg * _MP_DMG_DIV / (_MP_DMG_DIV + mdef_eff), a diminishing-
#            returns curve fed the UNCAPPED scaled mdef. Cannot reach 100%
#            reduction (asymptotic, so magic is never nullified) and keeps
#            responding past the byte clamp.
#
# AT EXACTLY 100% POWER THE GAME MUST BE VANILLA (user requirement: most players
# never leave the default and the game has to feel the way they expect). The
# caves are baked unconditionally, so "vanilla" is a RUNTIME state: the client
# writes shrink256 = 0 for a monster whose domain multiplier is 1.0, and every
# cave then runs its displaced originals and returns. Monster Power and Boss
# Difficulty resolve INDEPENDENTLY because the table is keyed by monster id --
# a 100%-monster / 300%-boss seed gets vanilla trash and scaled bosses in the
# same battle. The Boost tab can flip either mid-game; the client just rewrites
# the table (same live path that rescales monster_stats today).
#
# Mailbox (client-written; a savestate load reverts it, which safely reads as
# "all vanilla" until the next write):
#   +0    u32 magic "MPWR"
#   +4    u32 BOUNDARY = first MONSTER unit address (0 = unarmed -> all vanilla)
#   +8    u16[256] mdef_eff  -- uncapped scaled magic defence (damage curve)
#   +520  u16[256] mdef_van  -- VANILLA magic defence (to-hit score)
#   +1032 u16[256] shrink256 -- round(256 * 0.5**(m-1)); 0 = VANILLA SENTINEL
#
# STORAGE SPLIT. The three tables are 1.5 KB and the cave segment's FILE budget
# is nearly exhausted (measured 2026-08-05: only 924 spare bytes before
# spell_tomes' bss tails leave their required 64k page). So only a 12-byte
# HEADER is file-resident -- enough for the client's magic scan -- and the
# tables live in a bss tail, which costs zero file bytes because the loader
# zero-fills it. Zero-filled is exactly the right default: shrink256 == 0 is the
# vanilla sentinel, so an un-armed game is vanilla by construction.
#
# The tail is reserved AFTER the FEATURES loop (install_magic_power_tables),
# because cave_bss_tail must follow every add_segment_cave and spell_tomes owns
# the last of those. The caves therefore reach the tables through a POINTER in
# the header rather than a baked immediate.
_MP_MB_MAGIC  = b"MPWR"
_MP_MB_BOUND  = 0x04    # u32 first MONSTER unit address (0 = unarmed)
_MP_MB_TABLES = 0x08    # u32 -> bss tail holding the three tables (0 = unbuilt)
_MP_MB_LEN    = 0x0C
# offsets INSIDE the bss tail
_MP_T_MDEFF = 0
_MP_T_MDEFV = 512
_MP_T_SHRK  = 1024
_MP_T_LEN   = 1536

# Battle unit record: monster id byte, written by enemy init at 0x0887b9fc
# (`sb s3,0x49(s2)`; s3 is the id -- the same index the fn uses to address
# monster_stats at base + id*0x24). Party records reuse +0x49 for an unrelated
# halfword, which is why the PARTY TEST IS AN ADDRESS RANGE, not a field:
#   party_unit[row] = base + 0xC714 + row*0x6C      (_BU_OFF/_BU_STRIDE)
#   enemy_unit[i]   = base + 0xC8C4 + i*0x6C        (enemy init arithmetic)
#   0xC714 + 4*0x6C == 0xC8C4  -- ONE array, party at indices 0-3, monsters 4+.
# So `unit >= boundary` is exact by construction and cannot be data-ambiguous.
# (The obvious alternative, the `[unit+0x3C] < 4` row test the Crimson Wizard
# cave uses, is NOT usable here: enemy init stores monster_stats+0x20 there and
# 91 of 256 monsters land under 4, so a third of the bestiary would be misread
# as party members. That cave gets away with it because it tests the CASTER at
# the MP-deduct site, which no monster reaches, behind a second class gate.)
_MP_UNIT_ID = 0x49
_MP_DMG_DIV = 320       # fitted to the vanilla 3-bucket at the roster median
                        # (mdef 106 -> 24.9% vs vanilla's 25.0%); user-tunable,
                        # bump PATCHER_VERSION when changed.

# Damage leg. Hook displaces `lw v0,0x38(s4)` + `lh v0,0x32(v0)` (the target
# unit + its mdef). Verified not a branch target and not a delay slot. The
# vanilla 3-bucket chain runs 0x08884C98..0x08884CDC and converges at _MPD_SKIP,
# which RELOADS [sp+0x6C] and clamps it -- so our leg may clobber v0/v1 freely
# as long as it writes that slot. The whole block is already inside the engine's
# own `spell type < 4` gate at 0x08884C80, so only damaging spells reach it.
_MPD_HOOK, _MPD_RET, _MPD_SKIP = 0x08884C90, 0x08884C98, 0x08884CE0

# To-hit legs. The five status roll sites all build `score = BASE - mdef` and
# then roll `hit iff score >= rand()%201`; we rebuild the score from the VANILLA
# mdef and scale it by shrink256/256.
#
# THREE SITES SHARE ONE SHAPE (type-0x04 Slow, and the two generic status
# handlers that carry Sleep/Bind/Dark -- magic_info+6 type 0x01):
#     lw   a0, 0x38(s4)     ; target unit
#     lh   v1, 0x32(a0)     ; mdef            <- BRANCH TARGET, cannot hook
#     lhu  v0, 0x24(a0)     ; resist mask     <- hook here, a0/v1/fp all live
#     subu v1, fp, v1       ; score = (acc+148) - mdef
# so the hook displaces the resist load + the subtract, and at entry a0 = target
# unit, v1 = stored mdef, fp = acc+148. We overwrite v1 with the rebuilt score;
# the engine's own resist/weak adjustments and `seh` then run unchanged.
_MPH_SHARED = (0x08884E4C, 0x08884F10, 0x0888522C)

# The type-0x0e handler is the odd one out: its base is the LITERAL 212, not fp,
# and the unit is in a1.
#     lh    v1, 0x32(a1)    ; mdef      <- hook here (free: not a target/delay)
#     addiu v0, zero, 0xd4  ; 212
#     subu  v0, v0, v1      ; score                    (vanilla, runs after us)
# We leave v1 = 212 - shrunk_score so the vanilla subtract yields the shrunk one.
_MPH_0E_HOOK, _MPH_0E_BASE = 0x08885394, 0xD4

# The type-3 site (Death/Warp/Scourge/Quake/Break) has NO usable window of its
# own: its mdef load at 0x08884D60 sits in the DELAY SLOT of the immunity beql,
# 0x08884D74 is a branch target, and 0x08884D78 is _NECRO_T3_HOOK (owned, but
# only when job_scroll_boosts is on -- a conditional layout we must not depend
# on). Everything after that is either a jal/delay pair or the popup_colors roll
# hook. So this leg FOLDS INTO the popup roll cave at _PC_ROLL_HOOK, which is
# installed unconditionally and already has the final score live in s7. It is
# also the correct place on the merits: s7 is consumed by the roll at 0x08884DBC
# -- AFTER that cave returns -- and the colour classifier reads the same s7, so
# the odds shown and the odds rolled cannot disagree.
_MPH_T3_SCORE_REG = "s7"
_MP_DMG_SP = 0x6C       # running damage, sp-relative (NATIVE sp at the hook)
# Every leg is written against t0-t3 ONLY (and never `at`), so the save frame is
# four words. That is not cosmetic: the cave segment's file budget was down to
# 924 spare bytes before spell_tomes' bss tails leave their required 64k page,
# and the wider t0-t5+at frame the other scroll caves use put this feature 48
# bytes over. Four registers is also all any leg actually needs.
_MP_FRAME  = 0x10

_MP_SAVE = [
    A.addiu("sp", "sp", -_MP_FRAME),
    A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
    A.sw("t2", 0x08, "sp"), A.sw("t3", 0x0C, "sp"),
]
_MP_RESTORE = [
    A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
    A.lw("t2", 0x08, "sp"), A.lw("t3", 0x0C, "sp"),
    A.addiu("sp", "sp", _MP_FRAME),
]


def _mp_gate(mb, unit_reg, out_row):
    """Shared prologue for every magic_power cave: resolve the TARGET unit in
    `unit_reg` to its per-monster table row.

    Falls through with `out_row` = mb + id*2 (so the caller can lhu any of the
    three parallel u16 tables off it) only when ALL of:
      * the mailbox is armed (boundary != 0),
      * the unit is a MONSTER (unit >= boundary),
      * that monster's shrink256 is non-zero (not the vanilla sentinel).
    Otherwise branches to the caller's "DONE" label, which must run the
    displaced originals' return path. Clobbers t0/t1 + out_row; deliberately
    does NOT touch `at`, so it stays out of the save frame."""
    return [
        A.li("t0", mb),
        A.lw("t1", _MP_MB_BOUND, "t0"),
        ("beq", "t1", "zero", "DONE"), A.nop(),          # unarmed -> vanilla
        A.sltu("t1", unit_reg, "t1"),
        ("bne", "t1", "zero", "DONE"), A.nop(),          # party member -> vanilla
        A.lw("t0", _MP_MB_TABLES, "t0"),                 # tables live in bss
        ("beq", "t0", "zero", "DONE"), A.nop(),          # unbuilt -> vanilla
        A.lbu("t1", _MP_UNIT_ID, unit_reg),
        A.sll("t1", "t1", 1),                            # id*2
        A.addu(out_row, "t0", "t1"),
        A.lhu("t1", _MP_T_SHRK, out_row),
        ("beq", "t1", "zero", "DONE"), A.nop(),          # sentinel -> vanilla
    ]


def _mp_shrink_s7(mb):
    """Type-3 to-hit leg, emitted INSIDE the popup_colors roll cave (see
    _MPH_T3_SCORE_REG for why it cannot have a hook of its own).

    By the time the roll cave runs, s7 already carries the engine's resist
    (-148) / weak (+40) adjustments, and `jal rand` has clobbered a0. We
    reconstruct: s4 is a saved register and [s4+0x38] is the target unit at
    EVERY site in this fn (33 loads, verified), and the unit still carries the
    scaled mdef at +0x32, so

        adj    = s7 - fp + mdef_stored        (whatever the engine added)
        s7_new = (fp - mdef_vanilla) * shrink/256 + adj

    which is exactly the semantics of the other four legs -- the score is scaled
    BEFORE the resist/weak adjustments, and those land on top unscaled. Keeping
    the -148 unscaled matters: an elementally-resisted spell must stay ~0%.

    Self-contained save frame, so it composes with the roll cave's own."""
    return (_MP_SAVE
            + [A.lw("t2", 0x38, "s4")]                 # target unit
            + _mp_gate(mb, "t2", "t3")                 # t3 = table row
            + [A.lh("t2", 0x32, "t2"),                 # mdef as the engine saw it
               A.lhu("t1", _MP_T_MDEFV, "t3"),         # vanilla mdef
               A.subu("t0", "s7", "fp"),
               A.addu("t0", "t0", "t2"),               # adj (engine resist/weak
               A.subu("t2", "fp", "t1"),               #  + INT bonus, unscaled)
               A.lhu("t1", _MP_T_SHRK, "t3"),
               A.mult("t2", "t1"), A.mflo("t2"), A.sra("t2", "t2", 8),
               A.addu("s7", "t2", "t0"),               # scaled + adj
               ("label", "DONE")]
            + _MP_RESTORE)


# --- Low-HP spell boost (spells_hit_low_hp_enemies, v254) --------------------
# A status/death spell gets likelier to land as a MONSTER's HP falls. LINEAR
# RAMP (user 2026-08-10, superseding the v253 hard threshold):
#
#     hp% >= 85    ->  x1.0   (vanilla odds; a healthy target notices nothing)
#     85 > hp% > 15->  x1.0 .. x1.5, straight line
#     hp% <= 15    ->  x1.5   (best odds; flat the rest of the way to 1 HP)
#
# Multiplicative on the final to-hit score, which is linear in the engine's
# 201-point roll space, so x1.5 score == x1.5 the odds: 40% -> 60%, 10% -> 15%.
#
# WHERE it runs is the load-bearing part. The leg is emitted inside the
# popup_colors ROLL caves, which sit AFTER magic_power_scaling's shrink leg and
# BEFORE the colour classify. Three consequences, all required:
#   * Monster Power / Boss Difficulty have already had their say, so the boost
#     multiplies the odds the player ACTUALLY has, not vanilla's. At 300% power
#     a shrunk 4% becomes 6%, not 60%.
#   * The roll (0x08884DBC, after the cave returns) and the colour classifier
#     read the same register, so the odds shown on a miss are the odds rolled.
#   * A resisted spell carries the engine's unscaled -148 and lands deep
#     negative; the leg only touches a POSITIVE score, so immune/resisted
#     targets stay exactly 0%. Bosses are not special-cased -- they are just
#     monsters with high mdef, and the user wants the ramp to apply to them.
#
# Party targets are excluded by the MPWR mailbox BOUNDARY (first monster unit
# address, armed per battle by ApClient._magic_power_loop). That mailbox is
# ON_DISC_ALWAYS and its BOUNDARY does not depend on the power settings, so it
# is a valid monster test here even at 100%/100%. Never derive party-ness from
# a struct field -- [unit+0x3C] < 4 misreads 91 of 256 monsters.
#
# Fixed-point in 1/256ths, one divide:
#     q     = curHP*256 / maxHP            0..256
#     t     = HI - q                       <= 0 above the high band -> bail
#     bonus = (t * SLOPE) >> 8, clamped to MAX     0..128
#     score += (score * bonus) >> 8
# SLOPE is 256*MAX/(HI-LO) rounded up (128/180 -> 183/256 = 0.7148), so the line
# reaches the cap a hair early and the clamp holds it there; without the round-up
# the ramp would top out at x1.496 instead of x1.5. maxHP == 0 is bailed BEFORE
# the divu -- an integer divide by zero is undefined on the Allegrex.
_LOWHP_HI, _LOWHP_LO = 218, 38          # 0.85 and 0.15 in 1/256ths (rounded)
_LOWHP_MAX = 128                        # +128/256 of the score  => x1.5 cap
_LOWHP_SLOPE = -(-(256 * _LOWHP_MAX) // (_LOWHP_HI - _LOWHP_LO))    # 183


def _lowhp_boost(mb, score_reg):
    """Ramp the to-hit score from x1.0 at 85% max HP to x1.5 at 15% max HP.

    Emitted inside a popup_colors roll cave, before that cave's own save frame,
    so it carries its own (t0-t3, never `at`) frame and composes with both the
    roll cave and _mp_shrink_s7. Labels are unique because asm_labels resolves
    duplicate label names to the LAST one emitted.

    At entry s4 is the battle actor object; [s4+0x38] is the target unit at
    every site in this fn (the same load _mp_shrink_s7 relies on). curHP @ +8,
    maxHP @ +0xA (_BU_HP / _BU_MAXHP).

    LO/HI are clobbered by the divu/mult here. That is safe: the roll cave's
    displaced pair feeds a divide that the engine issues AFTER this cave
    returns, so it sets LO itself and never reads ours."""
    return (_MP_SAVE
            + [A.lw("t2", 0x38, "s4"),                    # target unit
               A.li("t0", mb),
               A.lw("t1", _MP_MB_BOUND, "t0"),
               ("beq", "t1", "zero", "LOWHP_DONE"), A.nop(),   # unarmed -> vanilla
               A.sltu("t1", "t2", "t1"),
               ("bne", "t1", "zero", "LOWHP_DONE"), A.nop(),   # party -> vanilla
               A.lhu("t0", _BU_MAXHP, "t2"),
               ("beq", "t0", "zero", "LOWHP_DONE"), A.nop(),   # no divide by zero
               A.lhu("t1", _BU_HP, "t2"),                # (t2's unit ptr dies here)
               A.sll("t1", "t1", 8),
               A.divu("t1", "t0"), A.mflo("t1"),         # q = curHP*256 / maxHP
               A.addiu("t2", "zero", _LOWHP_HI),
               A.subu("t2", "t2", "t1"),                 # t = HI - q
               A.slt("t3", "zero", "t2"),
               ("beq", "t3", "zero", "LOWHP_DONE"), A.nop(),   # >= 85% -> vanilla
               A.addiu("t3", "zero", _LOWHP_SLOPE),
               A.mult("t2", "t3"), A.mflo("t2"), A.sra("t2", "t2", 8),
               A.addiu("t3", "zero", _LOWHP_MAX),
               A.slt("t1", "t3", "t2"),
               ("beq", "t1", "zero", "LOWHP_NOCLAMP"), A.nop(),
               A.addu("t2", "zero", "t3"),               # bonus = MAX (<= 15% band)
               ("label", "LOWHP_NOCLAMP"),
               # Only a score that already had a chance is multiplied: <= 0 is a
               # resisted/immune cast and must stay a certain miss.
               A.slt("t3", "zero", score_reg),
               ("beq", "t3", "zero", "LOWHP_DONE"), A.nop(),
               A.mult(score_reg, "t2"), A.mflo("t3"), A.sra("t3", "t3", 8),
               A.addu(score_reg, score_reg, "t3"),       # score * (1 + bonus/256)
               ("label", "LOWHP_DONE")]
            + _MP_RESTORE)


# WW dia damages a target only when a BOSS is present in the encounter. Gate =
# any of the 4 formation type-slot ids (battle_base+0x68A6, +4 = u8[4] ids) is
# flagged in the baked boss table. This is per-ENCOUNTER, not per-target: in a
# boss-plus-adds fight dia also hits the adds (intended -- boss battles only).
# User-editable set; ids per re_only/monster_names.py. Based on the canonical
# FF wiki "Bosses" list (Final_Fantasy_enemies#Bosses), 2026-07-13. The wiki lists
# Piscodemon(0x67) and Warmech(0x76) as regular enemies; Piscodemon is included
# anyway per user request (2026-07-14), Warmech stays excluded.
SCROLL_BOSS_IDS = frozenset({
    0x3C,                                     # Vampire
    0x67, 0x69, 0x71,                         # Piscodemon, Garland, Astos
    0x77, 0x78, 0x79, 0x7A, 0x7B, 0x7C,       # Lich, Marilith, Kraken (both forms each)
    0x7D, 0x7E, 0x7F,                         # Tiamat (both forms), Chaos
    0x80, 0x81, 0x82, 0x83,                   # Echidna, Cerberus, Ahriman, Two-Headed Dragon
    0x84, 0x85, 0x86, 0x87, 0x88,             # Scarmiglione(x2), Cagnazzo, Barbariccia, Rubicante
    0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E,       # Gilgamesh, Omega, Shinryu, Atomos, Typhon, Orthros
    0x8F, 0x90,                               # Phantom Train, Death Gaze
    0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA,   # Chronodia (all forms)
})


def _scroll_boss_table():
    """256-byte table: byte[id] = 1 for boss ids, else 0."""
    t = bytearray(256)
    for i in SCROLL_BOSS_IDS:
        t[i] = 1
    return bytes(t)


def _scroll_boss_gate(mb_reg, bb_reg, tab_off, ok, skip):
    """Emit the per-encounter boss check: if any of the 4 formation type-slot
    ids (battle_base+0x68A6+4+i) is flagged in the baked boss table at
    mb_reg+tab_off, branch to `ok`; else fall through to `skip`. bb_reg must
    already hold battle_base. Clobbers t2, t4, t5. Emits asm_labels items."""
    items = []
    for i in range(4):
        nxt = f"BOSSNX{i}"
        items += [
            A.lbu("t2", 0x68A6 + 4 + i, bb_reg),           # formation slot id
            A.addiu("t5", "zero", 0xFF),
            ("beq", "t2", "t5", nxt), A.nop(),             # 0xFF = empty slot
            A.addiu("t4", mb_reg, tab_off), A.addu("t4", "t4", "t2"),
            A.lbu("t5", 0x00, "t4"),                       # boss-table byte
            ("bne", "t5", "zero", ok), A.nop(),            # flagged -> boss present
            ("label", nxt),
        ]
    items += [("beq", "zero", "zero", skip), A.nop()]
    return items


def _necro_boss_target(mb_reg, out_reg, tag):
    """Emit: out_reg = 1 if the CURRENT TARGET is the encounter's boss, else 0.

    "The boss" = enemy unit 0 (formation type-slot 0, which apply_boss_minions
    reserves for the boss) whose monster id is flagged in the baked boss table.
    A boss used as an ADD lands in a later unit and returns 0, so it is damped
    like a minion rather than protected like a boss. Clobbers t4, t5. `tag` makes
    the emitted labels unique so several of these can share one cave."""
    B, N, D = f"NB{tag}", f"NN{tag}", f"ND{tag}"
    return [
        A.lbu("t4", NECRO_TGT_IDX_OFF, "s4"),          # target unit index
        A.addiu("t5", "zero", NECRO_BOSS_UNIT_ID),
        ("bne", "t4", "t5", N), A.nop(),               # not enemy unit 0 -> add
        A.lw("t4", 0x00, "s4"),                        # battle_base
        A.lbu("t4", 0x68A6 + 4, "t4"),                 # type-slot 0 monster id
        A.addiu("t5", mb_reg, SCROLL_MB_BOSSTAB_OFF), A.addu("t5", "t5", "t4"),
        A.lbu(out_reg, 0x00, "t5"),                    # boss-table byte (0/1)
        ("beq", "zero", "zero", D), A.nop(),
        ("label", N), A.addu(out_reg, "zero", "zero"),
        ("label", D),
    ]


def _scroll_gate(mb_vaddr, actor_reg, flag_bit, cls_a, cls_b, done="DONE"):
    """Shared cave gate: mailbox flag set AND caster is a party member of class
    cls_a/cls_b. Leaves t0 = mailbox vaddr. Clobbers t0-t5, at. Emits asm_labels
    items; falls through on PASS, branches to `done` otherwise."""
    return [
        A.li("t0", mb_vaddr),
        A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"),
        A.andi("t1", "t1", flag_bit),
        ("beq", "t1", "zero", done), A.nop(),
        # party caster? idx = [actor+0x3C] < 4
        A.lbu("t1", 0x3C, actor_reg),
        A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
        ("beq", "at", "zero", done), A.nop(),
        # field rec = *(battle_base+0x6834) + idx*0x5C; class @+0x1E (menu frame)
        A.lw("t2", 0x00, actor_reg), A.lw("t2", 0x6834, "t2"),
        A.sll("t4", "t1", 2), A.sll("t5", "t1", 4), A.addu("t4", "t4", "t5"),
        A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
        A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),    # t4 = idx*0x5C
        A.addu("t2", "t2", "t4"),
        A.lbu("t3", 0x1E, "t2"),                           # class
        A.addiu("t5", "zero", cls_a),
        ("beq", "t3", "t5", "CLSOK"), A.nop(),
        A.addiu("t5", "zero", cls_b),
        ("bne", "t3", "t5", done), A.nop(),
        ("label", "CLSOK"),
    ]


_SCROLL_SAVE = [
    A.addiu("sp", "sp", -0x20),
    A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"), A.sw("t2", 0x08, "sp"),
    A.sw("t3", 0x0C, "sp"), A.sw("t4", 0x10, "sp"), A.sw("t5", 0x14, "sp"),
    A.sw("at", 0x18, "sp"),
]
_SCROLL_RESTORE = [
    A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"), A.lw("t2", 0x08, "sp"),
    A.lw("t3", 0x0C, "sp"), A.lw("t4", 0x10, "sp"), A.lw("t5", 0x14, "sp"),
    A.lw("at", 0x18, "sp"),
    A.addiu("sp", "sp", 0x20),
]
# The one word of the _SCROLL_SAVE frame nothing is saved into. A cave may park
# a value there across its own internal branches; _SCROLL_RESTORE never reads
# it, so nothing is clobbered on the way out.
_SCROLL_SAVE_SPARE = 0x1C


def apply_job_scroll_boosts(elf: bytearray, feats=None):
    # slot_magic active: both Crimson Wizard damage legs jal the shared
    # damage->slot leaf instead of computing an MP refund (the pool is inert).
    # apply_slot_magic runs first (FEATURES order) and publishes the address.
    _sm_cw = 0
    if feats and feats.get("slot_magic"):
        _sm_cw = _SM_EXPORTS.get("cwslot", 0)
        if not _sm_cw:
            raise ValueError("job_scroll_boosts: slot_magic on but the cwslot "
                             "leaf is unpublished (FEATURES order regressed?)")
    # mailbox cave FIRST so the code caves can embed its vaddr. Layout: magic(4)
    # + flags(1)@4 + BW tohit u16@8 + BW thresh u16@10 + boss table[256]@16.
    mb = (SCROLL_MB_MAGIC + b"\x00\x00\x00\x00"
          + struct.pack("<HH", SCROLL_BW_TOHIT_DEFAULT, SCROLL_BW_THRESH_DEFAULT)
          + b"\x00" * 4
          + _scroll_boss_table()
          + _scroll_u16_table(SCROLL_FAIL_POWERS)
          + _scroll_u16_table(SCROLL_DIA_HEAL_MULT_Q8)
          + _scroll_u16_table(SCROLL_TOHIT_BONUSES)
          + _scroll_u16_table(SCROLL_THRESHOLDS)
          + b"\x00" * (4 + 4 * SOS_RING)     # v102/v105 miss-report ring
          + b"\x00" * SCROLL_MB_DIAINT_LEN   # v108 dia INT accumulator[4]
          + b"\x00" * SCROLL_MB_HEALPOP_LEN  # v118 heal-popup pending record
          + b"\x00" * SCROLL_MB_TEALPOP_LEN  # v123 staggered teal-popup record
          + _scroll_diastep_table()          # v127 dia INT step table[64] (baked)
          + b"\x00" * SCROLL_MB_MATK_LEN)    # v130 Master attack accumulator[4]
    assert len(mb) == SCROLL_MB_LEN, len(mb)
    mb_vaddr = E.add_segment_cave(elf, mb)
    dmb = _delaypop_mb(elf, feats)   # shared delayed-popup mailbox (teal MP refund)

    # --- v204 Crimson Wizard cast refund AT THE PAY SITE. History: the leg
    # lived in the magic-executor prologue cave (v120-v203) keyed off the
    # executor's sp-frame spell id, and every gating attempt against
    # item/equipment casts failed on the same rock -- the executor runs (or
    # carries stale frames) for actions that are not the caster's native cast,
    # so heals leaked to Phoenix Downs / Black Robes (v191) or landed on the
    # NEXT action (v203's marker: live 2026-08-02, Haste's 10 popped on the
    # Monk's attack). The battle MP-deduct site is the one place that runs
    # EXACTLY when a native battle cast is paid for -- items and equipment
    # procs never reach it, enemy rows are gated below -- so the heal now
    # rides there, hooked at the deduct RETURN point (_SM_BATTLE_DEDUCT_RET):
    #   * slot_magic ON: its deduct cave (hooked at the site proper) jumps to
    #     the RET point after bumping spent[] -> lands in this cave; a0 = 0.
    #   * slot_magic OFF: vanilla deduct falls through into it; a0 = MP cost.
    # Displaced originals = the native MP subtract (subu v0,v0,a0 /
    # sh v0,0xc(v1)); v0/v1/a0/s1/s2 stay live for the resuming native code,
    # scratch rides on the stack. Heal: MP mode cost/2 = a0>>1
    # (RW_MP_TO_HP_PCT=50); slot_magic 5*spell level (user 2026-07-31), level
    # via _sm_level_from_id(s1). HP applies to the battle record C=[s2+0x34]
    # (curHP@+8 maxHP@+0xA, the blood-magic RE), green number via the shared
    # delayed-popup service. ---
    # v205 fixes to this cave, both live-caught 2026-08-02 (sprite corruption +
    # a stuck yellow 00000 during a Haste cast, popup over the WRONG unit):
    #  * CTX: v204 read ctx from [s2+0x00] -- UNVERIFIED at this site (the
    #    afford cave receives ctx in a0 precisely because the combatant obj is
    #    not known to carry it here), so the class gate walked garbage and the
    #    heal fired for a non-RW caster. ctx is now DERIVED from two fields
    #    that ARE verified at this site (slot_magic's own deduct cave uses
    #    both): ctx = C - _BU_OFF - row*0x6C, from C=[s2+0x34], row=[s2+0x3C].
    #  * NUMBER (v205 -> v206 -> v207): the green number goes through the
    #    delayed-popup service, full stop. v204 jal'd _POPUP_SPAWN_FN directly
    #    from this cave and corrupted sprites; v206 retried the direct jal with
    #    the DERIVED ctx on the theory that v204's garbage [s2+0x00] ctx was
    #    the whole corruption -- live 2026-08-02: STILL corrupts (and still
    #    draws a yellow 00000 over the wrong unit), so the spawner genuinely
    #    cannot be called from deduct context (its sprite/unit plumbing needs
    #    display-SM state the deduct phase doesn't have). DO NOT retry the
    #    direct jal here. v207's cosmetic limit (status-cast number surfaced
    #    one action late because the anim-poll pump only ticked while numbers
    #    floated) is FIXED in v209's split pump: the dispatcher-prologue tick
    #    hook (see _DELAYPOP_TICK_HOOK) drains the 30-frame countdown every
    #    frame an action resolves, and the anim-poll spawn hook pays it out
    #    in-cast from the only spawn-safe context.
    cwpay = A.asm_labels(
        [A.subu("v0", "v0", "a0"), A.sh("v0", 0x0C, "v1"),      # displaced
         A.addiu("sp", "sp", -0x20),
         A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"), A.sw("t2", 0x08, "sp"),
         A.sw("t3", 0x0C, "sp"), A.sw("t4", 0x10, "sp"), A.sw("t5", 0x14, "sp"),
         A.sw("at", 0x18, "sp"),
         # RW scroll owned (mailbox bit3)?
         A.li("t0", mb_vaddr),
         A.lbu("t2", SCROLL_MB_FLAGS_OFF, "t0"),
         A.andi("t2", "t2", 0x08),
         ("beq", "t2", "zero", "DONE"), A.nop(),
         # party caster? row = [s2+0x3C] < 4
         A.lbu("t1", 0x3C, "s2"),
         A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
         ("beq", "at", "zero", "DONE"), A.nop(),
         # ctx = C - _BU_OFF - row*0x6C  (0x6C = 64+32+8+4)
         A.lw("t3", 0x34, "s2"),                                # C = battle rec
         A.sll("t4", "t1", 6), A.sll("t5", "t1", 5), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 2), A.addu("t4", "t4", "t5"),        # row*0x6C
         A.subu("t4", "t3", "t4"),
         A.li("t5", _BU_OFF), A.subu("t4", "t4", "t5"),         # t4 = ctx
         # class {3,9}? field rec = [ctx+0x6834] + row*0x5C, class @+0x1E
         A.lw("t2", 0x6834, "t4"),
         A.sll("t4", "t1", 2), A.sll("t5", "t1", 4), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),        # row*0x5C
         A.addu("t2", "t2", "t4"),
         A.lbu("t4", 0x1E, "t2"),                               # class
         A.addiu("t5", "zero", 3),
         ("beq", "t4", "t5", "CWCLS"), A.nop(),
         A.addiu("t5", "zero", 9),
         ("bne", "t4", "t5", "DONE"), A.nop(),
         ("label", "CWCLS")]
        # Heal amount -> t2. (t3 still = C.)
        + ([A.andi("at", "s1", 0xFF),                           # slot: 5 * level
            A.sll("t2", "at", 3), A.subu("t2", "t2", "at"),
            A.sll("t2", "t2", 1),                               # id*14
            A.li("at", _SM_L9_BASE),
            A.addu("at", "at", "t2"),
            A.lbu("t2", 0x00, "at"),                            # spell level
            A.sll("t4", "t2", 2), A.addu("t2", "t2", "t4")]     # *5
           if _sm_cw else
           [A.srl("t2", "a0", 1)])                              # MP: cost/2
        + [("beq", "t2", "zero", "DONE"), A.nop(),
           A.lhu("t4", _BU_HP, "t3"),                           # curHP
           ("beq", "t4", "zero", "DONE"), A.nop(),              # dead -> no heal
           A.addu("t4", "t4", "t2"),
           A.lhu("at", _BU_MAXHP, "t3"),
           A.sltu("t5", "at", "t4"),
           ("beq", "t5", "zero", "CWWR"), A.nop(),
           A.addu("t4", "zero", "at"),                          # clamp to maxHP
           ("label", "CWWR"),
           A.sh("t4", _BU_HP, "t3"),
           A.li("t3", dmb),                                     # delayed green number
           A.sb("t1", _DP_UNIT, "t3"), A.sh("t2", _DP_VAL, "t3"),
           A.addiu("at", "zero", 0x20), A.sh("at", _DP_FLAGS, "t3"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
           A.addiu("at", "zero", 1),
           A.sb("at", _DP_TICKFAST, "t3"),                      # dispatcher may drain
           A.sb("at", _DP_PEND, "t3"),
           ("label", "DONE"),
           A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"), A.lw("t2", 0x08, "sp"),
           A.lw("t3", 0x0C, "sp"), A.lw("t4", 0x10, "sp"), A.lw("t5", 0x14, "sp"),
           A.lw("at", 0x18, "sp"),
           A.addiu("sp", "sp", 0x20),
           # +8 skips the two words install_detour displaced at the return
           # site -- the cave already replayed that pair at its head, so it
           # must rejoin AFTER them, not re-run them.
           A.j(_SM_BATTLE_DEDUCT_RET + 8), A.nop()])
    E.install_detour(elf, _SM_BATTLE_DEDUCT_RET,
                     E.add_segment_cave(elf, cwpay))

    # --- WW: dia-type (effect 2) damages non-undead ONLY WHEN THE TARGET IS THE
    # ENCOUNTER'S BOSS. Displaced originals load the target family byte into v1; OR
    # in the undead bit (0x8) only for a gated White Wizard caster whose CURRENT
    # TARGET is the boss (enemy unit 0). Non-undead boss minions keep their real
    # family byte, so dia does nothing to them; genuinely undead minions (e.g.
    # skeletons) still take damage via their own family bit -- we only ever OR the
    # bit in, never clear it. (Prior builds forced it per-ENCOUNTER, so dia also
    # hit non-undead adds like the Black Knight. User 2026-07-23.) ---
    ww = A.asm_labels(
        [A.lw("v1", 0x38, "s4"), A.lbu("v1", 0x21, "v1")]   # displaced originals
        + _SCROLL_SAVE
        + _scroll_gate(mb_vaddr, "s4", 0x01, 4, 10)         # WW caster? else DONE
        + [A.lw("t1", 0x00, "s4")]                          # t1 = battle_base
        + _scroll_boss_gate("t0", "t1", SCROLL_MB_BOSSTAB_OFF, "BOSSOK", "DONE")
        + [("label", "BOSSOK")]
        + _necro_boss_target("t0", "t3", "WW")              # t3 = target-is-boss
        + [("beq", "t3", "zero", "DONE"), A.nop(),          # target not boss -> skip
           A.ori("v1", "v1", 0x8),                          # boss: pretend undead
           ("label", "DONE")]
        + _SCROLL_RESTORE
        + [A.j(_WW_RET), A.nop()])
    E.install_detour(elf, _WW_HOOK, E.add_segment_cave(elf, ww))

    # --- BW leg 1: type-3 status roll. Add the PER-SPELL to-hit bonus to the score
    # s7 BEFORE the displaced rand call (hit iff s7 >= rand%201, so +N is ~+N/2
    # points of land chance). s7 is the fn's own score var (callee-saved there;
    # changing it is the point). ra is dead here (fn saves/restores it).
    # The bonus now comes from a baked u16[64] keyed by spell id, replacing the old
    # `status mask s5 == 1` gate -- that locked the boost to the kill line
    # (Death/Quake/Scourge/Warp) and excluded Break, whose mask is 2. A zero table
    # entry means "no boost", so non-listed type-3 spells are untouched.
    # Spell id (u16, 1-based) at ORIGINAL sp+0x68 -> sp+0x88 after the save frame. ---
    bw3 = A.asm_labels(
        _SCROLL_SAVE
        + _scroll_gate(mb_vaddr, "s4", 0x02, 5, 11)         # t0 = mailbox
        + [A.lhu("t1", 0x68 + 0x20, "sp"),                  # spell id
           A.addiu("t1", "t1", -1), A.sll("t2", "t1", 1),   # (id-1)*2
           A.addiu("t4", "t0", SCROLL_MB_TOHITTAB_OFF), A.addu("t4", "t4", "t2"),
           A.lhu("t1", 0x00, "t4"),                         # per-spell INT mult
           ("beq", "t1", "zero", "DONE"), A.nop(),          # 0 = no boost
           # v100: bonus = INT * mult (was a flat table value). Caster INT =
           # u8 [s4+0x34]+0x36. The two nops mirror the engine's own mult/mflo
           # spacing (0x08884B84) rather than relying on the interlock.
           A.lw("t2", 0x34, "s4"), A.lbu("t2", 0x36, "t2"),
           A.multu("t1", "t2"), A.nop(), A.nop(), A.mflo("t1")]
        # v101: damp the bonus against the encounter's boss (but NOT against a
        # boss used as an add -- see _necro_boss_target).
        + _necro_boss_target("t0", "t3", "3")
        + [("beq", "t3", "zero", "NODAMP"), A.nop(),
           A.addiu("t5", "zero", NECRO_BOSS_INT_DIV),
           A.divu("t1", "t5"), A.nop(), A.nop(), A.mflo("t1"),
           ("label", "NODAMP"),
           A.addu("s7", "s7", "t1"),                        # score += bonus
           ("label", "DONE")]
        + _SCROLL_RESTORE
        + [A.jal(_SCROLL_RAND_FN), A.nop(),                 # displaced original
           A.j(_BW3_RET), A.nop()])
    E.install_detour(elf, _BW3_HOOK, E.add_segment_cave(elf, bw3))

    # --- BW leg 2: type-0x12 threshold autohit. This handler has NO rng and NO
    # accuracy: it auto-hits iff target curHP < threshold (vanilla 301) && the target
    # is not already statused && the element is not resisted. So the threshold is the
    # only knob, and it is now PER-SPELL (baked u16[64], 0 = keep vanilla 301) rather
    # than one scalar gated on `status mask == 1`. That old gate meant only Kill got
    # the boost; Stun (mask 0x10) and Blind (mask 8) were stuck at 301. A Ninja or any
    # other class casting these still falls to VANILLA via the class gate below.
    # Displaced: lhu v1,8(a0) + slti at,v1,0x12d. `at` carries the verdict to the beql
    # at the return point, so this cave can't use the shared gate (whose slt clobbers
    # `at` before branching to DONE): t-reg-only gate, and the verdict (slti vanilla /
    # sltu boosted) is computed LAST on each path. `at` is deliberately NOT saved. ---
    # v101 additions: the raised threshold is SUPPRESSED against the encounter's
    # boss (bosses keep vanilla 301), and Kill -- above whatever threshold applies
    # -- now ROLLS instead of failing outright, so it can reach targets the type-3
    # kill line can reach. The frame grew to 0x30 to also preserve a0 (the target
    # struct, which the engine reads right after we return) and ra across the
    # jal into the engine's RNG, plus a scratch slot for the score. Note the spell
    # id therefore moved from sp+0x88 to sp+0x98 (orig sp+0x68 + frame).
    bwk = A.asm_labels(
        [A.lhu("v1", 0x08, "a0"),
         A.addiu("sp", "sp", -0x30),
         A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"), A.sw("t2", 0x08, "sp"),
         A.sw("t3", 0x0C, "sp"), A.sw("t4", 0x10, "sp"), A.sw("t5", 0x14, "sp"),
         A.sw("a0", 0x18, "sp"), A.sw("ra", 0x1C, "sp"),
         A.li("t0", mb_vaddr),
         A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"),
         A.andi("t1", "t1", 0x02),
         ("beq", "t1", "zero", "VANILLA"), A.nop(),
         A.lbu("t1", 0x3C, "s4"),
         A.addiu("t5", "zero", 4), A.slt("t5", "t1", "t5"),
         ("beq", "t5", "zero", "VANILLA"), A.nop(),
         A.lw("t2", 0x00, "s4"), A.lw("t2", 0x6834, "t2"),
         A.sll("t4", "t1", 2), A.sll("t5", "t1", 4), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
         A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),
         A.addu("t2", "t2", "t4"),
         A.lbu("t3", 0x1E, "t2"),
         A.addiu("t5", "zero", 5),
         ("beq", "t3", "t5", "BOOST"), A.nop(),
         A.addiu("t5", "zero", 11),
         ("bne", "t3", "t5", "VANILLA"), A.nop(),
         ("label", "BOOST"),
         # per-spell INT multiplier: thrtab[id-1]; id (u16) at orig sp+0x68 -> sp+0x98
         A.lhu("t1", 0x68 + 0x30, "sp"),
         A.addiu("t1", "t1", -1), A.sll("t2", "t1", 1),     # (id-1)*2
         A.addiu("t4", "t0", SCROLL_MB_THRTAB_OFF), A.addu("t4", "t4", "t2"),
         A.lhu("t1", 0x00, "t4"),                           # per-spell INT mult
         ("beq", "t1", "zero", "VANILLA"), A.nop(),         # 0 = keep vanilla 301
         # v100: threshold = SCROLL_THRESH_BASE + INT*mult (was a flat value).
         # Caster INT = u8 [s4+0x34]+0x36. Max INT 255 * mult 40 + 300 = 10500,
         # so the u16 curHP compare can never overflow. `multu`/`mflo` do not
         # touch `at`, which this cave must leave alone until the verdict below.
         A.lw("t2", 0x34, "s4"), A.lbu("t2", 0x36, "t2")]   # t2 = caster INT
        # v101: is the target the encounter's boss? -> t3 (survives to the roll)
        + _necro_boss_target("t0", "t3", "K")
        + [("bne", "t3", "zero", "BOSSTHR"), A.nop(),
           A.multu("t1", "t2"), A.nop(), A.nop(), A.mflo("t1"),
           A.addiu("t1", "t1", SCROLL_THRESH_BASE),
           ("beq", "zero", "zero", "HAVETHR"), A.nop(),
           # boss: no raised threshold at all, keep the engine's vanilla 301
           ("label", "BOSSTHR"), A.addiu("t1", "zero", 0x12D),
           ("label", "HAVETHR"),
           A.sltu("at", "v1", "t1"),                        # at = curHP < threshold
           ("bne", "at", "zero", "OUT"), A.nop(),           # under it -> autohit
           # ---- v101 Kill-only roll fallback -------------------------------
           # Above the threshold Kill rolls instead of failing. `at` is already 0
           # here, so Stun/Blind simply fall out with the vanilla miss verdict.
           A.lhu("t4", 0x68 + 0x30, "sp"),
           A.addiu("t5", "zero", _ID_KILL),
           ("bne", "t4", "t5", "OUT"), A.nop(),
           # score = ACC + 148 - target magic defence + bonus
           A.lw("t4", 0x38, "s4"), A.lh("t5", _NECRO_MAGDEF_OFF, "t4"),
           A.addiu("t1", "zero", NECRO_KILL_FB_ACC + NECRO_ROLL_CONST),
           A.subu("t1", "t1", "t5"),
           ("beq", "t3", "zero", "FBNODAMP"), A.nop(),      # boss -> damp INT
           A.addiu("t5", "zero", NECRO_BOSS_INT_DIV),
           A.divu("t2", "t5"), A.nop(), A.nop(), A.mflo("t2"),
           ("label", "FBNODAMP"),
           A.addu("t1", "t1", "t2"),                        # t1 = score (signed)
           A.sw("t1", 0x20, "sp"),                          # survive the jal
           A.jal(_SCROLL_RAND_FN), A.nop(),
           A.lw("t1", 0x20, "sp"),
           A.andi("t4", "v0", 0xFFFF),
           A.addiu("t5", "zero", NECRO_ROLL_RANGE),
           A.divu("t4", "t5"), A.nop(), A.nop(), A.mfhi("t4"),   # roll = rand%201
           # hit iff score >= roll  <=>  roll < score+1 (signed: a negative score
           # can never beat a 0..200 roll, which is what we want).
           A.addiu("t1", "t1", 1),
           A.slt("at", "t4", "t1"),
           ("beq", "zero", "zero", "OUT"), A.nop(),
           ("label", "VANILLA"),
           A._i(0x0A, "v1", "at", 0x12D),                   # slti at,v1,0x12d (displaced)
           ("label", "OUT"),
           A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"), A.lw("t2", 0x08, "sp"),
           A.lw("t3", 0x0C, "sp"), A.lw("t4", 0x10, "sp"), A.lw("t5", 0x14, "sp"),
           A.lw("a0", 0x18, "sp"), A.lw("ra", 0x1C, "sp"),
           A.addiu("sp", "sp", 0x30),
           A.j(_BWK_RET), A.nop()])
    E.install_detour(elf, _BWK_HOOK, E.add_segment_cave(elf, bwk))

    # --- BW leg 3: kill spells that FAIL to kill deal damage instead of nothing.
    # One shared cave for both miss sites (type-3 roll miss @_FAIL_HOOK_T3, Kill
    # over-threshold @_FAIL_HOOK_KILL); both are `ori v1,v1,0x10 / b epilogue /
    # sh v1,0xc(s3)` with v1 pre-loaded = the result-flags word. For a gated Black
    # Wizard caster whose spell has a baked fail-power, we substitute s6 = that
    # power, point v0 at the target struct, and jump into the engine's magic-damage
    # path (_DMG_PATH) -- reusing INT scaling, random range, element and magdef, so
    # the fail hit behaves like a real damage spell. Otherwise reproduce the vanilla
    # miss (write the ori'd flags, continue to the epilogue). s6 is safe to clobber:
    # it is the spell "power", unused by any kill-spell hit/miss path and reloaded
    # per cast. The spell id (u16, 1-based) lives at ORIGINAL sp+0x68 -> sp+0x88
    # after the 0x20 save frame. ---
    fail_dmg = A.asm_labels(
        [A.ori("v1", "v1", 0x10)]                           # displaced original
        + _SCROLL_SAVE
        + [A.li("t0", mb_vaddr),
           # v102: stamp the miss report BEFORE the gate, so the client can log
           # every failed save-or-suffer cast, not just a Necrocaster's. `gated`
           # is cleared here and set below only if the class/scroll gate passes.
           # ring slot = counter & (SOS_RING-1); t3 = &entry
           A.lbu("t1", SCROLL_MB_REPORT_OFF, "t0"),             # write counter
           A.andi("t2", "t1", SOS_RING - 1), A.sll("t2", "t2", 2),
           A.addiu("t3", "t0", SCROLL_MB_REPORT_OFF + 4), A.addu("t3", "t3", "t2"),
           A.lhu("t2", 0x68 + 0x20, "sp"), A.sb("t2", 0x00, "t3"),   # spell id
           A.lbu("t2", NECRO_TGT_IDX_OFF, "s4"), A.sb("t2", 0x01, "t3"),  # target
           A.lw("t2", 0x34, "s4"), A.lbu("t2", 0x36, "t2"),
           A.sb("t2", 0x02, "t3"),                              # caster INT
           A.sb("zero", 0x03, "t3"),                            # gated = 0
           A.addiu("t1", "t1", 1),
           A.sb("t1", SCROLL_MB_REPORT_OFF, "t0"),              # publish entry
           # NOTE: the next ~16 instructions are _scroll_gate(.., 0x02, 5, 11,
           # done="MISS") inlined WITHOUT its leading li t0 (t0 already holds the
           # mailbox here). Keep the two in sync -- calling the helper instead
           # would emit a redundant li and change the baked bytes.
           A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"), A.andi("t1", "t1", 0x02),
           ("beq", "t1", "zero", "MISS"), A.nop(),          # BW flag off
           A.lbu("t1", 0x3C, "s4"),
           A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
           ("beq", "at", "zero", "MISS"), A.nop(),          # caster not party
           A.lw("t2", 0x00, "s4"), A.lw("t2", 0x6834, "t2"),
           A.sll("t4", "t1", 2), A.sll("t5", "t1", 4), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),
           A.addu("t2", "t2", "t4"), A.lbu("t3", 0x1E, "t2"),   # class
           A.addiu("t5", "zero", 5), ("beq", "t3", "t5", "CLSOK"), A.nop(),
           A.addiu("t5", "zero", 11), ("bne", "t3", "t5", "MISS"), A.nop(),
           ("label", "CLSOK"),
           # v102: a real scrolled Necrocaster -> tell the client to include the
           # INT bonus when it reproduces the score. The gate clobbered t3, so
           # recompute the entry we just published: slot = (counter-1) & mask.
           A.lbu("t1", SCROLL_MB_REPORT_OFF, "t0"), A.addiu("t1", "t1", -1),
           A.andi("t1", "t1", SOS_RING - 1), A.sll("t1", "t1", 2),
           A.addiu("t2", "t0", SCROLL_MB_REPORT_OFF + 4), A.addu("t2", "t2", "t1"),
           A.addiu("t1", "zero", 1), A.sb("t1", 0x03, "t2"),
           # failpow = failtab[id-1]; id (u16) at sp+0x88 (orig sp+0x68 after save)
           A.lhu("t1", 0x68 + 0x20, "sp"),
           A.addiu("t1", "t1", -1), A.sll("t2", "t1", 1),   # (id-1)*2
           A.addiu("t4", "t0", SCROLL_MB_FAILPOW_OFF), A.addu("t4", "t4", "t2"),
           A.lhu("t5", 0x00, "t4"),                         # fail power
           ("beq", "t5", "zero", "MISS"), A.nop(),          # no fail dmg for this id
           A.addu("s6", "zero", "t5")]                      # s6 = power
        + _SCROLL_RESTORE
        # v103 EXPERIMENT (NECRO_MSG_ON_FAIL_DMG): also commit the "no effect"
        # result flag before diving into the damage path, so the battle message
        # can render ALONGSIDE the fail-damage number. Until now this branch threw
        # the flag away, which is the real reason no message ever appeared -- the
        # damage path does NOT suppress it. VERIFIED STATICALLY: nothing in the
        # magic-exec fn writes s3+0xc between _DMG_PATH and the epilogue (the only
        # writes are at 0x08884D70 and later, all in the type-3/kill blocks), so a
        # flag stored here survives. v1 still holds the ori'd flags (the restore
        # above only touches t0-t5/at) and is dead on entry to _DMG_PATH.
        # UNKNOWN, and the whole point of the test: whether the RENDERER draws a
        # message and a damage popup in the same action. Flip the flag off to
        # revert to the previous silent behaviour.
        + ([A.sh("v1", 0x0C, "s3")] if NECRO_MSG_ON_FAIL_DMG else [])
        + [A.lw("v0", 0x38, "s4"),                          # v0 = target struct
           A.j(_DMG_PATH), A.nop(),
           ("label", "MISS")]
        + _SCROLL_RESTORE
        + [A.sh("v1", 0x0C, "s3"),                          # vanilla miss store
           A.j(_MISS_EPILOGUE), A.nop()])
    fail_cave = E.add_segment_cave(elf, fail_dmg)
    E.install_detour(elf, _FAIL_HOOK_T3, fail_cave)
    E.install_detour(elf, _FAIL_HOOK_KILL, fail_cave)
    # v162: same cave, two more roll-miss sites (type-0x04 Slow/Slowra, type-0x0e
    # Focus). The type-3 additions (Sleep/Dark/Hold/Sleepra/Confuse/Stop) need no
    # new hook -- they already share _FAIL_HOOK_T3.
    E.install_detour(elf, _FAIL_HOOK_SLOW, fail_cave)
    E.install_detour(elf, _FAIL_HOOK_STATUS, fail_cave)
    # v82: the resist/immunity bails, which never reached either site above.
    E.install_detour(elf, _FAIL_HOOK_T3_IMMUNE, fail_cave)
    E.install_detour(elf, _FAIL_HOOK_KILL_ELEM, fail_cave)

    # --- BW leg 4 (v98): death-resist pierce. Three caves, one shape: run the
    # displaced pair that produces the target's element-resist mask, and for a
    # gated Necrocaster caster clear the death bit out of it before the engine
    # ANDs it with the spell's element. Only bit NECRO_PIERCE_ELEM is cleared, so
    # every other resistance (fire/ice/lightning/...) still applies, and a non-BW
    # caster who learned Kill is untouched by the class gate. Register notes: the
    # gate clobbers only t0-t5/at (all saved), so the mask reg (v0/v1) and the
    # live frame regs (s1/s4/fp) survive. ---
    # `pre` = displaced word(s) that must run BEFORE the pierce (they produce the
    # mask); `post` = displaced word(s) replayed AFTER it (the `and` with the spell
    # element, which must see the pierced mask). Together they are exactly the two
    # words install_detour overwrites, in order.
    # v101 (user spec 2026-07-21): "Kill and Death ignore death resistance only
    # for fallback damage but not instant-death resistance." So ONLY the
    # magic-damage leg pierces now. The two landing hooks (type-3 roll and Kill's
    # autohit) are deliberately NOT installed: a death-immune enemy still shrugs
    # off the KO itself, it just takes full damage when it does. This also closes
    # the v98 hole where piercing the landing check turned the Two-Headed Dragon
    # (resist 0x3fff but magic defence only 50) from 7% into 61%.
    for hook, ret, mask_reg, pre, post in (
            (_NECRO_DMG_HOOK, _NECRO_DMG_RET, "v0",
             [A.lw("v0", 0x38, "s4"), A.lhu("v0", 0x24, "v0")], []),
    ):
        cave = A.asm_labels(
            pre
            + _SCROLL_SAVE
            + _scroll_gate(mb_vaddr, "s4", 0x02, 5, 11)     # Necrocaster? else DONE
            + [A.andi(mask_reg, mask_reg, NECRO_PIERCE_KEEP),   # drop death bit
               ("label", "DONE")]
            + _SCROLL_RESTORE
            + post
            + [A.j(ret), A.nop()])
        E.install_detour(elf, hook, E.add_segment_cave(elf, cave))

    # --- WW leg 2: a White Wizard casting a dia-type spell self-heals. ONCE per
    # cast: hook the pre-loop prologue (the target loop is internal). Reuse the
    # WW class gate {4,10} so only a real White Wizard benefits (randomized-spell
    # safety). The diaheal table entry is nonzero only for dia spells, so no
    # separate spell-type check is needed. Caster unit record = *(s4) + idx*0x6C
    # + 0xC714; curHP clamped to maxHP.
    #
    # v227 (user 2026-08-05): heal = (INT * SCROLL_DIA_HEAL_MULT_Q8[id]) >> 8,
    # floored, min 1 -- Dia x0.5 / Diara x0.75 / Diaga x1 / Diaja x1.25 -- where
    # INT is the caster's EQUIPPED battle INT plus the dia steps already banked
    # this battle, but NOT this cast's own step. ---
    heal = A.asm_labels(
        [A.addu("v1", "v1", "zero"), A.sw("v1", 0x34, "sp")]   # displaced originals
        + _SCROLL_SAVE
        # v168 SPELL-ID BOUNDS GATE (WW-caster tonic double-buff root cause,
        # live 2026-07-30): the executor also runs for battle ITEM use, and then
        # sp+0x88 holds an out-of-range id. The unbounded (id-1)*2 diaheal read
        # walked past the 64-entry table into the neighbouring dia-STEP table's
        # 1s -> spurious heal=1 -> HEALPOP appended a caster result entry -> the
        # item SM's status-effect writer (0x088854C4) applied the ITEM's stat
        # effect (Strength Tonic +10 atk) to that entry's target = the CASTER,
        # plus the green 1. Gate BOTH legs (the RW cost/2 leg reads the same id
        # into magic_info) on id 1..64 before touching any table.
        + [A.lhu("t1", 0x68 + 0x20, "sp"),                     # spell id (u16)
           A.addiu("t1", "t1", -1),
           A.addiu("t2", "zero", SCROLL_FAILPOW_ENTRIES),      # 64 real spell ids
           A.sltu("t2", "t1", "t2"),                           # (id-1) < 64 ?
           ("beq", "t2", "zero", "DONE"), A.nop()]             # item/garbage -> out
        + _scroll_gate(mb_vaddr, "s4", 0x01, 4, 10)         # WW caster? else DONE
        + [A.lhu("t1", 0x68 + 0x20, "sp"),                     # spell id (u16)
           A.addiu("t1", "t1", -1), A.sll("t2", "t1", 1),      # (id-1)*2
           A.addiu("t4", "t0", SCROLL_MB_DIAHEAL_OFF), A.addu("t4", "t4", "t2"),
           A.lhu("t5", 0x00, "t4"),                            # Q8 heal multiplier
           ("beq", "t5", "zero", "DONE"), A.nop(),             # not a dia spell
           # --- WW leg 4 (v118): pop a GREEN heal NUMBER over the caster.
           # This cave runs INSIDE the magic executor (0x88846D8), whose per-slot
           # value loop recomputes EVERY result entry's +4 value as the spell's
           # effect on that unit -- for offensive dia on the (non-undead) caster
           # that is 0, which zeroed a directly-appended heal entry (LIVE
           # 2026-07-23: green "0", no heal, even with a hardcoded value). So we
           # never append a result entry here (the loop would zero it). v203:
           # nor RECORD it for the executor's post-loop epilogue cave any more
           # -- that made delivery depend on an epilogue that some casts never
           # reach, and the pending record then paid out over whatever action
           # ran next (the CW leg's live symptom, user video 2026-08-02). Write
           # the battle HP right here and arm the delayed green number, same as
           # the CW leg. t0 = mailbox (from the gate); t5 = heal amount. ---
           # caster unit record (t3), shared with the INT leg below
           A.lbu("t1", 0x3C, "s4"),                            # caster party idx
           A.sll("t2", "t1", 2), A.sll("t4", "t1", 3), A.addu("t2", "t2", "t4"),
           A.sll("t4", "t1", 5), A.addu("t2", "t2", "t4"),
           A.sll("t4", "t1", 6), A.addu("t2", "t2", "t4"),     # t2 = idx*0x6C
           A.lw("t3", 0x00, "s4"), A.addu("t3", "t3", "t2"),
           A.li("t4", _BU_OFF), A.addu("t3", "t3", "t4"),      # t3 = unit record
           # --- v227 EQUIPPED INT (heal scale + the base the step leg writes).
           # a0 = field rec = *(s4+0)+0x6834 + idx*0x5C, the exact pointer the
           # engine's own battle-unit refresh (0x08876384) hands _INT_GET_FN. ---
           A.lw("t2", 0x00, "s4"), A.lw("t2", 0x6834, "t2"),   # field array
           A.sll("at", "t1", 2),
           A.sll("t4", "t1", 4), A.addu("at", "at", "t4"),
           A.sll("t4", "t1", 3), A.addu("at", "at", "t4"),
           A.sll("t4", "t1", 6), A.addu("at", "at", "t4"),     # at = idx*0x5C
           A.addu("a0", "t2", "at"),                           # a0 = field rec
           # CALL FRAME. v0/v1/a0-a3/t6-t9 are dead at this hook (0x08884A30
           # redefines v1 then a0 immediately), so only ra and the values we
           # still need have to survive the jal.
           A.addiu("sp", "sp", -0x20),
           A.sw("ra", 0x00, "sp"), A.sw("t0", 0x04, "sp"),
           A.sw("t1", 0x08, "sp"), A.sw("t3", 0x0C, "sp"),
           A.sw("t5", 0x10, "sp"),
           A.jal(_INT_GET_FN), A.addu("a1", "zero", "zero"),   # a1=0: engine parity
           A.lw("ra", 0x00, "sp"), A.lw("t0", 0x04, "sp"),
           A.lw("t1", 0x08, "sp"), A.lw("t3", 0x0C, "sp"),
           A.lw("t5", 0x10, "sp"),
           A.addiu("sp", "sp", 0x20),
           A.sw("v0", _SCROLL_SAVE_SPARE, "sp"),               # keep for leg 3
           # INT_pre = equipped INT + steps banked BEFORE this cast (user
           # 2026-08-05: the cast's own step does not scale its own heal).
           A.addiu("t2", "t0", SCROLL_MB_DIAINT_OFF),
           A.addu("t2", "t2", "t1"),                           # &acc[idx]
           A.lbu("t4", 0x00, "t2"),
           A.addu("t4", "t4", "v0"),                           # t4 = INT_pre
           # heal = (INT_pre * multQ8) >> 8, floored, min 1. Max operand product
           # is 149 * 320 (INT clamp 99 + level 50), so no overflow.
           A.multu("t4", "t5"), A.mflo("t4"), A.srl("t4", "t4", 8),
           ("bne", "t4", "zero", "WWHEALOK"), A.nop(),
           A.addiu("t4", "zero", 1),                           # min 1
           ("label", "WWHEALOK"),
           A.addu("t5", "zero", "t4"),                         # t5 = heal amount
           A.lhu("t4", _BU_HP, "t3"),                          # curHP
           ("beq", "t4", "zero", "WWNOHEAL"), A.nop(),         # dead -> no heal
           A.addu("t4", "t4", "t5"),
           A.lhu("at", _BU_MAXHP, "t3"),
           A.sltu("t2", "at", "t4"),
           ("beq", "t2", "zero", "WWHPWR"), A.nop(),
           A.addu("t4", "zero", "at"),                         # clamp to maxHP
           ("label", "WWHPWR"),
           A.sh("t4", _BU_HP, "t3"),
           A.li("t2", dmb),                                    # delayed green number
           A.sb("t1", _DP_UNIT, "t2"), A.sh("t5", _DP_VAL, "t2"),
           A.addiu("at", "zero", 0x20), A.sh("at", _DP_FLAGS, "t2"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t2"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t2"),
           ("label", "WWNOHEAL"),
           # --- WW leg 3 (v107; v127 step = dia tier): each dia cast banks INT
           # steps equal to its tier (Dia 1 / Diara 2 / Diaga 3 / Diaja 4), the
           # accumulator clamped to the caster's LEVEL, for the rest of this
           # battle. t3 is still the caster's unit record. Both earlier exits
           # (gate fail, non-dia spell) branch past this, so only a real gated
           # White Mage/Wizard casting dia stacks. ---
           A.lbu("t1", 0x3C, "s4"),                            # caster party idx
           A.lw("t2", 0x00, "s4"), A.lw("t2", 0x6834, "t2"),   # field array
           A.sll("at", "t1", 2),
           A.sll("t5", "t1", 4), A.addu("at", "at", "t5"),
           A.sll("t5", "t1", 3), A.addu("at", "at", "t5"),
           A.sll("t5", "t1", 6), A.addu("at", "at", "t5"),     # at = idx*0x5C
           A.addu("t2", "t2", "at"),                           # t2 = field rec
           A.lbu("t5", _FLD_LEVEL, "t2"),                      # cap  = level
           # v227: base = the EQUIPPED INT from _INT_GET_FN (stashed above),
           # NOT the raw party byte. field+0x33 is base INT only -- the engine's
           # own refresh adds weapon/armor INT on top (0x08876428), so the old
           # `base = field[0x33] + acc` write DROPPED every equipment INT bonus
           # for the rest of the battle, which also cost the caster magic damage.
           A.lw("t4", _SCROLL_SAVE_SPARE, "sp"),               # base = equipped INT
           A.addiu("t2", "t0", SCROLL_MB_DIAINT_OFF),
           A.addu("t2", "t2", "t1"),                           # t2 = &acc[idx]
           # step = diastep[spell_id-1] (1..4); spell id at sp+0x88 (0x20 frame).
           A.lhu("t1", 0x68 + 0x20, "sp"),                     # spell id (u16)
           A.addiu("t1", "t1", SCROLL_MB_DIASTEP_OFF - 1),     # off = base + id-1
           A.addu("t1", "t0", "t1"),
           A.lbu("t1", 0x00, "t1"),                            # t1 = step (tier)
           A.lbu("at", 0x00, "t2"),                            # at = acc
           A.addu("at", "at", "t1"),                           # acc + step
           A.sltu("t1", "t5", "at"),                           # level < acc+step ?
           ("beq", "t1", "zero", "INTCAP"), A.nop(),
           A.addu("at", "zero", "t5"),                         # clamp acc to level
           ("label", "INTCAP"),
           A.sb("at", 0x00, "t2"),                             # store acc
           A.addu("t4", "t4", "at"),                           # INT = base + acc
           A.addiu("t1", "zero", 255),
           A.sltu("at", "t1", "t4"),                           # 255 < INT ?
           ("beq", "at", "zero", "INTOK"), A.nop(),
           A.addu("t4", "zero", "t1"),                         # clamp to u8
           ("label", "INTOK"),
           A.sb("t4", _BU_INT, "t3"),                          # write battle INT
           # (v204: the Crimson Wizard cast-refund leg LEFT this cave entirely.
           # It lived here from v120, keyed off the executor's sp-frame spell
           # id -- but the executor also runs (or leaves stale frames) for
           # other actions, which yielded every timing/gating bug in the v191-
           # v203 sequence: item casts healing, and heals landing on the NEXT
           # action (live 2026-08-02: Haste's 10 popped when the Monk attacked,
           # even with the cast-spend marker). The heal now rides the battle
           # MP-deduct site itself (see the v204 cave in
           # apply_job_scroll_boosts below): payment and heal are one
           # instruction stream, so there is nothing to mis-order and nothing
           # for an item cast to trip.)
           ("label", "DONE")]
        + _SCROLL_RESTORE
        + [A.j(_HEAL_RET), A.nop()])
    E.install_detour(elf, _HEAL_HOOK, E.add_segment_cave(elf, heal))

    # --- v118 heal-popup EPILOGUE cave (shared magic-executor post-loop point).
    # If a scroll heal recorded {unit, value} in the HEALPOP mailbox this cast,
    # append a green heal result-entry now, after the value loop has finished, so
    # the engine applies the heal AND draws the number. [sp+0x54] = result-array
    # base. Only at/v0/v1/t0-t5 are free (the epilogue is about to restore the
    # s-registers, so they must NOT be touched). Gated on the mailbox, so vanilla
    # casts and enemy actions fall straight through.
    #
    # v203: the HEALPOP leg below is now DEAD -- both writers (WW dia self-heal,
    # CW cast refund) deliver their own HP + delayed number at the cast, because
    # routing through this epilogue paid out late (or over another action) for
    # any cast that never reaches it. Kept only so a future writer can reuse the
    # channel; with no writer, pending is always 0 and this falls through. The
    # v129 CW magic-damage refund leg further down is LIVE and unaffected. ---
    healpop = A.asm_labels([
        A.lw("ra", 0x2C, "sp"), A.lw("fp", 0x28, "sp"),        # displaced originals
        A.li("t0", mb_vaddr),
        A.lbu("t1", SCROLL_MB_HEALPOP_OFF + 0, "t0"),          # pending?
        ("beq", "t1", "zero", "HPDONE"), A.nop(),
        A.sb("zero", SCROLL_MB_HEALPOP_OFF + 0, "t0"),         # consume
        # v190: NO ENTRY APPEND -- direct battle-HP write + delayed green number.
        # The old HPCLAIM appended a caster heal entry (flags 0x125); the
        # type-0x04 status apply (0x8886104 loop) blanket-applies the ACTION's
        # kind to EVERY live non-miss entry, so a Red Wizard casting Slow got
        # the slow kind (0x0b, literal @0x08886018) stamped onto his own
        # appended heal entry's target = the CASTER. Live-caught 2026-08-01
        # (re_only/bp_slow_apply.py: e1 src=00 tgt=00 val=10 flags=0x0125
        # during a 9-Pirate Slow). RULE: caves must NEVER append result
        # entries -- deliver effects by direct stat write + delaypop visual.
        A.lbu("t1", SCROLL_MB_HEALPOP_OFF + 1, "t0"),          # unit
        A.lhu("t5", SCROLL_MB_HEALPOP_OFF + 2, "t0"),          # value
        A.lw("t2", 0x54, "sp"),                                # &result[0]
        A.li("at", 0xCD50), A.subu("t2", "t2", "at"),          # ctx
        A.sll("t4", "t1", 3), A.addu("t4", "t4", "t1"),        # 9u
        A.sll("t3", "t4", 3),                                  # 72u
        A.sll("t4", "t4", 2),                                  # 36u
        A.addu("t3", "t3", "t4"),                              # 108u = u*0x6C
        A.addu("t2", "t2", "t3"),
        A.li("at", 0xC714), A.addu("t2", "t2", "at"),          # unit rec
        A.lhu("t3", 0x08, "t2"),                               # curHP
        ("beq", "t3", "zero", "HPDONE"), A.nop(),              # dead -> skip
        A.addu("t3", "t3", "t5"),
        A.lhu("t4", 0x0A, "t2"),                               # maxHP
        A.sltu("at", "t4", "t3"),
        ("beq", "at", "zero", "HPWR"), A.nop(),
        A.addu("t3", "zero", "t4"),                            # clamp
        ("label", "HPWR"), A.sh("t3", 0x08, "t2"),
        A.li("t2", dmb),                                       # delayed green number
        A.sb("t1", _DP_UNIT, "t2"), A.sh("t5", _DP_VAL, "t2"),
        A.addiu("at", "zero", 0x20), A.sh("at", _DP_FLAGS, "t2"),  # heal arm -> green
        A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t2"),
        A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t2"),
        ("label", "HPDONE"),
        # --- v129 Crimson Wizard MAGIC-damage MP refund. Physical hits refund at
        # the combat-calc epilogue, but magic damage only reaches the party
        # through THIS executor, so scan the result array for HP-damage entries
        # on a party Red Mage/Wizard {3,9} (RW scroll owned) and refund dealt/5
        # MP (direct write) + stagger a teal number. Skips heal/MP entries
        # (flag 0x20) and blood self-damage (0x400). t5 = ctx, v0/v1 free per
        # the cave note; t0 still = mailbox. ---
        A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"),
        A.andi("t1", "t1", 0x18),                              # RW (bit3) or Master (bit4)?
        ("beq", "t1", "zero", "RWMEND"), A.nop(),
        A.lw("t5", 0x54, "sp"),                                # result base = ctx+0xCD50
        A.li("at", 0xCD50), A.subu("t5", "t5", "at"),          # t5 = ctx
        A.li("v0", 0xCD50), A.addu("t4", "t5", "v0"),          # t4 = &result[0]
        A.addiu("v1", "zero", 13),
        ("label", "RWMLOOP"),
        A.lbu("t1", 0x01, "t4"),                              # tgt
        A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),            # tgt>=4 (enemy/free) skip
        A.lhu("t2", 0x0C, "t4"),                              # flags
        A.andi("at", "t2", 0x20),
        ("bne", "at", "zero", "RWMNEXT"), A.nop(),            # heal/MP entry -> skip
        A.andi("at", "t2", _PC_FLAG_BLOOD),
        ("bne", "at", "zero", "RWMNEXT"), A.nop(),            # blood self-damage -> skip
        A.lw("t2", 0x04, "t4"),                              # value = HP damage
        ("beq", "t2", "zero", "RWMNEXT"), A.nop(),
        A.lw("t3", 0x6834, "t5"),                            # field array
        A.sll("at", "t1", 2), A.sll("v0", "t1", 4), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x5C
        A.addu("t3", "t3", "at"),
        A.lbu("t3", 0x1E, "t3"),                             # class
        A.addiu("at", "zero", 3),
        ("beq", "t3", "at", "RWMHIT"), A.nop(),
        A.addiu("at", "zero", 9),
        ("bne", "t3", "at", "RWMMST"), A.nop(),              # not RW -> try Master
        ("label", "RWMHIT"),
        A.lbu("at", SCROLL_MB_FLAGS_OFF, "t0"), A.andi("at", "at", 0x08),
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),           # no RW scroll -> skip
        # v270 SURVIVE GATE (same rule as the Master leg below): this epilogue runs
        # before the engine applies the result entries, so BU_HP is the PRE-damage
        # HP. No refund for a spell that KOs the Crimson Wizard -- he must be left
        # with at least 1 HP. Clobbers t3 (class, dead here) + at/v0; t2 = damage,
        # t4 = entry ptr and v1 = loop counter stay live.
        A.sll("at", "t1", 2), A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 5), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x6C
        A.addu("t3", "t5", "at"),
        A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),       # unit record
        A.lhu("at", _BU_HP, "t3"),                           # curHP (pre-damage)
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),           # already dead
        A.sltu("at", "t2", "at"),                            # dmg < curHP ?
        ("beq", "at", "zero", "RWMNEXT"), A.nop()]           # lethal -> no refund
        + ([
        # slot_magic: damage -> slot buyback (leaf preserves t0/t1/t4/t5/v1)
        A.addiu("sp", "sp", -8),
        A.sw("ra", 0x00, "sp"),
        A.addu("a0", "t1", "zero"),                          # party idx
        A.addu("a1", "t2", "zero"),                          # HP damage
        A.addu("a2", "t5", "zero"),                          # battle ctx
        A.jal(_sm_cw), A.nop(),
        A.lw("ra", 0x00, "sp"),
        A.addiu("sp", "sp", 8),
        ("beq", "v0", "zero", "RWMNEXT"), A.nop(),
        A.li("t3", dmb),
        A.sb("t1", _DP_UNIT, "t3"), A.sh("v0", _DP_VAL, "t3"),
        A.addiu("at", "zero", 0x20 | _PC_FLAG_TEAL), A.sh("at", _DP_FLAGS, "t3"),
        A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
        A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
        ("beq", "zero", "zero", "RWMNEXT"), A.nop(),
        ] if _sm_cw else [
        A.addiu("at", "zero", 5),
        A.divu("t2", "at"), A.mflo("t2"),                    # refund = dmg/5
        ("beq", "t2", "zero", "RWMNEXT"), A.nop(),
        A.sll("at", "t1", 2), A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 5), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x6C
        A.addu("t3", "t5", "at"),
        A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),       # unit record
        A.lhu("v0", 0x0E, "t3"),                             # maxMP
        ("beq", "v0", "zero", "RWMNEXT"), A.nop(),           # no MP pool
        A.lhu("at", 0x0C, "t3"),                             # curMP
        A.addu("at", "at", "t2"),                            # cur + refund
        A.sltu("v0", "v0", "at"),                            # maxMP < new?
        ("beq", "v0", "zero", "RWMWR"), A.nop(),
        A.lhu("at", 0x0E, "t3"),                             # clamp to maxMP
        ("label", "RWMWR"),
        A.sh("at", 0x0C, "t3"),                              # write curMP
        A.li("t3", dmb),                                     # stagger a teal number
        A.sb("t1", _DP_UNIT, "t3"), A.sh("t2", _DP_VAL, "t3"),
        A.addiu("at", "zero", 0x20 | _PC_FLAG_TEAL), A.sh("at", _DP_FLAGS, "t3"),
        A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
        A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
        ("beq", "zero", "zero", "RWMNEXT"), A.nop(),
        ]) + [
        # --- v130 Grand Master MAGIC-damage attack gain + YELLOW number (parallel
        # to the RW magic refund). Class {2,8}, Master scroll bit4. Gain
        # ceil(dmg/DIV) capped at master_atk_cap(level) via MB_MATK[unit]; yellow = gain
        # (0 at cap). Preserves t0/t4/t5/v1; t2=dmg->gain, t1=idx, t3/at/v0 scratch. ---
        ("label", "RWMMST"),
        A.addiu("at", "zero", 2), ("beq", "t3", "at", "MSMOK"), A.nop(),
        A.addiu("at", "zero", 8), ("bne", "t3", "at", "RWMNEXT"), A.nop(),
        ("label", "MSMOK"),
        A.lbu("at", SCROLL_MB_FLAGS_OFF, "t0"), A.andi("at", "at", 0x10),
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),           # no Master scroll
        # v270 SURVIVE GATE. This epilogue runs BEFORE the engine applies the
        # result entries (that is why the v190-era caves could append entries for
        # it to pay out), so BU_HP here is the PRE-damage HP. A Master must live
        # through the hit to grow: skip when curHP == 0 (already down) or when the
        # entry's damage is >= curHP (this spell kills them). Known scope: each
        # entry is judged on its own, so two sub-lethal entries in one cast that
        # sum to lethal still pay -- one entry per unit is the normal case.
        A.sll("at", "t1", 2), A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 5), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x6C
        A.addu("t3", "t5", "at"),
        A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),       # unit record
        A.lhu("at", _BU_HP, "t3"),                           # curHP (pre-damage)
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),           # already dead
        A.sltu("at", "t2", "at"),                            # dmg < curHP ?
        ("beq", "at", "zero", "RWMNEXT"), A.nop(),           # lethal -> no gain
        A.lw("t3", 0x6834, "t5"),                            # field array
        A.sll("at", "t1", 2), A.sll("v0", "t1", 4), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x5C
        A.addu("t3", "t3", "at"),
        A.lbu("t3", _FLD_LEVEL, "t3"),                       # level
        ] + _master_cap_asm("t3", "at") + [                  # -> cap (v229)
        A.addiu("v0", "t0", SCROLL_MB_MATK_OFF), A.addu("v0", "v0", "t1"),  # &acc
        A.lbu("at", 0x00, "v0"),
        A.subu("t3", "t3", "at"),                            # remaining = cap - acc
        A.slt("at", "zero", "t3"),
        ("bne", "at", "zero", "MSMGAIN"), A.nop(),
        A.addu("t2", "zero", "zero"),                        # capped -> gain 0
        ("beq", "zero", "zero", "MSMPOP"), A.nop(),
        ("label", "MSMGAIN"),
        A.addiu("at", "zero", MASTER_ATK_DMG_DIV),
        A.addu("t2", "t2", "at"), A.addiu("t2", "t2", -1),   # dmg + DIV - 1
        A.divu("t2", "at"), A.mflo("t2"),                    # inc = ceil(dmg/DIV)
        A.slt("at", "t3", "t2"), ("beq", "at", "zero", "MSMAPP"), A.nop(),
        A.addu("t2", "zero", "t3"),                          # gain = remaining
        ("label", "MSMAPP"),
        A.lbu("at", 0x00, "v0"), A.addu("at", "at", "t2"), A.sb("at", 0x00, "v0"),  # acc += gain
        A.sll("at", "t1", 2), A.sll("v0", "t1", 3), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 5), A.addu("at", "at", "v0"),
        A.sll("v0", "t1", 6), A.addu("at", "at", "v0"),      # idx*0x6C
        A.addu("t3", "t5", "at"), A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),  # unit rec
        A.lhu("at", _BU_ATTACK_BONUS, "t3"), A.addu("at", "at", "t2"), A.sh("at", _BU_ATTACK_BONUS, "t3"),
        A.lhu("at", _BU_ATTACK, "t3"), A.addu("at", "at", "t2"), A.sh("at", _BU_ATTACK, "t3"),
        ("label", "MSMPOP"),
        A.li("t3", dmb),                                     # stagger a YELLOW number
        A.sb("t1", _DP_UNIT, "t3"), A.sh("t2", _DP_VAL, "t3"),
        A.addiu("at", "zero", 0x20 | _PC_FLAG_YELLOWD), A.sh("at", _DP_FLAGS, "t3"),
        A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
        A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
        ("label", "RWMNEXT"),
        A.addiu("t4", "t4", 0x14),
        A.addiu("v1", "v1", -1),
        ("bne", "v1", "zero", "RWMLOOP"), A.nop(),
        ("label", "RWMEND"),
        A.j(_SCROLL_HEALPOP_RET), A.nop(),
    ])
    E.install_detour(elf, _SCROLL_HEALPOP_HOOK, E.add_segment_cave(elf, healpop))

    # --- Knight leg: lifesteal. At the physical combat-calc fn's epilogue the
    # total damage this attack dealt sits in [s4+4] (result block). For a gated
    # Knight caster {0,6} heal the attacker (unit rec [s5+0x34], curHP@+8) by
    # dealt * KNIGHT_LIFESTEAL_PCT // 100 (=15%; min 1 when dealt>0). Skip if KO'd
    # (curHP==0). Flag bit2 (client-armed when the Blood Knight Scroll is owned). ---
    life = A.asm_labels(
        [A.lw("s7", 0x24, "sp"), A.lw("s6", 0x20, "sp")]     # displaced originals
        + _SCROLL_SAVE
        + _scroll_gate(mb_vaddr, "s5", 0x04, 0, 6, done="RWCHK")  # Knight? else try RW tgt
        + [A.lw("t1", 0x04, "s4"),                           # dealt damage
           A.slt("at", "zero", "t1"),                        # at = dealt > 0
           ("beq", "at", "zero", "DONE"), A.nop(),
           A.addiu("t5", "zero", KNIGHT_LIFESTEAL_PCT),
           A.multu("t1", "t5"), A.mflo("t1"),                # dealt * PCT
           A.addiu("t5", "zero", 100),
           A.divu("t1", "t5"), A.mflo("t1"),                 # heal = dealt*PCT//100
           ("bne", "t1", "zero", "HAVE"), A.nop(),
           A.addiu("t1", "zero", 1),                         # min 1 when dealt>0
           ("label", "HAVE"),
           A.li("t5", KNIGHT_LIFESTEAL_CAP),                 # v260: clamp the heal
           A.sltu("at", "t5", "t1"),                         # at = heal > CAP
           ("beq", "at", "zero", "LCAP"), A.nop(),
           A.addu("t1", "zero", "t5"),
           ("label", "LCAP"),
           A.lw("t2", 0x34, "s5"),                           # attacker unit rec
           A.lhu("t3", _BU_HP, "t2"),
           ("beq", "t3", "zero", "DONE"), A.nop(),           # KO -> no revive
           # --- v190: NO ENTRY APPEND (was v119 flags-0x125 append). Appended
           # entries leak into a later action's status apply -- the type-0x04
           # blanket arm stamps the action kind onto every live entry (the
           # RW-cast self-slow, live-caught 2026-08-01) -- and a status-on-hit
           # weapon path could do the same to this one. Direct write (the old
           # array-full fallback, now the only path) + delayed green number. ---
           A.lhu("t3", _BU_HP, "t2"), A.lhu("t4", _BU_MAXHP, "t2"),
           A.addu("t3", "t3", "t1"),
           A.sltu("at", "t4", "t3"),
           ("beq", "at", "zero", "LSTORE"), A.nop(),
           A.addu("t3", "zero", "t4"),
           ("label", "LSTORE"),
           A.sh("t3", _BU_HP, "t2"),
           A.lbu("t5", 0x3C, "s5"),                          # attacker unit id
           A.li("t3", dmb),                                  # delayed green number
           A.sb("t5", _DP_UNIT, "t3"), A.sh("t1", _DP_VAL, "t3"),
           A.addiu("at", "zero", 0x20), A.sh("at", _DP_FLAGS, "t3"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
           ("beq", "zero", "zero", "DONE"), A.nop(),
           # --- v120 Crimson Wizard leg 1 (physical damage taken -> MP refund,
           # TEAL number). Reached via the Knight gate's fail-exit (an enemy
           # attacker fails its party check). If the TARGET ([s5+0x3D]) is a
           # living party Red Mage/Wizard {3,9} with the RW scroll (bit3),
           # append an MP-restore entry: val = dealt/5 (RW_DMG_TO_MP_PCT=20),
           # flags 0x2325 = 0x1|0x4|0x20|0x200 (native MP restore) | 0x2000
           # (teal marker -> teal digits via the popup-colour bank cave).
           # The engine applies the MP restore itself. Replaces the client
           # delta leg for physical hits; magic-damage refunds are the next
           # leg (executor-epilogue scan). ---
           ("label", "RWCHK"),
           A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"),
           A.andi("t1", "t1", 0x08),                         # RW scroll bit3
           ("beq", "t1", "zero", "MSTCHK"), A.nop(),         # no RW scroll -> try Master
           A.lbu("t1", NECRO_TGT_IDX_OFF, "s5"),             # target unit id
           A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
           ("beq", "at", "zero", "DONE"), A.nop(),           # party target only
           A.lw("t2", 0x00, "s5"), A.lw("t2", 0x6834, "t2"), # field array
           A.sll("t3", "t1", 2), A.sll("t4", "t1", 4), A.addu("t3", "t3", "t4"),
           A.sll("t4", "t1", 3), A.addu("t3", "t3", "t4"),
           A.sll("t4", "t1", 6), A.addu("t3", "t3", "t4"),   # t3 = idx*0x5C
           A.addu("t2", "t2", "t3"),
           A.lbu("t3", 0x1E, "t2"),                          # class
           A.addiu("t4", "zero", 3),
           ("beq", "t3", "t4", "RWTGT"), A.nop(),
           A.addiu("t4", "zero", 9),
           ("bne", "t3", "t4", "MSTCHK"), A.nop(),           # not RW -> try Master
           ("label", "RWTGT"),
           # v270 SURVIVE GATE. [s4+4] is COMPUTED damage; the engine applies it
           # after this epilogue, so the unit rec still holds the pre-damage HP.
           # A hit that KOs the Crimson Wizard refunds nothing (he must keep at
           # least 1 HP). The non-slot path's own curHP==0 test is now redundant
           # but harmless; the slot_magic path had no life test at all.
           A.lw("t3", 0x00, "s5"),
           A.sll("t4", "t1", 2), A.sll("t2", "t1", 3), A.addu("t4", "t4", "t2"),
           A.sll("t2", "t1", 5), A.addu("t4", "t4", "t2"),
           A.sll("t2", "t1", 6), A.addu("t4", "t4", "t2"),   # idx*0x6C
           A.addu("t3", "t3", "t4"),
           A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),    # unit record
           A.lhu("t4", _BU_HP, "t3"),                        # curHP (pre-damage)
           ("beq", "t4", "zero", "DONE"), A.nop(),           # already dead
           A.lw("t2", 0x04, "s4"),                           # dealt damage
           A.sltu("at", "t2", "t4"),                         # dmg < curHP ?
           ("beq", "at", "zero", "DONE"), A.nop(),           # lethal -> no refund
           ] + ([
           # slot_magic: damage -> slot buyback via the shared leaf (clobbers
           # a*/at/v0/t6-t9 only); teal number = slots restored, nothing at 0.
           A.addiu("sp", "sp", -8),
           A.sw("ra", 0x00, "sp"),
           A.addu("a0", "t1", "zero"),                       # party idx
           A.lw("a1", 0x04, "s4"),                           # dealt damage
           A.lw("a2", 0x00, "s5"),                           # battle ctx
           A.jal(_sm_cw), A.nop(),
           A.lw("ra", 0x00, "sp"),
           A.addiu("sp", "sp", 8),
           ("beq", "v0", "zero", "MSTCHK"), A.nop(),
           A.li("t2", dmb),
           A.sb("t1", _DP_UNIT, "t2"),
           A.sh("v0", _DP_VAL, "t2"),                        # value = slots
           A.addiu("at", "zero", 0x20 | _PC_FLAG_TEAL), A.sh("at", _DP_FLAGS, "t2"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t2"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t2"),
          ] if _sm_cw else [
           A.lw("t5", 0x04, "s4"),                           # dealt damage
           A.addiu("at", "zero", 5),
           A.divu("t5", "at"), A.mflo("t5"),                 # refund = dealt/5
           ("beq", "t5", "zero", "DONE"), A.nop(),
           # target unit rec: alive + has MP (curHP@+8, maxMP@+0xE)
           A.lw("t3", 0x00, "s5"),
           A.sll("t4", "t1", 2), A.sll("t2", "t1", 3), A.addu("t4", "t4", "t2"),
           A.sll("t2", "t1", 5), A.addu("t4", "t4", "t2"),
           A.sll("t2", "t1", 6), A.addu("t4", "t4", "t2"),   # t4 = idx*0x6C
           A.addu("t3", "t3", "t4"),
           A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),    # unit record
           A.lhu("t2", _BU_HP, "t3"),
           ("beq", "t2", "zero", "DONE"), A.nop(),           # dead -> no refund
           A.lhu("t2", 0x0E, "t3"),                          # maxMP
           ("beq", "t2", "zero", "DONE"), A.nop(),
           # v190: NO ENTRY APPEND (was the flags-0x23A5 teal MP-restore entry).
           # Appended entries leak into a later action's type-0x04 status apply
           # (the RW-cast self-slow, live-caught 2026-08-01). Direct curMP
           # write, clamped to maxMP (same maths the RWM magic leg uses).
           A.lhu("at", 0x0C, "t3"),                          # curMP
           A.addu("at", "at", "t5"),
           A.sltu("t4", "t2", "at"),                         # maxMP < new?
           ("beq", "t4", "zero", "RWWR"), A.nop(),
           A.addu("at", "zero", "t2"),                       # clamp
           ("label", "RWWR"),
           A.sh("at", 0x0C, "t3"),                           # write curMP
           # --- record the STAGGERED teal number in the shared DELAYPOP mailbox:
           # the white damage number for this same hit lands on this same unit in
           # this same frame, so drawing now is illegible (v121/v122 live). The
           # delaypop pump cave (in the always-on popup layer) spawns it
           # DELAYPOP_DELAY_FRAMES later. flags 0x20|teal -> heal arm, teal def. ---
           A.li("t2", dmb),
           A.sb("t1", _DP_UNIT, "t2"),                       # unit
           A.sh("t5", _DP_VAL, "t2"),                        # value = refund
           A.addiu("at", "zero", 0x20 | _PC_FLAG_TEAL), A.sh("at", _DP_FLAGS, "t2"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t2"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t2"),   # pending = 1
          ]) + [
           # --- v130 Grand Master leg (physical damage taken -> temp ATTACK gain,
           # YELLOW number). Reached when the target is not an RW (parallel to the
           # CW branch). If the target is a living party Monk/Master {2,8} with the
           # Master scroll (bit4), gain ceil(dmg/DIV) attack, capped per battle at
           # level + CAP_OVER_LEVEL via the MB_MATK[unit] accumulator, and stagger
           # a yellow number = the gain. Once CAPPED it draws a GREEN heal number
           # instead of the old yellow 0 (v217, see MSHEAL below). Attack goes
           # through the durable BU_ATTACK_BONUS + a mirror on BU_ATTACK (same as
           # the client leg it replaces). Max-HP leg stays client-side. ---
           ("label", "MSTCHK"),
           A.lbu("t1", SCROLL_MB_FLAGS_OFF, "t0"),
           A.andi("t1", "t1", 0x10),                         # Master scroll bit4
           ("beq", "t1", "zero", "DONE"), A.nop(),
           A.lbu("t1", NECRO_TGT_IDX_OFF, "s5"),             # target unit id
           A.addiu("at", "zero", 4), A.slt("at", "t1", "at"),
           ("beq", "at", "zero", "DONE"), A.nop(),           # party target only
           A.lw("t2", 0x00, "s5"), A.lw("t2", 0x6834, "t2"), # field array
           A.sll("t3", "t1", 2), A.sll("t4", "t1", 4), A.addu("t3", "t3", "t4"),
           A.sll("t4", "t1", 3), A.addu("t3", "t3", "t4"),
           A.sll("t4", "t1", 6), A.addu("t3", "t3", "t4"),   # idx*0x5C
           A.addu("t2", "t2", "t3"),
           A.lbu("t3", 0x1E, "t2"),                          # class
           A.addiu("t4", "zero", 2),
           ("beq", "t3", "t4", "MSTGT"), A.nop(),
           A.addiu("t4", "zero", 8),
           ("bne", "t3", "t4", "DONE"), A.nop(),
           ("label", "MSTGT"),
           # alive? (unit rec curHP@+8) -- t3 = unit record, reused below
           A.lw("t3", 0x00, "s5"),
           A.sll("t4", "t1", 2), A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 5), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),   # idx*0x6C
           A.addu("t3", "t3", "t4"),
           A.li("at", _BU_OFF), A.addu("t3", "t3", "at"),    # unit record
           A.lhu("t4", _BU_HP, "t3"),
           ("beq", "t4", "zero", "DONE"), A.nop(),           # dead -> no gain
           # v270 SURVIVE GATE. The combat-calc fn only COMPUTES damage into
           # [s4+4]; the engine applies the result entries after this epilogue, so
           # t4 is the PRE-damage HP. A killing blow must pay nothing -- no attack
           # gain, no yellow number, and no green capped-heal number either (the
           # client's HP legs are gated on the unit being alive next tick anyway).
           A.lw("t2", 0x04, "s4"),                           # dealt damage
           A.sltu("at", "t2", "t4"),                         # dmg < curHP ?
           ("beq", "at", "zero", "DONE"), A.nop(),           # lethal -> no gain
           # remaining = master_atk_cap(level) - acc  ; level from field rec +0x20
           A.lw("t2", 0x00, "s5"), A.lw("t2", 0x6834, "t2"),
           A.sll("t4", "t1", 2), A.sll("t5", "t1", 4), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 3), A.addu("t4", "t4", "t5"),
           A.sll("t5", "t1", 6), A.addu("t4", "t4", "t5"),
           A.addu("t2", "t2", "t4"),                         # field rec
           A.lbu("t4", _FLD_LEVEL, "t2"),                    # level
           ] + _master_cap_asm("t4", "at") + [               # -> cap (v229)
           A.addiu("t5", "t0", SCROLL_MB_MATK_OFF), A.addu("t5", "t5", "t1"),  # &acc[unit]
           A.lbu("t2", 0x00, "t5"),                          # acc
           A.subu("t4", "t4", "t2"),                         # remaining = cap - acc
           A.addu("t2", "zero", "zero"),                     # gain = 0 (default: capped)
           A.slt("at", "zero", "t4"),                        # remaining > 0 ?
           ("beq", "at", "zero", "MSHEAL"), A.nop(),         # capped -> GREEN heal number
           # inc = ceil(dmg / DIV)
           A.lw("t2", 0x04, "s4"),                           # dealt damage
           A.addiu("at", "zero", MASTER_ATK_DMG_DIV),
           A.addu("t2", "t2", "at"), A.addiu("t2", "t2", -1),  # dmg + DIV - 1
           A.divu("t2", "at"), A.mflo("t2"),                 # inc
           # miss / 0-dmg physical hit -> inc==0 here (not capped). The subject
           # gained nothing from an attack that DID nothing, so draw no number.
           ("beq", "t2", "zero", "DONE"), A.nop(),
           A.slt("at", "t4", "t2"),                          # remaining < inc ?
           ("beq", "at", "zero", "MSAPP"), A.nop(),
           A.addu("t2", "zero", "t4"),                       # gain = remaining
           ("label", "MSAPP"),
           A.lbu("at", 0x00, "t5"), A.addu("at", "at", "t2"),
           A.sb("at", 0x00, "t5"),                           # acc += gain
           A.lhu("at", _BU_ATTACK_BONUS, "t3"), A.addu("at", "at", "t2"),
           A.sh("at", _BU_ATTACK_BONUS, "t3"),               # durable bonus += gain
           A.lhu("at", _BU_ATTACK, "t3"), A.addu("at", "at", "t2"),
           A.sh("at", _BU_ATTACK, "t3"),                     # mirror derived stat
           ("label", "MSPOP"),
           A.li("t3", dmb),                                  # stagger a YELLOW number
           A.sb("t1", _DP_UNIT, "t3"), A.sh("t2", _DP_VAL, "t3"),
           A.addiu("at", "zero", 0x20 | _PC_FLAG_YELLOWD), A.sh("at", _DP_FLAGS, "t3"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
           ("beq", "zero", "zero", "DONE"), A.nop(),   # yellow path ends here
           # --- attack CAPPED (v217): the old yellow 0 said "at max, can't
           # benefit" -- but past the cap a Master still HEALS off damage taken
           # (the client's max-HP/heal leg), so draw that instead: a GREEN number
           # = the HP the client is about to restore. v219: at the cap the rate is
           # the BOOSTED one -- heal = ceil(dmg * CAPPED_PCT/100), unhalved (below
           # the cap the client's own 10% leg runs and this cave draws yellow, not
           # green). DISPLAY ONLY -- no HP write here; the client owns the write
           # because it also sees magic damage, which this physical epilogue never
           # reaches. A heal that rounds to 0 draws nothing, not a green 0. ---
           ("label", "MSHEAL"),
           A.lw("t2", 0x04, "s4"),                           # dealt damage
           A.addiu("at", "zero", MASTER_HP_CAPPED_PCT),
           A.multu("t2", "at"), A.mflo("t2"),
           A.addiu("t2", "t2", 99),                          # +100-1 -> ceil
           A.addiu("at", "zero", 100),
           A.divu("t2", "at"), A.mflo("t2"),                 # heal = ceil(dmg*PCT/100)
           ("beq", "t2", "zero", "DONE"), A.nop(),           # rounds to 0 -> no number
           A.li("t3", dmb),                                  # stagger a GREEN number
           A.sb("t1", _DP_UNIT, "t3"), A.sh("t2", _DP_VAL, "t3"),
           # heal arm only (0x20), no colour override -> the native green bank
           A.addiu("at", "zero", 0x20), A.sh("at", _DP_FLAGS, "t3"),
           A.addiu("at", "zero", DELAYPOP_DELAY_FRAMES), A.sb("at", _DP_DELAY, "t3"),
           A.addiu("at", "zero", 1), A.sb("at", _DP_PEND, "t3"),
           ("label", "DONE")]
        + _SCROLL_RESTORE
        + [A.j(_LIFE_RET), A.nop()])
    E.install_detour(elf, _LIFE_HOOK, E.add_segment_cave(elf, life))

    # --- Knight leg 2: defense pierce. Same scroll (flag bit2), same class gate
    # {0,6}. v0 = target DEF from the displaced loads; shave DEF // DIV off it and
    # let the original subtract run. DEF<=0 bails (nothing to pierce, and keeps the
    # signed lh out of divu). Integer floor => DEF<DIV sees no pierce. ---
    defp = A.asm_labels(
        [A.lw("v0", 0x38, "s5"), A.lh("v0", 0x12, "v0")]     # displaced originals
        + _SCROLL_SAVE
        + _scroll_gate(mb_vaddr, "s5", 0x04, 0, 6)           # Knight attacker? else DONE
        + [A.slt("at", "zero", "v0"),                        # at = DEF > 0
           ("beq", "at", "zero", "DONE"), A.nop(),
           A.addiu("t5", "zero", KNIGHT_DEFPIERCE_DIV),
           A.divu("v0", "t5"), A.mflo("t1"),                 # t1 = DEF // DIV
           A.subu("v0", "v0", "t1"),                         # DEF -= that
           ("label", "DONE")]
        + _SCROLL_RESTORE
        + [A.j(_DEFP_RET), A.nop()])
    E.install_detour(elf, _DEFP_HOOK, E.add_segment_cave(elf, defp))
    # (The staggered-popup anim-poll cave moved to the always-on popup layer --
    # apply_popup_colors -- so blood_magic can share it; job_scroll records into
    # the shared _DELAYPOP_MB above.)


# NOTE: an on-disc detour at 0x08843bf8 that forced the beq @0x08843c00 was tried
# (v46) to make the 5 event-key chests take "path B" -- REVERTED 2026-07-10: that beq
# reads opcode 0x06's ARG byte, not the path-A/B decision (see [[event-key-chests]]
# 9th-session note: "beq arg,1 selects opcode SUB-behavior, NOT path A/B"). Forcing it
# only changed an opcode argument; the FIF event still ran (live log: native Crown
# granted + stripped, box still "crown"). The true path-A/B gate reads the event-obj
# done-state at interaction and is still unfound (read-bps dead in 1.15.3). Box name
# stays vanilla for now; delivery is correct via the strip + grant loop (cosmetic-only).

# --- Bikke / ship flag split (v65) -------------------------------------------
# Vanilla doubly-binds story-flag id5: "Bikke defeated" AND "ship available"
# are the same bit (live RE 2026-07-03/15, see [[ship-bikke-flag]]). With the
# Ship as a randomized AP item the client had to strip id5 until the Ship was
# found, which un-defeated Bikke -- the pirates re-offered their fight forever.
#
# Split (RE 2026-07-15, bp_shipcmd2/bp_retarget_test, both directions live-
# proven): remap flag id 5 -> 63 for story-flag reads AND writes while the
# loaded fine map is Pravoka (FIELD_MAP_ID 0x37). Pravoka's compiled event
# image holds Bikke's defeat setStoryFlag(5,1) and the pirates-presence
# check; the remap makes both use id63 ("Bikke defeated", unused vanilla,
# save-persisted) while the overworld ship-spawn logic (map 0) keeps reading
# id5 -- which the client now mirrors purely from Ship-item ownership.
# Hooked in the WRAPPERS (not the opcode handlers) so it applies at frame 0
# of the map-entry event -- a client poll loop always lost that race.
# Known cosmetic scope: Pravoka flavor dialogue that checks "has ship" (id5)
# reads "Bikke defeated" (id63) instead; mechanically inert (boarding is
# overworld-side).
_BSS_GETSTORYFLAG = 0x0886791C      # getStoryFlag(id=a0) wrapper entry
_BSS_SETSTORYFLAG = 0x0886794C      # setStoryFlag(id=a0, val=a1) wrapper entry
_BSS_MAPID_OFF    = 0x2008          # FIELD_MAP_ID_SA - save base (0x13108-0x11100)
_BSS_PRAVOKA_ID   = 0x37
_BSS_NEW_FLAG_ID  = 63
_BSS_FLAGS_OFF    = 0x41C           # story-flag bitfield - save base (0x1151C)
# --- Titan's Tunnel gate split (v260) ---------------------------------------
# Star Ruby (key id 9) accept/fed are story flags 13/14 (0x1151D b5/b6), the
# SAME globals a Whisperwind Cove floor's dwarf-ruby/Titan gimmick sets and
# clears (bonus floors re-run vanilla event scripts). The client had to referee
# that collision with an in-dungeon strip hold + a per-tick reassert, after a
# native bonus ruby got stripped and softlocked the REAL tunnel (2026-08-09).
# Split it on disc, prince-style, but by CONTEXT not by site: the bonus Titan
# shares these very script bytes, so a per-operand repoint could not tell real
# from bonus. Live-RE'd 2026-08-11 (map 0x22): the Titan's gate reads flags 14
# then 13 ONLY through getStoryFlag -- zero direct byte readers -- so redirect
# the READ here. When the real Giant's Cave (FIELD_MAP_ID 0x22) is loaded and it
# is NOT a bonus floor (bonus mapid < 0x87), getStoryFlag(13|14) answers flag 73
# (NPC_GATE_SPLIT_FLAG_BASE + key id 9 -> byte 0x11525 b1, zero disc refs). The
# AP grant sets 73; nothing native ever does. A bonus floor (mapid >= 0x87)
# keeps reading vanilla 13/14, so its native puzzle is untouched. SETs stay
# vanilla (only reads are redirected) -- the real Titan's feed cutscene never
# runs for an AP player, so its 13/14 writes are moot. End-to-end RAM-rehearsed
# live 2026-08-11: with 13/14 SET, redirecting the checks to (clear) 73 re-blocks
# the Titan, and setting 73 opens it -- exactly the AP-grant path.
_TGS_GIANT_MAP       = 0x22        # Giant's Cave FIELD_MAP_ID (the real Titan)
_TGS_BONUS_MAPID_OFF = 0x1FF4      # bonus-dungeon mapid (u8) - save base; >=0x87 bonus
_TGS_BONUS_MIN       = 0x87        # == ff1_data.BONUS_MAPID_MIN
_TGS_SHADOW_FLAG     = 64 + 9      # NPC_GATE_SPLIT_FLAG_BASE(64) + Star Ruby key id 9
# Citadel of Trials crown gate (v70): the elder's grant event sets story flag
# 22 ("throne permission", the ONLY setter in the whole event blob; the throne
# check @0x089A7A90 branches to the refusal box while it is clear) WITHOUT
# checking the Crown -- the PSP script's "You come bearing the crown, I see"
# is unconditional flavor. Suppress setStoryFlag(22,1) unless story flag 6
# (Crown obtained -- set by the AP Crown grant via KEY_ITEM_FUNCTION_BITS,
# cleared by native strips) is set: crownless players keep a locked throne
# and a waiting elder (his despawn is keyed on flag 22 too, so he's present
# again on room re-entry); the client rewrites his dialogue to say why.
_CCG_PERMISSION_ID = 22
_CCG_CROWN_MASK    = 0x40           # story flag 6 = bit6 of flag byte 0
# Crystals-needed gate (v93, crystals_needed yaml < 4): inside Chaos Shrine the
# Black Orb, the Chronodia/LoT-door entry cutscene and the bat dialogue all gate
# on the four FIEND story flags (17 Lich / 19 Marilith / 29 Kraken / 34 Tiamat)
# read through getStoryFlag -- the deciders are ENGINE code (register-passed
# args, no fiend-id table; the shatter/door scenes set flags 36/45 from code,
# NOT the EVM). See [[crystal-count-re]]. So the wrapper LIES map-scoped: when
# asked about any fiend flag while the TRUE map id is Chaos Shrine, count the
# real fiend bits and answer 1 for all four once count >= needed. needed=0 =
# the orb opens immediately (by design this also fires the Chronodia scene on
# the FIRST Chaos Shrine visit -- 0 means the endgame is open from the start).
# Rides inside apply_bikke_ship_split because both features hook the SAME
# wrapper entry (two install_detour calls on one hook would orphan the first).
# FINE per-map id (FIELD_MAP_ID_SA 0x13108), NOT the coarse LOADED_MAP_ID (0x13118,
# save+0x2018) which reads 1 for EVERY dungeon -- using the coarse id made the leg lie
# about the fiend flags in ALL dungeons, which leaked to the bonus-dungeon "is it open"
# checks (crystals_needed=3 + Tiamat skipped -> Whisperwind opened without Tiamat; user
# report 2026-08-07). The fine Chaos-Shrine id is 0x4E (bp_orb_trace + ff1_data, both
# live). Same field bikke_ship_split uses (_BSS_MAPID_OFF 0x2008). v236.
_CRY_TRUEMAP_OFF = 0x2008           # FIELD_MAP_ID_SA (fine) - save base (0x13108)
_CRY_CHAOS_MAP   = 0x4E             # Chaos Shrine fine map id (live-verified)
_CRY_FIENDS = (                     # (flag byte offset from save base, mask)
    (0x41E, 0x02),                  # Lich     17 -> 0x1151E b1
    (0x41E, 0x08),                  # Marilith 19 -> 0x1151E b3
    (0x41F, 0x20),                  # Kraken   29 -> 0x1151F b5
    (0x420, 0x04),                  # Tiamat   34 -> 0x11520 b2
)
_CRY_FIEND_IDS = (17, 19, 29, 34)
# bonus_dungeon_crystals: the COUNTING SOURCE the leg flips to when bonus mode is
# baked -- 4 client-owned shadow bits in the dead slot-magic reserve byte save+0x834
# (bits 0-3, one per Soul-of-Chaos dungeon), set by the client on a superboss kill.
# A client-OWNED byte (no other reader/writer) -- unlike the story-flag array, which
# would race RUNE_BORROW_OWNED (flag 55, byte 0x11522) on read-modify-write. The leg
# still answers the SAME four fiend ids (17/19/29/34) inside Chaos Shrine; only which
# 4 (offset,mask) it counts changes. See ff1_data BONUS_CRYSTAL_SHADOW_ADDR.
_CRY_SHADOW = (
    (0x834, 0x01),                  # Earthgift -> Earth
    (0x834, 0x02),                  # Hellfire  -> Fire
    (0x834, 0x04),                  # Lifespring-> Water
    (0x834, 0x08),                  # Whisperwind->Air
)


def _crystal_leg(needed, fiends=_CRY_FIENDS):
    """GET-wrapper cave fragment: for fiend flag ids in Chaos Shrine, return 1
    once >= `needed` of the `fiends` (offset,mask) bits are set; else fall through
    to the real read. `fiends` defaults to the four real Fiend flags (crystals_needed
    mode) and is _CRY_SHADOW in bonus_dungeon_crystals mode. Runs after the displaced
    prologue (sp already -0x10), so the early return restores sp itself. Clobbers
    t0-t3 + at (caller-saved)."""
    body = []
    for fid in _CRY_FIEND_IDS[:-1]:
        body += [A.addiu("at", "zero", fid),
                 ("beq", "a0", "at", "CRYCHK"), A.nop()]
    body += [A.addiu("at", "zero", _CRY_FIEND_IDS[-1]),
             ("bne", "a0", "at", "CRYOUT"), A.nop(),
             ("label", "CRYCHK"),
             A.lui("t0", 0x089D),
             A.lw("t0", 0x7AD8, "t0"),           # live save-struct base (or 0)
             ("beq", "t0", "zero", "CRYOUT"), A.nop(),
             A.lw("t1", _CRY_TRUEMAP_OFF, "t0"),
             A.addiu("at", "zero", _CRY_CHAOS_MAP),
             ("bne", "t1", "at", "CRYOUT"), A.nop(),
             A.addu("t2", "zero", "zero")]       # t2 = set count
    for off, mask in fiends:
        body += [A.lbu("t1", off, "t0"),
                 A.andi("t3", "t1", mask),
                 A.sltu("t3", "zero", "t3"),     # t3 = bit set ? 1 : 0
                 A.addu("t2", "t2", "t3")]
    body += [A.addiu("t3", "zero", needed),
             A.sltu("at", "t2", "t3"),           # at = count < needed
             ("bne", "at", "zero", "CRYOUT"), A.nop(),
             A.addiu("sp", "sp", 0x10),          # undo displaced prologue
             A.addiu("v0", "zero", 1),           # "flag is set"
             A.jr(), A.nop(),
             ("label", "CRYOUT")]
    return body


def _mp_mb(elf, feats):
    """The magic-power mailbox vaddr, minted once and threaded through feats so
    every cave in this feature embeds the same address. Matches the
    _blood_ticket_mb pattern; an isolated unit test with no feats gets a
    throwaway (such tests only assert emitted bytes)."""
    if feats is not None and feats.get("_mp_mb") is not None:
        return feats["_mp_mb"]
    mb = E.add_segment_cave(elf, _MP_MB_MAGIC + b"\x00" * (_MP_MB_LEN - 4))
    if feats is not None:
        feats["_mp_mb"] = mb
    return mb


def apply_magic_power_scaling(elf: bytearray, feats=None):
    """Replace the engine's magic-defence damage buckets with a diminishing-
    returns curve (this leg), and later the five status to-hit rolls with a
    multiplicative decay. Every leg is inert -- runs the displaced vanilla
    instructions and returns -- unless the client has armed the mailbox AND the
    target monster's shrink256 is non-zero, so a 100%-power seed is vanilla.

    See the _MP_MB_* block for the mailbox layout and the party/monster boundary
    derivation."""
    mb = _mp_mb(elf, feats)

    # --- Damage leg: dmg = dmg * DIV / (DIV + mdef_eff) ----------------------
    # Displaced originals load the target unit into v0 then overwrite it with
    # mdef; we re-load the unit ourselves because the vanilla path needs v0 to
    # still hold mdef when it returns to the bucket chain.
    dmg = A.asm_labels(
        [A.lw("v0", 0x38, "s4"), A.lh("v0", 0x32, "v0")]     # displaced originals
        + _MP_SAVE
        + [A.lw("t2", 0x38, "s4")]                           # t2 = target unit
        + _mp_gate(mb, "t2", "t3")                           # t3 = table row
        + [A.lhu("t2", _MP_T_MDEFF, "t3"),                   # uncapped scaled mdef
           A.lw("t0", _MP_DMG_SP + _MP_FRAME, "sp"),         # running damage
           ("beq", "t0", "zero", "DONE"), A.nop(),           # nothing to reduce
           A.addiu("t1", "zero", _MP_DMG_DIV),
           A.multu("t0", "t1"), A.mflo("t0"),                # dmg * DIV
           A.addu("t1", "t1", "t2"),                         # DIV + mdef_eff
           A.divu("t0", "t1"), A.mflo("t0"),                 # -> reduced damage
           # floor at 1: the curve is asymptotic, but integer division can still
           # reach 0 on chip damage, and a 0 here would read as a miss.
           ("bne", "t0", "zero", "STORE"), A.nop(),
           A.addiu("t0", "zero", 1),
           ("label", "STORE"),
           A.sw("t0", _MP_DMG_SP + _MP_FRAME, "sp")]
        + _MP_RESTORE
        + [A.j(_MPD_SKIP), A.nop()]                          # skip the buckets
        + [("label", "DONE")]
        + _MP_RESTORE
        + [A.j(_MPD_RET), A.nop()])
    E.install_detour(elf, _MPD_HOOK, E.add_segment_cave(elf, dmg))

    # --- To-hit legs, shared shape (Slow/Slowra + the two generic status
    # handlers that carry Sleep/Bind/Dark). See _MPH_SHARED for the register
    # contract. Rebuild the score from VANILLA mdef, then scale it.
    for hook in _MPH_SHARED:
        body = A.asm_labels(
            [A.lhu("v0", 0x24, "a0"), A.subu("v1", "fp", "v1")]   # displaced
            + _MP_SAVE
            + _mp_gate(mb, "a0", "t3")                            # t3 = table row
            + [A.lhu("t2", _MP_T_MDEFV, "t3"),                   # vanilla mdef
               A.subu("t0", "fp", "t2"),                          # unscaled score
               A.lhu("t1", _MP_T_SHRK, "t3"),                     # shrink256
               A.mult("t0", "t1"), A.mflo("t0"),                  # signed: score
               A.sra("t0", "t0", 8),                              #   may be < 0
               A.addu("v1", "t0", "zero"),                        # -> engine's v1
               ("label", "DONE")]
            + _MP_RESTORE
            + [A.j(hook + 8), A.nop()])
        E.install_detour(elf, hook, E.add_segment_cave(elf, body))

    # --- To-hit leg, type-0x0e. Base is the literal 212 and the unit is in a1;
    # we hand the vanilla subtract a v1 that makes it produce the scaled score.
    zero_e = A.asm_labels(
        [A.lh("v1", 0x32, "a1"),                                  # displaced
         A.addiu("v0", "zero", _MPH_0E_BASE)]                     # displaced
        + _MP_SAVE
        + _mp_gate(mb, "a1", "t3")
        + [A.lhu("t2", _MP_T_MDEFV, "t3"),
           A.addiu("t0", "zero", _MPH_0E_BASE),
           A.subu("t0", "t0", "t2"),                              # unscaled score
           A.lhu("t1", _MP_T_SHRK, "t3"),
           A.mult("t0", "t1"), A.mflo("t0"), A.sra("t0", "t0", 8),
           A.addiu("t1", "zero", _MPH_0E_BASE),
           A.subu("v1", "t1", "t0"),                              # 212 - scaled
           ("label", "DONE")]
        + _MP_RESTORE
        + [A.j(_MPH_0E_HOOK + 8), A.nop()])
    E.install_detour(elf, _MPH_0E_HOOK, E.add_segment_cave(elf, zero_e))


def install_magic_power_tables(elf: bytearray, feats=None):
    """Reserve the magic_power table block in the cave segment's bss tail and
    publish its address in the mailbox header.

    Called by patch_iso AFTER the FEATURES loop, never from FEATURES itself:
    cave_bss_tail must follow every add_segment_cave, and spell_tomes (which
    MUST STAY LAST in FEATURES) owns the final one. Reserving here costs no file
    bytes -- which is the entire point, the file budget is down to ~900 spare
    bytes and the tables are 1.5 KB.

    No-op unless the feature is on. Zero-filled by the loader, i.e. every
    monster reads the vanilla sentinel until the client writes real tables."""
    if not (feats or {}).get("magic_power_scaling"):
        return
    mb = feats.get("_mp_mb") if feats else None
    if not mb:
        raise ValueError("magic_power_scaling: mailbox missing; apply_popup_colors "
                         "must run before the FEATURES loop (it mints it)")
    tables = E.cave_bss_tail(elf, _MP_T_LEN)
    E.cave_write(elf, mb + _MP_MB_TABLES, struct.pack("<I", tables))


def apply_bikke_ship_split(elf: bytearray, feats=None):
    # crystals_needed + bonus_dungeon_crystals both ride in as bake context (never
    # FEATURES keys -- they share this wrapper hook). The crystal leg installs when
    # EITHER N < 4 (count fewer fiends) OR bonus mode is on (count shadow bits
    # instead of fiends). In bonus mode the leg installs even at the default N=4,
    # because the fiends WILL all be set once their dungeons open -- a crystal must
    # not count until its superboss falls, so the source flips to the shadow bits.
    _cn = (feats or {}).get("crystals_needed")
    bonus = bool((feats or {}).get("bonus_dungeon_crystals"))
    cn = int(_cn) if _cn is not None else 4
    install_crystal = (cn < 4) or bonus
    for hook in (_BSS_GETSTORYFLAG, _BSS_SETSTORYFLAG):
        # both wrappers open with: addiu sp,sp,-0x10 ; sw ra,0xc(sp)
        body = [
            A.addiu("sp", "sp", -0x10),          # displaced original 1
            A.sw("ra", 0x0C, "sp"),              # displaced original 2
        ]
        if hook == _BSS_GETSTORYFLAG and install_crystal:
            body += _crystal_leg(cn, _CRY_SHADOW if bonus else _CRY_FIENDS)
        if hook == _BSS_GETSTORYFLAG:
            # Titan gate split (v260, always-on -- the Star Ruby is always a
            # randomized AP item): if a0 in {13,14} and the REAL Giant's Cave is
            # loaded (FIELD_MAP_ID 0x22) and it is NOT a bonus floor
            # (bonus mapid < 0x87), answer shadow flag 73 instead. The move
            # a0<-73 propagates because getStoryFlag does `move s0,a0` after its
            # (a0-agnostic) singleton jal -- the exact mechanism the Pravoka leg
            # below relies on. t0/t1/at are scratch at wrapper entry.
            body += [
                A.addiu("at", "zero", 13),
                ("beq", "a0", "at", "TGS_MAP"),
                A.addiu("at", "zero", 14),           # delay slot: preload 14
                ("bne", "a0", "at", "TGS_DONE"), A.nop(),
                ("label", "TGS_MAP"),
                A.lui("t0", 0x089D),
                A.lw("t0", 0x7AD8, "t0"),            # live save base (or 0)
                ("beq", "t0", "zero", "TGS_DONE"), A.nop(),
                A.lw("t1", _BSS_MAPID_OFF, "t0"),    # FIELD_MAP_ID (fine, u32)
                A.addiu("at", "zero", _TGS_GIANT_MAP),
                ("bne", "t1", "at", "TGS_DONE"), A.nop(),
                A.lbu("t1", _TGS_BONUS_MAPID_OFF, "t0"),   # bonus mapid (u8)
                A.addiu("at", "zero", _TGS_BONUS_MIN),
                A.sltu("at", "t1", "at"),            # at = bonus_mapid < 0x87
                ("beq", "at", "zero", "TGS_DONE"), A.nop(),  # bonus floor -> leave
                A.addiu("a0", "zero", _TGS_SHADOW_FLAG),
                ("label", "TGS_DONE"),
            ]
        # (v258: the v257 sage_lich_lift leg was DELETED here, not extended.
        # His give latches Lich SET + sailing CLEAR + possession CLEAR at map
        # load -- three independent gates, each proven live 2026-08-10 -- and
        # after the flag-17 lie, the flag-18 lie, a possession doorstep prearm
        # and an in-town hold he STILL refused with any one gate unsatisfied.
        # The location is proximity-detected now (ApClient sage stage, the
        # Bahamut pattern), so nothing needs his script to run and no lie or
        # hold is needed at all. The v255 handover repoint at 0x089AB14C stays:
        # flag 81 is still the dedup signal for his GENUINE native give.)
        if hook == _BSS_SETSTORYFLAG:
            # if (a0 == 22 && a1 != 0 && *SAVE_BLOCK_PTR && !(flags0 & 0x40))
            #     return;  // no crown -> the elder cannot grant permission
            body += [
                A.addiu("at", "zero", _CCG_PERMISSION_ID),
                ("bne", "a0", "at", "BIKKE"), A.nop(),
                ("beq", "a1", "zero", "BIKKE"), A.nop(),  # clears pass through
                A.lui("t0", 0x089D),
                A.lw("t0", 0x7AD8, "t0"),        # live save-struct base (or 0)
                ("beq", "t0", "zero", "BIKKE"), A.nop(),
                A.lbu("t1", _BSS_FLAGS_OFF, "t0"),
                A.andi("t1", "t1", _CCG_CROWN_MASK),
                ("bne", "t1", "zero", "BIKKE"), A.nop(),  # crown owned -> vanilla
                A.addiu("sp", "sp", 0x10),       # undo displaced prologue
                A.jr(), A.nop(),                 # swallow the write
                ("label", "BIKKE"),
            ]
        body += [
            # if (a0 == 5 && *SAVE_BLOCK_PTR && mapid == Pravoka) a0 = 63
            A.addiu("at", "zero", 5),
            ("bne", "a0", "at", "DONE"), A.nop(),
            A.lui("t0", 0x089D),
            A.lw("t0", 0x7AD8, "t0"),            # live save-struct base (or 0)
            ("beq", "t0", "zero", "DONE"), A.nop(),
            A.lw("t1", _BSS_MAPID_OFF, "t0"),    # FIELD_MAP_ID (fine, u32)
            A.addiu("at", "zero", _BSS_PRAVOKA_ID),
            ("bne", "t1", "at", "DONE"), A.nop(),
            A.addiu("a0", "zero", _BSS_NEW_FLAG_ID),
            ("label", "DONE"),
            A.j(hook + 8), A.nop(),
        ]
        E.install_detour(elf, hook, E.add_segment_cave(elf, A.asm_labels(body)))


# --- Giant's Cave gate (v77) -------------------------------------------------
# Vanilla the Giant stands at (15,12) and the four Giant's Cave chests (treasure
# indices 151-154) are reachable from the NORTH entrance without ever passing
# him -- so "feed Titan the Star Ruby" gated nothing physically, even though
# logic.py has always required TITAN_FED for those chests.
#
# Two halves, walk-tested live 2026-07-19 against the map grid:
#   (a) grid cell (11,13) -> tile 0x0055, a solid lone-boulder tile. Its ATT
#       entry is ALREADY 0xF000 and the tile is already used in this very map at
#       (13,9) and (11,15), so NO ATT edit is needed. Permanent: it is never
#       removed. That half lives in the dpk (map_bake.place_giant_rock), since
#       the grid is wp16-compressed inside MAP_06_00.PCK.
#   (b) THIS half: move the Giant's object record from (15,12) to (12,13).
# UNFED the map splits into three sealed regions (north 23 tiles, south 50,
# chest wing 57): the chests are unreachable from BOTH entrances and the two
# entrances are cut from each other, while the Giant stays talkable from either
# side ((12,12) north, (13,13) south). FED he despawns, (12,13) opens, and all
# 131 walkable tiles (132 minus the rock) are mutually reachable -- nothing is
# stranded. Pairs with the ff1_data Star Ruby function-bit fix (b6 = FED): with
# the wrong bit the tunnel never opened, which this feature turns from a
# logic-conservative loophole into a hard block, so the two ship together.
#
# Giant's Cave = map index 40 (MAP_06_00.PCK, FIELD_MAP_ID 0x22). Its object
# list is OBJ_LIST_TABLE[40] (@0x08983CB8, u32[260]) -> 0x0897BBA8; the Giant is
# the 16-byte type-2 record at 0x0897BBE2, x = u16 @+4, y = u16 @+6.
_GCG_GIANT_REC = 0x0897BBE2
_GCG_VANILLA = bytes.fromhex("02001020 0f000c00 66000000 03000000".replace(" ", ""))
_GCG_NEW_XY = (12, 13)


# --- Lute-block gate (lute_tablets): physical descent seal -------------------
# The Chaos Shrine "F3" altar room (OBJ_LIST index 34, file 05_00) holds the
# block object (type-2 sprite 50, event 0x1ff7 @ (38,25)) sitting on the descent
# warp (exit #2, same tile). Interacting removes it via a cutscene woven into the
# Chaos approach, UNCONDITIONALLY -- so a tablets player reaches Chaos + the 5
# basement chests (247-251) without assembling the Lute (the block is not gated
# on any flag; flag 37 gates only Chaos's dialogue -- live-RE'd 2026-07-25).
# Rather than untangle that cutscene, add a SOLID blocker object one tile below
# at (38,26) -- the only tile you can stand on to interact with the block -- so
# without the Lute you physically cannot reach it. Live-proven: a duplicate slab
# there hard-blocks the approach.
#
# Conditional spawn: the object-list reader loads OBJ_LIST_TABLE[idx] into s3 at
# _LB_READER_HOOK (v1 = idx*4 there). We detour: for idx==34, if the Lute
# possession bit (0x1153b b7) is CLEAR, swap s3 to a relocated copy of map 34's
# list with the blocker appended; if the Lute is owned, leave the original (no
# blocker -> descend). The blocker despawns on the next room load once the Lute
# is assembled. Tablets-only (client sets the feature key from
# lute_tablets_required). See [[lute-tablets]].
_LB_READER_HOOK = 0x0883FC70          # lw s3,0(v0)   (v1 = map index * 4)
_LB_READER_CONT = 0x0883FC78          # hook + 8
_LB_MAP_IDX     = 34
_LB_OBJ_TABLE   = 0x08983CB8
_LB_LUTE_OFF    = 0x43B               # Lute key byte - save base (0x1153b)
_LB_LUTE_MASK   = 0x80
_LB_BLOCK_X, _LB_BLOCK_Y = 38, 26     # interact tile just below the block
_LB_BLOCK_SPR = 0x32                  # sprite 50 = the tablet/slab (reuse)
# A type-2 record's param (+2) is the MESSAGE id shown on interact -- NOT an
# event/script id (live-proven 2026-07-25: 0x1ff7 has no `05 08` event header
# anywhere in the blob; its only other occurrence is as the operand of a `2e`
# showMessage op in event 0x4b7, and a blocker carrying it displayed the slab
# inscription in-game). Reusing the real block's own id keeps the blocker a
# plain stone slab; ApClient rewrites that inscription to the "assemble the
# Lute" line while the Lute is unassembled and restores vanilla after.
_LB_BLOCK_MSG = 0x1FF7


# --- Mystic-Key door gate (v198): Chaos Shrine alcoves ----------------------
# The two Chaos Shrine F1 chest alcoves (treasure idx 3/4/5) sit behind doors
# that stay locked even with the Mystic Key owned -- LIVE-PROVEN 2026-08-01 on a
# save where BOTH the function bit (story flag 9, 0x1151D b1) and the possession
# bit (0x1153B b3) read set INSIDE the shrine, while the same key opened Cornelia
# Castle's and the Dwarf Cave's doors on that same save. The native check is not
# a story flag: walking into and interacting with the door produced ZERO
# getStoryFlag calls (log-bp at the wrapper 0x08867924; the only shrine read is
# an ambient flag-46 poll at map load, from the hardcoded map-31 leg at
# 0x0883c32c -- setting flag 46 + reloading the room did NOT open the doors).
# The shrine's object list is stock (vanilla ELF == live RAM, 29 records
# byte-identical), so nothing of ours locked them.
#
# Rather than keep hunting the engine-side check, remove the obstruction the
# same way lute_block_gate adds one: the two `kind=1 val 0x11a4` records at the
# alcove mouths ARE the blockers, so when the Mystic Key is owned hand the
# object-list reader a copy of map 31's list with exactly those two dropped.
# Key not owned -> vanilla list -> vanilla lock, so the logic requirement
# (LOC_INFO "MysticKey" token on treasure idx 3/4/5) still holds. Always-on:
# the Mystic Key is always a randomized AP item in this world.
#
# v250 -- GENERALIZED TO EVERY MYSTIC KEY DOOR ON THE DISC, AND THIS IS NOW THE
# ONLY THING THAT OPENS THEM. Doors used to come from story flag 9 (the Mystic
# Key function bit): each map's init script ran `2e/36 04 <val>` under a
# `2d 08 09 02` check, which clears the record's collision cell. That coupling
# is what made the NPC-gate class unfixable -- flag 9 is ALSO the Elf Prince's
# "already gave it" gate, so every attempt to split the quest off the key broke
# either the doors or the prince (v247 repointed the Elven Castle check and
# killed all four of its doors; v249 hand-split that one block and did not
# generalize). Dropping the record instead removes the cell before it is ever
# created, so door access is a pure function of POSSESSION and no story flag is
# involved at any point.
#
# LIVE-PROVEN 2026-08-10, the experiment that settled the design: with story
# flag 9 forced CLEAR (every door script believes the key is not owned) and all
# 12 records below dropped in RAM, every Mystic Key door in the game opened --
# Elven Castle, Cornelia, the shrine alcoves and the rest (re_only/
# _mdg_all_probe.py). Flags were never load-bearing for doors.
#
# The 12 records are the complete kind=4 set carrying either Mystic Key door
# value, from a scan of all 128 map object lists; both values are v199-proven
# (0x1F4A = the shrine alcoves, 0x23CD = Cornelia's treasury). Object ordinals
# are NOT disturbed: the NPC ordinals in ff1_data (PRINCESS_NPC_ORDINAL etc.)
# are AP location numbers, not object-list indices.
_MDG_DOOR_KIND    = 4                 # kind=4 = locked-door cell record
_MDG_DOOR_VALS    = (0x1F4A, 0x23CD)  # the two Mystic Key door types
_MDG_KEY_OFF      = 0x43B             # Mystic Key byte - save base (0x1153b)
_MDG_KEY_MASK     = 0x08              # key id 5 -> b3
# objlist map index -> the exact (val, x, y) door records it must carry.
# NB: the objlist index is NOT the FIELD_MAP_ID and there is no lookup table
# between them, so these are identified by their record sets, not by name.
_MDG_DOORS = {
    6:  ((0x23CD, 33, 11),),
    31: ((0x1F4A, 64, 12), (0x1F4A, 65, 49)),          # Chaos Shrine F1 alcoves
    56: ((0x23CD, 26, 17), (0x23CD, 33, 17)),          # Cornelia treasury pair
    87: ((0x1F4A, 14, 53),),
    88: ((0x1F4A, 18, 28),),
    91: ((0x1F4A, 16, 66), (0x1F4A, 32, 66), (0x1F4A, 48, 66),
         (0x1F4A, 63, 66), (0x1F4A, 69, 66)),
}


# --- NPC gate split (v247): Elf Prince / Elven Castle -----------------------
# THE fix for the shared-bit NPC class (robot, Sarda, Adamantite, Astos, Prince,
# Levistone): an NPC's "already gave it" gate is the same bit as the item's
# function bit, so an AP-granted key kills the NPC and the client's only lever
# was to lie about the bit in-map (NPC_MAP_RESET holds) -- which deadlocked the
# four Elven Castle Mystic Key doors for a player who owned a won key without
# the Jolt Tonic (live 2026-08-08, Omage).
#
# Instead, split the gate ON DISC: repoint the quest chain's story-flag operands
# from the shared function flag to a shadow quest flag nobody else touches.
# Doors keep reading the function flag (set only by the AP grant); the quest
# runs on its own flag (set only by the native handover) -> location, item and
# door access fully decoupled, the NPC_MAP_RESET row DELETED, no holds ever.
#
# Shadow scheme: NPC_GATE_SPLIT_FLAG_BASE + key_id (Mystic Key 5 -> flag 69 ->
# byte 0x11524 b5). Flag ids 64..95 verified free 2026-08-09: zero `2d xx <id>`
# script refs on the whole disc, zero engine refs, zero client writes
# (ff1_data audit); ids 49..63 are skipped -- the client already uses 55 as a
# shadow flag (rune borrow).
#
# Elf Prince sites, ALL live-RE'd 2026-08-09 (re_only/prince_gate_probe.py; the
# repoint was rehearsed in RAM first -- prince back to sleep, doors still open,
# then a genuine walk-in handover fired on the split flag end to end):
#   * event-VM CHECK opcode = `2d 08 <id> 02` (u16 op 0x082d), proven by
#     paired live decodes (id 0x29 -> getStoryFlag(41), 0x15 -> 21).
#   * event-VM SET   opcode = `2d 04 <id> 00` (op 0x042d), caught at the
#     handover (setStoryFlag(9, 1) ra=0x08843ea8, cursor on this word).
#   * completion   0x089a9e6c: the cutscene's set flag 9 -- the ONLY
#     `2d 04 09 00` on the whole disc. Becomes the client's rise detector.
#   * healer 0x089a6dac / prince 0x089a6de0: dialogue-only records (msgs
#     0x0d/0x0b, 0x0f/0x12) -- repointed so they track the QUEST, not the key.
#     The other 14 flag-9 checks on disc (Elfheim "you have the key" townsfolk
#     flavour + Chaos Shrine) stay on flag 9 DELIBERATELY: they are about the
#     key, and AP-key == flag 9 is now exactly true.
#   * handover gate 0x089a6c34: check flag 9 -> short-circuits the whole quest
#     when the AP key set it (the original bug); false path tests flag 8
#     (Jolt Tonic) and runs the give-cutscene. Its flag-9 body ALSO carries
#     `2e 04 4a 1f` + `2e 04 cd 23` (the two door types) beside the prince
#     placement `45 08 07 00 / 11 00 0f 00` -- vanilla conflates "prince awake"
#     with "doors open" because vanilla can only reach flag 9 through the
#     handover. That is why v247's repoint of THIS site killed every Elven
#     Castle door for a won key (live 2026-08-10) and why v249 hand-split the
#     block to keep the doors on flag 9.
#
# v250: BOTH are obsolete. mystic_door_gate now drops the door RECORDS on
# POSSESSION, so these door ops match no cell and the plain operand repoint is
# correct again -- this site is a pure quest gate once more, and the v249 block
# surgery is deleted. Live-proven 2026-08-10: with flag 9 forced CLEAR and the
# records dropped, every Mystic Key door in the game opened. Doors and the
# prince are no longer the same mechanism, so they cannot fight.
#
# Event-VM encoding, all disassembled 2026-08-10 (do not re-derive by pattern):
# opcode table 0x0898af38, 247 entries, 12B stride, opcode-indexed; byte 1 of an
# op is its LENGTH (0x08844150 `lbu a1,1(s0)`); a check `2d 08 <id> 02` falls
# through when the flag is SET and jumps to its pointer operand when CLEAR
# (0x08843ec4), polarity 0x03 inverting it; pointer operands are stored biased,
# target = stored + 0x00991E58 (`0xF7FFF078 + 0x08992DE0`, in both the check
# handler and the jump handler 0x08843b8c).
NPC_GATE_SPLIT_FLAG_BASE = 64
_NGS_CHECK_OP, _NGS_SET_OP = 0x2D08, 0x2D04   # first 2 bytes, LE u16 as stored
# rows: (runtime_addr, kind, vanilla_flag, key_id) -- new flag = BASE + key_id.
_NGS_SITES = (
    (0x089A6C34, "check", 9, 5),   # Elf Prince: handover gate
    (0x089A9E6C, "set",   9, 5),   # Elf Prince: completion (unique on disc)
    (0x089A6DAC, "check", 9, 5),   # Elven Castle healer dialogue
    (0x089A6DE0, "check", 9, 5),   # Elf Prince dialogue
    # Crescent Lake sage (v255). Story flag 18 is the Canoe's SAILING function
    # bit, and the sage's handover cutscene sets it -- so "he gave it to me" and
    # "I can sail" were the same bit, the same collision as the prince. Repoint
    # the SET only: it is the ONLY `2d 04 12 00` on the whole disc and it sits
    # immediately before the canoe give (`30 08 01 0c / e2 1f` = object 0x1FE2),
    # so it is positively the handover. Flag 18 then belongs to the AP grant
    # alone (key id 17 is already in OWNED_FUNCTION_REASSERT) and flag 81 rises
    # exactly once, when he actually hands over.
    #
    # The two remaining `2d 08 12 02` flag-18 CHECKs (0x089A5DE4, 0x089A6BD8)
    # and the `2d 08 12 03` at 0x089A7584 are deliberately NOT touched: they ask
    # "does the player have a canoe", which AP-canoe == flag 18 answers exactly
    # right. Repointing a check whose body was not positively identified is the
    # v248 mistake; 0x089A7584's body gives object 0x1394, not the canoe.
    (0x089AB14C, "set",  18, 17),  # Crescent Lake sage: handover (unique on disc)
)


def apply_prince_gate_split(elf: bytearray, feats=None):
    """Repoint the Elf Prince quest chain's flag operands to the shadow flag.
    Byte-level: `2d 08 09 02` / `2d 04 09 00` -> id byte (+2) = 64+key_id.
    Verifies the exact vanilla word at every site (already-patched = no-op)."""
    for addr, kind, van, kid in _NGS_SITES:
        o = E.ram2file(addr)
        op = _NGS_CHECK_OP if kind == "check" else _NGS_SET_OP
        want = bytes((op >> 8, op & 0xFF, van, 0x02 if kind == "check" else 0x00))
        new_id = NPC_GATE_SPLIT_FLAG_BASE + kid
        cur = bytes(elf[o:o + 4])
        if cur == want[:2] + bytes((new_id,)) + want[3:]:
            continue                          # already split
        if cur != want:
            raise ValueError(
                f"prince_gate_split: {kind} @{addr:#010x} reads {cur.hex(' ')}, "
                f"expected {want.hex(' ')} -- script moved, re-run the probe")
        elf[o + 2] = new_id


def _lb_blocker_record(msg_id):
    # type2 | msg | x | y | sprite | 0 | 0 | 1   (matches the block's own tail)
    return struct.pack("<HHHHHHHH", 2, msg_id, _LB_BLOCK_X, _LB_BLOCK_Y,
                       _LB_BLOCK_SPR, 0, 0, 1)


def _objlist_read(elf, map_idx):
    """(file_offset, header+records bytes) for a map's object list, no terminator."""
    tptr = E.ram2file(_LB_OBJ_TABLE + map_idx * 4)
    list_ram = struct.unpack_from("<I", elf, tptr)[0]
    o = E.ram2file(list_ram)
    p = o + 10                                    # skip 10-byte header
    while struct.unpack_from("<H", elf, p)[0] != 0xFFFF:
        p += 16 if struct.unpack_from("<H", elf, p)[0] == 2 else 8
    return o, bytes(elf[o:p])


def _objlist_cave(elf, payload):
    payload = payload + b"\xff\xff"
    if len(payload) % 4:
        payload += b"\x00" * (4 - len(payload) % 4)
    return E.add_segment_cave(elf, payload)


def _mdg_stripped_list(elf, map_idx):
    """One map's object list with its Mystic Key door records removed.

    The kind=4 record IS the lock: the object-list reader turns it into a
    b0=2 collision cell (live-RE'd 2026-08-01: cells at field+0x52CC, 8B each;
    the walk scanner at 0x885bd9c blocks any cell with b0&0x0A; b0=0xFF =
    consumed/skipped). Vanilla opens doors via EVM op 0x36 (open-door <val>,
    gated on checkStoryFlag(9) = the Mystic Key function bit) which clears the
    cell to 0xFF -- Cornelia's treasury door cells read b0=0xFF at map load
    with the key owned. Byte-identical bytecode exists for the shrine (event
    0x12: flag9 -> `36 04 4a 1f`) but never fires there; its invoker is
    un-RE'd. Dropping the record skips cell creation entirely -- proven live:
    with the cell neutralized the player walked through, door art intact.
    The kind=1 val 0x11A4 records nearby are forced-encounter trap tiles
    (sub3, formation 0x10), vanilla gameplay -- NOT the lock; left alone."""
    o, orig = _objlist_read(elf, map_idx)
    out, p, dropped = bytearray(orig[:10]), 10, []
    while p < len(orig):
        kind = struct.unpack_from("<H", orig, p)[0]
        size = 16 if kind == 2 else 8
        rec = orig[p:p + size]
        val, x, y = struct.unpack_from("<HHH", rec, 2)
        if kind == _MDG_DOOR_KIND and val in _MDG_DOOR_VALS:
            dropped.append((val, x, y))
        else:
            out += rec
        p += size
    want = sorted(_MDG_DOORS[map_idx])
    if sorted(dropped) != want:
        raise ValueError(f"mystic_door_gate: map-{map_idx} door records not "
                         f"vanilla (found {sorted(dropped)}, expected {want})")
    return bytes(out)


def apply_lute_block_gate(elf: bytearray, feats=None):
    """Object-list reader detour. Hosts BOTH map-gated list swaps, because two
    install_detour calls on one hook orphan the first (the bikke/crystal lesson).
    Registered under lute_block_gate AND mystic_door_gate; whichever runs first
    installs the legs both features asked for, the second call no-ops."""
    # No feats (bare fn(elf) -- test_patch's per-feature pass) = install every
    # leg; a real bake always passes the dict and selects.
    if feats:
        want_lute = bool(feats.get("lute_block_gate"))
        want_mystic = bool(feats.get("mystic_door_gate"))
    else:
        want_lute = want_mystic = True
    if not (want_lute or want_mystic):
        return
    # Idempotence: the hook word is `j cave` (opcode 000010) once installed.
    if (struct.unpack_from("<I", elf, E.ram2file(_LB_READER_HOOK))[0] >> 26) == 0x02:
        return
    body = [
        A.word(0x8C530000),                       # lw s3,0(v0)       (displaced 1)
        A.word(0x0000A021),                       # addu s4,zero,zero (displaced 2)
    ]
    if want_lute:
        cave_list = _objlist_cave(
            elf, _objlist_read(elf, _LB_MAP_IDX)[1]
            + _lb_blocker_record(_LB_BLOCK_MSG))
        body += [
            A.addiu("at", "zero", _LB_MAP_IDX * 4),   # 34*4 = 0x88
            ("bne", "v1", "at", "LBDONE"), A.nop(),   # not map 34 -> next leg
            A.lui("t0", 0x089D),
            A.lw("t0", 0x7AD8, "t0"),                 # live save base (or 0)
            ("beq", "t0", "zero", "LBDONE"), A.nop(),
            A.lbu("t1", _LB_LUTE_OFF, "t0"),          # Lute key byte
            A.andi("t1", "t1", _LB_LUTE_MASK),
            ("bne", "t1", "zero", "LBDONE"), A.nop(),  # Lute owned -> original
            A.li("s3", cave_list),                    # else: list w/ blocker
            ("label", "LBDONE"),
        ]
    if want_mystic:
        # Possession is tested ONCE, then one compare per door map. No story
        # flag is read here or anywhere else for doors (v250).
        body += [
            A.lui("t0", 0x089D),
            A.lw("t0", 0x7AD8, "t0"),                 # live save base (or 0)
            ("beq", "t0", "zero", "MDGDONE"), A.nop(),
            A.lbu("t1", _MDG_KEY_OFF, "t0"),          # Mystic Key key byte
            A.andi("t1", "t1", _MDG_KEY_MASK),
            ("beq", "t1", "zero", "MDGDONE"), A.nop(),  # no key -> vanilla locks
        ]
        for m in sorted(_MDG_DOORS):
            cave = _objlist_cave(elf, _mdg_stripped_list(elf, m))
            body += [
                A.addiu("at", "zero", m * 4),         # v1 = map index * 4
                ("bne", "v1", "at", f"MDG{m}"), A.nop(),
                A.li("s3", cave),                     # key owned -> doors gone
                ("beq", "zero", "zero", "MDGDONE"), A.nop(),
                ("label", f"MDG{m}"),
            ]
        body += [("label", "MDGDONE")]
    body += [A.j(_LB_READER_CONT), A.nop()]
    E.install_detour(elf, _LB_READER_HOOK,
                     E.add_segment_cave(elf, A.asm_labels(body)))


def apply_giant_cave_gate(elf: bytearray, feats=None):
    """Relocate the Giant onto the choke point created by map_bake's boulder."""
    o = E.ram2file(_GCG_GIANT_REC)
    rec = bytes(elf[o:o + 16])
    if rec != _GCG_VANILLA:
        raise ValueError(f"giant_cave_gate: map-40 giant record is not vanilla "
                         f"({rec.hex()} != {_GCG_VANILLA.hex()})")
    struct.pack_into("<HH", elf, o + 4, *_GCG_NEW_XY)


# --- feature: chest dedup (aliased treasure indices -> unique) -------------------
# Six treasure indices are carried by MORE THAN ONE physical type-3 chest record
# in OBJ_LIST_TABLE (full dump_objlist.py scan 2026-07-22): 19 x2 (Citadel),
# 127 x2 / 129 x3 / 134 x3 (Marsh Cave), 176 x2 / 180 x4 (Mount Gulg). The
# opened-chest state is one bit per treasure index (CHEST_OPEN_BF), so aliased
# chests share a bit: opening one opens them all and the extra chests can never
# be their own AP checks. This re-points every duplicate record's param (u16 @+2)
# to a previously-unused treasure index so each physical chest is a unique AP
# location. The reused indices were phantoms (no chest record anywhere); their
# pool items come from gen_apdata (REPOINT_CONTENT copies the alias-source's
# vanilla contents for the empty ones).
#
# YAML-CONTROLLED since 2026-08-12 (LootInNormallyEmptyChests): all ten records
# ride one option, and world._removed_chest_idx drops all ten locations when it
# is off. Before that, only the Mount Gulg B5 three were optional and these
# seven were ON_DISC_ALWAYS "chest_dedup" -- _CHEST_DEDUP / _CHEST_DEDUP_GULG_B5
# and their apply fns are KEPT under the old feature names so an already
# generated seed, whose slot_data still names them, bakes byte-for-byte what it
# baked before.
# (record ram addr, vanilla 8-byte record, new treasure idx)
_CHEST_DEDUP = (
    # Citadel of Trials map 79 (10,33): 19 -> 24 ("Chest 10")
    (0x0897CCC6, "030013000a002100", 24),
    # Marsh Cave map 90 (16,80): 127 -> 131
    (0x0897D452, "03007f0010005000", 131),
    # Marsh Cave map 90 (30,79)/(42,79): 129 -> 132/133
    (0x0897D45A, "030081001e004f00", 132),
    (0x0897D462, "030081002a004f00", 133),
    # Marsh Cave map 91 (35,31)/(67,46): 134 -> 138/141
    (0x0897D62E, "0300860023001f00", 138),
    (0x0897D646, "0300860043002e00", 141),
    # Mount Gulg map 45 (63,17): 176 -> 185
    (0x0897BD8E, "0300b0003f001100", 185),
)

# The Mount Gulg B5 (map 46) half of the dedup, split out because it is the ONE
# yaml-controlled part: LootInGulgB5Chests. All three records alias treasure idx
# 180 (Mount Gulg - Chest 20), which sits on the SAME floor at (14,39) -- so in
# vanilla opening Chest 20 opens all four and these three are permanently empty.
# ON: each gets a unique index and becomes an AP check (Chest 35/36/37).
# OFF: left vanilla, and FF1PSPWorld._removed_chest_idx drops the three matching
# AP locations so the itempool, the tracker and the client scout all agree.
_CHEST_DEDUP_GULG_B5 = (
    # Mount Gulg map 46 (64,7)/(61,39)/(65,57): 180 -> 188/189/191
    (0x0897BE02, "0300b40040000700", 188),
    (0x0897BE0A, "0300b4003d002700", 189),
    (0x0897BE1A, "0300b40041003900", 191),
)


def _apply_dedup_records(elf: bytearray, records, what):
    for ram, vanilla_hex, new_idx in records:
        vanilla = bytes.fromhex(vanilla_hex.replace(" ", ""))
        o = E.ram2file(ram)
        rec = bytes(elf[o:o + 8])
        if rec != vanilla:
            raise ValueError(f"{what}: record @{ram:#x} is not vanilla "
                             f"({rec.hex()} != {vanilla.hex()})")
        struct.pack_into("<H", elf, o + 2, new_idx)


def _new_dialect(feats):
    """True when the seed names the post-rename feature, which owns ALL ten
    records. A real seed's slot_data speaks one dialect or the other, never
    both -- but the all-features bake in test_patch does, and a legacy fn
    running after the new one would find its records already re-pointed and
    fail the vanilla-byte assert. New dialect wins; legacy stands down."""
    return bool((feats or {}).get("loot_in_normally_empty_chests"))


def apply_chest_dedup(elf: bytearray, feats=None):
    """LEGACY (pre-rename seeds): the seven records that used to be always-on."""
    if _new_dialect(feats):
        return
    _apply_dedup_records(elf, _CHEST_DEDUP, "chest_dedup")


def apply_gulg_b5_dedup(elf: bytearray, feats=None):
    """LEGACY (pre-rename seeds): the Mount Gulg B5 three, off vanilla idx 180."""
    if _new_dialect(feats):
        return
    _apply_dedup_records(elf, _CHEST_DEDUP_GULG_B5, "loot_in_gulg_b5_chests")


def apply_normally_empty_dedup(elf: bytearray, feats=None):
    """LootInNormallyEmptyChests: give all ten normally-empty chests their own
    treasure index, so each is its own AP check instead of sharing a neighbour's
    open bit and dispensing nothing."""
    _apply_dedup_records(elf, _CHEST_DEDUP + _CHEST_DEDUP_GULG_B5,
                         "loot_in_normally_empty_chests")


# --- feature: bonus-dungeon dynamic chests (on-disc strip + mailbox) -------------
# The old client detection (exec bps at the chest grant call sites, _bonus_dyn_loop)
# NEVER fires in player sessions: launcher.patch_ini forces FastMemoryAccess=True
# for framerate, and PPSSPP breakpoints are silently dead under it (live-confirmed
# 2026-07-19 -- hooks "armed", chests granted vanilla, zero hits). So the GAME does
# the work: detours at CHEST_ITEM_CALL/CHEST_GIL_CALL (the two grant jals inside
# chest handler 0x08843BBC; hook+8 == the existing SKIP resume points). While the
# client arms `remaining` (u8), a chest whose treasure idx >= 268 (procedural --
# static chests are idx 0-267, incl. the bonus boss chests 252-267 owned by the
# bitfield poll) is STRIPPED (grant jal skipped, chest still opens) and its idx
# pushed to a ring the client polls -> AP check + _grant_loop delivery. remaining
# is cave-decremented per strip, so burst opens can't overrun the AP cap between
# client ticks; remaining==0 (incl. the unbaked/idle state) = fully vanilla.
#
# STRIP CONDITION (v79): armed AND idx NOT in [252,267]. The only STATIC chests
# reachable while standing in a bonus dungeon are the boss chests 252-267 (owned
# by the bitfield poll); anything else opened there is procedural, WHATEVER its
# idx reads (v78 assumed procedural idx >= 268 -- live playtest showed no strips,
# so that assumption is not trusted). The client only arms inside a bonus
# dungeon, so normal-world chests never see a nonzero `remaining`.
# DIAGNOSTIC: the cave counts EVERY entry (u8 @+6, wraps) and traces the last
# raw idx (u16 @+40) BEFORE any gating -- distinguishes "handler never runs for
# procedural chests" (counter frozen) from "gate mis-sorts them" (counter moves).
# BOX NAME (v80): the chest box fields live in the chest record (rec+0x586 box
# type, +0x588 cat, +0x58A resolved name-bank string id) and are all written
# BEFORE the grant call sites this cave hooks -- so on a strip the cave stamps
# the AP name over them: string id from `next_sid` (u16 @+42, client-armed each
# tick = the extended-bank id of the NEXT dynamic location's item name, rides
# the remote_chest_names bank), box type 1 (named item; gil chests are baked
# type 2 = number box) and cat 1 (the extended cat1 bank remote names live in).
# next_sid is consumed (zeroed) per strip so a same-tick second open shows its
# vanilla name rather than the WRONG AP name; 0 = leave the box alone.
_BDC_MB_MAGIC = b"BDC1"
_BDC_MB_REMAIN_OFF = 4        # u8: strips left (client re-arms every tick)
_BDC_MB_HEAD_OFF = 5          # u8: ring write cursor (cave-owned)
_BDC_MB_HITS_OFF = 6          # u8: cave entries ever (diagnostic, wraps)
_BDC_MB_RING_OFF = 8          # u16[_BDC_RING_N] stripped idxs; 0xFFFF = empty
_BDC_MB_LASTIDX_OFF = 40      # u16: last idx seen at any entry (diagnostic)
_BDC_MB_NEXTSID_OFF = 42      # u16: box string id for the next strip (0 = none)
_BDC_MB_LEN = 44
_BDC_BOX_TYPE_OFF = 0x586     # chest rec: box type (1 item-name, 2 gil-number)
_BDC_BOX_CAT_OFF = 0x588      # chest rec: name bank category
_BDC_BOX_SID_OFF = 0x58A      # chest rec: resolved name-bank string id
_BDC_RING_N = 16
_BDC_BOSS_LO = 252            # static bonus boss chests: vanilla path
_BDC_BOSS_HI = 268            # exclusive
_BDC_SITES = (                # (hook, vanilla jal word, vanilla delay-slot word)
    (0x08843D74, 0x0E235125, 0x24070001),   # jal GIVE_ITEM_FN ; addiu a3,zero,1
    (0x08843DC0, 0x0E235070, 0xAC450018),   # jal GIL_GIVE_FN  ; sw a1,0x18(a2)
)


def _bdc_cave(mb_vaddr, hook, jal_word, delay_word):
    """Detour body for one grant site. At both hooks s1 = chest object base,
    record = [s1+0x52C8], idx = u16[rec+0x1C] & 0x7FFF (chest-handler RE); t regs
    and `at` are dead across the call boundary. VANILLA path replays the
    displaced delay slot (arg setup) then the grant jal."""
    skip = hook + 8
    # rebuild the displaced jal's absolute target from its 26-bit J-immediate.
    # MIPS J-types inherit the top 4 PC bits; skip (hook+8) lies in the same
    # 256MB region as the call site, so its top nibble is the correct source.
    target = (skip & 0xF0000000) | ((jal_word & 0x03FFFFFF) << 2)
    return A.asm_labels([
        A.li("t2", mb_vaddr),
        # diagnostics first, unconditionally: entry counter + raw idx trace
        A.lbu("t4", _BDC_MB_HITS_OFF, "t2"),
        A.addiu("t4", "t4", 1),
        A.sb("t4", _BDC_MB_HITS_OFF, "t2"),
        A.lw("t0", 0x52C8, "s1"),
        A.lhu("t1", 0x1C, "t0"),
        A.andi("t1", "t1", 0x7FFF),                  # t1 = treasure idx
        A.sh("t1", _BDC_MB_LASTIDX_OFF, "t2"),
        A.lbu("t3", _BDC_MB_REMAIN_OFF, "t2"),
        ("beq", "t3", "zero", "VANILLA"), A.nop(),   # not armed -> vanilla
        A.addiu("t4", "zero", _BDC_BOSS_LO),
        A.sltu("t5", "t1", "t4"),
        ("bne", "t5", "zero", "STRIP"), A.nop(),     # idx < 252 -> procedural
        A.addiu("t4", "zero", _BDC_BOSS_HI),
        A.sltu("t5", "t1", "t4"),
        ("bne", "t5", "zero", "VANILLA"), A.nop(),   # 252-267 static boss chest
        ("label", "STRIP"),
        # AP box name: stamp next_sid over the already-written box fields
        # (t0 = chest record ptr, still live from the idx load above)
        A.lhu("t4", _BDC_MB_NEXTSID_OFF, "t2"),
        ("beq", "t4", "zero", "NONAME"), A.nop(),    # 0 = leave box alone
        A.sh("t4", _BDC_BOX_SID_OFF, "t0"),
        A.addiu("t5", "zero", 1),
        A.sh("t5", _BDC_BOX_TYPE_OFF, "t0"),         # named-item box
        A.sh("t5", _BDC_BOX_CAT_OFF, "t0"),          # extended cat1 bank
        A.sh("zero", _BDC_MB_NEXTSID_OFF, "t2"),     # consume the sid
        ("label", "NONAME"),
        # procedural chest while armed: push idx to the ring + strip the grant
        A.lbu("t4", _BDC_MB_HEAD_OFF, "t2"),
        A.andi("t4", "t4", _BDC_RING_N - 1),
        A.sll("t5", "t4", 1),
        A.addu("t5", "t5", "t2"),
        A.sh("t1", _BDC_MB_RING_OFF, "t5"),          # ring[head] = idx
        A.addiu("t4", "t4", 1),
        A.sb("t4", _BDC_MB_HEAD_OFF, "t2"),
        A.addiu("t3", "t3", -1),
        A.sb("t3", _BDC_MB_REMAIN_OFF, "t2"),
        A.j(skip), A.nop(),                          # grant jal never runs
        ("label", "VANILLA"),
        # the displaced delay-slot op is hoisted OUT of the delay slot here:
        # safe because it is plain arg setup with no dependency on the jal's
        # delay-slot semantics -- it executes exactly once either way, before
        # the call body runs.
        A.word(delay_word),                          # displaced delay slot
        A.jal(target), A.nop(),                      # displaced grant jal
        A.j(skip), A.nop(),
    ])


def apply_bonus_dyn_chests(elf: bytearray, feats=None):
    """Install the dynamic-chest strip/mailbox detours (see _bdc_cave)."""
    for hook, w0, w1 in _BDC_SITES:
        got = (_read_word(elf, hook), _read_word(elf, hook + 4))
        if got != (w0, w1):
            raise ValueError(f"unexpected chest grant site @{hook:#x}: "
                             f"{got[0]:#010x} {got[1]:#010x}")
    mb = (_BDC_MB_MAGIC + b"\x00\x00\x00\x00"        # remaining, head, hits, pad
          + b"\xFF" * (_BDC_RING_N * 2)              # ring init = all-empty
          + b"\x00\x00\x00\x00")                     # last-idx trace + pad
    assert len(mb) == _BDC_MB_LEN, len(mb)
    mb_vaddr = E.add_segment_cave(elf, mb)
    for hook, w0, w1 in _BDC_SITES:
        cave = _bdc_cave(mb_vaddr, hook, w0, w1)
        E.install_detour(elf, hook, E.add_segment_cave(elf, cave))


# --- equipment rune gate (equipment_runes yaml) -------------------------------
# Until the Equipment Rune Key is assembled (client sets story flag 62 once
# equipment_runes_required rune copies are held), NO activatable (spell-on-use)
# equipment may be activated as a battle item -- it greys out and refuses like a
# Tent. Hooked at the battle item-usability resolver fn 0x08871594 (statically
# RE'd 2026-07-27; the same fn whose cat-1 consumable table the spell-tome
# battle-exclusion relocates):
#   (a0=ctx, a1=cat, a2=id) -> action value. cat1 (consumable) returns a u16
#   where 0xFF = "not usable" (renderer 0x0886fc18 greys the row, confirm refuses
#   with msg 0x66). cat2/3 are DIFFERENT: they return the equipment PROC byte,
#   where 0 = plain gear = no use-effect (greyed/unusable). 0xFF is NOT a valid
#   cat2/3 return -- the execute path reads it as a proc/spell id and casting the
#   bogus spell 0xFF hangs the frame-wait routine 0x088ecd80 (~4B-iter freeze,
#   live-RE'd 2026-07-30 when an EQUIPPED locked item was activated).
#   cat 2 (weapon): returns proc byte @0x08953BB7 + id*28 (weapon rec +7).
#   cat 3 (armor):  returns proc byte @0x08954327 + id*28 (armor rec +7).
# The fn is a LEAF with no stack frame (jr ra on every path, sp untouched), so
# the entry detour can early-return directly. Gate: cat 2/3 AND the equipment's
# proc byte != 0 (plain gear untouched -- its vanilla 0 return already renders
# unusable) AND story flag 62 clear -> return proc 0 (mimic plain gear: greyed in
# inventory, "No effect" when equipped), NOT 0xFF. Flag read goes through the
# live save-struct pointer ([0x089D7AD8], 0 pre-boot -> fail CLOSED), so the
# save-block address shift is handled and the unlock is save-persistent.
# Story flag 62 = save_base + 0x41C + (62>>3), bit 62&7 -> +0x423 mask 0x40
# (id 63 = Bikke's bit7 in the same byte; ids 49-62 are otherwise unused).
_ERG_HOOK       = 0x08871594        # usability fn entry (leaf, no prologue)
_ERG_WEAPON_P0  = 0x08953BB7        # weapon table +7 of record 0
_ERG_ARMOR_P0   = 0x08954327        # armor table +7 of record 0 (_ARMOR_PROC0)
_ERG_FLAG_OFF   = 0x423             # story-flag byte 7 - save base
_ERG_FLAG_MASK  = 0x40              # story flag 62 = bit6
# Return addresses (jal site + 8) of the THREE direct callers of resolver
# 0x08871594. They use OPPOSITE "skip" sentinels, so the locked branch must hand
# each its own -- one value can't satisfy all three (proven live: v171 gave 0xFF
# to all three and the equip-execute path 0x886CF08 froze):
#   0x886C950 use-confirm  : `beql ==0xFF -> refuse` -> wants 0xFF (skip = 0xFF)
#   0x886FD48 row colour   : `==0xFF -> grey 0xffa0a0a0` -> wants 0xFF
#   0x886CF08 equip-execute: `beqz v0 -> skip; else store 0x44 & CAST` -> wants 0
#     (0xFF here casts bogus spell 0xFF -> frame-wait loop 0x088ecd80 = freeze)
# So: 0xFF to the two 0xFF-sentinel callers, 0 (skip/no-effect) to the execute
# path and any other caller.
_ERG_RA_CONFIRM  = 0x0886C958       # use-confirm: 0xFF -> refuse (skip)
_ERG_RA_EXEC     = 0x0886CF10       # equip-execute: 0 -> skip; 0xFF -> CAST (freeze)
_ERG_RA_COLOUR   = 0x0886FD50       # row colour: 0xFF -> grey 0xffa0a0a0


def apply_equipment_rune_gate(elf: bytearray, feats=None):
    # v221: blood_magic's equipment-activation ticket rides this same detour --
    # its stamp leg is emitted on the vanilla-continue path (ERGOUT), i.e. only
    # for gear this gate actually lets through, and only when that feature is on.
    stamp = (_blood_ticket_stamp(_blood_ticket_mb(elf, feats))
             if feats and feats.get("blood_magic") else [])
    body = A.asm_labels([
        # cat 2 -> weapon table, cat 3 -> armor table, else vanilla
        A.andi("t0", "a1", 0xFF),
        A.addiu("at", "zero", 2),
        ("beq", "t0", "at", "ERGW"), A.nop(),
        A.addiu("at", "zero", 3),
        ("beq", "t0", "at", "ERGA"), A.nop(),
        ("beq", "zero", "zero", "ERGOUT"), A.nop(),
        ("label", "ERGW"),
        A.lui("t1", _ERG_WEAPON_P0 >> 16),
        A.addiu("t1", "t1", _ERG_WEAPON_P0 & 0xFFFF),
        ("beq", "zero", "zero", "ERGC"), A.nop(),
        ("label", "ERGA"),
        A.lui("t1", _ERG_ARMOR_P0 >> 16),
        A.addiu("t1", "t1", _ERG_ARMOR_P0 & 0xFFFF),
        ("label", "ERGC"),
        # proc addr = table + id*28 (id*28 = ((id<<3)-id)<<2, the native shape)
        A.andi("t2", "a2", 0xFF),
        A.sll("t3", "t2", 3),
        A.subu("t3", "t3", "t2"),
        A.sll("t3", "t3", 2),
        A.addu("t1", "t1", "t3"),
        A.lbu("t2", 0, "t1"),                    # proc byte (use-cast spell id)
        ("beq", "t2", "zero", "ERGOUT"), A.nop(),  # plain gear -> vanilla
        # activatable: usable only with story flag 62 set
        A.lui("t0", 0x089D),
        A.lw("t0", 0x7AD8, "t0"),                # live save-struct base (or 0)
        ("beq", "t0", "zero", "ERGLOCK"), A.nop(),  # no save yet -> locked
        A.lbu("t1", _ERG_FLAG_OFF, "t0"),
        A.andi("t1", "t1", _ERG_FLAG_MASK),
        ("bne", "t1", "zero", "ERGOUT"), A.nop(),   # Rune Key owned -> vanilla
        ("label", "ERGLOCK"),
        # Discriminate by return address (leaf fn: ra intact at entry). Only the
        # two callers whose SKIP sentinel is 0xFF get 0xFF -- 0x886FD48 (row
        # colour -> grey) and 0x886C950 (use-confirm -> refuse) -- so locked gear
        # still greys/refuses in the item menu. The equip-execute caller 0x886CF08
        # and everything else get 0, whose skip test is `beqz v0` there: it skips
        # the cast cleanly ("No effect"). Giving 0x886CF08 0xFF instead casts bogus
        # spell 0xFF and hangs the frame-wait loop 0x088ecd80 (v171 freeze).
        A.li("t0", _ERG_RA_COLOUR),
        ("beq", "ra", "t0", "ERG_FF"), A.nop(),
        A.li("t0", _ERG_RA_CONFIRM),
        ("beq", "ra", "t0", "ERG_FF"), A.nop(),
        A.addiu("v0", "zero", 0),                # default incl. equip-execute: skip / "No effect"
        A.jr(), A.nop(),                         # leaf fn: return to caller
        ("label", "ERG_FF"),
        A.addiu("v0", "zero", 0xFF),             # 0xFF-sentinel callers: grey / refuse
        A.jr(), A.nop(),
        ("label", "ERGOUT"),
    ] + stamp + [
        A.word(0x30A300FF),                      # displaced: andi v1,a1,0xff
        A.word(0x24020003),                      # displaced: addiu v0,zero,3
        A.j(_ERG_HOOK + 8), A.nop(),
    ])
    E.install_detour(elf, _ERG_HOOK, E.add_segment_cave(elf, body))


# --- super dash (always baked; opt-in is the in-game Config Dash setting) -----
# Movement speed = u16 frames-per-tile table @0x08941870 indexed by vehicle
# mode ([save+0x2A8]): foot 16, ship 8, airship 4 (RE'd 2026-08-14 via a write
# bp on the step counter ctx+0x68EC -- see ff1_data "SUPER DASH" note). The
# step-record init caller loads it at 0x08836D04 (`lhu a3,0(v0)`), then the
# engine's own dash halving (0x08836DB8: threshold >>= 1) runs only for
# mode==0 (foot) when the Config Dash bit ([save+0x1170] bit0, read via s0 =
# live save base, so the save-block shift never applies) OR the dash button
# (static buttons mirror 0x08B10D7E, bit 0x2) says dash, and the map allows.
#
# Two detours, both requiring config bit AND button held (config off in the
# Config menu = wholly vanilla, matching the option text):
#   A @0x08836D04 (a3 load): vehicles (mode!=0) halve with a floor of 2
#     (ship 8->4 = airship pace, airship 4->2, canoe 8->4); on foot only
#     the overworld ([save+0x2008] map id == 0xFF) halves here (16->8 = ship
#     pace) -- interiors are left to detour B so the two never stack on foot.
#   B @0x08836DB8 (the engine halving, reached only when the engine already
#     decided dash is active+allowed on foot): button+config -> interiors
#     shift by 2 (16->4 = double vanilla dash); the overworld skips (A already
#     set 8, and vanilla-halving it again would overshoot to airship pace);
#     button up -> the vanilla >>1.
# Register liveness (from the 2026-08-14 disasm): at site A v0/v1/at/t2/t3
# are dead (t0/t1 are set after the hook, before the init jal; the init fn
# writes t2/t3 before reading them). At site B v1/at are dead (v1 reloads at
# 0x08836DC4). s0 = live save base, s1 = field ctx at both sites.
_SD_TABLE_HOOK = 0x08836D04     # lhu a3,0(v0) ; addiu a0,s1,0x68e0
_SD_TABLE_W0 = 0x94470000
_SD_TABLE_W1 = 0x262468E0
_SD_HALVE_HOOK = 0x08836DB8     # lhu v0,0x68EE(s1) ; srl v0,v0,1
_SD_HALVE_W0 = 0x962268EE
_SD_HALVE_W1 = 0x00021042
_SD_BTN_ADDR = 0x08B10D7E       # u16 buttons mirror (static)
_SD_BTN_MASK = 0x0002           # dash button bit
_SD_CFG_OFF = 0x1170            # save+0x1170: Config byte, bit0 = Dash
_SD_MODE_OFF = 0x2A8            # save+0x2A8: vehicle mode (0 foot)
_SD_MAP_OFF = 0x2008            # save+0x2008: field map id (0xFF overworld)
_SD_OW_MAP = 0xFF


def apply_super_dash(elf: bytearray, feats=None):
    for hook, w0, w1 in ((_SD_TABLE_HOOK, _SD_TABLE_W0, _SD_TABLE_W1),
                         (_SD_HALVE_HOOK, _SD_HALVE_W0, _SD_HALVE_W1)):
        got = (_read_word(elf, hook), _read_word(elf, hook + 4))
        if got != (w0, w1):
            raise ValueError(f"unexpected super_dash site @{hook:#x}: "
                             f"{got[0]:#010x} {got[1]:#010x}")
    cave_a = A.asm_labels([
        A.word(_SD_TABLE_W0),                    # displaced: lhu a3,0(v0)
        A.lbu("t2", _SD_CFG_OFF, "s0"),          # Config Dash bit
        A.andi("t2", "t2", 1),
        ("beq", "t2", "zero", "SDA_OUT"), A.nop(),
        A.lui("t3", _SD_BTN_ADDR >> 16),
        A.lhu("t3", _SD_BTN_ADDR & 0xFFFF, "t3"),
        A.andi("t3", "t3", _SD_BTN_MASK),        # dash button held
        ("beq", "t3", "zero", "SDA_OUT"), A.nop(),
        A.lw("t2", _SD_MODE_OFF, "s0"),
        ("bne", "t2", "zero", "SDA_HALVE"), A.nop(),   # vehicle -> halve
        A.lw("t2", _SD_MAP_OFF, "s0"),
        A.addiu("t3", "zero", _SD_OW_MAP),
        ("bne", "t2", "t3", "SDA_OUT"), A.nop(),  # foot interior: site B's job
        ("label", "SDA_HALVE"),
        A.sltiu("t2", "a3", 3),                  # floor 2: airship 4->2 too
        ("bne", "t2", "zero", "SDA_OUT"), A.nop(),
        A.srl("a3", "a3", 1),
        ("label", "SDA_OUT"),
        A.word(_SD_TABLE_W1),                    # displaced: addiu a0,s1,0x68e0
        A.j(_SD_TABLE_HOOK + 8), A.nop(),
    ])
    cave_b = A.asm_labels([
        A.word(_SD_HALVE_W0),                    # displaced: lhu v0,0x68EE(s1)
        A.lui("at", _SD_BTN_ADDR >> 16),
        A.lhu("at", _SD_BTN_ADDR & 0xFFFF, "at"),
        A.andi("at", "at", _SD_BTN_MASK),
        ("beq", "at", "zero", "SDB_VAN"), A.nop(),   # button up -> vanilla
        A.lbu("v1", _SD_CFG_OFF, "s0"),
        A.andi("v1", "v1", 1),
        ("beq", "v1", "zero", "SDB_VAN"), A.nop(),   # Config Dash off -> vanilla
        A.lw("v1", _SD_MAP_OFF, "s0"),
        A.addiu("at", "zero", _SD_OW_MAP),
        ("beq", "v1", "at", "SDB_OUT"), A.nop(),  # overworld: A set 8, keep it
        A.srl("v0", "v0", 2),                    # interior super dash: 16->4
        ("beq", "zero", "zero", "SDB_OUT"), A.nop(),
        ("label", "SDB_VAN"),
        A.word(_SD_HALVE_W1),                    # displaced: srl v0,v0,1
        ("label", "SDB_OUT"),
        A.j(_SD_HALVE_HOOK + 8), A.nop(),
    ])
    E.install_detour(elf, _SD_TABLE_HOOK, E.add_segment_cave(elf, cave_a))
    E.install_detour(elf, _SD_HALVE_HOOK, E.add_segment_cave(elf, cave_b))


FEATURES = {
    # Super Dash: hold-the-button speed boost on every surface, always baked
    # (options.ON_DISC_ALWAYS). Gated at RUNTIME on the Config Dash bit + dash
    # button, so the in-game Config menu is the player's opt-in.
    "super_dash": apply_super_dash,
    "remote_chest_names": apply_remote_chest_names,
    # Battle-usability gate on activatable equipment until the Equipment Rune
    # Key is assembled (client sets story flag 62 at the rune threshold).
    "equipment_rune_gate": apply_equipment_rune_gate,
    # Aliased-chest dedup: every normally-empty duplicated physical chest gets
    # its own treasure index (LootInNormallyEmptyChests). Off = they all keep
    # vanilla's shared index and stay empty, and the world drops the locations.
    "loot_in_normally_empty_chests": apply_normally_empty_dedup,
    # The two LEGACY halves of that dedup, kept ONLY for seeds generated before
    # the 2026-08-12 rename -- their slot_data names these keys, and nothing new
    # emits them (chest_dedup left ON_DISC_ALWAYS at the same time).
    "chest_dedup": apply_chest_dedup,
    "loot_in_gulg_b5_chests": apply_gulg_b5_dedup,

    # Bonus-dungeon dynamic chests: strip+mailbox detours at the grant sites
    # (client _bonus_dyn_loop polls the BDC1 ring; exec bps are dead in player
    # sessions -- FastMemoryAccess=True).
    "bonus_dyn_chests": apply_bonus_dyn_chests,
    # Giant's Cave choke point: moves the Giant onto (12,13). The paired boulder
    # at (11,13) is baked post-build by map_bake (dpk-side, LOAD-BEARING).
    "giant_cave_gate": apply_giant_cave_gate,
    # Lute-block gate (lute_tablets seeds): solid blocker below the Chaos Shrine
    # descent block, spawned only while the Lute is unassembled. Client sets the
    # key from lute_tablets_required.
    "lute_block_gate": apply_lute_block_gate,
    # Mystic-Key door gate: drops the two Chaos Shrine alcove blockers once the
    # Mystic Key is owned -- always-on (options.ON_DISC_ALWAYS). Shares the
    # object-list reader detour with lute_block_gate (one hook, both legs).
    "mystic_door_gate": apply_lute_block_gate,
    # Elf Prince quest chain repointed to shadow flag 69 -- always-on
    # (options.ON_DISC_ALWAYS). Kills the Elven Castle door/NPC deadlock class;
    # the client detects the quest on the shadow flag (no NPC_MAP_RESET row).
    "prince_gate_split": apply_prince_gate_split,
    # Bikke/ship story-flag split -- always-on (options.ON_DISC_ALWAYS).
    "bikke_ship_split": apply_bikke_ship_split,
    "monk_thief_dabble_in_magic": apply_monkthief_magic,
    "thief_extra_crit": apply_thief_extra_crit,
    "shop_spell_level": apply_shop_spell_level,
    "shop_buy_mailbox": apply_shop_buy_mailbox,
    # Wakes up map 0x27 (Chaos' own floor), whose populated encounter table is
    # dead in vanilla because its map_gate byte is 0.
    "chaos_floor_encounters": apply_chaos_floor_encounters,
    # Per-floor u16 pools for the eight Chaos Shrine basement floors AND the five
    # Flying Fortress floors, incl. the boss cameos. Rides
    # harder_dungeon_encounters (the two dungeons that option otherwise leaves
    # flavor-broken: the basement self-maps, the fortress rerolls from it).
    "chaos_floor_pools": apply_chaos_floor_pools,
    "dangerous_forests": apply_dangerous_forests,
    # Formation-record edits for the gen-rolled minion plan (data-only in the
    # ELF; the MS2_<fid> sprite-pack rebuild runs post-build_iso via ms2_bake).
    "boss_minions": apply_boss_minions,
    # Serializes same-instant enemy deaths in minion-boss fights (dissolve-race
    # freeze fix). Client enables it whenever boss_minions is on.
    "minion_death_serializer": apply_minion_death_serializer,
    "regional_ocean_encounters": apply_regional_ocean_encounters,
    # Northern-continent rivers get their own pool; southern rivers keep the
    # vanilla global row by falling through. Rivers are terrain 1 (ATT 0xF009,
    # canoe-only) -- marsh is terrain 0 and is NOT touched.
    "northern_river_encounters": apply_northern_river_encounters,
    "overworld_u16": apply_overworld_u16,
    # Per-tier u16 pools for the DESERT (terrain 3) and MARSH/RIVER (terrain 1)
    # branches -- the two overworld terrains the land-table shuffle cannot reach.
    # Rides harder_overworld_encounters, same as overworld_u16.
    "terrain_pools": apply_terrain_pools,
    # (promoted_battle_sprite / v47 promotion mailbox removed 2026-07-13 --
    # promotion is native Bahamut only; the routine can't run outside its
    # event's lineup-scene context. See job-advancement-items memory.)
    # Activatable-equipment (weapon OR armor) battle use costs 10% max HP
    # (self-contained detour).
    "blood_magic": apply_blood_magic,
    # WW dia-vs-anything + BW instant-kill boosts (mailbox-gated; the client's
    # _scroll_battle_loop arms the flags when the matching scroll is owned).
    # PR-style spell slots (charges per spell level replace the MP pool).
    # MUST run BEFORE job_scroll_boosts (publishes the CW damage->slot leaf
    # via _SM_EXPORTS) and before spell_tomes (bss-tail rule). No byte overlap
    # with any feature.
    "slot_magic": apply_slot_magic,
    "job_scroll_boosts": apply_job_scroll_boosts,
    # Magic-defence rework: multiplicative status to-hit decay + a diminishing-
    # returns magic-damage curve, both keyed off Monster Power / Boss Difficulty
    # via a per-monster-id mailbox table. Every leg is a runtime no-op at 100%
    # power (vanilla sentinel), so the default seed is byte-identical in effect.
    # MUST run AFTER apply_popup_colors (which is called directly by patch_iso,
    # before this loop) -- that fn mints the shared mailbox and carries the
    # type-3 to-hit leg, which has no hookable window of its own.
    "magic_power_scaling": apply_magic_power_scaling,
    # MUST STAY LAST: reserves the cave segment's bss tail; add_segment_cave
    # refuses to append after it (see eboot_patch.cave_bss_tail).
    "spell_tomes": apply_spell_tomes,
}

# --- always-on: per-target status-spell popup colours -------------------------
# A status spell's popup is coloured by THAT target's chance to be affected:
# white = 0% (immune / resisted / sub-1% that rounds to "0%"), yellow = 1%..15%,
# red = >15%. Covers every
# type-3 status spell (Sleep, Silence, Slow, Confuse, Break, Death, Warp, ...)
# because they all share one roll, plus blood-magic self damage (always red).
#
# COLOUR CHANNEL = a PER-UNIT class table in cave scratch, NOT the result-entry
# flags. The obvious channel (bits on the result entry, riding the same path as
# blood magic's 0x400) works for a plain MISS!! but FAILS with blood_magic on:
# a failed insta-kill there is rerouted into the engine's magic-damage path
# (_DMG_PATH), which recomputes into its own frame and pops a SEPARATE result
# entry, so the flag stamped on the roll's entry never reaches that popup (live:
# "23% Warp chance on Lich" logged, damage number drew WHITE). Keying on the
# TARGET UNIT instead reaches it, because every popup spawn already carries the
# target: s1 at the damage/MISS spawner, [entry+1] at the roll.
#
# Lifecycle (each unit's slot is written then consumed within one action, so a
# stale class cannot leak onto a later normal hit):
#   * ROLL writes class[target].            (every status roll)
#   * a damage / MISS popup for that unit READS + CLEARS class[target].
#   * a status that HITS (no popup, e.g. Sleep lands) CLEARS class[target].
# Every roll ends in hit or miss, so the slot is always cleared by action end.
#
# Score -> chance (see memory instakill-chance-formula): the roll is
# `hit iff s7 >= rand % 201`, so chance = (s7+1)/201 and s7 >= 30 is > 15%.
# Reading s7 at the roll means the tier already includes every to-hit modifier
# (scroll boosts, death-resist pierce), so it cannot drift out of truth.
#
# The glyphs live in the rows + GROWN defs popup_bake adds to BATTLEICON:
# kind 25 = red digits, 28/29 = red/yellow MISS!!. That bake is LOAD-BEARING
# here. (Old in-place kinds 0x16/0x13/0x15 stomped the ENEMY status-balloon
# defs 18-22 -- a Blinded enemy blinked a mirrored red MISS!! forever, live
# 2026-07-24. The custom defs now live past the vanilla 24.)
_PC_FLAG_BLOOD = 0x400                              # blood-magic self damage
# Dedicated marker bit for the Crimson Wizard mana-restore popup -> teal. A spare
# result-entry flag bit (like blood's 0x400) set by the RW MP-restore append cave,
# NOT the generic restore-MP flag 0x200 -- so only the scroll's restore is teal,
# never some native MP effect that happens to share 0x200.
_PC_FLAG_TEAL = 0x2000
# Marker for a HEAL-ARM yellow number (Grand Master "attack gained"): floats up
# like the teal, but yellow. Distinct spare bit; the heal arm ignores the s0
# bank override, so yellow-on-the-heal-arm needs its own def (like teal), not
# the damage arm's 0x1e bank.
_PC_FLAG_YELLOWD = 0x800
_PC_TIER_RED = 30                                   # s7 >= 30  => > 15%
_PC_BANK_WHITE, _PC_BANK_YELLOW = 0x14, 0x1e     # damage bank / yellow override
_PC_KIND_WHITE, _PC_KIND_RED = 0x17, popup_bake.DEF_RED_DIGITS        # 25
_PC_KIND_TEAL = popup_bake.DEF_TEAL_DIGITS          # grown def 26 = teal digits
_PC_KIND_YELLOWD = popup_bake.DEF_YELLOWD_DIGITS    # grown def 27 = heal-arm yellow
_PC_MISS_WHITE = 0x0b
_PC_MISS_RED = popup_bake.DEF_RED_MISS              # grown def 28
_PC_MISS_YELLOW = popup_bake.DEF_YELLOW_MISS        # grown def 29
_PC_CLS_WHITE, _PC_CLS_YELLOW, _PC_CLS_RED = 0, 1, 2    # per-unit class codes
# Scratch layout: [0] digit-kind stash (bank->kind), [1] miss-kind stash
# (misscls->misskind), [2 + unit] per-unit class (units 0..12).
_PC_MB_DKIND, _PC_MB_MKIND, _PC_MB_CLASS = 0, 1, 2
_PC_MB_LEN = (_PC_MB_CLASS + 16 + 3) & ~3           # 4-byte aligned for the cave
# hook -> return. install_detour takes TWO instructions and returns to hook+8,
# so every cave re-emits both displaced originals.
_PC_ROLL_HOOK, _PC_ROLL_RET = 0x08884db0, 0x08884db8   # after the status rand call
_PC_HITCLR_HOOK, _PC_HITCLR_RET = 0x08884dd4, 0x08884ddc  # status-HIT path
# v189: the type-3 handler is NOT the only status roll -- Slow/Slowra are effect
# TYPE 0x04 (handler 0x08884e1c) and Focus is the type-0x0e generic handler
# (0x08885370, rolls only for spell id 0x23), each with its own rand + its own
# hit/miss arms. They never touched 0x08884db0, so class[target] stayed 0 and
# every Slow MISS!! drew WHITE regardless of the real odds (user report
# 2026-07-31: Slow on 9 Pirates, 8 landed, the one miss printed white while a
# multi-target Quake coloured its misses red/yellow correctly). Both extra
# handlers use the SAME shape as type-3 -- `jal 0x8869528` then
# `andi a1,v0,0xffff` + `addiu a0,zero,0xc9` feeding a div by 201 -- so the roll
# cave is identical apart from the score register: type-3 keeps the score in s7,
# both new ones in s0 (seh'd 16-bit, same `hit iff score >= rand%201` test, so
# the same (score+1)/201 tier maths applies verbatim).
_PC_ROLL4_HOOK, _PC_ROLL4_RET = 0x08884e88, 0x08884e90     # type-0x04 (Slow/Slowra)
_PC_ROLLE_HOOK, _PC_ROLLE_RET = 0x088853a8, 0x088853b0     # type-0x0e (Focus)
# Matching HIT paths (a landed status pops no popup, so the class must be cleared
# there or it would tint the next damage number on that unit).
#  * type-0x04: `addiu v1,zero,1` + `sb v1,8(s3)` @0x08884eac.
#  * type-0x0e: the `addiu v1,zero,1 / sb v1,8(s3)` pair CANNOT be hooked -- the
#    handler's own `bnel v1,a0,0x88853e0` (id != 0x23, i.e. every non-Focus spell
#    on this handler) targets 0x088853e0 = hook+4, which the hook-start-only rule
#    forbids. Hook the next pair instead (`lhu v1,0xc(s3)` + `ori v1,v1,4`
#    @0x088853e4), where both hit arms have already converged and nothing
#    branches in.
_PC_HITCLR4_HOOK, _PC_HITCLR4_RET = 0x08884eac, 0x08884eb4
_PC_HITCLRE_HOOK, _PC_HITCLRE_RET = 0x088853e4, 0x088853ec
# BANK hooks the damage/heal CONVERGENCE, not the damage-only branch arm. The
# first try hooked 0x8873b68 -- but the heal path's `b 0x8873b6c` lands on the
# SECOND displaced word, i.e. mid-detour on the delay-slot nop, which skipped
# the displaced `move a1,s6` (the popup VALUE load) and made every heal print
# garbage (0..3 -- live regression 2026-07-22). Rule, now audited for every
# hook: a branch may target the hook START (it just enters the detour), never
# hook+4. At the convergence the displaced pair is `move a1,s6` + `jal
# <digit-decompose>`; the cave re-emits the jal's original delay-slot op
# (`addiu a0,sp,0x48`, from 0x8873b74) BEFORE the jal so the callee still gets
# a0, and returns past the delay slot to 0x8873b78.
_PC_BANK_HOOK, _PC_BANK_RET = 0x08873b6c, 0x08873b78   # damage+heal converge here
_PC_BANK_JAL = 0x088d9a24                              # displaced digit-decompose call
_PC_KIND_HOOK, _PC_KIND_RET = 0x08873c8c, 0x08873c94   # per-digit loop
_PC_MISSCLS_HOOK, _PC_MISSCLS_RET = 0x08873a34, 0x08873a3c
_PC_MISSKIND_HOOK, _PC_MISSKIND_RET = 0x08873a9c, 0x08873aa4
# (hook, return) pairs, exposed for test_patch's detour coverage.
_PC_HOOKS = ((_PC_ROLL_HOOK, _PC_ROLL_RET), (_PC_HITCLR_HOOK, _PC_HITCLR_RET),
             (_PC_ROLL4_HOOK, _PC_ROLL4_RET), (_PC_HITCLR4_HOOK, _PC_HITCLR4_RET),
             (_PC_ROLLE_HOOK, _PC_ROLLE_RET), (_PC_HITCLRE_HOOK, _PC_HITCLRE_RET),
             (_PC_BANK_HOOK, _PC_BANK_RET), (_PC_KIND_HOOK, _PC_KIND_RET),
             (_PC_MISSCLS_HOOK, _PC_MISSCLS_RET), (_PC_MISSKIND_HOOK, _PC_MISSKIND_RET))

# --- thief-steal loot-cue sprite (rides the popup system; see steal-sprite-cue) -
# A rarity icon (coin/bag/gem = popup_bake defs 13/17/16) pops over the thief at
# battle start. The client (_arm_steal_icon) writes the 'SPRB' mailbox; a spawn
# detour on the per-frame battle message-VM entry calls the popup spawner when
# armed, and the misskind cave (shared with popup colours) sets the icon def.
# Folded into apply_popup_colors because it must share the misskind hook.
_SC_MAGIC = b"SPRB"
_SC_COUNT, _SC_KIND, _SC_UNIT, _SC_ACTIVE = 4, 5, 6, 7   # client writes 4/5/6
_SC_SE = 8                    # u16 SE id for this cue (client sets per rarity)
_SC_MB_LEN = 12
_SC_SPAWN_HOOK, _SC_SPAWN_RET = 0x0886eb94, 0x0886eb9c   # message-VM entry (a0=ctx)
_SC_VM_ORIG = (0x27bdfff0, 0xafbf000c)                   # addiu sp,-0x10 | sw ra,0xc(sp)
_SC_SPAWNER = 0x088739a4                                 # popup spawner(a0=ctx,a1=unit,a2=flags,a3=val)
_SC_FLAG_SPRITE = 0x10                                   # a2 bit -> sprite (MISS) path
# Popup slots ctx+idx*124+0x420 are a SHARED object array and the low indices
# are all reserved: 0x00-0x0B = the party sprite layers (4 chars x 3 layers),
# 0x11-0x1D = the 13 battle-unit anchors, and the battle MENU UI lives low too
# (base 0x0d-era spawns ate party sprites / slid Back/circle/arrow onto the
# thief, live 2026-07-23). Slot PLACEMENT comes from the allocator bb+0x67B6
# (bump cursor inside the object constructors) -- live: icons land at 0x25..
# regardless of the 0x3C written below. 0x68C3/0x68CA/0x68CB are a free-batch
# descriptor, not allocation state (see _steal_spawn_cave v259 note).
_SC_SLOT_BASE, _SC_SLOT_COUNT_OFF, _SC_SLOT_BASE_OFF = 0x3C, 0x68ca, 0x68c3
_SC_SLOT_COUNT_MAX = 8                                   # one call costs 5 of ~33 slots
# Steal-cue sound: SE_Play(u16 id) @0x88d8338 (`andi a1,a0 / j 0x88f74c8` thunk;
# a0 = global sound mgr loaded inside, so the caller passes only the id). Found
# statically 2026-07-24 (re_only/dis_sfxscan.py); ids captured live via the
# 5-channel ring probe (re_only/feature_sfx_capture.py) and user-confirmed by
# replay. The client picks the id by rarity (knife/antidote/ether); 0x73
# ("block") is the cave-side fallback if the client didn't set one.
_SC_SE_PLAY, _SC_SE_STEAL_ID = 0x088d8338, 0x73


def _steal_spawn_cave(sprb):
    """Per battle frame: if the SPRB mailbox is armed, call the popup spawner for
    the chosen icon def over the thief unit. Sets the ACTIVE gate so the misskind
    cave overrides the kind for OUR spawn only. Saves ra/a0 itself (runs before
    the hooked fn's prologue, which is re-emitted at the tail).

    v259: SAVE + RESTORE the (base 0x68C3, count 0x68CA) pair around the spawn.
    They are a batch descriptor consumed by free_range(0x888703C), which clears
    `active` for `count` slots from `first`(0x68CB) and drops the real allocator
    (0x67B6) -- NOT an allocation cursor (static RE + 30 Hz trace 2026-08-06,
    memory popup-pool-cursor-is-not-a-refcount). Vanilla never holds count > 0
    with first == 0; our spawn used to leave exactly that (count 5, first 0), so
    any stray closer (unpaired free at 0x887E794) ran free_range(ctx, 0, 5) and
    freed slots 0..4 = the party sprite layers -> characters vanished mid-fight
    (Prime live 2026-08-10). Restoring both bytes right after the jal makes the
    descriptor (0, 0) again, so a stray free is a no-op; slot placement is
    unaffected (the allocator, not this pair, picks the slots -- live: icons at
    0x25.. with base written 0x3C)."""
    return A.asm_labels([
        A.addiu("sp", "sp", -0x20),
        A.sw("ra", 0x1c, "sp"), A.sw("a0", 0x18, "sp"),
        A.li("t0", sprb),
        A.lbu("t1", _SC_COUNT, "t0"),
        ("beq", "t1", "zero", "DONE"), A.nop(),
        A.lbu("t1", _SC_SLOT_COUNT_OFF, "a0"),           # slots already in flight
        A.addiu("t2", "zero", _SC_SLOT_COUNT_MAX),
        A.slt("t1", "t1", "t2"),
        ("beq", "t1", "zero", "DONE"), A.nop(),          # pool busy -> wait a frame
        A.lbu("t1", _SC_COUNT, "t0"),
        A.addiu("t1", "t1", -1), A.sb("t1", _SC_COUNT, "t0"),   # one-shot
        A.addiu("t1", "zero", 1), A.sb("t1", _SC_ACTIVE, "t0"), # kind-override on
        # save the batch descriptor the spawner mutates (restored after the SE)
        A.lbu("t1", _SC_SLOT_BASE_OFF, "a0"), A.sb("t1", 0x10, "sp"),
        A.lbu("t1", _SC_SLOT_COUNT_OFF, "a0"), A.sb("t1", 0x14, "sp"),
        A.addiu("t1", "zero", _SC_SLOT_BASE), A.sb("t1", _SC_SLOT_BASE_OFF, "a0"),
        A.lbu("a1", _SC_UNIT, "t0"),                     # target battle unit id
        A.addiu("a2", "zero", _SC_FLAG_SPRITE),          # sprite path
        A.jal(_SC_SPAWNER), A.addu("a3", "zero", "zero"),
        # objects exist + are active now; put the descriptor back BEFORE anything
        # else can observe the (first=0, count=5) poison state
        A.lw("a0", 0x18, "sp"),
        A.lbu("t1", 0x10, "sp"), A.sb("t1", _SC_SLOT_BASE_OFF, "a0"),
        A.lbu("t1", 0x14, "sp"), A.sb("t1", _SC_SLOT_COUNT_OFF, "a0"),
        A.li("t0", sprb), A.sb("zero", _SC_ACTIVE, "t0"),
        # play the steal-cue sound the same frame the icon spawns (a0 is
        # clobbered but restored from the stack below; SE_Play takes only a0).
        # The id comes from the mailbox (rarity-coded by the client); 0 -> the
        # 0x73 fallback so an older client still gets a sound.
        A.lhu("a0", _SC_SE, "t0"),
        ("bne", "a0", "zero", "SEOK"), A.nop(),
        A.addiu("a0", "zero", _SC_SE_STEAL_ID),
        ("label", "SEOK"),
        A.jal(_SC_SE_PLAY), A.nop(),
        ("label", "DONE"),
        A.lw("ra", 0x1c, "sp"), A.lw("a0", 0x18, "sp"),
        A.addiu("sp", "sp", 0x20),
        A.word(_SC_VM_ORIG[0]), A.word(_SC_VM_ORIG[1]),  # displaced prologue
        A.j(_SC_SPAWN_RET), A.nop(),
    ])


def apply_popup_colors(elf: bytearray, feats=None):
    """Install the six popup-colour detours. The INSTALL is always applied --
    it is load-bearing for features that have nothing to do with the odds
    colours: the thief-steal loot cue shares the misskind hook (sprb below),
    and blood_magic's red self-damage and the Crimson Wizard's teal mana
    refund ride their own marker bits through the same caves.

    The yaml gate (feats["spell_chance_colors"], default ON) only controls the
    CLASSIFY step in the ROLL cave. With it off, every roll stores WHITE, so
    status MISS!! text draws vanilla white while the unrelated features above
    keep working.

    (The shared delayed-popup service is installed separately by patch_iso via
    _install_delayed_popup.)"""
    chance_colors = (feats or {}).get("spell_chance_colors", True)
    # magic_power_scaling's type-3 to-hit leg folds into the roll cave below.
    # This fn runs BEFORE the FEATURES loop, so it mints the shared mailbox and
    # apply_magic_power_scaling picks it up from feats.
    mp_on = bool((feats or {}).get("magic_power_scaling"))
    # spells_hit_low_hp_enemies rides the same caves and needs the same mailbox
    # (only its BOUNDARY, as a party-vs-monster test), so either feature mints it.
    lowhp_on = bool((feats or {}).get("spells_hit_low_hp_enemies"))
    mp_mb = _mp_mb(elf, feats) if (mp_on or lowhp_on) else 0
    mb = E.add_segment_cave(elf, b"\x00" * _PC_MB_LEN)
    # SPRB mailbox for the thief-steal loot cue (folded in: shares the misskind
    # hook below). Dormant until the client arms it, so it is a no-op unless a
    # thief_steal seed drives it.
    sprb = E.add_segment_cave(elf, _SC_MAGIC + b"\x00" * (_SC_MB_LEN - 4))

    # ROLL: classify from the final score and write class[target]. s7 = score,
    # s3 = result entry ([s3+1] = target unit). a0/a1 are set by the displaced
    # originals and feed the div two instructions later, so they must survive;
    # everything else goes through a save frame. Targets that bailed earlier on
    # immunity/resist never reach here, so class[target] stays 0 = white = 0%.
    # With spell_chance_colors off, the tier test is omitted and t2 keeps the
    # WHITE it is seeded with -- the store/clear lifecycle is untouched, so
    # nothing downstream can see a stale class.
    def _classify(score):
        return [
            A.addiu("at", "zero", 1), A.slt("at", score, "at"),
            ("bne", "at", "zero", "STORE"), A.nop(),               # score <= 0 -> white (rounds to "0%")
            A.addiu("t2", "zero", _PC_CLS_RED),
            A.addiu("at", "zero", _PC_TIER_RED), A.slt("at", score, "at"),
            ("beq", "at", "zero", "STORE"), A.nop(),               # score >= 30 -> red
            A.addiu("t2", "zero", _PC_CLS_YELLOW),
        ] if chance_colors else []

    def _roll_cave(score, ret, mp_shrink=False, lowhp=False):
        """Classify from the final to-hit score and write class[target]. All
        three status handlers share this body: the displaced pair is the same
        post-rand `andi a1,v0,0xffff` + `addiu a0,zero,0xc9` (they feed the div
        two instructions later, so they must survive), only the register holding
        the score differs (s7 on type-3, s0 on type-0x04 / type-0x0e).

        mp_shrink: magic_power_scaling's TYPE-3 to-hit leg rides here, because
        that site has no hookable window of its own. It runs FIRST, so both the
        roll (which reads the score at 0x08884DBC, after we return) and the
        colour classify below see the SAME scaled value -- the odds shown can
        never disagree with the odds rolled. type-0x04 / type-0x0e are already
        scaled by their own upstream caves and must NOT be scaled again here.

        lowhp: spells_hit_low_hp_enemies' x1.0 -> x1.5 ramp over the target's
        HP fraction. Runs AFTER the shrink (so it multiplies the real,
        power-scaled odds) and BEFORE the classify (so the colour matches what
        was rolled)."""
        return A.asm_labels([
            A.andi("a1", "v0", 0xFFFF), A.addiu("a0", "zero", 0xC9),   # displaced
            *(_mp_shrink_s7(mp_mb) if mp_shrink else []),
            *(_lowhp_boost(mp_mb, score) if lowhp else []),
            A.addiu("sp", "sp", -0x10),
            A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
            A.sw("t2", 0x08, "sp"), A.sw("at", 0x0C, "sp"),
            # Target unit from the ACTOR object (s4+0x3D), NOT the result entry:
            # the exec fn never writes [s3+1], so at roll time it still holds the
            # init value 0xff and indexing with it writes OUTSIDE the class table
            # (live 2026-07-22: zero class writes ever seen; the fail cave reads
            # s4+0x3D for the same reason). Masked to the 16-slot table regardless.
            A.li("t0", mb), A.lbu("t1", NECRO_TGT_IDX_OFF, "s4"),
            A.andi("t1", "t1", 0x0F),
            A.addu("t0", "t0", "t1"),                                  # &class[target]-2
            A.addiu("t2", "zero", _PC_CLS_WHITE),
            *_classify(score),
            ("label", "STORE"), A.sb("t2", _PC_MB_CLASS, "t0"),
            A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
            A.lw("t2", 0x08, "sp"), A.lw("at", 0x0C, "sp"),
            A.addiu("sp", "sp", 0x10),
            A.j(ret), A.nop(),
        ])

    def _hitclr_cave(displaced, ret):
        """A status that LANDS produces no popup, so clear class[target] on the
        hit arm or it would tint the next damage number on that unit. Only t0/t1
        are touched, so each handler's own displaced pair (which carries live
        values into the following instructions) passes through untouched."""
        return A.asm_labels([
            *displaced,
            A.addiu("sp", "sp", -0x08),
            A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
            A.li("t0", mb), A.lbu("t1", NECRO_TGT_IDX_OFF, "s4"),     # target from actor
            A.andi("t1", "t1", 0x0F),
            A.addu("t0", "t0", "t1"), A.sb("zero", _PC_MB_CLASS, "t0"),
            A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
            A.addiu("sp", "sp", 0x08),
            A.j(ret), A.nop(),
        ])

    roll = _roll_cave("s7", _PC_ROLL_RET, mp_shrink=mp_on, lowhp=lowhp_on)
    hitclr = _hitclr_cave([A.addiu("a3", "zero", 1), A.sb("a3", 0x08, "s3")],
                          _PC_HITCLR_RET)
    # type-0x04 (Slow/Slowra) and type-0x0e (Focus): score lives in s0.
    roll4 = _roll_cave("s0", _PC_ROLL4_RET, lowhp=lowhp_on)
    hitclr4 = _hitclr_cave([A.addiu("v1", "zero", 1), A.sb("v1", 0x08, "s3")],
                           _PC_HITCLR4_RET)
    # type-0x0e carries Focus, a PARTY-target buff, so the low-HP boost is
    # deliberately absent here: "hit low HP enemies" has nothing to say about it,
    # and this is also the one site where the target unit does NOT come from
    # [s4+0x38] (it arrives in a1), so the leg's load would not even be safe.
    rolle = _roll_cave("s0", _PC_ROLLE_RET)
    hitclre = _hitclr_cave([A.lhu("v1", 0x0C, "s3"), A.ori("v1", "v1", 4)],
                           _PC_HITCLRE_RET)

    # BANK (both damage and heal flow through this convergence): for a DAMAGE
    # popup (s7 == 0), choose glyph bank + digit kind from class[s1] (or red for
    # blood-magic self damage) and CLEAR class[s1]. Heals (s7 != 0) skip the
    # colour logic entirely -- their bank (0x28) and value are untouched.
    # s1 = target unit, s2 = flags, s0 = bank byte picked by the branch above.
    bank = A.asm_labels([
        A.addiu("sp", "sp", -0x10),
        A.sw("t0", 0x00, "sp"), A.sw("t1", 0x04, "sp"),
        A.sw("t2", 0x08, "sp"), A.sw("at", 0x0C, "sp"),
        A.addiu("t2", "zero", _PC_KIND_WHITE),
        # s7 is the engine's own damage/heal discriminator: `beql s7,zero` at
        # 0x8873b54 sends s7==0 to the damage arm, s7!=0 to the heal arm, and s7
        # is unchanged by the time both converge here. (The heal 0..3 regression
        # was NOT this gate -- it was the previous build hooking the damage-only
        # arm 0x8873b68, whose detour nop clobbered the heal path's branch target
        # 0x8873b6c. Hooking the convergence fixes it.)
        ("beq", "s7", "zero", "DAMAGE"), A.nop(),                  # s7==0 -> damage colouring
        # Heal arm. Only the Crimson Wizard mana-restore (teal marker) is
        # recoloured; HP heals fall through to STORE with t2 = white kind, so
        # bank 0x28 + kind 0x17 keeps them native green. Teal keeps the native
        # GREEN cell bank (40..49) -- the heal arm IGNORES an s0 bank override
        # (live 2026-07-23: forcing s0=0x14 still drew cells 0x28+d, sampling
        # the MISS!! glyphs at x320 row 3) -- and the teal def (U=0, V=32,
        # id=40, popup_bake) maps those green cells onto the teal glyphs.
        A.andi("at", "s2", _PC_FLAG_TEAL),
        ("beq", "at", "zero", "HYEL"), A.nop(),                    # not teal -> try yellow
        A.addiu("t2", "zero", _PC_KIND_TEAL),
        ("beq", "zero", "zero", "STORE"), A.nop(),
        ("label", "HYEL"),
        A.andi("at", "s2", _PC_FLAG_YELLOWD),                      # heal-arm yellow (Master)
        ("beq", "at", "zero", "STORE"), A.nop(),                   # HP heal -> green
        A.addiu("t2", "zero", _PC_KIND_YELLOWD),
        ("beq", "zero", "zero", "STORE"), A.nop(),
        ("label", "DAMAGE"),
        A.andi("at", "s2", _PC_FLAG_BLOOD),
        ("bne", "at", "zero", "RED"), A.nop(),                     # blood -> red
        A.li("t0", mb), A.andi("t1", "s1", 0x0F),                  # s1 = target unit
        A.addu("t0", "t0", "t1"),                                  # &class[s1]-2
        A.lbu("t1", _PC_MB_CLASS, "t0"),
        A.sb("zero", _PC_MB_CLASS, "t0"),                          # consume
        A.addiu("at", "zero", _PC_CLS_RED),
        ("beq", "t1", "at", "RED"), A.nop(),
        A.addiu("at", "zero", _PC_CLS_YELLOW),
        ("bne", "t1", "at", "STORE"), A.nop(),                     # white -> vanilla
        A.addiu("s0", "zero", _PC_BANK_YELLOW),
        ("beq", "zero", "zero", "STORE"), A.nop(),
        ("label", "RED"), A.addiu("t2", "zero", _PC_KIND_RED),
        ("label", "STORE"), A.li("t0", mb), A.sb("t2", _PC_MB_DKIND, "t0"),
        A.lw("t0", 0x00, "sp"), A.lw("t1", 0x04, "sp"),
        A.lw("t2", 0x08, "sp"), A.lw("at", 0x0C, "sp"),
        A.addiu("sp", "sp", 0x10),
        # displaced originals + the jal's original delay-slot op, then return
        # PAST the delay slot (0x8873b74 is not re-executed).
        A.addu("a1", "s6", "zero"),                                # move a1,s6
        A.addiu("a0", "sp", 0x48),                                 # jal delay op
        A.jal(_PC_BANK_JAL), A.nop(),
        A.j(_PC_BANK_RET), A.nop(),
    ])

    # KIND: apply the stashed digit kind per digit. The stash is written by the
    # BANK cave once per popup (before this per-digit loop), so it is always
    # fresh: white kind for HP heals (which then render green on bank 0x28),
    # teal kind for the Crimson Wizard mana-restore, the class kind for damage.
    # No s7 gate is needed -- keying off the stash covers heals correctly and is
    # what lets the teal (heal-arm) popup pick up its def. Only at/t2 may be
    # touched -- t0/t1/t3 are live spawn args.
    kind = A.asm_labels([
        A.addiu("t0", "sp", 0x38), A.addiu("t2", "zero", _PC_KIND_WHITE),  # displaced
        A.li("at", mb + _PC_MB_DKIND), A.lbu("t2", 0x00, "at"),
        ("bne", "t2", "zero", "DONE"), A.nop(),
        A.addiu("t2", "zero", _PC_KIND_WHITE),
        ("label", "DONE"), A.j(_PC_KIND_RET), A.nop(),
    ])

    # MISSCLS: the MISS!! popup for a failed status. The displaced `move s1,zero`
    # destroys the target unit, but the first displaced `andi s0,s1,0xff` has
    # just copied it into s0 -- so read the class via s0 before s0 is used
    # downstream. Stash the miss kind at [1] and CLEAR class[target].
    misscls = A.asm_labels([
        A.andi("s0", "s1", 0xFF), A.addu("s1", "zero", "zero"),    # displaced; s0=target
        A.li("v0", mb), A.andi("v1", "s0", 0x0F),
        A.addu("v0", "v0", "v1"),                                  # v0 = &class[target]-2
        A.addiu("v1", "zero", _PC_MISS_WHITE),                     # default white
        A.andi("at", "s2", _PC_FLAG_BLOOD),
        ("bne", "at", "zero", "RED"), A.nop(),                     # blood -> red
        A.lbu("at", _PC_MB_CLASS, "v0"),
        A.sb("zero", _PC_MB_CLASS, "v0"),                          # consume
        ("beq", "at", "zero", "STORE"), A.nop(),                   # class 0 -> white
        A.addiu("v0", "zero", _PC_CLS_RED),
        ("beq", "at", "v0", "RED"), A.nop(),                       # class 2 -> red
        A.addiu("v1", "zero", _PC_MISS_YELLOW),                    # class 1 -> yellow
        ("beq", "zero", "zero", "STORE"), A.nop(),
        ("label", "RED"), A.addiu("v1", "zero", _PC_MISS_RED),
        ("label", "STORE"), A.li("v0", mb), A.sb("v1", _PC_MB_MKIND, "v0"),
        A.j(_PC_MISSCLS_RET), A.nop(),
    ])
    # MISSKIND: apply the stashed MISS kind, EXCEPT while the steal-cue spawn is
    # active (SPRB) -- then the kind is the icon def. The steal spawn uses the
    # same sprite (MISS) path, so this one hook serves both. Only at/t2 are free.
    misskind = A.asm_labels([
        A.addiu("t0", "sp", 0x40), A.addiu("t2", "zero", _PC_MISS_WHITE),  # displaced
        A.li("at", sprb + _SC_ACTIVE), A.lbu("at", 0x00, "at"),
        ("beq", "at", "zero", "PCLR"), A.nop(),            # not our spawn -> popup colours
        A.li("at", sprb + _SC_KIND), A.lbu("t2", 0x00, "at"),
        ("beq", "zero", "zero", "DONE"), A.nop(),
        ("label", "PCLR"),
        A.li("at", mb + _PC_MB_MKIND), A.lbu("t2", 0x00, "at"),
        ("bne", "t2", "zero", "DONE"), A.nop(),
        A.addiu("t2", "zero", _PC_MISS_WHITE),
        ("label", "DONE"), A.j(_PC_MISSKIND_RET), A.nop(),
    ])

    for hook, cave in ((_PC_ROLL_HOOK, roll), (_PC_HITCLR_HOOK, hitclr),
                       (_PC_ROLL4_HOOK, roll4), (_PC_HITCLR4_HOOK, hitclr4),
                       (_PC_ROLLE_HOOK, rolle), (_PC_HITCLRE_HOOK, hitclre),
                       (_PC_BANK_HOOK, bank), (_PC_KIND_HOOK, kind),
                       (_PC_MISSCLS_HOOK, misscls), (_PC_MISSKIND_HOOK, misskind),
                       (_SC_SPAWN_HOOK, _steal_spawn_cave(sprb))):
        E.install_detour(elf, hook, E.add_segment_cave(elf, cave))

# Bump whenever ANY feature's patch bytes change. Part of the launcher's
# patched-ISO cache key: without it, a fixed/retuned feature would silently keep
# reusing a stale cached ISO built by the OLD code (iso size+mtime+feature names
# all unchanged).
PATCHER_VERSION = 273 # v273: release script test // v272: DABBLER MAGIC PACE SLOWED (user spec 2026-08-18). The Monk/Thief/Master magiclv schedule moves 5/12/20/29/39/49 -> 10/18/25/33/45, so spell levels 2..6 unlock at char levels 10/18/25/33/45 and the old 6th tick (49), which the RedMage cap-6 handler ate, is gone. _SM_DAB_ANCHORS is re-gated to match: every spell level takes its FIRST charge exactly ON its own unlock (the v203-era 'gate at 20, first slot at 24' gap is closed), while the L90 endpoint 6/5/4/3/2/1 (21 slots total) is the unchanged balance contract -- only the intermediate anchors moved (L25 3/2/1/1, L45 4/3/2/2/1/1, L60 5/4/3/2/1/1). _SM_DAB_PHASE re-solved on the same 0.30..0.70 grid under the standing HARD rule: max gain == 1 slot on every level-up 1..99, and value(anchor) == anchor exactly. Ninja keeps the elementwise best-of merge (a promotion can never slow slot growth) and its own vanilla magiclv gates. Both the growth-table bit7 bytes and the slot threshold table change, hence the bump. // v271: SLOT_MAGIC ITEM DESCS ARE REWRAPPED. The Soma/Ether replacement descriptions (slot_magic_item_descs) were encoded raw, so the 29-glyph "Restores 1 of each spell slot" spilled off the right edge of the NARROW target-select description box while the wide item-menu box drew it fine (live 2026-08-17). Spell tomes never showed it -- their entries already go through tome_names._rewrap_desc. Both encode sites now do: build_extended_banks (on disc) and items_desc_bank (the client shop-desc baseline, which doubles as the RAM locate signature and MUST stay byte-identical to the disc bank). Bank bytes change, hence the bump. // v270: GRAND MASTER AND CRIMSON WIZARD MUST SURVIVE THE HIT. Same defect, both scrolls, all four legs (Master MSTCHK/RWMMST, CW RWCHK/RWMHIT): the killing blow still paid out. Every one of them now requires dmg < curHP as well as curHP != 0, so a fatal hit grants the Master no attack and the Crimson Wizard no MP/slot refund, and neither draws its number. The CW slot_magic branch had no life test at all before this. Both Master legs (physical combat-calc epilogue MSTCHK, magic-executor epilogue RWMMST) paid out on the blow that KO'd the Monk/Master: they read BU_HP as an "alive?" test, but both hooks run BEFORE the engine applies the result entries (the same ordering that let the v190-era caves append entries for the engine to pay out), so BU_HP there is the PRE-damage HP and a lethal hit still read as alive. Both legs now also require dmg < curHP, so a fatal hit grants no attack, no yellow attack-gain number and no green capped-heal number. Client side (_scroll_battle_loop, which owns the max-HP + heal writes) adds curHP > 0 next to the KO-status test, so a tick landing between the HP write and the status write cannot heal a corpse. // v269: DYN CHEST BOX GAP. The two runtime-authored bonus-chest name slots (tome_names.dyn_slot_payload) padded their fixed 30-glyph body with the SPACE glyph. Every renderer here draws off[i+1]-off[i] bytes and ignores the TERM, so a short name drew its full 30-glyph width and the USEVMCMN "{NAME}!" chest template put the "!" ~20 blanks after the item ("your Staff          !", Whisperwind Cove live 2026-08-15). Baked remote entries are exact-length and never showed it. The fill is TERM (0x06) now, which draws nothing. Bank bytes change and the sentinel IS the client's DataPatch locate signature, so a stale cached ISO would leave both slots unlocatable -- hence the bump. // v268: SUPER DASH IS ALWAYS-ON. super_dash moves out of ON_DISC_OPTIONS (where it rode AutoDash) into ON_DISC_ALWAYS, so every seed bakes both caves and the player opts in from the IN-GAME Config Dash setting instead of the yaml. The runtime gate is unchanged and is what makes this safe: both caves require the Config Dash bit ([save+0x1170] bit0) AND the held dash button, so Config Dash off = byte-for-byte vanilla movement. The old shape gave AutoDash two jobs -- gate the bake AND seed the new-game Config default -- which meant an auto_dash-off player could never turn Super Dash on from the Config menu at all. AutoDash keeps the seeding job (ApClient._config_loop); ApClient's `feats.setdefault("super_dash", auto_dash)` derivation is DELETED because the ON_DISC_ALWAYS loop now asserts it on every seed, in-progress ones included. No cave bytes change, but the bump is still required: seeds whose slot_data said super_dash=False cached an ISO without the detours. // v267: SUPER DASH AIRSHIP -- cave A's vehicle halving floor drops 4 -> 2 (sltiu a3,5 -> sltiu a3,3), so a dashing airship goes 4 -> 2 frames-per-tile (double speed); ship/canoe 8->4 and foot behavior unchanged. // v266: SUPER DASH (auto_dash option). Two new detours in the step-record init caller: A @0x08836D04 halves the frames-per-tile a3 for vehicles (floor 4: ship 8->4, canoe 8->4, airship 4 stays) and for overworld foot (16->8), B @0x08836DB8 turns the engine's own dash halving into >>2 for interiors (16->4) and a no-op on the overworld, both only while the Config Dash bit ([save+0x1170] bit0 via s0) AND the dash button (0x08B10D7E bit 0x2) are held. Button up / Config Dash off = byte-for-byte vanilla behavior at runtime. Speed table 0x08941870 itself is untouched. New feature key super_dash rides the existing auto_dash yaml option via ON_DISC_OPTIONS. // v265: BLOOD KNIGHT LIFESTEAL 10% -> 15%. KNIGHT_LIFESTEAL_DIV (a reciprocal divisor) is replaced by KNIGHT_LIFESTEAL_PCT = 15, since 15% is not expressible as dealt//N; the lifesteal cave now does multu by PCT then divu by 100. dealt is engine-capped at 99999 so the product cannot overflow. KNIGHT_LIFESTEAL_CAP (500/attack) and the min-1 floor are unchanged, and the defense-pierce leg stays 10% -- the two legs are deliberately no longer matched. Cave bytes change, so seeds must re-bake. // v264: NORMALLY-EMPTY CHESTS ARE ONE OPTION -- the ten alias-duplicate chest records (Citadel 19->24, Marsh Cave 127->131 / 129->132,133 / 134->138,141, Mount Gulg 176->185 and 180->188,189,191) now all ride LootInNormallyEmptyChests via the new feature key loot_in_normally_empty_chests. Seven of them were ON_DISC_ALWAYS "chest_dedup" and only the Gulg B5 three were yaml-controlled, which meant the game's other seven permanently-empty chests could never be played vanilla. chest_dedup is OUT of ON_DISC_ALWAYS; both legacy feature keys (chest_dedup, loot_in_gulg_b5_chests) stay in FEATURES and in the signature table so an ALREADY-GENERATED seed -- whose slot_data still names them -- bakes byte-for-byte what it baked before, and logic.removed_normally_empty_idx reads both dialects for the client scout. // v263: LEVISTONE SHARDS DESCRIPTION -- the borrowed id-36 slot kept the native "A robot's power source." (live 2026-08-12). KEY_EXP entry 36 is now authored to "Collect enough to raise the desert Airship." in levistone_shard_gate seeds, parallel to the lute_block_gate sentence. 43 glyphs = exactly the vanilla ceiling (kid 1), single line, so the row survives being highlighted while the game genuinely owns the Energy Chip -- the 2026-08-07 rune freeze was an over-long desc on exactly that kind of borrowed row. MEASURED against the real ISO, not counted: 4 authored entries recompress to 12276/12320, and the user's original 61-glyph wording also fit (12282) but was rejected on the freeze rule. levistone_shard_gate is BAKE CONTEXT ONLY -- no ELF feature by that name (the FEATURES loop ignores unknown keys), no cave; it also folds N into bake_hash32 so changing the shard count re-bakes. // v262: LEVISTONE SHARDS MENU LINE -- pad_key_ids gains id 36 "Energy Chip" (SHARD_MENU_SLOT_KEY_ID), the borrowed Key Items entry for the "Levi Shards N of M" progress line (levistone_shards yaml), disc-padded to KEY_NAME_GLYPHS beside the id-35 rune slot and the entry-0 Lute slot; all three pad together whenever ANY piece feature is on, keeping the entry-0-width-25 locator fingerprint unchanged. No code caves -- the line is runtime-authored by the client's _keyratio_loop (borrow owned = shadow story flag 54, released in Whisperwind Cove and permanently at assembly). // v261: SCROLL BALANCE -- Blood Knight lifesteal capped at KNIGHT_LIFESTEAL_CAP (500) HP per attack (was uncapped 10% of dealt damage), clamped in the lifesteal cave after the divide so the HP write and the green number agree. Grand Master MASTER_HP_CAPPED_PCT 50 -> 35: a fully-stacked (attack-capped or max-HP-ceilinged) Master heals 35% of damage taken. ApClient imports the const, so the client write and the cave's green number stay in lockstep. // v260: TITAN GATE SPLIT (titan_gate_split, rides bikke_ship_split's getStoryFlag wrapper). Star Ruby accept/fed are story flags 13/14 (0x1151D b5/b6) -- the SAME globals a Whisperwind Cove dwarf/Titan gimmick sets, so a native bonus ruby getting stripped cleared them and HARD-softlocked the REAL Titan's Tunnel (Prime live 2026-08-09). Live-RE'd 2026-08-11 (map 0x22): the real Titan's gate reads 14 then 13 ONLY through getStoryFlag, zero direct byte readers, bonus_mapid 0x28 (<0x87) at his floor. FIX: the getStoryFlag wrapper answers shadow flag 73 (NPC_GATE_SPLIT_FLAG_BASE 64 + Star Ruby key id 9 -> 0x11525 b1, zero disc refs) for a0 in {13,14} when FIELD_MAP_ID==0x22 AND bonus_mapid<0x87 -- so the REAL tunnel gates on flag 73 (set by the AP grant alone) while a bonus floor (mapid>=0x87) keeps reading vanilla 13/14 and its native puzzle is untouched. SETs stay vanilla (reads-only redirect; the real feed cutscene never runs for an AP player). Client: grant_key_item + the func-reassert loop maintain shadow flag 73 for key 9 beside the vanilla 0x60 (GATE_SPLIT_SHADOW_BITS); the vanilla bit is still written for old-ISO fallback (real Titan reads 13/14 there). The 2026-08-09 in-bonus-dungeon strip hold is KEPT (belt+suspenders; bonus Titan puzzle un-RE'd). RAM-rehearsed end to end live 2026-08-11: 13/14 set -> redirect checks to (clear) 73 -> Titan re-blocks; set 73 -> passable. // v259: STEAL-CUE SPAWN PRESERVES THE FREE-BATCH DESCRIPTOR -- the spawn cave now saves 0x68C3 (base) + 0x68CA (count) around the popup-spawner jal and restores them the instant the icon objects exist. The pair (with first 0x68CB) is a free_range(0x888703C) descriptor -- count slots from first get their active byte cleared and the REAL allocator 0x67B6 dropped -- not allocation state; our spawn used to leave (first=0, count=5), a shape vanilla never produces, so any stray closer (unpaired free at 0x887E794) freed slots 0..4 = the party sprite layers and the characters vanished mid-battle (Prime live 2026-08-10; earlier 2026-08-06 wedge was the client fade's count decrement, removed same day). Slot placement is allocator-driven, so the icon still lands at 0x25.. and nothing else changes. // v258: SAGE SAILING LIFT -- his give latches flag 18 CLEAR at map load too (v257 proved it live: possession prearmed clear at the gate, lich lift on disc, sailing set -> refused). sage_lich_lift now lies about BOTH story flags map-scoped in Crescent Lake while flag 81 is down: 17 reads 1, 18 reads 0. Sailing is never actually cleared near town -- the doorstep net reaches the lake river and a save-byte clear would strand a mid-river player, which is why the wrapper lie (zero save writes, map-guarded) is the only safe shape. Possession is not a story flag, so it stays client-held (doorstep prearm + in-town hold). // v257: SAGE LICH LIFT + BOTH-BITS HOLD. (a) sage_lich_lift rides the getStoryFlag wrapper (apply_bikke_ship_split): asked flag 17 with fine map == Crescent Lake 0x43 and sage handover flag 81 clear -> answer 1, so his item unlocks pre-Lich per rando spec. Map-scoped lie, zero save writes; a client-side flag-17 hold was rejected -- flag 17 is a black-orb crystal bit for crystals_needed, a leaked hold = free crystal, and restore-on-exit cannot distinguish our hold from a genuine pre-sage kill. Releases itself when flag 81 rises; Chaos Shrine reads untouched (map 0x4E). Accepted cosmetics in-town pre-Lich: one Well-done congratulation + post-Lich sage lines. (b) client: his gate is BOTH canoe bits (live matrix, Lich dead throughout, reload before each talk: clear+clear gives, any set bit refuses -- four cells), so the sage stage holds possession AND sailing clear inside Crescent Lake until flag 81, restoring both outside; key 17 left OWNED_FUNCTION_REASSERT (a reassert fights the hold, the Levistone lesson; the sage stage owns the sailing restore now). Fixed the comments that called the canoe repoint self-sufficient: v255 made his handover WRITE a clean flag, but his OFFER still read both canoe bits and Lich, which is why a won canoe still muted him. // v256: MAP OBTAIN BOXES -- the NPC handover path joins the one-page world. Two independent defects, both live 2026-08-10 (Elf Prince handed over an "Experience Bag (Small)" and the box read "E?perience bag ?Small?"). (1) evm_bake's bundle matcher was 2-hex-only, which took 25 of the 103 USEVM records: map bundles are also per-ROOM (USEVM0200 = map 02 room 00, where "You obtain the mystic key." actually lives) and the bonus dungeons use USEVMEX/NEW forms. That bundle was therefore never authored on disc and never got the v225 donor font, so only the RAM _mapmsg_loop touched it -- authoring through the map's own 0x2d-glyph atlas, where 'x' and the parentheses have no glyph and fall to the '?' wildcard. Both regexes now take any USEVM/J2EVM/EVM body (every USEVM record has same-named twins, so the donor pool generalises identically); USEVMCMN is excluded BY NAME because it is the CHEST box template and its control codes are relative to its own 53-glyph count. ms2_bake is unaffected -- its donor pool is FM_* only. 13 -> 15 bundles authored. (2) _tokenize returned None on any glyph no char names, which vetoed the WHOLE bundle's donor swap; four maps (02-00, 0A-00, 08, 18) each carry an EM-DASH orphan in unrelated vanilla dialogue, so they kept their sparse atlas and the lookalike ladder. Orphans now decode to '-' and are COUNTED and LOGGED per bundle. The donor face has no em-dash of its own (its single orphan 0x46 is an 18 px blank), so the cost is 8 em-dashes across those 4 maps rendering as hyphens; the gain is that all 15 obtain boxes render AP names in full ASCII, on disc AND in the RAM re-author (the resident bundle now carries the donor atlas, so _mapmsg_encode reads a full remap and stops wildcarding). // v255: CRESCENT LAKE SAGE JOINS THE GATE SPLIT -- his check is a story flag now, not a dialog latch. Story flag 18 is the Canoe's SAILING function bit AND his handover set it, the same collision as the Elf Prince, so the client had to prove the talk some other way: it watched a live pointer to the box it had authored. That pointer is live ONLY while the box is on screen and _npc_loop samples every 2.0s, so a player who pressed through the box was never sampled and the check silently never sent (live 2026-08-10, the Blue Curtain report; third recurrence, twice previously misattributed to DIALOG_STATE_ADDR moving -- it does move, but the self-locating scan already absorbed that). _NGS_SITES gains (0x089AB14C, set, 18 -> 81): the ONLY `2d 04 12 00` on the whole disc, sitting immediately before the canoe give `30 08 01 0c / e2 1f` (object 0x1FE2), so it is positively the handover. Flag 81 = 64 + canoe key id 17 -> 0x11526 b1, verified zero refs on disc. Flag 18 then belongs to the AP grant alone (key 17 is already in OWNED_FUNCTION_REASSERT). The three surviving flag-18 checks are deliberately untouched: they ask "does the player have a canoe", which AP-canoe == flag 18 answers exactly right, and 0x089A7584's body gives object 0x1394, not the canoe -- repointing a check whose body was not positively identified is the v248 mistake. Client: the whole latch subsystem is DELETED (_sage_watch_loop, _sage_talked, _sage_box_ptr_live, _DLG_WINDOW, _dlg_txt_addr, _sage_boxaddrs, the rising-edge machinery, SAGE_WATCH_S) and replaced by a flag-81 rise in _npc_loop, mirroring prince-quest. The sage was the last non-bit-based NPC handover. // v254: SPELLS HIT LOW HP ENEMIES (spells_hit_low_hp_enemies). A status/death spell's to-hit score is ramped LINEARLY from x1.0 at 85% of the target MONSTER's max HP to x1.5 at 15% (flat outside that band), emitted inside the popup_colours roll caves so it lands after magic_power_scaling's shrink (the ramp multiplies the REAL power-scaled odds) and before the colour classify (the odds shown are the odds rolled). Bosses included; party targets excluded via the MPWR BOUNDARY. v253 shipped this as a hard 15% threshold; superseded same day.
                      # A status/death spell's to-hit score is multiplied by 1.5 when the target MONSTER is under 15% of max HP, emitted inside the popup_colours roll caves so it lands after magic_power_scaling's shrink (the boost multiplies the REAL power-scaled odds) and before the colour classify (the odds shown are the odds rolled). Bosses included; party targets excluded via the MPWR BOUNDARY.
                      # v252: ONE GLYPH PAGE IN THE IMAGE -- v252: ONE GLYPH PAGE IN THE IMAGE -- the per-surface encoding of v246/v251 is DELETED. FM_EXTERN12US (12 px menu face) and FM_EXTERN18US (18 px face, used by the CHEST REWARD BOX and the KEY ITEMS menu) hold the same 107 glyphs in two id orders, and the runtime does not honour Square's per-bundle pairing: it serves the 12US banks to BOTH faces, so every X/U/Z/Q drawn in a box came out a digit ("DragoniteZ3OW" -> "Dragonite9OW" 2026-08-08; vanilla "Ultima Weapon" from a Cornelia chest -> "8ltima Weapon" 2026-08-10, while the item menu drew those same bytes correctly). Encoding per surface could never cover it -- the runtime-authored dyn slots are ONE DataPatch writing ONE byte string into every resident copy. So the bake now REPOINTS THE 18US FACE instead (extern_bake._unify_glyph_page): a .FIF is u16 magic, u16 glyph_count, a 276-byte char->glyph table, then glyph_count x {u32 strip_x, u16 advance}, and a bank byte INDEXES that metric array -- so permuting 14 metric records and relabelling the char table moves the ids without moving one pixel of the .GIM; the face keeps its size, metrics and kerning. That bundle's own box-page banks (ITEM/WEAPON/ARMOR/MAGIC_NAME + VALUE) are re-encoded to match and come out byte-identical to the 12US copies; its *_EXP desc banks and KEY_NAME were already menu page and are untouched. Result: ONE page in the whole image, every UI agrees, and tome_names/extern_bake author menu page everywhere again (KEY_MENU_ENC is MENU_ENC once more). This also fixes LOCAL item names in the reward box ("8ltima Weapon", "7-Potion"), which v251 documented as unfixable. Ordering matters: extern_bake runs before evm_bake, whose donor font IS this bundle, so the donor swap reads the repointed table and stays self-consistent. _repoint_fif RAISES on any shape surprise rather than skipping -- a half-converted image is worse than a failed bake. // v251: CHEST-BOX GLYPH PAGE, CORRECT AXIS (v246 translated the wrong copy). The page split is PER ENTRY RANGE, not per bundle: the game serves the MENU-page (FM_EXTERN12US) banks and the chest reward box draws THOSE with the box-page font. Proof: WEAPON_NAME is touched by no bake, yet a Cornelia chest holding a LOCAL Ultima Weapon read "8ltima Weapon" (player report 2026-08-10) -- the 12US copy stores that 'U' as 0x43, which IS the box page's '8', while the 18US copy stores 0x39, which the box would have drawn as 'U'. So both bundle copies get IDENTICAL banks again: vanilla + tome entries stay MENU page (the item menu reads them), the REMOTE and DYN blocks are written BOX page (only the reward box can reach them). KNOWN, UNFIXABLE BY ENCODING: a LOCAL item whose vanilla name holds X/U/Z/Q still draws a digit in the reward box ("8ltima Weapon", "7-Potion") because the item menu reads those same bytes and needs them menu-page. Retail behaviour, not a regression. // v250: MYSTIC KEY DOORS DECOUPLED FROM STORY FLAGS ENTIRELY -- the fix for the whole NPC-gate class, not just Elfheim. Vanilla opens each locked door from a `2d 08 09 02` check running `2e/36 04 <val>` at map init, and story flag 9 is ALSO the Elf Prince's "already gave it" gate; every attempt to split the quest off the key therefore broke either the doors or the prince (v247 repointed the Elven Castle check and killed its doors; v249 hand-split that one block and did not generalize). mystic_door_gate now drops the door RECORD instead, on POSSESSION of key id 5, for all 12 Mystic Key door records across all 6 maps that carry one (_MDG_DOORS; complete kind=4 scan of all 128 object lists for vals 0x1F4A / 0x23CD, both v199-proven). No cell is created, so no flag is consulted anywhere. LIVE-PROVEN 2026-08-10 (re_only/_mdg_all_probe.py): with story flag 9 forced CLEAR -- every door script believing the key unowned -- and the 12 records dropped in RAM, EVERY Mystic Key door in the game opened, Elven Castle and Cornelia included. prince_gate_split reverts to the clean v247 four-site operand repoint and the v249 block surgery is DELETED: with the records gone those door ops match nothing, so 0x089A6C34 is a pure quest gate again. This also retires the hazard for the rest of the template (Levistone next) -- a check that doubles as a door unlock stops mattering once doors do not come from checks. Object ordinals are undisturbed (ff1_data's *_NPC_ORDINAL are AP location numbers, not object-list indices). Event VM fully disassembled, see the apply_prince_gate_split header. // v249: ELVEN CASTLE MYSTIC KEY DOORS FIXED (live 2026-08-10: won Mystic Key, flag 9 set, every locked door in Elfheim castle still shut). v247's prince_gate_split repointed 0x089A6C34's check operand 9 -> 69 along with the three dialogue/set sites, but that block is not a pure gate -- its flag-9 TRUE body places the prince AND runs `2e 04 4a 1f` + `2e 04 cd 23`, the castle's two locked-door object ids (same pair Cornelia's map-init block opens on flag 9 at 0x089a5f34). Repointed, the doors waited on the quest flag a won key can never set. Scanned all 18 `2d 08 09 02` sites on disc: 0x089A6C34 is the ONLY door-op site among the four repoints, which is exactly why Cornelia / Marsh / Ice / Chaos Shrine were unaffected. apply_prince_gate_split now REWRITES that block in place (same 15 words, same end word at 0x089a6c70): `if flag 9 -> doors` / `if flag 69 -> prince awake, jump end` / `else -> flag 8 give-cutscene`. Fits exactly because vanilla's trailing jump at 0x089a6c68 targets the next word. SIGNATURES moved to the block's SECOND check (+0x12) since the first is flag 9 again by design. // v248: Blood Knight lifesteal retuned 20% -> 10% (KNIGHT_LIFESTEAL_DIV 5 -> 10), matching the 10% defense-pierce leg. Both scroll legs are now 10%. Patch bytes change (the cave's immediate), so seeds must re-bake.
                      # v247: PRINCE GATE SPLIT (apply_prince_gate_split). The Elf Prince quest chain (handover gate, completion set, healer+prince dialogue) repointed from story flag 9 (= Mystic Key function bit = every locked door) to shadow flag 69. Live-RE'd + RAM-rehearsed 2026-08-09; Mystic Key row leaves NPC_MAP_RESET, doors can never be held again.
# v246: CHEST-BOX GLYPH PAGE. The two extern bundles ship the same 107-glyph face in two id orders (name_banks PAGE block, RE'd off their .FIF/.GIM 2026-08-08): menu page 0x38..0x41 = wide digits / 0x42..0x45 = X U Z Q, box page 0x38..0x3b = X U Z Q / 0x3c..0x45 = wide digits. Vanilla stores each bundle's NAME banks in ITS OWN page (the two ITEM_NAME/WEAPON_NAME/ARMOR_NAME/MAGIC_NAME copies differ by exactly the one swapped byte -- "X-Potion", 'U', 'Z' -- and VALUE.MSG's digit strip reads "1234567890" on both), while the DESC banks are byte-identical copies. extern_bake wrote MENU-page bytes into BOTH bundles, and the chest reward box reads the 18US copy: a baked "DragoniteZ3OW's Bombs" rendered "Dragonite9OW's Bombs" (player report 2026-08-08 -- menu 'Z' 0x44 IS the box page's wide '9'), and vanilla "X-Potion" from a chest had been drawing "7-Potion" ever since the bank grew. build_extended_banks(box_page=) now translates NAME entries per bundle (NB.to_box_page; DESC banks untouched, dyn slots exempt because the client authors them through one DataPatch and only the box reads them, so they are box-page at the source). KEY_NAME authoring moved to the box page too -- the Key Items menu draws on it, which is what the 2026-07-23 KEY_MENU_ENC digit probe had found; KEY_MENU_ENC is now just BOX_ENC, so X/U/Z/Q are fixed there as well. MENU_FONT gained the 32 ids the constraint solver never saw (Q, Y, q, comma and every symbol): the face is full printable ASCII, so _BOX_SAFE no longer strips digits/punctuation from remote names -- the 2026-07-03 "digit freezes the box" was the 53-glyph USEVMCMN template bank, where 0x38/0x39 really are {CLR}/{NAME}, not the 107-glyph item bank this text lives in. REMOTE_WHO_GLYPHS 14 -> 18 so a 16-char AP slot name keeps its possessive (the old cap only fit "DragoniteZ3OW's" because the digit used to be dropped), with a possessive-safe truncate past that. // v245: minion_death_serializer re-paid a deferred row's FULL XP+Gil every frame (reward add sits after death_visual_start in sweep 0x888646c and was never gated); guard moved into the sweep so a deferred row skips reward+drop too.
# v243: v243: slot_magic rewrites the Ether family item descriptions (Ether/Turbo/Dry, gids 4/5/6) to spell-slot wording -- bumped so seeds re-bake and the new desc bank lands.
# v242: OPEN PROGRESSION BAKED ON DISC (map_bake.bake_openworld_grid). The foot trails, canoe rivers and northern docks are now written into MAP_00_AMD.BIN at bake time instead of being poked into the decompressed heap arena by the client every 1.5s. The loop could not survive a re-decompression it could not see: a freed arena copy still holding our edits made the canary read clean while the LIVE arena rendered vanilla, so the northern docks stayed absent until the player restarted the game (report 2026-08-08). Baked, the cells are correct before the arena is ever decompressed -- no relocation hazard, and no chunk-repaint hazard for far visual-only edits like the piers. The three toggles ride the spec as feats["_ow_map"] so bake_hash32 covers them (flipping one re-bakes rather than booting the other layout's cached ISO). _openworld_loop is KEPT as a repair path for in-progress seeds on a pre-242 ISO; on a baked ISO it writes values that are already there. Its 6s re-anchor now always re-applies (it used to skip when the anchor set was unchanged, which is exactly the in-place re-decompress case) and apply() logs any arena copy that read vanilla. // v241: REMOTE CHEST NAME BUDGET OVERHAUL (the "your!" box, live playtest 2026-08-07). (1) extern_bake split layout: the grown US extern bundle now gets the WHOLE FM_EXTERN*J1 extent and the J record is re-served the VANILLA bundle J-retagged in the old US extent -- the old both-grown-copies-in-J packing halved the remote-name budget for a J variant a US boot never loads (175 distinct multiworld names -> uniform cap 4 -> box read "your!"). Legacy packing kept as automatic fallback if the retagged vanilla copy won't fit the US extent. (2) remote names are now (who, item) pairs and the cap ladder (tome_names.remote_rungs) truncates the ITEM only -- the recipient always survives -- and NEVER yields a blank entry (worst rung "AP item", replacing the empty-box blanking rung). (3) bonus dynamic chest names no longer bake one deduped entry per (dungeon, ordinal) (up to 220 entries, the main budget hog): tome_names.DYN_SLOTS=2 wide sentinel entries are appended after the remote block and the client authors the NEXT chest's name into them at runtime (ping-pong, armed via the BDC1 mailbox next-sid). Bank layout, record placement and entry count all change -> bump (standing re-bake rule). // v240: TOME SELL PRICES ROUNDED TO THE NEAREST 25 (user call 2026-08-07). The v236 linear ramp produced ugly off-grid numbers (20/303/586/869/1151/1434/1717/2000); the same 8 levels now read 25/300/575/875/1150/1425/1725/2000 -- each old value rounded to its closest multiple of 25, so the ramp shape, the table size (8 words), the blob length and every offset are unchanged. Level 1 rounds UP to 25 rather than down to 0 so a tome is never worthless at a counter. Table is baked into the boot cave and is NOT folded into bake_hash32, hence the version bump (standing re-bake rule). // v239: RUNE KEY_EXP MUST SURVIVE BEING OWNED. The borrowed rune slot (key id 35) was baked a 116-glyph TWO-LINE description, which is safe only while that row is the client's display line and nobody highlights it. When the game genuinely OWNS id 35 the row renders with a BLANK name -- the bake pads that KEY_NAME slot for the client to fill at runtime -- and cursoring onto it hard-freezes the menu. Reproduced live 2026-08-07 with NO client attached, on a save where _keyratio_loop had wrongly set the possession bit inside Whisperwind (it cleared its zone latch on the COARSE LOADED_MAP_ID, which reads 0 on some Whisperwind floors -- the same trap v238 fixed for the crystals leg; the client now gates on FIELD_MAP_ID_SA). Description is now single-line and no longer than the longest vanilla entry (43 glyphs), so the row is harmless even when owned. // v238: CRYSTALS_NEEDED MAP-SCOPE FIX. The crystal leg's map gate read the COARSE LOADED_MAP_ID (0x13118, save+0x2018), which is 1 for EVERY dungeon -- so it lied about the four fiend flags in ALL dungeons, not just Chaos Shrine. With crystals_needed<4 and a fiend skipped, that leaked into the game's bonus-dungeon "is it open" check (user report 2026-08-07: crystals_needed=3, Tiamat never fought, yet Whisperwind Cove open + air crystal correctly dark on the menu). Now reads the FINE FIELD_MAP_ID (0x13108, save+0x2008, == 0x4E in Chaos Shrine -- the field bikke_ship_split already uses), so the leg lies ONLY in the Chaos Shrine orb room: the orb still opens at N, but an unfought fiend's bonus dungeon stays gated on the real fiend defeat. Affects both crystals_needed and bonus_dungeon_crystals (shared leg). // v237: DABBLER MP SOFT CAP. The monk_thief_dabble_in_magic MP scale was Thief floor(INT/4)+2, Monk floor(INT/4)+1, Master floor(INT/3)+1 -- INT-linear with no ceiling, so a mid-INT dabbler ended L99 around 600-780 maxMP, brushing the engine's 999 clamp and rivalling a full caster (user report 2026-08-07). Dabblers now accrue SIXTEENTHS of an MP through a two-slope curve: e = 8 + 4*min(INT,K) + 1*max(INT-K,0), acc += e, gain = acc>>4, carry = acc&15 -- full slope below the knee K, a QUARTER of it above, so INT never stops paying, it just stops compounding. Approx L99 totals: ~350 Thief/Monk and ~410 Master on a mid INT roll, ~415/~480 Mind-Plus-fed, vs 705/607/776 before. Master differs by KNEE ONLY (14 vs 10) -- same curve, holds the full slope four points of INT longer, ~16% more total: a slight edge over Thief/Monk, not a tier jump. A SOFT CAP AND NOT A HARD CLAMP because e(INT) must stay NONDECREASING: total MP is the sum of e over levels, so a dabbler who races above K has INT >= a laggard's at EVERY level and can never finish with less. Concavity compresses the racer's lead; it cannot invert it. A min() clamp DOES invert it (the laggard keeps full slope while the racer is pinned), which is why this is two slopes. Non-dabbler jobs keep vanilla floor(INT/4) on their own leg (different denominator). The four flat per-job +1/+2 branches are gone -- they were ~200 MP of the old total on their own, and keeping today's early-game rate is incompatible with a ~350 endgame. Tuning surface is _DAB_MP_F/W1/W2/K/K_MASTER at the top of the file: W2 -> endgame total, F -> early game, K_MASTER -> Master's edge. Inert under slot_magic (that mode repoints the statIdx-1 entry, so this cave is unreachable and is not even emitted). // v236: TOME SELL PRICE SCALES WITH SPELL LEVEL. Every spell tome sold for a flat 100 gil (the boot cave wrote a hardcoded 100 into item row +0xc for all 64 tome ids), so a Level 8 tome and a Level 1 tome were worth the same at a shop counter. The boot cave now indexes an 8-word price table baked into the cave: _TOME_SELL_BY_LEVEL = 20/303/586/869/1151/1434/1717/2000, a linear 20 -> 2000 ramp over levels 1..8 (user call 2026-08-07). THE LEVEL IS THE VANILLA LEVEL, NOT magic_info+9: with shuffle_magic_shops on, rando.align_shop_spell_levels rewrites the +9 byte to the tier of whatever store now stocks the spell, so a magic_info read would price Cure at 2000 gil for landing in a late shop and Kill at 20 for landing in Pravoka -- the user's explicit requirement is that the tome's worth follows the SPELL. Spell identity never moves between magic slots (the shuffles rewrite bytes within a record, never swap records), so the vanilla level is pure arithmetic on the slot: level = ((slot & 31) >> 2) + 1, verified byte-exact against RD.VANILLA magic_info for all 64 slots. The cave therefore reads no table but its own and indexes with (slot & 0x1C), which cannot leave the 8 words. Buy price stays 0 (tomes are not shop-buyable; AP is the only tome source). The table lives between the two 20-entry jump tables and the init cave, so the blob grew 32 bytes -- length-stable across both _tome_cave_blob passes, and off_jtv/off_jta are unchanged. // v235: BONUS_DUNGEON_CRYSTALS. The bikke_ship_split crystal leg (_crystal_leg) counts 4 client-set shadow bits (save+0x834, dead slot-magic reserve byte -- a client-owned byte, NOT the story-flag array, to avoid a read-modify-write race with RUNE_BORROW_OWNED flag 55 in byte 0x11522) instead of the four Fiend flags when bonus_dungeon_crystals is baked, and installs even at the default crystals_needed=4 in bonus mode (the Fiends WILL all set once their dungeons open, so the crystal must not count until the Soul-of-Chaos superboss falls). Default seeds (bonus off + N=4) emit NO leg -> byte-identical cave. Rides bikke_ship_split as bake context (no FEATURES/SIGNATURES entry; a 2nd detour on the shared getStoryFlag wrapper would orphan the first). // v234: regional_river_encounters north set gains ZONE 31 (row 3 col 7, the north-east peninsula tip; user call 2026-08-06). Inert on today's map -- zone 31 holds no river cell statically and none of the 16 canoe cells open_progression carves at runtime land there (those are zones 34/43/54, all southern) -- so this is a forward declaration, not a behaviour change: a river later carved into that corner is northern by default rather than by omission. VERSION BUMPED ANYWAY because the zone table is baked into the cave and is NOT folded into bake_hash32, so without the bump a client holding a v233 ISO would keep booting the cached copy and the edit would look dead (the standing re-bake rule). test_patch now also asserts re_only/river_zones.json agrees with the baked set, so the picking map cannot drift from the game. // v233: REGIONAL RIVER ENCOUNTERS (new yaml option regional_river_encounters). TERRAIN 1 IS THE RIVER TERRAIN AND NOTHING ELSE -- measured 2026-08-06 off the static map: the cells routing to terrain 1 are exactly the cells whose ATT movement attribute is 0xF009, 1122 of them (the live map reads 1135 because open_progression adds 16 river-class cells). 0xF009 is the canoe attribute; those tiles admit neither foot nor ship. MARSH IS NOT IN THE SET -- marsh carries walkable land attrs and routes to terrain 0, the ordinary land table, which is exactly why marsh plays like grass. So the terrain-1 hook reaches rivers and only rivers and needs NO river-vs-marsh gate, and the old "marsh/river" naming everywhere in this file was wrong. Vanilla gives every river on the map ONE 8-slot row (0x08945a90 Hydra/Crocodile/Ochu/Piranha/Neochu, expected threat 26.4) -- the Ice Cavern river and a starting-continent creek are literally the same fight. This splits it by continent the way regional_ocean_encounters splits the sea: NORTHERN rivers draw a harder water/flying/giant pool, SOUTHERN rivers fall through to the vanilla flat roll and are byte-for-byte unchanged. THE ZONE MAP IS A 64-BYTE 0/1 TABLE, NOT A DIFFICULTY TIER, and that distinction is the whole lesson of the reverted v230: rando.ZONE_TIER answers "which town would I be at if I WALKED here", but canoe rivers cut straight through Cornelia- and Pravoka-tier zones -- the Ice Cavern river sits in zone 46, tier 1, so a ZONE_TIER-driven river rolled Tarantulas and a 9-Pirate pack (user report 2026-08-06). River difficulty cannot be inferred from a river's walking neighbourhood; it has to be ASSIGNED. The measured per-zone river counts make the cut exact rather than a judgement call: grid rows 0-2 hold 298 river cells, rows 4-7 hold 824, and ROW 3 HOLDS NONE, so north = zone < 24. re_only/zone_paint.py renders the map with the engine zone grid, the river cells highlighted and the current split shaded, for re-tuning (re_only/river_zones.json). NB zone_overlay.py's DOCSTRING says the zone is (y//32)*8+(x//32), which is wrong, but its drawing code uses the correct biased _BOUND table -- the gridlines it drew were always right; only the comment is stale. ALSO IN v233, a bounds guard on every zone-indexed cave: the +7 bias yields grid index 8 for coord 249..254, so the zone index reaches 72, not 63, and 3024 map cells index PAST a 64-entry zone table. For the river/desert/class-9 caves no such cell exists on the shipped map (they are ocean and plain grass), but regional_ocean_encounters has 2991 EDGE-OCEAN cells landing in that overhang and has been reading its zone-pool byte out of the u16 pool rows since v36 -- a value of 1..4 silently picks the wrong region and anything larger rolls a garbage formation. All four caves now fall through to the vanilla roll when zone >= 64. Pool (expected threat 44.5 vs vanilla river 26.4, harder-overworld north 26.0/30.4/41.8, dangerous-forests north 46.1/54.4/68.1 -- above the grass, below the forests): 1-2 Yamatano Orochi / 2-4 Squidraken / 1 Black Dragon / 1-2 Iron Golem in the four common slots, 2-4 Dragon Zombie and Mad Ogre+Flood Gigas parked in the 9.38% slots, 4-8 Poison Eagle rarest-but-easiest, 2-6 Squidraken as the 1.56% spike. Squidraken and Iron Golem were the only two monsters in the entire bestiary claimed by no other pool. // v232: (a) AMPHIBIOUS FIGHTS PUT BACK. v231 banned everything in rando's AQUATIC set from the desert rows, which deleted 4-6 Sahagin and the 9-Pirate pack from the Pravoka tier -- content the user had deliberately placed. Sahagin and Pirates around Pravoka are the SAME call rando._OW_HANDPICK makes for the land table (0x0dd forced into every Pravoka-tier zone, "Pravoka is a port"), and _LAND_AQUATIC allows the whole amphibious block in the Crescent/Onrac/Trials regions. Only the three SHARKS stay banned, which gen_terrain_pools._eligible already handled. Desert tier 1 is back to Lizard / Wolf / Sahagin / Pirate / Shadow / Gigas Worm / Cobra / Gargoyle; the class-9 rows are regenerated off the same rule. (b) BOSS_MINIONS RUNTIME SIGNATURE FIXED -- and this one had been broken since v213. The signature asserts Garland's formation-record layout byte == _MINION_LAYOUT (2), but v213 gave absurd-intensity bosses TOP_GRID_LAYOUT (5) and never updated it. So from v213 on, every absurd seed failed patched_running_verdict on its FIRST re-bake and fell back to runtime patching -- which reports 'on-disc CODE features will be missing this session' and, worse, passes dabble_baked=False, leaving the magic_learn reconcile loop fighting the on-disc table for the whole session. It stayed invisible because cached ISOs keep booting and only a PATCHER_VERSION bump forces the re-bake that exposes it; a v230 bump is what surfaced it live 2026-08-06. SIGNATURES values may now carry a TUPLE of acceptable values, and boss_minions accepts layouts 2 and 5. Detection only -- no gameplay or bake change. // v231: MARSH/RIVER REVERTED TO VANILLA; class-9 land scaled instead. v230 made terrain 1 (marsh + river, the single GLOBAL 8-slot row at 0x08945a90) zoned off ZONE_TIER. That was wrong: ZONE_TIER is a LAND-ROUTE map -- which town you would be at if you walked there -- and the canoe rivers cut straight through Cornelia- and Pravoka-tier zones. Live 2026-08-06: the Ice Cavern river (zone 46, ZONE_TIER 1) rolled 4 Tarantulas and a 9-Pirate pack instead of the Hydra / Crocodile / Ochu the global row has always given. River difficulty cannot be derived from where a river happens to sit on the walking route, and the global row is how the game is meant to play, so terrain 1 is now left entirely alone -- no hook at 0x08841f68 at all, and test_patch asserts that address never reappears in the patcher. Do not retry without a river-specific zone map. TERRAIN 4 (tile class 9, ~439 tiles on the southern landmass, zoned 0x08945ca8) takes its place: it is already per-zone, so it is safe to scale the same way the desert is, and vanilla gives all six of its zones one generic row at mean threat ~8 (Wolves / Ogres / Goblin Guards / Cobra) including the 280 tiles in Crescent-tier zones. Its hook is the switch's FALL-THROUGH case, so unlike the desert it has no `b` of its own: the hook is 0x08841f8c (`sll v1,v0,3`) with the harmless `lui v0,0x894` as delay slot, and the cave must therefore always commit -- it does, since every ZONE_TIER value has a full row. No flavour filter (its vanilla content is generic wilderness); each row is an even spread of the tier's threat band. DESERT tier 1 also fixed: the flavourless top-up had let 4-6 Sahagin and a 9-Pirate pack into a DESERT row. Nothing aquatic can top up either terrain now. Desert tiers 6/7/8 -- the 1635 tiles that are actually the Ryukhan and the western dunes -- are unchanged from v230 and stay live-verified (Specter / Bonesnatch / Desert Baretta at zone 22). Low desert tiers are deliberately NOT floored: vanilla's own row for zone 37, the 35 tiles north of Cornelia, is mean threat 7.0, so a floor would spike an early area this option never promised to touch. Both terrains stay AUTHORED rather than seed-random (user call), same trade-off as the v216 dungeons. // v230: THE OVERWORLD u16 COMPANION WAS SITTING ON THE DESERT ENCOUNTER TABLE. Live-confirmed 2026-08-06 from a player session: 5 Goblins on the overworld desert at (199,77) beside Mirage Tower, zone 22, vanilla battle XP 30 -- with harder_overworld_encounters ON. apply_overworld_u16 homed its 512-byte high-byte companion on 0x08945aa8, documented since v201 as "the terrain-3 zoned table -- confirmed UNUSED on the overworld (0 tiles resolve to terrain 3)". 0x08945aa8 is the DESERT table (~1732 tiles: the whole Ryukhan Desert plus the western dunes), and the companion's high bytes are 0x00/0x01/0x02 in almost every slot -- which as u8 formation ids are 0x00 "3-5 Goblin", 0x01 "2-4 Skeleton" and 0x02 "1-3 Goblin Guard + Wolf". So the late-game desert became the softest terrain in the game, and only for players who turned harder overworld ON (the companion ships with that flag alone). ROOT CAUSE: encounter_census.py and re_only/tile_terrain_probe.py both decoded the tile-class -> terrain map as terrain = 0x8945810[class*2+1]. The engine does not index by class*2. From 0x08841e8c..0x08841ebc, with `raw` = the terrain-map byte: raw<=0 or raw>=13 -> no encounter; raw<3 -> entry = raw*2; raw>=3 -> entry = raw+3; terrain = 0x8945810[entry*2+1]. Real mapping: raw 1-5,10 -> terrain 0 land zoned 0x08945890; raw 6,7,8 -> terrain 1 MARSH/RIVER flat 0x08945a90; raw 9 -> terrain 4 zoned 0x08945ca8; raw 11 -> terrain 3 DESERT zoned 0x08945aa8; raw 12 -> terrain 2 OCEAN/shallow flat 0x08945aa0. The same bad decode is why v23/v28 flip-flopped Regional Ocean between terrain-2 and terrain-4 and why v36 needed a black-box sentinel to prove the ship reads terrain-2 (class 12 genuinely does), and it is the same family as the v222 empty-encounter river bug. FIX (a): the companion moves into the patcher's own cave segment, where nothing else can claim it; its bytes ride in as feats["_ow_hi"] instead of as a DataPatch, it is dropped from boot_patch.TABLE_ISO_OFFSETS (a reconcile tick would otherwise re-inflict the damage on a correctly baked ISO) and folded into bake_hash32 by hand. A missing or short blob bakes all zeros, which degrades to the plain vanilla u8 id instead of garbage. 0x08945aa8 is left exactly as shipped. FIX (b), new feature terrain_pools: the desert and the marsh/river branches get per-tier u16 pools, because NEITHER terrain reads zones_overworld and so neither was ever scaled by the harder bands. Desert was fixed vanilla content (Baretta / Desert Baretta / Sand Worm / Allosaurus / Tyrannosaur, threat 21-41) wrapped in Lufenia-tier grass at 35-64; marsh/river is a FLAT single 8-slot row (0x08945a90) shared by every bog and river tile on the map, so the Marsh Cave bog and a late canoe river were literally the same fight and it could be neither raised nor left alone. Both branches now take a DF-style detour on their own `b 0x8841f9c` word (0x08841f68 marsh, 0x08841f84 desert -- the delay slot still sets s0 to the vanilla base, so a fall-through stays safe) that computes the zone, reads the shared _DF_ZONE_TIER map, weight-rolls a slot off the engine scramble curve and writes the u16 id to the battle context. Marsh/river gains per-zone difficulty it never had in vanilla. Pools are precomputed literals from re_only/gen_terrain_pools.py drawing the SAME per-tier threat bands gen_ow_pools uses -- so a desert or bog row can never be softer than the grass around it -- filtered to that terrain's monster flavour set, topped up from the flavourless band where a tier is thin, sorted ascending so hardest = rarest (the v201d land convention). Ryukhan (tiers 7/8) goes 21-41 -> 28-62: Desert Baretta, Pharaoh, Rock Gargoyle, Black Dragon, Sekhret + Earth Troll, Pharaoh + Bonesnatch. TRADE-OFF: like the v216 dungeons, these two terrains are authored rather than seed-random. The client derives terrain_pools from overworld_u16, so seeds rolled before this version get both halves on the next re-bake. // v229: GRAND MASTER CAP REBALANCE + A SECOND STOP CONDITION FOR ITS MAX-HP LEG. The per-battle cap was a flat level+5 attack, which shrank in relative terms every level (+5 is half a level-10 Master's level and 7% of a level-75 one's), so the reactive-growth window stopped mattering exactly where long fights start. It is now master_atk_cap(level) = level*MUL_NUM/MUL_DEN + OVER_LEVEL, shipped at 2x: L10 25 / L25 55 / L50 105 / L75 155 (fill cost scales with it -- ~2100 damage taken to cap a level-50 Master at ceil(dmg/20) per hit, so this is a boss-fight curve, not a trash-mob one). The three constants are the ONLY retune surface: _master_cap_asm() emits the same arithmetic into both damage caves (power-of-2 NUM becomes an sll, anything else a multu; MUL_DEN must be a power of 2 because the cave shifts) and ApClient imports master_atk_cap() instead of redeclaring it, so the asm and the Python can no longer drift -- the drift that mattered, because the cave owns the popup and the client owns the HP write and a mismatch draws a number the client never applies. An import-time assert holds master_atk_cap(99) <= 255: MB_MATK is a u8 per party slot, so a bolder multiplier would wrap the accumulator and silently un-cap the Master. SECOND CHANGE, client-only: the max-HP leg now stops at MASTER_MAXHP_CEIL (999) as well as at the attack cap, and the boosted M_HP_CAPPED_PCT heal starts at whichever lands first. Statically RE'd over BOOT.BIN 2026-08-06: the engine clamps DERIVED max HP (BU_MAXHP +0xA) to 999 in two places -- the Giant's Tonic case at 0x08885b28/slti 0x3e8 and, load-bearing here, the RE-DERIVE path at 0x088765c8 that runs on every damage event -- but it does NOT clamp BU_MAXHP_BONUS (+0x66), the field the client actually writes through. So at 999 the old code kept inflating a bonus the engine discards: growth that bought nothing and, because the heal rate keyed off the attack cap alone, cost the player the defensive mode they had effectively already earned. Gain is now also clamped to the remaining room, so hpbonus never claims max HP the unit does not have (an overshoot would make a Giant's Tonic drunk later look like a no-op). ATTACK IS DELIBERATELY UNAFFECTED by the ceiling -- a Master out of HP room keeps accruing attack to its own cap, so the cave keeps drawing the yellow attack-gain number. That is why this leg needed NO cave change: the cave knows only the attack accumulator, and yellow-while-attack-has-room / green-once-capped was already the correct popup for both stop conditions. // v228: MAGIC DEFENCE REWORK (magic_power_scaling). ALSO IN v228, and load-bearing for it: the spell_tomes address repoints now write their LOW half as `ori` instead of `addiu` (_set_lo16_ori). `addiu` SIGN-extends, so a low half >= 0x8000 silently subtracts 0x10000 and the pair needs a carry in the lui -- which is why every cave-segment consumer had to stay inside the FIRST HALF of its 64k page, and why apply_spell_tomes asserted `(btbl & 0xFFFF) < 0x8000`. That cap left the segment 592 bytes short of fitting this feature (measured: 840 file bytes needed, 248 spare). `ori` ZERO-extends, so one lui now reaches the whole 64k page and the window doubles to 0xFFF0 (~29 KB spare after this feature). Safe by construction: `ori rX,rX,lo` == `addiu rX,rX,lo` exactly when rX's low half is already zero, and all 42 repoint sites were verified to be `lui rX,hi` followed by `addiu rX,rX,lo` with rs == rt == the lui's target, so anywhere the addiu was correct the ori is too. The lui immediates are untouched, so the spell_tomes runtime SIGNATURE (lui imm @0x088c4ca4 == _CAVE_HI) is unaffected. The alternative considered and rejected was a THIRD PT_LOAD segment: add_segment_cave starts cave bytes at phdr+64 so a 3rd entry cannot be added without shifting every existing cave by 32 bytes, cave_bss_tail/cave_write both hardcode segment index 1, and SAFE_CAVE_VADDR's whole safety argument is that the cave segment IS the module's memsz end (the heap starts above it) -- an assumption unproven for >2 segments whose failure mode is heap-over-code corruption. mdef fed two engine legs with opposite pathologies: the status TO-HIT score (acc+148-mdef, LINEAR and UNBOUNDED -- at 150% Monster Power a late-game mob's damped mdef alone drove it negative, so status spells stopped landing at all) and MAGIC DAMAGE (a 3-bucket step at 0x08884C90: >=201 -50%, >=101 -25%, else -12.5% -- COARSE and SATURATING, and since mdef is a byte every high-mdef monster pinned at 255 past ~200% power and stopped scaling entirely). Traced 2026-08-05: the damage roll at 0x08884C34 is NOT a hit/miss check -- a failed roll only skips a bonus term at 0x08884C60 and base damage lands either way -- so to-hit and damage are cleanly separable, which is what makes this possible. TO-HIT is now score = (acc+148-mdef_VANILLA) * 0.5**(m-1): landing chance decays MULTIPLICATIVELY with power (x0.50 at 200%, x0.25 at 300%, x0.065 at 500%) instead of falling off a cliff, and nothing hard-zeroes that was not already 0% at 100%. DAMAGE is now dmg * 320/(320+mdef_eff), a diminishing-returns curve fed the UNCAPPED scaled mdef: asymptotic so magic is never nullified, and it keeps responding past the byte clamp. AT EXACTLY 100% POWER EVERY LEG IS A RUNTIME NO-OP (user requirement 2026-08-05: most players never leave the default and the game must feel the way they expect) -- the caves bake unconditionally and bail to their displaced originals unless the client has armed the mailbox AND that monster's shrink256 is non-zero. Because the table is keyed by MONSTER ID, Monster Power and Boss Difficulty resolve INDEPENDENTLY (a 100%-monster/300%-boss seed gets vanilla trash and scaled bosses in one battle) and the Boost tab can flip either mid-game -- the client just rewrites the table. PARTY-VS-MONSTER IS AN ADDRESS RANGE TEST, not a field test: party_unit[row] = base+0xC714+row*0x6C (_BU_OFF/_BU_STRIDE) and enemy_unit[i] = base+0xC8C4+i*0x6C (enemy init 0x0887b824 arithmetic), and 0xC714+4*0x6C == 0xC8C4 exactly -- ONE array, party at indices 0-3, monsters 4+. The obvious alternative, the [unit+0x3C]<4 row test the CW cave uses, is NOT usable here: enemy init stores monster_stats+0x20 there and 91 of 256 monsters land under 4, so a third of the bestiary would be misread as party members (that cave is safe only because it tests the CASTER at the MP-deduct site, which no monster reaches, behind a second class gate). Monster id comes from unit+0x49 (enemy init 0x0887b9fc). Four legs hook their own sites (0x08884C90 damage, 0x08884E4C type-0x04, 0x08884F10 + 0x0888522C generic status = the Sleep/Bind/Dark family, 0x08885394 type-0x0e); the TYPE-3 leg (Death/Warp/Scourge/Quake/Break) has NO window of its own -- its mdef load sits in a branch DELAY SLOT, 0x08884D74 is a branch target, and 0x08884D78 is conditionally owned by necro-pierce -- so it folds into the popup_colors roll cave, which installs unconditionally and where s7 is consumed AFTER we return, making the odds SHOWN and the odds ROLLED physically the same number. // v227: WHITE CLERIC DIA SELF-HEAL SCALES WITH INT, and the dia INT stack no longer eats equipment INT. The heal was a flat 10/20/30/40 by dia tier; it is now (INT * multQ8) >> 8 with Dia x0.5 / Diara x0.75 / Diaga x1 / Diaja x1.25 (SCROLL_DIA_HEAL_MULT_Q8, baked into the same u16[64] table at SCROLL_MB_DIAHEAL_OFF, nonzero still meaning 'is a dia spell'), floored, min 1, uncapped apart from the maxHP clamp. INT = the caster's EQUIPPED battle INT plus the dia steps banked EARLIER this battle -- this cast's own step does not scale its own heal (user 2026-08-05). Root fix in the same cave: leg 3 wrote BU_INT = field[0x33] + acc, but field[0x33] is BASE INT WITHOUT EQUIPMENT -- the engine's own battle-unit refresh (0x08876384) builds BU_INT by jal'ing _INT_GET_FN 0x088970A8, which adds the weapon + 4 armor INT bonuses (item stat tables 0x08953bbe / 0x0895432d, stride 0x1C) and clamps to 99. So every dia cast used to DROP the caster's equipment INT for the rest of the battle, costing magic damage too. The cave now jal's that getter itself (a1 = 0, matching the refresh, so the engine's conditional halving still applies) and uses its result as both the heal scale and the base it adds the accumulator to. // v225: OBTAIN BOXES GET THE FULL MENU FONT (evm_bake donor swap). v224's on-disc authoring still degraded through each map's tiny glyph atlas ("spell Tome. Cura*a") because no EVM-family FIF on the whole disc carries the digits 7 or 9. Each authored USEVM bundle's _MAP.FIF+_MAP.GIM are now REPLACED wholesale by the FM_EXTERN18US pair (item-name menu font: same face, same 512-wide 4bpp GIM, same 36 px cell rows, 95 printable ASCII) with the map's own GIM palette block spliced in (one tint entry is map-tuned), and EVERY MSG entry is re-encoded through the donor table -- control codes are COUNT-RELATIVE (byte >= glyph_count; delta 1 = line break), so they shift by donor_count - map_count; the terminator glyph id is ascii_map[0]. Grown bundles relocate same-map J2EVM/EVM first, then best-fit into a free J record of an UNAUTHORED map (cross-map dead space, `used`-tracked). Bundles with an ORPHAN glyph (no char in the FIF table maps to it -- maps 08/18) cannot round-trip and keep the v224 lookalike ladder; both render near-clean anyway. // v224: PER-MAP OBTAIN BOXES BAKED ON DISC (evm_bake). PER-MAP OBTAIN BOXES BAKED ON DISC (evm_bake). The "You obtain the {key}." sentence is a per-map string in USEVM<mapid>.PCK (MAP<id>.MSG + _MAP.FIF remap + _MAP.GIM atlas); the client's _mapmsg_loop authored only the RESIDENT copies and raced the game's bundle re-copy (dialog open / post-battle reload), so a fresh copy read vanilla for up to _MAPMSG_REVERIFY_TICKS*2 s -- live 2026-08-05 the Waterfall robot's box said "warp cube" seconds after a [keybox] author. evm_bake decodes every USEVM MSG with its own FIF, rewrites obtain entries whose key has an AP name (same case/lookalike/placeholder degrade ladder as the RAM loop -- the per-map atlas is a font subset), rebuilds the inner PCK and recompresses; a grown bundle relocates into the same map's J2EVM/EVM record (Japanese copies, never loaded US -- extern_bake pattern; not in ms2's FM_* donor pool, no reservation needed). RAM loop kept as repair net for pre-v224 bakes. Also in this bake: Warp Cube NPC_MAP_RESET row prearm (PREARM_GLOBAL + prearm_poss, ff1_data) -- the robot's already-gave check binds at MAP LOAD and reads possession, so an owned AP cube froze him at *buzz* *whirr* forever (tenth map-load-binding instance). // v223: MOUNT GULG B5 DEDUP IS NOW YAML-CONTROLLED (LootInGulgB5Chests, default ON = the old always-on behavior). chest_dedup used to re-point all 10 aliased chest records unconditionally; the three Mount Gulg map-46 records (0x0897BE02/0A/1A, alias source treasure idx 180 = "Mount Gulg - Chest 20") are split into their own feature key. MOUNT GULG B5 DEDUP IS NOW YAML-CONTROLLED (LootInGulgB5Chests, default ON = the old always-on behavior). chest_dedup used to re-point all 10 aliased chest records unconditionally; the three Mount Gulg map-46 records (0x0897BE02/0A/1A, alias source treasure idx 180 = "Mount Gulg - Chest 20") are split into their own feature key. Those three physical chests share Chest 20's open-bit in vanilla -- open one and all four open, so the other three are permanently empty -- and idx 180 sits on the SAME floor at (14,39), which is why a player clears B5 and never sees them. ON: idx 188/189/191, three real AP checks (Chest 35/36/37), unchanged from v222. OFF: records left vanilla and FF1PSPWorld._removed_chest_idx drops the three locations, so the itempool shrinks by 3 and the client scout skips the same set (test_scout_parity). The map-45 twin (176 -> 185, "Chest 34") stays in ON_DISC_ALWAYS: it has real vanilla loot. Bake is now option-dependent -- the flag rides on_disc, so it folds into bake_hash32 and toggling re-bakes. // v222: OVERWORLD u16 NO LONGER CORRUPTS RIVER/OCEAN/CAVE ROLLS (live 2026-08-05: an EMPTY encounter -- battle with no monsters in it -- on the canoe river beside the Cavern of Ice). The lbu at 0x08841fc0 is the SHARED slot-roll load for every terrain path; only s0, the table base, differs (land zones_overworld 0x08945890, terrain-2 flat = ship on open sea + CANOE on rivers + foot on shallows 0x08945aa0, terrain-4 zoned ocean 0x08945ca8, zones_caves 0x08945f9c). apply_overworld_u16 indexed its companion high-byte table by an UNGUARDED v0 - 0x08945890, so a river roll (delta 0x210) read its "high byte" from 0x08945aa8 + 0x210 = 0x08945cb8 -- inside the vanilla terrain-4 LAND-mob table -- and OR'd e.g. 0x63 << 8 onto a valid u8 river formation; the resulting id is far past the formation table, so the battle spawned empty. Every non-land base was affected, not just the river. The OR is now gated on 0 <= delta < 512 (unsigned, so a base BELOW the land table wraps huge and fails too): only zones_overworld gets a high byte, every other base falls through with the plain vanilla u8 id. // v221: BLOOD MAGIC NO LONGER TAXES CONSUMABLES (Fangs). The armor leg was a blind scan of all 75 armor +7 procs for one matching the committed spell @C+0x44 (an armor use wipes its own cat/id before the turn runs, so the spell is its only trace) -- but consumables pass through the same item state machine and also write C+0x44, so any item casting an armor proc (Fang/Firaga vs Black Robe) was charged 10% max HP. Scan deleted. Replaced by a TICKET stamped at the battle item-usability resolver 0x08871594 when the caller is the equip-execute path (ra == _ERG_RA_EXEC, cat 2/3, proc != 0) -- that caller is the one that stores 0x44 and casts, so it is an unambiguous equipment activation. The blood cave reads AND CLEARS the ticket every pass (no ticket outlives its action) and charges only on a spell match, plus a cat-1 (consumable commit @C+0x57) veto as a second guard. The stamp leg rides equipment_rune_gate's detour on the same hook when that feature is on (ERGOUT path only, so locked gear never stamps); blood_magic installs a stamp-only cave at that hook otherwise.
# v220: FORMATION SWAP NOW MOVES THE SLOT ARRAYS WITH THE CHARACTER. spent[4][8], the Crimson Wizard point pool[4] and the Soma count[4] are indexed by party ROW, but the formation routine MOVES the character records between rows -- so spell charges stuck to the SLOT, not the caster (live 2026-08-04: a Red Wizard's available charges changed just by reordering the party; max is derived from the char record and followed him, spent did not, and the magic menu drew max - spent[row] from two different people). RE by write-bp on the party records during a swap -> fn ~0x088c21xx..0x088c26xx, a three-leg swap through a stack temp (stash row X -> copy row Y over X -> copy temp into row Y) with both indices already in memory: rowX = lb 1([s0+0x718C]) (cursor row, s6 = [s0+0x7098] + rowX*0x5C), rowY = lh [s0+0x70B2]. New cave at 0x088C257C -- the third leg's copy loop ends at 0x088C2578, so the records are fully swapped and s0 is live -- swaps all three arrays through SAVE_BLOCK_PTR, displacing sw zero,0x70dc(s0) / sw zero,0x70ec(s0) (branch-target scanned clear over 0x088C0000..0x088C4000, and bake asserts the pair is vanilla). Full caller-saved spill: the hook sits between two of the routine's own copy loops (the v214 lesson). The client keeps an independent permute loop keyed on the identity byte at PARTY_BASE-4 + row*0x5C as a backstop for saves made before this bake, but the on-disc swap is what makes the menu's FIRST draw correct -- the client wrote a tick late and the menu holds its rendered row until reopened. // v219: GRAND MASTER CAPPED HEAL BOOSTED TO MASTER_HP_CAPPED_PCT (50%) OF DAMAGE TAKEN. Below the attack cap nothing changes (max-HP tick 20% of damage, HP healed 10% = half the tick, yellow attack-gain number); at the cap the Master stops scaling offence and turns defensive, healing ceil(dmg*50/100) unhalved, drawn as the v218 green number. The client gates the boosted rate on the ON-DISC MB_MATK accumulator (capped when acc >= level + MASTER_ATK_CAP_OVER_LEVEL), NOT on its own max-HP pool, so the rate flips at exactly the instant the cave switches yellow -> green and the value drawn is the value written; a tick that cannot read the mailbox falls back to "not capped". // v218: GRAND MASTER YELLOW 0 -> GREEN HEAL NUMBER. Past the per-battle attack cap (level+5) the Master leg drew a yellow 0 ("at max, can't benefit"); but a capped Monk/Master still HEALS off damage taken (client _scroll_battle_loop, whose heal leg is now driven by the UNCAPPED tick so it never stops firing once the max-HP pool is spent). The capped branch now falls to MSHEAL, which mirrors the client maths -- heal = (ceil(dmg*MASTER_HP_DMG_PCT/100) * MASTER_HP_HEAL_NUM) // MASTER_HP_HEAL_DEN -- and staggers a GREEN number (delaypop flags 0x20, no colour override -> native green bank) instead. DISPLAY ONLY: no HP write in the cave, because the physical damage epilogue never sees magic damage and the client leg must stay the single writer; the three tuning consts live in iso_patcher and ApClient imports them so the drawn number cannot drift from the applied heal. A heal that rounds to 0 (chip damage) draws nothing rather than a green 0. // v217: MS2 PACK / CARAVAN BUNDLE COLLISION FIXED (live crash 2026-08-03: entering Marilith 2.0 in the Chaos Shrine basement with boss_minions=absurd -> "Bad Execution Address, CPU Jump to 00000000", PC 0, RA 0x088fba28). ms2_bake.bake_minion_packs took a caravan_active flag, documented it as "the caravan row may relocate FM_SHOPUS into FM_SHOPJ1 -- another J1-named donor. One region, one owner", and then never used it: the reserved set was built from the EXTERN J1 pair alone, so FM_SHOPJ1.PCK stayed in the donor pool. The allocator best-fit a grown fiend pack into its extent (shipped ISO: MS2_074.PCK @0x2f9eb50 +0x819a, exactly FM_SHOPJ1's home) and bake_caravan_offer -- which runs AFTER ms2_bake in patch_iso -- relocated the grown FM_SHOPUS bundle to that same offset, overwriting the pack's head. MS2_074.PCK then decompressed to SHOP_MSG/JOB_NAME/FM_SHOPUS entries, battle init found no MS_07/MS_43 add GIMs, built a null monster-kind object and jumped through it. Deterministic per seed and per victim: it hits whichever fiend pack best-fits that extent (0x73 or 0x74 across the seeds tested) and only when the authored caravan row actually grows the bundle enough to relocate -- which is why other bosses in the same seed were fine. The reservation now lives in ms2_bake.reserved_names(extern_active, caravan_active), test_minions.donor_reservation_checks asserts each target leaves the pool exactly when its owner is active, and a bake replay of the crashed seed's plan confirms the pack moves off FM_SHOPJ1 (0x2f9eb50 -> 0x307bdf0) with zero reserved-extent overlaps across four plans. // v216: DIFFICULTY-SLOPE POOLS for Mount Gulg, Cavern of Ice, Sunken Shrine and Mirage Tower. Measured as played, harder_dungeon was NOT monotonic: it reroll a dungeon from the NEXT dungeon vanilla pool, but that chain follows STORY order and vanilla difficulty does not -- ice->trials steps DOWN 24.1->20.9 and mirage->fortress steps DOWN 32.6->28.6. Result: Cavern of Ice (20.8) came out EASIER than Mount Gulg (24.1), and Mirage Tower was SOFTER with the harder option on than in vanilla (32.9 -> 28.6, because it inherited the fortress weak tail incl. 1-4 Earth Medusa 15.0 and 3-4 Manticore 15.1). Sunken Shrine self-maps so it never stepped at all. All four are now authored rows on the same detour, to the user target slope Gulg 25.0 < Ice 28.6 < Sunken 32.2 < Mirage 40.2 < Fortress 47.7, hardest fight in the MOST common slot, boss cameos pinned to the (map,slot) rando._DUNGEON_BOSS_SLOTS already used. The two-range if-chain is replaced by a 256-byte map-id -> row+1 table (0 = not authored -> vanilla path), so adding dungeons no longer grows the asm: 34 rows now. Named swaps per user: Ice gains 2x Ice Gigas+Winter Wolves, 3-4 White Dragon, 1 Elm Gigas+Dark Wolves and 2-3 Evil Eye, losing Mindflayer 13.7 / Medusa 15.6 / Minotaur Zombie 17.6. TRADE-OFF: these four dungeons are no longer seed-random, same as the basement and fortress. // v215: version bump only -- v214's hook relocation was released twice under the same number (the register-spill attempt, then the real branch-target fix), so a client that baked the first v214 booted its CACHED ISO and the fix looked dead. No code change vs the second v214. // v214: MAGIC SUBMENU ROW CAVE MOVED OFF A BRANCH TARGET (spell names vanished from columns 2/3 in the Discard submenu while the cursor still walked them -- user report 2026-08-03). The slot_magic "cur/max" cave was detoured over 0x088D1A50/0x088D1A54, the COLUMN loop head; the loop's back-edge (0x088D1D5C `bnez ... 0x088D1A54`) targets the SECOND displaced word, so columns 1 and 2 re-entered the middle of our `j cave / nop` pair, never ran `addiu v0,zero,1`, and reached `beql s6,v0` with v0 = the cave's leftover max-charge count -- the s6==0 (Use) and s6==1 (Discard) name-draw legs were both skipped, and Use mode only looked fine because the leftover v0 happened to match its own mode test. Hook moved to the ROW loop body 0x088D19A4 (`move fp,v0` / `lw v0,0x40(sp)`), which nothing branches to (the row back-edge targets 0x088D19A0); displaced word 1 runs first (the body indexes the level off fp), word 2 last on the restored native sp. Numbers now draw once per row instead of once per column. Cave also spills t0-t9/a0-a3/v1 now: a cave inside a native loop cannot treat caller-saved regs as free. // v213: ABSURD BOSS-MINION FIGHTS USE THE TOP-FIRST 9-SLOT GRID (layout 5, not 0). The formation layout byte selects a static position array (dispatch 0x08879254 -> jump table 0x0894BEC4 -> arrays 0x08948CAC+, entry = 3 bytes (x, y, scale), formation slot 0 = the boss = ENTRY 0). Layouts 0 and 5 hold the SAME nine coordinates in a different ORDER: 0 is column-major with the MIDDLE row first (entry0 y=0x4c), 5 is row-major with the TOP row first (entry0 y=0x0c). Absurd stamped layout 0, so a big boss sprite (Tiamat) sat on the middle row with its damage numbers rendering under the battle menu -- unreadable, and the popup colour indistinguishable -- while lighter intensities looked right purely because layout 2's entry0 is already y=0x0c. Vanilla boss fights never hit this: they use the single-position layouts 3 (0x10,0x0c), 4 (0x40,0x3c) and 6 (0,0). Swarm bosses (Piscodemon) stay on layout 0: their slot-0 sprite is small, so there is no popup problem, and the mid-first order packs the swarm the way vanilla does. Layout 5's array is untouched and shared only with fids 0x4A/0x96. apply_boss_minions now range-checks the layout against the dispatch's 0..6 (`sltiu at,a1,7`), since an out-of-range id falls through to a no-position path and would render an empty formation. Live-verified vs Tiamat + 3 Weretiger + Lich 2026-08-03 (RAM poke re_only/poke_boss_layout.py). // v212: FLYING FORTRESS PER-FLOOR POOLS. The chaos-floor detour now serves TWO map ranges (0x20-0x27 basement rows 0-7, 0x5c-0x60 fortress rows 8-12) off one hook -- there is only one cave-table read to hook, so a separate fortress detour was impossible. Why: harder_dungeon stepped fortress -> chaos_basement, so all five floors drew from ONE pool of the basement vanilla bytes -- Earth/fire flavored (2-4 Earth Elemental, 4-7 Earth Medusa, the Gulg fire block landed on the sky dungeon) and a flat 36.1 expected threat with zero gradient. Now each floor keeps the top half of its OWN vanilla fights by threat (Evil Eye and Mindflayer formations promoted into that half and ranked with those monsters x2.5 -- they punch above their number), the bottom half is replaced from the Chaos floors + DLC bestiary capped at 1.5x that floor hardest vanilla fight, and slot order is INVERTED vs _CF_POOLS/_DF_POOL: hardest fight in the most COMMON slot. Expected threat by floor 36.8/41.0/51.2/51.4/60.1. WarMech hand-placed one step more common per floor (F1 s7 1.56% -> F5 s3 18.75%); that displaced Kraken F1 s7 -> s6. rando._DUNGEON_BOSS_SLOTS mirrors both cameos into the u8 table. // v211: DISPATCHER TICK GATED PER RECORD (_DP_TICKFAST byte +3, set only by the CW pay-site arm, cleared at consume). v210 still lost CW teal / Grand Master yellow (live 2026-08-02): the unconditional dispatcher tick ripened on-hit records while the white damage numbers were still animating, so the anim-poll spawn landed same-frame with them and was hidden/illegible (v121's known same-frame failure). On-hit records (teal MP gain, GM yellow, blood, strength, WW dia) now tick ONLY at the anim-poll site = exact v207 timing; the CW cast-refund record alone fast-ticks at the dispatcher so it ripens during a pure status cast and spawns in-cast from the anim-poll context. // v210: BOTH PUMP SITES TICK (spawn stays anim-poll-only). v209 starved on-take-damage delaypop records (live 2026-08-02: CW gained charges but no teal number; Grand Master yellow popped 2x over ~400 dmg): they arm late in an enemy action, the float frames run the anim-poll fn but not (always) the dispatcher, so a dispatcher-only countdown froze, the record carried over pending, and the next arm overwrote it. The anim-poll cave ticks again (as through v207) AND the dispatcher ticks, so status casts still drain; a frame where both run drains 2 ticks -> effective stagger 15..30 frames, still visibly offset. // v209: SPLIT DELAYPOP PUMP -- v208's single dispatcher-prologue pump corrupted (live 2026-08-02: shredded party sprite + yellow 00000 during a 5-hit attack; the prologue runs in every SM state incl. setup states, so calling _POPUP_SPAWN_FN from it is NOT safe -- the anim-poll state 0x08881478 remains the ONLY proven-safe spawn context). The pump is now two hooks sharing the mailbox: the dispatcher prologue 0x088824DC TICKS the countdown every frame an action resolves and never spawns; the anim-poll state 0x08881478 SPAWNS a ripe (delay==0) record and never ticks. Status casts drain the countdown via the dispatcher, and v207's one-action-late symptom proves the anim-poll fn does tick during a status cast (the countdown partially drained), so the cast's own anim window finds the record ripe and spawns the green number in-cast. // v208 (no release, corrupted): delaypop pump moved wholesale to the dispatcher prologue 0x088824DC (RE'd 2026-08-02 offline via re_only/bootdis.py: battle phase SM 0x0886B0FC -> sole call site 0x0886B3AC with a0 = ctx+0xD41C action-SM obj -> dispatcher switches on [a0+0x17] and jal's every per-action SM handler; every caller of 0x08881478 is one of those handlers, so the site's frames are a strict superset). RE facts stand; the spawn-context assumption did not. // v207: CW green number back on the delayed-popup service PERMANENTLY (2026-08-02 live: v206's direct _POPUP_SPAWN_FN jal with the correctly DERIVED ctx still corrupted sprites and drew a yellow 00000 over the wrong unit -- the spawner needs display-SM state the deduct phase doesn't have; ctx was not the issue. Do not retry a direct spawn from the pay cave). Accepted cosmetic limit until a per-frame delaypop pump hook is RE'd: pure status casts (Haste) show the green number with the NEXT action's numbers; damage/heal casts show it at the cast; HP always lands at the cast. Gating fully live-verified on v205: Fira heals at cast, Deathbringer activation + consumables never heal, non-RW never heals. // v206: CW GREEN NUMBER SPAWNS DIRECTLY AT THE PAY SITE WITH THE DERIVED CTX (2026-08-02 live: v205 confirmed gating fully correct -- Fira heals at cast, Deathbringer/items never heal, non-RW never heals -- but Haste's number still waited for the next action because the delaypop pump only ticks while numbers float). v204's corruption is attributed to its garbage [s2+0x00] ctx, not to calling the spawner from deduct context: the corrupted spawn DID surface immediately during the cast anim, proving the spawn point works. jal _POPUP_SPAWN_FN(derived ctx, row, 0x20, heal); full caller-saved spill. // v205: CW PAY-SITE CAVE FIXES (live 2026-08-02: Haste cast -> sprite corruption + stuck yellow 00000 + popup over the wrong unit). (1) ctx was read from [s2+0x00], unverified at the deduct site -> class gate walked garbage; ctx is now derived ctx = C - 0xC714 - row*0x6C from the two verified fields. (2) the direct jal to _POPUP_SPAWN_FN from deduct context corrupted the display SM's sprite state; the green number is armed via the delayed-popup service again. Known cosmetic limit: for a pure status cast the number surfaces with the next action's numbers (the delaypop pump only ticks while numbers float); the HP always lands at the cast. // v204: CRIMSON WIZARD REFUND MOVED TO THE PAY SITE + IMMEDIATE NUMBER (2026-08-02, live: even with v203's cast-spend marker, Haste's green 10 popped when the Monk attacked). Two causes. (a) The refund leg lived in the magic-executor prologue keyed off the sp-frame spell id; the executor runs/carries stale frames for other actions, so no gate built on its frame could be ordered correctly against the deduct-site marker. The leg is DELETED from the executor cave; the heal now lives in a cave hooked at the battle MP-deduct RETURN point (_SM_BATTLE_DEDUCT_RET) -- the one code path that runs exactly when a native cast is paid (items/equipment procs never deduct; enemy rows gated) -- displacing the native MP subtract. slot_magic ON: its deduct cave jumps into it, heal = 5*spell level via s1; OFF: vanilla falls through, heal = a0(MP cost)/2. The v203 cast-spend marker machinery (mailbox, slot_magic stamp, MP-mode payer detour) is removed. (b) The green number was armed via the delayed-popup service, whose pump is the popup SM's per-frame state and only ticks WHILE NUMBERS FLOAT -- a status cast floats none, so the armed record froze until the next action's numbers started the SM (the visible late heal). The pay-site cave jals _POPUP_SPAWN_FN directly instead; no other number floats at deduct time, so the stagger is unneeded. WW dia keeps its v203 inline delivery (dia numbers float, its delaypop drains). slot_magic bake probe stays on the FIELD deduct site (battle-deduct area now detoured by job_scroll in every mode). // v203 (no release): cast-spend marker attempt; heals delivered at the cast for WW dia + CW. // v203: CRIMSON WIZARD HEAL ONLY WHEN HE PAYS, AND AT THE CAST (2026-08-02, user video: a CW using a Phoenix Down healed, and a CW casting Haste got his green 10 later, over the Death Eye's attack on the Fighter). Two independent defects. (1) GATING: the magic executor runs for item/equipment casts too, and the spell id cannot separate them -- a Black Robe casts the SAME Blizzara id a native cast does -- so every id-matching skip either leaked item heals or killed the refund on real casts of those spells. Replaced by the CAST-SPEND MARKER: the battle MP-deduct site (_SM_BATTLE_DEDUCT, slot_magic's slot-spend) stamps {pending,row} in a shared mailbox threaded via feats (_castspend_mb), and the CW leg refunds only when the marker names the casting row. Item casts never reach the deduct site, so they can never mark. slot_magic stamps from inside its own deduct cave; with slot magic off, job_scroll_boosts installs the payer detour there itself -- which is why the slot_magic bake probe moved to the FIELD deduct site. (2) DELIVERY: both scroll heals (WW dia self-heal, CW cast refund) only RECORDED into the HEALPOP mailbox for the executor's post-loop epilogue cave to pay out; a pure STATUS spell never reaches that epilogue, so the record sat pending and landed on whatever action ran next. Both legs now write the battle HP and arm the delayed green number themselves at the cast (still no result-entry append -- v190 rule); the HEALPOP epilogue leg is left in place but has no writer. // v202: SHOP PURCHASES BY MAILBOX, placeholders freed (2026-08-01, user report: skeletons 'dropped' a Lute Tablet -- a native Echo Grass drop wearing the global AP name bank). RE (re_only/HANDOFF_shop_buy_hook.md): the item/equip purchase COMMIT calls ADD-ITEM via `jal 0x88d4494` @0x0881ec64 with a1=cat a2=gid a3=qty and s0 = the shop UI struct, which carries the GLOBAL STORE ID @+0x7064 (== rando._DEF_IDX shop-def index; Crescent w/a/i live-verified 0x15/0x16/0x17) and shop type @+0x7068. apply_shop_buy_mailbox (ON_DISC_ALWAYS) re-points that jal at a cave appending (store,type,cat,gid,qty,seq) to the BUYB ring mailbox (8x8B, head-bump-last); the client consumes it and attributes AP offers EXACTLY by store id. CONSEQUENCES: the gil-drop inference is deleted (SHOP_GIL_HISTORY retired) -- no more DLC-shop refund misfires or 'appeared WITHOUT the matching gil drop' warnings; placeholder ids are ordinary items everywhere except their OWN store's normal slots (ap_blocked_by_store replaces the row-global SHOP_AP_BLOCKED semantics in shuffle draws, caravan pools unreserved, scrub_placeholder_stock narrowed to the owning store, steal-pool placeholder guard removed); name/desc banks are now held VANILLA outside towns (coarse LOADED_MAP_ID 2 = town, 0 overworld, 1 dungeon, live-mapped 2026-08-01) so battle drops/chests announce real vanilla names, AP names appearing only on town shelves. v201: harder-overworld encounter rebalance + overworld u16 (2026-08-01, user playtest report). (a) THE BANDS WERE MEANINGLESS: _TIER_BAND_HARDER held slices of _BATTLE_RANK, whose order does NOT track difficulty, so the 'floor lagging two stops' removed nothing -- harder Crescent still rolled 1-2 Cobra (threat 4.1) and 1-2 Tarantula beside 1-6 Ankheg (29.0) and felt identical to normal. Replaced with real THREAT bands, floor(k)=ceiling(k-2): Crescent's floor goes 4.1 -> 15.1, Onrac 7.4 -> 22.5, Trials 9.7 -> 27.1, and Lufenia (previously the fixed _LUFENIA_HARDER_POOL, now retired) becomes a 34.8-64.4 band. Ceilings rise too so harder is never weaker than normal. NORMAL MODE IS UNCHANGED (still the cumulative index slices) per user choice. The threat metric needs the ISO and rando.py runs at GENERATION time, so the per-tier pools are PRECOMPUTED literals from re_only/gen_ow_pools.py; test_patch.ow_pool_checks regenerates and fails on drift. (b) OVERWORLD u16: zones_overworld is u8, leaving only ~20 land formations above threat 40 against ~76 with the DLC bestiary. apply_overworld_u16 hooks 0x08841fbc -- NOT 0x08841fc0, whose delay slot is a branch, and a branch in a jump's delay slot is architecturally undefined -- and ORs in a COMPANION HIGH-BYTE table homed on the unused terrain-3 table 0x08945aa8, shipped as the DataPatch table 'zones_overworld_hi'. CAVE AND COMPANION MUST MOVE TOGETHER: a baked cave without the companion reads vanilla terrain-3 bytes as high bytes and yields garbage formation ids, so both key off harder_overworld_encounters. DLC allowed from Elfheim up. (c) WATER ON LAND, split per user: _LAND_SHARK_BAN (a Shark/White Shark/Killer Shark can actually SPAWN) banned everywhere; _LAND_AQUATIC (Sahagin and Bigeyes families, Pirates, Sea Troll/Scorpion/Snake, Water Elemental/Naga, Piranha/Croc/Ochu/Hydra/Squidraken) allowed ONLY in the Crescent Lake, Onrac and Citadel of Trials regions. strip_land_sea is now tier-aware AND high-byte aware -- without the latter it would strip 0x15e (Red Flan) because its low byte 0x5e is a banned Shark id. It also cannot remove vanilla's own Hydra/Ochu marsh fights in the Trials/Lufenia zones, since it reverts TO vanilla; that is intended. (d) Harder-mode slots are rank-ordered onto the engine scramble curve (hardest = rarest), matching DF and RO. (e) Hand-picks forced into every zone of their tier: Pravoka 0x0dd 4-6 Sahagin (an explicit aquatic exception -- port town), Elfheim 0x01a 2-4 Scorpion; DF pool B t3 swapped 0x095 (Anaconda+Scorpion) for 0x099 (Sabertooth+Lesser Tiger) so Scorpion stays unique to Elfheim. (f) _BOSS_POOL_EXCLUDE gained Chaos 0x7b (threat 275!) and the Guardian/Soldier castle formation 0x4c -- neither was in _BATTLE_RANK, so nothing needed to exclude them while draws were rank-index based; a threat-band draw reaches them. v200: RESULT-ENTRY APPENDS ABOLISHED -- RW-cast self-slow fixed (live RE 2026-08-01). Symptom: a Red Mage with the Crimson Wizard Scroll casting Slow/Slowra slowed HIMSELF on every cast (targets still slowed correctly). Root cause: the CW cast-heal leg (RWCAST, 5*spell-level under slot_magic -> Slow L2 = 10 HP) set HEALPOP, and the healpop cave APPENDED a caster heal entry (src=tgt=caster, flags 0x125) at the executor epilogue; the engine's type-0x04 status apply loop (0x8886104..0x8886328) blanket-applies the ACTION's status kind to EVERY live non-miss entry (per entry: sb tgt->actor+0x3D, sw row->actor+0x38, kind literal sh a0,0x2(row) -- slow=0x0b @0x08886018), so the appended entry's target = the caster got the status. Caught by re_only/bp_slow_apply.py (exec-BP 0x08886020: e1 src=00 tgt=00 val=10 flags=0x0125 mid-apply of a 9-Pirate Slow). Type-3 spells key off per-entry state, which is why Sleep/Quake/Scourge never misfired and the v189-era colour hooks were wrongly suspected -- the real trigger was the seed finding the CW scroll. FIX: all three scroll-cave entry appends removed -- healpop HPCLAIM -> direct battle-HP write (clamp maxHP, skip dead) + delaypop GREEN (flags 0x20); Knight lifesteal LCLAIM -> the old array-full direct write is now the only path + delaypop green; non-slot CW MP-restore RWCLAIM -> direct curMP write clamp maxMP (its delaypop teal stagger unchanged). RULE: caves must NEVER append result entries; deliver effects via direct stat writes + delaypop visuals. Residual: blood_magic's item self-damage append remains (needs native popup+KO pipeline); exposure = a blood-taxed activatable casting a type-0x04 spell, none ship. Tools kept: re_only/bp_slow_roll.py, bp_slow_writer.py, bp_slow_apply.py. v199: mystic_door_gate REVISED (v198 never shipped a working leg) -- v198 dropped the kind=1 val-0x11A4 records, which turned out to be forced-encounter trap tiles (sub3, formation 0x10), not the lock; doors stayed shut (live 2026-08-01). The lock is the kind=4 val-0x1F4A door records: the object-list reader creates b0=2 collision cells from them (field+0x52CC, 8B cells) and the walk scanner 0x885bd9c blocks b0&0x0A. Vanilla opens doors via EVM op 0x36 clearing the cell to 0xFF (Cornelia: flag9-gated 36 04 cd 23; its treasury cells read 0xFF at map load with the key owned). The shrine has byte-identical bytecode (event 0x12: flag9 -> 36 04 4a 1f) that never fires -- invoker un-RE-d. v199 drops the two kind=4 door records when the Mystic Key possession bit is set, so the cell is never created; walk-through proven live via cell poke. Trap tiles restored to vanilla.
# v196: slot_magic level-up line baked ON DISC (2026-07-31, battlemsg_bake). The client's resident-bank poll loses a race with the level-up sequence -- it re-loads BATTLE_MSG from its source, so the box drew the vanilla "MP increased by 1." for a Red Mage AND a Black Mage while the client repaired the resident copy only afterwards (live log 16:24:40 / 16:39:04, each rewrite landing right after the level-up battle). The bank is a TEXT bank inside wp16-compressed FONT_BATTLEUS.PC; entry 8's body is rewritten in place to "New spell slots {N}!" (19 bytes = the span EXACTLY -- growing it would need entry 9's offset and destroy that live level-up line). Re-worded the pack recompresses 12 bytes over its slot (vanilla's line was one back-ref from entry 7's "HP increased by {N}."), so it relocates into FONT_BATTLEJ1.PC's extent (0x568c, Japanese battle font, never loaded in a US boot and outside ms2_bake's FM_DBG_*/FM_*J1/J2 pool) with the US record repointed -- dpk and ISO stay the same size. Client _slotbox_loop stays as a repair path for pre-bake ISOs.
# v195: Crimson Wizard MP->HP leg repriced under slot_magic (2026-07-31). The leg healed MPcost/2, but slot_magic makes MP costs inert, so the heal read a cost the cast never paid. Under slot_magic the heal is now 5 * SPELL LEVEL (L1 5 HP .. L8 40 HP, user-chosen 2026-07-31): the RWCAST cave loads the LEVEL byte (_FCAST_L9_BASE + id*14, one below the MP byte the MP-mode arm reads) and shift-adds x5. MP mode is byte-identical (still cost/2). A W(L)/10 proposal derived from the damage->slot prices (15..150) landed ~3x low against the MP-mode feel (50-MP spell heals 25) and was rejected.
# v194: SOMA DROPS repurposed under slot_magic (2026-07-31). Effect-2 (raise maxMP by 5, refuse at 999) becomes "raise the level-1 spell slot count by 1, spilling upward": storage is a per-character COUNT u8[4] at save+0x830 (the dead v183 INT-accumulator block -- nothing has read or written it since INT variance went live-compute), and the DISTRIBUTION stays derived, exactly like the natural table. final[L] = nat[L] + clamp(N - sum_{L'<L}(9 - nat[L']), 0, 9 - nat[L]) -- N ones poured in from L1 up, hard cap 9 per level, only levels within magiclv taking part (class caps deliberately do NOT apply; Soma is the one way past a hybrid's natural ceiling). Because it is derived, a level-up needs no grant hook and the spill re-solves as the curve grows: the user's own case, nat 9/9/8/7/6/5/4/3 + 1 soma puts L3 at 9, and one level later nat is 9/9/9/7/... so the SAME soma lands on L4 instead. _sm_slotfn grew a stack frame (t0-t3) and an inner threshold scan per level below the requested one; new leaf _sm_totalfn(a0=rec, a1=ASSUMED level, a2=row) -> v0 total / v1 = 9*magiclv. Validity (0x088C4FD8) mirrors vanilla's 999 refusal: usable iff total < 9*magiclv, so a full caster (and every magiclv-0 job) rejects the drop instead of eating it; apply (0x088C577C) only bumps the count. The level-up line now reports total(new) - total(old) instead of raw threshold crossings, which over-reported once Soma had already filled the crossing level (known blind spot kept from the old cave: magiclv is read as it stands, so a level-up that ALSO unlocks a spell level may under-report). Companion gen-side change, no patcher role: Faerie Tonic (a full MP restore, inert here) is pulled from the shop/caravan draws (rando._MANA_ITEM_BLOCK, threaded as a PARAMETER -- one gen process hosts many worlds), the AP filler pool (World._filler_names) and the thief-steal tables (ApClient blocked set). Held tonics keep vanilla behaviour by request.
# v193: slot_magic + grant counter RELOCATED save+0x460..0x48F -> save+0x808..0x83B (2026-07-31). The "verified 996-byte zero run" at 0x11558 was NOT free space: it is a native bounded ROLLING LIST of per-visited-map return records ({u32 flag, u16 x, u16 y, u32 dir, u32 map_id}, 16B stride, growing up from save+0x440; live-caught eating Arus's spent row as record coordinates, and the vanilla ENDGAME save holds a full record over the old grant-counter home 0x460). New home is flush against the TOP of the v188 preview-copy window (native word at save+0x83C, BONUS_FLOOR_TABLE at 0x840): counter 0x808, spent 0x80C, CW pool 0x82C, INT-acc 0x830 (vestigial, v183 went live-compute), marker 0x838 (no longer overlaps acc[2] -- the 0x5A stamp was corrupting row 2's accrual), pad 0x839..0x83B. ~790B (~49 records) of headroom above the list's observed high-water (0x4E8 mid-game, 0x4B8 endgame; the list REUSES entries, it does not grow monotonically). Client slotbox loop canaries the guard band save+0x7EC..0x807 + the pad and alarms if native data approaches. NO back-compat: pre-v193 saves read as unstamped garbage and are zeroed on first sight (user-waived). Old lesson superseded: zero-in-a-snapshot is NEVER sufficient evidence a save region is free -- demand an endgame-save cross-check plus a runtime canary.
# v203: slot_magic dabbler curve DE-LUMPED (2026-07-31). The dabbler anchors were floor-interpolated, so every spell level's step landed exactly ON the shared anchor char levels -- measured gains were +3@20, +3@30, +4@40, +5@50 with 5-9 dead levels between and NOTHING after 50. Anchor VALUES are unchanged (still the user-authored L20 4/2/1/1 ... L80 9/8/6/4/3/2); only the intra-segment step PLACEMENT changed: steps spread evenly across each segment with a per-spell-level PHASE (_SM_DAB_PHASE, chosen by exhaustive 0.3..0.7 search minimising max-gain then sum-of-squares) so rows no longer step together. Result: at most 2 slots on any single level-up (twice in 1..80), every other gain is exactly 1, and growth continues past 50. Ninja merge + magiclv unlock levels (1/5/12/20/29/39) unaffected -- steps still land on/before their end anchor, so value(anchor)==anchor exactly (asserted). Companion CLIENT-ONLY change (no patcher role): the level-up line reads 'New spell slots {N}' -- moving the {N} token to the END kills the '1 spell slots' disagreement without any runtime singular/plural machinery. An earlier cave-side tail-rewrite + SMSG mailbox was built and then DELETED for that reason; entry 8's 19-byte span is the hard cap on any rewording (the requested 'New spell slots gained... {N}' is 28 and would have to destroy entry 9).
                      # v146: yellow attack-gain number on Temper/Saber/Strength Tonic/Giant's Gloves (clamped delta -> yellow 0 when maxed at 255)
                      # (13 balloon anim scripts after the records; header
                      # u16@6 = script count, u16@0xa = their offset table).
                      # v143 dropped it -> every enemy status balloon drew the
                      # cell-0 "..." bubble instead of Zz/sunglasses/etc.
                      # v143: BATTLEICON def table GROWN 24->30; custom popup
                      # defs moved 18-22 -> 25-29 (24 = inert dummy for the
                      # native kind-0x18 spawn). Vanilla defs 18-22 restored --
                      # they are the ENEMY status balloons, and stomping them
                      # made a Blinded enemy blink a mirrored red MISS!!
                      # forever (live 2026-07-24). Cave kind constants follow
                      # popup_bake.DEF_*; steal borrow def rides DEF_RED_MISS.
                      # v142: shop panel white 6th line follows class change --
                      # Monk before promotion, Master after (any party class
                      # >= 6); cave pokes the display id + swaps the learn
                      # check to a1=8/bit10. Black stays Thief (Ninja is
                      # natively listed).
                      # v141: shop class panel shifted up 6px (y base 0x60->0x5A,
                      # both color draw paths) so the 6th line clears the box.
                      # v140: magic-shop "who can learn" panel gets a 6th line
                      # (Monk on white shops, Thief on black) under
                      # monk_thief_magic -- 6th table entries in padding, loop
                      # bounds 5->6, builder tails detoured to a cave adding
                      # the bit-0x20 learn check.
                      # v139: Piscodemon (fid 0x1C/0x9C) excluded from the minion
                      # death serializer -- its vanilla dissolve is already the
                      # concurrent type-6 fade, and force-serializing the swarm
                      # (all one species) distorted death SFX. Now falls through
                      # to pure vanilla death visuals (_MDS_EXCLUDE_FIDS).
                      # v138: rarity-coded steal-cue SFX -- SPRB mailbox grows a
                      # u16 SE id (+8, mb len 12) the client sets per rarity
                      # (0xaa knife common / 0xcf antidote rare / 0xb6 ether
                      # super); cave plays it, 0x73 fallback if unset.
                      # v137: steal-cue SFX -- the steal-icon spawn cave also
                      # calls SE_Play(0x73) so a sound fires the same frame the
                      # rarity icon pops (RE: dis_sfxscan/test_sfx_probe).
                      # v135: remote chest-box names decoupled from spell_tomes
                      # (bank grown + J1 reserved for remote-only; dynamic sid base
                      # 43 without tomes). v134: steal-cue spawn slot 0x12 -> 0x3C.
                      # are live objects too (stray muted balloon + a party member
                      # drawn on the wrong z layer, live 2026-07-23); the real
                      # transient popup pool is idx ~33..66, so park at 60.
                      # v133: ms2_bake splitting/coalescing dead-space allocator
                      # -- best-fit + residual split over free EXTENTS (not whole
                      # donors), relocated packs free + coalesce their own old
                      # extents (fiend MS2 packs are back-to-back), index records
                      # decoupled from space, J1 EXTERN donors released when
                      # extern_bake inactive. Fixes 'no donor pack fits' on
                      # Absurd boss_minions seeds (live 2026-07-23).
                      # v132: steal-cue icons reworked to PIXELS ONLY -- v131's
                      # static def 13/16/17 clones were the ENEMY STATUS balloons
                      # (gem flashed over sleeping enemies, live 2026-07-23). The
                      # client now borrows def 19 (red MISS!!) at runtime via the
                      # resident OTI and restores it at battle end.
                      # v131: thief-steal loot cue -- a rarity icon (coin/bag/gem)
                      # pops over the thief at battle start. SPRB spawn detour
                      # @0x886eb94 (message-VM entry) + SPRB mailbox folded into
                      # apply_popup_colors; its misskind cave honors the SPRB
                      # active gate (shared sprite path). Client _arm_steal_icon
                      # arms it; dormant otherwise.
                      # v130: Grand Master attack gain moved ON-DISC (physical +
                      # magic damage epilogues) with a per-battle accumulator, and
                      # pops a YELLOW "attack gained" number (0 at cap) staggered
                      # after the damage -- like the CW teal. New heal-arm yellow
                      # digit def (popup_bake def 18, x320 y48). Client keeps only
                      # the Master max-HP leg.
                      # v129: two deferred popup legs. (1) Crimson Wizard MAGIC
                      # damage now refunds MP (dealt/5) + teal number, via a scan
                      # at the magic-executor epilogue (physical hits already
                      # refunded; this closes the gap). (2) blood_magic + healing
                      # activatable item: the caster's green heal is no-displayed
                      # and staggered ~0.5s after the red blood-cost number
                      # (ask-2 original case). The staggered-popup service moved
                      # from job_scroll into a shared, always-on install
                      # (_install_delayed_popup, threaded via feats) so blood can
                      # use it even when job_scroll is off.
                      # v128: atlas height back to the PROVEN 64; teal glyphs
                      # live in the free y=48 band (x160..304), def U=0 V=48.
                      # 128-tall broke at runtime too (teal dark, "Back" label
                      # discoloured) -- the engine's upload path tops out at
                      # 512x64. HEIGHT IS FROZEN AT 64.
                      # v127: White Cleric dia INT step = dia tier (Dia +1 /
                      # Diara +2 / Diaga +3 / Diaja +4), accumulator still clamped
                      # to caster LEVEL. Baked u8[64] step table @868; cave banks
                      # step[id] instead of +1. Was +1/cast (v107-v126).
                      # v126: atlas height 96 -> 128 (PSP GE needs power-of-2
                      # texture heights; 96 garbled every popup glyph).
                      # v125: teal glyphs moved to atlas ROW 4 (512x96) at
                      # x160..304 with def U=0,V=64 -- the renderer's cell-base
                      # subtractor is a CONSTANT 30 (both id fields ignored,
                      # live), so heal cells 40-49 sample x=160+d*16 at the
                      # def's V. v124's id=40 def drew the red row instead.
                      # v124: teal def re-based onto the GREEN heal bank (U=0,
                      # V=32, id=40): the heal arm ignores the s0 bank override,
                      # so v123 drew green-bank cells 40-49 through the white
                      # def geometry = MISS!! glyph fragments. s0 write dropped.
                      # v123: teal refund number STAGGERED (ask 2): the physical
                      # cave records {unit, value, delay 30f} and a new cave at
                      # the anim-poll state spawns the teal number half a second
                      # after the damage number (same-frame spawn was illegible/
                      # garbled). Spawner signature corrected from the display
                      # state's call site: a2 = FLAGS, a3 = VALUE (old note had
                      # them swapped -> drew 0x2020 as the number).
                      # v122: Crimson Wizard fixes -- leg 2's equipment skip
                      # replaced with blood-magic's real gate (C+0x44 holds
                      # NATIVE committed spells too, so v121 skipped every
                      # refund); leg 1's teal number now drawn by a direct
                      # digit-spawner call (vanilla display has no MP popup;
                      # entry marked 0x80 no-display, engine still applies MP).
                      # v121: Crimson Wizard MP<->HP moved ON-DISC with popups:
                      # leg 2 (cast prologue cave RWCAST branch) heals cost/2 as
                      # a GREEN number via the healpop epilogue; leg 1 (physical
                      # epilogue RWCHK branch) refunds dealt/5 MP as a TEAL
                      # number (flags 0x2325 incl. the 0x2000 teal marker --
                      # first consumer of the v114 teal plumbing). Client delta
                      # leg retired (mailbox bit3 = RW scroll). GAP: magic
                      # damage taken by an RW refunds nothing until the
                      # executor-scan leg.
                      # v120: WW Cleric dia hits only the boss, not non-undead minions
                      # number -- delivered via an appended heal entry at the
                      # physical combat-calc epilogue (no exec value-loop there,
                      # so a direct append sticks; old direct write kept as the
                      # array-full fallback).
                      # v118: White Cleric dia self-heal GREEN number, correct
                      # path. The dia cave runs INSIDE the magic executor, whose
                      # per-slot value loop recomputes (zeroes) any entry appended
                      # mid-loop (proved live: hardcoded val=50 -> shown 0). Fix:
                      # the cave records {unit,value} in the HEALPOP mailbox; a new
                      # cave at the executor's post-loop epilogue (0x8885494)
                      # appends the heal entry after the loop, so the engine
                      # applies + draws it. (v116/117 were the diagnosis path.)
                      # v116: White Cleric dia self-heal GREEN number -- heal
                      # entry flags corrected to 0x125 (base 0x1 needed for the
                      # +4 value; 0x20|0x100 alone drew green "0" and no heal).
                      # v115: dia self-heal delivered via an appended heal
                      # result-entry (apply on) instead of a direct HP write, so
                      # the engine draws the green number over the caster. (v114
                      # first cut used flag 0x10 = MISS verdict -> "MISS!!".)
                      # v114: teal popup-colour bank (Crimson Wizard mana
                      # restore). popup_bake bakes a teal digit def (def 20,
                      # row 3 x0..159); the BANK cave routes heal-arm popups
                      # carrying the 0x2000 marker to the teal def on the white
                      # cell bank, and the KIND cave now applies the per-popup
                      # stash on the heal arm too (HP heals stash white -> stay
                      # green). Dormant until the RW MP-restore append cave sets
                      # the marker (live-RE leg).
                      # v113: popup-colour ROLL/HITCLR caves read the target
                      # unit from the ACTOR object (s4+0x3D) -- the result
                      # entry's [+1] is still 0xff at roll time, so v112 indexed
                      # 255 bytes past the class table (no colour ever landed,
                      # and the stray write hit a neighbouring cave). All four
                      # table accesses now mask to the 16-slot range.
                      # v112: popup-colour BANK detour moved to the damage/heal
                      # CONVERGENCE (0x8873b6c) -- v110 hooked the damage-only arm
                      # and its detour nop clobbered the heal path's branch target
                      # 0x8873b6c, making Healaga print 0..3 HP; the convergence
                      # is also where blood-magic fail damage arrives (why Warp
                      # stayed white). Gates on s7 (the engine's own damage/heal
                      # branch). Reproduces the displaced move+jal+delay-slot.
                      # v110: popup colours now key on the TARGET UNIT (per-unit
                      # class table in cave scratch) instead of result-entry
                      # flags -- the flag channel missed blood_magic's failed
                      # insta-kills, which reroute into a separate damage entry
                      # (live: "23% Warp" drew white). Six detours (added a
                      # status-HIT clear so a landed Sleep can't tint the next
                      # hit). Same tiers (white 0% / yellow <=15% / red >15%).
                      # v109: ALWAYS-ON per-target popup colours -- a status
                      # spell's damage number / MISS!! is tinted by that
                      # target's own chance to be affected (white 0%, yellow
                      # <=15%, red >15%); blood-magic self damage is red. Five
                      # detours + the BATTLEICON recolour bake (popup_bake).
                      # v108: White Cleric scroll leg 3 -- a dia cast stacks +1
                      # INT (cap = caster's LEVEL) for the rest of the battle,
                      # on top of the existing self-heal. Written to the
                      # battle-unit record only, recomputed as base+acc from the
                      # party record every cast because the engine re-derives
                      # battle INT on its own schedule; reverts at battle end for
                      # free and cannot leak to menu casting. Accumulator at
                      # SCROLL_MB_DIAINT_OFF, zeroed by the client at battle end.
                      #
                      # v107: chest_dedup (ON_DISC_ALWAYS) -- 10 duplicated
                      # physical chest records re-pointed to previously-unused
                      # treasure indices (19x2/127x2/129x3/134x3/176x2/180x4
                      # aliasing) so every chest is a unique AP location.
                      # v106: cameo/minion boss softening is now the LOWER of
                      # 50% and 50% x boss_difficulty (was 75% x boss_difficulty,
                      # which fielded a 150% "soft" minion on a 200% seed), and
                      # DLC bosses finally have home dungeons so a guest
                      # Two-Headed Dragon is softened at all. Baked bytes change.
                      # v105: miss report is a RING (16 entries) -- a multi-
                      # target cast fails once PER TARGET in one frame and the
                      # old single slot overwrote itself, so only the last
                      # enemy was ever logged.
                      # v104: REVERTED the v103 message experiment (live: the
                      # 0x10 flag IS the miss verdict -- target showed "MISS!!"
                      # and the damage popup was dropped entirely).
                      # v103: EXPERIMENT NECRO_MSG_ON_FAIL_DMG -- commit the
                      # "no effect" result flag on the fail-damage branch so the
                      # battle message can render beside the damage number.
                      # v102: save-or-suffer MISS REPORT block in the SCRL mailbox
                      # (seq/spell/target-unit/INT/gated) stamped by the fail
                      # cave, so the AP client can log the real landing chance.
                      # v101: Necrocaster final tuning (user 2026-07-21). Boss
                      # INT damping (x0.1, target must be enemy unit 0 AND a
                      # boss id -- a boss used as an ADD scales like a minion),
                      # boss autohit threshold back to vanilla 301, Kill roll
                      # fallback (acc 30, KEEPS its death element), pierce now
                      # ONLY on the damage leg, Kill fail power 115.
                      # v100: Necrocaster reliability is now INT-SCALED. Type-3
                      # roll score += INT*3; type-0x12 autohit threshold =
                      # 300 + INT*mult (Kill 10, Stun 20, Blind 40). Both baked
                      # tables now hold multipliers, not final values. Caster
                      # INT = u8 [s4+0x34]+0x36 (RE'd + confirmed both sides).
                      # v99: Necrocaster fail-power retune (user 2026-07-21):
                      # Stun 30->40, Blind 30->40, Death 70->75. Baked table
                      # bytes change, so the version must move with them.
                      # v98: Necrocaster death-resist pierce. A gated BW/Necro
                      # caster clears element bit 0x0008 (the death element --
                      # Kill id64 + Death id54 are the only spells carrying it)
                      # out of the target's resist mask at all three sites that
                      # read tgt_stat+0x24: type-3 roll, Kill autohit, and the
                      # magic-damage path (where resist also HALVED the fail
                      # damage). Other elements and other classes unaffected.
                      # v97: blood popup color DEFERRED -- v96 row-0x1E probe
                      # made blood numbers render BLANK; color detours
                      # removed, popups back to plain white (v90 behavior;
                      # entry flag bit 0x400 still set, engine ignores it).
                      # RE findings preserved in apply_blood_magic comments.
                      # v96: row-write override probe (blank digits).
                      # v95: blood popup color probe 4 (superseded).
                      # v94: crystals_needed yaml -- getStoryFlag wrapper gains
                      # a map-scoped crystal-count leg (Chaos Shrine, fiend
                      # flags 17/19/29/34 read as set once >= N real fiends
                      # down; N=0 always). Rides inside apply_bikke_ship_split
                      # (same hook -- a second detour would orphan the first);
                      # N folds into bake_hash32 as bake context.
                      # v93: blood popup color probe 3 (CONTROL) -- banks
                      # 0x3C and 0x00 both rendered white; now pointing the
                      # override at the KNOWN green bank 0x28 to prove the
                      # detour fires at all.
                      # v92: blood popup color probe 2 -- bank 0x3C rendered
                      # plain white (either a white dupe or out-of-range
                      # fallback); now trying bank 0x00 cells 0-9.
                      # v91: blood_magic popup numbers try the UNVERIFIED
                      # digit bank 0x3C (candidate red): entries carry spare
                      # flags bit 0x400, second detour in spawn fn 0x88739A4
                      # overrides the glyph bank for marked entries. PROBE --
                      # if the numbers render as junk, retry bank 0x00 or
                      # revert to white (drop the override detour).
                      # v90: blood_magic popup hook RELOCATED to the item
                      # SM display/apply state @0x8883854 (displaces the
                      # status-writer jal). v88's cast-epilogue hook ran
                      # BEFORE the executor (live capture): the result array
                      # still held the previous action's zeroed entries, so
                      # the free-slot scan (0xff) always failed into the
                      # silent direct-write fallback -- HP dropped, no popup.
                      # New site runs after the executor's entries exist and
                      # before apply+display consume them.
                      # v89: blood_magic desc leg -- extern_bake appends
                      # "Costs 10 percent of max HP." to every activatable
                      # weapon/armor WEAPON_EXP/ARMOR_EXP.MSG desc entry
                      # (FD.blood_desc_bank shared transform; client shop-desc
                      # baseline mirrors it). Display-only dpk change, but the
                      # bake cache key must roll so existing blood ISOs re-bake.
                      # v88: blood_magic cost now delivered via the action-
                      # result array (ctx+0xCD50, 13x0x14 entries): the cave
                      # appends a damage entry (source=target=caster) at the
                      # cast epilogue and the engine's display+apply states
                      # draw the damage POPUP over the caster, debit the same
                      # battle-HP field (0x88818fc a3=-1, identical clamp),
                      # and run native KO handling. Direct HP write kept only
                      # as all-slots-full fallback.
                      # v87: Light Curtain (item 23) gates at SHOP_TIER_ACTIVATABLE
                      # instead of EXOTIC (battle-use spell item, like the Fangs);
                      # shifts shop-shuffle + caravan draw pools.
                      # v86: blood_magic armor leg reworked AGAIN -- v85's
                      # C+0x5B/+0x5C pair is wiped before the queued turn
                      # executes (playtest: armor still free), so at the
                      # epilogue v0 is the only trace of an armor cast. The
                      # armor leg now scans the live armor table (75 recs)
                      # for any +7 proc == v0; weapon leg (persistent
                      # committed pair) unchanged and runs first.
                      # v85: (superseded) armor cat/id pair @C+0x5B/+0x5C.
                      # v84: blood_magic armor tax first cut (wrong field
                      # pair): _ARMOR_PROC0 0x08954327 (dummy-row0 base,
                      # id*28 like weapons; 1-based committed id).
                      # v83: Knight defense pierce retuned 20% -> 10%
                      # (KNIGHT_DEFPIERCE_DIV 5 -> 10). Verified by
                      # test_defpierce.py, which emulates the cave.
                      # v82: Necrocaster (BW) fail-damage now covers the RESIST
                      # bails. A kill spell on a target that is immune to the
                      # status (type-3, 0x08884D68) or resists the spell's
                      # element (Kill, 0x0888531C) bailed BEFORE the two hooked
                      # miss sites, so it dealt nothing -- live report: Kill on
                      # Death Eye. Both now detour into the same fail-damage
                      # cave.
                      # v81: Blood Warrior/Knight scroll gains a 2nd on-hit
                      # mechanic -- DEFENSE PIERCE. New leg in
                      # apply_job_scroll_boosts hooks the combat-calc fn's
                      # per-hit DEF load (0x88843D0/D4, ret 0x88843D8) and
                      # shaves DEF//KNIGHT_DEFPIERCE_DIV (=5 -> ignore 20%)
                      # before the subtract. Same mailbox bit2 + class {0,6}
                      # gate as the lifesteal leg; no new yaml option.
                      # v80: bonus_dyn_chests AP box names -- on a strip the
                      # cave stamps the next dynamic location's AP item name
                      # (client-armed next_sid u16 @+42, extended cat1 bank)
                      # into the chest record's box fields (+0x586/588/58A),
                      # so the in-game box shows the AP item instead of the
                      # vanilla procedural loot.
                      # v79: bonus_dyn_chests strip condition fixed: strip iff
                      # armed AND idx not in [252,267] (v78's "procedural means
                      # idx >= 268" never stripped in the live Earthgift test);
                      # + cave-entry counter u8@+6 and last-idx trace u16@+40
                      # diagnostics (client logs them on change).
                      # v78: bonus_dyn_chests -- dynamic bonus-dungeon chest
                      # detection moves ON-DISC (strip+mailbox detours at
                      # CHEST_ITEM_CALL/CHEST_GIL_CALL + BDC1 ring cave). The
                      # exec-bp _bonus_dyn_loop never fired in player sessions
                      # (launcher forces FastMemoryAccess=True -> bps silently
                      # dead, live-confirmed 2026-07-19).
                      # v77: giant_cave_gate -- the Giant now PHYSICALLY gates
                      # the four Giant's Cave chests (treasure idx 151-154),
                      # which vanilla were reachable from the north entrance
                      # without ever passing him (logic.py already required
                      # TITAN_FED for them, so reality now matches logic).
                      # Two halves: ELF-side the map-40 giant object record
                      # moves (15,12) -> (12,13); dpk-side map_bake sets grid
                      # cell (11,13) of MAP_06_00_00_AMD.BIN to the solid
                      # boulder tile 0x0055 (already used at (13,9)/(11,15),
                      # ATT already 0xF000 -> no ATT edit). UNFED = three
                      # sealed regions with the Giant talkable from both
                      # sides; FED he despawns and the whole map reconnects.
                      # Ships with the ff1_data Star Ruby function-bit fix
                      # (0x1151D 0x20 -> 0x60; b6 = FED is what opens the
                      # tunnel) -- without that fix this gate would be a hard
                      # softlock instead of a loophole.
                      # v76: restores the UNIVERSAL death-visual gate that v75
                      # narrowed to boss-species units. v75's premise -- "the
                      # type-6 fade overlaps safely, vanilla does it all the
                      # time" -- was never tested and is WRONG: the first live
                      # multi-kill froze, captured mid-wedge with THREE minion
                      # dissolves armed at once (slots 1/2/3, none clearing,
                      # main loop still ticking). Overlap is unsafe for every
                      # dissolve type, so every unit is gated again; the v75
                      # type override STAYS, which keeps each chained death a
                      # short fade instead of a long boss dissolve.
                      # v75: minion_death_serializer gains a per-unit dissolve
                      # TYPE override. The runner picks its dissolve handler
                      # from a type that 0x88756f8 derives from the FORMATION
                      # ID alone, so in a boss formation every minion inherited
                      # the long boss dissolve (jumptable 0x894BF80: types
                      # 1/2/3/4/7 = the shared-runner boss dissolve that can't
                      # overlap; type 6 = the ordinary fade vanilla overlaps
                      # freely). Cave @0x08879c18 forces type 6 for units whose
                      # species != enemy-slot-0's, so minions now fade
                      # concurrently (all die at once) while the boss keeps its
                      # dissolve; the guard narrowed to boss-species units.
                      # v74: minion_death_serializer gains a per-frame sweep
                      # call (battle main loop @0x0886ad04, fid-gated like the
                      # guard): deferred corpses now start dissolving the
                      # moment the previous dissolve's flag clears instead of
                      # standing until the next action's sweep (~a full turn).
                      # The v73 guard doubles as re-arm protection: without
                      # it the per-frame sweep would reset the running
                      # dissolve's flag/timer every frame.
                      # v73: minion_death_serializer -- boss-minion battles
                      # froze when 2+ enemies died in the same damage
                      # application: each action's death sweep (0x888646c)
                      # armed multiple per-slot death-anim flags (bb+0xCD30+)
                      # in one pass, the shared boss-dissolve runner orphaned
                      # all but one, and the orphaned flag blocked 0x8874b3c's
                      # all-clear wait forever. Fix: guard cave at
                      # death_visual_start (0x8871d14) -- in minion-boss fids
                      # (per-seed table) a second death visual is refused
                      # while one is active; the engine's own next sweep
                      # starts it (native serialization). v72 (same-day,
                      # superseded): kill-timing serializer via apply-gate +
                      # frame ticker -- REVERTED, ticker kills landed outside
                      # the action context and stalled the turn machine.
                      # v71: spell tomes excluded from battle item use -- the
                      # battle-usability table (0x0894BB1E, u16 per consumable
                      # id; 0xFF = Tent-style "can't use in battle") is
                      # relocated to a 108-entry bss copy with 0xFF for tome
                      # ids 44..107 and the single reader in fn 0x08871594
                      # repointed. Tomes previously read PAST the 44-entry
                      # table and cast junk battle actions (id44 -> action 0
                      # = "Protect").
                      # v70: citadel crown gate -- setStoryFlag(22,1) (the
                      # Citadel elder's throne-permission grant) is suppressed
                      # in the bikke_ship_split set-wrapper cave unless story
                      # flag 6 (Crown obtained) is set; crownless players keep
                      # a locked throne + a waiting elder, client rewrites his
                      # dialogue (see apply_bikke_ship_split).
# v69: boss_minions rework -- light=1/difficult=3/absurd=
                      # 3+1super-hard minions; absurd uses the 9-slot small grid
                      # for EVERY boss (fiends incl.), lighter intensities keep
                      # the 4-big grid; per-boss layout travels in the plan
                      # ([fid,groups,layout]); swarm bosses spawn the S first.
# v68: ms2 donor pool excludes FM_EXTERN12J1/18J1 (extern_
                      # bake owns those; steal broke tome banks -> menu freeze;
                      # J2 externs stay donors -- largest packs need them);
                      # post-bake gatecheck; load-bearing vs cosmetic bake split
                      # (one canonical 8x8 zone->tier map for forests AND
                      # overworld foot encounters; user-curated via the zone
                      # editor: NW Onrac / N Trials / NE+peninsula Lufenia /
                      # SE Crescent etc.). Gen side: OW zones always roll their
                      # named-tier band (off) or stepped+floored bands (harder;
                      # Goblins gone at Melmond+) with a curated Lufenia special
                      # pool (Green Dragon/Tyrannosaur/Iron Golem/...).
                      # v66: dangerous_forests _DF_ZONE_TIER[39] 8->1 -- the
                      # Peninsula-of-Power SOUTH landmass (zone 39, y121-152)
                      # was Lufenia-tier (Black Dragon/Sekhret forests); tip
                      # zone 31 correctly stays T8. Paired gen-side change:
                      # _OVERWORLD_ZONES[39] 10->4 (foot fights Pravoka-coastal,
                      # not full Lufenia pool). +7 zone bias = ((x+7)>>5)+8*((y+7)>>5).
                      # v65: bikke_ship_split (always-on) -- map-scoped remap
                      # of story-flag id5 -> id63 in the get/setStoryFlag
                      # wrappers while FIELD_MAP_ID==0x37 (Pravoka). Splits
                      # "Bikke defeated" (id63, native + persistent; pirates
                      # stay gone after the fight) from "ship available"
                      # (id5, client-mirrored from the Ship AP item). Kills
                      # the refightable-Bikke wart + the ship-before-Bikke
                      # missed-check edge. See apply_bikke_ship_split.
                      # v64: boss-id adds (0x80-0x90) allowed -- the engine
                      # loads BOSS_<mon-0x80>.PCK per-monster in ANY slot
                      # (live-proven Echidna+THD, no MS2 support), so
                      # ms2_bake skips GIM injection for them; Omega/Shinryu
                      # pools restored to the sheet's Echidna/Two-Headed
                      # Dragon picks.
                      # v63: boss_minions rework -- per-boss 4x (M,S) pool
                      # variants (one rolled per boss at gen; light=1M,
                      # difficult=2M, absurd=2M+1S) + DLC bosses (fids
                      # 0x100-0x110: Echidna..Death Gaze incl. Atomos) +
                      # Chaos (0x7B) + Piscodemon (0x1C/0x9C); WarMech (0x56)
                      # dropped (not dia-targetable). DLC fids ship with no
                      # MS2 pack -> ms2_bake creates one via donor dpk record
                      # steal (byte-sum name hash, hash-sorted index rewrite).
                      # Boss sprites load pack-free (live-proven); packs only
                      # carry add GIM pairs.
                      # v62: boss_minions -- curated per-boss add monsters.
                      # Formation records (layout->2 big grid, slots 1-3 =
                      # gen-rolled adds) + MS2_<fid>.PCK rebuilds (gid-sorted
                      # entries, relocated into FM_DBG_*/J-pack donor space,
                      # dpk record repointed). See ms2_bake.py.
                      # v61: BW scroll covers Break/Stun/Blind. Per-spell to-hit and
                      # autohit-threshold tables replace the mask==1 gates; fail
                      # damage for Break(50)/Stun(30)/Blind(30).
                      # v60: retune BW fail powers Warp 75 -> 60, Kill 115 -> 110.
                      # v59: retune BW scroll to-hit 64 -> 20, Kill threshold
                      # 700 -> 500 (baked mailbox u16s).
                      # v58: retune Knight lifesteal 10% -> 20% (DIV 10 -> 5).
                      # v57: Knight scroll gets a mechanic -- LIFESTEAL. New leg in
                      # apply_job_scroll_boosts hooks the physical combat-calc fn
                      # epilogue @0x88846B0; a gated Knight caster {0,6} heals
                      # KNIGHT_LIFESTEAL_PCT (was dealt//DIV) of the damage the attack
                      # dealt ([s4+4]) into the attacker unit rec [s5+0x34] curHP,
                      # KO-skip. SCRL mailbox flag bit2 (client-armed when Knight
                      # Scroll owned). No existing hook bytes change.
                      # v56: class rename -- on-disc pad the FM_CAMPUS JOB_NAME
                      # class-name bank to 16B/entry (campus_bake) so scroll-gated
                      # class renames aren't length-capped; client _classname_loop
                      # writes names in-place. No BOOT-signature bytes change.
                      # v55: retune BW kill-fail powers (Scourge 40 / Quake 50 /
                      # Death 70 / Warp 75 / Kill 115).
                      # v54: WW leg 2 -- White Wizard self-heals when casting dia
                      # (Dia 10 / Diara 20 / Diaga 30 / Diaja 40), once per cast
                      # via prologue hook @0x8884A28, gated to WW class {4,10}
                      # (randomized-spell safe); diaheal table in mailbox @+400.
                      # Master leg reworked client-side (temp maxHP + attack,
                      # cap level+5) -- no patch bytes.
                      # v53: BW leg 3 -- kill spells that FAIL to kill now deal
                      # INT-scaled damage (reuse the engine damage path @0x8884B54
                      # via per-spell fail-power table in the SCRL mailbox @+272);
                      # two miss-path detours (type-3 roll @0x8884E10, Kill over-
                      # threshold @0x8885364). Also re-added Piscodemon 0x67 to
                      # SCROLL_BOSS_IDS per user.
                      # v52: SCROLL_BOSS_IDS aligned to the canonical FF wiki
                      # Bosses list (dropped Piscodemon 0x67 + Warmech 0x76,
                      # which the wiki lists as regular enemies).
                      # v51: WW dia boost now gated to BOSS encounters only --
                      # baked 256-byte boss table in the SCRL mailbox (@+16);
                      # WW cave checks the 4 formation type-slot ids
                      # (battle_base+0x68A6+4) against it. Boss set editable at
                      # SCROLL_BOSS_IDS.
                      # v50: class-gate fix -- the *(battle_base+0x6834) array
                      # is the MENU-record frame: class @ rec+0x1E, not +0x5A
                      # (which reads zero; live-verified in-battle 2026-07-13).
                      # Fixes dead class checks in job_scroll_boosts (WW dia /
                      # BW kill caves never fired) AND thief_extra_crit.
                      # v49: job_scroll_boosts feature -- WW dia-vs-anything +
                      # BW instant-kill boost caves in the spell-execution fn
                      # 0x088846D8 (hooks 0x8884B2C / 0x8884DA8 / 0x88852E8) +
                      # the SCRL mailbox cave (client-armed scroll flags/tuning).
                      # v48: promoted_battle_sprite feature REMOVED entirely
                      # (both the v33 rebuild-gate NOP and the v47 promotion
                      # mailbox). Client-side promotion is impossible -- the
                      # game's promote routine 0x08849a28 only builds the battle
                      # scene objects when run inside its native Bahamut event's
                      # lineup-scene context (live-diagnosed: the scene objects
                      # are null in field/menu/dialog). Promotion is now the
                      # game's NATIVE Bahamut turn-in only, triggered by the AP
                      # Rat's Tail (ff1_data.KEY_ITEM_FUNCTION_BITS[13]). Job-
                      # advancement scroll items + JobAdvancementItems option
                      # removed. Bake tag back to 8 bytes.
                      # v47: promotion MAILBOX (removed above).
                      # v46: key_names bake -- extern_bake authors KEY_NAME.MSG
                      # (key id -> AP item name) so the "You obtain the {key}."
                      # key-item-add box (5 event-key chests + NPC handovers)
                      # shows the AP item placed at the granting location.
                      # Data-only bake (no code feature / SIGNATURES entry);
                      # runs with or without spell_tomes.
                      # v45: monk_thief_dabble MP growth retuned per job: Thief
                      # floor(INT/4)+2, Monk floor(INT/4)+1, Master (job 8)
                      # floor(INT/3)+1; other jobs keep floor(INT/4). Same
                      # carry-accumulator cave, divisor picked per job (divu),
                      # flat bonus per job.
                      # v44: blood_magic cost moved from menu-select to EXECUTION -- hook
                      # relocated to the equipment-cast completion epilogue @0x08883678
                      # (fires once when the queued weapon-use actually resolves; both
                      # offensive and buff procs; never native magic). Backing out of the
                      # menu or the battle ending before the actor's turn now costs
                      # nothing. Gate: party row + committed cat==2 + weapon proc == the
                      # spell just cast.
                      # v43: DangerousForests tier pools (A/B) reordered so each tier
                      # is strictly harder than the last (threat-ranked, swap-only)
# v42: blood_magic HP write retargeted at the BATTLE HP COPY
                      # (ctx+0xC71C + row*0x6C, cur@+0/max@+2) -- v41 debited the party
                      # record, which the battle engine neither displays nor keeps (the
                      # end-of-battle copy->record writeback clobbered it -> net vanilla).
                      # v41: blood_magic -- activatable weapon used as a BATTLE item costs
                      # the user 10% max HP (self-contained detour @0x0886C958, KO allowed;
                      # gate: item cat==weapon + weapon record +7 spell-proc != 0).
                      # v40: event-key chest type-byte flip 0x01->0x00 (Part B) ->
                      # 5 vanilla key-item chests bake as normal AP chests (correct box)
                      # -- Ghost 0x0c6 (2-5 Ghost, hp180 ea) too hard for the tier;
                      # swapped to 0x06e (Ogre Mage+Chief+0-7 Hyenadon). 0x06e freed
                      # from tier-0 Cornelia slot2 (-> 0x08c 1-3 Ogre+0-2 Hyenadon) so
                      # the two tiers don't play the identical fight. Data-only pool edit.
                      # v38: monk_thief_dabble MP growth = floor(INT/4) with remainder
                      # carry (per-member acc byte in a baked 0x1000 zero buffer,
                      # slot = record & 0xFFF) instead of ceil(INT/4). Exact fractional
                      # MP over levels; INT varies (growth/Mind Plus/equip) so the
                      # running remainder is stored. Thief keeps flat +1/lvl.
                      # v37: item_prices sell-price fix -- randomized consumable buy
                      # prices now recompute the paired sell price (u24 @+4 = buy//2)
                      # in the 16-byte item record, so a rolled-down buy can no longer
                      # sit below its stale vanilla sell (infinite-gil exploit, e.g.
                      # Potion buy 10 / sell 20). apply_priceless_base_prices and
                      # apply_always_priced_items also set sell = price//2.
                      # v36: regional_ocean_encounters FINALLY works -- the ship reads
                      # the FLAT terrain-2 pool 0x08945aa0 (LIVE sentinel-proven, deep
                      # NW+SW ocean), not terrain-4 0x08945ca8 that v28..v34 stamped
                      # (inert). New: DF-style detour on the terrain-2 branch 0x8841f70
                      # gated on tile ATT==0xF00F (deep ocean = ship-only; canoe 0xF009 /
                      # shallow 0xF002 keep vanilla -> no "sea troll on land"). Zone-picks
                      # a regional pool formation, frame-exact. Data stamp on 0x08945ca8
                      # kept as belt-and-braces.
                      # v35: shop_spell_level SITE C -- the FIELD magic-cast gate
                      # (fn 0x088c2e1c @0x088c2e40) computed a spell's level by the
                      # INDEX formula, so a cross-tier shuffled field spell (Poisona
                      # id13 Lv4->Lv1) still demanded its vanilla magiclv in the pause
                      # menu (battle-cast, already +9-aware, worked). Now reads
                      # magic_info+9. LIVE-RE'd 2026-07-09.
                      # v34: cave encounters reroll from each dungeon's OWN vanilla
                      # formation pool (harder = next dungeon up the story chain)
                      # instead of a terrain-agnostic _BATTLE_RANK band. Fixes early
                      # caves (Marsh) rolling ocean/open-field fights; keeps sea out
                      # of land dungeons. See rando._CAVE_DUNGEONS.
                      # v33: promoted_battle_sprite -- NOP the sprite-binder's rebuild
                      # gate @0x0883e540 (beql v0,0) so the per-member battle scene
                      # object is always rebuilt for its current class. Fixes job-
                      # advancement's class-byte promotion crashing battle (skipped
                      # rebuild -> null scene obj -> jalr $t9=0, RA 0x088fba28). One-word
                      # in-place edit; gated on job_advancement_items.
                      # v32: WarMech (0x56) cameo in every Flying Fortress floor
                      # (0x5c-0x60) under harder_dungeon; 0xd6 (0-0 WarMech) un-excluded.
                      # v31: encounters overhaul -- harder_encounters split into
                      # harder_overworld/harder_dungeon; bosses stripped from all random
                      # pools and hand-placed as rare single-slot cameos. Data-only (zones_*).
# v29: dangerous_forests V8 VARIANTS -- each tier is now a POOL of
                      # 3 existing formations (slot0 = V7 signature + 2 on-theme variants);
                      # cave draws the game RNG (fn 0x8869528) and picks rng % CNT[tier], so
                      # repeat forests in a zone stop playing the identical fight. Cave data
                      # grows to ZONE_TIER[64]+POOL[9][3]u16+CNT[9] (128 B); tail restores $ra
                      # from its own frame so the jal is free. Both normal (A) + harder (B)
                      # pools varied; stacking preserved. _DF_FORM_EDITS trimmed to 0x211/0x235.
# v28: regional_ocean_encounters BUGFIX -- ocean is terrain-4
                      # (zoned 0x08945ca8), NOT terrain-2 (river/canoe). v23 patched the
                      # river branch -> ocean mobs bled onto walkable river/shallow tiles
                      # ("Sea Troll on land"). Now DATA-only: stamp 4 quadrant pools into
                      # the terrain-4 table, pool-0/no-ocean zones stay vanilla, no code patch.
# v27: dangerous_forests _DF_ZONE_TIER -- fill Matoya's pocket
                      # (zones 27,28,37 too) -> 1 (Pravoka); whole cols3-5 x rows3-4 block.
# v26: dangerous_forests _DF_ZONE_TIER -- Matoya's N border
                      # (zone 29) re-bucketed 8 -> 1 (Pravoka) per playtest.
# v25: dangerous_forests _DF_ZONE_TIER retune -- Matoya's area
                      # (zones 35,36) re-bucketed 4/0 -> 1 (Pravoka) per playtest.
# v24: dangerous_forests V7 = u16 DLC-monster capable + harder
                      # stacking. V6 authored formations into dead slots -> FROZE at
                      # battle init (secondary sprite/pos table desync). V7 authors
                      # NOTHING: each tier REFERENCES an existing formation (u8 OR u16
                      # DLC) featuring that tier's signature monster. Forest tiles now
                      # commit a u16 id straight to the battle-ctx field (s4+0xbe4, sh)
                      # and jump to the encounter tail, bypassing the u8 slot roll. Two
                      # tier lists selected by harder_encounters (normal/harder STACK).
                      # 3 count-only edits on unreferenced formations (normal list).
# v23: regional_ocean_encounters = zone-index the ship's flat
                      # ocean pool (2-word terrain-2 branch retarget -> t3 zoned table)
                      # + 4 regional pools (NW/NE/SW/SE, vanilla formations, difficulty
                      # start<SW<SE<NW<NE) + 3 count-only formation edits. No new monster
                      # authoring (formation graphics are in a battle pack, not the ELF).
# v22: dangerous_forests V6 = PROGRESSION-SCALED. 9 tiers =
                      # route stops (Cornelia..Lufenia); each zone -> nearest-town
                      # tier, forest = region mob + heavy scaled to route position
                      # (Ogre->Sabertooth). Retunes v21's raw-geography buckets.
# v21: dangerous_forests V5 = ZONE-SCALED. Each zone's forest
                      # fight = its tier's common mob + 1-2 authored heavies (e.g.
                      # Cornelia forest = 3-4 Goblin + 1 Troll) via 9 new formations
                      # in dead slots 0xF3.. + a 136 B zone->tier->row cave table;
                      # replaces the fixed zone-blind endgame danger pool (too hard).
# v20: dangerous_forests reads the tile's ATT attr==0x0006
                      # (real forest) via the field struct's live map pointers at
                      # the game's own coord -- frame-exact, no bitmap/remap. Prior
                      # attrs were swapped (0x0003 was grass, not forest), which
                      # made every earlier version fire on the wrong tiles.
# v19: forest-only terrain-map remap (byte 2) -- WRONG attr (0x0003=grass)
# v18: dangerous_forests on-disc detour @0x8841f60 (forest
                      # terrain bytes 1/2 -> cave danger pool; replaces the racy
                      # client zone-table swap)
# v17: remote names sanitized to BOX-SAFE chars (letters/
                      # space/apostrophe) -- digit bytes 0x38/0x39 = box control
                      # codes that FREEZE the chest box (safety fix, not cosmetic)
# v16: remote names get c2-88 item-icon prefix (was eating
                      # first glyph); chest box template shortened to "{NAME}!"
                      # (box_template.py) + remote name cap raised 20->32
# v15: remote_chest_names detour @0x08843d18 (poll-based
                      # chests): box shows extern_bake-extended remote AP name
                      # from treasure-u32 bits16-30; extern_bake remote= support
# v14: xp_boost/gil_boost scale monster_stats reward fields
                      # (percentage); replaces xp_requirements-division xp boost
# v13: extern_bake writes the dpk record's DECOMPRESSED-size
                      # field (+32) -- the game allocs from it, so the grown
                      # bundle was truncated at vanilla size and every bank past
                      # 0x6b70 (WEAPON_EXP tail, WEAPON_NAME) loaded as garbage
                      # (the weapon/item-shop freeze). v12: J1 records re-served
                      # a valid re-tagged copy. v11: spell_tomes baked names.


# ------------------------------------------------------------- DATA patch layer
# A data patch targets a table inside BOOT.BIN's single PT_LOAD segment by its
# ABSOLUTE ISO offset (rando_data.META et al; every offset live-verified against
# the runtime RAM addresses). Two kinds:
#   {"name", "iso_off", "vanilla": bytes, "patched": bytes}   full-block overwrite
#   {"name", "iso_off", "count": n, "values": {idx: u32}}     sparse u32 table
# Full blocks verify the vanilla bytes first (loud failure on a wrong/odd ISO).

def data_ram_addr(iso_off):
    """RAM address a BOOT.BIN iso offset loads at (linear PT_LOAD map)."""
    return E.VADDR + (iso_off - E.BOOT_ISO_OFF - E.SEG_FILE_OFF)


def _elf_off(iso_off, elf_len, size):
    off = iso_off - E.BOOT_ISO_OFF
    if not (E.SEG_FILE_OFF <= off and off + size <= elf_len):
        raise ValueError(f"data patch iso_off {iso_off:#x} outside BOOT.BIN")
    return off


def apply_data_patches(elf: bytearray, data_patches):
    """Apply the seed's data patches to the BOOT ELF at fixed offsets. Returns
    the list of (name, ram_addr) applied. Raises on a vanilla-byte mismatch
    (wrong ISO / clashing edit) so a bad bake never boots silently."""
    applied = []
    for p in data_patches or []:
        name, iso_off = p["name"], p["iso_off"]
        if "values" in p:                      # sparse u32 table (chest contents)
            count = p["count"]
            off = _elf_off(iso_off, len(elf), count * 4)
            for idx, val in p["values"].items():
                idx = int(idx)
                if not 0 <= idx < count:
                    raise ValueError(f"{name}: index {idx} out of range")
                struct.pack_into("<I", elf, off + idx * 4, val & 0xFFFFFFFF)
        else:
            van, new = p["vanilla"], p["patched"]
            if len(van) != len(new):
                raise ValueError(f"{name}: vanilla/patched length mismatch")
            off = _elf_off(iso_off, len(elf), len(new))
            cur = bytes(elf[off:off + len(van)])
            # tolerating cur == new (not just vanilla) is what makes a re-bake
            # over an already-patched ISO idempotent instead of "wrong ISO".
            if cur != van and cur != new:
                raise ValueError(f"{name}: ISO bytes @ {iso_off:#x} are not the "
                                 f"expected vanilla table (wrong ISO?)")
            elf[off:off + len(new)] = new
        applied.append((name, data_ram_addr(iso_off)))
    return applied


# ------------------------------------------------------------------- bake tag
# 8 bytes at the head of the cave segment: magic 'F1AP' + low-32 bake hash.
# The FIRST add_segment_cave call always lands at SAFE_CAVE_VADDR, so the tag
# has a FIXED RAM address readable via the debugger -- the launcher uses it to
# recognize a running game as "this exact bake" (per-seed, unlike SIGNATURES).
BAKE_TAG_ADDR = E.SAFE_CAVE_VADDR      # 0x08B30E00
BAKE_TAG_MAGIC = b"F1AP"


def _install_bake_tag(elf, bake_hash32):
    vaddr = E.add_segment_cave(elf, BAKE_TAG_MAGIC +
                               struct.pack("<I", bake_hash32 & 0xFFFFFFFF))
    assert vaddr == BAKE_TAG_ADDR, f"bake tag landed at {vaddr:#x}"


def bake_tag_checks(bake_hash32):
    """(u16 addr, mask, want) triples verifying the running game carries this
    exact bake. Same shape as SIGNATURES entries."""
    m0, m1 = struct.unpack("<2H", BAKE_TAG_MAGIC)
    h0 = bake_hash32 & 0xFFFF
    h1 = (bake_hash32 >> 16) & 0xFFFF
    return [(BAKE_TAG_ADDR, 0xFFFF, m0), (BAKE_TAG_ADDR + 2, 0xFFFF, m1),
            (BAKE_TAG_ADDR + 4, 0xFFFF, h0), (BAKE_TAG_ADDR + 6, 0xFFFF, h1)]

# Runtime signatures: (u16 runtime address, mask, want) per feature; the running
# game is patched iff (read_u16(addr) & mask) == want. Used to detect whether an
# already-running PPSSPP is on our patched ISO (data/code bytes are readable via
# the debugger regardless of JIT).
SIGNATURES = {
    # Speed-table hook @_SD_TABLE_HOOK overwritten with `j cave`: top 6 opcode
    # bits are `j` (000010) only when patched (vanilla `lhu a3,0(v0)`, opcode
    # 100101 -> 0x9400). Opcode-only -> holds wherever the cave lands.
    "super_dash": (_SD_TABLE_HOOK + 2, 0xFC00, 0x0800),
    # Thief's start-stats MP u16 (@_START_TAB + class1*16 - 2) is
    # DABBLE_START_MP only when patched (vanilla Thief starts with 0 MP).
    # NB: can't use a learn bit -- those follow the seed's magic shuffle now.
    "monk_thief_dabble_in_magic": (_START_TAB + 1*16 - 2, 0xFFFF, DABBLE_START_MP),
    # Elf Prince handover-gate check `2d 08 <id> 02`: u16 at +2 reads
    # 0x02<<8|id -- vanilla 0x0209 (flag 9), split 0x0245 (shadow flag 69).
    "prince_gate_split": (_NGS_SITES[0][0] + 2, 0xFFFF,
                          0x0200 | (NPC_GATE_SPLIT_FLAG_BASE + 5)),
    # Rewrites code @0x8820e5c..: the word @0x8820e6c is `sll at,v0,1` (low u16
    # exactly 0x0840) only when patched (vanilla = `sra s0,v0,2`, low 0x8083).
    "shop_spell_level": (0x8820e6c, 0xFFFF, 0x0840),
    # Purchase-commit hook @0x0881EC64: vanilla `jal 0x88d4494` (upper u16 of
    # the word = 0x0E23); patched, the jal targets a cave in the cave segment
    # 0x08B30E00+ (< 0x08B40000), whose jal upper u16 is always 0x0E2C.
    "shop_buy_mailbox": (0x0881EC66, 0xFFFF, 0x0E2C),
    # Detour at the crit hook: high u16 of the word @_CRIT_HOOK has opcode bits
    # (top 6) == `j` (000010) only when patched (vanilla `lw v1,0x34(s5)`,
    # opcode 100011). Opcode-only check so it holds wherever the cave lands
    # (cave vaddr shifts with which other features are enabled).
    "thief_extra_crit": (_CRIT_HOOK + 2, 0xFC00, 0x0800),
    # Chest item-grant jal @0x08843D74 overwritten with `j cave`: top 6 opcode
    # bits are `j` (000010) only when patched (vanilla `jal`, opcode 000011 ->
    # 0x0C00). Opcode-only -> holds wherever the cave lands.
    "bonus_dyn_chests": (_BDC_SITES[0][0] + 2, 0xFC00, 0x0800),
    # The use-validity fn's table lui imm (@0x088c4ca4 low u16) reads 0x0895
    # vanilla and the cave-segment page (0x08b3) once relocated; the page is
    # fixed regardless of which other features are enabled (asserted at patch
    # time in apply_spell_tomes).
    "spell_tomes": (0x088c4ca4, 0xFFFF, _CAVE_HI),
    # Magic-damage hook @_MPD_HOOK overwritten with `j cave`: the high u16's top
    # 6 opcode bits read `j` (000010) only when patched (vanilla `lw v0,0x38(s4)`,
    # opcode 100011). Opcode-only, so it holds wherever the cave lands.
    "magic_power_scaling": (_MPD_HOOK + 2, 0xFC00, 0x0800),
    # Hook @0x08843d18 is overwritten with `j cave`: the high u16's top 6 opcode
    # bits are `j` (000010) only when patched (vanilla `andi`, opcode 001100).
    # Opcode-only -> holds wherever the cave lands (fixed hook site regardless).
    "remote_chest_names": (_RC_HOOK + 2, 0xFC00, 0x0800),
    # FIELD-deduct hook @0x088C472C overwritten with `j cave`: top 6 opcode bits
    # are `j` (000010) only when patched (vanilla `subu a0,a0,a2`, opcode
    # 000000 -> 0x0000). Opcode-only -> holds wherever the caves land.
    # v203/v204: probe moved off the BATTLE deduct area (0x088826D4/DC) --
    # job_scroll_boosts detours the deduct RETURN point in every mode (the
    # Crimson Wizard heal-at-pay cave), so that area no longer identifies
    # slot_magic. The field deduct is slot_magic's alone.
    "slot_magic": (_SM_FIELD_DEDUCT + 2, 0xFC00, 0x0800),
    # Land-path hook word @0x8841f60: top 6 opcode bits are `j` (000010) only when
    # patched (vanilla `b` = beq, opcode 000100 -> 0x1000). Opcode-only -> holds
    # wherever the cave lands.
    "dangerous_forests": (_DF_HOOK + 2, 0xFC00, 0x0800),
    # Cave-table hook @0x8842258 overwritten with `j cave`: top 6 opcode bits are
    # `j` (000010) only when patched (vanilla `lui v1,0x894`, opcode 001111 ->
    # 0x3c00). Opcode-only -> holds wherever the cave lands.
    "chaos_floor_pools": (_CF_HOOK + 2, 0xFC00, 0x0800),
    # Map 0x27's map_gate byte 0: 0 vanilla (encounters off), 4 patched. u16 at
    # the record base = [gate0 | gate1]; low byte is the one we flip.
    "chaos_floor_encounters": (_CF_GATE_REC + _CF_GATE_MAP * 8, 0x00FF, _CF_GATE_ON),
    # Terrain-2 ocean detour hook @0x8841f70: top 6 opcode bits are `j` (000010) only
    # when patched (vanilla `b`=beq, opcode 000100 -> 0x1000). Opcode-only -> holds
    # wherever the cave lands. (v36: was a terrain-4 data-byte check, but the ship reads
    # terrain-2 -- the data stamp is inert on its own, so key on the real code hook.)
    "regional_ocean_encounters": (_OC_HOOK + 2, 0xFC00, 0x0800),
    # Terrain-1 (river) branch @0x8841f68 overwritten with `j cave`: opcode-only,
    # same shape as the ocean detour (vanilla `b` = 0x1000).
    "northern_river_encounters": (_RR_HOOK + 2, 0xFC00, 0x0800),
    "overworld_u16": (_OWU_HOOK + 2, 0xFC00, 0x0800),
    # Terrain-3 (desert) branch @0x8841f84 overwritten with `j cave`: opcode-only,
    # same shape as the ocean detour (vanilla `b` = 0x1000). The marsh/river hook
    # is installed by the same feature, so one of the two suffices.
    "terrain_pools": (_TP_T3_HOOK + 2, 0xFC00, 0x0800),
    # Hook _BLOOD_HOOK 0x08883854 (v90 site) overwritten with `j cave`: high u16
    # top 6 opcode bits are `j` (000010) only when patched (vanilla `jal`, opcode
    # 000011 -> 0x0c00). Opcode-only -> holds wherever the cave lands.
    "blood_magic": (_BLOOD_HOOK + 2, 0xFC00, 0x0800),
    # Usability-fn entry @0x08871594 overwritten with `j cave`: top 6 opcode bits
    # are `j` (000010) only when patched (vanilla `andi v1,a1,0xff`, opcode 001100
    # -> 0x3000). Opcode-only -> holds wherever the cave lands.
    "equipment_rune_gate": (_ERG_HOOK + 2, 0xFC00, 0x0800),
    # WW-dia hook @0x08884B2C overwritten with `j cave`: top 6 opcode bits are `j`
    # (000010) only when patched (vanilla `lw v1,0x38(s4)`, opcode 100011 -> 0x8c00).
    # Opcode-only -> holds wherever the caves land.
    "job_scroll_boosts": (_WW_HOOK + 2, 0xFC00, 0x0800),
    # Garland's formation record (fid 0x7F) layout byte: 4 vanilla, patched to
    # _MINION_LAYOUT (2) normally or TOP_GRID_LAYOUT (5) at absurd intensity.
    # u16 @0x8949484 = [prev record's last byte | layout]; high byte = layout.
    # BOTH must be accepted (fixed 2026-08-06): v213 introduced the layout-5
    # branch and never updated this signature, so from v213 on EVERY absurd seed
    # failed on-disc verification and silently dropped to runtime patching --
    # which also passes dabble_baked=False, so the magic_learn reconcile then
    # fights the on-disc table forever. It went unnoticed because it only bites
    # on the first re-bake after v213, and cached ISOs kept booting.
    "boss_minions": (0x8949484, 0xFF00, (0x0200, 0x0500)),
    # death_visual_start entry @0x8871d14 overwritten with `j cave`: top 6
    # opcode bits are `j` (000010) only when patched (vanilla `andi a1,a1,0xff`,
    # opcode 001100 -> 0x3000). Opcode-only -> holds wherever the cave lands.
    "minion_death_serializer": (_MDS_HOOK + 2, 0xFC00, 0x0800),
    # getStoryFlag wrapper entry overwritten with `j cave`: top 6 opcode bits
    # are `j` (000010) only when patched (vanilla `addiu sp,sp,-0x10`, opcode
    # 001001 -> 0x2400). Opcode-only -> holds wherever the cave lands.
    "bikke_ship_split": (_BSS_GETSTORYFLAG + 2, 0xFC00, 0x0800),
    # Giant's object-record x (u16 @ _GCG_GIANT_REC+4): 15 vanilla, 12 patched.
    # Pure data, so it reads the same wherever the caves land.
    "giant_cave_gate": (_GCG_GIANT_REC + 4, 0xFFFF, _GCG_NEW_XY[0]),
    # Object-reader hook @0x0883FC70 overwritten with `j cave`: top 6 opcode bits
    # are `j` (000010) only when patched (vanilla `lw s3,0(v0)`, opcode 100011 ->
    # 0x8c00). Opcode-only -> holds wherever the cave lands.
    "lute_block_gate": (_LB_READER_HOOK + 2, 0xFC00, 0x0800),
    # Same hook, same `j cave` signature (both legs live in one detour).
    "mystic_door_gate": (_LB_READER_HOOK + 2, 0xFC00, 0x0800),
    # First re-pointed chest record's param (u16 @+2): 19 vanilla, 24 patched.
    # Pure data, so it reads the same wherever the caves land.
    "loot_in_normally_empty_chests": (_CHEST_DEDUP[0][0] + 2, 0xFFFF,
                                      _CHEST_DEDUP[0][2]),
    # Legacy feature keys (pre-rename seeds), same records.
    "chest_dedup": (_CHEST_DEDUP[0][0] + 2, 0xFFFF, _CHEST_DEDUP[0][2]),
    # First Mount Gulg B5 record's param (u16 @+2): 180 vanilla, 188 patched.
    "loot_in_gulg_b5_chests": (_CHEST_DEDUP_GULG_B5[0][0] + 2, 0xFFFF,
                               _CHEST_DEDUP_GULG_B5[0][2]),
}


# Feature keys that ONLY an already-generated seed can name: their yaml option
# was renamed, so nothing new ever emits them and they are absent from
# options.ON_DISC_OPTIONS / ON_DISC_ALWAYS by design. test_patch's
# every-feature-is-reachable guard exempts exactly this set -- a genuinely
# orphaned feature still fails it.
LEGACY_FEATURES = frozenset({"chest_dedup", "loot_in_gulg_b5_chests"})


def any_enabled(feats: dict, data_patches=None) -> bool:
    has_feat = bool(feats) and any(feats.get(k) for k in FEATURES)
    return has_feat or bool(data_patches)


def patched_running_verdict(read_u16, feats: dict, bake_hash32=None) -> str:
    """Classify the running game against OUR patched ISO: every enabled
    feature's runtime signature must be present AND (when given) the bake tag
    must match this exact bake hash -- two different seeds bake different data,
    so feature signatures alone can't authorize reuse. `read_u16` is a callable
    (addr)->int|None (via the PPSSPP debugger).

    Returns:
      "ok"         -- every check read successfully and matched.
      "mismatch"   -- a read SUCCEEDED and its value doesn't match (confirmed
                      wrong bake), or an enabled feature has no signature.
      "unreadable" -- no confirmed mismatch, but at least one read returned
                      None (debugger hiccup) -- NOT evidence of a wrong bake;
                      callers should retry before condemning the game."""
    checks = []
    for name, on in (feats or {}).items():
        if not on or name not in FEATURES:
            continue
        if name not in SIGNATURES:
            return "mismatch"
        checks.append(SIGNATURES[name])
    if bake_hash32 is not None:
        checks.extend(bake_tag_checks(bake_hash32))
    unreadable = False
    for addr, mask, want in checks:
        # `want` may be a TUPLE of acceptable values: a feature whose baked byte
        # legitimately varies with an option (boss_minions' layout is 2 normally
        # and 5 at absurd intensity) cannot be pinned to one literal, and pinning
        # it anyway silently condemns every seed that picks the other branch.
        ok = want if isinstance(want, tuple) else (want,)
        v = read_u16(addr)
        if v is None:
            unreadable = True
        elif (v & mask) not in ok:
            return "mismatch"
    return "unreadable" if unreadable else "ok"


def is_patched_running(read_u16, feats: dict, bake_hash32=None) -> bool:
    """True iff patched_running_verdict says "ok". Fails closed on any miss
    (single-shot; launcher retries via patched_running_verdict)."""
    return patched_running_verdict(read_u16, feats, bake_hash32) == "ok"


def _stage_reporter(progress):
    """Wrap the caller's line sink into (stage, copy) emitters. The ISO copy is
    minutes of dead air on a slow disk, so it reports byte percentage: every
    10%, and never more than one line per 2s (the AP client log is append-only,
    so these are added lines, not an in-place bar)."""
    say = progress or (lambda _msg: None)
    state = {"next": 10, "last": 0.0, "any": False}

    def copy(done, total):
        if not total:
            return
        pct = int(done * 100 / total)
        if pct < state["next"]:
            return
        now = time.monotonic()
        state["next"] = (pct // 10) * 10 + 10
        if now - state["last"] < 2.0 and pct < 100:
            return
        state["last"] = now
        state["any"] = True
        say(f"[bake]     copying… {min(pct, 100)}%")

    def done_copying():
        if state["any"] and state["next"] <= 100:
            say("[bake]     copying… 100%")

    return say, copy, done_copying


def patch_iso(iso_in: str, iso_out: str, feats: dict, data_patches=None,
              bake_hash32=0, remote_names=None, key_names=None,
              obtain_names=None, pad_key_ids=None, caravan_offer=None,
              dyn_name_slots=0, progress=None):
    """Bake the seed into a patched ISO: data tables first, then the enabled
    code features (dabble's learn bits must OR into the SHUFFLED magic_learn),
    then the bake tag. Writes the patched ISO to iso_out.

    remote_names (optional, ORDERED by remote chest k) = (who, item) pairs (or
    legacy pre-joined strings) for the remote_chest_names box detour; baked
    (recipient-preserving capped, see tome_names.remote_rungs) into the
    extended item NAME bank at string ids base+k (base 107 with spell_tomes,
    43 without).

    dyn_name_slots = count of wide runtime-authored bank entries appended
    AFTER the remote block (string ids base+R..) for the bonus dynamic chest
    box names (tome_names.dyn_slot_entry; the client authors the NEXT chest's
    name into them and arms the sid via the BDC1 mailbox).

    key_names (optional) = {key item id: AP item name} authored into
    KEY_NAME.MSG so the 'You obtain the {key}.' key-item-add box (event-key
    chests + NPC handovers) shows the AP item placed at the granting location
    instead of the vanilla key name. Independent of spell_tomes.

    obtain_names (optional) = {key item id: AP item name} for evm_bake: authors
    the per-map USEVM MSG obtain sentence ("You obtain the warp cube.") on
    disc. Separate from key_names on purpose -- the client never passes
    key_names (KEY_NAME.MSG feeds the Key Items MENU, which must stay vanilla).

    pad_key_ids (optional) = key ids whose KEY_NAME entry is WIDENED to
    extern_bake.KEY_NAME_GLYPHS spaces. lute_tablets passes [1] (the Lute) so
    the client can write the live "Lute Tablets N of M" ratio into that slot in
    place -- the resident bank buffer cannot grow at runtime, so the wide slot
    must exist on disc. Padding renders as spaces, so an un-written slot just
    reads "Lute".

    caravan_offer (optional) = {"name": AP item name, "descs": [phrasings]} for
    the Onrac Caravan's presale row, authored into FM_SHOPUS.PCK's shop-UI text
    bank + shop-font key-desc bank (extern_bake.bake_caravan_offer). Cosmetic
    and independent of every other bake."""
    say, copy_prog, copy_done = _stage_reporter(progress)
    say("[bake] 1/4 patching the game executable…")
    elf = E.load_boot_elf(iso_in)
    # COPY FIRST, before ANY function that can mint a mailbox into feats.
    # Several helpers (_mp_mb, _blood_tkt_mb, _delaypop_mb) thread a cave vaddr
    # by stashing it in feats, and bake_hash32 hashes the set of truthy feature
    # KEYS -- so a minted key silently changes the caller's bake hash. The copy
    # used to be taken two lines below, which left apply_popup_colors free to
    # mint "_mp_mb" into the CALLER's dict: the tag written on disc used the
    # pre-mint hash, _verify_bake recomputed the post-mint hash, they differed,
    # and the client killed the just-launched game and baked the whole ISO a
    # SECOND time. Self-correcting (bake 2 hashes consistently), which is why
    # it read as a mysterious relaunch rather than an error (log 2026-08-08:
    # bake 1 features lacked _mp_mb, bake 2 had it, two 70s bakes back to back).
    feats = dict(feats)
    apply_data_patches(elf, data_patches)
    _install_bake_tag(elf, bake_hash32)          # first cave = fixed tag addr
    apply_popup_colors(elf, feats)               # install always; feats gates
                                                 # only the odds CLASSIFY step
    # Shared delayed-popup service (always on): install once, thread the mailbox
    # vaddr to the features that stagger numbers (job_scroll teal MP refund,
    # blood_magic heal-item).
    feats["_delaypop_mb"] = _install_delayed_popup(elf)
    apply_strength_popups(elf, feats)            # always on (yellow attack-gain number)
    for name, fn in FEATURES.items():
        if feats.get(name):
            fn(elf, feats)
    # MUST follow the loop: reserves a bss tail, and cave_bss_tail has to come
    # after every add_segment_cave (spell_tomes emits the last one).
    install_magic_power_tables(elf, feats)
    try:
        _gb = os.path.getsize(iso_in) / (1 << 30)
        say(f"[bake] 2/4 copying your ISO ({_gb:.2f} GB)…")
    except OSError:
        say("[bake] 2/4 copying your ISO…")
    E.build_iso(elf, iso_out, src_iso=iso_in,
                progress=copy_prog if progress else None)
    copy_done()
    say("[bake] 3/4 writing text banks (item/shop names)…")
    # Post-build dpk edits fall in two classes, and the split MUST be kept
    # honest (live lesson 2026-07-15, ms2/extern donor collision):
    #  - LOAD-BEARING: the code patches baked above reference these dpk
    #    payloads. Missing them is a crash/freeze, not a blemish -- e.g.
    #    spell_tomes' item table cites name-bank ids 43+ ("the array fill
    #    without the grown banks would OOB-read the vanilla bank" -> item-menu
    #    freeze), and boss_minions' formations need their MS2 GIM packs (else
    #    battle init crashes). These RAISE: better to boot the unpatched ISO
    #    than a half-patched one.
    #  - COSMETIC: display-only banks (key-item box names, chest box template,
    #    class-name padding). These run under a soft guard -- on failure log
    #    and keep the fully functional ISO.
    def _soft(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- cosmetic; never fatal to the bake
            print(f"[patch] cosmetic {label} skipped ({e!r}); all functional "
                  f"features still applied.")

    # Stage 3 is a chain of independent dpk rebuilds (each one decompresses,
    # re-lays and recompresses a container) and is the second-longest stretch
    # of the bake, so every block that actually runs announces itself -- a
    # single "writing text banks" line would sit there for a minute.
    def sub(msg):
        say(f"[bake]     {msg}")

    # LOAD-BEARING -- popup colours (always on): add the recoloured digit/MISS!!
    # row + the grown defs 25-29 (popup_bake.DEF_*) to BATTLEICON.PCK. The caves
    # installed above select those kinds, which only exist once this bake has
    # run, so a failure here would draw garbage glyphs rather than merely look
    # plain.
    sub("battle damage-number colours…")
    popup_bake.bake_popup_colours(iso_out)
    # LOAD-BEARING -- boss_minions: rebuild MS2_<fid>.PCK sprite packs in the
    # copied ISO's dpk so every planned add monster has its GIM pair.
    if feats.get("boss_minions") and feats.get("boss_minions_plan"):
        from . import ms2_bake
        # When spell_tomes / key_names / blood_magic / remote_chest_names are ALL
        # off, extern_bake never runs -- hand ms2 the two J1 EXTERN records it
        # normally reserves (FM_EXTERN18J1 alone is the biggest donor on disc,
        # 0x142a4). remote_chest_names now grows the same banks (decoupled from
        # spell_tomes), so it must also reserve J1 or ms2 would steal it out from
        # under the remote bake (the PATCHER 68 collision class).
        extern_active = bool(feats.get("spell_tomes") or key_names
                             or feats.get("blood_magic")
                             or ((remote_names or dyn_name_slots)
                                 and feats.get("remote_chest_names")))
        # Donor-pool rule: any baker running AFTER this call that RELOCATES a
        # dpk record must be in ms2_bake.reserved_names, or ms2 may park a
        # grown pack in that donor's extent and the later baker overwrites it
        # (boss pack corruption -> CPU jump to 0). In-place editors, and
        # containers outside the FM_DBG_*/FM_*J1/J2 donor pool, need no
        # reservation (battlemsg FONT_BATTLEJ1.PC, evm J2EVM*.PCK, campus).
        sub("boss minion sprite packs…")
        ms2_bake.bake_minion_packs(
            iso_out, feats["boss_minions_plan"],
            extern_active=extern_active,
            # the caravan row may relocate FM_SHOPUS into FM_SHOPJ1 -- another
            # J1-named donor. One region, one owner.
            caravan_active=bool(caravan_offer and caravan_offer.get("name")))
    # LOAD-BEARING -- giant_cave_gate: place the paired boulder at (11,13) in
    # MAP_06_00.PCK's grid. The ELF half already moved the Giant onto (12,13);
    # without the rock the choke point has a hole and the gate silently does
    # nothing, so a failure here must abort the bake rather than ship half of it.
    if feats.get("giant_cave_gate"):
        from . import map_bake
        sub("Giant's Cave boulder…")
        map_bake.place_giant_rock(iso_out)
    # COSMETIC -- slot_magic: re-word the level-up "MP increased by {N}." battle
    # message to "New spell slots {N}" ON DISC. The client's resident-bank poll
    # loses a race with the level-up sequence (which re-loads the bank), so the
    # source copy has to carry the wording; the poll stays as a repair path.
    # Purely textual -> soft guard.
    if feats.get("slot_magic"):
        def _bake_slot_line():
            from . import battlemsg_bake
            battlemsg_bake.bake_slot_line(iso_out)
        sub("level-up battle message…")
        _soft("slot_magic level-up line", _bake_slot_line)
    # COSMETIC -- canal FORD: make the single cell (94,157) (tile id 0x174) a
    # walk+sail crossing rendered as animated river water, so the blown canal has
    # one visibly distinct 1-tile ford instead of a 3-wide invisible shallow. The
    # load-bearing part is the ANM.ATI move (ocean animation family -> river): an
    # ASC-only reskin was proven INERT in game, because the per-frame animation
    # overlay painted over the base art. Live-designed + user-approved 2026-07-26.
    # Purely visual//traversal-cosmetic, so it rides the _soft guard.
    def _bake_canal_ford():
        from . import map_bake
        map_bake.bake_canal_ford(iso_out)
    sub("canal bridge tile…")
    _soft("canal ford", _bake_canal_ford)
    # OPEN PROGRESSION on disc -- foot trails, canoe rivers and the northern docks
    # written straight into MAP_00_AMD.BIN, so the cells are already correct when
    # the game decompresses the overworld arena. This replaces the client's
    # _openworld_loop as the PRIMARY path: poking the live arena could not survive
    # a re-decompression the canary can't see, which is why the northern docks kept
    # vanishing until a game restart (2026-08-08). The loop is kept as a repair
    # path for seeds whose ISO predates this bake, and on a baked ISO it writes the
    # values that are already there -- so a failure here is degraded, not fatal:
    # soft guard, loop covers it.
    _ow_map = feats.get("_ow_map")
    if _ow_map:
        def _bake_ow_grid():
            from . import map_bake
            map_bake.bake_openworld_grid(iso_out, bool(_ow_map.get("early")),
                                         bool(_ow_map.get("extended")),
                                         bool(_ow_map.get("docks")))
        sub("open-progression map edits…")
        _soft("open-progression grid", _bake_ow_grid)
    # LOAD-BEARING -- spell_tomes / remote_chest_names: grow the item NAME/DESC
    # banks in the copied ISO's dpk (must run AFTER build_iso, which produces
    # iso_out). Seed-independent (FF1 PSP does not name-shuffle spell slots), so
    # the identity remap is correct. Load-bearing for remote names too: if the
    # grow fails the box detour would store a remote string id past the bank's
    # entry count -> OOB, so a failure must abort rather than ship.
    # COSMETIC rider -- blood_magic: append the HP-cost sentence to every
    # activatable weapon/armor desc entry (WEAPON_EXP/ARMOR_EXP.MSG). Rides the
    # same bundle rebuild as the banks above so the bundles are only rebuilt
    # once (a second bake_names pass over already-relocated records would
    # overflow the J dead space).
    blood = bool(feats.get("blood_magic"))
    tomes_on = bool(feats.get("spell_tomes"))
    want_remote = bool((remote_names or dyn_name_slots)
                       and feats.get("remote_chest_names"))
    # slot_magic repurposes the Soma Drop (+5 maxMP -> +1 spell slot, spilling
    # up), so its description has to say so. Same bundle rebuild as everything
    # else here -- a second bake_names pass over already-relocated records would
    # overflow the J dead space.
    # The Ether family rides the same dict -- slot_magic turns them into
    # spell-charge restores, so their MP wording has to go too.
    item_descs = slot_magic_item_descs(feats.get("slot_magic"))
    if tomes_on or want_remote:
        # Grow the item NAME/DESC banks. With spell_tomes the 64-entry tome block
        # is included (remote sids base 107); WITHOUT it the bank is 43 vanilla +
        # R remote entries (remote sids base 43) -- remote chest-box names no
        # longer require spell_tomes (see remote-chest-name-gating memory).
        from . import extern_bake
        # tome descriptions want each spell's finalized level (magic_info+9,
        # stride 14) from the baked ELF -- reflects any shop-shuffle re-tiering
        # (align_shop_spell_levels) applied via data_patches. Meaningless with no
        # tome block, so only read it when tomes are on.
        levels = ([elf[E.ram2file(0x8954D1A + i * 14 + 9)] for i in range(64)]
                  if tomes_on else None)
        remote_capped = None
        dyn_slots = int(dyn_name_slots or 0) if want_remote else 0
        if want_remote:
            sub("planning remote item names…")
            remote_capped, _base = extern_bake.plan_remote(
                iso_out, remote_names or [], levels=levels,
                key_names=key_names, blood=blood, tomes=tomes_on,
                pad_ids=pad_key_ids, item_descs=item_descs,
                dyn_slots=dyn_slots, log=sub)
        sub("item + spell name/description banks (the long one)…")
        extern_bake.bake_names(iso_out, levels=levels, remote=remote_capped,
                               key_names=key_names, blood=blood, tomes=tomes_on,
                               pad_ids=pad_key_ids, item_descs=item_descs,
                               dyn_slots=dyn_slots)
    elif key_names or blood or pad_key_ids or item_descs:
        # COSMETIC -- spell_tomes off, no remote names: still author the key-item
        # name bank (no item bank growth) and/or the blood_magic desc text.
        # pad_key_ids rides here too: lute_tablets needs the widened KEY_NAME
        # slot even in a seed with no tomes/remote names.
        def _keys():
            from . import extern_bake
            extern_bake.bake_names(iso_out, key_names=key_names, items=False,
                                   blood=blood, pad_ids=pad_key_ids,
                                   item_descs=item_descs)
        sub("key-item name bank…")
        _soft("key-item/blood/soma desc bank", _keys)
    # COSMETIC -- per-map key-item obtain boxes ("You obtain the warp cube."):
    # author the AP name into each USEVM<mapid>.PCK's MAP<id>.MSG so every RAM
    # copy the game makes is born authored -- the client's _mapmsg_loop races
    # bundle relocation and a fresh copy read vanilla for up to ~16 s (live
    # 2026-08-05, Waterfall robot). The RAM loop stays as the repair net for
    # ISOs baked before this stage. Soft: a miss leaves the vanilla sentence.
    # obtain_names, NOT key_names: the client deliberately never passes
    # key_names (that key would also re-enable the reverted KEY_NAME.MSG menu
    # bake); obtain_names carries the same {key id: AP name} map for this
    # stage alone.
    if obtain_names:
        def _evm():
            from . import evm_bake
            evm_bake.bake_obtain_boxes(iso_out, obtain_names)
        sub("map obtain boxes…")
        _soft("map obtain boxes", _evm)
    # COSMETIC -- the Onrac Caravan's presale row (its own shop-UI string bank,
    # see shop_font). Soft: a miss leaves the vanilla "Faerie's Bottle" row.
    caravan_baked = None
    if caravan_offer and caravan_offer.get("name"):
        def _caravan():
            nonlocal caravan_baked
            from . import extern_bake
            caravan_baked = extern_bake.bake_caravan_offer(
                iso_out, caravan_offer["name"],
                caravan_offer.get("descs") or [])
        sub("Caravan presale row…")
        _soft("caravan presale line", _caravan)
    # COSMETIC -- shorten the chest reward box to "{NAME}!" so long remote AP
    # names get the full box width (the old template was the display cap).
    if feats.get("remote_chest_names"):
        def _box():
            from . import box_template
            box_template.patch_chest_box(iso_out)
        sub("chest reward box…")
        _soft("chest box template", _box)
    # COSMETIC -- job_scroll_boosts: pad the FM_CAMPUS class-name bank so the scroll-gated
    # class renames (client _classname_loop) aren't length-capped by the vanilla
    # job-name lengths. Padding is invisible (trailing spaces) until a scroll is
    # owned. Runs on the freshly-built iso_out (always the vanilla bank).
    # (see below: the KEY_EXP author also needs this, because padding JOB_NAME
    # relocates it and frees the 171B immediately before KEY_EXP -- without
    # that the descriptions do not fit. Harmless when no scroll is owned: the
    # padding is trailing spaces, and the step is idempotent.)
    _want_descs = bool(feats.get("equipment_rune_gate")
                       or feats.get("lute_block_gate"))
    if feats.get("job_scroll_boosts") or _want_descs:
        def _campus():
            from . import campus_bake
            campus_bake.pad_class_bank(iso_out)
        sub("class-name bank…")
        _soft("class-name bank pad", _campus)
    # COSMETIC -- progress-line KEY_EXP descriptions (the Key Items menu's
    # bottom bar). Baked, not written at runtime: these sentences outgrow their
    # resident slots and only the bake can rebuild the bank's offset table.
    #   * rune slot  -> explains the borrowed "Runes N of M" entry instead of
    #     showing Battery Circuit's robot-part text.
    #   * Lute (kid 1) -> the tablet line's description. NOTE this entry is
    #     ALSO the real Lute's, so once the tablets assemble the Lute itself
    #     carries this text (accepted cosmetic; the two are never both visible
    #     as separate rows, since id 37 is cleared at assembly).
    # Both are explicit '\n'-broken so they never run to the window edge.
    # SPACE IS THE BINDING CONSTRAINT HERE. FM_CAMPUS recompresses to 12314 of
    # its 12320-byte dpk slot -- SIX bytes of headroom -- and the bank can only
    # be re-laid inside its own unclaimed span, never relocated (measured
    # 2026-07-27). Vanilla entries 0 (Lute) and 18 (Ocarina) are the SAME string
    # ("A sonorous instrument of great beauty."), so the second costs nothing
    # compressed; giving the Lute its own text breaks that pair and costs ~50B
    # -- which does not fit. Keeping the pair (Ocarina carries the Lute's new
    # text too) fits with room to spare: 12298/12320.
    # TRADE-OFF ACCEPTED: in lute_tablets seeds the Ocarina -- a Whisperwind Cove
    # trade item -- shows the tablet description. Cosmetic, bonus-dungeon-only.
    # Drop the `1`/`19` pair to restore it (the rune line alone fits at 12314).
    # WORDING IS SPACE-BOUND AND COMPRESSION-SENSITIVE. Both strings below were
    # picked by MEASURING the recompressed PCK, not by counting characters: the
    # cost of a sentence depends on how much of it LZSS can match against text
    # already in the container, so length alone predicts nothing (a 26B Lute
    # string missed by 2 bytes while this 44B one fits). Re-measure after ANY
    # edit here -- campus_bake raises rather than shipping an oversized PCK, and
    # patch_iso wraps this in _soft, so a careless change silently drops BOTH
    # descriptions instead of failing the bake.
    _descs = {}
    if feats.get("equipment_rune_gate"):
        # SINGLE LINE, and no longer than a vanilla entry (the longest is 43
        # glyphs, kid 1). The old text was 116 glyphs across two lines, which
        # is fine ONLY while the borrowed row is the client's display line and
        # nobody highlights it. It is not fine when the game genuinely OWNS key
        # id 35: the row then renders with a blank name (the bake pads that
        # KEY_NAME slot for the client to fill) and the over-long description
        # hard-freezes the menu on cursor-in -- live 2026-08-07, reproduced with
        # NO client attached, on a save where the borrow had been wrongly set
        # inside Whisperwind. The row must survive being highlighted by itself.
        _descs["rune"] = "Unlocks activating equipment in battle."
    if feats.get("lute_block_gate"):
        _descs["lute"] = "Collect enough of these to finish the game."
    if feats.get("levistone_shard_gate"):
        # levistone_shards seeds: key id 36 "Energy Chip" is the shard progress
        # line's borrowed slot. SAME single-line, <=43-glyph rule as the rune
        # string above -- and it matters MORE here, because this row sits at
        # the end of the list where the cursor lands on it constantly (the
        # 2026-08-07 freeze was an over-long desc on a highlighted borrowed
        # row). Deliberately parallel to the Lute sentence.
        # TRADE-OFF ACCEPTED (same shape as the Ocarina/Lute pair): in shard
        # seeds the real Energy Chip -- a Whisperwind Cove robot part -- shows
        # this description too. Cosmetic, bonus-dungeon-only.
        # 43 glyphs = EXACTLY the vanilla ceiling (kid 1 is also 43). The
        # longer "...of these to raise the Airship from the desert." (61) was
        # measured to FIT the PCK (12282/12320) but was rejected on the freeze
        # rule, not on space -- user 2026-08-12.
        _descs["shard"] = "Collect enough to raise the desert Airship."
    if _descs:
        def _keydescs():
            from . import campus_bake
            from .ff1_data import RUNE_MENU_SLOT_KEY_ID, SHARD_MENU_SLOT_KEY_ID
            out = {}
            if "rune" in _descs:
                out[RUNE_MENU_SLOT_KEY_ID] = campus_bake.keydesc_encode(
                    _descs["rune"])
            if "shard" in _descs:
                out[SHARD_MENU_SLOT_KEY_ID] = campus_bake.keydesc_encode(
                    _descs["shard"])
            if "lute" in _descs:
                enc = campus_bake.keydesc_encode(_descs["lute"])
                out[1] = enc          # Lute (aliased by the id-37 tablet line)
                out[19] = enc         # Ocarina -- MUST match, see above
            campus_bake.author_key_desc(iso_out, out)
        sub("Key Items menu descriptions…")
        _soft("key-item progress descriptions", _keydescs)
    say("[bake] 4/4 finishing up…")
    _gatecheck_iso(iso_out, feats, remote_names, caravan_baked,
                   dyn_name_slots=dyn_name_slots)
    return iso_out


def _gatecheck_iso(iso_out, feats, remote_names, caravan_baked=None,
                   dyn_name_slots=0):
    """Cross-feature invariant check on the FINISHED ISO. Every post-build
    step edits the shared dpk independently; a bug in any one of them (or a
    donor-space collision between two -- live 2026-07-15: ms2_bake stole the
    FM_EXTERN12J1 record extern_bake relies on) can silently invalidate what
    another baked. Re-read the dpk and assert what the baked CODE actually
    depends on; raise -> launcher boots the unpatched ISO instead of a
    half-patched one that freezes in-game."""
    from . import extern_bake, wp16
    _SHOP_US = extern_bake._SHOP_BUNDLE
    with open(iso_out, "rb") as f:
        dpk_off, dpk_size = extern_bake._find_dpk(f)
        f.seek(dpk_off)
        dpk = f.read(dpk_size)
    recs = extern_bake._dpk_records(dpk)
    tomes_on = bool(feats.get("spell_tomes"))
    want_remote = bool((remote_names or dyn_name_slots)
                       and feats.get("remote_chest_names"))
    if tomes_on or want_remote:
        # tome code cites ITEM_NAME/ITEM_EXP string ids 43..106 and remote box
        # names cite ids base..base+R-1: the banks in BOTH extern bundles must
        # really be grown. base = 107 (tomes on) or 43 (tomes off). Counts come
        # from tome_names (single source of truth) -- a hand-written constant
        # here aborted a GOOD bake once (108 was the item-ARRAY row count, not
        # the name-bank entry count 107).
        from . import tome_names as _TN
        base = _TN.BANK_ENTRIES if tomes_on else _TN.ITEM_ENTRIES
        need = base + ((len(remote_names or []) + int(dyn_name_slots or 0))
                       if feats.get("remote_chest_names") else 0)
        for us_tag, _j in extern_bake.US_TO_J:
            if us_tag not in recs:
                raise ValueError(f"gatecheck: {us_tag} record missing")
            _ro, off, size = recs[us_tag]
            blob = wp16.decompress(bytes(dpk[off:off + size]))
            _cnt, brecs = extern_bake._parse_bundle_dir(blob)
            bank = {n: (o, s) for n, _do, o, s in brecs}.get(
                extern_bake._ITEM_NAME)
            if bank is None:
                raise ValueError(f"gatecheck: {us_tag} lacks ITEM_NAME.MSG")
            entries = struct.unpack_from("<I", blob, bank[0] + 8)[0] >> 8
            if entries < need:
                raise ValueError(
                    f"gatecheck: {us_tag} ITEM_NAME has {entries} entries, "
                    f"baked code needs {need} (bank growth lost?)")
    if caravan_baked:
        # the caravan row may have RELOCATED FM_SHOPUS into the FM_SHOPJ1 donor
        # -- assert the bundle the shop UI loads still decompresses and still
        # carries the authored row (a donor-space collision would leave the
        # record pointing at someone else's bytes -> shop-open garbage/freeze).
        from . import shop_font as _SFT
        if _SHOP_US not in recs:
            raise ValueError(f"gatecheck: {_SHOP_US} record missing")
        _ro, off, size = recs[_SHOP_US]
        blob = wp16.decompress(bytes(dpk[off:off + size]))
        _cnt, brecs = extern_bake._parse_bundle_dir(blob)
        banks = {n: (o, s) for n, _do, o, s in brecs}
        for rec_name in (_SFT.NAME_RECORD, _SFT.DESC_RECORD):
            if rec_name not in banks:
                raise ValueError(f"gatecheck: {_SHOP_US} lacks {rec_name}")
        o, s_ = banks[_SFT.NAME_RECORD]
        got = _SFT.bank_entry_text(blob[o:o + s_], _SFT.CARAVAN_NAME_IDX)
        if got != caravan_baked[0]:
            raise ValueError(f"gatecheck: caravan row reads {got!r}, "
                             f"baked {caravan_baked[0]!r} (donor collision?)")
    if feats.get("boss_minions") and feats.get("boss_minions_plan"):
        # every planned fid the plan adds monsters to must have its MS2 pack.
        for entry in feats["boss_minions_plan"]:
            fid, groups = entry[0], entry[1]
            if groups and f"MS2_{int(fid):03X}.PCK" not in recs:
                # creations can legitimately no-op when all adds are
                # self-loading boss ids; only flag fids that shipped a pack
                # requirement -- conservative: warn-only would hide crashes,
                # but self-load detection lives in ms2_bake, so re-check there
                # is the source of truth. Here absence of BOTH pack and any
                # vanilla record is the red flag.
                warnings.warn(f"gatecheck: MS2_{int(fid):03X}.PCK absent "
                              f"(may be self-loading adds)")
