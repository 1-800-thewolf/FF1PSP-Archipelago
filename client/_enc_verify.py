"""Live read-back verify that the encounter_rate table (0x8945654, 96xu16 data
block) accepts direct RAM writes -- i.e. no JIT wall for this table. Loads the
process-memory bridge standalone (no apworld import). Run with a BAKED FF1 ISO
loaded in PPSSPP:

    .venv\\Scripts\\python.exe ff1psp\\client\\_enc_verify.py

Non-destructive: restores the original table bytes before exit.
"""
import asyncio
import importlib.util
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
ADDR = 0x8945654          # encounter_rate RAM home (boot_patch.table_ram_addr)
N = 192                   # 96 * u16


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def main():
    pm = _load("ppsspp_mem")
    mem = pm.PPSSPPMem()
    if not mem.attach():
        print("ATTACH FAILED -- is a BAKED FF1 ISO loaded in PPSSPP? "
              "(needs the F1AP bake tag).")
        return

    orig = await mem.read(ADDR, N)
    print(f"attached pid={mem.pid} host_base={mem.host_base:#x}")
    print("orig first6 u16:", struct.unpack("<6H", orig[:12]))

    try:
        for label, payload in (("x0(off)", b"\x00" * N),
                               ("x2", struct.pack("<96H",
                                *[min(v * 2, 0xFFFF) for v in
                                  struct.unpack("<96H", orig)]))):
            await mem.write(ADDR, payload)
            back = await mem.read(ADDR, N)
            ok = back == payload
            print(f"{label:8} write+readback: "
                  f"{'STICKS' if ok else 'MISMATCH'}  first6={struct.unpack('<6H', back[:12])}")
    finally:
        await mem.write(ADDR, orig)
        restored = await mem.read(ADDR, N)
        print("restored orig:", restored == orig)


if __name__ == "__main__":
    asyncio.run(main())
