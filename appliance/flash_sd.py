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
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.FindFirstVolumeW.restype = wintypes.HANDLE
k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME = 0x00090018, 0x00090020


def lock_volumes(disk):
    """Alle volumes op deze schijf vergrendelen en loskoppelen (anders weigert Windows schrijven in hun gebied).
    De handles blijven open tot het einde, zodat Windows ze niet opnieuw koppelt."""
    held, buf = [], ctypes.create_unicode_buffer(300)
    fh = k32.FindFirstVolumeW(buf, 300)
    if fh in (None, INVALID):
        return held
    try:
        while True:
            name = buf.value.rstrip("\\")
            h = k32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                OPEN_EXISTING, 0, None)
            if h not in (None, INVALID):
                out, n = ctypes.create_string_buffer(256), wintypes.DWORD()
                mine = False
                if k32.DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 0, out, 256, ctypes.byref(n), None):
                    cnt = int.from_bytes(out.raw[0:4], "little")
                    for i in range(cnt):
                        if int.from_bytes(out.raw[8 + 24 * i:12 + 24 * i], "little") == disk:
                            mine = True
                if mine:
                    k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(n), None)
                    k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(n), None)
                    held.append(h)
                    print(f"  volume losgekoppeld: {name}")
                else:
                    k32.CloseHandle(h)
            if not k32.FindNextVolumeW(fh, buf, 300):
                break
    finally:
        k32.FindVolumeClose(fh)
    return held


def fail(what):
    raise OSError(f"{what} mislukt (Windows-fout {ctypes.get_last_error()})")


def open_disk(n):
    h = k32.CreateFileW(rf"\\.\PhysicalDrive{n}", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)          # zonder write-through: veel sneller; FlushFileBuffers op het einde
    if h in (None, INVALID):
        fail("schijf openen")
    return h


def main():
    disk, img = int(sys.argv[1]), sys.argv[2]
    src = lzma.open(img, "rb") if img.lower().endswith(".xz") else open(img, "rb")
    held = lock_volumes(disk)
    h = open_disk(disk)
    head = hashlib.sha256()
    first = None                      # het eerste stuk (partitietabel) pas op het einde schrijven
    zero = bytes(1024 * 1024)        # oude partitietabel meteen wissen
    n0 = wintypes.DWORD()
    if not k32.WriteFile(h, zero, len(zero), ctypes.byref(n0), None):
        fail("oude partitietabel wissen")
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
            if first is None:
                first = buf
                if not k32.SetFilePointerEx(h, len(buf), None, 0):
                    fail("verder spoelen")
            elif not k32.WriteFile(h, buf, len(buf), ctypes.byref(n), None) or n.value != len(buf):
                fail(f"schrijven op {total / 2**20:.0f} MB")
            total += len(buf)
            mb = total / 2**20
            sys.stdout.write(f"\r  {mb:7.0f} MB geschreven  ({mb / max(1e-6, time.time() - t0):5.1f} MB/s)")
            sys.stdout.flush()
        # nu pas de partitietabel: Windows herkent de nieuwe partities pas als alles erop staat
        if first is not None:
            if not k32.SetFilePointerEx(h, 0, None, 0) or not k32.WriteFile(h, first, len(first), ctypes.byref(n), None):
                fail("partitietabel schrijven")
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
        for v in held:
            k32.CloseHandle(v)
        src.close()
    print(f"\n  klaar: {total / 2**20:.0f} MB in {time.time() - t0:.0f} s, controle OK")


if __name__ == "__main__":
    main()
