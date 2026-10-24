"""框架共用的成员身份和新收到的群文本记录，按账号、群及成员 ID 隔离。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .account import validate_account
from .config import validate_name
from .text import validate_mentions, validate_target


class MemberError(ValueError):
    pass


class SnapshotExpired(RuntimeError):
    """清空或跨天后，取消仍在生成/等待发送的总结。"""


BEIJING = timezone(timedelta(hours=8))
MEMORY_LINE_CHARS = 500  # 智能体记忆里单条消息最多带的字数，防止长段粘贴挤占记忆


@dataclass(frozen=True)
class ChatSnapshot:
    member: Member
    day: str
    generation: int
    messages: tuple

    @property
    def room(self):
        return self.member.room


@dataclass(frozen=True)
class GroupChatSnapshot:
    room: str
    day: str
    generation: int
    messages: tuple


@dataclass(frozen=True)
class ConversationSnapshot:
    room: str
    day: str
    generation: int
    speaker_name: str
    messages: tuple
    current: str


@dataclass(frozen=True)
class Member:
    room: str
    name: str
    display_name: str
    wxid: str | None
    aliases: tuple[str, ...]
    self_named: bool = False


@dataclass(frozen=True)
class VoiceSelection:
    room: str
    voice_id: str = ""
    name: str = ""
    enabled: bool = True
    generation: int = 0


def name_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(c in value for c in "\0\n\r"):
        raise MemberError("成员称呼应为 1–64 字符的单行文本")
    return value.strip().casefold()


class MemberStore:
    def __init__(self, config_file, account, *, now=None):
        self.account = validate_account(account)
        self.config_file = Path(config_file).resolve()
        options = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        if not isinstance(options, dict) or not isinstance(options.get("groups"), dict):
            raise MemberError("成员配置应包含 groups 对象")
        database = options.get("database_file", ".runtime/member_data.sqlite3")
        if not isinstance(database, str) or not database.strip() or "\0" in database:
            raise MemberError("database_file 应为数据库路径")
        self.path = (self.config_file.parent / database).resolve()
        self.history_enabled = options.get("history_enabled", False)
        if type(self.history_enabled) is not bool:
            raise MemberError("history_enabled 必须是布尔值")
        self.daily_clear = options.get("daily_clear", False)
        if type(self.daily_clear) is not bool:
            raise MemberError("daily_clear 必须是布尔值")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._active_day = None
        # 记录哪些会话：配置里的群默认记录；聊天页的开关（chat_settings 表）优先，打开数据库时读入。
        self.groups = set(options["groups"])
        self.private_chats, self.reply_chats = set(), set()
        self.profiles = []
        for room, profiles in options["groups"].items():
            validate_mentions(room, [self.account])
            if not isinstance(profiles, list):
                raise MemberError("每个群的成员配置应为列表")
            keys, names, ids = set(), set(), set()
            for profile in profiles:
                if not isinstance(profile, dict):
                    raise MemberError("成员资料应为 JSON 对象")
                key = validate_name(profile.get("key"))
                name = profile.get("name")
                display = profile.get("display_name", name)
                aliases = profile.get("aliases", [])
                if not isinstance(aliases, list):
                    raise MemberError("aliases 应为称呼列表")
                normalized = {name_key(value) for value in [name, display, *aliases]}
                wxid = profile.get("wxid")
                if wxid is not None:
                    validate_mentions(room, [wxid])
                if key in keys or normalized & names or (wxid and wxid in ids):
                    raise MemberError("同一群的成员标识、称呼和微信 ID 不能重复")
                keys.add(key)
                names.update(normalized)
                if wxid:
                    ids.add(wxid)
                self.profiles.append((room, key, name.strip(), display.strip(), wxid, normalized))
        self._lock = threading.RLock()
        self._db = None
        self._closed = False

    @classmethod
    def from_config(cls, config, directory):
        filename = config.get("members_config_file")
        if filename is None:
            return None
        if not isinstance(filename, str) or not filename.strip() or "\0" in filename:
            raise MemberError("members_config_file 应为成员配置文件路径或 null")
        if not config.get("wechat_account"):
            raise MemberError("启用成员目录需要固定 wechat_account，以隔离各机器人账号的数据")
        return cls(Path(directory) / filename, config["wechat_account"])

    def _connection(self):
        if self._closed:
            raise RuntimeError("成员目录已关闭")
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS people (
                    account TEXT NOT NULL, room TEXT NOT NULL, person_key TEXT NOT NULL,
                    name TEXT NOT NULL, display_name TEXT NOT NULL, wxid TEXT,
                    PRIMARY KEY(account, room, person_key), UNIQUE(account, room, wxid));
                CREATE TABLE IF NOT EXISTS aliases (
                    account TEXT NOT NULL, room TEXT NOT NULL, alias TEXT NOT NULL, person_key TEXT NOT NULL,
                    PRIMARY KEY(account, room, alias),
                    FOREIGN KEY(account, room, person_key) REFERENCES people(account, room, person_key));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, sender_id TEXT NOT NULL,
                    text TEXT NOT NULL, received_at TEXT NOT NULL, session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, is_self INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_by_member ON messages(account, room, sender_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS messages_by_sequence
                    ON messages(account, session_id, sequence) WHERE sequence > 0;
                CREATE TABLE IF NOT EXISTS chat_buffers (
                    account TEXT NOT NULL, room TEXT NOT NULL, local_day TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS room_personalities (
                    account TEXT NOT NULL, room TEXT NOT NULL, name TEXT NOT NULL,
                    generation INTEGER NOT NULL, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS wechat_names (
                    account TEXT NOT NULL, wxid TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
                    PRIMARY KEY(account,wxid));
                CREATE TABLE IF NOT EXISTS room_members (
                    account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
                    PRIMARY KEY(account,room,wxid));
                CREATE TABLE IF NOT EXISTS group_nicknames (
                    account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL, nickname TEXT NOT NULL,
                    PRIMARY KEY(account,room,wxid));
                CREATE TABLE IF NOT EXISTS room_voices (
                    account TEXT NOT NULL, room TEXT NOT NULL, voice_id TEXT NOT NULL,
                    name TEXT NOT NULL, enabled INTEGER NOT NULL, generation INTEGER NOT NULL,
                    PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS chat_settings (
                    account TEXT NOT NULL, chat TEXT NOT NULL, memory INTEGER NOT NULL, reply INTEGER NOT NULL,
                    cleared_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(account,chat));
                CREATE TABLE IF NOT EXISTS agent_summaries (
                    account TEXT NOT NULL, chat TEXT NOT NULL, summary TEXT NOT NULL, upto INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account,chat));
                INSERT OR IGNORE INTO room_members SELECT DISTINCT account,room,sender_id FROM messages;
            """)
            if "self_named" not in {row["name"] for row in db.execute("PRAGMA table_info(people)")}:
                db.execute("ALTER TABLE people ADD COLUMN self_named INTEGER NOT NULL DEFAULT 0")
            if "bot_generated" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN bot_generated INTEGER NOT NULL DEFAULT 0")
            if "upto" not in {row["name"] for row in db.execute("PRAGMA table_info(agent_summaries)")}:
                db.execute("ALTER TABLE agent_summaries ADD COLUMN upto INTEGER NOT NULL DEFAULT 0")
            if "cleared_at" not in {row["name"] for row in db.execute("PRAGMA table_info(chat_settings)")}:
                db.execute("ALTER TABLE chat_settings ADD COLUMN cleared_at TEXT NOT NULL DEFAULT ''")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_turns'").fetchone():
                # 旧版单独的问答表：摘要游标 upto 指的是它的 id，换成聊天记录后对不上，作废旧摘要和旧表。
                with db:
                    db.execute("DELETE FROM agent_summaries")
                    db.execute("DROP TABLE agent_turns")
            for row in db.execute("SELECT chat,memory,reply FROM chat_settings WHERE account=?", (self.account,)):
                self._apply_setting(row["chat"], bool(row["memory"]), bool(row["reply"]))
            with db:
                for room, key, name, display, wxid, aliases in self.profiles:
                    existing = db.execute("SELECT wxid FROM people WHERE account=? AND room=? AND person_key=?",
                                          (self.account, room, key)).fetchone()
                    if existing and existing["wxid"] and wxid and existing["wxid"] != wxid:
                        raise MemberError(f"{name} 的配置 ID 与已保存绑定不同，请先核对")
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(account,room,person_key) DO UPDATE SET
                            name=CASE WHEN people.self_named=0 THEN excluded.name ELSE people.name END,
                            display_name=CASE WHEN people.self_named=0 THEN excluded.display_name ELSE people.display_name END,
                            wxid=COALESCE(people.wxid,excluded.wxid)""", (self.account, room, key, name, display, wxid))
                    for alias in aliases:
                        old = db.execute("SELECT person_key FROM aliases WHERE account=? AND room=? AND alias=?",
                                         (self.account, room, alias)).fetchone()
                        if old and old["person_key"] != key:
                            raise MemberError(f"称呼 {alias} 已属于另一位成员")
                        db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            self._db = db
            return db
        except BaseException:
            db.close()
            raise

    def initialize(self):
        with self._lock:
            self._connection()
            self.rollover()

    def _day_bounds(self):
        local = self._now().astimezone(BEIJING)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.date().isoformat(), start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())

    def rollover(self):
        """即使没有新消息也可调用；停机期间跨天则在启动时补清理。"""
        with self._lock:
            day, start, _ = self._day_bounds()
            if day == self._active_day:
                return False
            with self._connection() as db:
                for room in self.groups:
                    db.execute("""INSERT INTO chat_buffers(account,room,local_day) VALUES(?,?,?)
                        ON CONFLICT(account,room) DO UPDATE SET local_day=excluded.local_day,
                        generation=chat_buffers.generation+1 WHERE chat_buffers.local_day<>excluded.local_day""",
                               (self.account, room, day))
                    if self.daily_clear:
                        db.execute("DELETE FROM messages WHERE account=? AND room=? AND received_at<?",
                                   (self.account, room, start))
                        db.execute("DELETE FROM agent_summaries WHERE account=? AND chat=?", (self.account, room))
            self._active_day = day
            return True

    def clear(self, room):
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with self._connection() as db:
                count = db.execute("DELETE FROM messages WHERE account=? AND room=?", (self.account, room)).rowcount
                db.execute("DELETE FROM agent_summaries WHERE account=? AND chat=?", (self.account, room))
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return count

    def today_snapshot(self, name, room):
        """完整读取当天的成员文本，数量不受控制台 history 的条数限制。"""
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            if state is None:
                raise MemberError("这个群尚未启用聊天记录")
            rows = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND received_at>=? AND received_at<? AND bot_generated=0 ORDER BY id""",
                              (self.account, room, member.wxid, start, end)).fetchall()
            return ChatSnapshot(member, day, state[0], tuple(dict(row) for row in rows))

    def today_group_snapshot(self, room):
        """完整读取本群今天所有成员的文本，未绑定者也保留身份区分。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            rows = db.execute("""SELECT m.*, p.name AS speaker_name FROM messages m
                LEFT JOIN people p ON p.account=m.account AND p.room=m.room AND p.wxid=m.sender_id
                WHERE m.account=? AND m.room=? AND m.received_at>=? AND m.received_at<?
                AND m.bot_generated=0 ORDER BY m.id""", (self.account, room, start, end)).fetchall()
            speakers, messages = {}, []
            for row in rows:
                item = dict(row)
                number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
                # 发给模型的稳定编号替代真实微信 ID；昵称只用于可读性。
                item["speaker"] = f"P{number}"
                item["speaker_name"] = item["speaker_name"] or self.display_name(item["sender_id"], room) or f"未绑定成员{number}"
                messages.append(item)
            return GroupChatSnapshot(room, day, state[0], tuple(messages))

    def snapshot_is_current(self, snapshot):
        with self._lock:
            self.rollover()
            state = self._connection().execute("SELECT local_day,generation FROM chat_buffers WHERE account=? AND room=?",
                                               (self.account, snapshot.room)).fetchone()
            return bool(state and state[0] == snapshot.day and state[1] == snapshot.generation)

    def personality(self, room):
        from .personality import PersonalityState
        with self._lock:
            row = self._connection().execute("SELECT name,generation FROM room_personalities WHERE account=? AND room=?",
                                              (self.account, room)).fetchone()
            return PersonalityState(room, row[0], row[1]) if row else PersonalityState(room)

    def set_personality(self, room, name):
        from .personality import PROMPTS
        if room not in self.groups or (name not in PROMPTS and name != "默认"):
            raise MemberError("人格应为 嘴臭、卑微、夸夸 或 默认，且在已启用的群中切换")
        with self._lock:
            current = self.personality(room)
            if current.name == name:
                return current
            with self._connection() as db:
                db.execute("""INSERT INTO room_personalities VALUES(?,?,?,1)
                    ON CONFLICT(account,room) DO UPDATE SET name=excluded.name,
                    generation=room_personalities.generation+1 WHERE room_personalities.name<>excluded.name""",
                           (self.account, room, name))
            return self.personality(room)

    def personality_is_current(self, state):
        return self.personality(state.room) == state

    def voice_selection(self, room):
        with self._lock:
            row = self._connection().execute(
                "SELECT voice_id,name,enabled,generation FROM room_voices WHERE account=? AND room=?",
                (self.account, room)).fetchone()
            return VoiceSelection(room, row[0], row[1], bool(row[2]), row[3]) if row else VoiceSelection(room)

    def set_voice(self, room, voice_id, name, *, enabled=True):
        from .tts import VOICE_ID
        if room not in self.groups or type(enabled) is not bool:
            raise MemberError("请在已配置的群中切换音色")
        if enabled and (not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id)):
            raise MemberError("音色 ID 无效")
        if not isinstance(name, str) or len(name) > 128 or any(ord(ch) < 32 for ch in name):
            raise MemberError("音色名称无效")
        with self._lock, self._connection() as db:
            current = self.voice_selection(room)
            if not enabled:
                voice_id, name = current.voice_id, current.name
            if (current.voice_id, current.name, current.enabled) != (voice_id, name, enabled):
                db.execute("""INSERT INTO room_voices VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account,room) DO UPDATE SET voice_id=excluded.voice_id,
                    name=excluded.name,enabled=excluded.enabled,generation=excluded.generation""",
                    (self.account, room, voice_id, name, int(enabled), current.generation + 1))
            return self.voice_selection(room)

    @contextmanager
    def voice_delivery_guard(self, state):
        with self._lock:
            if self.voice_selection(state.room) != state:
                raise SnapshotExpired("音色已切换，取消旧语音")
            yield

    @contextmanager
    def personality_delivery_guard(self, state):
        from .personality import PersonalityExpired
        with self._lock:
            if not self.personality_is_current(state):
                raise PersonalityExpired("人格已切换")
            yield

    def conversation_snapshot(self, message):
        """当前发言者在本群、当前消息之前的 10 条有效文本，不混入后来消息。"""
        from .summary import CONTROL
        if message.chat_id not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = self._connection()
            current = db.execute("""SELECT id FROM messages WHERE account=? AND room=? AND sender_id=?
                AND session_id=? AND sequence=? AND text=? AND received_at>=? AND received_at<?
                ORDER BY id DESC LIMIT 1""", (self.account, message.chat_id, message.sender_id,
                message.session_id, message.sequence, message.text, start, end)).fetchone()
            if current is None:
                raise SnapshotExpired("当前消息已被清空或已跨天")
            cursor = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND id<? AND received_at>=? AND received_at<? AND bot_generated=0 ORDER BY id DESC""",
                (self.account, message.chat_id, message.sender_id, current[0], start, end))
            previous = []
            for row in cursor:
                text = row["text"].strip()
                if text and "WXBOT_AT_CHAT_PROBE" not in text and text.split(maxsplit=1)[0] not in CONTROL:
                    previous.append(dict(row))
                    if len(previous) == 10:
                        break
            member = self.find_by_wxid(message.sender_id, message.chat_id)
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, message.chat_id)).fetchone()
            return ConversationSnapshot(message.chat_id, day, state[0], member.name if member else
                                        self.display_name(message.sender_id, message.chat_id) or "群友",
                                        tuple(reversed(previous)), message.text)

    @contextmanager
    def delivery_guard(self, snapshot):
        # 仅原生发送期间持锁，随机等待和大模型调用不会阻塞 /清空。
        with self._lock:
            if not self.snapshot_is_current(snapshot):
                raise SnapshotExpired("聊天记录已清空或已跨天")
            yield

    def _member(self, row):
        if row["person_key"] is None:
            return Member(row["room"], row["name"], row["name"], row["wxid"], (), False)
        aliases = self._connection().execute(
            "SELECT alias FROM aliases WHERE account=? AND room=? AND person_key=? ORDER BY alias",
            (self.account, row["room"], row["person_key"])).fetchall()
        return Member(row["room"], row["name"], row["display_name"], row["wxid"], tuple(r[0] for r in aliases),
                      bool(row["self_named"]))

    def remember_wechat_name(self, wxid, name):
        """微信原始名字按账号 ID 缓存，不产生任何群内绑定或别名。"""
        validate_account(wxid)
        normalized = name_key(name)
        if any(ord(ch) < 32 for ch in name):
            raise MemberError("微信名不能包含控制字符")
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO wechat_names VALUES(?,?,?,?) ON CONFLICT(account,wxid)
                DO UPDATE SET name=excluded.name,name_key=excluded.name_key""",
                (self.account, wxid, name.strip(), normalized))

    def wechat_name(self, wxid):
        with self._lock:
            row = self._connection().execute("SELECT name FROM wechat_names WHERE account=? AND wxid=?",
                                             (self.account, wxid)).fetchone()
            return row[0] if row else None

    def speaker_name(self, wxid, chat):
        """给模型和聊天页看的发言人名字；读不到时群里叫“群友”，私聊叫“对方”。"""
        return self.display_name(wxid, chat) or ("群友" if chat.endswith("@chatroom") else "对方")

    def display_name(self, wxid, room):
        """群内绑定优先；其他群的绑定永远不作为默认名字。"""
        member = self.find_by_wxid(wxid, room)
        return member.display_name if member else self.group_nickname(wxid, room) or self.wechat_name(wxid)

    def remember_group_nickname(self, wxid, room, nickname):
        """微信群昵称按账号、群和成员隔离，不覆盖 /绑定 的称呼。"""
        validate_mentions(room, [wxid])
        if not isinstance(nickname, str) or len(nickname) > 64 or any(ord(ch) < 32 for ch in nickname):
            raise MemberError("群昵称应为最多 64 字符的单行文本")
        with self._lock, self._connection() as db:
            if nickname.strip():
                db.execute("""INSERT INTO group_nicknames VALUES(?,?,?,?)
                    ON CONFLICT(account,room,wxid) DO UPDATE SET nickname=excluded.nickname""",
                    (self.account, room, wxid, nickname.strip()))
            else:
                db.execute("DELETE FROM group_nicknames WHERE account=? AND room=? AND wxid=?",
                           (self.account, room, wxid))

    def group_nickname(self, wxid, room):
        with self._lock:
            row = self._connection().execute(
                "SELECT nickname FROM group_nicknames WHERE account=? AND room=? AND wxid=?",
                (self.account, room, wxid)).fetchone()
            return row[0] if row else None

    def _row(self, name, room=None):
        sql = """SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                 WHERE a.account=? AND a.alias=?"""
        values = [self.account, name_key(name)]
        if room is not None:
            sql += " AND a.room=?"
            values.append(room)
        rows = self._connection().execute(sql, values).fetchall()
        if not rows:
            # 未绑定也可按已核对的微信名查询，但必须确实在这个群说过话。
            query = """SELECT DISTINCT m.room,n.name,n.wxid FROM wechat_names n JOIN room_members m
                ON m.account=n.account AND m.wxid=n.wxid WHERE n.account=? AND n.name_key=?"""
            parameters = [self.account, name_key(name)]
            if room is not None:
                query += " AND m.room=?"
                parameters.append(room)
            names = self._connection().execute(query, parameters).fetchall()
            if len(names) > 1:
                raise MemberError(f"微信名 {name} 对应多个成员或群，请用本群绑定称呼查询")
            if not names:
                raise MemberError(f"未登记称呼：{name}")
            bound = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                (self.account, names[0]["room"], names[0]["wxid"])).fetchone()
            if bound:
                return bound
            return {"room": names[0]["room"], "name": names[0]["name"], "wxid": names[0]["wxid"],
                    "person_key": None, "display_name": names[0]["name"], "self_named": 0}
        if len(rows) != 1:
            raise MemberError(f"多个群都有 {name}，请指定群 ID")
        return rows[0]

    def resolve(self, name, room=None):
        with self._lock:
            return self._member(self._row(name, room))

    def list_members(self, room=None):
        with self._lock:
            sql, values = "SELECT * FROM people WHERE account=?", [self.account]
            if room is not None:
                sql += " AND room=?"
                values.append(room)
            return [self._member(row) for row in self._connection().execute(sql + " ORDER BY room,person_key", values).fetchall()]

    def find_by_wxid(self, wxid, room):
        with self._lock:
            row = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                                             (self.account, room, wxid)).fetchone()
            return self._member(row) if row else None

    def bind(self, name, room, wxid):
        validate_mentions(room, [wxid])
        with self._lock:
            row = self._row(name, room)
            if row["person_key"] is None:
                raise MemberError("请本人发送 /绑定 昵称 来设置本群称呼")
            if row["wxid"] and row["wxid"] != wxid:
                raise MemberError(f"{row['name']} 已绑定其他账号，未修改原绑定")
            other = self._connection().execute("SELECT name FROM people WHERE account=? AND room=? AND wxid=?",
                                                (self.account, room, wxid)).fetchone()
            if other and other["name"] != row["name"]:
                raise MemberError(f"这个微信账号已绑定为 {other['name']}，未修改原绑定")
            with self._connection() as db:
                db.execute("UPDATE people SET wxid=? WHERE account=? AND room=? AND person_key=?",
                           (wxid, self.account, room, row["person_key"]))
            return self._member(self._row(name, room))

    def register(self, name, room, wxid):
        """本人注册新称呼或改称呼；身份、旧称呼和记录仍由同一个 ID 关联。"""
        alias = name_key(name)
        name = name.strip()
        validate_mentions(room, [wxid])
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self._lock:
            db = self._connection()
            by_name = db.execute("""SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                WHERE a.account=? AND a.room=? AND a.alias=?""", (self.account, room, alias)).fetchone()
            by_id = db.execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                               (self.account, room, wxid)).fetchone()
            if by_name and by_name["wxid"] and by_name["wxid"] != wxid:
                raise MemberError(f"称呼 {name} 已被其他成员绑定，请换一个昵称")
            if by_name and by_id and by_name["person_key"] != by_id["person_key"]:
                raise MemberError(f"称呼 {name} 已属于另一份成员资料，请换一个昵称")
            row = by_id or by_name
            key = row["person_key"] if row else "member_" + uuid.uuid4().hex
            display = name  # 本人 /绑定 的名字同时用于本群查询和艾特显示。
            with db:
                if row:
                    db.execute("UPDATE people SET name=?,display_name=?,wxid=?,self_named=1 WHERE account=? AND room=? AND person_key=?",
                               (name, display, wxid, self.account, room, key))
                else:
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid,self_named)
                        VALUES(?,?,?,?,?,?,1)""", (self.account, room, key, name, name, wxid))
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            return self._member(self._row(name, room))

    def _settings(self):
        if self._db is None:  # 开关存在库里，打开数据库时读入
            with self._lock:
                self._connection()

    def remembers(self, chat):
        """这个会话是否记录（长期记忆）。"""
        self._settings()
        return chat in (self.groups if chat.endswith("@chatroom") else self.private_chats)

    def replies(self, chat):
        """这个会话是否自动回复（群里被艾特时、私聊每条）。"""
        self._settings()
        return chat in self.reply_chats

    def remembered_chats(self):
        self._settings()
        return sorted(self.groups | self.private_chats)

    def chat_history(self, chat, *, before=None, limit=200):
        """聊天页重开时显示的以前的聊天：开了长期记忆的会话最近 limit 条原文（含已压缩的），按时间顺序。
        before 为 Unix 时间时只取它 2 秒之前的，和管理台本次已显示的消息接上而不重复。"""
        if not self.remembers(chat):
            return []
        sql, values = "SELECT id,sender_id,text,received_at,is_self FROM messages WHERE account=? AND room=?", [self.account, chat]
        if before is not None:
            sql += " AND received_at<?"
            values.append(datetime.fromtimestamp(before - 2, timezone.utc).isoformat())
        with self._lock:
            rows = self._connection().execute(sql + " ORDER BY id DESC LIMIT ?", values + [limit]).fetchall()
            names = {}
            for row in rows:
                if not row["is_self"] and row["sender_id"] not in names:
                    names[row["sender_id"]] = self.speaker_name(row["sender_id"], chat)
            return [{"id": row["id"], "at": row["received_at"], "text": row["text"], "own": bool(row["is_self"]),
                     "name": "我" if row["is_self"] else names[row["sender_id"]]} for row in reversed(rows)]

    @contextmanager
    def reply_guard(self, chat, inner=None):
        """自动回复发送前再确认一次：生成途中关掉了“自动回复”就不发。inner 为插件原有的发送保护。"""
        with self._lock:
            if chat not in self.reply_chats:
                raise SnapshotExpired("自动回复已关闭")
            with inner() if inner is not None else nullcontext():
                yield

    def forget(self, chat):
        """清空这个会话的长期记忆（聊天记录和摘要），开关不变，之后的新消息照常记录。
        记下清空时间：之后再打开长期记忆补记时，不会把清空前窗口里的消息补回来。"""
        validate_target(chat)
        with self._lock:
            if chat in self.groups:
                count = self.clear(chat)  # 群里同 /清空：进行中的回复也会取消
            else:
                with self._connection() as db:
                    count = db.execute("DELETE FROM messages WHERE account=? AND room=?", (self.account, chat)).rowcount
                    db.execute("DELETE FROM agent_summaries WHERE account=? AND chat=?", (self.account, chat))
            setting = self.chat_setting(chat)
            with self._connection() as db:
                db.execute("""INSERT INTO chat_settings(account,chat,memory,reply,cleared_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(account,chat) DO UPDATE SET cleared_at=excluded.cleared_at""",
                    (self.account, chat, int(setting["memory"]), int(setting["reply"]),
                     self._now().astimezone(timezone.utc).isoformat()))
            return count

    def chat_setting(self, chat):
        return {"memory": self.remembers(chat), "reply": self.replies(chat)}

    def _apply_setting(self, chat, memory, reply):
        target = self.groups if chat.endswith("@chatroom") else self.private_chats
        (target.add if memory else target.discard)(chat)
        (self.reply_chats.add if reply else self.reply_chats.discard)(chat)

    def set_chat(self, chat, *, memory, reply):
        """聊天页的两个开关，立即生效、重启后仍在。自动回复离不开记录：开自动回复会一并开长期记忆；
        页面关长期记忆时会同时关自动回复。"""
        validate_target(chat)
        if type(memory) is not bool or type(reply) is not bool:
            raise MemberError("开关应为 true 或 false")
        memory = memory or reply
        if memory and not self.history_enabled:
            raise MemberError("请先在群聊配置中开启“记录当天文字”")
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO chat_settings(account,chat,memory,reply) VALUES(?,?,?,?) ON CONFLICT(account,chat)
                DO UPDATE SET memory=excluded.memory,reply=excluded.reply""", (self.account, chat, int(memory), int(reply)))
            if memory and chat.endswith("@chatroom"):  # 新开记录的群也要有当天缓冲，艾特时才能取到当前消息
                db.execute("INSERT OR IGNORE INTO chat_buffers(account,room,local_day) VALUES(?,?,?)",
                           (self.account, chat, self._day_bounds()[0]))
            self._apply_setting(chat, memory, reply)
        return self.chat_setting(chat)

    def record(self, message):
        if not self.history_enabled or message.type != 1 or not self.remembers(message.chat_id) \
                or message.is_group != message.chat_id.endswith("@chatroom"):
            return False
        if message.data.get("self_wxid", self.account) != self.account:
            return False
        if message.is_group:
            validate_mentions(message.chat_id, [message.sender_id])
        else:
            validate_account(message.chat_id)
            validate_account(message.sender_id or message.chat_id)
        if not isinstance(message.text, str) or not message.session_id:
            raise MemberError("群记录缺少正文或连接标识")
        native_name = message.data.get("sender_wechat_name")
        if native_name:
            self.remember_wechat_name(message.sender_id, native_name)
        with self._lock, self._connection() as db:
            self.rollover()
            if message.is_group:
                db.execute("INSERT OR IGNORE INTO room_members VALUES(?,?,?)",
                           (self.account, message.chat_id, message.sender_id))
            result = db.execute("""INSERT OR IGNORE INTO messages
                (account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated) VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.account, message.chat_id, message.sender_id or message.chat_id, message.text,
                 self._now().astimezone(timezone.utc).isoformat(), message.session_id, message.sequence,
                 int(message.is_self), int(bool(message.data.get("bot_generated")))))
            return result.rowcount > 0

    def memory(self, chat, *, exclude=None):
        """智能体的记忆就是这个群或私聊的完整聊天记录：较早部分的摘要，加摘要之后的全部原文（按时间顺序）。
        每条含 id、at、name（本账号说的为“你”）、text；exclude 传当前消息时不含它。重启后仍在。"""
        from .summary import CONTROL
        with self._lock:
            self.rollover()
            db = self._connection()
            row = db.execute("SELECT summary,upto FROM agent_summaries WHERE account=? AND chat=?", (self.account, chat)).fetchone()
            summary, upto = tuple(row) if row else ("", 0)
            rows = db.execute("""SELECT id,sender_id,text,received_at,is_self,session_id,sequence FROM messages
                WHERE account=? AND room=? AND id>? ORDER BY id""", (self.account, chat, upto)).fetchall()
            names, lines = {}, []
            for row in rows:
                text = row["text"].strip()
                if (exclude is not None and exclude.sequence and (row["session_id"], row["sequence"]) ==
                        (exclude.session_id, exclude.sequence)):
                    continue
                if not text or "WXBOT_AT_CHAT_PROBE" in text or text.split(maxsplit=1)[0] in CONTROL:
                    continue
                if row["sender_id"] not in names:
                    names[row["sender_id"]] = self.speaker_name(row["sender_id"], chat)
                lines.append({"id": row["id"], "at": row["received_at"], "text": text[:MEMORY_LINE_CHARS],
                              "name": "你" if row["is_self"] else names[row["sender_id"]]})
            return summary, lines

    def memory_size(self, chat):
        """摘要加摘要之后原文的字数，按发给模型时的截断计；判断要不要压缩用，不逐条读出。"""
        with self._lock:
            db = self._connection()
            row = db.execute("SELECT summary,upto FROM agent_summaries WHERE account=? AND chat=?", (self.account, chat)).fetchone()
            summary, upto = tuple(row) if row else ("", 0)
            size = db.execute("SELECT COALESCE(SUM(MIN(length(text),?)),0) FROM messages WHERE account=? AND room=? AND id>?",
                              (MEMORY_LINE_CHARS, self.account, chat, upto)).fetchone()[0]
            return len(summary) + size

    def compact_memory(self, chat, summary, upto):
        """摘要从此代替 id<=upto 的聊天记录给模型看；原文保留，可用 search 查到。
        期间被 /清空 或每日清理过（upto 已不在）就放弃，不把清掉的内容写回。"""
        # ponytail: 原文只在 /清空 或开启每日清理时删除，会一直增长（活跃群一年约几十到上百 MB）；真占地方再加保留天数。
        with self._lock, self._connection() as db:
            if not db.execute("SELECT 1 FROM messages WHERE account=? AND room=? AND id=?",
                              (self.account, chat, upto)).fetchone():
                return False
            db.execute("""INSERT INTO agent_summaries(account,chat,summary,upto) VALUES(?,?,?,?)
                ON CONFLICT(account,chat) DO UPDATE SET summary=excluded.summary,upto=excluded.upto
                WHERE excluded.upto>agent_summaries.upto""", (self.account, chat, summary, upto))
            return True

    def backfill(self, chat, rows):
        """打开长期记忆时补记管理台已显示的文字（MessageBuffer.texts 的行），按原来的时间；
        只补库里这个会话最后一条之后的，之前记过的不会重复。返回补了几条。"""
        if not self.history_enabled or not self.remembers(chat):
            return 0
        with self._lock, self._connection() as db:
            last = db.execute("""SELECT MAX(COALESCE((SELECT MAX(received_at) FROM messages WHERE account=? AND room=?),''),
                COALESCE((SELECT cleared_at FROM chat_settings WHERE account=? AND chat=?),''))""",
                (self.account, chat, self.account, chat)).fetchone()[0]
            count = 0
            for row in rows:
                at, text = datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat(), row["content"].strip()
                if at <= last or not text:
                    continue
                own = row["direction"] == "outgoing"
                sender = self.account if own else row["sender_id"] or chat
                if not own and row.get("sender_name") and self.display_name(sender, chat) is None:
                    try:  # 补记的人没有原生名字缓存，用窗口里显示的名字，模型才知道是谁说的
                        self.remember_wechat_name(sender, row["sender_name"])
                    except (MemberError, ValueError):
                        pass
                db.execute("""INSERT INTO messages(account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated)
                    VALUES(?,?,?,?,?,'backfill',0,?,?)""", (self.account, chat, sender, text, at, int(own), int(own)))
                count += 1
            return count

    def record_own(self, chat, text, session_id):
        """补记本账号发出的话（语音、私聊文字都没有原生回传），聊天记录（也就是智能体的记忆）里才有。
        sequence 为 0，不与原生消息冲突；没开长期记忆的会话不记。"""
        if not self.history_enabled or not self.remembers(chat) or not isinstance(text, str) or not text.strip():
            return False
        with self._lock, self._connection() as db:
            self.rollover()
            db.execute("""INSERT INTO messages(account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated)
                VALUES(?,?,?,?,?,?,0,1,1)""", (self.account, chat, self.account, text,
                                               self._now().astimezone(timezone.utc).isoformat(), session_id or "own"))
            return True

    def _speaker_wxid(self, name, room):
        """按群友称呼找微信 ID：先查绑定称呼和微信名，再查本群群昵称（仅唯一匹配时）。"""
        try:
            member = self.resolve(name, room)
            if member.wxid is not None:
                return member.wxid
            error = MemberError(f"{member.name} 尚未绑定微信 ID")
        except MemberError as failed:
            error = failed
        rows = self._connection().execute("SELECT wxid FROM group_nicknames WHERE account=? AND room=? AND nickname=?",
                                          (self.account, room, name)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if rows:
            raise MemberError(f"群昵称“{name}”对应多人，请换个称呼")
        raise error

    def search(self, room, keyword=None, *, date=None, name=None, limit=20):
        """在本群（或白名单私聊）保存的聊天原文里查找，关键词、日期（北京时间 YYYY-MM-DD）、发言人称呼可任意组合，
        至少给一项；发言人填“你”查本账号说过的话。返回符合条件的最近 limit 条，按时间顺序。"""
        if not self.remembers(room):
            raise MemberError("这个会话没有开启长期记忆")
        if keyword is None and date is None and name is None:
            raise MemberError("至少需要关键词、日期或发言人之一")
        sql, values = " FROM messages WHERE account=? AND room=?", [self.account, room]
        if keyword is not None:
            if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 100:
                raise MemberError("搜索关键词应为 1–100 个字符")
            sql += " AND instr(text,?)>0"
            values.append(keyword.strip())
        if date is not None:
            try:
                start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=BEIJING)
            except (TypeError, ValueError):
                raise MemberError("日期格式应为 YYYY-MM-DD") from None
            sql += " AND received_at>=? AND received_at<?"
            values += [start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()]
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise MemberError("发言人应为群友称呼")
        with self._lock:
            self.rollover()
            if name is not None:
                sql += " AND sender_id=?"
                values.append(self.account if name.strip() == "你" else self._speaker_wxid(name.strip(), room))
            rows = self._connection().execute("SELECT sender_id,text,received_at,is_self" + sql + " ORDER BY id DESC LIMIT ?",
                                              values + [limit]).fetchall()
            return [dict(row, name="你" if row["is_self"] else self.speaker_name(row["sender_id"], room))
                    for row in reversed(rows)]

    def history(self, name, room=None, *, limit=100, since=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MemberError("历史查询 limit 应为 1–1000 的整数")
        if since is not None:
            if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
                raise MemberError("since 应为带时区的 datetime")
            since = since.astimezone(timezone.utc).isoformat()
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            sql = "SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?"
            values = [self.account, member.room, member.wxid]
            if since is not None:
                sql += " AND received_at>=?"
                values.append(since)
            rows = self._connection().execute(sql + " ORDER BY id DESC LIMIT ?", [*values, limit]).fetchall()
            return [dict(row) for row in reversed(rows)]

    def close(self):
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None
            self._closed = True
