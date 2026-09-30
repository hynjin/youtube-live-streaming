from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
import uuid

from .catalog import RadioError


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RadioError('이미 송출 또는 이력 동기화가 실행 중입니다.') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Journal:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root/'history.sqlite3', timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, manifest TEXT NOT NULL, status TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        ''')

    def begin(self, manifest):
        try:
            with self.db:
                self.db.execute('INSERT INTO sessions(id,manifest,status,updated_at) VALUES(?,?,?,?)',
                                (manifest['session_id'], json.dumps(manifest), 'starting', now()))
        except sqlite3.IntegrityError:
            raise RadioError('이미 실행한 session입니다. 재송출은 새 session을 생성하세요.') from None

    def progress(self, manifest, seconds):
        # Output time is a mux-level indication, not proof of viewer playback.
        # Require 2 seconds beyond the track boundary before counting partial use.
        confirmed = max(0, seconds - 2)
        with self.db:
            self.db.execute('UPDATE sessions SET status=?,progress=max(progress,?),updated_at=? WHERE id=?',
                            ('streaming', seconds, now(), manifest['session_id']))
            for position, track in enumerate(manifest['tracks'], 1):
                start = track['start_sample']/48000
                if confirmed <= start:
                    break
                aid = str(uuid.uuid5(uuid.UUID(manifest['session_id']), track['recording_id']))
                annotation = dict(id=aid, recording_id=track['recording_id'],
                    annotation_type='playlist_usage', body='Slow Wednesday Radio: output confirmed',
                    metadata=dict(source='slow_wednesday_radio', venture_key='sw',
                        usage_kind='radio_live', history_event='used', use_count_increment=1,
                        radio_session_id=manifest['session_id'], sequence_position=position,
                        evidence='ffmpeg_mux_progress', confirmed_at=now(),
                        observed_output_seconds=seconds, track_start_seconds=start))
                self.db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES(?,?)',
                                (aid, json.dumps(annotation)))

    def finish(self, sid, status):
        with self.db:
            self.db.execute('UPDATE sessions SET status=?,updated_at=? WHERE id=?', (status,now(),sid))

    def sync(self, client):
        count = 0
        for aid, payload in self.db.execute('SELECT id,payload FROM outbox WHERE sent=0').fetchall():
            client.append_usage(json.loads(payload))
            with self.db:
                self.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (aid,))
            count += 1
        return count

    def status(self):
        return {'sessions': [dict(zip(('id','status','progress_seconds','updated_at'), r))
                             for r in self.db.execute('SELECT id,status,progress,updated_at FROM sessions ORDER BY updated_at DESC')],
                'pending_usage': self.db.execute('SELECT count(*) FROM outbox WHERE sent=0').fetchone()[0]}
