"""只读联系人库快照：认证 DB/WAL，在内存 SQLite 中仅提取 wxid 和微信名。

不打开真实 SQLite 文件，不创建 SHM、临时数据库或密钥文件。异常不得包含密钥、
页面内容或联系人字段。可变明文缓冲区在关闭 SQLite 后清零；Python/系统内存副本
无法保证安全擦除，因此不宣称能防止系统换页或进程转储。
"""
from __future__ import annotations

import ctypes as c
import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
MAX_DB = 64 * 1024 * 1024
MAX_WAL = 128 * 1024 * 1024


class ContactDatabaseError(RuntimeError):
    """只包含固定、非敏感诊断的可恢复错误。"""


def _require(ok, message):
    if not ok:
        raise ContactDatabaseError(message)


def wipe(buffer):
    if buffer is not None:
        c.memset((c.c_ubyte * len(buffer)).from_buffer(buffer), 0, len(buffer))


def read_snapshot(path):
    """固定只读 contact.db 及同目录 WAL，双读一致后才发布。"""
    path = Path(path)
    _require(path.name == "contact.db", "只允许读取联系人库")
    wal_path = path.with_name("contact.db-wal")

    def bounded_read(file, limit, optional=False):
        try:
            with file.open("rb") as stream:
                data = stream.read(limit + 1)
        except FileNotFoundError:
            if optional:
                return b""
            raise
        _require(len(data) <= limit, "联系人快照超过内存预算")
        return data

    for _ in range(3):
        main = bounded_read(path, MAX_DB)
        wal = bounded_read(wal_path, MAX_WAL, True)
        if main == bounded_read(path, MAX_DB) and wal == bounded_read(wal_path, MAX_WAL, True):
            _require(len(main) >= PAGE and len(main) % PAGE == 0, "联系人库页面不完整")
            return main, wal
    raise ContactDatabaseError("联系人库正在变化，稍后重试")


def mac_key(key, salt):
    return hashlib.pbkdf2_hmac("sha512", key, bytes(x ^ 0x3A for x in salt), 2, 32)


def authenticates(key, page1):
    if len(key) != 32 or len(page1) != PAGE:
        return False
    expected = hmac.new(mac_key(key, page1[:16]), page1[16:PAGE-64] + struct.pack("<I", 1), "sha512")
    return hmac.compare_digest(expected.digest(), page1[PAGE-64:])


