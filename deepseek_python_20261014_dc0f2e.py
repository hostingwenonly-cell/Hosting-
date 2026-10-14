#!/usr/bin/env python3
"""
main.py — Telegram file/repo hosting bot with admin panel.

Single-file deploy. Auto-installs missing deps. Auto-detects repo file types.
Railway-ready (reads PORT, RAILWAY_PUBLIC_DOMAIN).

Run:
    python main.py

Railway:
    - set env: BOT_TOKEN, ADMIN_ID, PANEL_PASS (optional: PUBLIC_HOST, PANEL_USER)
    - deploy this file as main.py
    - Railway assigns PORT automatically
"""

# --------------------------------------------------------------------------- #
# bootstrap — stdlib only, runs before any third-party import

import importlib
import os
import subprocess
import sys

INSTALL = [
    ("telegram", "python-telegram-bot==21.6"),
    ("aiohttp", "aiohttp==3.10.5"),
    ("aiosqlite", "aiosqlite==0.20.0"),
    ("dotenv", "python-dotenv==1.0.1"),
]


def _ensure_deps():
    missing = []
    for mod, pkg in INSTALL:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"[bootstrap] installing: {', '.join(missing)}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *missing]
        )
    except subprocess.CalledProcessError as e:
        print(f"[bootstrap] pip failed: {e}")
        sys.exit(1)
    # re-exec so freshly installed modules are importable in a clean process
    os.execv(sys.executable, [sys.executable, *sys.argv])


_ensure_deps()

# --------------------------------------------------------------------------- #
# real imports (deps guaranteed present)

import asyncio
import base64
import html
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# config

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8684850689:AAF4Zei3alyloujJwxTu0BF4S4slq9rx1hc").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8685134338"))
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASS = os.getenv("PANEL_PASS", "change_this_now")
PANEL_PORT = int(os.getenv("PORT") or os.getenv("PANEL_PORT") or "8080")
PUBLIC_HOST = (os.getenv("PUBLIC_HOST") or
               (f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")

STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "storage"))
REPO_DIR = Path(os.getenv("REPO_DIR", "repo"))
DB_PATH = Path(os.getenv("DB_PATH", "data.db"))

STORAGE_DIR.mkdir(exist_ok=True)
REPO_DIR.mkdir(exist_ok=True)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")
if not ADMIN_ID:
    raise SystemExit("ADMIN_ID not set")
if PANEL_PASS == "change_this_now":
    print("[!] PANEL_PASS is default — set a real one before exposing the panel.")


def panel_base() -> str:
    if PUBLIC_HOST:
        return PUBLIC_HOST
    return f"http://localhost:{PANEL_PORT}"


# --------------------------------------------------------------------------- #
# db

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    owner_id    INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mime        TEXT,
    path        TEXT NOT NULL,
    note        TEXT,
    created_at  INTEGER NOT NULL,
    downloads   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS repo_index (
    relpath     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id);
