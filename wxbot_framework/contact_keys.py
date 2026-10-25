"""已核对 Windows 构建的联系人库密钥，只读进程内存；不注入、不保存。

Config.Cipher 对象布局参考 wcdb-key-tool 的 Windows runtime 实现；混淆掩码
是程序常量，不是用户密钥。这里只验证一个指定 contact.db，不收集其他库的密钥。
"""
from __future__ import annotations

import ctypes as c
import hashlib
from pathlib import Path
import re
import struct
import time

from .account import read_current_account
from .contact_database import ContactDatabaseError, authenticates, _require, wipe
from .native_profiles import profile_for_fingerprint

_NAME = b"com.Tencent.WCDB.Config.Cipher"
_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
_LIMIT = 0x800000000000
_SCAN_BYTES = 512 * 1024 * 1024


class ContactProcess:
    """独立 PROCESS_QUERY_INFORMATION | PROCESS_VM_READ 句柄，不复用注入通道。"""

    def __init__(self, pid, core_base, core_path):
        self.base, self.path = core_base, Path(core_path)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("OpenProcess", [c.c_uint32, c.c_int, c.c_uint32], c.c_void_p),
            ("CloseHandle", [c.c_void_p], c.c_int),
            ("ReadProcessMemory", [c.c_void_p, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("VirtualQueryEx", [c.c_void_p, c.c_void_p, c.c_void_p, c.c_size_t], c.c_size_t),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.OpenProcess(0x410, False, pid)
        _require(self.handle, "联系人进程只读访问不可用")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, address, size):
        if not (0x10000 <= address < _LIMIT and 0 < size <= 2 * 1024 * 1024 and address + size <= _LIMIT):
            return b""
        buffer = c.create_string_buffer(size)
        count = c.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, c.byref(count)) or count.value != size:
            return b""
        return buffer.raw

    def regions(self):
        class MBI(c.Structure):
            _fields_ = [("base", c.c_void_p), ("allocation", c.c_void_p),
                        ("allocation_protect", c.c_uint32), ("partition", c.c_uint16),
                        ("size", c.c_size_t), ("state", c.c_uint32),
                        ("protect", c.c_uint32), ("type", c.c_uint32)]
        address, total, result = 0, 0, []
        while address < _LIMIT:
            info = MBI()
            if not self.kernel.VirtualQueryEx(self.handle, address, c.byref(info), c.sizeof(info)):
                break
            following = int(info.base or address) + info.size
            _require(following > address, "进程内存布局未确认")
            if info.state == 0x1000 and info.type == 0x20000 and not info.protect & 0x101 and info.protect & 0xFF in (2, 4, 8):
                total += info.size
                _require(total <= _SCAN_BYTES, "联系人只读扫描超过预算")
                result.append((int(info.base), info.size))
            address = following
        return result

    def anchors(self, check):
        """核对完整 DLL 指纹后，从 PE 静态字符串定位，不猜绝对地址。"""
        import pefile
        digest, offsets, carry, offset = hashlib.sha256(), [], b"", 0
        with self.path.open("rb") as stream:
            while True:
                check()
                block = stream.read(2 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                data = carry + block
                pos = data.find(_NAME)
                while pos >= 0:
                    offsets.append(offset - len(carry) + pos)
                    pos = data.find(_NAME, pos + 1)
                offset += len(block)
                carry = data[-(len(_NAME)-1):]
        profile = profile_for_fingerprint(offset, digest.hexdigest())
        # 尚未对旧版 Config.Cipher 布局做实机验证，不能套用。
        _require(profile is not None and profile.version == "4.1.15.56", "此微信构建的联系人库读取尚未验证")
        pe = pefile.PE(str(self.path), fast_load=True)
        try:
            anchors = {self.base + pe.get_rva_from_offset(pos) for pos in offsets}
        finally:
            pe.close()
        _require(anchors and all(self.read(addr, len(_NAME)) == _NAME for addr in anchors),
                 "联系人配置标识未确认")
        return anchors


def read_contact_key(process, account, page1, check=lambda: None):
    """返回需 wipe 的单个密钥；候选需嵌入目标库 salt 并通过 page1 HMAC。"""
    _require(read_current_account(process.read, process.base) == account, "联系人账号不匹配")
    anchors = process.anchors(check)
    patterns = [struct.pack("<QQ", addr, len(_NAME)) for addr in anchors]
    target = page1[:16].hex().encode()
    deadline, visited = time.monotonic() + 5, set()
    for start, size in process.regions():
        carry = b""
        for offset in range(0, size, 2 * 1024 * 1024):
            check()
            _require(time.monotonic() < deadline, "联系人只读扫描超时")
            chunk = process.read(start + offset, min(2 * 1024 * 1024, size-offset))
            if not chunk:
                carry = b""
                continue
            data, address = carry + chunk, start + offset - len(carry)
            for pattern in patterns:
                pos = data.find(pattern)
                while pos >= 0:
                    check()
                    _require(time.monotonic() < deadline, "联系人只读扫描超时")
                    node_address = address + pos - 16
                    node = process.read(node_address, 80)
                    if len(node) == 80 and struct.unpack_from("<Q", node, 16)[0] in anchors and struct.unpack_from("<Q", node, 24)[0] == len(_NAME):
                        config = struct.unpack_from("<Q", node, 40)[0]
                        if 0x10000 <= config < _LIMIT and config not in visited:
                            visited.add(config)
                            obj = process.read(config + 0x88, 40)
                            if len(obj) == 40:
                                pointer, length = struct.unpack_from("<QQ", obj, 8)
                                if 0x10000 <= pointer < _LIMIT and 0 < length <= 1024:
                                    blob = process.read(pointer, length)
                                    decoded = bytes(v ^ _MASK[i % 32] for i, v in enumerate(blob))
                                    for match in re.finditer(rb"[xX]'([0-9a-fA-F]{96,192})'", decoded):
                                        run = match.group(1).lower()
                                        for index in range(0, len(run)-95, 32):
                                            if run[index+64:index+96] != target:
                                                continue
                                            key = bytearray.fromhex(run[index:index+64].decode("ascii"))
                                            stable = (len(blob) == length and process.read(node_address, 80) == node and
                                                      process.read(config+0x88, 40) == obj and process.read(pointer, length) == blob)
                                            if stable and authenticates(key, page1):
                                                if read_current_account(process.read, process.base) == account:
                                                    return key
                                            wipe(key)
                    pos = data.find(pattern, pos + 1)
            carry = data[-128:]
    raise ContactDatabaseError("联系人库密钥暂不可用")
