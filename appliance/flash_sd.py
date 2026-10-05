"""Schrijft het KHZS Timing-image (.img of .img.xz) naar een SD-kaart. Wordt gestart door flash-sd.ps1 (als administrator).

Gebruik: python flash_sd.py <schijfnummer> <image>

Schrijft rechtstreeks met de Windows-API (CreateFile/WriteFile op \\\\.\\PhysicalDriveN); de C-bibliotheek van
Python kan dat niet betrouwbaar. Leest daarna het begin van de kaart terug ter controle.
"""
import ctypes
import hashlib
import lzma
import sys
import time
from ctypes import wintypes

CHUNK = 4 * 1024 * 1024          # veelvoud van de sectorgrootte (512/4096)
VERIFY = 64 * 1024 * 1024        # zoveel van het begin terug lezen en vergelijken

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID = ctypes.c_void_p(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]
k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
k32.CloseHandle.argtypes = [wintypes.HANDLE]


def fail(what):
    raise OSError(f"{what} mislukt (Windows-fout {ctypes.get_last_error()})")


def open_disk(n):
    h = k32.CreateFileW(rf"\\.\PhysicalDrive{n}", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, FILE_FLAG_WRITE_THROUGH, None)
    if h in (None, INVALID):
        fail("schijf openen")
    return h


def main():
    disk, img = int(sys.argv[1]), sys.argv[2]
    src = lzma.open(img, "rb") if img.lower().endswith(".xz") else open(img, "rb")
    h = open_disk(disk)
    head = hashlib.sha256()
    total, t0, n = 0, time.time(), wintypes.DWORD()
    try:
        while True:
            buf = src.read(CHUNK)
            if not buf:
                break
            if len(buf) % 4096:
                buf += b"\0" * (4096 - len(buf) % 4096)
            if total < VERIFY:
                head.update(buf[:VERIFY - total])
            if not k32.WriteFile(h, buf, len(buf), ctypes.byref(n), None) or n.value != len(buf):
                fail(f"schrijven op {total / 2**20:.0f} MB")
            total += len(buf)
            mb = total / 2**20
            sys.stdout.write(f"\r  {mb:7.0f} MB geschreven  ({mb / max(1e-6, time.time() - t0):5.1f} MB/s)")
            sys.stdout.flush()
        k32.FlushFileBuffers(h)
        # controle: begin van de kaart terug lezen
        if not k32.SetFilePointerEx(h, 0, None, 0):
            fail("terugspoelen")
        back, done, rb = hashlib.sha256(), 0, ctypes.create_string_buffer(CHUNK)
        want = min(VERIFY, total)
        while done < want:
            if not k32.ReadFile(h, rb, CHUNK, ctypes.byref(n), None) or not n.value:
                fail("terug lezen")
            back.update(rb.raw[:min(n.value, want - done)])
            done += n.value
        if back.hexdigest() != head.hexdigest():
            raise OSError("controle mislukt: wat op de kaart staat is niet wat geschreven werd")
    finally:
        k32.CloseHandle(h)
        src.close()
    print(f"\n  klaar: {total / 2**20:.0f} MB in {time.time() - t0:.0f} s, controle OK")


if __name__ == "__main__":
    main()
