"""合成联系人库和进程快照；不访问微信，不写真实密钥或明文数据库。"""
import ctypes as c
import hashlib
import hmac
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wxbot_framework import contact_database as db
from wxbot_framework import contact_keys as keys

SELF, PEER = "wxid_synthetic_self", "wxid_synthetic_peer"
KEY, SALT = bytes(range(32)), bytes(range(32, 48))


def sqlite_image(name="合成群友", *, extra_sql="", schema="CREATE TABLE contact(username TEXT PRIMARY KEY,nick_name TEXT,remark TEXT)"):
    """用 SQLite 内存 C API 构造带 80 字节保留区的测试数据库。"""
    sql = db._sqlite_library()
    signatures = {
        "sqlite3_exec": ([c.c_void_p, c.c_char_p, c.c_void_p, c.c_void_p, c.c_void_p], c.c_int),
        "sqlite3_file_control": ([c.c_void_p, c.c_char_p, c.c_int, c.c_void_p], c.c_int),
        "sqlite3_serialize": ([c.c_void_p, c.c_char_p, c.POINTER(c.c_int64), c.c_uint], c.c_void_p),
        "sqlite3_free": ([c.c_void_p], None),
    }
    for symbol, (args, result) in signatures.items():
        getattr(sql, symbol).argtypes, getattr(sql, symbol).restype = args, result
    handle = c.c_void_p()
    assert sql.sqlite3_open(b":memory:", c.byref(handle)) == 0
    try:
        reserved = c.c_int(80)
        assert sql.sqlite3_file_control(handle, b"main", 38, c.byref(reserved)) == 0
        query = ("PRAGMA page_size=4096; " + schema + "; INSERT INTO contact VALUES ('" + PEER + "','" +
                 name.replace("'", "''") + "','不应读取的备注');" + extra_sql)
        assert sql.sqlite3_exec(handle, query.encode(), None, None, None) == 0
        size = c.c_int64()
        pointer = sql.sqlite3_serialize(handle, b"main", c.byref(size), 0)
        assert pointer
        try:
            return bytearray(c.string_at(pointer, size.value))
        finally:
            sql.sqlite3_free(pointer)
    finally:
        sql.sqlite3_close(handle)


def encrypt(image, key=KEY, salt=SALT):
    result = bytearray()
    hk = db.mac_key(key, salt)
    for number, offset in enumerate(range(0, len(image), db.PAGE), 1):
        page = image[offset:offset+db.PAGE]
        skip = 16 if number == 1 else 0
        iv = hashlib.sha256(struct.pack("<I", number)).digest()[:16]
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        body = enc.update(page[skip:db.PAGE-80]) + enc.finalize() + iv
        tag = hmac.new(hk, body + struct.pack("<I", number), "sha512").digest()
        result.extend((salt if number == 1 else b"") + body + tag)
    return bytes(result)