def _wal_pages(main, wal):
    """SQLite WAL 校验链；只覆盖最后有效提交，旧 epoch/未提交尾部不生效。"""
    total = len(main) // PAGE
    overlay = {}
    if not wal:
        return total, overlay
    _require(len(wal) >= 32, "WAL 头不完整")
    magic, version, size = struct.unpack_from(">III", wal)
    _require(magic in (0x377F0682, 0x377F0683) and version == 3007000 and size == PAGE,
             "WAL 格式未确认")
    order = "<" if magic == 0x377F0682 else ">"

    def checksum(data, previous=(0, 0)):
        words = struct.unpack(order + "I" * (len(data) // 4), data)
        a, b = previous
        for index in range(0, len(words), 2):
            a = (a + words[index] + b) & 0xFFFFFFFF
            b = (b + words[index+1] + a) & 0xFFFFFFFF
        return a, b

    current = checksum(wal[:24])
    _require(current == struct.unpack_from(">II", wal, 24), "WAL 头校验失败")
    frames = []
    committed = 0
    for pos in range(32, len(wal) - 24 - PAGE + 1, 24 + PAGE):
        header, body = wal[pos:pos+24], wal[pos+24:pos+24+PAGE]
        if header[8:16] != wal[16:24]:
            break  # checkpoint 后预分配文件仍可能保留旧帧。
        following = checksum(header[:8] + body, current)
        if following != struct.unpack_from(">II", header, 16):
            break  # SQLite 同样只采用校验通过的已提交前缀。
        current = following
        number, commit_size = struct.unpack_from(">II", header)
        _require(1 <= number <= MAX_DB // PAGE and commit_size <= MAX_DB // PAGE,
                 "WAL 页面超过内存预算")
        frames.append((number, body))
        if commit_size:
            committed, total = len(frames), commit_size
    for number, body in frames[:committed]:
        overlay[number] = body
    return total, overlay


def decrypt_snapshot(main, wal, key, *, check=lambda: None):
    """返回调用方负责 wipe 的 bytearray；所有使用的页面均须通过 HMAC。"""
    _require(len(main) >= PAGE and len(main) % PAGE == 0, "联系人库页面不完整")
    _require(authenticates(key, main[:PAGE]), "联系人库密钥认证失败")
    total, overlay = _wal_pages(main, wal)
    _require(1 <= total <= MAX_DB // PAGE, "联系人库超过内存预算")
    plain = bytearray(total * PAGE)
    hk = bytearray(mac_key(key, main[:16]))
    try:
        for number in range(1, total + 1):
            check()
            body = overlay.get(number, main[(number-1)*PAGE:number*PAGE])
            _require(len(body) == PAGE, "联系人库缺少页面")
            offset = 16 if number == 1 else 0
            expected = hmac.new(hk, body[offset:PAGE-64] + struct.pack("<I", number), "sha512")
            _require(hmac.compare_digest(expected.digest(), body[PAGE-64:]), "联系人库页面认证失败")
            decryptor = Cipher(algorithms.AES(key), modes.CBC(body[PAGE-80:PAGE-64])).decryptor()
            content = decryptor.update(body[offset:PAGE-80]) + decryptor.finalize()
            plain[(number-1)*PAGE:number*PAGE] = (b"SQLite format 3\0" if number == 1 else b"") + content + bytes(80)
        _require(plain[16:18] == b"\x10\0" and plain[20] == 80, "联系人库页头未确认")
        # 仅修改内存副本：脱离真实 WAL 后供只读 deserialize 使用。
        plain[18:20] = b"\1\1"
        plain[28:32] = struct.pack(">I", total)
        return plain
    except BaseException:
        wipe(plain)
        raise
    finally:
        wipe(hk)


def _sqlite_library():
    """使用 Python 已加载的 SQLite，兼容没有 Connection.deserialize 的 Python 3.10。"""
    import _sqlite3
    if sys.platform == "win32":
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [c.c_wchar_p]
        kernel.GetModuleHandleW.restype = c.c_void_p
        kernel.GetModuleFileNameW.argtypes = [c.c_void_p, c.c_wchar_p, c.c_uint32]
        module = kernel.GetModuleHandleW("sqlite3.dll")
        name = c.create_unicode_buffer(32768)
        _require(module and kernel.GetModuleFileNameW(module, name, len(name)), "SQLite 内存接口不可用")
        library = c.CDLL(name.value)
    else:
        library = c.CDLL(_sqlite3.__file__)
    signatures = {
        "sqlite3_open": ([c.c_char_p, c.POINTER(c.c_void_p)], c.c_int),
        "sqlite3_close": ([c.c_void_p], c.c_int),
        "sqlite3_deserialize": ([c.c_void_p, c.c_char_p, c.c_void_p, c.c_int64, c.c_int64, c.c_uint], c.c_int),
        "sqlite3_prepare_v2": ([c.c_void_p, c.c_char_p, c.c_int, c.POINTER(c.c_void_p), c.c_void_p], c.c_int),
        "sqlite3_step": ([c.c_void_p], c.c_int),
        "sqlite3_finalize": ([c.c_void_p], c.c_int),
        "sqlite3_column_count": ([c.c_void_p], c.c_int),
        "sqlite3_column_text": ([c.c_void_p, c.c_int], c.c_void_p),
        "sqlite3_column_bytes": ([c.c_void_p, c.c_int], c.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    return library


def contact_names(plain):
    """只查询微信名，不查询备注、聊天、群记录或其他资料。调用方负责清零 plain。"""
    from .account import validate_account
    sql = _sqlite_library()
    db = c.c_void_p()
    # 强引用缓冲区直到 sqlite3_close；不能让 SQLite 释放 Python 分配的内存。
    buffer = c.create_string_buffer(len(plain) + 20)  # SQLite 建议保留少量越界读防护空间。
    c.memmove(buffer, (c.c_ubyte * len(plain)).from_buffer(plain), len(plain))

    def query(statement):
        stmt = c.c_void_p()
        try:
            _require(sql.sqlite3_prepare_v2(db, statement.encode(), -1, c.byref(stmt), None) == 0,
                     "联系人库查询准备失败")
            rows = []
            while True:
                status = sql.sqlite3_step(stmt)
                if status == 101:
                    return rows
                _require(status == 100, "联系人库查询失败")
                row = []
                for col in range(sql.sqlite3_column_count(stmt)):
                    pointer = sql.sqlite3_column_text(stmt, col)
                    size = sql.sqlite3_column_bytes(stmt, col)
                    row.append(c.string_at(pointer, size).decode("utf-8") if pointer else "")
                rows.append(tuple(row))
        finally:
            if stmt.value:
                sql.sqlite3_finalize(stmt)

    try:
        _require(sql.sqlite3_open(b":memory:", c.byref(db)) == 0, "内存 SQLite 打开失败")
        query("PRAGMA temp_store=MEMORY")
        _require(sql.sqlite3_deserialize(db, b"main", buffer, len(plain), len(plain), 4) == 0,
                 "内存 SQLite 载入失败")  # SQLITE_DESERIALIZE_READONLY
        query("PRAGMA query_only=ON")
        query("PRAGMA trusted_schema=OFF")
        _require(query("PRAGMA quick_check") == [("ok",)], "联系人库完整性校验失败")
        _require(query("SELECT type FROM sqlite_master WHERE name='contact'") == [("table",)],
                 "联系人表结构未确认")
        result = {}
        for wxid, name in query("SELECT username,nick_name FROM contact"):
            try:
                validate_account(wxid)
            except ValueError:
                continue
            if name.strip() and len(name) <= 256 and not any(ord(char) < 32 or ord(char) == 127 for char in name):
                _require(wxid not in result, "联系人唯一 ID 重复")
                result[wxid] = name
        return result
    finally:
        if db.value:
            sql.sqlite3_close(db)
        c.memset(buffer, 0, len(buffer))
