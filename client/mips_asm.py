"""Minimal MIPS32 little-endian assembler for Route-2 code caves.

Hand-rolled (no keystone dep). Covers the instructions needed to build detour
caves: load-immediate, load/store, jumps, nop, plus raw-word passthrough for
displaced original instructions. Returns bytes (LE) ready to splice into the ELF.

Register names -> numbers. MIPS branch-delay slot is the CALLER's concern:
every j/jr you emit executes the NEXT word before transferring control, so put
a nop (or a useful instr) after a jump.
"""
import struct

REG = {"zero":0,"at":1,"v0":2,"v1":3,"a0":4,"a1":5,"a2":6,"a3":7,
       "t0":8,"t1":9,"t2":10,"t3":11,"t4":12,"t5":13,"t6":14,"t7":15,
       "s0":16,"s1":17,"s2":18,"s3":19,"s4":20,"s5":21,"s6":22,"s7":23,
       "t8":24,"t9":25,"k0":26,"k1":27,"gp":28,"sp":29,"fp":30,"ra":31}

def _r(x):
    return x if isinstance(x, int) else REG[x]

def word(w):            # raw 32-bit instruction word (e.g. displaced original)
    return struct.pack("<I", w & 0xffffffff)

def nop():
    return word(0)

def _i(op, rs, rt, imm):
    return word((op<<26)|(_r(rs)<<21)|(_r(rt)<<16)|(imm & 0xffff))

def lui(rt, imm):       return _i(0x0F, 0, rt, imm)
def ori(rt, rs, imm):   return _i(0x0D, rs, rt, imm)
def addiu(rt, rs, imm): return _i(0x09, rs, rt, imm)
def andi(rt, rs, imm):  return _i(0x0C, rs, rt, imm)
def xori(rt, rs, imm):  return _i(0x0E, rs, rt, imm)
# UNSIGNED compare-immediate: rt = (rs < imm). Unsigned is what a bounds check
# wants -- a negative/huge rs fails the test instead of passing it.
def sltiu(rt, rs, imm): return _i(0x0B, rs, rt, imm)
def sw(rt, off, base):  return _i(0x2B, base, rt, off)
def lw(rt, off, base):  return _i(0x23, base, rt, off)
def lhu(rt, off, base): return _i(0x25, base, rt, off)
def lh(rt, off, base):  return _i(0x21, base, rt, off)
def lbu(rt, off, base): return _i(0x24, base, rt, off)
def sb(rt, off, base):  return _i(0x28, base, rt, off)
def sh(rt, off, base):  return _i(0x29, base, rt, off)

def srl(rd, rt, sh):    return word((_r(rt)<<16)|(_r(rd)<<11)|((sh&31)<<6)|0x02)
def sra(rd, rt, sh):    return word((_r(rt)<<16)|(_r(rd)<<11)|((sh&31)<<6)|0x03)
def sll(rd, rt, sh):    return word((_r(rt)<<16)|(_r(rd)<<11)|((sh&31)<<6)|0x00)
def addu(rd, rs, rt):   return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x21)
def subu(rd, rs, rt):   return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x23)
def and_(rd, rs, rt):   return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x24)
def or_(rd, rs, rt):    return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x25)
def slt(rd, rs, rt):    return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x2A)
def sltu(rd, rs, rt):   return word((_r(rs)<<21)|(_r(rt)<<16)|(_r(rd)<<11)|0x2B)

def multu(rs, rt):      return word((_r(rs)<<21)|(_r(rt)<<16)|0x19)  # LO=rs*rt (unsigned)
def mult(rs, rt):       return word((_r(rs)<<21)|(_r(rt)<<16)|0x18)  # signed
def divu(rs, rt):       return word((_r(rs)<<21)|(_r(rt)<<16)|0x1B)  # LO=rs/rt, HI=rs%rt
def div(rs, rt):        return word((_r(rs)<<21)|(_r(rt)<<16)|0x1A)  # signed
def mflo(rd):           return word((_r(rd)<<11)|0x12)
def mfhi(rd):           return word((_r(rd)<<11)|0x10)

def beq(rs, rt, off):   return _i(0x04, rs, rt, off)   # off in INSTRUCTIONS (branch delay-relative)
def bne(rs, rt, off):   return _i(0x05, rs, rt, off)


def asm_labels(items):
    """Assemble a list mixing raw byte-blobs with label markers and pending
    branches, resolving branch offsets so they never need hand-counting.
      ("label", name)            -- marks a position
      ("beq"|"bne", rs, rt, name) -- branch to the label (offset filled in)
    The instruction AFTER a pending branch is its delay slot, as usual."""
    # pass 1: layout
    pos, labels, out = 0, {}, []
    for it in items:
        if isinstance(it, tuple) and it[0] == "label":
            labels[it[1]] = pos
        elif isinstance(it, tuple):
            out.append((pos, it)); pos += 4
        else:
            out.append((pos, it)); pos += len(it)
    # pass 2: emit
    blob = b""
    for pos, it in out:
        if isinstance(it, tuple):
            op, rs, rt, name = it
            off = (labels[name] - (pos + 4)) // 4
            blob += (beq if op == "beq" else bne)(rs, rt, off)
        else:
            blob += it
    return blob

def li(rt, val):        # load 32-bit immediate (lui+ori); returns 8 bytes
    val &= 0xffffffff
    return lui(rt, (val>>16)&0xffff) + ori(rt, rt, val & 0xffff)

def j(target):          # j uses (target>>2) in low 26 bits; same 256MB region
    return word((0x02<<26)|((target>>2)&0x03ffffff))

def jal(target):
    return word((0x03<<26)|((target>>2)&0x03ffffff))

def jr(rs=31):          # default jr ra
    return word((_r(rs)<<21)|0x08)

def assemble(parts):
    """Concatenate a list of byte-blobs into one blob."""
    return b"".join(parts)
