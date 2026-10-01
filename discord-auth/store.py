"""Persistent account ownership, one-time challenges, and authoritative decisions."""
import hashlib
import secrets
import sqlite3
import time


class LinkError(ValueError):
    pass


class Store:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(
                pid TEXT PRIMARY KEY, discord_id TEXT UNIQUE, name TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'unknown', checked_at REAL,
                revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS codes(
                digest TEXT PRIMARY KEY, pid TEXT UNIQUE NOT NULL,
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS retired_codes(
                digest TEXT PRIMARY KEY, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(discord_id TEXT, attempted_at REAL);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
        ''')
        with self.db:
            format_row = self.db.execute("SELECT value FROM metadata WHERE key='code_format'").fetchone()
            if not format_row or format_row['value'] != 'four-ascii-digits-v1':
                self.db.execute('DELETE FROM codes')
                self.db.execute('DELETE FROM retired_codes')
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('code_format','four-ascii-digits-v1')")

    def configure_policy(self, fingerprint):
        row = self.db.execute("SELECT value FROM metadata WHERE key='policy'").fetchone()
        with self.db:
            if row and row['value'] != fingerprint:
                self.db.execute("UPDATE accounts SET decision='unknown', checked_at=NULL, revision=revision+1")
                self.db.execute('INSERT OR REPLACE INTO retired_codes SELECT digest,expires_at FROM codes')
                self.db.execute('DELETE FROM codes')
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('policy',?)", (fingerprint,))

    def player(self, pid, name=None):
        if name is not None:
            with self.db:
                self.db.execute('INSERT INTO accounts(pid,name) VALUES(?,?) ON CONFLICT(pid) DO UPDATE SET name=excluded.name', (pid, name))
        row = self.db.execute('SELECT * FROM accounts WHERE pid=?', (pid,)).fetchone()
        return dict(row) if row else None

    def user(self, discord_id):
        row = self.db.execute('SELECT * FROM accounts WHERE discord_id=?', (str(discord_id),)).fetchone()
        return dict(row) if row else None

    def linked(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM accounts WHERE discord_id IS NOT NULL')]

    def challenge(self, pid):
        now = self.clock()
        with self.db:
            self.db.execute('DELETE FROM codes WHERE expires_at<=?', (now,))
            self.db.execute('DELETE FROM retired_codes WHERE expires_at<=?', (now,))
            occupied = {row['digest'] for row in self.db.execute('SELECT digest FROM codes UNION SELECT digest FROM retired_codes')}
            if len(occupied) >= 10000:
                raise LinkError('連携コードを発行できません。しばらくして再接続してください。')
            for _ in range(128):
                code = f'{secrets.randbelow(10000):04d}'
                digest = hashlib.sha256(code.encode()).hexdigest()
                if digest not in occupied:
                    break
            else:
                available = [f'{number:04d}' for number in range(10000)
                             if hashlib.sha256(f'{number:04d}'.encode()).hexdigest() not in occupied]
                code = secrets.choice(available)
                digest = hashlib.sha256(code.encode()).hexdigest()
            # Keep replaced codes reserved for their remaining lifetime, so an
            # old disconnect screen cannot link a different player by replay.
            self.db.execute('INSERT OR REPLACE INTO retired_codes SELECT digest,expires_at FROM codes WHERE pid=?', (pid,))
            self.db.execute('DELETE FROM codes WHERE pid=?', (pid,))
            self.db.execute('INSERT INTO codes VALUES(?,?,?)', (digest, pid, now + 600))
        return code

    def claim(self, discord_id, code, allowed):
        discord_id = str(discord_id)
        now = self.clock()
        with self.db:
            self.db.execute('DELETE FROM attempts WHERE attempted_at<?', (now - 600,))
            count = self.db.execute('SELECT count(*) FROM attempts WHERE discord_id=?', (discord_id,)).fetchone()[0]
            if count >= 5:
                raise LinkError('試行回数の上限です。10分後に再試行してください。')
            self.db.execute('INSERT INTO attempts VALUES(?,?)', (discord_id, now))
        if not allowed:
            raise LinkError('サブスクロールが確認できません。DiscordのTwitch連携を確認してください。')
        code = code.strip()
        if len(code) != 4 or any(character not in '0123456789' for character in code):
            raise LinkError('コードは半角数字4桁で入力してください。')
        digest = hashlib.sha256(code.encode()).hexdigest()
        row = self.db.execute('SELECT * FROM codes WHERE digest=? AND expires_at>?', (digest, now)).fetchone()
        if not row:
            raise LinkError('コードが無効または期限切れです。Minecraftへ再接続して新しいコードを取得してください。')
        account = self.player(row['pid'])
        owned = self.user(discord_id)
        if account['discord_id'] or owned:
            raise LinkError('すでに連携されたアカウントです。変更する場合は先に /mc unlink を実行してください。')
        with self.db:
            self.db.execute("UPDATE accounts SET discord_id=?, decision='allowed', checked_at=?, revision=revision+1 WHERE pid=?", (discord_id, now, row['pid']))
            self.db.execute('INSERT OR REPLACE INTO retired_codes VALUES(?,?)', (row['digest'], row['expires_at']))
            self.db.execute('DELETE FROM codes WHERE pid=?', (row['pid'],))
        return self.player(row['pid'])

    def decide(self, discord_id, allowed):
        with self.db:
            self.db.execute('UPDATE accounts SET decision=?,checked_at=?,revision=revision+1 WHERE discord_id=?', ('allowed' if allowed else 'denied', self.clock(), str(discord_id)))

    def unlink(self, discord_id):
        account = self.user(discord_id)
        if not account:
            return False
        with self.db:
            self.db.execute("UPDATE accounts SET discord_id=NULL,decision='denied',checked_at=?,revision=revision+1 WHERE pid=?", (self.clock(), account['pid']))
            self.db.execute('INSERT OR REPLACE INTO retired_codes SELECT digest,expires_at FROM codes WHERE pid=?', (account['pid'],))
            self.db.execute('DELETE FROM codes WHERE pid=?', (account['pid'],))
        return True

    def close(self):
        self.db.close()
