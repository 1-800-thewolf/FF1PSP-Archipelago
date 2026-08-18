"""Shorten the chest-reward box template on disc so long remote AP names have the
full box width (see tier2-poll-chests / chest-box-name-source).

The box archive (USEVMCMN.PCK, wp16 in the dpk) entry[0] is the message-font
template "{NAME} obtained from the\nchest!" -- a 0x39 NAME-insert then static
text. The renderer draws entry[0] bounded by the offset table (off1-off0),
ignoring the 0x0d terminator, so we overwrite entry[0] IN PLACE at the SAME byte
length (new template + 0x0d + 0x00 space padding). Same length => no offset-table
rebuild; the simpler content recompresses smaller => fits the original dpk slot
(zero-padded). Applies to ALL chests (own items read "Cottage!" etc.).
"""
import struct

from . import wp16
from . import extern_bake as EB
from . import font_map as FONT

_PCK = "USEVMCMN.PCK"
_NAME_INSERT = 0x39
_TERM = 0x0D


def _archive_entry0(dec):
    """Return (abs_off0, length) of TEXT-archive entry[0] in the decompressed blob."""
    j = dec.find(b"TEXT")
    if j < 0:
        raise ValueError("USEVMCMN: no TEXT archive")
    base = j - 4
    # header: [base]=0, TEXT@+4, count@+8 (>>8), total@+0xC; entry offset
    # table (bank-relative u32s) starts at base+0x10.
    off0 = struct.unpack_from("<I", dec, base + 0x10)[0]
    off1 = struct.unpack_from("<I", dec, base + 0x14)[0]
    return base + off0, off1 - off0


def build_template(text="{NAME}!"):
    """Encode a box template to message-font bytes (no length limit here; the
    caller pads/truncates to the slot). '{NAME}' -> 0x39 insert; rest via FONT."""
    out = bytearray()
    i = 0
    while i < len(text):
        if text.startswith("{NAME}", i):
            out.append(_NAME_INSERT)
            i += 6
        else:
            out += FONT.encode_name(text[i])
            i += 1
    out.append(_TERM)
    return bytes(out)


def patch_chest_box(iso_path, template="{NAME}!", log=print):
    payload = build_template(template)
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = EB._find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        recs = EB._dpk_records(dpk)
        rec_off, off, size = recs[_PCK]
        dec = bytearray(wp16.decompress(bytes(dpk[off:off + size])))
        e0, length = _archive_entry0(dec)
        if len(payload) > length:
            raise ValueError(f"box template {len(payload)}B exceeds slot {length}B")
        dec[e0:e0 + length] = payload + b"\x00" * (length - len(payload))
        comp = wp16.compress(bytes(dec))
        if len(comp) > size:
            raise ValueError(f"USEVMCMN recompress {len(comp):#x} > slot {size:#x}")
        dpk[off:off + len(comp)] = comp
        for i in range(off + len(comp), off + size):      # zero-pad the slack
            dpk[i] = 0
        # decompressed size is unchanged (in-place same-length edit); leave +32.
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[box_template] chest box -> {template!r} "
        f"({len(payload)}B in {length}B slot; USEVMCMN {len(comp):#x}/{size:#x})")
    return iso_path
