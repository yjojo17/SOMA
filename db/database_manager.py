"""
DatabaseManager — sync PostgreSQL/TimescaleDB writer for audit data.

One connection per session: open at session start, close at session end.
Uses psycopg (v3) — the current stable PostgreSQL driver for Python.

Usage:
    db = DatabaseManager()
    db.connect()
    try:
        db.ensure_account(...)
        db.insert_session(...)
        db.insert_posts(...)
        db.insert_interactions(...)
        db.finalize_session(...)
    finally:
        db.close()
"""
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb


DEFAULT_DSN = os.environ.get(
    'AUDIT_DB_DSN',
    'postgresql://audit:audit@127.0.0.1:5432/audit',
)


class DatabaseManager:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self.conn: Optional[psycopg.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self.conn is not None:
            raise RuntimeError('Connection already open')
        self.conn = psycopg.connect(self.dsn, autocommit=False)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @contextmanager
    def _cursor(self):
        if self.conn is None:
            raise RuntimeError('Not connected. Call connect() first.')
        with self.conn.cursor() as cur:
            try:
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    
    def ensure_account(
        self,
        account_id: str,
        email: str,
        firefox_profile: str,
        role: str,
        bucket: Optional[str] = None,
        assigned_interests: Optional[Sequence[str]] = None,
        gender: Optional[str] = None,
        condition: Optional[str] = None,       # ← add this
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts
                    (id, email, firefox_profile, role, bucket, assigned_interests, gender, condition)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    account_id,
                    email,
                    firefox_profile,
                    role,
                    bucket,
                    list(assigned_interests) if assigned_interests else None,
                    gender,
                    condition,                  # ← add this
                ),
            )
    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def insert_session(
        self,
        session_id: str,
        account_id: str,
        started_at: datetime,
        experiment_id: Optional[str] = None,
        planned_duration_seconds: Optional[int] = None,
        target_posts: Optional[int] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions
                    (id, account_id, experiment_id, started_at, planned_duration, target_posts, status)
                VALUES (%s, %s, %s, %s, make_interval(secs => %s), %s, 'running')
                """,
                (
                    session_id,
                    account_id,
                    experiment_id,
                    started_at,
                    planned_duration_seconds,
                    target_posts,
                ),
            )

    def finalize_session(
        self,
        session_id: str,
        ended_at: datetime,
        status: str,
        final_stats: Dict,
        raw_archive_path: Optional[str],
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET ended_at = %s,
                    status = %s,
                    final_stats = %s,
                    raw_archive_path = %s
                WHERE id = %s
                """,
                (ended_at, status, Jsonb(final_stats), raw_archive_path, session_id),
            )

    # ------------------------------------------------------------------
    # posts
    # ------------------------------------------------------------------
    def insert_posts(
        self,
        session_id: str,
        account_id: str,
        posts: List[Dict],
        experiment_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Bulk-insert post observations. Returns {post_pk: db_id} for linking interactions.

        Each post dict must contain: pk, postLink, profile_name, is_suggested, is_following.
        Everything else is stored in post_data JSONB. feed_position is taken from 'position'
        if present, else inferred from input order (1-based).

        Input is expected to be deduplicated by the interceptor (one entry per postLink
        per session), which means one DB row per (session, post). Multiple interaction
        events in the session that reference the same post all link to that one row.
        """
        if not posts:
            return {}

        rows: List[Tuple] = []
        pks: List[str] = []
        for i, post in enumerate(posts, start=1):
            pk = post.get('pk') or ''
            pks.append(pk)

            # posted_at: Instagram sends taken_at as a unix timestamp string
            posted_at = None
            raw_ts = post.get('timestamp')
            if raw_ts:
                try:
                    posted_at = datetime.fromtimestamp(int(raw_ts))
                except (ValueError, TypeError):
                    posted_at = None

            # like_count: expected to be raw int from interceptor
            raw_likes = post.get('likes')
            like_count = raw_likes if isinstance(raw_likes, int) else None

            rows.append((
                post.get('collection_timestamp') or datetime.now(),
                account_id,
                experiment_id,
                session_id,
                post.get('position') or i,
                pk,
                post.get('postLink'),
                post.get('profile_name'),
                posted_at,
                post.get('description'),  # caption
                like_count,
                bool(post.get('is_suggested', False)),
                bool(post.get('is_following', False)),
                Jsonb(post),
            ))

        with self._cursor() as cur:
            cur.executemany(
                """
                INSERT INTO posts
                    (collected_at, account_id, experiment_id, session_id,
                     feed_position, post_pk, post_link, profile_name,
                     posted_at, caption, like_count,
                     is_suggested, is_following, post_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                rows,
                returning=True,
            )
            # psycopg3 pattern: one row per INSERT, iterate cur.results()
            ids = [cur.fetchone()[0] for _ in cur.results()]

        return dict(zip(pks, ids))

    # ------------------------------------------------------------------
    # interactions
    # ------------------------------------------------------------------
    def insert_interactions(
        self,
        session_id: str,
        account_id: str,
        actions: List[Dict],
        post_pk_to_id: Dict[str, int],
        experiment_id: Optional[str] = None,
    ) -> None:
        """
        Bulk-insert interaction events from ActionLogger.actions.

        Each action dict has: timestamp (ISO str), action_type, details, post_context.
        post_pk_to_id is the mapping returned by insert_posts() — used to link interactions
        to the specific post observation via post_observation_id. Interactions without a
        current post context (session_start, mouse_move with no active article, etc.)
        get NULL post_observation_id.
        """
        if not actions:
            return

        rows: List[Tuple] = []
        for action in actions:
            details = action.get('details') or {}
            post_context = action.get('post_context') or {}
            post_pk = post_context.get('post_id')
            post_observation_id = post_pk_to_id.get(post_pk) if post_pk else None

            rows.append((
                action['timestamp'],
                account_id,
                experiment_id,
                session_id,
                action['action_type'],
                post_observation_id,
                Jsonb(details),
            ))

        with self._cursor() as cur:
            cur.executemany(
                """
                INSERT INTO interactions
                    (occurred_at, account_id, experiment_id, session_id,
                     interaction_type, post_observation_id, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )