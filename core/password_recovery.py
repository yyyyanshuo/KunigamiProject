"""Short-lived recovery state shared by workers; no plaintext codes or addresses on disk."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import secrets
import sqlite3
import time

COOLDOWN = 60
LIFETIME = 600
MAX_GUESSES = 5


class Limited(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, retry_after)


class Unavailable(Exception):
    pass


def source_address(peer, real_ip=None):
    # Production nginx overwrites X-Real-IP with $remote_addr before its loopback
    # proxy_pass. Never trust arbitrary X-Forwarded-For or non-local senders.
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return 'unknown'
    if address.is_loopback and real_ip:
        try:
            address = ipaddress.ip_address(real_ip)
        except ValueError:
            pass
    return str(address)


class RecoveryStore:
    def __init__(self, users_db, secret):
        self.path = Path(users_db).with_name('password-recovery.sqlite3')
        self.secret = secret.encode() if isinstance(secret, str) else secret

    def tag(self, *fields):
        value = '\0'.join(('saki-recovery-v1', *fields)).encode()
        return hmac.new(self.secret, value, hashlib.sha256).hexdigest()

    @contextmanager
    def _transaction(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, hits INTEGER, until INTEGER)')
            db.execute('''CREATE TABLE IF NOT EXISTS challenges (account TEXT, stage TEXT, ticket TEXT,
                       uid INTEGER, version INTEGER, proof TEXT, remaining INTEGER, until INTEGER,
                       PRIMARY KEY(account,stage))''')
            now = int(time.time())
            db.execute('DELETE FROM limits WHERE until<=?', (now,))
            db.execute('DELETE FROM challenges WHERE until<=?', (now,))
            yield db, now

    @staticmethod
    def _charge(db, now, budgets):
        delay = 0
        for key, maximum, window in budgets:
            row = db.execute('SELECT hits,until FROM limits WHERE key=?', (key,)).fetchone()
            if row and row[0] >= maximum:
                delay = max(delay, row[1] - now)
        if delay:
            raise Limited(delay)
        for key, maximum, window in budgets:
            db.execute('INSERT INTO limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET hits=hits+1',
                       (key, now + window))

    def transport_retry_after(self):
        # A public, service-wide signal only. Never accept an address or return a
        # recipient's delivery status. A fresh deployment needs no state file yet.
        if self.path.is_symlink():
            raise OSError('Invalid recovery state path')
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            row = db.execute("SELECT until FROM limits WHERE key='smtp-down'").fetchone()
        return max(0, row[0] - int(time.time())) if row else 0

    def reserve(self, email, ip):
        account = self.tag('email', email)
        with self._transaction() as (db, now):
            if db.execute("SELECT 1 FROM limits WHERE key='smtp-down'").fetchone():
                raise Unavailable()
            self._charge(db, now, [('send-global', 100, 3600), (self.tag('send-ip', ip), 20, 3600),
                                   ('send:'+account, 5, 3600), ('cool:'+account, 1, COOLDOWN)])
            if db.execute("SELECT COUNT(*) FROM challenges WHERE stage='pending'").fetchone()[0] >= 8:
                raise Limited(15)
            ticket = secrets.token_hex(16)
            db.execute("INSERT OR REPLACE INTO challenges VALUES (?,'pending',?,NULL,NULL,NULL,0,?)",
                       (account, ticket, now + COOLDOWN))
            return ticket

    def finish(self, email, ticket, *, accepted=None, transport_down=False):
        account = self.tag('email', email)
        with self._transaction() as (db, now):
            if not db.execute("DELETE FROM challenges WHERE account=? AND stage='pending' AND ticket=?",
                              (account, ticket)).rowcount:
                return False  # An expired/superseded delivery cannot publish a late code.
            if transport_down:
                db.execute("INSERT OR REPLACE INTO limits VALUES ('smtp-down',1,?)", (now + 30,))
            if accepted is not None:
                uid, version, code = accepted
                db.execute("INSERT OR REPLACE INTO challenges VALUES (?,'active',?,?,?,?,?,?)",
                           (account, ticket, uid, version, self.tag('otp', account, ticket, code),
                            MAX_GUESSES, now + LIFETIME))
            return True

    def claim(self, email, ip, code):
        account = self.tag('email', email)
        with self._transaction() as (db, now):
            self._charge(db, now, [('check-global', 600, 900), (self.tag('check-ip', ip), 30, 900),
                                   ('check:'+account, 10, 900)])
            row = db.execute("SELECT ticket,uid,version,proof,remaining FROM challenges WHERE account=? AND stage='active'",
                             (account,)).fetchone()
            if row is None:
                return None
            ticket, uid, version, proof, remaining = row
            good = hmac.compare_digest(proof, self.tag('otp', account, ticket, code))
            if good:
                db.execute('DELETE FROM challenges WHERE account=?', (account,))
                return uid, version
            if remaining <= 1:
                db.execute("DELETE FROM challenges WHERE account=? AND stage='active'", (account,))
            else:
                db.execute("UPDATE challenges SET remaining=remaining-1 WHERE account=? AND stage='active'", (account,))
            return None
