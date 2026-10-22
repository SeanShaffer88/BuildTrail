"""Read PE metadata without loading or executing a binary. Standard library only."""
import ctypes
import mmap
import os
import struct


def inspect_pe(path):
    with open(path, 'rb') as stream:
        if stream.read(2) != b'MZ':
            return None
        stream.seek(0)
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            def unpack(fmt, offset):
                if offset < 0 or offset + struct.calcsize(fmt) > len(data):
                    raise ValueError('Truncated PE structure')
                return struct.unpack_from(fmt, data, offset)

            pe = unpack('<I', 0x3c)[0]
            if data[pe:pe + 4] != b'PE\0\0':
                raise ValueError('Invalid PE signature')
            machine, count = unpack('<HH', pe + 4)
            opt_size = unpack('<H', pe + 20)[0]
            opt = pe + 24
            magic = unpack('<H', opt)[0]
            if magic not in (0x10b, 0x20b):
                raise ValueError('Unsupported PE optional header')
            directory = 96 if magic == 0x10b else 112
            if opt_size < directory or opt + opt_size > len(data):
                raise ValueError('Invalid optional header length')
            headers_size = unpack('<I', opt + 60)[0]
            sections = []
            if count > 96:
                raise ValueError('Too many PE sections')
            for index in range(count):
                offset = opt + opt_size + index * 40
                virtual_size, va, raw_size, raw = unpack('<IIII', offset + 8)
                sections.append((va, virtual_size, raw_size, raw))

            def rva_offset(rva):
                if rva < min(headers_size, len(data)):
                    return rva, min(headers_size, len(data))
                for va, virtual_size, raw_size, raw in sections:
                    delta = rva - va
                    if 0 <= delta < max(virtual_size, raw_size):
                        if delta >= raw_size or raw + delta >= len(data):
                            raise ValueError('PE RVA points outside stored section data')
                        return raw + delta, min(raw + raw_size, len(data))
                raise ValueError('Unmapped PE RVA')

            def dll_name(rva):
                start, end = rva_offset(rva)
                stop = data.find(b'\0', start, min(end, start + 4096))
                if stop < 0:
                    raise ValueError('Unterminated DLL name')
                return data[start:stop].decode('ascii', errors='replace')

            names = set()
            delay_names = set()
            number = unpack('<I', opt + directory - 4)[0]
            for slot, width, output in ((1, 20, names), (13, 32, delay_names)):
                if number <= slot:
                    continue
                if directory + (slot + 1) * 8 > opt_size:
                    raise ValueError('Data directory exceeds optional header')
                rva, size = unpack('<II', opt + directory + slot * 8)
                if not rva or not size:
                    continue
                start, end = rva_offset(rva)
                limit = min(size // width, (end - start) // width, 65536)
                terminated = False
                for index in range(limit):
                    values = unpack('<' + 'I' * (width // 4), start + index * width)
                    if not any(values):
                        terminated = True
                        break
                    name_rva = values[3] if slot == 1 else values[1]
                    if slot == 13 and not values[0] & 1:
                        image_base = unpack('<I' if magic == 0x10b else '<Q', opt + (28 if magic == 0x10b else 24))[0]
                        name_rva -= image_base
                    output.add(dll_name(name_rva))
                if not terminated:
                    raise ValueError('Unterminated or oversized import directory')
            return {'architecture': {0x14c: 'x86', 0x8664: 'x64', 0xaa64: 'ARM64',
                                     0xa641: 'ARM64EC', 0xa64e: 'ARM64X', 0x1c4: 'ARM Thumb-2'}.get(machine, f'0x{machine:04x}'),
                    'imports': sorted(names, key=str.casefold),
                    'delay_imports': sorted(delay_names, key=str.casefold)}


def file_version(path):
    if os.name != 'nt':
        return None
    from ctypes import wintypes
    version = ctypes.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    length = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not length:
        return None
    if length > 64 * 1024 * 1024:
        raise ValueError('Version resource is too large')
    buffer = ctypes.create_string_buffer(length)
    if not version.GetFileVersionInfoW(str(path), 0, length, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer = ctypes.c_void_p()
    size = wintypes.UINT()
    if not version.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(size)) or size.value < 52:
        return None
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if values[0] != 0xfeef04bd:
        return None
    ms, ls = values[2], values[3]
    return f'{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}'