CREATE INDEX IF NOT EXISTS idx_repo_kind ON repo_index(kind);
"""


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def db_exec(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, p)
        await db.commit()
        return cur.lastrowid


async def db_fetch(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, p) as cur:
            return await cur.fetchall()


async def db_fetch_one(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, p) as cur:
            return await cur.fetchone()


def new_id():
    return secrets.token_urlsafe(8)


def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


# --------------------------------------------------------------------------- #
# repo auto-detect
#
# Classifier rules, in order:
#   1. Path-based: repo/web/**  -> "web"
#                  repo/telegram/** or repo/bots/** -> "telegram"
#                  repo/static/** -> "static"
#   2. Content-based:
#      - index.html present in a dir tree -> that subtree is "web"
#      - a .py file inside contains any of: telegram, aiogram, pyrogram, telethon
#        -> "telegram"
#      - .js/.ts/.jsx/.tsx/.html/.css at repo root -> "web"
#      - anything else -> "static"
#
# Result: manifest written to repo/manifest.json, indexed into SQLite.

TELEGRAM_MARKERS = (
    "import telegram",
    "from telegram",
    "import aiogram",
    "from aiogram",
    "import pyrogram",
    "from pyrogram",
    "import telethon",
    "from telethon",
    "BotFather",
    "Bot(token",
)

WEB_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".vue", ".svelte"}
WEB_INDEX = {"index.html", "index.htm"}


def _sniff_py(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except Exception:
        return False
    return any(m in head for m in TELEGRAM_MARKERS)


def classify_tree(root: Path) -> dict:
    """
    Return {relpath: kind} for every file under root.
    kind in {"web", "telegram", "static"}.
    """
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name == "manifest.json" and p.parent == root:
            continue
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")

        # rule 1: path-based
        if parts[0] == "web":
            out[rel] = "web"
            continue
        if parts[0] in ("telegram", "bots"):
            out[rel] = "telegram"
            continue
        if parts[0] == "static":
            out[rel] = "static"
            continue

        # rule 2: content-based
        if p.suffix.lower() == ".py":
            out[rel] = "telegram" if _sniff_py(p) else "static"
            continue

        if p.suffix.lower() in WEB_EXT:
            # is there an index.* in this file's directory?
            if any((p.parent / ix).exists() for ix in WEB_INDEX):
                out[rel] = "web"
            elif p.suffix.lower() in (".html", ".htm"):
                out[rel] = "web"
            else:
                out[rel] = "web"
            continue

        out[rel] = "static"
    return out


async def rebuild_repo_index():
    tree = classify_tree(REPO_DIR)
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM repo_index")
        await db.executemany(
            "INSERT OR REPLACE INTO repo_index (relpath, kind, size, updated_at) VALUES (?, ?, ?, ?)",
            [(rel, kind, (REPO_DIR / rel).stat().st_size, now) for rel, kind in tree.items()],
        )
        await db.commit()

    # manifest for panel + debugging
    manifest = {
        "generated_at": now,
        "counts": {k: sum(1 for v in tree.values() if v == k) for k in ("web", "telegram", "static")},
        "files": tree,
    }
    (REPO_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- #
# bot helpers


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if uid != ADMIN_ID:
            if update.effective_message:
                await update.effective_message.reply_text("admin only.")
            return
        return await func(update, ctx)
    return wrapper


# --------------------------------------------------------------------------- #
# bot commands


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_admin = uid == ADMIN_ID
    lines = [
        "📦 <b>host bot</b>",
        "",
        "send a file → get a link.",
        "drop files into <code>repo/</code> → they get classified and served.",
        "",
        "<b>user</b>",
        "/start /help",
        "/list — your files",
        "/get &lt;id&gt; — link",
        "/del &lt;id&gt; — delete",
        "/stats — usage",
        "/repo — repo listing (classified)",
    ]
    if is_admin:
        lines += [
            "",
            "<b>admin</b>",
            "/admin — panel link + stats",
            "/users — all uploaders",
            "/allfiles — every stored file",
            "/delany &lt;id&gt;",
            "/broadcast &lt;text&gt;",
            "/setnote &lt;id&gt; &lt;note&gt;",
            "/reload — rescan repo",
            "/manifest — show repo classification",
            "/panel — creds reminder",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_help(update, ctx):
    await cmd_start(update, ctx)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = await db_fetch(
        "SELECT id, filename, size, created_at, downloads FROM files "
        "WHERE owner_id = ? ORDER BY created_at DESC LIMIT 50", (uid,))
    if not rows:
        await update.message.reply_text("no files yet.")
        return
    out = ["<b>your files</b>", ""]
    for r in rows:
        ts = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime("%Y-%m-%d")
        out.append(f"<code>{r['id']}</code> — {html.escape(r['filename'])} "
                   f"({human_size(r['size'])}, {r['downloads']}↓, {ts})")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


async def cmd_get(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("usage: /get <id>")
        return
    fid = ctx.args[0]
    uid = update.effective_user.id
    row = await db_fetch_one("SELECT * FROM files WHERE id = ?", (fid,))
    if not row:
        await update.message.reply_text("not found.")
        return
    if row["owner_id"] != uid and uid != ADMIN_ID:
        await update.message.reply_text("not yours.")
        return
    url = f"{panel_base()}/f/{row['id']}"
    await update.message.reply_text(f"<code>{url}</code>", parse_mode=ParseMode.HTML)


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("usage: /del <id>")
        return
    fid = ctx.args[0]
    uid = update.effective_user.id
    row = await db_fetch_one("SELECT * FROM files WHERE id = ?", (fid,))
    if not row:
        await update.message.reply_text("not found.")
        return
    if row["owner_id"] != uid and uid != ADMIN_ID:
        await update.message.reply_text("not yours.")
        return
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    await db_exec("DELETE FROM files WHERE id = ?", (fid,))
    await update.message.reply_text(f"deleted <code>{fid}</code>", parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    row = await db_fetch_one(
        "SELECT COUNT(*) c, COALESCE(SUM(size),0) s, COALESCE(SUM(downloads),0) d "
        "FROM files WHERE owner_id = ?", (uid,))
    await update.message.reply_text(
        f"files: {row['c']}\nsize: {human_size(row['s'])}\ndownloads: {row['d']}")


async def cmd_repo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    base = panel_base()
    rows = await db_fetch("SELECT relpath, kind FROM repo_index ORDER BY kind, relpath LIMIT 200")
    if not rows:
        await update.message.reply_text("repo is empty.")
        return
    by_kind = {"web": [], "telegram": [], "static": []}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["relpath"])
    out = [f"<b>repo</b> ({len(rows)} files)"]
    for kind in ("web", "telegram", "static"):
        items = by_kind.get(kind) or []
        if not items:
            continue
        out.append(f"\n<b>{kind}</b> ({len(items)})")
        for rel in items[:20]:
            url = f"{base}/repo/{rel}"
            out.append(f'<a href="{url}">{html.escape(rel)}</a>')
        if len(items) > 20:
            out.append(f"... +{len(items)-20} more")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def cmd_manifest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args and ctx.args[0] == "regen":
        m = await rebuild_repo_index()
        await update.message.reply_text(
            f"regenerated. web={m['counts']['web']} telegram={m['counts']['telegram']} "
            f"static={m['counts']['static']}")
        return
    path = REPO_DIR / "manifest.json"
    if not path.exists():
        await update.message.reply_text("no manifest — run /manifest regen")
        return
    data = json.loads(path.read_text())
    c = data["counts"]
    await update.message.reply_text(
        f"web={c['web']} telegram={c['telegram']} static={c['static']}\n"
        f"generated: {datetime.fromtimestamp(data['generated_at'], tz=timezone.utc).isoformat()}")


# --------------------------------------------------------------------------- #
# uploads


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    uid = update.effective_user.id

    fid = new_id()
    safe_name = Path(doc.file_name or f"{fid}.bin").name
    dest = STORAGE_DIR / f"{fid}__{safe_name}"

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))

    size = dest.stat().st_size
    await db_exec(
        "INSERT INTO files (id, owner_id, filename, size, mime, path, note, created_at, downloads) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (fid, uid, safe_name, size, doc.mime_type, str(dest), None, int(time.time())),
    )
    url = f"{panel_base()}/f/{fid}"
    await msg.reply_text(
        f"✅ <code>{fid}</code>\n{html.escape(safe_name)} ({human_size(size)})\n<code>{url}</code>",
        parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# admin commands


@admin_only
async def cmd_admin(update, ctx):
    n_files = (await db_fetch_one("SELECT COUNT(*) c FROM files"))["c"]
    n_users = (await db_fetch_one("SELECT COUNT(DISTINCT owner_id) c FROM files"))["c"]
    total = (await db_fetch_one("SELECT COALESCE(SUM(size),0) s FROM files"))["s"]
    repo = (await db_fetch_one("SELECT COUNT(*) c FROM repo_index"))["c"]
    await update.message.reply_text(
        f"<b>panel</b>\n{panel_base()}/\n"
        f"user: <code>{html.escape(PANEL_USER)}</code>\n"
        f"pass: <code>{html.escape(PANEL_PASS)}</code>\n\n"
        f"files: {n_files}\nusers: {n_users}\nstored: {human_size(total)}\nrepo: {repo}",
        parse_mode=ParseMode.HTML)


@admin_only
async def cmd_users(update, ctx):
    rows = await db_fetch(
        "SELECT owner_id, COUNT(*) c, COALESCE(SUM(size),0) s FROM files "
        "GROUP BY owner_id ORDER BY c DESC")
    if not rows:
        await update.message.reply_text("no uploads.")
        return
    out = ["<b>users</b>", ""]
    for r in rows:
        out.append(f"<code>{r['owner_id']}</code> — {r['c']} files, {human_size(r['s'])}")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_allfiles(update, ctx):
    rows = await db_fetch(
        "SELECT id, owner_id, filename, size, downloads FROM files "
        "ORDER BY created_at DESC LIMIT 100")
    if not rows:
        await update.message.reply_text("no files.")
        return
    out = ["<b>all files</b>", ""]
    for r in rows:
        out.append(f"<code>{r['id']}</code> u=<code>{r['owner_id']}</code> "
                   f"{html.escape(r['filename'])} ({human_size(r['size'])}, {r['downloads']}↓)")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_delany(update, ctx):
    if not ctx.args:
        await update.message.reply_text("usage: /delany <id>")
        return
    fid = ctx.args[0]
    row = await db_fetch_one("SELECT * FROM files WHERE id = ?", (fid,))
    if not row:
        await update.message.reply_text("not found.")
        return
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    await db_exec("DELETE FROM files WHERE id = ?", (fid,))
    await update.message.reply_text(f"deleted <code>{fid}</code>", parse_mode=ParseMode.HTML)


@admin_only
async def cmd_broadcast(update, ctx):
    if not ctx.args:
        await update.message.reply_text("usage: /broadcast <text>")
        return
    text = " ".join(ctx.args)
    rows = await db_fetch("SELECT DISTINCT owner_id FROM files")
    sent = failed = 0
    for r in rows:
        try:
            await ctx.bot.send_message(r["owner_id"], f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"sent: {sent}, failed: {failed}")


@admin_only
async def cmd_setnote(update, ctx):
    if len(ctx.args) < 2:
        await update.message.reply_text("usage: /setnote <id> <note...>")
        return
    fid, *rest = ctx.args
    note = " ".join(rest)
    if not await db_fetch_one("SELECT id FROM files WHERE id = ?", (fid,)):
        await update.message.reply_text("not found.")
        return
    await db_exec("UPDATE files SET note = ? WHERE id = ?", (note, fid))
    await update.message.reply_text("note set.")


@admin_only
async def cmd_reload(update, ctx):
    m = await rebuild_repo_index()
    await update.message.reply_text(
        f"rescan: web={m['counts']['web']} telegram={m['counts']['telegram']} "
        f"static={m['counts']['static']}")


@admin_only
async def cmd_panel(update, ctx):
    await update.message.reply_text(
        f"{panel_base()}/\nuser: <code>{PANEL_USER}</code>\npass: <code>{PANEL_PASS}</code>",
        parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# admin panel


def _auth_ok(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        u, p = base64.b64decode(auth[6:]).decode().split(":", 1)
    except Exception:
        return False
    return u == PANEL_USER and p == PANEL_PASS


def require_auth(handler):
    async def wrapper(request: web.Request):
        if not _auth_ok(request):
            return web.Response(status=401,
                                headers={"WWW-Authenticate": 'Basic realm="panel"'},
                                text="auth required")
        return await handler(request)
    return wrapper


PANEL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>host panel</title>
<style>
body{font-family:ui-monospace,monospace;background:#0b0d10;color:#d6d9dd;margin:0;padding:24px}
h1{font-size:16px;color:#8ab4f8;margin:0 0 16px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #1f2329}
th{color:#8ab4f8;font-weight:500}
a{color:#8ab4f8;text-decoration:none}
a:hover{text-decoration:underline}
.stats{display:flex;gap:24px;margin-bottom:20px;font-size:13px;color:#9aa0a6}
.stats b{color:#d6d9dd}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px}
.tag-web{background:#1a3a1a;color:#7ee787}
.tag-telegram{background:#1a2a3a;color:#79c0ff}
.tag-static{background:#2a2a2a;color:#aaa}
h2{font-size:14px;color:#8ab4f8;margin:24px 0 8px}
</style></head>
<body>
<h1>host panel</h1>
<div class="stats">
  <span>files: <b>{n_files}</b></span>
  <span>users: <b>{n_users}</b></span>
  <span>stored: <b>{total_size}</b></span>
  <span>repo: <b>{n_repo}</b></span>
</div>

<h2>uploads</h2>
<table>
<tr><th>id</th><th>owner</th><th>filename</th><th>size</th><th>↓</th><th>note</th><th></th></tr>
{rows}
</table>

<h2>repo (auto-classified)</h2>
<table>
<tr><th>path</th><th>kind</th><th>size</th></tr>
{repo_rows}
</table>
</body></html>"""


