"""账号隔离的后台联系人名字补全；主消息线程只读内存字典。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import sys
import threading
import time

from .account import read_current_account, validate_account
from .contact_database import (ContactDatabaseError, _require, read_snapshot,
                               decrypt_snapshot, contact_names, wipe)
from .contact_keys import read_contact_key

logger = logging.getLogger("ContactNames")


def default_data_roots():
    """默认 Documents（含重定向）和本地盘根目录；不递归扫磁盘或网络盘。"""
    home = Path.home()
    roots = [home / "Documents/xwechat_files", home / "xwechat_files"]
    if sys.platform == "win32":
        import ctypes as c
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                documents = os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0])
                roots.append(Path(documents) / "xwechat_files")
        except OSError:
            pass
        kernel = c.WinDLL("kernel32")
        kernel.GetDriveTypeW.argtypes = [c.c_wchar_p]
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = letter + ":\\"
            if kernel.GetDriveTypeW(drive) in (2, 3):
                roots.append(Path(drive) / "xwechat_files")
    return tuple(dict.fromkeys(roots))


def find_contact_database(account, directory=None, *, roots=None):
    """只接受唯一且目录身份吻合的 contact.db；绝不按时间挑账号。"""
    validate_account(account)

    def matches(name):
        return name == account or re.fullmatch(re.escape(account) + r"_[0-9a-fA-F]{4}", name) is not None

    candidates = set()
    search = [Path(directory)] if directory is not None else (default_data_roots() if roots is None else roots)
    for root in search:
        root = Path(root)
        # 已配置的账号目录、db_storage、contact 目录，或 contact.db 本身。
        account_dir = (root.parent.parent.parent if root.name == "contact.db" else
                       root.parent.parent if root.name == "contact" else
                       root.parent if root.name == "db_storage" else root)
        if matches(account_dir.name):
            paths = [account_dir / "db_storage/contact/contact.db"]
        elif root.is_dir():
            try:
                paths = [child / "db_storage/contact/contact.db" for child in root.iterdir()
                         if matches(child.name) and child.is_dir()]
            except OSError:
                if directory is not None:
                    raise ContactDatabaseError("联系人配置目录不可读") from None
                continue
        else:
            paths = []
        for path in paths:
            if path.is_file():
                resolved = path.resolve()
                _require(matches(resolved.parents[2].name), "联系人目录与绑定账号不匹配")
                candidates.add(resolved)
    _require(len(candidates) == 1, "联系人目录未找到或存在歧义，可配置 wechat_data_directory")
    return next(iter(candidates))


def _stamp(path):
    result = [str(path)]
    for file in (path, path.with_name("contact.db-wal")):
        try:
            stat = file.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def load_contact_names(account, process_factory, directory=None, *, previous_stamp=None, cancelled=lambda: False):
    """一次后台读取；未改变的加密文件只检查身份，不重复找密钥、解密。"""
    def check():
        _require(not cancelled(), "联系人读取已取消")

    key = plain = None
    try:
        check()
        with process_factory() as process:
            _require(read_current_account(process.read, process.base) == account, "联系人账号不匹配")
            path = find_contact_database(account, directory)
            stamp = _stamp(path)
            if stamp == previous_stamp:
                check()
                _require(read_current_account(process.read, process.base) == account, "联系人账号发生变化")
                return stamp, None
            main, wal = read_snapshot(path)
            key = read_contact_key(process, account, main[:4096], check)
            check()
            plain = decrypt_snapshot(main, wal, key, check=check)
            names = contact_names(plain)
            check()
            _require(stamp == _stamp(path), "联系人库读取期间发生变化")
            _require(read_current_account(process.read, process.base) == account, "联系人账号发生变化")
            return stamp, names
    finally:
        wipe(plain)
        wipe(key)


class ContactNames:
    """每个已登录连接独立实例；后台更新，不阻塞收发，不写持久化缓存。"""

    def __init__(self, account, loader, *, refresh_seconds=60, retry_seconds=30):
        self.account = validate_account(account)
        self._loader = loader
        self._refresh, self._retry = refresh_seconds, retry_seconds
        self._names = {}
        self._stamp = None
        self._updated = 0
        self._status = "loading"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="WxContactNames", daemon=True)
        self._thread.start()

    @property
    def status(self):
        with self._lock:
            return self._status

    def get(self, wxid, *, account):
        with self._lock:
            if (self._stop.is_set() or account != self.account or
                    time.monotonic() - self._updated > max(self._refresh * 2, 120)):
                return None
            return self._names.get(wxid)

    def _run(self):
        while not self._stop.is_set():
            delay = self._refresh
            try:
                stamp, names = self._loader(previous_stamp=self._stamp, cancelled=self._stop.is_set)
                with self._lock:
                    if self._stop.is_set():
                        return
                    if names is not None:
                        self._names = dict(names)
                    self._stamp, self._updated, self._status = stamp, time.monotonic(), "ready"
            except Exception:
                # 不打印异常 repr/traceback，第三方库异常也不能带出页面或密钥。
                with self._lock:
                    if self._stop.is_set():
                        return
                    prior = self._status
                    self._names.clear()
                    self._stamp, self._status = None, "unavailable"
                if not self._stop.is_set() and prior != "unavailable":
                    logger.warning("联系人名字补全暂不可用，保留原有缓存并稍后重试")
                delay = self._retry
            self._stop.wait(delay)

    def close(self):
        self._stop.set()
        with self._lock:
            self._names.clear()
            self._stamp, self._status = None, "stopped"
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
