"""SQLite 持久层：书籍、章节、节点、问题、变更记录（含撤销所需的逆操作）。"""
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT, language TEXT, path TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INT, href TEXT, title TEXT, order_index INT);
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INT, chapter_id INT, dom_path TEXT, parent_path TEXT,
  tag TEXT, text TEXT, level INT, lang TEXT, alt TEXT,
  epub_type TEXT, img_src TEXT, el_id TEXT, order_index INT);
CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INT, chapter_href TEXT, node_path TEXT,
  check_name TEXT, severity TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INT, ts TEXT, kind TEXT, summary TEXT,
  inverse TEXT, undone INT DEFAULT 0);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- books ----------
def create_book(title, language, path):
    with connect() as c:
        cur = c.execute(
            "INSERT INTO books(title,language,path,created) VALUES(?,?,?,?)",
            (title, language, path, datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def update_book_path(book_id, path):
    with connect() as c:
        c.execute("UPDATE books SET path=? WHERE id=?", (path, book_id))


def get_book(book_id):
    with connect() as c:
        r = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return dict(r) if r else None


def list_books():
    with connect() as c:
        return _rows(c.execute("SELECT * FROM books ORDER BY id DESC"))


# ---------- chapters ----------
def replace_chapters(book_id, chapters):
    """chapters: [{href,title,order_index}]"""
    with connect() as c:
        c.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
        for ch in chapters:
            c.execute(
                "INSERT INTO chapters(book_id,href,title,order_index) VALUES(?,?,?,?)",
                (book_id, ch["href"], ch["title"], ch["order_index"]))


def list_chapters(book_id):
    with connect() as c:
        return _rows(c.execute(
            "SELECT * FROM chapters WHERE book_id=? ORDER BY order_index", (book_id,)))


def get_chapter(book_id, chapter_id):
    with connect() as c:
        r = c.execute("SELECT * FROM chapters WHERE book_id=? AND id=?",
                      (book_id, chapter_id)).fetchone()
        return dict(r) if r else None


def update_chapter_order(book_id, ordered_hrefs):
    with connect() as c:
        for i, href in enumerate(ordered_hrefs):
            c.execute("UPDATE chapters SET order_index=? WHERE book_id=? AND href=?",
                      (i, book_id, href))


# ---------- nodes ----------
def replace_nodes(book_id, chapter_id, nodes):
    with connect() as c:
        c.execute("DELETE FROM nodes WHERE book_id=? AND chapter_id=?",
                  (book_id, chapter_id))
        for i, n in enumerate(nodes):
            c.execute(
                """INSERT INTO nodes(book_id,chapter_id,dom_path,parent_path,tag,text,
                   level,lang,alt,epub_type,img_src,el_id,order_index)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (book_id, chapter_id, n["dom_path"], n["parent_path"], n["tag"],
                 n["text"], n["level"], n["lang"], n["alt"], n["epub_type"],
                 n["img_src"], n["el_id"], i))


def list_nodes(book_id, chapter_id=None):
    with connect() as c:
        if chapter_id:
            return _rows(c.execute(
                "SELECT * FROM nodes WHERE book_id=? AND chapter_id=? ORDER BY order_index",
                (book_id, chapter_id)))
        return _rows(c.execute(
            "SELECT * FROM nodes WHERE book_id=? ORDER BY chapter_id, order_index",
            (book_id,)))


# ---------- issues ----------
def delete_issues(book_id, check_name, chapter_href="__ALL__"):
    with connect() as c:
        if chapter_href == "__ALL__":
            c.execute("DELETE FROM issues WHERE book_id=? AND check_name=?",
                      (book_id, check_name))
        else:
            c.execute(
                "DELETE FROM issues WHERE book_id=? AND check_name=? AND chapter_href=?",
                (book_id, check_name, chapter_href))


def add_issues(book_id, issues):
    with connect() as c:
        for it in issues:
            c.execute(
                """INSERT INTO issues(book_id,chapter_href,node_path,check_name,severity,message)
                   VALUES(?,?,?,?,?,?)""",
                (book_id, it.get("chapter"), it.get("path"), it["check"],
                 it["severity"], it["message"]))


def list_issues(book_id):
    with connect() as c:
        return _rows(c.execute(
            "SELECT * FROM issues WHERE book_id=? ORDER BY check_name, id", (book_id,)))


# ---------- changes / undo ----------
def add_change(book_id, kind, summary, inverse):
    with connect() as c:
        cur = c.execute(
            "INSERT INTO changes(book_id,ts,kind,summary,inverse,undone) VALUES(?,?,?,?,?,0)",
            (book_id, datetime.now().isoformat(timespec="seconds"), kind, summary,
             json.dumps(inverse, ensure_ascii=False)))
        return cur.lastrowid


def last_active_change(book_id):
    with connect() as c:
        r = c.execute(
            "SELECT * FROM changes WHERE book_id=? AND undone=0 ORDER BY id DESC LIMIT 1",
            (book_id,)).fetchone()
        return dict(r) if r else None


def mark_change_undone(change_id):
    with connect() as c:
        c.execute("UPDATE changes SET undone=1 WHERE id=?", (change_id,))


def list_changes(book_id, limit=200):
    with connect() as c:
        return _rows(c.execute(
            "SELECT id,ts,kind,summary,undone FROM changes WHERE book_id=? ORDER BY id DESC LIMIT ?",
            (book_id, limit)))