@require_auth
async def panel_index(request):
    rows = await db_fetch("SELECT * FROM files ORDER BY created_at DESC LIMIT 500")
    n_files = len(rows)
    n_users = len({r["owner_id"] for r in rows})
    total_size = human_size(sum(r["size"] for r in rows))
    repo = await db_fetch("SELECT relpath, kind, size FROM repo_index ORDER BY kind, relpath LIMIT 500")
    n_repo = len(repo)

    body = []
    for r in rows:
        body.append(
            f'<tr>'
            f'<td><a href="/f/{r["id"]}">{r["id"]}</a></td>'
            f'<td>{r["owner_id"]}</td>'
            f'<td>{html.escape(r["filename"])}</td>'
            f'<td>{human_size(r["size"])}</td>'
            f'<td>{r["downloads"]}</td>'
            f'<td>{html.escape(r["note"] or "")}</td>'
            f'<td><a href="/del/{r["id"]}" onclick="return confirm(\'delete?\')">del</a></td>'
            f'</tr>'
        )
    repo_rows = []
    for r in repo:
        kind = r["kind"]
        cls = f"tag tag-{kind}"
        repo_rows.append(
            f'<tr>'
            f'<td><a href="/repo/{html.escape(r["relpath"])}">{html.escape(r["relpath"])}</a></td>'
            f'<td><span class="{cls}">{kind}</span></td>'
            f'<td>{human_size(r["size"])}</td>'
            f'</tr>'
        )
    return web.Response(
        text=PANEL_HTML.format(
            n_files=n_files, n_users=n_users, total_size=total_size,
            n_repo=n_repo, rows="\n".join(body), repo_rows="\n".join(repo_rows)),
        content_type="text/html")


