"""SQLite storage: influencers, messages, outreach log, contacted (dedupe) table."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

INFLUENCER_COLS = {
    "id": "TEXT PRIMARY KEY", "name": "TEXT", "platform": "TEXT", "profile_url": "TEXT",
    "handle": "TEXT", "followers": "INTEGER", "engagement_rate": "REAL", "avg_views": "REAL",
    "country": "TEXT", "description": "TEXT", "keywords": "TEXT", "recent_titles": "TEXT",
    "last_post_date": "TEXT", "data_source": "TEXT", "discovered_at": "TEXT",
    "niche": "TEXT", "relevance_score": "REAL", "themes": "TEXT",
    "status": "TEXT DEFAULT 'discovered'", "filter_summary": "TEXT", "filter_checks": "TEXT",
    "email": "TEXT", "email_source": "TEXT", "instagram_url": "TEXT", "tiktok_url": "TEXT",
    "website": "TEXT", "audience_age": "TEXT", "audience_gender": "TEXT",
    "audience_geo": "TEXT", "enriched_at": "TEXT",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cols = ", ".join(f"{k} {v}" for k, v in INFLUENCER_COLS.items())
        self.conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS influencers ({cols});
        CREATE TABLE IF NOT EXISTS messages (
            influencer_id TEXT PRIMARY KEY, email_subject TEXT, email_body TEXT,
            instagram_dm TEXT, collab_angle TEXT, generator TEXT,
            approved INTEGER DEFAULT 0, generated_at TEXT);
        CREATE TABLE IF NOT EXISTS outreach_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, influencer_id TEXT, name TEXT, email TEXT,
            channel TEXT, message_generated INTEGER, sent INTEGER, sent_at TEXT,
            status TEXT, detail TEXT, mode TEXT);
        CREATE TABLE IF NOT EXISTS contacted (
            email TEXT, mode TEXT, influencer_id TEXT, sent_at TEXT,
            PRIMARY KEY (email, mode));
        """)
        self.conn.commit()

    # ---- influencers -------------------------------------------------------
    def upsert_influencer(self, rec: dict):
        cols = [c for c in rec if c in INFLUENCER_COLS]
        if "id" not in cols:
            raise ValueError("record needs an id")
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        sql = f"INSERT INTO influencers ({names}) VALUES ({marks})"
        if updates:
            sql += f" ON CONFLICT(id) DO UPDATE SET {updates}"
        self.conn.execute(sql, [rec[c] for c in cols])
        self.conn.commit()

    def update_influencer(self, inf_id: str, **fields):
        bad = [k for k in fields if k not in INFLUENCER_COLS or k == "id"]
        if bad:
            raise ValueError(f"unknown influencer columns: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE influencers SET {sets} WHERE id=?", [*fields.values(), inf_id])
        self.conn.commit()

    def influencers(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute("SELECT * FROM influencers WHERE status=? ORDER BY followers DESC", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM influencers ORDER BY followers DESC")
        return [dict(r) for r in rows]

    def get_influencer(self, inf_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM influencers WHERE id=?", (inf_id,)).fetchone()
        return dict(r) if r else None

    # ---- messages ----------------------------------------------------------
    def upsert_message(self, inf_id: str, **m):
        self.conn.execute(
            """INSERT INTO messages (influencer_id, email_subject, email_body, instagram_dm,
                   collab_angle, generator, approved, generated_at)
               VALUES (?,?,?,?,?,?,0,?)
               ON CONFLICT(influencer_id) DO UPDATE SET email_subject=excluded.email_subject,
                   email_body=excluded.email_body, instagram_dm=excluded.instagram_dm,
                   collab_angle=excluded.collab_angle, generator=excluded.generator,
                   approved=0, generated_at=excluded.generated_at""",
            (inf_id, m["email_subject"], m["email_body"], m["instagram_dm"],
             m["collab_angle"], m["generator"], now_iso()))
        self.conn.commit()

    def get_message(self, inf_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM messages WHERE influencer_id=?", (inf_id,)).fetchone()
        return dict(r) if r else None

    def messages(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM messages")]

    def approve(self, inf_ids: list[str] | None = None) -> int:
        if inf_ids is None:
            cur = self.conn.execute("UPDATE messages SET approved=1")
        else:
            cur = self.conn.executemany("UPDATE messages SET approved=1 WHERE influencer_id=?",
                                        [(i,) for i in inf_ids])
        self.conn.commit()
        return cur.rowcount

    # ---- outreach log / dedupe --------------------------------------------
    def log_outreach(self, **row):
        self.conn.execute(
            """INSERT INTO outreach_log (influencer_id, name, email, channel, message_generated,
                   sent, sent_at, status, detail, mode) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (row["influencer_id"], row.get("name"), row.get("email"), row["channel"],
             int(row.get("message_generated", 1)), int(row.get("sent", 0)), row.get("sent_at"),
             row["status"], row.get("detail", ""), row.get("mode", "")))
        self.conn.commit()

    def has_log(self, inf_id: str, channel: str, status: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM outreach_log WHERE influencer_id=? AND channel=? AND status=?",
            (inf_id, channel, status)).fetchone() is not None

    def outreach_rows(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM outreach_log ORDER BY id")]

    def already_contacted(self, email: str, mode: str) -> bool:
        """A live send always blocks; a dry-run only blocks repeat dry-runs."""
        return self.conn.execute(
            "SELECT 1 FROM contacted WHERE email=? AND (mode='live' OR mode=?)",
            (email.lower(), mode)).fetchone() is not None

    def mark_contacted(self, email: str, mode: str, inf_id: str):
        self.conn.execute("INSERT OR IGNORE INTO contacted VALUES (?,?,?,?)",
                          (email.lower(), mode, inf_id, now_iso()))
        self.conn.commit()