def wal_file(frames, *, big_endian=False, stale=False):
    order = ">" if big_endian else "<"
    def checksum(data, previous=(0, 0)):
        words = struct.unpack(order+"I"*(len(data)//4), data)
        a, b = previous
        for i in range(0, len(words), 2):
            a = (a + words[i] + b) & 0xFFFFFFFF
            b = (b + words[i+1] + a) & 0xFFFFFFFF
        return a, b
    epoch = b"epoch123"
    header = struct.pack(">IIII", 0x377F0683 if big_endian else 0x377F0682, 3007000, db.PAGE, 1) + epoch
    current = checksum(header)
    result = header + struct.pack(">II", *current)
    for number, body, commit in frames:
        prefix = struct.pack(">II", number, commit)
        current = checksum(prefix+body, current)
        result += prefix + (b"old12345" if stale else epoch) + struct.pack(">II", *current) + body
    return result


class ContactDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.image = sqlite_image()
        self.main = encrypt(self.image)
        self.addCleanup(db.wipe, self.image)

    def test_authenticated_roundtrip_and_no_remark(self):
        self.assertTrue(db.authenticates(KEY, self.main[:db.PAGE]))
        plain = db.decrypt_snapshot(self.main, b"", KEY)
        try:
            self.assertEqual(db.contact_names(plain), {PEER: "合成群友"})
            self.assertEqual(plain, self.image)
        finally:
            db.wipe(plain)
        self.assertFalse(any(plain))

    def test_wrong_key_and_tampering_are_rejected_and_error_is_redacted(self):
        self.assertFalse(db.authenticates(bytes(32), self.main[:db.PAGE]))
        for pos in (32, db.PAGE+100, len(self.main)-1):
            damaged = bytearray(self.main)
            damaged[pos] ^= 1
            with self.subTest(pos=pos), self.assertRaises(db.ContactDatabaseError) as caught:
                db.decrypt_snapshot(damaged, b"", KEY)
            self.assertNotIn(KEY.hex(), str(caught.exception))
            self.assertNotIn("合成群友", str(caught.exception))

    def test_committed_wal_overlays_main_but_uncommitted_tail_does_not(self):
        newer = sqlite_image("合成新名字")
        self.addCleanup(db.wipe, newer)
        encrypted = encrypt(newer)
        total = len(newer)//db.PAGE
        frames = [(n, encrypted[(n-1)*db.PAGE:n*db.PAGE], total if n == total else 0)
                  for n in range(1, total+1)]
        frames.append((2, self.main[db.PAGE:db.PAGE*2], 0))
        for endian in (False, True):
            plain = db.decrypt_snapshot(self.main, wal_file(frames, big_endian=endian), KEY)
            try:
                self.assertEqual(db.contact_names(plain)[PEER], "合成新名字")
            finally:
                db.wipe(plain)

    def test_checkpoint_residual_old_epoch_is_ignored(self):
        plain = db.decrypt_snapshot(self.main, wal_file([(1, self.main[:db.PAGE], 3)], stale=True), KEY)
        try:
            self.assertEqual(db.contact_names(plain)[PEER], "合成群友")
        finally:
            db.wipe(plain)

    def test_uncommitted_or_bad_checksum_wal_does_not_publish_a_transaction(self):
        good = wal_file([(1, self.main[:db.PAGE], 0)])
        bad = bytearray(wal_file([(1, self.main[:db.PAGE], 3)]))
        bad[-1] ^= 1
        for wal in (good, bad, good[:-17]):
            plain = db.decrypt_snapshot(self.main, wal, KEY)
            try:
                self.assertEqual(db.contact_names(plain)[PEER], "合成群友")
            finally:
                db.wipe(plain)

    def test_authenticated_wal_is_still_checked_per_encrypted_page(self):
        corrupt = bytearray(self.main[db.PAGE:db.PAGE*2]); corrupt[50] ^= 1
        with self.assertRaisesRegex(db.ContactDatabaseError, "页面认证"):
            db.decrypt_snapshot(self.main, wal_file([(2, corrupt, 3)]), KEY)

    def test_bad_header_and_missing_or_oversized_pages_fail_closed(self):
        cases = [b"short", bytes(32), wal_file([(4, self.main[:db.PAGE], 4)]),
                 wal_file([(1, self.main[:db.PAGE], db.MAX_DB//db.PAGE+1)])]
        for wal in cases:
            with self.subTest(length=len(wal)), self.assertRaises(db.ContactDatabaseError):
                db.decrypt_snapshot(self.main, wal, KEY)

    def test_cancellation_wipes_partially_decrypted_image(self):
        buffers = []
        real_wipe = db.wipe
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise db.ContactDatabaseError("取消")
        def capture(buffer):
            buffers.append(buffer)
            real_wipe(buffer)
        with patch.object(db, "wipe", side_effect=capture), self.assertRaises(db.ContactDatabaseError):
            db.decrypt_snapshot(self.main, b"", KEY, check=check)
        self.assertTrue(buffers)
        self.assertTrue(all(not any(buffer) for buffer in buffers))

    def test_snapshot_only_reads_contact_and_does_not_create_shm(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"contact.db"
            path.write_bytes(self.main)
            self.assertEqual(db.read_snapshot(path), (self.main, b""))
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["contact.db"])
            with self.assertRaisesRegex(db.ContactDatabaseError, "只允许"):
                db.read_snapshot(path.with_name("message_0.db"))
            with patch.object(db, "MAX_DB", 32), self.assertRaises(db.ContactDatabaseError):
                db.read_snapshot(path)

    def test_duplicate_id_is_rejected_and_group_rows_not_returned(self):
        image = sqlite_image(extra_sql="INSERT INTO contact VALUES('12345678@chatroom','群名','');")
        try:
            self.assertEqual(db.contact_names(image), {PEER: "合成群友"})
        finally:
            db.wipe(image)
        image = sqlite_image(schema="CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)",
                             extra_sql="INSERT INTO contact VALUES('"+PEER+"','重复名字','');")
        try:
            with self.assertRaisesRegex(db.ContactDatabaseError, "唯一"):
                db.contact_names(image)
        finally:
            db.wipe(image)

    def test_empty_and_control_character_names_are_not_used(self):
        for expression in ("''", "'   '", "'前半' || char(0) || '后半'", "'名字' || char(10)", "char(127)"):
            image = sqlite_image(extra_sql="UPDATE contact SET nick_name="+expression+";")
            try:
                self.assertEqual(db.contact_names(image), {})
            finally:
                db.wipe(image)
class KeyMemory:
    base = 0x100000
    anchor, start, config, pointer = 0x200000, 0x300000, 0x400000, 0x500000

    def __init__(self, key=KEY, salt=SALT):
        self.blocks = {}
        region = bytearray(4096)
        struct.pack_into("<QQ", region, 0x110, self.anchor, len(keys._NAME))
        struct.pack_into("<Q", region, 0x128, self.config)
        obj = bytearray(40)
        literal = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
        blob = bytes(v ^ keys._MASK[i % 32] for i, v in enumerate(literal))
        struct.pack_into("<QQ", obj, 8, self.pointer, len(blob))
        self.blocks[self.start] = region
        self.blocks[self.config+0x88] = obj
        self.blocks[self.pointer] = blob

    def read(self, address, size):
        for start, data in self.blocks.items():
            if start <= address and address+size <= start+len(data):
                return bytes(data[address-start:address-start+size])
        return b""

    def regions(self):
        return [(self.start, 4096)]

    def anchors(self, check):
        check()
        return {self.anchor}


class ContactKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = sqlite_image()
        try:
            cls.page1 = encrypt(image)[:db.PAGE]
        finally:
            db.wipe(image)

    def test_target_salt_and_page_hmac_are_both_required(self):
        with patch.object(keys, "read_current_account", return_value=SELF):
            key = keys.read_contact_key(KeyMemory(), SELF, self.page1)
            try:
                self.assertEqual(key, KEY)
            finally:
                db.wipe(key)
            for memory in (KeyMemory(salt=bytes(16)), KeyMemory(key=bytes(32))):
                with self.assertRaises(db.ContactDatabaseError):
                    keys.read_contact_key(memory, SELF, self.page1)

    def test_account_switch_or_unstable_blob_cannot_return_a_key(self):
        with patch.object(keys, "read_current_account", side_effect=[SELF, "wxid_other"]), self.assertRaises(db.ContactDatabaseError):
            keys.read_contact_key(KeyMemory(), SELF, self.page1)
        memory = KeyMemory()
        original = memory.read
        reads = 0
        def unstable(address, size):
            nonlocal reads
            if address == memory.pointer:
                reads += 1
                if reads > 1:
                    return bytes(size)
            return original(address, size)
        memory.read = unstable
        with patch.object(keys, "read_current_account", return_value=SELF), self.assertRaises(db.ContactDatabaseError):
            keys.read_contact_key(memory, SELF, self.page1)

    def test_wrong_initial_account_does_not_scan(self):
        memory = KeyMemory()
        with patch.object(keys, "read_current_account", return_value="wxid_other"), patch.object(memory, "regions") as scan:
            with self.assertRaises(db.ContactDatabaseError):
                keys.read_contact_key(memory, SELF, self.page1)
            scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