@require_auth
async def panel_del(request):
    fid = request.match_info["id"]
    row = await db_fetch_one("SELECT * FROM files WHERE id = ?", (fid,))
    if row:
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except Exception:
            pass
        await db_exec("DELETE FROM files WHERE id = ?", (fid,))
    raise web.HTTPFound("/")


async def file_serve(request):
    fid = request.match_info["id"]
    row = await db_fetch_one("SELECT * FROM files WHERE id = ?", (fid,))
    if not row:
        return web.Response(status=404, text="not found")
    path = Path(row["path"])
    if not path.exists():
        return web.Response(status=410, text="gone")
    await db_exec("UPDATE files SET downloads = downloads + 1 WHERE id = ?", (fid,))
    return web.FileResponse(
        path,
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'})


async def repo_serve(request):
    rel = request.match_info.get("path", "")
    if rel.endswith("/"):
        rel = rel.rstrip("/") + "/index.html"
    target = (REPO_DIR / rel).resolve()
    try:
        target.relative_to(REPO_DIR.resolve())
    except ValueError:
        return web.Response(status=403, text="forbidden")
    if target.is_dir():
        for ix in ("index.html", "index.htm"):
            cand = target / ix
            if cand.exists():
                return web.FileResponse(cand)
        return web.Response(status=404, text="no index")
    if not target.is_file():
        return web.Response(status=404, text="not found")
    return web.FileResponse(target)


async def panel_stats_api(request):
    if not _auth_ok(request):
        return web.json_response({"error": "auth"}, status=401)
    files = await db_fetch_one("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files")
    repo = await db_fetch("SELECT kind, COUNT(*) c FROM repo_index GROUP BY kind")
    return web.json_response({
        "files": files["c"], "stored": files["s"],
        "repo": {r["kind"]: r["c"] for r in repo},
    })


async def start_panel():
    app = web.Application()
    app.router.add_get("/", panel_index)
    app.router.add_get("/api/stats", panel_stats_api)
    app.router.add_get("/del/{id}", panel_del)
    app.router.add_get("/f/{id}", file_serve)
    app.router.add_get("/repo", lambda r: web.HTTPFound("/repo/"))
    app.router.add_get("/repo/{path:.*}", repo_serve)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PANEL_PORT)
    await site.start()
    print(f"[+] panel on http://0.0.0.0:{PANEL_PORT}/  public={panel_base()}")


# --------------------------------------------------------------------------- #
# main


async def post_init(app: Application):
    await db_init()
    manifest = await rebuild_repo_index()
    print(f"[+] repo index: {manifest['counts']}")
    asyncio.create_task(start_panel())


def build_app():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for name, fn in [
        ("start", cmd_start), ("help", cmd_help), ("list", cmd_list),
        ("get", cmd_get), ("del", cmd_del), ("stats", cmd_stats), ("repo", cmd_repo),
    ]:
        app.add_handler(CommandHandler(name, fn))

    for name, fn in [
        ("admin", cmd_admin), ("users", cmd_users), ("allfiles", cmd_allfiles),
        ("delany", cmd_delany), ("broadcast", cmd_broadcast), ("setnote", cmd_setnote),
        ("reload", cmd_reload), ("manifest", cmd_manifest), ("panel", cmd_panel),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    return app


def main():
    print("[+] bot starting")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()