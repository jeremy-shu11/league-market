from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import secrets
import sqlite3
import threading
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ingestion import compact_snapshot, prune_artifacts, record_ingestion, record_ingestion_error
from market_engine import (
    PAYOUT_CREDITS,
    liquidity_value,
    lmsr_prices,
    shares_for_budget,
    shares_to_move_probability,
    trade_cost,
    worst_case_subsidy,
)
from modeling import MODEL_VERSION, model_fingerprint, projection_points, run_league_model
from sleeper import NflverseRankingsAdapter, SleeperAdapter


PROJECT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
NUXT_PUBLIC_DIR = PROJECT_DIR / ".output" / "public"
NUXT_ASSET_DIR = NUXT_PUBLIC_DIR / "_nuxt"
TURSO_DATABASE_URL = (
    os.environ.get("TURSO_DATABASE_URL")
    or os.environ.get("LIBSQL_URL")
    or os.environ.get("LIBSQL_DATABASE_URL")
    or ""
).strip()
TURSO_AUTH_TOKEN = (
    os.environ.get("TURSO_AUTH_TOKEN")
    or os.environ.get("LIBSQL_AUTH_TOKEN")
    or ""
).strip()


def env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


DEFAULT_DATA_DIR = Path("/tmp/league-market") if TURSO_DATABASE_URL else PROJECT_DIR / "data"
DATA_DIR = Path(os.environ.get("LEAGUE_MARKET_DATA_DIR", DEFAULT_DATA_DIR))
DB_PATH = Path(os.environ.get("LEAGUE_MARKET_DB", DATA_DIR / "market.sqlite"))
RAW_DATA_DIR = Path(os.environ.get("LEAGUE_MARKET_RAW_DATA", DATA_DIR / "raw"))
BACKUP_DIR = Path(os.environ.get("LEAGUE_MARKET_BACKUP_DIR", DB_PATH.parent / "backups"))
APP_ENV = os.environ.get("LEAGUE_MARKET_ENV", "development").strip().lower()
APP_HOST = os.environ.get("LEAGUE_MARKET_HOST", "127.0.0.1").strip()
APP_PORT = int(os.environ.get("LEAGUE_MARKET_PORT", "5065"))
DEFAULT_LEAGUE_ID = os.environ.get("LEAGUE_MARKET_LEAGUE_ID", "1326428061876371456")
DEFAULT_INVITE_CODE = os.environ.get("LEAGUE_MARKET_INVITE_CODE", "theleague")
DEFAULT_ADMIN_CODE = os.environ.get("LEAGUE_MARKET_ADMIN_CODE", "commissioner")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "LEAGUE_MARKET_ALLOWED_ORIGINS",
        f"http://127.0.0.1:{APP_PORT},http://localhost:{APP_PORT}",
    ).split(",")
    if origin.strip()
]
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "LEAGUE_MARKET_ALLOWED_HOSTS",
        "127.0.0.1,localhost,testserver",
    ).split(",")
    if host.strip()
]
STARTING_BALANCE = 10_000.0
DEFAULT_FUND_BALANCE = 599.0
DEFAULT_TROPHY_RESERVE = 100.0
DEFAULT_CUP_RESERVE = 225.0
DEFAULT_DRAFT_RESERVE = 110.0
DEFAULT_SAFETY_BUFFER = 50.0
DEFAULT_CORE_ALLOCATION = 0.50
DEFAULT_TEAM_ALLOCATION = 0.30
DEFAULT_PLAYER_ALLOCATION = 0.20
MODEL_SIMULATIONS = int(os.environ.get("LEAGUE_MARKET_SIMULATIONS", "20000"))
LIVE_SCORE_SIMULATIONS = int(os.environ.get("LEAGUE_MARKET_LIVE_SCORE_SIMULATIONS", "5000"))
LIVE_SCORE_MARK_INTERVAL_HOURS = float(os.environ.get("LEAGUE_MARKET_LIVE_SCORE_INTERVAL_HOURS", "0.08"))
AUTOMATION_DASHBOARD_GRACE_HOURS = float(os.environ.get("LEAGUE_MARKET_AUTOMATION_DASHBOARD_GRACE_HOURS", "1"))
AUTOMATION_RUNNING_TIMEOUT_HOURS = float(os.environ.get("LEAGUE_MARKET_AUTOMATION_RUNNING_TIMEOUT_HOURS", "0.25"))
SCHEDULE_PIPELINE = env_flag("LEAGUE_MARKET_SCHEDULE_PIPELINE", True)
ADMIN_SESSION_COOKIE = "league_market_admin"
ADMIN_SESSION_HOURS = 8
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_PATH: Optional[str] = None
_DB_CONFIG_LOCK = threading.Lock()
_DB_CONFIG_READY_PATH: Optional[str] = None
_PIPELINE_LOCK = threading.Lock()
_SCHEDULE_LOCK = threading.Lock()


def validate_runtime_config(
    app_env: str = APP_ENV,
    invite_code: str = DEFAULT_INVITE_CODE,
    admin_code: str = DEFAULT_ADMIN_CODE,
) -> None:
    if TURSO_DATABASE_URL and not TURSO_AUTH_TOKEN:
        raise RuntimeError("TURSO_AUTH_TOKEN or LIBSQL_AUTH_TOKEN is required when using Turso/libSQL")
    if app_env != "production":
        return
    if invite_code == "theleague" or admin_code == "commissioner":
        raise RuntimeError("Production requires non-default invite and admin codes")
    if len(invite_code) < 12 or len(admin_code) < 16:
        raise RuntimeError("Production invite code must be 12+ characters and admin code 16+ characters")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_config()
    configure_database()
    execute_schema()
    yield


app = FastAPI(
    title="The League Market API",
    lifespan=lifespan,
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None if APP_ENV == "production" else "/redoc",
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Participant-Token", "X-Admin-Code"],
)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
if NUXT_ASSET_DIR.exists():
    app.mount("/_nuxt", StaticFiles(directory=NUXT_ASSET_DIR), name="nuxt")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https://sleepercdn.com; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    if APP_ENV == "production" and request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/_nuxt/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path == "/_payload.json":
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    elif request.url.path in {"/static/app.js", "/static/styles.css"}:
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


class JoinRequest(BaseModel):
    invite_code: str
    display_name: str = Field(default="", max_length=80)
    sleeper_username: Optional[str] = ""
    league_id: str = DEFAULT_LEAGUE_ID


class AdminSessionRequest(BaseModel):
    admin_code: str = Field(min_length=1, max_length=256)


class TradeRequest(BaseModel):
    outcome_id: int
    shares: float = Field(gt=0, le=500)
    market_revision: Optional[int] = Field(default=None, ge=0)
    max_cost: Optional[float] = Field(default=None, gt=0)
    min_proceeds: Optional[float] = Field(default=None, ge=0)


class QuoteRequest(BaseModel):
    outcome_id: int
    side: Literal["buy", "sell"]
    shares: Optional[float] = Field(default=None, gt=0, le=500)
    budget: Optional[float] = Field(default=None, gt=0, le=STARTING_BALANCE)


class ManualMarketRequest(BaseModel):
    title: str
    market_type: str = "binary"
    outcomes: list[str]
    close_time: Optional[str] = ""
    resolution_source: str = "Commissioner"
    resolution_rule: str
    liquidity_label: str = "medium"
    league_id: str = DEFAULT_LEAGUE_ID


class ResolveRequest(BaseModel):
    winning_outcome_id: Optional[int] = None


class SyncSleeperRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID


class SeedMarketsRequest(BaseModel):
    reset_seeded: bool = True
    league_id: str = DEFAULT_LEAGUE_ID


class SetupLeagueRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID
    reset_markets: bool = True
    include_cup: bool = True


class DemoPopulateRequest(BaseModel):
    reset_seeded_if_empty: bool = True
    reset_existing: bool = True


class ClaimIdentityRequest(BaseModel):
    user_id: str


class AccountCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=80)


class ParticipantRoleRequest(BaseModel):
    role: str = Field(pattern="^(participant|commissioner|admin)$")


class ParticipantLinkRequest(BaseModel):
    sleeper_user_id: Optional[str] = ""


class InviteCreateRequest(BaseModel):
    code: Optional[str] = ""
    display_name: Optional[str] = ""
    sleeper_user_id: Optional[str] = ""
    sleeper_username: Optional[str] = ""
    role: str = Field(default="participant", pattern="^(participant|commissioner|admin)$")
    uses_remaining: Optional[int] = Field(default=None, ge=1, le=500)
    league_id: str = DEFAULT_LEAGUE_ID


class ManagerInvitesRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID


class FundSettingsRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID
    starting_balance: float = Field(ge=0)
    trophy_reserve: float = Field(ge=0)
    cup_reserve: float = Field(ge=0)
    draft_reserve: float = Field(ge=0)
    safety_buffer: float = Field(ge=0)
    core_pct: float = Field(ge=0, le=1)
    team_pct: float = Field(ge=0, le=1)
    player_pct: float = Field(ge=0, le=1)


class FundManualEntryRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID
    entry_type: str = Field(pattern="^(income|expense|adjustment)$")
    amount: float
    description: str = Field(min_length=2, max_length=160)
    team_roster_id: Optional[int] = None


class FundSleeperSyncRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID
    start_round: int = Field(default=1, ge=1, le=18)
    end_round: int = Field(default=18, ge=1, le=18)


class FundMarketPrizeRequest(BaseModel):
    league_id: str = DEFAULT_LEAGUE_ID
    category: str = Field(pattern="^(core|team|player)$")
    prize_pool: float = Field(gt=0)
    payout_mode: str = Field(default="proportional_shares", pattern="^proportional_shares$")


class FeedbackRequest(BaseModel):
    category: Literal["bug", "confusing", "pricing_odds", "trade_flow", "settlement", "idea", "other"] = "other"
    message: str = Field(min_length=3, max_length=2000)
    page: str = Field(default="", max_length=80)
    market_id: Optional[int] = None
    market_title: str = Field(default="", max_length=160)
    context: dict = Field(default_factory=dict)


class FeedbackStatusRequest(BaseModel):
    status: Literal["new", "reviewing", "resolved", "wont_fix"]
    note: str = Field(default="", max_length=1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_league_id(value: Optional[str]) -> str:
    return (value or DEFAULT_LEAGUE_ID).strip() or DEFAULT_LEAGUE_ID


def invite_code_slug(value: Optional[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or secrets.token_urlsafe(5).lower().replace("_", "-")


def display_name_from_code(code: str) -> str:
    words = [part for part in re.split(r"[-_\s]+", code.strip()) if part]
    return " ".join(part[:1].upper() + part[1:] for part in words) or "League Trader"


def invite_is_personal(invite) -> bool:
    return bool(
        str(invite["display_name"] or "").strip()
        or str(invite["sleeper_user_id"] or "").strip()
        or str(invite["sleeper_username"] or "").strip()
    )


def visible_market_environments() -> tuple[str, ...]:
    if APP_ENV == "development":
        return ("development", "production")
    return (APP_ENV,)


def data_environment() -> str:
    return "test" if APP_ENV == "test" else "production"


def using_remote_database() -> bool:
    return bool(TURSO_DATABASE_URL)


def database_identity() -> str:
    return f"libsql:{TURSO_DATABASE_URL}" if using_remote_database() else str(DB_PATH.resolve())


def remote_database_url() -> str:
    return TURSO_DATABASE_URL


class RemoteDatabaseRow:
    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]):
        self._columns = columns
        self._values = values
        self._index = {column: index for index, column in enumerate(columns)}

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __bool__(self) -> bool:
        return bool(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def get(self, key: str, default: Any = None) -> Any:
        index = self._index.get(key)
        return default if index is None else self._values[index]


class RemoteDatabaseCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def _columns(self) -> tuple[str, ...]:
        return tuple(column[0] for column in (self.description or ()))

    def _wrap_row(self, row):
        if row is None:
            return None
        if hasattr(row, "keys"):
            return row
        return RemoteDatabaseRow(self._columns(), tuple(row))

    def fetchone(self):
        return self._wrap_row(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall() or []
        return [self._wrap_row(row) for row in rows]

    def fetchmany(self, size: Optional[int] = None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._wrap_row(row) for row in (rows or [])]

    def execute(self, sql: str, parameters=None):
        cursor = self._cursor.execute(sql, parameters) if parameters is not None else self._cursor.execute(sql)
        self._cursor = cursor
        return self

    def executemany(self, sql: str, parameters):
        self._cursor = self._cursor.executemany(sql, parameters)
        return self

    def executescript(self, script: str):
        self._cursor.executescript(script)
        return self


class RemoteDatabaseConnection:
    def __init__(self, connection, sync_on_commit: bool = True):
        self._connection = connection
        self._sync_on_commit = sync_on_commit

    @property
    def in_transaction(self) -> bool:
        return bool(getattr(self._connection, "in_transaction", False))

    def execute(self, sql: str, parameters=None) -> RemoteDatabaseCursor:
        cursor = self._connection.execute(sql, parameters) if parameters is not None else self._connection.execute(sql)
        return RemoteDatabaseCursor(cursor)

    def executemany(self, sql: str, parameters) -> RemoteDatabaseCursor:
        return RemoteDatabaseCursor(self._connection.executemany(sql, parameters))

    def executescript(self, script: str):
        return self._connection.executescript(script)

    def commit(self) -> None:
        self._connection.commit()
        sync = getattr(self._connection, "sync", None)
        if self._sync_on_commit and callable(sync):
            sync()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def begin_immediate(conn) -> None:
    if not bool(getattr(conn, "in_transaction", False)):
        conn.execute("BEGIN IMMEDIATE")


def db():
    if using_remote_database():
        try:
            import libsql
        except ImportError as error:
            raise RuntimeError("Install the libsql package to use TURSO_DATABASE_URL") from error
        connection = libsql.connect(
            database=remote_database_url(),
            auth_token=TURSO_AUTH_TOKEN,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return RemoteDatabaseConnection(connection, sync_on_commit=False)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def configure_database() -> None:
    global _DB_CONFIG_READY_PATH
    resolved_path = database_identity()
    if _DB_CONFIG_READY_PATH == resolved_path:
        return
    with _DB_CONFIG_LOCK:
        if _DB_CONFIG_READY_PATH == resolved_path:
            return
        _configure_database_unchecked()
        _DB_CONFIG_READY_PATH = resolved_path


def _configure_database_unchecked() -> None:
    if using_remote_database():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 10000")
    finally:
        connection.close()


def create_database_backup() -> dict:
    if using_remote_database():
        return {
            "provider": "turso/libsql",
            "managed": True,
            "path": None,
            "bytes": None,
            "created_at": now_iso(),
            "integrity_check": "managed",
            "note": "Remote libSQL storage is active; use the database provider's restore tooling for backups.",
        }
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = BACKUP_DIR / f"market-{stamp}.sqlite"
    temporary = destination.with_suffix(".sqlite.tmp")
    source = sqlite3.connect(DB_PATH, timeout=10)
    target = sqlite3.connect(temporary)
    try:
        try:
            source.backup(target)
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
        finally:
            target.close()
            source.close()
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "created_at": now_iso(),
        "integrity_check": "ok",
    }


def _execute_schema_unchecked() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                league_id TEXT NOT NULL DEFAULT '1326428061876371456',
                role TEXT NOT NULL DEFAULT 'participant',
                uses_remaining INTEGER,
                display_name TEXT NOT NULL DEFAULT '',
                sleeper_user_id TEXT NOT NULL DEFAULT '',
                sleeper_username TEXT NOT NULL DEFAULT '',
                participant_id INTEGER,
                claimed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL DEFAULT '1326428061876371456',
                display_name TEXT NOT NULL,
                sleeper_username TEXT,
                token TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'participant',
                cash REAL NOT NULL DEFAULT 10000,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fantasy_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                roster_id INTEGER NOT NULL,
                owner_id TEXT,
                team_name TEXT NOT NULL,
                display_name TEXT,
                avatar TEXT,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                ties INTEGER NOT NULL DEFAULT 0,
                points_for REAL NOT NULL DEFAULT 0,
                points_against REAL NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(league_id, roster_id)
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                full_name TEXT NOT NULL,
                position TEXT NOT NULL,
                team TEXT,
                roster_id INTEGER,
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(league_id, player_id)
            );
            CREATE TABLE IF NOT EXISTS nfl_players (
                player_id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                position TEXT NOT NULL,
                team TEXT,
                status TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roster_memberships (
                league_id TEXT NOT NULL,
                roster_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                is_starter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (league_id, roster_id, player_id)
            );
            CREATE TABLE IF NOT EXISTS league_managers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT,
                display_name TEXT,
                team_name TEXT,
                avatar TEXT,
                is_owner INTEGER NOT NULL DEFAULT 0,
                roster_ids_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(league_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL DEFAULT '1326428061876371456',
                title TEXT NOT NULL,
                market_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                liquidity_label TEXT NOT NULL DEFAULT 'medium',
                liquidity REAL NOT NULL,
                payout REAL NOT NULL DEFAULT 100,
                close_time TEXT,
                resolution_source TEXT NOT NULL,
                resolution_rule TEXT NOT NULL,
                source_key TEXT UNIQUE,
                winning_outcome_id INTEGER,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                source_ref TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS positions (
                participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
                outcome_id INTEGER NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
                shares REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (participant_id, outcome_id)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL REFERENCES participants(id),
                market_id INTEGER NOT NULL REFERENCES markets(id),
                outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
                side TEXT NOT NULL,
                shares REAL NOT NULL,
                cash_delta REAL NOT NULL,
                price_before REAL NOT NULL,
                price_after REAL NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER REFERENCES participants(id),
                entry_type TEXT NOT NULL,
                amount REAL NOT NULL,
                market_id INTEGER,
                outcome_id INTEGER,
                note TEXT,
                is_demo INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                league_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                dataset TEXT NOT NULL,
                league_id TEXT NOT NULL,
                season TEXT NOT NULL DEFAULT '',
                week INTEGER,
                fetched_at TEXT NOT NULL,
                source_updated_at TEXT,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                artifact_path TEXT NOT NULL DEFAULT '',
                byte_count INTEGER NOT NULL DEFAULT 0,
                schema_version TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS projection_observations (
                ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
                player_id TEXT NOT NULL,
                season TEXT NOT NULL,
                week INTEGER NOT NULL,
                projected_points REAL NOT NULL DEFAULT 0,
                stats_json TEXT NOT NULL,
                PRIMARY KEY (ingestion_run_id, player_id)
            );
            CREATE TABLE IF NOT EXISTS model_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                season TEXT NOT NULL,
                week INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                seed INTEGER NOT NULL,
                simulation_count INTEGER NOT NULL,
                input_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                coverage REAL NOT NULL,
                diagnostics_json TEXT NOT NULL,
                assumptions_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'succeeded',
                created_at TEXT NOT NULL,
                published_at TEXT,
                UNIQUE(league_id, season, week, model_version, input_hash)
            );
            CREATE TABLE IF NOT EXISTS model_run_inputs (
                model_run_id INTEGER NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
                ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
                PRIMARY KEY (model_run_id, ingestion_run_id)
            );
            CREATE TABLE IF NOT EXISTS probability_estimates (
                model_run_id INTEGER NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
                market_family TEXT NOT NULL,
                subject_ref TEXT NOT NULL,
                probability REAL NOT NULL,
                PRIMARY KEY (model_run_id, market_family, subject_ref)
            );
            CREATE TABLE IF NOT EXISTS team_score_estimates (
                model_run_id INTEGER NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
                roster_id INTEGER NOT NULL,
                projected_mean REAL NOT NULL,
                projected_stdev REAL NOT NULL,
                PRIMARY KEY (model_run_id, roster_id)
            );
            CREATE TABLE IF NOT EXISTS contract_definitions (
                contract_key TEXT PRIMARY KEY,
                market_family TEXT NOT NULL,
                settlement_version TEXT NOT NULL,
                resolution_source TEXT NOT NULL,
                resolution_rule TEXT NOT NULL,
                close_policy TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(market_id, revision)
            );
            CREATE TABLE IF NOT EXISTS price_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                outcome_id INTEGER NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
                event_id INTEGER REFERENCES market_events(id) ON DELETE CASCADE,
                market_probability REAL NOT NULL,
                model_probability REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resolution_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                source_snapshot_at TEXT NOT NULL,
                result_key TEXT NOT NULL,
                scores_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(market_id, source_snapshot_at)
            );
            CREATE TABLE IF NOT EXISTS fund_settings (
                league_id TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL DEFAULT 599,
                trophy_reserve REAL NOT NULL DEFAULT 100,
                cup_reserve REAL NOT NULL DEFAULT 225,
                draft_reserve REAL NOT NULL DEFAULT 110,
                safety_buffer REAL NOT NULL DEFAULT 50,
                core_pct REAL NOT NULL DEFAULT 0.5,
                team_pct REAL NOT NULL DEFAULT 0.3,
                player_pct REAL NOT NULL DEFAULT 0.2,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fund_ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                source_key TEXT,
                team_roster_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(league_id, source_key)
            );
            CREATE TABLE IF NOT EXISTS fund_market_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                category TEXT NOT NULL,
                allocated_budget REAL NOT NULL DEFAULT 0,
                reserved_exposure REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(league_id, category)
            );
            CREATE TABLE IF NOT EXISTS fund_market_prizes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                prize_pool REAL NOT NULL,
                payout_mode TEXT NOT NULL DEFAULT 'proportional_shares',
                status TEXT NOT NULL DEFAULT 'assigned',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paid_at TEXT,
                UNIQUE(market_id)
            );
            CREATE TABLE IF NOT EXISTS fund_payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                participant_id INTEGER NOT NULL REFERENCES participants(id),
                outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
                shares REAL NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                fund_ledger_entry_id INTEGER REFERENCES fund_ledger_entries(id),
                created_at TEXT NOT NULL,
                paid_at TEXT,
                UNIQUE(market_id, participant_id, outcome_id)
            );
            CREATE TABLE IF NOT EXISTS feedback_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id TEXT NOT NULL,
                participant_id INTEGER REFERENCES participants(id),
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                page TEXT NOT NULL DEFAULT '',
                market_id INTEGER,
                market_title TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                user_agent TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                admin_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        migrate_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO invite_codes (code, league_id, role, uses_remaining, created_at) VALUES (?, ?, 'participant', NULL, ?)",
            (DEFAULT_INVITE_CODE, DEFAULT_LEAGUE_ID, now_iso()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO invite_codes (code, league_id, role, uses_remaining, created_at) VALUES (?, ?, 'admin', NULL, ?)",
            (DEFAULT_ADMIN_CODE, DEFAULT_LEAGUE_ID, now_iso()),
        )


def execute_schema() -> None:
    global _SCHEMA_READY_PATH
    resolved_path = database_identity()
    if _SCHEMA_READY_PATH == resolved_path:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY_PATH == resolved_path:
            return
        _execute_schema_unchecked()
        _SCHEMA_READY_PATH = resolved_path


def table_columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_schema(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "invite_codes", "league_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_LEAGUE_ID}'")
    ensure_column(conn, "invite_codes", "display_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "invite_codes", "sleeper_user_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "invite_codes", "sleeper_username", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "invite_codes", "participant_id", "INTEGER")
    ensure_column(conn, "invite_codes", "claimed_at", "TEXT")
    ensure_column(conn, "participants", "league_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_LEAGUE_ID}'")
    ensure_column(conn, "participants", "sleeper_user_id", "TEXT")
    ensure_column(conn, "participants", "environment", "TEXT NOT NULL DEFAULT 'production'")
    ensure_column(conn, "markets", "league_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_LEAGUE_ID}'")
    ensure_column(conn, "markets", "environment", "TEXT NOT NULL DEFAULT 'production'")
    ensure_column(conn, "markets", "origin", "TEXT NOT NULL DEFAULT 'legacy'")
    ensure_column(conn, "markets", "visibility", "TEXT NOT NULL DEFAULT 'public'")
    ensure_column(conn, "markets", "contract_key", "TEXT")
    ensure_column(conn, "markets", "contract_version", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "markets", "model_run_id", "INTEGER")
    ensure_column(conn, "markets", "latest_model_run_id", "INTEGER")
    ensure_column(conn, "markets", "trade_revision", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "markets", "opened_at", "TEXT")
    ensure_column(conn, "markets", "closed_at", "TEXT")
    ensure_column(conn, "outcomes", "prior_probability", "REAL")
    ensure_column(conn, "outcomes", "model_probability", "REAL")
    ensure_column(conn, "trades", "is_demo", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "ledger_entries", "is_demo", "INTEGER NOT NULL DEFAULT 0")
    migrate_fantasy_teams(conn)
    migrate_players(conn)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_participants_league ON participants(league_id);
        CREATE INDEX IF NOT EXISTS idx_invite_codes_league ON invite_codes(league_id);
        CREATE INDEX IF NOT EXISTS idx_invite_codes_participant ON invite_codes(participant_id);
        CREATE INDEX IF NOT EXISTS idx_markets_league ON markets(league_id);
        CREATE INDEX IF NOT EXISTS idx_fantasy_teams_league ON fantasy_teams(league_id);
        CREATE INDEX IF NOT EXISTS idx_players_league ON players(league_id);
        CREATE INDEX IF NOT EXISTS idx_roster_memberships_league ON roster_memberships(league_id, roster_id);
        CREATE INDEX IF NOT EXISTS idx_league_managers_league ON league_managers(league_id);
        CREATE INDEX IF NOT EXISTS idx_trades_demo ON trades(participant_id, is_demo);
        CREATE INDEX IF NOT EXISTS idx_ledger_demo ON ledger_entries(participant_id, is_demo);
        CREATE INDEX IF NOT EXISTS idx_source_snapshots_league ON source_snapshots(league_id);
        CREATE INDEX IF NOT EXISTS idx_ingestion_lookup ON ingestion_runs(provider, dataset, league_id, season, week, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_model_runs_lookup ON model_runs(league_id, season, week, created_at);
        CREATE INDEX IF NOT EXISTS idx_score_estimates_run ON team_score_estimates(model_run_id, roster_id);
        CREATE INDEX IF NOT EXISTS idx_market_visibility ON markets(league_id, environment, visibility, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_contract_version ON markets(league_id, contract_key, contract_version, environment) WHERE contract_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_market_events_market ON market_events(market_id, revision);
        CREATE INDEX IF NOT EXISTS idx_price_ticks_outcome ON price_ticks(outcome_id, id);
        CREATE INDEX IF NOT EXISTS idx_resolution_observations_market ON resolution_observations(market_id, id);
        CREATE INDEX IF NOT EXISTS idx_fund_ledger_league ON fund_ledger_entries(league_id);
        CREATE INDEX IF NOT EXISTS idx_fund_ledger_team ON fund_ledger_entries(league_id, team_roster_id);
        CREATE INDEX IF NOT EXISTS idx_fund_allocations_league ON fund_market_allocations(league_id);
        CREATE INDEX IF NOT EXISTS idx_fund_prizes_league ON fund_market_prizes(league_id);
        CREATE INDEX IF NOT EXISTS idx_fund_payouts_league ON fund_payouts(league_id);
        CREATE INDEX IF NOT EXISTS idx_fund_payouts_market ON fund_payouts(market_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_league_status ON feedback_items(league_id, status, id);
        CREATE INDEX IF NOT EXISTS idx_feedback_market ON feedback_items(market_id);
        """
    )
    run_numbered_migrations(conn)


def run_numbered_migrations(conn: sqlite3.Connection) -> None:
    migrations = [
        (1, "archive_playwright_contracts", archive_playwright_contracts),
        (2, "compact_legacy_snapshots", compact_legacy_snapshots),
        (3, "promote_launch_records", promote_launch_records),
        (4, "archive_legacy_ui_qa_participants", archive_legacy_ui_qa_participants),
        (5, "admin_sessions_and_operations", create_admin_operations_tables),
        (6, "repair_untraded_market_liquidity", repair_untraded_market_liquidity),
        (7, "schedule_weekly_sleeper_settlement", schedule_weekly_sleeper_settlement),
    ]
    applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, name, migration in migrations:
        if version in applied:
            continue
        migration(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, now_iso()),
        )


def create_admin_operations_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS admin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id TEXT NOT NULL,
            participant_id INTEGER REFERENCES participants(id),
            session_id INTEGER REFERENCES admin_sessions(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT '',
            entity_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_hash ON admin_sessions(token_hash);
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at);
        CREATE INDEX IF NOT EXISTS idx_job_runs_lookup ON job_runs(league_id, job_type, id);
        CREATE INDEX IF NOT EXISTS idx_admin_events_league ON admin_events(league_id, id);
        """
    )


def repair_untraded_market_liquidity(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE markets
        SET liquidity = 35.0
        WHERE origin = 'model' AND status IN ('draft', 'open') AND liquidity < 35.0
          AND NOT EXISTS (SELECT 1 FROM trades WHERE trades.market_id = markets.id)
        """
    )


def schedule_weekly_sleeper_settlement(conn: sqlite3.Connection) -> None:
    settlement_copy = " Settlement runs automatically Tuesday at 1:00 AM ET from official Sleeper scores."
    conn.execute(
        """
        UPDATE markets
        SET resolution_rule = RTRIM(resolution_rule) || ?,
            resolution_source = 'Official Sleeper weekly scores'
        WHERE origin = 'model'
          AND (contract_key LIKE '%:week:%:week_top' OR contract_key LIKE '%:week:%:week_low')
          AND resolution_rule NOT LIKE '%Tuesday at 1:00 AM ET%'
        """,
        (settlement_copy,),
    )
    conn.execute(
        """
        UPDATE contract_definitions
        SET resolution_rule = RTRIM(resolution_rule) || ?,
            resolution_source = 'Official Sleeper weekly scores',
            close_policy = 'first_nfl_kickoff;settle_tuesday_0100_et'
        WHERE market_family IN ('week_top', 'week_low')
          AND resolution_rule NOT LIKE '%Tuesday at 1:00 AM ET%'
        """,
        (settlement_copy,),
    )


def archive_playwright_contracts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE markets
        SET environment = 'test', origin = 'automated_test', visibility = 'hidden', status = 'archived'
        WHERE title LIKE 'Playwright Coin Toss%'
        """
    )
    conn.execute(
        """
        UPDATE markets
        SET origin = 'legacy_template', visibility = 'hidden', status = 'archived'
        WHERE (source_key LIKE 'season:%' OR source_key LIKE 'cup:%')
          AND NOT EXISTS (SELECT 1 FROM trades WHERE trades.market_id = markets.id)
        """
    )
    conn.execute(
        """
        UPDATE markets
        SET origin = 'legacy_template', status = 'closed', closed_at = COALESCE(closed_at, ?)
        WHERE (source_key LIKE 'season:%' OR source_key LIKE 'cup:%')
          AND EXISTS (SELECT 1 FROM trades WHERE trades.market_id = markets.id)
          AND status = 'open'
        """,
        (now_iso(),),
    )
    conn.execute(
        """
        UPDATE participants SET environment = 'test'
        WHERE display_name LIKE 'Playwright%'
           OR id IN (
             SELECT DISTINCT t.participant_id
             FROM trades t JOIN markets m ON m.id = t.market_id
             WHERE m.origin = 'automated_test'
           )
        """
    )


def compact_legacy_snapshots(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, league_id, payload_json FROM source_snapshots ORDER BY league_id, id DESC"
    ).fetchall()
    kept_leagues: set[str] = set()
    for row in rows:
        league_id = str(row["league_id"])
        if league_id in kept_leagues:
            conn.execute("DELETE FROM source_snapshots WHERE id = ?", (row["id"],))
            continue
        kept_leagues.add(league_id)
        try:
            payload = compact_snapshot(json.loads(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        conn.execute(
            "UPDATE source_snapshots SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, separators=(",", ":")), row["id"]),
        )


def promote_launch_records(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE markets SET environment = 'production' WHERE environment = 'development' AND origin = 'model'"
    )
    conn.execute(
        "UPDATE participants SET environment = 'production' WHERE environment = 'development'"
    )


def archive_legacy_ui_qa_participants(conn: sqlite3.Connection) -> None:
    qa_prefixes = (
        "layout check%",
        "design review%",
        "portfolio demo%",
        "chart review%",
        "debug %",
        "robin %",
        "audit %",
        "theme %",
        "grid %",
        "light tester%",
        "qa %",
        "visual %",
        "final %",
        "money %",
        "admin %",
        "flow %",
        "quote %",
        "smoke %",
        "stock ui qa%",
    )
    clauses = " OR ".join("LOWER(display_name) LIKE ?" for _ in qa_prefixes)
    conn.execute(
        f"""
        UPDATE participants SET environment = 'test'
        WHERE environment = 'production' AND sleeper_user_id IS NULL
          AND ({clauses})
        """,
        qa_prefixes,
    )


def migrate_fantasy_teams(conn: sqlite3.Connection) -> None:
    columns = table_columns(conn, "fantasy_teams")
    if "id" in columns and "league_id" in columns and columns["roster_id"]["pk"] == 0:
        return
    conn.execute("ALTER TABLE fantasy_teams RENAME TO fantasy_teams_old")
    conn.execute(
        """
        CREATE TABLE fantasy_teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id TEXT NOT NULL,
            roster_id INTEGER NOT NULL,
            owner_id TEXT,
            team_name TEXT NOT NULL,
            display_name TEXT,
            avatar TEXT,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            ties INTEGER NOT NULL DEFAULT 0,
            points_for REAL NOT NULL DEFAULT 0,
            points_against REAL NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(league_id, roster_id)
        )
        """
    )
    old_columns = table_columns(conn, "fantasy_teams_old")
    league_expr = "league_id" if "league_id" in old_columns else f"'{DEFAULT_LEAGUE_ID}'"
    conn.execute(
        f"""
        INSERT OR IGNORE INTO fantasy_teams
          (league_id, roster_id, owner_id, team_name, display_name, avatar, wins, losses, ties, points_for, points_against, raw_json, updated_at)
        SELECT {league_expr}, roster_id, owner_id, team_name, display_name, avatar, wins, losses, ties, points_for, points_against, raw_json, updated_at
        FROM fantasy_teams_old
        """
    )
    conn.execute("DROP TABLE fantasy_teams_old")


def migrate_players(conn: sqlite3.Connection) -> None:
    columns = table_columns(conn, "players")
    if "id" in columns and "league_id" in columns and columns["player_id"]["pk"] == 0:
        return
    conn.execute("ALTER TABLE players RENAME TO players_old")
    conn.execute(
        """
        CREATE TABLE players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id TEXT NOT NULL,
            player_id TEXT NOT NULL,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            team TEXT,
            roster_id INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(league_id, player_id)
        )
        """
    )
    old_columns = table_columns(conn, "players_old")
    league_expr = "league_id" if "league_id" in old_columns else f"'{DEFAULT_LEAGUE_ID}'"
    conn.execute(
        f"""
        INSERT OR IGNORE INTO players
          (league_id, player_id, full_name, position, team, roster_id, raw_json, updated_at)
        SELECT {league_expr}, player_id, full_name, position, team, roster_id, raw_json, updated_at
        FROM players_old
        """
    )
    conn.execute("DROP TABLE players_old")


def frontend_asset_version() -> str:
    files = [
        FRONTEND_DIR / "index.html",
        FRONTEND_DIR / "app.js",
        FRONTEND_DIR / "styles.css",
        FRONTEND_DIR / "vendor" / "chart.umd.js",
        FRONTEND_DIR / "vendor" / "lucide.min.js",
    ]
    if (NUXT_PUBLIC_DIR / "index.html").exists():
        files.append(NUXT_PUBLIC_DIR / "index.html")
    signature = ":".join(str(path.stat().st_mtime_ns) for path in files)
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]


def frontend_index_path() -> Path:
    nuxt_index = NUXT_PUBLIC_DIR / "index.html"
    if nuxt_index.exists():
        return nuxt_index
    return FRONTEND_DIR / "index.html"


@app.get("/")
def index() -> HTMLResponse:
    document = frontend_index_path().read_text(encoding="utf-8")
    document = document.replace("__ASSET_VERSION__", frontend_asset_version())
    return HTMLResponse(document, headers={"Cache-Control": "no-store"})


@app.get("/_payload.json", include_in_schema=False)
def nuxt_payload() -> FileResponse:
    payload = NUXT_PUBLIC_DIR / "_payload.json"
    if not payload.exists():
        raise HTTPException(status_code=404, detail="Nuxt payload not generated")
    return FileResponse(payload, media_type="application/json")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def get_participant(conn: sqlite3.Connection, token: Optional[str]) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Missing participant token")
    row = conn.execute("SELECT * FROM participants WHERE token = ?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid participant token")
    return dict(row)


def require_admin(x_admin_code: Optional[str] = Header(default=None)) -> None:
    if not x_admin_code or not secrets.compare_digest(x_admin_code, DEFAULT_ADMIN_CODE):
        raise HTTPException(status_code=403, detail="Admin code required")


def admin_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def active_admin_session(conn: sqlite3.Connection, token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    row = conn.execute(
        """
        SELECT s.*, p.display_name, p.league_id
        FROM admin_sessions s
        JOIN participants p ON p.id = s.participant_id
        WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
        """,
        (admin_token_hash(token), now_iso()),
    ).fetchone()
    return dict(row) if row else None


def record_admin_event(
    conn: sqlite3.Connection,
    action: str,
    league_id: str = DEFAULT_LEAGUE_ID,
    actor: Optional[dict] = None,
    entity_type: str = "",
    entity_id: str = "",
    payload: Optional[dict] = None,
) -> None:
    actor = actor or {}
    conn.execute(
        """
        INSERT INTO admin_events
          (league_id, participant_id, session_id, action, entity_type, entity_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalize_league_id(league_id),
            actor.get("participant_id"),
            actor.get("session_id"),
            action,
            entity_type,
            str(entity_id or ""),
            json.dumps(payload or {}, separators=(",", ":")),
            now_iso(),
        ),
    )


def compact_feedback_context(context: dict) -> str:
    try:
        payload = json.dumps(context or {}, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = "{}"
    if len(payload) > 4000:
        return json.dumps({"truncated": True}, separators=(",", ":"))
    return payload


def feedback_item_payload(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        context = json.loads(item.pop("context_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        context = {}
    item["context"] = context
    item["display_name"] = item.get("display_name") or "Unknown tester"
    return item


def feedback_items_payload(
    conn: sqlite3.Connection,
    league_id: str,
    status: str = "open",
    category: str = "all",
) -> dict:
    league_id = normalize_league_id(league_id)
    where = ["f.league_id = ?"]
    args: list = [league_id]
    if status == "open":
        where.append("f.status IN ('new', 'reviewing')")
    elif status != "all":
        if status not in {"new", "reviewing", "resolved", "wont_fix"}:
            raise HTTPException(status_code=400, detail="Invalid feedback status")
        where.append("f.status = ?")
        args.append(status)
    if category != "all":
        if category not in {"bug", "confusing", "pricing_odds", "trade_flow", "settlement", "idea", "other"}:
            raise HTTPException(status_code=400, detail="Invalid feedback category")
        where.append("f.category = ?")
        args.append(category)
    rows = conn.execute(
        f"""
        SELECT f.*, p.display_name
        FROM feedback_items f
        LEFT JOIN participants p ON p.id = f.participant_id
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE f.status WHEN 'new' THEN 0 WHEN 'reviewing' THEN 1 WHEN 'resolved' THEN 2 ELSE 3 END,
          f.id DESC
        LIMIT 100
        """,
        tuple(args),
    ).fetchall()
    status_counts = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM feedback_items
            WHERE league_id = ?
            GROUP BY status
            """,
            (league_id,),
        ).fetchall()
    }
    category_counts = {
        row["category"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM feedback_items
            WHERE league_id = ?
            GROUP BY category
            """,
            (league_id,),
        ).fetchall()
    }
    return {
        "league": active_league_meta(conn, league_id),
        "feedback": [feedback_item_payload(row) for row in rows],
        "summary": {"statuses": status_counts, "categories": category_counts},
    }


def realtime_revision(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    market_event = conn.execute(
        f"""
        SELECT COALESCE(MAX(me.id), 0) AS value
        FROM market_events me
        JOIN markets m ON m.id = me.market_id
        WHERE m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
        """,
        (league_id, *environments),
    ).fetchone()["value"]
    trade = conn.execute(
        f"""
        SELECT COALESCE(MAX(t.id), 0) AS value
        FROM trades t
        JOIN markets m ON m.id = t.market_id
        WHERE m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
        """,
        (league_id, *environments),
    ).fetchone()["value"]
    model_run = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS value FROM model_runs WHERE league_id = ?",
        (league_id,),
    ).fetchone()["value"]
    job_run = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS value FROM job_runs WHERE league_id = ?",
        (league_id,),
    ).fetchone()["value"]
    fund_entry = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS value FROM fund_ledger_entries WHERE league_id = ?",
        (league_id,),
    ).fetchone()["value"]
    feedback = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS value FROM feedback_items WHERE league_id = ?",
        (league_id,),
    ).fetchone()["value"]
    participant = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS value FROM participants WHERE league_id = ?",
        (league_id,),
    ).fetchone()["value"]
    signature = f"{market_event}:{trade}:{model_run}:{job_run}:{fund_entry}:{feedback}:{participant}"
    return {
        "league_id": league_id,
        "signature": signature,
        "market_event_id": int(market_event or 0),
        "trade_id": int(trade or 0),
        "model_run_id": int(model_run or 0),
        "job_run_id": int(job_run or 0),
        "fund_entry_id": int(fund_entry or 0),
        "feedback_id": int(feedback or 0),
        "participant_id": int(participant or 0),
        "generated_at": now_iso(),
    }


@app.middleware("http")
async def commissioner_session_auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/admin") or path == "/api/admin/session":
        return await call_next(request)

    supplied_code = request.headers.get("X-Admin-Code") or ""
    if supplied_code and secrets.compare_digest(supplied_code, DEFAULT_ADMIN_CODE):
        request.state.admin_actor = {"method": "header", "participant_id": None, "session_id": None}
        return await call_next(request)

    execute_schema()
    with db() as conn:
        session = active_admin_session(conn, request.cookies.get(ADMIN_SESSION_COOKIE))
    if not session:
        return await call_next(request)

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        request_origin = str(request.base_url).rstrip("/")
        if origin and origin.rstrip("/") not in {*ALLOWED_ORIGINS, request_origin}:
            return JSONResponse(status_code=403, content={"detail": "Admin request origin is not allowed"})

    request.state.admin_actor = {
        "method": "session",
        "participant_id": int(session["participant_id"]),
        "session_id": int(session["id"]),
    }
    request.scope["headers"] = [
        (key, value) for key, value in request.scope["headers"] if key.lower() != b"x-admin-code"
    ] + [(b"x-admin-code", DEFAULT_ADMIN_CODE.encode("utf-8"))]
    response = await call_next(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and response.status_code < 400:
        with db() as conn:
            record_admin_event(
                conn,
                f"{request.method.lower()}:{path}",
                league_id=session["league_id"],
                actor=request.state.admin_actor,
            )
    return response


@app.post("/api/admin/session")
def create_admin_session(
    payload: AdminSessionRequest,
    response: Response,
    x_participant_token: Optional[str] = Header(default=None),
) -> dict:
    execute_schema()
    if not secrets.compare_digest(payload.admin_code, DEFAULT_ADMIN_CODE):
        raise HTTPException(status_code=403, detail="Invalid admin code")
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(hours=ADMIN_SESSION_HOURS)
        cursor = conn.execute(
            """
            INSERT INTO admin_sessions
              (participant_id, token_hash, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                participant["id"],
                admin_token_hash(token),
                created_at.isoformat(),
                expires_at.isoformat(),
                created_at.isoformat(),
            ),
        )
        actor = {"participant_id": participant["id"], "session_id": cursor.lastrowid}
        record_admin_event(conn, "session.created", participant["league_id"], actor)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        max_age=ADMIN_SESSION_HOURS * 60 * 60,
        expires=expires_at,
        path="/api/admin",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="strict",
    )
    return {
        "authenticated": True,
        "expires_at": expires_at.isoformat(),
        "participant": {"id": participant["id"], "display_name": participant["display_name"]},
    }


@app.get("/api/admin/session")
def get_admin_session(request: Request) -> dict:
    execute_schema()
    with db() as conn:
        session = active_admin_session(conn, request.cookies.get(ADMIN_SESSION_COOKIE))
    if not session:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "expires_at": session["expires_at"],
        "participant": {"id": session["participant_id"], "display_name": session["display_name"]},
    }


@app.delete("/api/admin/session")
def delete_admin_session(request: Request, response: Response) -> dict:
    execute_schema()
    with db() as conn:
        session = active_admin_session(conn, request.cookies.get(ADMIN_SESSION_COOKIE))
        if session:
            actor = {"participant_id": session["participant_id"], "session_id": session["id"]}
            record_admin_event(conn, "session.revoked", session["league_id"], actor)
            conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE id = ?", (now_iso(), session["id"]))
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/api/admin")
    return {"authenticated": False}


def latest_snapshot(conn: sqlite3.Connection, league_id: str = DEFAULT_LEAGUE_ID) -> Optional[dict]:
    league_id = normalize_league_id(league_id)
    row = conn.execute(
        "SELECT payload_json FROM source_snapshots WHERE source = 'sleeper' AND league_id = ? ORDER BY id DESC LIMIT 1",
        (league_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def active_league_meta(conn: sqlite3.Connection, league_id: str = DEFAULT_LEAGUE_ID) -> dict:
    league_id = normalize_league_id(league_id)
    row = conn.execute(
        "SELECT league_id, payload_json, created_at FROM source_snapshots WHERE source = 'sleeper' AND league_id = ? ORDER BY id DESC LIMIT 1",
        (league_id,),
    ).fetchone()
    if not row:
        return {
            "league_id": league_id,
            "name": "The League" if league_id == DEFAULT_LEAGUE_ID else "Sleeper League",
            "season": "2026",
            "synced_at": None,
        }
    payload = json.loads(row["payload_json"])
    league = payload.get("league") or {}
    rosters = payload.get("rosters") or []
    managers = stored_managers(conn, league_id)
    if not managers:
        managers = manager_records_from_snapshot(payload, league_id)
    return {
        "league_id": row["league_id"],
        "name": league.get("name") or "Sleeper League",
        "season": str(league.get("season") or "2026"),
        "synced_at": row["created_at"],
        "total_rosters": league.get("total_rosters") or len(rosters) or None,
        "roster_count": len(rosters) or league.get("total_rosters"),
        "manager_count": len(managers),
    }


def starter_player_ids(rosters: list[dict]) -> set[str]:
    ids = set()
    for roster in rosters:
        for player_id in roster.get("players") or []:
            ids.add(str(player_id))
    return ids


def roster_manager_ids(roster: dict) -> list[str]:
    ids = []
    for value in [roster.get("owner_id"), *(roster.get("co_owners") or []), *(roster.get("co_owner_ids") or [])]:
        if value and str(value) not in ids:
            ids.append(str(value))
    return ids


def manager_records_from_snapshot(snapshot: dict, league_id: str) -> list[dict]:
    league_id = normalize_league_id(league_id)
    users = snapshot.get("users") or []
    users_by_id = {str(user.get("user_id")): user for user in users if user.get("user_id")}
    roster_ids_by_user: dict[str, list[int]] = {}
    owner_ids = set()
    team_names: dict[str, str] = {}
    for roster in snapshot.get("rosters") or []:
        roster_id = int(roster.get("roster_id") or 0)
        owner_id = roster.get("owner_id")
        if owner_id:
            owner_ids.add(str(owner_id))
        for user_id in roster_manager_ids(roster):
            roster_ids_by_user.setdefault(user_id, []).append(roster_id)
            user = users_by_id.get(user_id) or {}
            team_name = (user.get("metadata") or {}).get("team_name")
            if team_name:
                team_names[user_id] = team_name

    for user_id in users_by_id:
        roster_ids_by_user.setdefault(user_id, [])

    records = []
    for user_id, roster_ids in sorted(roster_ids_by_user.items(), key=lambda item: (min(item[1]) if item[1] else 999, item[0])):
        user = users_by_id.get(user_id) or {"user_id": user_id}
        metadata = user.get("metadata") or {}
        display_name = user.get("display_name") or metadata.get("display_name") or user.get("username") or user_id
        records.append(
            {
                "league_id": league_id,
                "user_id": user_id,
                "username": user.get("username") or "",
                "display_name": display_name,
                "team_name": team_names.get(user_id) or metadata.get("team_name") or display_name,
                "avatar": user.get("avatar") or "",
                "is_owner": 1 if user_id in owner_ids else 0,
                "roster_ids": sorted(set(roster_ids)),
                "raw_json": user,
            }
        )
    return records


def store_manager_records(conn: sqlite3.Connection, managers: list[dict], created_at: str) -> None:
    if not managers:
        return
    league_id = normalize_league_id(managers[0]["league_id"])
    conn.execute("DELETE FROM league_managers WHERE league_id = ?", (league_id,))
    for manager in managers:
        conn.execute(
            """
            INSERT INTO league_managers
              (league_id, user_id, username, display_name, team_name, avatar, is_owner, roster_ids_json, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league_id, user_id) DO UPDATE SET
              username = excluded.username,
              display_name = excluded.display_name,
              team_name = excluded.team_name,
              avatar = excluded.avatar,
              is_owner = excluded.is_owner,
              roster_ids_json = excluded.roster_ids_json,
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                manager["league_id"],
                manager["user_id"],
                manager["username"],
                manager["display_name"],
                manager["team_name"],
                manager["avatar"],
                manager["is_owner"],
                json.dumps(manager["roster_ids"]),
                json.dumps(manager["raw_json"]),
                created_at,
            ),
        )


def sync_snapshot(conn: sqlite3.Connection, snapshot: dict, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    created_at = now_iso()
    users_by_id = {user.get("user_id"): user for user in snapshot.get("users") or []}
    rosters = snapshot.get("rosters") or []
    players_by_id = snapshot.get("players") or {}
    matchups = snapshot.get("matchups") or {}

    conn.execute("DELETE FROM fantasy_teams WHERE league_id = ?", (league_id,))
    conn.execute("DELETE FROM players WHERE league_id = ?", (league_id,))
    conn.execute("DELETE FROM roster_memberships WHERE league_id = ?", (league_id,))

    league = snapshot.get("league") or {}
    season = str(league.get("season") or "")
    league_ingestion = record_ingestion(
        conn,
        provider="sleeper",
        dataset="league_snapshot",
        league_id=league_id,
        season=season,
        week=int((league.get("settings") or {}).get("leg") or 1),
        payload=compact_snapshot(snapshot),
        artifact_dir=RAW_DATA_DIR,
    )
    players_ingestion = record_ingestion(
        conn,
        provider="sleeper",
        dataset="nfl_players",
        league_id="global",
        season=season,
        week=None,
        payload=players_by_id,
        artifact_dir=RAW_DATA_DIR,
    )

    rostered_player_ids = {
        str(player_id)
        for roster in rosters
        for player_id in (roster.get("players") or [])
    }
    player_rows = []
    for player_id in sorted(rostered_player_ids):
        player = players_by_id.get(player_id) or {}
        name = player.get("full_name") or " ".join(
            part for part in [player.get("first_name"), player.get("last_name")] if part
        )
        player_rows.append(
            (
                player_id,
                name or player_id,
                player.get("position") or "UNK",
                player.get("team"),
                player.get("status"),
                created_at,
            )
        )
    conn.executemany(
        """
        INSERT INTO nfl_players (player_id, full_name, position, team, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
          full_name = excluded.full_name,
          position = excluded.position,
          team = excluded.team,
          status = excluded.status,
          updated_at = excluded.updated_at
        """,
        player_rows,
    )

    managers = manager_records_from_snapshot(snapshot, league_id)
    store_manager_records(conn, managers, created_at)

    for roster in rosters:
        roster_id = int(roster["roster_id"])
        user = users_by_id.get(roster.get("owner_id")) or {}
        metadata = user.get("metadata") or {}
        settings = roster.get("settings") or {}
        conn.execute(
            """
            INSERT INTO fantasy_teams
              (league_id, roster_id, owner_id, team_name, display_name, avatar, wins, losses, ties, points_for, points_against, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league_id, roster_id) DO UPDATE SET
              owner_id = excluded.owner_id,
              team_name = excluded.team_name,
              display_name = excluded.display_name,
              avatar = excluded.avatar,
              wins = excluded.wins,
              losses = excluded.losses,
              ties = excluded.ties,
              points_for = excluded.points_for,
              points_against = excluded.points_against,
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                league_id,
                roster_id,
                roster.get("owner_id"),
                metadata.get("team_name") or user.get("display_name") or f"Roster {roster_id}",
                user.get("display_name") or metadata.get("team_name") or f"Roster {roster_id}",
                user.get("avatar"),
                int(settings.get("wins") or 0),
                int(settings.get("losses") or 0),
                int(settings.get("ties") or 0),
                float(settings.get("fpts") or 0) + float(settings.get("fpts_decimal") or 0) / 100,
                float(settings.get("fpts_against") or 0) + float(settings.get("fpts_against_decimal") or 0) / 100,
                json.dumps(roster),
                created_at,
            ),
        )
        for player_id in roster.get("players") or []:
            player = players_by_id.get(str(player_id)) or {}
            name = player.get("full_name") or " ".join(
                part for part in [player.get("first_name"), player.get("last_name")] if part
            )
            conn.execute(
                """
                INSERT INTO players (league_id, player_id, full_name, position, team, roster_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_id, player_id) DO UPDATE SET
                  full_name = excluded.full_name,
                  position = excluded.position,
                  team = excluded.team,
                  roster_id = excluded.roster_id,
                  raw_json = excluded.raw_json,
                  updated_at = excluded.updated_at
                """,
                (
                    league_id,
                    str(player_id),
                    name or str(player_id),
                    player.get("position") or "UNK",
                    player.get("team"),
                    roster_id,
                    json.dumps(player),
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO roster_memberships (league_id, roster_id, player_id, is_starter, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    league_id,
                    roster_id,
                    str(player_id),
                    1 if str(player_id) in {str(value) for value in (roster.get("starters") or [])} else 0,
                    created_at,
                ),
            )

    conn.execute("DELETE FROM source_snapshots WHERE source = 'sleeper' AND league_id = ?", (league_id,))
    conn.execute(
        "INSERT INTO source_snapshots (source, league_id, payload_json, created_at) VALUES ('sleeper', ?, ?, ?)",
        (league_id, json.dumps(compact_snapshot(snapshot), separators=(",", ":")), created_at),
    )
    return {
        "league": {
            "league_id": league_id,
            "name": league.get("name") or "Sleeper League",
            "season": str(league.get("season") or ""),
            "total_rosters": league.get("total_rosters") or len(rosters),
        },
        "teams": len(rosters),
        "players": len(starter_player_ids(rosters)),
        "weeks": len(matchups),
        "managers": managers,
        "ingestion": {
            "league_run_id": league_ingestion["id"],
            "players_run_id": players_ingestion["id"],
            "league_deduplicated": league_ingestion.get("deduplicated", False),
            "players_deduplicated": players_ingestion.get("deduplicated", False),
        },
    }


def create_market(
    conn: sqlite3.Connection,
    league_id: str,
    title: str,
    market_type: str,
    outcome_labels: list[tuple[str, str]],
    close_time: str,
    resolution_source: str,
    resolution_rule: str,
    liquidity_label: str,
    source_key: str,
    *,
    priors: Optional[list[float]] = None,
    model_run_id: Optional[int] = None,
    contract_key: Optional[str] = None,
    contract_version: int = 1,
    origin: str = "manual",
    status: str = "open",
    environment: Optional[str] = None,
    liquidity_override: Optional[float] = None,
) -> Optional[int]:
    league_id = normalize_league_id(league_id)
    existing = conn.execute("SELECT id FROM markets WHERE source_key = ?", (source_key,)).fetchone()
    if existing:
        return None
    normalized_priors = priors or [1.0 / len(outcome_labels)] * len(outcome_labels)
    total_prior = sum(normalized_priors)
    if len(normalized_priors) != len(outcome_labels) or total_prior <= 0 or any(value <= 0 for value in normalized_priors):
        raise ValueError("Market priors must be positive and match the outcomes")
    normalized_priors = [value / total_prior for value in normalized_priors]
    liquidity = float(liquidity_override or liquidity_value(liquidity_label, len(outcome_labels)))
    created_at = now_iso()
    resolved_environment = environment or data_environment()
    resolved_contract_key = contract_key or source_key
    cursor = conn.execute(
        """
        INSERT INTO markets
          (league_id, title, market_type, status, liquidity_label, liquidity, payout, close_time,
           resolution_source, resolution_rule, source_key, created_at, environment, origin,
           visibility, contract_key, contract_version, model_run_id, latest_model_run_id, trade_revision, opened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'public', ?, ?, ?, ?, 0, ?)
        """,
        (
            league_id,
            title,
            market_type,
            status,
            liquidity_label,
            liquidity,
            PAYOUT_CREDITS,
            close_time,
            resolution_source,
            resolution_rule,
            source_key,
            created_at,
            resolved_environment,
            origin,
            resolved_contract_key,
            contract_version,
            model_run_id,
            model_run_id,
            created_at if status == "open" else None,
        ),
    )
    market_id = cursor.lastrowid
    outcome_ids = []
    for index, ((label, source_ref), prior) in enumerate(zip(outcome_labels, normalized_priors)):
        outcome_cursor = conn.execute(
            """
            INSERT INTO outcomes
              (market_id, label, source_ref, quantity, sort_order, prior_probability, model_probability)
            VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (market_id, label, source_ref, index, prior, prior),
        )
        outcome_ids.append(int(outcome_cursor.lastrowid))
    event_cursor = conn.execute(
        """
        INSERT INTO market_events (market_id, event_type, actor_type, revision, payload_json, created_at)
        VALUES (?, ?, 'system', 0, ?, ?)
        """,
        (
            market_id,
            "opened" if status == "open" else "drafted",
            json.dumps({"model_run_id": model_run_id, "contract_key": resolved_contract_key}),
            created_at,
        ),
    )
    for outcome_id, prior in zip(outcome_ids, normalized_priors):
        conn.execute(
            """
            INSERT INTO price_ticks
              (market_id, outcome_id, event_id, market_probability, model_probability, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (market_id, outcome_id, event_cursor.lastrowid, prior, prior, created_at),
        )
    return int(market_id)


def seed_standard_markets(conn: sqlite3.Connection, league_id: str = DEFAULT_LEAGUE_ID, reset_seeded: bool = False) -> dict:
    league_id = normalize_league_id(league_id)
    teams = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM fantasy_teams WHERE league_id = ? ORDER BY roster_id",
            (league_id,),
        ).fetchall()
    ]
    if not teams:
        raise HTTPException(status_code=409, detail="Sync Sleeper before seeding markets")

    latest_model = conn.execute(
        "SELECT id FROM model_runs WHERE league_id = ? AND status = 'succeeded' ORDER BY id DESC LIMIT 1",
        (league_id,),
    ).fetchone()
    if latest_model:
        modeled = originate_modeled_markets(conn, league_id, int(latest_model["id"]))
        return {**modeled, "league": active_league_meta(conn, league_id)}
    if APP_ENV == "production":
        raise HTTPException(status_code=409, detail="Run a valid projection model before publishing markets")

    league_meta = active_league_meta(conn, league_id)
    season = str(league_meta.get("season") or "2026")
    team_outcomes = [(team["team_name"], f"roster:{team['roster_id']}") for team in teams]
    created = []
    seeded = [
        create_market(
            conn,
            league_id,
            f"{season} League Champion",
            "multi",
            team_outcomes,
            "",
            "Sleeper final playoff bracket",
            "The winning outcome is the fantasy team Sleeper or the commissioner records as league champion.",
            "medium-high",
            f"season:{league_id}:{season}:champion",
        ),
    ]
    created.extend([market_id for market_id in seeded if market_id])

    for team in teams:
        market_id = create_market(
            conn,
            league_id,
            f"{team['team_name']} Makes Playoffs",
            "binary",
            [("YES", f"roster:{team['roster_id']}:yes"), ("NO", f"roster:{team['roster_id']}:no")],
            "",
            "Sleeper playoff bracket",
            f"YES wins if {team['team_name']} appears in the official Sleeper winners bracket.",
            "medium",
            f"season:{league_id}:{season}:makes-playoffs:{team['roster_id']}",
        )
        if market_id:
            created.append(market_id)

    snapshot = latest_snapshot(conn, league_id) or {}
    current_week = max(1, int(((snapshot.get("league") or {}).get("settings") or {}).get("leg") or 1))
    for week in [current_week]:
        top_market_id = create_market(
            conn,
            league_id,
            f"Week {week} Top Scoring Team",
            "multi",
            team_outcomes,
            "",
            "Sleeper weekly matchups",
            f"The winning outcome is the team with the highest official Sleeper score in Week {week}.",
            "medium",
            f"season:{league_id}:{season}:week:{week}:top-scoring-team",
        )
        low_market_id = create_market(
            conn,
            league_id,
            f"Week {week} Lowest Scoring Team",
            "multi",
            team_outcomes,
            "",
            "Sleeper weekly matchups",
            f"The winning outcome is the team with the lowest official Sleeper score in Week {week}. Commissioner tiebreakers apply if needed.",
            "medium",
            f"season:{league_id}:{season}:week:{week}:lowest-scoring-team",
        )
        created.extend([market_id for market_id in [top_market_id, low_market_id] if market_id])
    return {"created": len(created), "market_ids": created, "league": league_meta}


def seed_commissioners_cup_markets(conn: sqlite3.Connection, league_id: str = DEFAULT_LEAGUE_ID, reset_seeded: bool = False) -> dict:
    league_id = normalize_league_id(league_id)
    teams = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM fantasy_teams WHERE league_id = ? ORDER BY roster_id",
            (league_id,),
        ).fetchall()
    ]
    if not teams:
        raise HTTPException(status_code=409, detail="Sync Sleeper before seeding Cup markets")
    return {
        "created": 0,
        "market_ids": [],
        "league": active_league_meta(conn, league_id),
        "status": "deferred",
    }

def ingest_projections(
    conn: sqlite3.Connection,
    snapshot: dict,
    projections: dict[str, dict],
    *,
    provider: str = "sleeper",
    source_updated_at: Optional[str] = None,
) -> dict:
    league = snapshot.get("league") or {}
    league_id = normalize_league_id(str(league.get("league_id") or DEFAULT_LEAGUE_ID))
    season = str(league.get("season") or "")
    week = max(1, int((league.get("settings") or {}).get("leg") or 1))
    run = record_ingestion(
        conn,
        provider=provider,
        dataset="weekly_projections",
        league_id=league_id,
        season=season,
        week=week,
        payload=projections,
        artifact_dir=RAW_DATA_DIR,
        source_updated_at=source_updated_at,
    )
    existing = conn.execute(
        "SELECT 1 FROM projection_observations WHERE ingestion_run_id = ? LIMIT 1",
        (run["id"],),
    ).fetchone()
    if not existing:
        scoring = league.get("scoring_settings") or {}
        conn.executemany(
            """
            INSERT INTO projection_observations
              (ingestion_run_id, player_id, season, week, projected_points, stats_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run["id"],
                    str(player_id),
                    season,
                    week,
                    projection_points(stats, scoring),
                    json.dumps(stats, separators=(",", ":")),
                )
                for player_id, stats in projections.items()
                if isinstance(stats, dict)
            ],
        )
    return run


def historical_scores_for_snapshot(snapshot: dict) -> list[float]:
    scores = []
    for entries in (snapshot.get("matchups") or {}).values():
        for entry in entries or []:
            points = entry.get("points")
            if isinstance(points, (int, float)) and points > 0:
                scores.append(float(points))
    return scores


def persist_model_run(
    conn: sqlite3.Connection,
    result: dict,
    ingestion_run_ids: list[int],
) -> dict:
    input_rows = conn.execute(
        f"SELECT id, content_hash FROM ingestion_runs WHERE id IN ({','.join('?' for _ in ingestion_run_ids)}) ORDER BY id",
        ingestion_run_ids,
    ).fetchall()
    input_hash = ":".join(str(row["content_hash"]) for row in input_rows)
    result_hash = model_fingerprint(result)
    existing = conn.execute(
        """
        SELECT * FROM model_runs
        WHERE league_id = ? AND season = ? AND week = ? AND model_version = ? AND input_hash = ?
        """,
        (result["league_id"], result["season"], result["week"], result["model_version"], input_hash),
    ).fetchone()
    if existing:
        model_run_id = int(existing["id"])
    else:
        cursor = conn.execute(
        """
        INSERT INTO model_runs
          (league_id, season, week, model_version, seed, simulation_count, input_hash, result_hash,
           coverage, diagnostics_json, assumptions_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?)
        """,
            (
                result["league_id"],
                result["season"],
                result["week"],
                result["model_version"],
                result["seed"],
                result["simulation_count"],
                input_hash,
                result_hash,
                float(result["diagnostics"]["starter_coverage"]),
                json.dumps(result["diagnostics"], separators=(",", ":")),
                json.dumps(result["assumptions"], separators=(",", ":")),
                now_iso(),
            ),
        )
        model_run_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO model_run_inputs (model_run_id, ingestion_run_id) VALUES (?, ?)",
            [(model_run_id, run_id) for run_id in ingestion_run_ids],
        )
        estimates = []
        for family, probabilities in result["probabilities"].items():
            for roster_id, probability in probabilities.items():
                estimates.append((model_run_id, family, f"roster:{roster_id}", float(probability)))
        conn.executemany(
            """
            INSERT INTO probability_estimates (model_run_id, market_family, subject_ref, probability)
            VALUES (?, ?, ?, ?)
            """,
            estimates,
        )
    conn.executemany(
        """
        INSERT INTO team_score_estimates (model_run_id, roster_id, projected_mean, projected_stdev)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(model_run_id, roster_id) DO NOTHING
        """,
        [
            (model_run_id, int(roster_id), float(values["mean"]), float(values["stdev"]))
            for roster_id, values in result.get("team_strengths", {}).items()
        ],
    )
    return dict(conn.execute("SELECT * FROM model_runs WHERE id = ?", (model_run_id,)).fetchone())


def run_model_for_snapshot(
    conn: sqlite3.Connection,
    snapshot: dict,
    projections: dict[str, dict],
    ingestion_run_ids: list[int],
    simulation_count: int = MODEL_SIMULATIONS,
    historical_scores: Optional[list[float]] = None,
) -> dict:
    projection_run = next(
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM ingestion_runs WHERE id IN ({','.join('?' for _ in ingestion_run_ids)})",
            ingestion_run_ids,
        ).fetchall()
        if row["dataset"] == "weekly_projections"
    )
    result = run_league_model(
        snapshot,
        projections,
        simulation_count=simulation_count,
        historical_scores=historical_scores if historical_scores is not None else historical_scores_for_snapshot(snapshot),
        projection_hash=projection_run["content_hash"],
    )
    result["diagnostics"]["historical_score_count"] = len(
        historical_scores if historical_scores is not None else historical_scores_for_snapshot(snapshot)
    )
    stored = persist_model_run(conn, result, ingestion_run_ids)
    return {"run": stored, "result": result}


def fetch_historical_league_scores(snapshot: dict, max_seasons: int = 3) -> dict:
    league = snapshot.get("league") or {}
    active_slots = lambda slots: [slot for slot in slots if slot not in {"BN", "IR", "TAXI"}]
    nonzero_scoring = lambda settings: {key: value for key, value in settings.items() if float(value or 0) != 0}
    expected_scoring = nonzero_scoring(league.get("scoring_settings") or {})
    expected_slots = active_slots(league.get("roster_positions") or [])
    previous_id = league.get("previous_league_id")
    seasons = []
    scores = []
    for _ in range(max_seasons):
        if not previous_id:
            break
        try:
            previous = SleeperAdapter.get_league(str(previous_id))
        except Exception:
            break
        if (
            nonzero_scoring(previous.get("scoring_settings") or {}) != expected_scoring
            or active_slots(previous.get("roster_positions") or []) != expected_slots
        ):
            previous_id = previous.get("previous_league_id")
            continue
        playoff_week = int((previous.get("settings") or {}).get("playoff_week_start") or 15)
        matchup_payload = {}
        for week in range(1, playoff_week):
            try:
                entries = SleeperAdapter.get_matchups(str(previous_id), week)
            except Exception:
                entries = []
            matchup_payload[str(week)] = entries
            scores.extend(
                float(entry["points"])
                for entry in entries
                if isinstance(entry.get("points"), (int, float)) and float(entry["points"]) > 0
            )
        seasons.append(
            {
                "league_id": str(previous_id),
                "season": str(previous.get("season") or ""),
                "matchups": matchup_payload,
            }
        )
        previous_id = previous.get("previous_league_id")
    return {"seasons": seasons, "scores": scores}


def first_nfl_kickoff(season: int) -> datetime:
    eastern = ZoneInfo("America/New_York")
    september_first = datetime(season, 9, 1, tzinfo=eastern)
    labor_day = september_first + timedelta(days=(7 - september_first.weekday()) % 7)
    return (labor_day + timedelta(days=3)).replace(hour=20, minute=20).astimezone(timezone.utc)


def market_close_time(season: int, week: int) -> str:
    return (first_nfl_kickoff(season) + timedelta(days=7 * (week - 1))).isoformat()


def weekly_contract_schedule(contract_key: Optional[str]) -> Optional[tuple[int, int]]:
    key = str(contract_key or "")
    if ":week:" not in key or not (key.endswith(":week_top") or key.endswith(":week_low")):
        return None
    try:
        prefix, week_family = key.split(":week:", 1)
        return int(prefix.rsplit(":", 1)[1]), int(week_family.split(":", 1)[0])
    except (IndexError, TypeError, ValueError):
        return None


def weekly_settlement_time(season: int, week: int) -> datetime:
    eastern = ZoneInfo("America/New_York")
    kickoff_local = (first_nfl_kickoff(season) + timedelta(days=7 * (week - 1))).astimezone(eastern)
    settlement_day = kickoff_local.date() + timedelta(days=5)
    return datetime(
        settlement_day.year,
        settlement_day.month,
        settlement_day.day,
        1,
        0,
        tzinfo=eastern,
    ).astimezone(timezone.utc)


def upsert_contract_definition(
    conn: sqlite3.Connection,
    contract_key: str,
    family: str,
    source: str,
    rule: str,
    close_policy: str,
) -> None:
    conn.execute(
        """
        INSERT INTO contract_definitions
          (contract_key, market_family, settlement_version, resolution_source, resolution_rule, close_policy, created_at)
        VALUES (?, ?, 'v1', ?, ?, ?, ?)
        ON CONFLICT(contract_key) DO NOTHING
        """,
        (contract_key, family, source, rule, close_policy, now_iso()),
    )


def append_market_event_with_ticks(
    conn: sqlite3.Connection,
    market_id: int,
    event_type: str,
    actor_type: str,
    payload: Optional[dict] = None,
    actor_id: Optional[str] = None,
) -> int:
    market = dict(conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone())
    outcomes = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM outcomes WHERE market_id = ? ORDER BY sort_order, id", (market_id,)
        ).fetchall()
    ]
    revision = int(market.get("trade_revision") or 0) + 1
    created_at = now_iso()
    conn.execute("UPDATE markets SET trade_revision = ? WHERE id = ?", (revision, market_id))
    event_cursor = conn.execute(
        """
        INSERT INTO market_events
          (market_id, event_type, actor_type, actor_id, revision, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (market_id, event_type, actor_type, actor_id, revision, json.dumps(payload or {}), created_at),
    )
    priors = [float(row.get("prior_probability") or 1.0 / len(outcomes)) for row in outcomes]
    prices = lmsr_prices(
        [float(row["quantity"]) for row in outcomes],
        float(market["liquidity"]),
        float(market["payout"]),
        priors,
    )
    conn.executemany(
        """
        INSERT INTO price_ticks
          (market_id, outcome_id, event_id, market_probability, model_probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                market_id,
                row["id"],
                event_cursor.lastrowid,
                price / float(market["payout"]),
                row.get("model_probability"),
                created_at,
            )
            for row, price in zip(outcomes, prices)
        ],
    )
    return revision


def live_week_started(matchups: list[dict]) -> bool:
    for entry in matchups:
        if isinstance(entry.get("points"), (int, float)) and abs(float(entry["points"])) > 0.001:
            return True
        starter_points = entry.get("starters_points")
        if isinstance(starter_points, list) and any(
            isinstance(value, (int, float)) and abs(float(value)) > 0.001 for value in starter_points
        ):
            return True
        player_points = entry.get("players_points")
        if isinstance(player_points, dict) and any(
            isinstance(value, (int, float)) and abs(float(value)) > 0.001 for value in player_points.values()
        ):
            return True
    return False


def starter_progress_from_matchup(entry: dict, expected_mean: float) -> float:
    starter_points = entry.get("starters_points")
    if isinstance(starter_points, list) and starter_points:
        numeric = [float(value) for value in starter_points if isinstance(value, (int, float))]
        if numeric:
            return min(1.0, max(0.0, sum(1 for value in numeric if abs(value) > 0.001) / len(starter_points)))
    points = float(entry.get("points") or 0.0) if isinstance(entry.get("points"), (int, float)) else 0.0
    if points <= 0 or expected_mean <= 0:
        return 0.0
    return min(0.95, max(0.0, points / expected_mean))


def live_starter_projection_context(
    entry: dict,
    player_projections: Optional[dict[str, float]],
    expected_mean: float,
) -> dict:
    starters = [str(player_id) for player_id in entry.get("starters") or [] if player_id not in {None, "", "0"}]
    player_points = entry.get("players_points") if isinstance(entry.get("players_points"), dict) else {}
    starter_points = entry.get("starters_points") if isinstance(entry.get("starters_points"), list) else []
    points = float(entry.get("points") or 0.0) if isinstance(entry.get("points"), (int, float)) else 0.0

    if starters and player_projections:
        projected_by_starter = {
            player_id: max(0.0, float(player_projections.get(player_id) or 0.0))
            for player_id in starters
        }
        projected_total = sum(projected_by_starter.values())
        has_enough_projection_coverage = projected_total >= max(1.0, expected_mean * 0.45)
        if has_enough_projection_coverage:
            scored_starters = set()
            for index, player_id in enumerate(starters):
                value = player_points.get(player_id)
                if not isinstance(value, (int, float)) and index < len(starter_points):
                    value = starter_points[index]
                if isinstance(value, (int, float)) and abs(float(value)) > 0.001:
                    scored_starters.add(player_id)
            remaining_mean = sum(
                projected
                for player_id, projected in projected_by_starter.items()
                if player_id not in scored_starters
            )
            return {
                "actual_score": points,
                "remaining_mean": remaining_mean,
                "progress": min(1.0, max(0.0, 1.0 - (remaining_mean / projected_total if projected_total else 0.0))),
                "starter_count": len(starters),
                "scored_starters": len(scored_starters),
                "projection_basis": "starter_projection",
            }

    progress = starter_progress_from_matchup(entry, expected_mean)
    return {
        "actual_score": points,
        "remaining_mean": max(0.0, expected_mean * (1.0 - progress)),
        "progress": progress,
        "starter_count": len(starters),
        "scored_starters": 0,
        "projection_basis": "roster_progress",
    }


def live_week_score_probabilities(
    *,
    league_id: str,
    season: str,
    week: int,
    model_run_id: int,
    estimates: dict[int, tuple[float, float]],
    matchups: list[dict],
    player_projections: Optional[dict[str, float]] = None,
    simulation_count: int = LIVE_SCORE_SIMULATIONS,
) -> dict:
    if simulation_count < 100:
        raise ValueError("At least 100 simulations are required")
    roster_ids = sorted(estimates)
    matchup_by_roster = {
        int(entry["roster_id"]): entry
        for entry in matchups
        if entry.get("roster_id") is not None
    }
    if not roster_ids or any(roster_id not in matchup_by_roster for roster_id in roster_ids):
        return {"available": False, "reason": "missing_roster_scores"}
    contexts = {
        roster_id: live_starter_projection_context(
            matchup_by_roster[roster_id],
            player_projections,
            estimates[roster_id][0],
        )
        for roster_id in roster_ids
    }
    actual_scores = {roster_id: float(contexts[roster_id]["actual_score"]) for roster_id in roster_ids}
    progress = {roster_id: float(contexts[roster_id]["progress"]) for roster_id in roster_ids}
    projected_remaining = {roster_id: float(contexts[roster_id]["remaining_mean"]) for roster_id in roster_ids}
    top_counts = {roster_id: 0 for roster_id in roster_ids}
    low_counts = {roster_id: 0 for roster_id in roster_ids}
    seed = int(model_fingerprint({
        "league_id": league_id,
        "season": season,
        "week": week,
        "model_run_id": model_run_id,
        "scores": {str(key): round(value, 4) for key, value in actual_scores.items()},
        "progress": {str(key): round(value, 4) for key, value in progress.items()},
        "projected_remaining": {str(key): round(value, 4) for key, value in projected_remaining.items()},
    })[:15], 16)
    rng = random.Random(seed)
    for _ in range(simulation_count):
        simulated = {}
        for roster_id in roster_ids:
            mean, stdev = estimates[roster_id]
            remaining_mean = max(0.0, projected_remaining[roster_id])
            remaining_fraction = min(1.0, remaining_mean / mean) if mean > 0 else 0.0
            if remaining_mean <= 0.001 or remaining_fraction <= 0.001:
                remaining = 0.0
            else:
                remaining_stdev = max(4.0, stdev * math.sqrt(remaining_fraction))
                remaining = max(0.0, rng.gauss(remaining_mean, remaining_stdev))
            simulated[roster_id] = actual_scores[roster_id] + remaining
        top_counts[max(roster_ids, key=lambda roster_id: (simulated[roster_id], -roster_id))] += 1
        low_counts[min(roster_ids, key=lambda roster_id: (simulated[roster_id], roster_id))] += 1
    denominator = simulation_count + 0.5 * len(roster_ids)
    return {
        "available": True,
        "seed": seed,
        "simulation_count": simulation_count,
        "scores": actual_scores,
        "progress": progress,
        "projected_remaining": projected_remaining,
        "starter_context": {
            roster_id: {
                "starter_count": int(contexts[roster_id]["starter_count"]),
                "scored_starters": int(contexts[roster_id]["scored_starters"]),
                "projection_basis": contexts[roster_id]["projection_basis"],
            }
            for roster_id in roster_ids
        },
        "probabilities": {
            "week_top": {roster_id: (count + 0.5) / denominator for roster_id, count in top_counts.items()},
            "week_low": {roster_id: (count + 0.5) / denominator for roster_id, count in low_counts.items()},
        },
    }


def weekly_mark_targets(conn: sqlite3.Connection, league_id: str, season: str, week: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, status, contract_key, latest_model_run_id, model_run_id
        FROM markets
        WHERE league_id = ? AND origin = 'model' AND visibility = 'public'
          AND status IN ('open', 'closed')
          AND contract_key IN (?, ?)
        ORDER BY id
        """,
        (
            normalize_league_id(league_id),
            f"model:{normalize_league_id(league_id)}:{season}:week:{week}:week_top",
            f"model:{normalize_league_id(league_id)}:{season}:week:{week}:week_low",
        ),
    ).fetchall()


def model_player_projections(conn: sqlite3.Connection, model_run_id: int) -> dict[str, float]:
    return {
        str(row["player_id"]): float(row["projected_points"])
        for row in conn.execute(
            """
            SELECT po.player_id, po.projected_points
            FROM model_run_inputs mri
            JOIN ingestion_runs ir ON ir.id = mri.ingestion_run_id
            JOIN projection_observations po ON po.ingestion_run_id = ir.id
            WHERE mri.model_run_id = ? AND ir.dataset = 'weekly_projections'
            """,
            (model_run_id,),
        ).fetchall()
    }


def apply_live_score_mark(
    conn: sqlite3.Connection,
    *,
    league_id: str,
    season: str,
    week: int,
    model_run_id: int,
    ingestion_run_id: Optional[int],
    matchups: list[dict],
    actor_type: str,
    as_of: Optional[datetime] = None,
    simulation_count: int = LIVE_SCORE_SIMULATIONS,
) -> dict:
    estimates = {
        int(row["roster_id"]): (float(row["projected_mean"]), float(row["projected_stdev"]))
        for row in conn.execute(
            "SELECT roster_id, projected_mean, projected_stdev FROM team_score_estimates WHERE model_run_id = ?",
            (model_run_id,),
        ).fetchall()
    }
    player_projections = model_player_projections(conn, model_run_id)
    live = live_week_score_probabilities(
        league_id=league_id,
        season=season,
        week=week,
        model_run_id=model_run_id,
        estimates=estimates,
        matchups=matchups,
        player_projections=player_projections,
        simulation_count=simulation_count,
    )
    if not live.get("available"):
        return {"marked": 0, "closed": 0, "reason": live.get("reason") or "unavailable"}
    started = live_week_started(matchups)
    if not started:
        return {"marked": 0, "closed": 0, "started": False, "reason": "no_live_points"}
    marked = []
    closed = []
    current = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    for market in weekly_mark_targets(conn, league_id, season, week):
        family = automated_resolution_family(market["contract_key"])
        if family not in {"week_top", "week_low"}:
            continue
        probabilities = live["probabilities"][family]
        existing_rows = conn.execute(
            "SELECT id, source_ref, model_probability FROM outcomes WHERE market_id = ?",
            (market["id"],),
        ).fetchall()
        updates = []
        max_delta = 0.0
        for row in existing_rows:
            source_ref = str(row["source_ref"])
            if not source_ref.startswith("roster:") or source_ref.count(":") != 1:
                continue
            roster_id = int(source_ref.split(":", 1)[1])
            probability = float(probabilities.get(roster_id, 0.0))
            previous = float(row["model_probability"] or 0.0)
            max_delta = max(max_delta, abs(probability - previous))
            updates.append((probability, int(row["id"])))
        should_mark = max_delta >= 0.00005
        if market["status"] == "open":
            conn.execute(
                "UPDATE markets SET status = 'closed', closed_at = COALESCE(closed_at, ?) WHERE id = ?",
                (current, market["id"]),
            )
            append_market_event_with_ticks(
                conn,
                int(market["id"]),
                "closed",
                actor_type,
                {
                    "season": season,
                    "week": week,
                    "reason": "sleeper_live_score_started",
                    "ingestion_run_id": ingestion_run_id,
                },
            )
            closed.append(int(market["id"]))
        if not should_mark:
            continue
        conn.executemany("UPDATE outcomes SET model_probability = ? WHERE id = ?", updates)
        append_market_event_with_ticks(
            conn,
            int(market["id"]),
            "live_score_mark",
            actor_type,
            {
                "season": season,
                "week": week,
                "model_run_id": model_run_id,
                "ingestion_run_id": ingestion_run_id,
                "simulation_count": live["simulation_count"],
                "seed": live["seed"],
                "scores": {str(key): round(value, 2) for key, value in live["scores"].items()},
                "progress": {str(key): round(value, 4) for key, value in live["progress"].items()},
                "projected_remaining": {str(key): round(value, 2) for key, value in live["projected_remaining"].items()},
                "starter_context": live["starter_context"],
                "policy": "actual_sleeper_points_plus_unscored_starter_projection",
            },
        )
        marked.append(int(market["id"]))
    return {
        "marked": len(marked),
        "market_ids": marked,
        "closed": len(closed),
        "closed_market_ids": closed,
        "started": True,
        "week": week,
        "season": season,
        "score_count": len(live["scores"]),
    }


def run_live_score_mark_operation(
    league_id: str = DEFAULT_LEAGUE_ID,
    actor_type: str = "scheduler",
    matchups_override: Optional[list[dict]] = None,
    as_of: Optional[datetime] = None,
    simulation_count: int = LIVE_SCORE_SIMULATIONS,
) -> dict:
    league_id = normalize_league_id(league_id)
    with db() as conn:
        model = conn.execute(
            """
            SELECT id, season, week FROM model_runs
            WHERE league_id = ? AND status = 'succeeded'
            ORDER BY id DESC LIMIT 1
            """,
            (league_id,),
        ).fetchone()
        if not model:
            return {"fetched": False, "marked": 0, "closed": 0, "reason": "no_model_run"}
        season = str(model["season"])
        week = int(model["week"])
        targets = weekly_mark_targets(conn, league_id, season, week)
        if not targets:
            return {"fetched": False, "marked": 0, "closed": 0, "reason": "no_active_weekly_markets"}

    if matchups_override is None:
        try:
            matchups = SleeperAdapter.get_matchups(league_id, week)
        except Exception as error:
            with db() as conn:
                record_ingestion_error(
                    conn,
                    provider="sleeper",
                    dataset="live_matchups",
                    league_id=league_id,
                    season=season,
                    week=week,
                    error=str(error),
                )
            raise HTTPException(status_code=502, detail=f"Sleeper live scoring refresh failed: {error}") from error
    else:
        matchups = deepcopy(matchups_override)

    with db() as conn:
        ingestion = record_ingestion(
            conn,
            provider="sleeper",
            dataset="live_matchups",
            league_id=league_id,
            season=season,
            week=week,
            payload=matchups,
            artifact_dir=RAW_DATA_DIR,
        )
        if not ingestion.get("deduplicated"):
            conn.execute(
                """
                INSERT INTO source_snapshots (source, league_id, payload_json, created_at)
                VALUES ('sleeper_live_score', ?, ?, ?)
                """,
                (
                    league_id,
                    json.dumps(
                        {
                            "source": "sleeper",
                            "provenance": "live_score_mark",
                            "league": {"league_id": league_id, "season": season, "settings": {"leg": week}},
                            "matchups": {str(week): matchups},
                        },
                        separators=(",", ":"),
                    ),
                    now_iso(),
                ),
            )
        result = apply_live_score_mark(
            conn,
            league_id=league_id,
            season=season,
            week=week,
            model_run_id=int(model["id"]),
            ingestion_run_id=int(ingestion["id"]) if ingestion.get("id") is not None else None,
            matchups=matchups,
            actor_type=actor_type,
            as_of=as_of,
            simulation_count=simulation_count,
        )
        return {
            "fetched": True,
            "ingestion_run_id": int(ingestion["id"]) if ingestion.get("id") is not None else None,
            "deduplicated": bool(ingestion.get("deduplicated")),
            **result,
        }


def originate_modeled_markets(conn: sqlite3.Connection, league_id: str, model_run_id: Optional[int] = None) -> dict:
    league_id = normalize_league_id(league_id)
    if model_run_id is None:
        model_row = conn.execute(
            "SELECT * FROM model_runs WHERE league_id = ? AND status = 'succeeded' ORDER BY id DESC LIMIT 1",
            (league_id,),
        ).fetchone()
    else:
        model_row = conn.execute(
            "SELECT * FROM model_runs WHERE id = ? AND league_id = ? AND status = 'succeeded'",
            (model_run_id, league_id),
        ).fetchone()
    if not model_row:
        raise HTTPException(status_code=409, detail="Run a valid projection model before publishing markets")
    model = dict(model_row)
    model_created_at = datetime.fromisoformat(str(model["created_at"]).replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - model_created_at.astimezone(timezone.utc) > timedelta(hours=24):
        raise HTTPException(status_code=409, detail="Model inputs are more than 24 hours old; refresh data first")
    if float(model["coverage"]) < 0.95:
        raise HTTPException(status_code=409, detail="Model starter coverage is below 95%")
    teams = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM fantasy_teams WHERE league_id = ? ORDER BY roster_id", (league_id,)
        ).fetchall()
    ]
    estimates = {
        (row["market_family"], row["subject_ref"]): float(row["probability"])
        for row in conn.execute(
            "SELECT market_family, subject_ref, probability FROM probability_estimates WHERE model_run_id = ?",
            (model["id"],),
        ).fetchall()
    }
    season = str(model["season"])
    week = int(model["week"])
    team_outcomes = [(team["team_name"], f"roster:{team['roster_id']}") for team in teams]

    specs = []
    champion_priors = [estimates[("champion", source_ref)] for _, source_ref in team_outcomes]
    specs.append(
        {
            "family": "champion",
            "suffix": "champion",
            "title": f"{season} League Champion",
            "type": "multi",
            "outcomes": team_outcomes,
            "priors": champion_priors,
            "close_week": int(json.loads(model["diagnostics_json"]).get("playoff_week") or 15),
            "source": "Sleeper final playoff bracket",
            "rule": "The official Sleeper league champion wins. If Sleeper voids the season, the contract is refunded.",
            "target_b": 35.0,
        }
    )
    for team in teams:
        subject_ref = f"roster:{team['roster_id']}"
        yes = estimates[("makes_playoffs", subject_ref)]
        specs.append(
            {
                "family": "makes_playoffs",
                "suffix": f"makes-playoffs:{team['roster_id']}",
                "title": f"{team['team_name']} Makes Playoffs",
                "type": "binary",
                "outcomes": [("YES", f"{subject_ref}:yes"), ("NO", f"{subject_ref}:no")],
                "priors": [yes, 1.0 - yes],
                "close_week": int(json.loads(model["diagnostics_json"]).get("playoff_week") or 15),
                "source": "Sleeper playoff bracket",
                "rule": f"YES wins if {team['team_name']} appears in the official Sleeper winners bracket.",
                "target_b": 50.0,
            }
        )
    for family, adjective in (("week_top", "Top"), ("week_low", "Lowest")):
        specs.append(
            {
                "family": family,
                "suffix": f"week:{week}:{family}",
                "title": f"Week {week} {adjective} Scoring Team",
                "type": "multi",
                "outcomes": team_outcomes,
                "priors": [estimates[(family, source_ref)] for _, source_ref in team_outcomes],
                "close_week": week,
                "source": "Official Sleeper weekly scores",
                "rule": f"The team with the {adjective.lower()} official Sleeper score in Week {week} wins. Settlement runs automatically Tuesday at 1:00 AM ET. An exact tie voids and refunds this contract.",
                "target_b": 35.0,
            }
        )

    aggregate_cap = STARTING_BALANCE * max(1, len(teams)) * 0.5
    target_exposure = sum(
        worst_case_subsidy(spec["priors"], spec["target_b"], PAYOUT_CREDITS) for spec in specs
    )
    scale = min(1.0, aggregate_cap / target_exposure) if target_exposure else 1.0
    created = []
    existing = []
    for spec in specs:
        contract_key = f"model:{league_id}:{season}:{spec['suffix']}"
        upsert_contract_definition(
            conn,
            contract_key,
            spec["family"],
            spec["source"],
            spec["rule"],
            "first_nfl_kickoff;settle_tuesday_0100_et" if spec["family"] in {"week_top", "week_low"} else "first_nfl_kickoff",
        )
        source_key = f"{contract_key}:v1"
        market_id = create_market(
            conn,
            league_id,
            spec["title"],
            spec["type"],
            spec["outcomes"],
            market_close_time(int(season), int(spec["close_week"])),
            spec["source"],
            spec["rule"],
            "modeled",
            source_key,
            priors=spec["priors"],
            model_run_id=int(model["id"]),
            contract_key=contract_key,
            origin="model",
            status="open",
            liquidity_override=max(35.0, spec["target_b"] * scale),
        )
        if market_id:
            created.append(market_id)
        else:
            row = conn.execute("SELECT id FROM markets WHERE source_key = ?", (source_key,)).fetchone()
            if row:
                existing_id = int(row["id"])
                existing.append(existing_id)
                current = conn.execute(
                    "SELECT latest_model_run_id FROM markets WHERE id = ?", (existing_id,)
                ).fetchone()
                if int(current["latest_model_run_id"] or 0) != int(model["id"]):
                    for (_, source_ref), probability in zip(spec["outcomes"], spec["priors"]):
                        conn.execute(
                            "UPDATE outcomes SET model_probability = ? WHERE market_id = ? AND source_ref = ?",
                            (probability, existing_id, source_ref),
                        )
                    conn.execute(
                        "UPDATE markets SET latest_model_run_id = ? WHERE id = ?",
                        (model["id"], existing_id),
                    )
                    append_market_event_with_ticks(
                        conn,
                        existing_id,
                        "model_mark",
                        "system",
                        {"model_run_id": model["id"], "opening_prior_unchanged": True},
                    )
    conn.execute("UPDATE model_runs SET published_at = COALESCE(published_at, ?) WHERE id = ?", (now_iso(), model["id"]))
    return {
        "created": len(created),
        "market_ids": created + existing,
        "existing": len(existing),
        "model_run_id": model["id"],
        "aggregate_subsidy_cap": round(aggregate_cap, 2),
        "target_subsidy": round(target_exposure * scale, 2),
        "liquidity_scale": round(scale, 4),
        "week": week,
    }


def load_last_known_projections(
    conn: sqlite3.Connection,
    league_id: str,
    season: str,
    week: int,
    max_age_hours: int = 24,
) -> Optional[tuple[dict, dict[str, dict]]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    run = conn.execute(
        """
        SELECT * FROM ingestion_runs
        WHERE provider IN ('sleeper', 'nflverse') AND dataset = 'weekly_projections'
          AND league_id = ? AND season = ? AND week = ? AND status = 'succeeded'
          AND fetched_at >= ?
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (league_id, season, week, cutoff),
    ).fetchone()
    if not run:
        return None
    rows = conn.execute(
        "SELECT player_id, stats_json FROM projection_observations WHERE ingestion_run_id = ?",
        (run["id"],),
    ).fetchall()
    if not rows:
        return None
    return dict(run), {str(row["player_id"]): json.loads(row["stats_json"]) for row in rows}


def _execute_live_pipeline_unlocked(league_id: str, simulation_count: Optional[int] = None) -> dict:
    league_id = normalize_league_id(league_id)
    try:
        snapshot = SleeperAdapter.build_snapshot(league_id)
        league = snapshot.get("league") or {}
        season = str(league.get("season") or "")
        week = max(1, int((league.get("settings") or {}).get("leg") or 1))
        historical = fetch_historical_league_scores(snapshot)
    except Exception as error:
        with db() as conn:
            record_ingestion_error(
                conn,
                provider="sleeper",
                dataset="launch_pipeline",
                league_id=league_id,
                error=str(error),
            )
        raise HTTPException(status_code=502, detail=f"Sleeper pipeline failed: {error}") from error
    projection_error = None
    try:
        projections = SleeperAdapter.get_projections(season, week)
    except Exception as error:
        projections = None
        projection_error = error
    with db() as conn:
        sync_result = sync_snapshot(conn, snapshot, league_id)
        projection_source = "sleeper_live"
        if projections is not None:
            projection_run = ingest_projections(conn, snapshot, projections)
        else:
            fallback = load_last_known_projections(conn, league_id, season, week)
            if fallback is not None:
                projection_run, projections = fallback
                projection_source = "last_known_good"
            else:
                nflverse_error = None
                try:
                    projections, source_updated_at = NflverseRankingsAdapter.get_projections()
                    projection_run = ingest_projections(
                        conn,
                        snapshot,
                        projections,
                        provider="nflverse",
                        source_updated_at=source_updated_at,
                    )
                    projection_source = "nflverse_rankings"
                except Exception as error:
                    nflverse_error = error
                record_ingestion_error(
                    conn,
                    provider="sleeper",
                    dataset="weekly_projections",
                    league_id=league_id,
                    season=season,
                    week=week,
                    error=str(projection_error),
                )
                if nflverse_error is not None:
                    record_ingestion_error(
                        conn,
                        provider="nflverse",
                        dataset="weekly_projections",
                        league_id=league_id,
                        season=season,
                        week=week,
                        error=str(nflverse_error),
                    )
                    conn.commit()
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Projection refresh failed; no fresh last-known-good or nflverse fallback exists: "
                            f"Sleeper={projection_error}; nflverse={nflverse_error}"
                        ),
                    ) from projection_error
        ingestion_ids = [
            int(sync_result["ingestion"]["league_run_id"]),
            int(sync_result["ingestion"]["players_run_id"]),
            int(projection_run["id"]),
        ]
        if historical["seasons"]:
            historical_run = record_ingestion(
                conn,
                provider="sleeper",
                dataset="historical_matchups",
                league_id=league_id,
                season=season,
                week=None,
                payload=historical["seasons"],
                artifact_dir=RAW_DATA_DIR,
            )
            ingestion_ids.append(int(historical_run["id"]))
        try:
            modeled = run_model_for_snapshot(
                conn,
                snapshot,
                projections,
                ingestion_ids,
                simulation_count or MODEL_SIMULATIONS,
                historical_scores=historical["scores"],
            )
        except ValueError as error:
            conn.commit()
            raise HTTPException(status_code=422, detail=f"Model validation failed: {error}") from error
        markets = originate_modeled_markets(conn, league_id, int(modeled["run"]["id"]))
        market_total = len(markets["market_ids"])
        managers = stored_managers(conn, league_id)
        return {
            "league": sync_result["league"],
            "synced": {
                "teams": sync_result["teams"],
                "players": sync_result["players"],
                "weeks": sync_result["weeks"],
                "managers": len(managers),
                "ingestion": sync_result["ingestion"],
            },
            "ingestion": {
                "projection_run_id": projection_run["id"],
                "projection_source": projection_source,
                "deduplicated": projection_run.get("deduplicated", False),
            },
            "model": {
                "id": modeled["run"]["id"],
                "version": modeled["run"]["model_version"],
                "simulations": modeled["run"]["simulation_count"],
                "coverage": modeled["run"]["coverage"],
                "diagnostics": modeled["result"]["diagnostics"],
            },
            "markets": {**markets, "season": market_total, "cup": 0, "total": market_total},
            "managers": managers,
        }


def execute_live_pipeline(league_id: str, simulation_count: Optional[int] = None) -> dict:
    if not _PIPELINE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A league data pipeline is already running")
    try:
        return _execute_live_pipeline_unlocked(league_id, simulation_count)
    finally:
        _PIPELINE_LOCK.release()


def outcome_image_url(outcome: dict) -> str:
    source_ref = str(outcome.get("source_ref") or "")
    if not source_ref.startswith("player:"):
        return ""
    player_id = source_ref.replace("player:", "", 1)
    if not player_id or not player_id.isdigit():
        return ""
    return f"https://sleepercdn.com/content/nfl/players/{player_id}.jpg"


def market_with_outcomes(conn: sqlite3.Connection, market_id: int, league_id: Optional[str] = None) -> dict:
    if league_id:
        environments = visible_market_environments()
        placeholders = ",".join("?" for _ in environments)
        market = row_to_dict(
            conn.execute(
                f"""
                SELECT * FROM markets
                WHERE id = ? AND league_id = ? AND visibility = 'public'
                  AND environment IN ({placeholders})
                """,
                (market_id, normalize_league_id(league_id), *environments),
            ).fetchone()
        )
    else:
        market = row_to_dict(conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone())
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    outcomes = [dict(row) for row in conn.execute(
        "SELECT * FROM outcomes WHERE market_id = ? ORDER BY sort_order, id", (market_id,)
    ).fetchall()]
    priors = [
        float(row["prior_probability"])
        if row.get("prior_probability") is not None
        else 1.0 / max(1, len(outcomes))
        for row in outcomes
    ]
    prices = lmsr_prices(
        [float(row["quantity"]) for row in outcomes],
        float(market["liquidity"]),
        float(market["payout"]),
        priors,
    )
    for outcome, price in zip(outcomes, prices):
        outcome["price"] = round(price, 4)
        outcome["probability"] = round(price / float(market["payout"]), 4)
        outcome["prior_probability"] = round(
            float(outcome.get("prior_probability") or 1.0 / max(1, len(outcomes))), 4
        )
        outcome["model_probability"] = round(
            float(outcome.get("model_probability") or outcome["prior_probability"]), 4
        )
        outcome["model_edge"] = round(outcome["model_probability"] - outcome["probability"], 4)
        outcome["depth_five_points"] = round(
            shares_to_move_probability(
                [float(row["quantity"]) for row in outcomes],
                outcomes.index(outcome),
                float(market["liquidity"]),
                0.05,
                priors,
            ),
            2,
        )
        outcome["image_url"] = outcome_image_url(outcome)
    market["outcomes"] = outcomes
    market["metrics"] = market_liquidity_metrics(conn, market, priors)
    market["model"] = market_model_meta(conn, market)
    market["fund_prize"] = market_prize(conn, market_id)
    market["fund_payouts"] = market_payouts(conn, market_id)
    weekly_schedule = weekly_contract_schedule(market.get("contract_key"))
    market["settlement_time"] = (
        weekly_settlement_time(*weekly_schedule).isoformat() if weekly_schedule else None
    )
    return market


def market_model_meta(conn: sqlite3.Connection, market: dict) -> dict:
    model_run_id = market.get("latest_model_run_id") or market.get("model_run_id")
    if not model_run_id:
        return {"available": False, "version": None, "updated_at": None, "coverage": None}
    row = conn.execute(
        "SELECT model_version, simulation_count, coverage, created_at, diagnostics_json FROM model_runs WHERE id = ?",
        (model_run_id,),
    ).fetchone()
    if not row:
        return {"available": False, "version": None, "updated_at": None, "coverage": None}
    live_mark = conn.execute(
        """
        SELECT payload_json, created_at FROM market_events
        WHERE market_id = ? AND event_type = 'live_score_mark'
        ORDER BY id DESC LIMIT 1
        """,
        (market["id"],),
    ).fetchone()
    live_payload = json.loads(live_mark["payload_json"]) if live_mark else None
    return {
        "available": True,
        "run_id": model_run_id,
        "version": row["model_version"],
        "simulations": row["simulation_count"],
        "coverage": round(float(row["coverage"]), 4),
        "updated_at": row["created_at"],
        "mark_source": "live_scores" if live_mark else "projection_model",
        "live_score_mark": (
            {
                "updated_at": live_mark["created_at"],
                "week": live_payload.get("week"),
                "score_count": len(live_payload.get("scores") or {}),
                "policy": live_payload.get("policy"),
            }
            if live_mark and isinstance(live_payload, dict)
            else None
        ),
    }


def market_liquidity_metrics(conn: sqlite3.Connection, market: dict, priors: list[float]) -> dict:
    rows = conn.execute(
        """
        SELECT participant_id, side, shares, cash_delta, created_at
        FROM trades WHERE market_id = ? AND is_demo = 0
        """,
        (market["id"],),
    ).fetchall()
    volume = sum(abs(float(row["cash_delta"])) for row in rows)
    net_flow = sum(float(row["shares"]) * (1 if row["side"] == "buy" else -1) for row in rows)
    by_participant: dict[int, float] = {}
    for row in rows:
        participant_id = int(row["participant_id"])
        by_participant[participant_id] = by_participant.get(participant_id, 0.0) + abs(float(row["cash_delta"]))
    concentration = max(by_participant.values(), default=0.0) / volume if volume else 0.0
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    def signed_flow_since(cutoff: str) -> float:
        return sum(
            float(row["shares"]) * (1 if row["side"] == "buy" else -1)
            for row in rows
            if row["created_at"] >= cutoff
        )

    subsidy = worst_case_subsidy(priors, float(market["liquidity"]), float(market["payout"]))
    return {
        "volume": round(volume, 2),
        "net_flow": round(net_flow, 2),
        "net_flow_24h": round(signed_flow_since(cutoff_24h), 2),
        "net_flow_7d": round(signed_flow_since(cutoff_7d), 2),
        "unique_traders": len(by_participant),
        "trader_concentration": round(concentration, 4),
        "house_subsidy_bound": round(subsidy, 2),
    }


def outcome_evidence(conn: sqlite3.Connection, league_id: str, outcome: dict) -> dict:
    source_ref = str(outcome.get("source_ref") or "")
    if source_ref.startswith("roster:"):
        parts = source_ref.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            team = conn.execute(
                """
                SELECT roster_id, team_name, display_name, wins, losses, ties, points_for, points_against
                FROM fantasy_teams
                WHERE league_id = ? AND roster_id = ?
                """,
                (league_id, int(parts[1])),
            ).fetchone()
            if team:
                return {"type": "team", **dict(team)}
    if source_ref.startswith("player:"):
        player_id = source_ref.replace("player:", "", 1)
        player = conn.execute(
            """
            SELECT p.player_id, p.full_name, p.position, p.team, p.roster_id, ft.team_name
            FROM players p
            LEFT JOIN fantasy_teams ft ON ft.league_id = p.league_id AND ft.roster_id = p.roster_id
            WHERE p.league_id = ? AND p.player_id = ?
            """,
            (league_id, player_id),
        ).fetchone()
        if player:
            return {"type": "player", **dict(player)}
    return {"type": "manual"}


def market_price_history(conn: sqlite3.Connection, market_id: int, limit: int = 18) -> list[dict]:
    tick_rows = conn.execute(
        """
        SELECT pt.id, pt.outcome_id, pt.market_probability, pt.model_probability, pt.created_at,
               o.label AS outcome_label, me.event_type, me.payload_json
        FROM price_ticks pt
        JOIN outcomes o ON o.id = pt.outcome_id
        LEFT JOIN market_events me ON me.id = pt.event_id
        WHERE pt.market_id = ?
        ORDER BY pt.id DESC LIMIT ?
        """,
        (market_id, max(limit * 12, 120)),
    ).fetchall()
    if tick_rows:
        return [
            {
                "tick_id": row["id"],
                "outcome_id": row["outcome_id"],
                "outcome_label": row["outcome_label"],
                "price_after": round(float(row["market_probability"]) * PAYOUT_CREDITS, 4),
                "probability": round(float(row["market_probability"]), 6),
                "model_probability": round(float(row["model_probability"]), 6) if row["model_probability"] is not None else None,
                "event_type": row["event_type"] or "mark",
                "created_at": row["created_at"],
            }
            for row in reversed(tick_rows)
        ]
    rows = conn.execute(
        """
        SELECT t.id, t.outcome_id, t.side, t.shares, t.price_before, t.price_after, t.created_at,
               o.label AS outcome_label, p.display_name
        FROM trades t
        JOIN outcomes o ON o.id = t.outcome_id
        JOIN participants p ON p.id = t.participant_id
        WHERE t.market_id = ?
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (market_id, limit),
    ).fetchall()
    points = []
    for row in reversed(rows):
        points.append(
            {
                "trade_id": row["id"],
                "outcome_id": row["outcome_id"],
                "outcome_label": row["outcome_label"],
                "display_name": row["display_name"],
                "side": row["side"],
                "shares": round(float(row["shares"]), 2),
                "price_before": round(float(row["price_before"]), 2),
                "price_after": round(float(row["price_after"]), 2),
                "created_at": row["created_at"],
            }
        )
    return points


def market_resolution_card(conn: sqlite3.Connection, market_id: int, league_id: str) -> dict:
    market = market_with_outcomes(conn, market_id, league_id)
    ranked = sorted(market["outcomes"], key=lambda item: float(item["price"]), reverse=True)
    evidence = []
    for outcome in ranked[:6]:
        evidence.append(
            {
                "outcome_id": outcome["id"],
                "label": outcome["label"],
                "price": outcome["price"],
                "probability": outcome["probability"],
                "source_ref": outcome["source_ref"],
                "image_url": outcome.get("image_url") or "",
                "evidence": outcome_evidence(conn, league_id, outcome),
            }
        )
    return {
        "market_id": market["id"],
        "title": market["title"],
        "status": market["status"],
        "origin": market["origin"],
        "contract_key": market["contract_key"],
        "resolution_source": market["resolution_source"],
        "resolution_rule": market["resolution_rule"],
        "winning_outcome_id": market["winning_outcome_id"],
        "close_time": market["close_time"],
        "created_at": market["created_at"],
        "candidates": evidence,
        "trade_count": conn.execute("SELECT COUNT(*) AS count FROM trades WHERE market_id = ?", (market_id,)).fetchone()["count"],
    }


def participant_position_map(conn: sqlite3.Connection, participant_id: int) -> dict[int, float]:
    rows = conn.execute("SELECT outcome_id, shares FROM positions WHERE participant_id = ?", (participant_id,)).fetchall()
    return {int(row["outcome_id"]): float(row["shares"]) for row in rows}


def cash_and_open_value(conn: sqlite3.Connection, participant: dict) -> tuple[float, float]:
    positions = participant_position_map(conn, int(participant["id"]))
    open_value = 0.0
    league_id = normalize_league_id(participant.get("league_id"))
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    markets = conn.execute(
        f"""
        SELECT id FROM markets
        WHERE league_id = ? AND status IN ('open', 'closed') AND visibility = 'public'
          AND environment IN ({placeholders})
        """,
        (league_id, *environments),
    ).fetchall()
    for market_row in markets:
        market = market_with_outcomes(conn, int(market_row["id"]), league_id)
        for outcome in market["outcomes"]:
            open_value += positions.get(int(outcome["id"]), 0.0) * float(outcome["price"])
    return float(participant["cash"]), open_value


def leaderboard_open_value(
    conn: sqlite3.Connection,
    participant_id: int,
    league_id: str,
    environments: tuple[str, ...],
) -> float:
    placeholders = ",".join("?" for _ in environments)
    positions = conn.execute(
        f"""
        SELECT m.id AS market_id, o.id AS outcome_id,
               SUM(CASE WHEN t.side = 'buy' THEN t.shares ELSE -t.shares END) AS shares
        FROM trades t
        JOIN markets m ON m.id = t.market_id
        JOIN outcomes o ON o.id = t.outcome_id
        WHERE t.participant_id = ? AND t.is_demo = 0
          AND m.league_id = ? AND m.status IN ('open', 'closed')
          AND m.visibility = 'public' AND m.environment IN ({placeholders})
        GROUP BY m.id, o.id
        HAVING ABS(shares) > 0.000001
        """,
        (participant_id, league_id, *environments),
    ).fetchall()
    total = 0.0
    markets: dict[int, dict] = {}
    for position in positions:
        market_id = int(position["market_id"])
        if market_id not in markets:
            markets[market_id] = market_with_outcomes(conn, market_id, league_id)
        outcome = next(
            item for item in markets[market_id]["outcomes"] if int(item["id"]) == int(position["outcome_id"])
        )
        total += float(position["shares"]) * float(outcome["price"])
    return total


def stored_managers(conn: sqlite3.Connection, league_id: str) -> list[dict]:
    league_id = normalize_league_id(league_id)
    managers = []
    for row in conn.execute(
        """
        SELECT user_id, username, display_name, team_name, avatar, is_owner, roster_ids_json, updated_at
        FROM league_managers
        WHERE league_id = ?
        ORDER BY is_owner DESC, team_name COLLATE NOCASE, display_name COLLATE NOCASE
        """,
        (league_id,),
    ).fetchall():
        item = dict(row)
        item["roster_ids"] = json.loads(item.pop("roster_ids_json") or "[]")
        item["is_owner"] = bool(item["is_owner"])
        managers.append(item)
    return managers


def manager_claims(conn: sqlite3.Connection, league_id: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT id, display_name, sleeper_user_id
        FROM participants
        WHERE league_id = ? AND sleeper_user_id IS NOT NULL AND sleeper_user_id != ''
        """,
        (normalize_league_id(league_id),),
    ).fetchall()
    return {
        row["sleeper_user_id"]: {"participant_id": row["id"], "display_name": row["display_name"]}
        for row in rows
    }


def participant_admin_rows(conn: sqlite3.Connection, league_id: str) -> list[dict]:
    league_id = normalize_league_id(league_id)
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    rows = conn.execute(
        f"""
        SELECT p.id, p.display_name, p.sleeper_username, p.sleeper_user_id, p.role, p.cash, p.created_at,
               lm.username AS manager_username, lm.display_name AS manager_display_name,
               lm.team_name AS manager_team_name, lm.avatar AS manager_avatar
        FROM participants p
        LEFT JOIN league_managers lm ON lm.league_id = p.league_id AND lm.user_id = p.sleeper_user_id
        WHERE p.league_id = ? AND p.environment IN ({placeholders})
        ORDER BY p.role DESC, p.display_name COLLATE NOCASE
        """,
        (league_id, *environments),
    ).fetchall()
    participants = []
    for row in rows:
        item = dict(row)
        cash, open_value = cash_and_open_value(conn, item)
        item["cash"] = round(cash, 2)
        item["open_value"] = round(open_value, 2)
        item["net_worth"] = round(cash + open_value, 2)
        item["trade_count"] = conn.execute(
            "SELECT COUNT(*) AS count FROM trades WHERE participant_id = ?",
            (item["id"],),
        ).fetchone()["count"]
        participants.append(item)
    return participants


def invite_rows(conn: sqlite3.Connection, league_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT code, league_id, role, uses_remaining, display_name, sleeper_user_id,
               sleeper_username, participant_id, claimed_at, created_at
        FROM invite_codes
        WHERE league_id = ?
        ORDER BY
          CASE WHEN participant_id IS NULL THEN 0 ELSE 1 END,
          display_name COLLATE NOCASE,
          created_at DESC,
          code
        """,
        (normalize_league_id(league_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def participant_invite_code(conn: sqlite3.Connection, participant_id: int) -> str:
    row = conn.execute(
        """
        SELECT code
        FROM invite_codes
        WHERE participant_id = ?
        ORDER BY claimed_at DESC, created_at DESC, code
        LIMIT 1
        """,
        (participant_id,),
    ).fetchone()
    return str(row["code"]) if row else ""


def participant_payload(conn: sqlite3.Connection, participant: dict) -> dict:
    item = dict(participant)
    item["invite_code"] = participant_invite_code(conn, int(item["id"]))
    return item


def default_fund_settings(league_id: str) -> dict:
    return {
        "league_id": normalize_league_id(league_id),
        "starting_balance": DEFAULT_FUND_BALANCE,
        "trophy_reserve": DEFAULT_TROPHY_RESERVE,
        "cup_reserve": DEFAULT_CUP_RESERVE,
        "draft_reserve": DEFAULT_DRAFT_RESERVE,
        "safety_buffer": DEFAULT_SAFETY_BUFFER,
        "core_pct": DEFAULT_CORE_ALLOCATION,
        "team_pct": DEFAULT_TEAM_ALLOCATION,
        "player_pct": DEFAULT_PLAYER_ALLOCATION,
        "updated_at": now_iso(),
    }


def ensure_fund_settings(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    row = conn.execute("SELECT * FROM fund_settings WHERE league_id = ?", (league_id,)).fetchone()
    if row:
        return dict(row)
    settings = default_fund_settings(league_id)
    conn.execute(
        """
        INSERT INTO fund_settings
          (league_id, starting_balance, trophy_reserve, cup_reserve, draft_reserve, safety_buffer, core_pct, team_pct, player_pct, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings["league_id"],
            settings["starting_balance"],
            settings["trophy_reserve"],
            settings["cup_reserve"],
            settings["draft_reserve"],
            settings["safety_buffer"],
            settings["core_pct"],
            settings["team_pct"],
            settings["player_pct"],
            settings["updated_at"],
        ),
    )
    return settings


def market_category(market: dict) -> str:
    title = str(market.get("title") or "").lower()
    source_key = str(market.get("source_key") or "").lower()
    if "week " in title or ":week:" in source_key:
        return "player"
    if "makes playoffs" in title or "commissioners cup" in title or source_key.startswith("cup:"):
        return "team"
    return "core"


def fund_settings_payload(settings: dict) -> dict:
    reserved_total = (
        float(settings["trophy_reserve"])
        + float(settings["cup_reserve"])
        + float(settings["draft_reserve"])
        + float(settings["safety_buffer"])
    )
    allocation_total = float(settings["core_pct"]) + float(settings["team_pct"]) + float(settings["player_pct"])
    return {
        **settings,
        "reserved_total": round(reserved_total, 2),
        "allocation_total": round(allocation_total, 4),
    }


def category_allocation(conn: sqlite3.Connection, league_id: str, category: str) -> dict:
    league_id = normalize_league_id(league_id)
    allocate_sidequest(conn, league_id)
    row = conn.execute(
        """
        SELECT category, allocated_budget, reserved_exposure, updated_at
        FROM fund_market_allocations
        WHERE league_id = ? AND category = ?
        """,
        (league_id, category),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Unknown market category")
    return dict(row)


def market_prize(conn: sqlite3.Connection, market_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT fmp.*, m.title AS market_title
        FROM fund_market_prizes fmp
        JOIN markets m ON m.id = fmp.market_id
        WHERE fmp.market_id = ?
        """,
        (market_id,),
    ).fetchone()
    return dict(row) if row else None


def market_payouts(conn: sqlite3.Connection, market_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT fp.*, p.display_name
            FROM fund_payouts fp
            JOIN participants p ON p.id = fp.participant_id
            WHERE fp.market_id = ?
            ORDER BY fp.amount DESC, fp.id
            """,
            (market_id,),
        ).fetchall()
    ]


def adjust_reserved_exposure(conn: sqlite3.Connection, league_id: str, category: str, delta: float) -> None:
    if abs(delta) < 0.005:
        return
    allocation = category_allocation(conn, league_id, category)
    next_reserved = max(0.0, float(allocation["reserved_exposure"] or 0) + delta)
    conn.execute(
        """
        UPDATE fund_market_allocations
        SET reserved_exposure = ?, updated_at = ?
        WHERE league_id = ? AND category = ?
        """,
        (round(next_reserved, 2), now_iso(), normalize_league_id(league_id), category),
    )


def assign_market_prize(
    conn: sqlite3.Connection,
    market_id: int,
    league_id: str,
    category: str,
    prize_pool: float,
    payout_mode: str = "proportional_shares",
) -> dict:
    league_id = normalize_league_id(league_id)
    market = market_with_outcomes(conn, market_id, league_id)
    existing = market_prize(conn, market_id)
    current_reserved = 0.0
    if existing and existing["status"] != "paid":
        current_reserved = float(existing["prize_pool"] or 0)
    delta = round(float(prize_pool) - current_reserved, 2)
    summary = fund_summary(conn, league_id)
    available = float(summary["summary"]["available_unreserved"]) + max(0.0, current_reserved)
    allocation = category_allocation(conn, league_id, category)
    category_available = float(allocation["allocated_budget"] or 0) - float(allocation["reserved_exposure"] or 0)
    if existing and existing["category"] == category and existing["status"] != "paid":
        category_available += current_reserved
    if float(prize_pool) > available + 1e-9:
        raise HTTPException(status_code=409, detail="Prize pool exceeds available Side Quest dollars")
    if float(prize_pool) > category_available + 1e-9:
        raise HTTPException(status_code=409, detail="Prize pool exceeds available category budget")
    if existing and existing["status"] == "paid":
        raise HTTPException(status_code=409, detail="Paid market prizes cannot be changed")
    if existing and existing["category"] != category:
        adjust_reserved_exposure(conn, league_id, existing["category"], -current_reserved)
        delta = float(prize_pool)
    conn.execute(
        """
        INSERT INTO fund_market_prizes
          (league_id, market_id, category, prize_pool, payout_mode, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'assigned', ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
          league_id = excluded.league_id,
          category = excluded.category,
          prize_pool = excluded.prize_pool,
          payout_mode = excluded.payout_mode,
          status = CASE
            WHEN fund_market_prizes.status = 'pending' THEN 'pending'
            ELSE 'assigned'
          END,
          updated_at = excluded.updated_at
        """,
        (league_id, market_id, category, round(float(prize_pool), 2), payout_mode, now_iso(), now_iso()),
    )
    adjust_reserved_exposure(conn, league_id, category, delta)
    if market["status"] == "resolved" and market.get("winning_outcome_id"):
        generate_market_payout_plan(conn, market_id)
    return market_prize(conn, market_id) or {}


def generate_market_payout_plan(conn: sqlite3.Connection, market_id: int) -> list[dict]:
    prize = market_prize(conn, market_id)
    if not prize or prize["status"] == "paid":
        return market_payouts(conn, market_id)
    market = market_with_outcomes(conn, market_id, prize["league_id"])
    winning_outcome_id = market.get("winning_outcome_id")
    if market["status"] != "resolved" or not winning_outcome_id:
        return market_payouts(conn, market_id)
    conn.execute("DELETE FROM fund_payouts WHERE market_id = ? AND status = 'pending'", (market_id,))
    winners = conn.execute(
        """
        SELECT pos.participant_id, pos.shares
        FROM positions pos
        JOIN participants p ON p.id = pos.participant_id
        WHERE pos.outcome_id = ? AND pos.shares > 0 AND p.league_id = ?
        ORDER BY pos.shares DESC, pos.participant_id
        """,
        (winning_outcome_id, prize["league_id"]),
    ).fetchall()
    total_shares = sum(float(row["shares"] or 0) for row in winners)
    if total_shares <= 0:
        return []
    remaining = round(float(prize["prize_pool"]), 2)
    for index, row in enumerate(winners):
        if index == len(winners) - 1:
            amount = remaining
        else:
            amount = round(float(prize["prize_pool"]) * float(row["shares"]) / total_shares, 2)
            remaining = round(remaining - amount, 2)
        if amount <= 0:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO fund_payouts
              (league_id, market_id, participant_id, outcome_id, shares, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                prize["league_id"],
                market_id,
                row["participant_id"],
                winning_outcome_id,
                round(float(row["shares"]), 4),
                amount,
                now_iso(),
            ),
        )
    conn.execute(
        "UPDATE fund_market_prizes SET status = 'pending', updated_at = ? WHERE market_id = ?",
        (now_iso(), market_id),
    )
    return market_payouts(conn, market_id)


def mark_market_payouts_paid(conn: sqlite3.Connection, market_id: int) -> dict:
    prize = market_prize(conn, market_id)
    if not prize:
        raise HTTPException(status_code=404, detail="No prize pool assigned to this market")
    if prize["status"] == "paid":
        return {"prize": prize, "payouts": market_payouts(conn, market_id)}
    payouts = market_payouts(conn, market_id)
    pending = [row for row in payouts if row["status"] == "pending"]
    if not pending:
        raise HTTPException(status_code=409, detail="Resolve the market before marking payouts paid")
    total = round(sum(float(row["amount"] or 0) for row in pending), 2)
    source_key = f"market-payout:{market_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO fund_ledger_entries
          (league_id, entry_type, amount, description, source, source_key, created_at)
        VALUES (?, 'expense', ?, ?, 'market_payout', ?, ?)
        """,
        (
            prize["league_id"],
            -abs(total),
            f"Paid market prize: {prize['market_title']}",
            source_key,
            now_iso(),
        ),
    )
    ledger = conn.execute(
        "SELECT id FROM fund_ledger_entries WHERE league_id = ? AND source_key = ?",
        (prize["league_id"], source_key),
    ).fetchone()
    paid_at = now_iso()
    conn.execute(
        """
        UPDATE fund_payouts
        SET status = 'paid', fund_ledger_entry_id = ?, paid_at = ?
        WHERE market_id = ? AND status = 'pending'
        """,
        (ledger["id"] if ledger else None, paid_at, market_id),
    )
    conn.execute(
        "UPDATE fund_market_prizes SET status = 'paid', updated_at = ?, paid_at = ? WHERE market_id = ?",
        (paid_at, paid_at, market_id),
    )
    adjust_reserved_exposure(conn, prize["league_id"], prize["category"], -float(prize["prize_pool"] or 0))
    return {"prize": market_prize(conn, market_id), "payouts": market_payouts(conn, market_id)}


def cancel_market_prize(conn: sqlite3.Connection, market_id: int) -> None:
    prize = market_prize(conn, market_id)
    if not prize or prize["status"] == "paid":
        return
    adjust_reserved_exposure(conn, prize["league_id"], prize["category"], -float(prize["prize_pool"] or 0))
    conn.execute("DELETE FROM fund_payouts WHERE market_id = ? AND status = 'pending'", (market_id,))
    conn.execute(
        "UPDATE fund_market_prizes SET status = 'cancelled', updated_at = ? WHERE market_id = ?",
        (now_iso(), market_id),
    )


def fund_summary(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    settings = ensure_fund_settings(conn, league_id)
    fund_activity = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM fund_ledger_entries
        WHERE league_id = ?
        """,
        (league_id,),
    ).fetchone()
    fee_activity = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM fund_ledger_entries
        WHERE league_id = ? AND source = 'sleeper'
        """,
        (league_id,),
    ).fetchone()
    reserved_total = (
        float(settings["trophy_reserve"])
        + float(settings["cup_reserve"])
        + float(settings["draft_reserve"])
        + float(settings["safety_buffer"])
    )
    season_fee_total = float(fee_activity["total"] or 0)
    fund_total = float(settings["starting_balance"]) + float(fund_activity["total"] or 0)
    committed = conn.execute(
        """
        SELECT COALESCE(SUM(reserved_exposure), 0) AS total
        FROM fund_market_allocations
        WHERE league_id = ?
        """,
        (league_id,),
    ).fetchone()
    committed_market_reserves = float(committed["total"] or 0)
    side_quest_pool = max(0.0, fund_total - reserved_total)
    available_unreserved = max(0.0, side_quest_pool - committed_market_reserves)
    allocation_rows = conn.execute(
        """
        SELECT category, allocated_budget, reserved_exposure, updated_at
        FROM fund_market_allocations
        WHERE league_id = ?
        ORDER BY CASE category WHEN 'core' THEN 0 WHEN 'team' THEN 1 WHEN 'player' THEN 2 ELSE 3 END
        """,
        (league_id,),
    ).fetchall()
    allocation_map = {row["category"]: dict(row) for row in allocation_rows}
    allocations = []
    for category, pct_key, label in [
        ("core", "core_pct", "Core Season Markets"),
        ("team", "team_pct", "Team Markets"),
        ("player", "player_pct", "Weekly Markets"),
    ]:
        stored = allocation_map.get(category) or {}
        default_budget = round(side_quest_pool * float(settings[pct_key]), 2)
        allocations.append(
            {
                "category": category,
                "label": label,
                "pct": float(settings[pct_key]),
                "allocated_budget": round(float(stored.get("allocated_budget", default_budget)), 2),
                "reserved_exposure": round(float(stored.get("reserved_exposure", 0)), 2),
                "market_count": conn.execute(
                    "SELECT COUNT(*) AS count FROM markets WHERE league_id = ?",
                    (league_id,),
                ).fetchone()["count"]
                if category == "all"
                else 0,
            }
        )
    category_counts = {"core": 0, "team": 0, "player": 0}
    for row in conn.execute("SELECT title, source_key FROM markets WHERE league_id = ?", (league_id,)).fetchall():
        category_counts[market_category(dict(row))] += 1
    for allocation in allocations:
        allocation["market_count"] = category_counts.get(allocation["category"], 0)

    teams = []
    for row in conn.execute(
        """
        SELECT ft.roster_id, ft.team_name,
               COALESCE(SUM(CASE WHEN fle.source = 'sleeper' THEN fle.amount ELSE 0 END), 0) AS fee_total,
               COUNT(fle.id) AS entry_count
        FROM fantasy_teams ft
        LEFT JOIN fund_ledger_entries fle ON fle.league_id = ft.league_id AND fle.team_roster_id = ft.roster_id
        WHERE ft.league_id = ?
        GROUP BY ft.roster_id, ft.team_name
        ORDER BY ft.roster_id
        """,
        (league_id,),
    ).fetchall():
        teams.append(
            {
                "roster_id": row["roster_id"],
                "team_name": row["team_name"],
                "fee_total": round(float(row["fee_total"] or 0), 2),
                "entry_count": int(row["entry_count"] or 0),
            }
        )

    ledger = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, entry_type, amount, description, source, source_key, team_roster_id, created_at
            FROM fund_ledger_entries
            WHERE league_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (league_id,),
        ).fetchall()
    ]
    market_prizes = [
        dict(row)
        for row in conn.execute(
            """
            SELECT fmp.id, fmp.market_id, m.title AS market_title, fmp.category, fmp.prize_pool,
                   fmp.payout_mode, fmp.status, fmp.created_at, fmp.updated_at, fmp.paid_at,
                   COALESCE(SUM(fp.amount), 0) AS payout_total,
                   COALESCE(SUM(CASE WHEN fp.status = 'pending' THEN fp.amount ELSE 0 END), 0) AS pending_total,
                   COUNT(fp.id) AS payout_count
            FROM fund_market_prizes fmp
            JOIN markets m ON m.id = fmp.market_id
            LEFT JOIN fund_payouts fp ON fp.market_id = fmp.market_id
            WHERE fmp.league_id = ?
            GROUP BY fmp.id
            ORDER BY CASE fmp.status WHEN 'pending' THEN 0 WHEN 'assigned' THEN 1 WHEN 'paid' THEN 2 WHEN 'cancelled' THEN 3 ELSE 4 END, fmp.id DESC
            LIMIT 30
            """,
            (league_id,),
        ).fetchall()
    ]
    return {
        "league": active_league_meta(conn, league_id),
        "settings": fund_settings_payload(settings),
        "summary": {
            "starting_balance": round(float(settings["starting_balance"]), 2),
            "fund_activity_total": round(float(fund_activity["total"] or 0), 2),
            "fund_total": round(fund_total, 2),
            "season_fee_total": round(season_fee_total, 2),
            "reserved_total": round(reserved_total, 2),
            "side_quest_pool": round(side_quest_pool, 2),
            "committed_market_reserves": round(committed_market_reserves, 2),
            "available_unreserved": round(available_unreserved, 2),
        },
        "allocations": allocations,
        "teams": teams,
        "ledger": ledger,
        "market_prizes": market_prizes,
    }


def allocate_sidequest(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    summary = fund_summary(conn, league_id)
    pool = float(summary["summary"]["side_quest_pool"])
    settings = summary["settings"]
    for category, pct_key in [("core", "core_pct"), ("team", "team_pct"), ("player", "player_pct")]:
        allocated = round(pool * float(settings[pct_key]), 2)
        reserved = min(allocated, float(
            next((item["reserved_exposure"] for item in summary["allocations"] if item["category"] == category), 0)
        ))
        conn.execute(
            """
            INSERT INTO fund_market_allocations (league_id, category, allocated_budget, reserved_exposure, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(league_id, category) DO UPDATE SET
              allocated_budget = excluded.allocated_budget,
              reserved_exposure = MIN(fund_market_allocations.reserved_exposure, excluded.allocated_budget),
              updated_at = excluded.updated_at
            """,
            (league_id, category, allocated, reserved, now_iso()),
        )
    return fund_summary(conn, league_id)


def sync_sleeper_fee_entries(conn: sqlite3.Connection, league_id: str, start_round: int, end_round: int) -> dict:
    league_id = normalize_league_id(league_id)
    ensure_fund_settings(conn, league_id)
    start_round, end_round = min(start_round, end_round), max(start_round, end_round)
    created = 0
    scanned = 0
    errors = []
    for round_number in range(start_round, end_round + 1):
        try:
            transactions = SleeperAdapter.get_transactions(league_id, round_number)
        except Exception as error:
            errors.append({"round": round_number, "error": str(error)})
            continue
        for transaction in transactions or []:
            scanned += 1
            if transaction.get("status") != "complete":
                continue
            transaction_id = str(transaction.get("transaction_id") or "")
            kind = str(transaction.get("type") or "")
            roster_ids = [int(value) for value in transaction.get("roster_ids") or [] if str(value).isdigit()]
            rows_to_insert: list[tuple[int, float, str, str]] = []
            if kind == "trade":
                for roster_id in sorted(set(roster_ids)):
                    rows_to_insert.append((roster_id, 4.0, "trade_fee", f"Trade fee · Week {round_number}"))
            elif kind in ("free_agent", "waiver"):
                adds = transaction.get("adds") or {}
                adds_by_roster: dict[int, int] = {}
                for roster_id in adds.values():
                    if str(roster_id).isdigit():
                        adds_by_roster[int(roster_id)] = adds_by_roster.get(int(roster_id), 0) + 1
                for roster_id, add_count in sorted(adds_by_roster.items()):
                    rows_to_insert.append((roster_id, float(add_count * 2), "add_fee", f"{add_count} add fee{'s' if add_count != 1 else ''} · Week {round_number}"))
            for roster_id, amount, entry_type, description in rows_to_insert:
                source_key = f"sleeper:{transaction_id}:{entry_type}:{roster_id}"
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO fund_ledger_entries
                      (league_id, entry_type, amount, description, source, source_key, team_roster_id, created_at)
                    VALUES (?, ?, ?, ?, 'sleeper', ?, ?, ?)
                    """,
                    (league_id, entry_type, amount, description, source_key, roster_id, now_iso()),
                )
                created += cursor.rowcount
    return {
        "created": created,
        "scanned": scanned,
        "errors": errors,
        "fund": fund_summary(conn, league_id),
    }


def ticker_items(conn: sqlite3.Connection, league_id: str, limit: int = 18) -> list[dict]:
    league_id = normalize_league_id(league_id)
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    rows = conn.execute(
        f"""
        SELECT t.id, t.side, t.shares, t.cash_delta, t.price_before, t.price_after, t.is_demo, t.created_at,
               p.display_name, m.title, o.label AS outcome_label
        FROM trades t
        JOIN participants p ON p.id = t.participant_id
        JOIN markets m ON m.id = t.market_id
        JOIN outcomes o ON o.id = t.outcome_id
        WHERE m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (league_id, *environments, limit),
    ).fetchall()
    items = []
    for row in rows:
        price_move = float(row["price_after"]) - float(row["price_before"])
        items.append(
            {
                "type": "trade",
                "side": row["side"],
                "is_demo": bool(row["is_demo"]),
                "headline": f"{row['display_name']} {row['side'].upper()} {row['outcome_label']}",
                "detail": f"{float(row['shares']):.2f} sh in {row['title']}",
                "price_before": round(float(row["price_before"]), 2),
                "price_after": round(float(row["price_after"]), 2),
                "price_move": round(price_move, 2),
                "cash_delta": round(float(row["cash_delta"]), 2),
                "created_at": row["created_at"],
            }
        )
    if items:
        return items

    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    markets = conn.execute(
        f"""
        SELECT id, title
        FROM markets
        WHERE league_id = ? AND status = 'open' AND visibility = 'public'
          AND environment IN ({placeholders})
        ORDER BY id DESC
        LIMIT ?
        """,
        (league_id, *environments, min(limit, 8)),
    ).fetchall()
    for row in markets:
        market = market_with_outcomes(conn, int(row["id"]), league_id)
        leader = max(market["outcomes"], key=lambda outcome: float(outcome["price"]), default=None)
        if leader:
            items.append(
                {
                    "type": "market",
                    "side": "watch",
                    "headline": f"{leader['label']} leads",
                    "detail": f"{market['title']} at {round(float(leader['probability']) * 100)}%",
                    "price_before": None,
                    "price_after": round(float(leader["price"]), 2),
                    "price_move": 0,
                    "cash_delta": 0,
                    "created_at": market["created_at"],
                }
            )
    return items


def operational_metrics(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    ingestion = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ir.* FROM ingestion_runs ir
            JOIN (
              SELECT dataset, MAX(id) AS id FROM ingestion_runs
              WHERE league_id IN (?, 'global') GROUP BY dataset
            ) latest ON latest.id = ir.id
            ORDER BY ir.dataset
            """,
            (league_id,),
        ).fetchall()
    ]
    model = conn.execute(
        "SELECT * FROM model_runs WHERE league_id = ? ORDER BY id DESC LIMIT 1", (league_id,)
    ).fetchone()
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    trades = conn.execute(
        f"""
        SELECT t.participant_id, ABS(t.cash_delta) AS volume
        FROM trades t JOIN markets m ON m.id = t.market_id
        WHERE m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
          AND t.is_demo = 0
        """,
        (league_id, *environments),
    ).fetchall()
    total_volume = sum(float(row["volume"]) for row in trades)
    participant_volume: dict[int, float] = {}
    for row in trades:
        participant_volume[int(row["participant_id"])] = participant_volume.get(int(row["participant_id"]), 0.0) + float(row["volume"])

    brier_scores = []
    log_losses = []
    score_errors = []
    snapshot = latest_snapshot(conn, league_id) or {}
    actual_scores = {
        (int(week), int(entry["roster_id"])): float(entry["points"])
        for week, entries in (snapshot.get("matchups") or {}).items()
        for entry in entries or []
        if entry.get("roster_id") is not None
        and isinstance(entry.get("points"), (int, float))
        and float(entry["points"]) > 0
    }
    score_rows = conn.execute(
        """
        SELECT mr.week, tse.roster_id, tse.projected_mean
        FROM team_score_estimates tse
        JOIN model_runs mr ON mr.id = tse.model_run_id
        JOIN (
          SELECT season, week, MAX(id) AS id FROM model_runs
          WHERE league_id = ? AND status = 'succeeded' GROUP BY season, week
        ) latest ON latest.id = mr.id
        """,
        (league_id,),
    ).fetchall()
    for row in score_rows:
        actual = actual_scores.get((int(row["week"]), int(row["roster_id"])))
        if actual is not None:
            score_errors.append(abs(float(row["projected_mean"]) - actual))
    resolved = conn.execute(
        """
        SELECT id, winning_outcome_id FROM markets
        WHERE league_id = ? AND origin = 'model' AND status = 'resolved' AND winning_outcome_id IS NOT NULL
        """,
        (league_id,),
    ).fetchall()
    for market in resolved:
        outcomes = conn.execute(
            "SELECT id, prior_probability FROM outcomes WHERE market_id = ?", (market["id"],)
        ).fetchall()
        if not outcomes:
            continue
        brier_scores.append(
            sum(
                (float(outcome["prior_probability"] or 0) - (1.0 if outcome["id"] == market["winning_outcome_id"] else 0.0)) ** 2
                for outcome in outcomes
            )
            / len(outcomes)
        )
        winning = next(outcome for outcome in outcomes if outcome["id"] == market["winning_outcome_id"])
        log_losses.append(-math.log(max(1e-9, float(winning["prior_probability"] or 0))))

    market_counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            f"""
            SELECT status, COUNT(*) AS count FROM markets
            WHERE league_id = ? AND visibility = 'public' AND environment IN ({placeholders})
            GROUP BY status
            """,
            (league_id, *environments),
        ).fetchall()
    }
    resolution = conn.execute(
        """
        SELECT AVG((julianday(resolved_at) - julianday(closed_at)) * 24.0) AS avg_hours
        FROM markets WHERE league_id = ? AND resolved_at IS NOT NULL AND closed_at IS NOT NULL
        """,
        (league_id,),
    ).fetchone()
    return {
        "generated_at": now_iso(),
        "data": {
            "latest_runs": ingestion,
            "failed_runs": conn.execute(
                "SELECT COUNT(*) AS count FROM ingestion_runs WHERE league_id IN (?, 'global') AND status = 'failed'",
                (league_id,),
            ).fetchone()["count"],
        },
        "model": {
            "run_id": model["id"] if model else None,
            "version": model["model_version"] if model else None,
            "coverage": round(float(model["coverage"]), 4) if model else None,
            "simulation_count": model["simulation_count"] if model else None,
            "calibration_samples": len(brier_scores),
            "score_samples": len(score_errors),
            "score_mae": round(sum(score_errors) / len(score_errors), 4) if score_errors else None,
            "brier_score": round(sum(brier_scores) / len(brier_scores), 6) if brier_scores else None,
            "log_loss": round(sum(log_losses) / len(log_losses), 6) if log_losses else None,
        },
        "exchange": {
            "markets": market_counts,
            "unique_traders": len(participant_volume),
            "turnover": round(total_volume, 2),
            "trader_concentration": round(max(participant_volume.values(), default=0.0) / total_volume, 4) if total_volume else 0.0,
            "average_resolution_hours": round(float(resolution["avg_hours"]), 2) if resolution["avg_hours"] is not None else None,
        },
    }


def admin_overview_payload(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    latest_runs = {
        row["dataset"]: dict(row)
        for row in conn.execute(
            """
            SELECT ir.* FROM ingestion_runs ir
            JOIN (
              SELECT dataset, MAX(id) AS id FROM ingestion_runs
              WHERE league_id IN (?, 'global') GROUP BY dataset
            ) latest ON latest.id = ir.id
            """,
            (league_id,),
        ).fetchall()
    }
    model_row = conn.execute(
        "SELECT * FROM model_runs WHERE league_id = ? ORDER BY id DESC LIMIT 1", (league_id,)
    ).fetchone()
    model = dict(model_row) if model_row else None
    if model:
        model["diagnostics"] = json.loads(model.pop("diagnostics_json"))
        model["assumptions"] = json.loads(model.pop("assumptions_json"))
    market_counts = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            f"""
            SELECT status, COUNT(*) AS count FROM markets
            WHERE league_id = ? AND visibility = 'public' AND environment IN ({placeholders})
            GROUP BY status
            """,
            (league_id, *environments),
        ).fetchall()
    }
    next_close = conn.execute(
        f"""
        SELECT id, title, close_time FROM markets
        WHERE league_id = ? AND status = 'open' AND visibility = 'public'
          AND environment IN ({placeholders}) AND close_time != ''
        ORDER BY close_time LIMIT 1
        """,
        (league_id, *environments),
    ).fetchone()
    participants = participant_admin_rows(conn, league_id)
    managers = stored_managers(conn, league_id)
    invites = invite_rows(conn, league_id)
    projection_run = latest_runs.get("weekly_projections")
    now = datetime.now(timezone.utc)

    def age_hours(timestamp: Optional[str]) -> Optional[float]:
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600)
        except (TypeError, ValueError):
            return None

    projection_age = age_hours(projection_run.get("fetched_at") if projection_run else None)
    model_age = age_hours(model.get("created_at") if model else None)
    managed_database = using_remote_database()
    backup_files = [] if managed_database else sorted(BACKUP_DIR.glob("market-*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
    latest_backup = backup_files[0] if backup_files else None
    backup_age = (
        max(0.0, (now.timestamp() - latest_backup.stat().st_mtime) / 3600) if latest_backup else None
    )
    warnings = []

    def warn(code: str, severity: str, title: str, detail: str, action: str) -> None:
        warnings.append({"code": code, "severity": severity, "title": title, "detail": detail, "action": action})

    if not projection_run or projection_run.get("status") != "succeeded":
        warn("projection_missing", "blocked", "Projection feed unavailable", "No successful weekly projection run is available.", "Run pipeline")
    elif projection_age is None or projection_age > 24:
        warn("projection_stale", "blocked", "Projection data is stale", f"The latest projection run is {projection_age:.1f} hours old.", "Run pipeline")
    if not model:
        warn("model_missing", "blocked", "No model run", "Contracts cannot be published without a validated model.", "Run pipeline")
    elif float(model.get("coverage") or 0) < 0.95:
        warn("coverage_low", "blocked", "Starter coverage below 95%", f"Current coverage is {float(model.get('coverage') or 0):.1%}.", "Review projections")
    if not market_counts.get("open"):
        warn("markets_closed", "attention", "No open markets", "There are no contracts currently accepting trades.", "Review publication")
    if market_counts.get("closed"):
        warn("resolution_queue", "attention", "Markets await settlement", f"{market_counts['closed']} closed contracts need confirmation or automatic settlement.", "Open resolution queue")
    unlinked = sum(1 for participant in participants if not participant.get("sleeper_user_id"))
    if unlinked:
        warn("identity_unlinked", "attention", "Participant identities need review", f"{unlinked} participant accounts are not linked to Sleeper managers.", "Open people")
    if not managers:
        warn("managers_missing", "attention", "Manager directory is empty", "Sleeper managers have not been loaded for this league.", "Refresh data")
    if not managed_database and (backup_age is None or backup_age > 24):
        detail = "No database backup was found." if backup_age is None else f"The newest backup is {backup_age:.1f} hours old."
        warn("backup_stale", "attention", "Backup required", detail, "Create backup")
    failed_runs = conn.execute(
        "SELECT COUNT(*) AS count FROM ingestion_runs WHERE league_id IN (?, 'global') AND status = 'failed'",
        (league_id,),
    ).fetchone()["count"]
    if failed_runs:
        warn("ingestion_failures", "attention", "Ingestion failures recorded", f"{failed_runs} failed ingestion runs remain in the audit log.", "Inspect data health")
    readiness = "blocked" if any(item["severity"] == "blocked" for item in warnings) else "attention" if warnings else "ready"
    return {
        "generated_at": now_iso(),
        "readiness": readiness,
        "pipeline_running": _PIPELINE_LOCK.locked(),
        "league": active_league_meta(conn, league_id),
        "data": {
            "projection_source": projection_run.get("provider") if projection_run else None,
            "projection_updated_at": projection_run.get("fetched_at") if projection_run else None,
            "projection_age_hours": round(projection_age, 2) if projection_age is not None else None,
            "failed_runs": int(failed_runs),
        },
        "model": {
            "id": model.get("id") if model else None,
            "version": model.get("model_version") if model else None,
            "coverage": float(model.get("coverage") or 0) if model else None,
            "simulation_count": int(model.get("simulation_count") or 0) if model else None,
            "updated_at": model.get("created_at") if model else None,
            "age_hours": round(model_age, 2) if model_age is not None else None,
        },
        "markets": {
            "counts": market_counts,
            "next_close": dict(next_close) if next_close else None,
        },
        "people": {
            "managers": len(managers),
            "participants": len(participants),
            "linked": len(participants) - unlinked,
            "unlinked": unlinked,
            "invites": len(invites),
        },
        "backup": {
            "managed_by": "turso/libSQL" if managed_database else None,
            "path": str(latest_backup) if latest_backup else None,
            "created_at": datetime.fromtimestamp(latest_backup.stat().st_mtime, timezone.utc).isoformat() if latest_backup else None,
            "age_hours": round(backup_age, 2) if backup_age is not None else None,
        },
        "warnings": warnings,
    }


def admin_request_actor(request: Request) -> dict:
    return getattr(
        request.state,
        "admin_actor",
        {"method": "header", "participant_id": None, "session_id": None},
    )


def run_tracked_job(
    league_id: str,
    job_type: str,
    triggered_by: str,
    operation,
) -> dict:
    league_id = normalize_league_id(league_id)
    started_at = now_iso()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_runs (league_id, job_type, status, triggered_by, started_at)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (league_id, job_type, triggered_by, started_at),
        )
        run_id = int(cursor.lastrowid)
    try:
        result = operation()
    except BaseException as error:
        with db() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
                (now_iso(), str(error)[:2000], run_id),
            )
        raise
    with db() as conn:
        conn.execute(
            """
            UPDATE job_runs SET status = 'succeeded', completed_at = ?, result_json = ?
            WHERE id = ?
            """,
            (now_iso(), json.dumps(result, default=str, separators=(",", ":")), run_id),
        )
    return result


def admin_dashboard_payload(conn: sqlite3.Connection, league_id: str) -> dict:
    league_id = normalize_league_id(league_id)
    overview = admin_overview_payload(conn, league_id)
    now = datetime.now(timezone.utc)
    actions = []

    def parsed(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def add_action(
        action_id: str,
        action_type: str,
        priority: int,
        severity: str,
        title: str,
        detail: str,
        action_label: str,
        payload: Optional[dict] = None,
    ) -> None:
        actions.append({
            "id": action_id,
            "type": action_type,
            "priority": priority,
            "severity": severity,
            "title": title,
            "detail": detail,
            "action_label": action_label,
            "payload": payload or {},
        })

    recent_job_rows = conn.execute(
        "SELECT * FROM job_runs WHERE league_id = ? ORDER BY id DESC LIMIT 100", (league_id,)
    ).fetchall()
    automation_config = {
        "pipeline": {
            "interval_hours": 12.0 if SCHEDULE_PIPELINE else 24.0,
            "health_hours": 24.0,
            "running_hours": AUTOMATION_RUNNING_TIMEOUT_HOURS,
            "label": "Data pipeline",
            "severity": "blocked",
        },
        "live_scores": {
            "interval_hours": LIVE_SCORE_MARK_INTERVAL_HOURS,
            "health_hours": max(LIVE_SCORE_MARK_INTERVAL_HOURS, AUTOMATION_DASHBOARD_GRACE_HOURS),
            "running_hours": AUTOMATION_RUNNING_TIMEOUT_HOURS,
            "label": "Live scoring marks",
            "severity": "attention",
        },
        "lifecycle": {
            "interval_hours": 0.25,
            "health_hours": max(0.25, AUTOMATION_DASHBOARD_GRACE_HOURS),
            "running_hours": AUTOMATION_RUNNING_TIMEOUT_HOURS,
            "label": "Market lifecycle",
            "severity": "attention",
        },
        "backup": {
            "interval_hours": 24.0,
            "health_hours": 24.0,
            "running_hours": AUTOMATION_RUNNING_TIMEOUT_HOURS,
            "label": "Database backup",
            "severity": "attention",
        },
    }
    latest_jobs = {}
    latest_success = {}
    for job_type in automation_config:
        latest = conn.execute(
            """
            SELECT * FROM job_runs
            WHERE league_id = ? AND job_type = ?
            ORDER BY id DESC LIMIT 1
            """,
            (league_id, job_type),
        ).fetchone()
        successful = conn.execute(
            """
            SELECT * FROM job_runs
            WHERE league_id = ? AND job_type = ? AND status = 'succeeded'
            ORDER BY id DESC LIMIT 1
            """,
            (league_id, job_type),
        ).fetchone()
        if latest:
            latest_jobs[job_type] = dict(latest)
        if successful:
            latest_success[job_type] = dict(successful)

    automation = {}
    for job_type, config in automation_config.items():
        latest = latest_jobs.get(job_type)
        successful = latest_success.get(job_type)
        fallback_at = None
        if job_type == "pipeline":
            fallback_at = overview["model"].get("updated_at")
        elif job_type == "backup":
            fallback_at = overview["backup"].get("created_at")
        last_success_at = (successful or {}).get("completed_at") or fallback_at
        success_time = parsed(last_success_at)
        age_hours = (now - success_time).total_seconds() / 3600 if success_time else None
        started_time = parsed((latest or {}).get("started_at"))
        running_age_hours = (now - started_time).total_seconds() / 3600 if started_time else None
        state = "healthy"
        state_reason = ""
        if latest and latest["status"] == "running":
            if running_age_hours is not None and running_age_hours > config["running_hours"]:
                state = "failed"
                state_reason = (
                    f"The latest attempt has been running for {running_age_hours:.1f} hours "
                    "and may have been interrupted."
                )
            else:
                state = "running"
        elif latest and latest["status"] == "failed" and (not success_time or parsed(latest["started_at"]) > success_time):
            state = "failed"
        elif age_hours is None:
            state = "unknown"
        elif age_hours > config["health_hours"]:
            state = "stale"
        automation[job_type] = {
            "job_type": job_type,
            "label": config["label"],
            "state": state,
            "last_success_at": last_success_at,
            "last_attempt_at": (latest or {}).get("started_at"),
            "next_due_at": (
                (success_time + timedelta(hours=config["interval_hours"])).isoformat() if success_time else None
            ),
            "error": state_reason or (latest or {}).get("error") or "",
        }
        if state in {"failed", "stale"} or (state == "unknown" and job_type != "lifecycle"):
            reason = (
                state_reason or (latest or {}).get("error")
                if state == "failed"
                else f"No successful run has been recorded in the last {config['health_hours']:g} hours."
            )
            add_action(
                f"job:{job_type}",
                "job_retry",
                10 if job_type == "pipeline" else 20,
                config["severity"],
                f"{config['label']} needs attention",
                reason or "The latest attempt failed.",
                "Retry",
                {"job_type": job_type},
            )

    environments = visible_market_environments()
    placeholders = ",".join("?" for _ in environments)
    closed = conn.execute(
        f"""
        SELECT id FROM markets
        WHERE league_id = ? AND status = 'closed' AND visibility = 'public'
          AND environment IN ({placeholders})
        ORDER BY closed_at, id
        """,
        (league_id, *environments),
    ).fetchall()
    for row in closed:
        card = market_resolution_card(conn, int(row["id"]), league_id)
        if card["origin"] == "model" and automated_resolution_family(card["contract_key"]):
            continue
        add_action(
            f"resolve:{row['id']}",
            "market_resolution",
            30,
            "attention",
            card["title"],
            f"Trading is closed · {card['trade_count']} trades",
            "Resolve",
            {"market": card},
        )

    payout_rows = conn.execute(
        """
        SELECT fp.market_id, m.title, COUNT(*) AS participant_count, SUM(fp.amount) AS total
        FROM fund_payouts fp JOIN markets m ON m.id = fp.market_id
        WHERE fp.league_id = ? AND fp.status = 'pending'
        GROUP BY fp.market_id, m.title ORDER BY fp.market_id
        """,
        (league_id,),
    ).fetchall()
    for row in payout_rows:
        add_action(
            f"payout:{row['market_id']}",
            "fund_payout",
            40,
            "attention",
            f"Confirm payout for {row['title']}",
            f"{row['participant_count']} recipients · ${float(row['total'] or 0):,.2f}",
            "Review",
            dict(row),
        )

    feedback_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM feedback_items
        WHERE league_id = ? AND status IN ('new', 'reviewing')
        """,
        (league_id,),
    ).fetchone()["count"]
    if feedback_count:
        add_action(
            "feedback:open",
            "feedback_review",
            45,
            "attention",
            "Review beta feedback",
            f"{feedback_count} tester note{'s' if feedback_count != 1 else ''} waiting for triage",
            "Open",
            {"settings_tab": "feedback"},
        )

    participants = participant_admin_rows(conn, league_id)
    linked_usernames = {
        str(participant.get("sleeper_username") or "").strip().casefold()
        for participant in participants
        if participant.get("sleeper_user_id") and str(participant.get("sleeper_username") or "").strip()
    }
    queued_identities = set()
    for participant in participants:
        if participant.get("sleeper_user_id"):
            continue
        username = str(participant.get("sleeper_username") or "").strip().casefold()
        if username and username in linked_usernames:
            continue
        identity_key = f"username:{username}" if username else f"participant:{participant['id']}"
        if identity_key in queued_identities:
            continue
        queued_identities.add(identity_key)
        add_action(
            f"identity:{participant['id']}",
            "participant_link",
            50,
            "attention",
            f"Link {participant['display_name']} to Sleeper",
            "This account is not connected to a league manager.",
            "Link",
            {"participant": participant},
        )

    ignored_warning_codes = {
        "projection_missing", "projection_stale", "model_missing", "backup_stale",
        "resolution_queue", "identity_unlinked", "managers_missing",
    }
    for warning in overview["warnings"]:
        if warning["code"] in ignored_warning_codes:
            continue
        add_action(
            f"warning:{warning['code']}",
            "settings_warning",
            60,
            warning["severity"],
            warning["title"],
            warning["detail"],
            "Review settings",
            {"settings_tab": "system"},
        )

    actions.sort(key=lambda item: (item["priority"], item["id"]))
    recent_events = [
        {**dict(row), "payload": json.loads(row["payload_json"] or "{}")}
        for row in conn.execute(
            "SELECT * FROM admin_events WHERE league_id = ? ORDER BY id DESC LIMIT 20", (league_id,)
        ).fetchall()
    ]
    return {
        "generated_at": now_iso(),
        "all_clear": len(actions) == 0,
        "summary": "Nothing needs your attention" if not actions else f"{len(actions)} item{'s' if len(actions) != 1 else ''} need attention",
        "league": overview["league"],
        "actions": actions,
        "automation": automation,
        "managers": stored_managers(conn, league_id),
        "job_history": [dict(row) for row in recent_job_rows[:20]],
        "recent_activity": recent_events,
        "environment": APP_ENV,
    }


@app.get("/api/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "environment": APP_ENV}


@app.post("/api/auth/join")
def join(payload: JoinRequest) -> dict:
    execute_schema()
    raw_code = (payload.invite_code or "").strip()
    requested_code = invite_code_slug(raw_code)
    with db() as conn:
        invite = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (requested_code,)).fetchone()
        if not invite and raw_code and raw_code != requested_code:
            invite = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (raw_code,)).fetchone()
        if not invite:
            raise HTTPException(status_code=403, detail="Invalid invite code")
        invite_code = invite["code"]
        personal_invite = invite_is_personal(invite)
        league_id = normalize_league_id(invite["league_id"] if personal_invite else payload.league_id)
        if personal_invite and invite["participant_id"]:
            participant = conn.execute("SELECT * FROM participants WHERE id = ?", (invite["participant_id"],)).fetchone()
            if participant:
                return {
                    "token": participant["token"],
                    "participant": {
                        "id": participant["id"],
                        "league_id": participant["league_id"],
                        "display_name": participant["display_name"],
                        "invite_code": invite_code,
                        "role": participant["role"],
                        "cash": float(participant["cash"]),
                    },
                    "returning": True,
                }
        if invite["uses_remaining"] is not None and invite["uses_remaining"] <= 0:
            raise HTTPException(status_code=403, detail="Invite code has no uses remaining")
        display_name = (invite["display_name"] or payload.display_name or "").strip()
        if not display_name:
            if invite["sleeper_username"]:
                display_name = str(invite["sleeper_username"]).strip()
            else:
                display_name = display_name_from_code(invite_code)
        if len(display_name) < 2:
            raise HTTPException(status_code=400, detail="Display name must be at least 2 characters")
        sleeper_user_id = (invite["sleeper_user_id"] or "").strip()
        sleeper_username = (invite["sleeper_username"] or payload.sleeper_username or "").strip()
        if sleeper_user_id:
            manager = conn.execute(
                "SELECT * FROM league_managers WHERE league_id = ? AND user_id = ?",
                (league_id, sleeper_user_id),
            ).fetchone()
            if manager:
                sleeper_username = manager["username"] or sleeper_username
                display_name = invite["display_name"] or manager["display_name"] or display_name
            claimed = conn.execute(
                """
                SELECT id, display_name, token, role, cash, league_id
                FROM participants
                WHERE league_id = ? AND sleeper_user_id = ?
                """,
                (league_id, sleeper_user_id),
            ).fetchone()
            if claimed:
                conn.execute(
                    "UPDATE invite_codes SET participant_id = ?, claimed_at = COALESCE(claimed_at, ?) WHERE code = ?",
                    (claimed["id"], now_iso(), invite_code),
                )
                return {
                    "token": claimed["token"],
                    "participant": {
                        "id": claimed["id"],
                        "league_id": claimed["league_id"],
                        "display_name": claimed["display_name"],
                        "invite_code": invite_code,
                        "role": claimed["role"],
                        "cash": float(claimed["cash"]),
                    },
                    "returning": True,
                }
        token = secrets.token_urlsafe(32)
        role = invite["role"]
        cursor = conn.execute(
            """
            INSERT INTO participants
              (league_id, display_name, sleeper_username, sleeper_user_id, token, role, cash, created_at, environment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                league_id,
                display_name,
                sleeper_username,
                sleeper_user_id or None,
                token,
                role,
                STARTING_BALANCE,
                now_iso(),
                data_environment(),
            ),
        )
        if invite["uses_remaining"] is not None:
            conn.execute("UPDATE invite_codes SET uses_remaining = uses_remaining - 1 WHERE code = ?", (invite_code,))
        if personal_invite:
            conn.execute(
                "UPDATE invite_codes SET participant_id = ?, claimed_at = COALESCE(claimed_at, ?) WHERE code = ?",
                (cursor.lastrowid, now_iso(), invite_code),
            )
        return {
            "token": token,
            "participant": {
                "id": cursor.lastrowid,
                "league_id": league_id,
                "display_name": display_name,
                "invite_code": invite_code if personal_invite else "",
                "role": role,
                "cash": STARTING_BALANCE,
            },
            "returning": False,
        }


@app.get("/api/session")
def session(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        cash, open_value = cash_and_open_value(conn, participant)
        return {
            "participant": participant_payload(conn, participant),
            "cash": cash,
            "open_value": round(open_value, 2),
            "net_worth": round(cash + open_value, 2),
            "league": active_league_meta(conn, participant.get("league_id")),
        }


@app.post("/api/account/code")
def update_account_code(payload: AccountCodeRequest, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    code = invite_code_slug(payload.code)
    if len(code) < 2:
        raise HTTPException(status_code=400, detail="League code must include at least 2 letters or numbers")
    if len(code) > 80:
        raise HTTPException(status_code=400, detail="League code must be 80 characters or fewer")
    with db() as conn:
        begin_immediate(conn)
        participant = get_participant(conn, x_participant_token)
        participant_id = int(participant["id"])
        league_id = normalize_league_id(participant.get("league_id"))
        existing = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
        if existing and (
            existing["participant_id"] is None or int(existing["participant_id"]) != participant_id
        ):
            raise HTTPException(status_code=409, detail="League code is already in use")

        owned = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM invite_codes
                WHERE participant_id = ?
                ORDER BY claimed_at DESC, created_at DESC, code
                """,
                (participant_id,),
            ).fetchall()
        ]
        current = next((row for row in owned if row["code"] == code), None) or (owned[0] if owned else None)
        display_name = str(participant.get("display_name") or "").strip() or display_name_from_code(code)
        sleeper_user_id = str(participant.get("sleeper_user_id") or "").strip()
        sleeper_username = str(participant.get("sleeper_username") or "").strip()
        role = str(participant.get("role") or "participant")
        timestamp = now_iso()
        if current:
            conn.execute(
                """
                UPDATE invite_codes
                SET code = ?, league_id = ?, role = ?, uses_remaining = NULL,
                    display_name = ?, sleeper_user_id = ?, sleeper_username = ?,
                    participant_id = ?, claimed_at = COALESCE(claimed_at, ?)
                WHERE code = ?
                """,
                (
                    code,
                    league_id,
                    role,
                    display_name,
                    sleeper_user_id,
                    sleeper_username,
                    participant_id,
                    timestamp,
                    current["code"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO invite_codes
                  (code, league_id, role, uses_remaining, display_name, sleeper_user_id,
                   sleeper_username, participant_id, claimed_at, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    league_id,
                    role,
                    display_name,
                    sleeper_user_id,
                    sleeper_username,
                    participant_id,
                    timestamp,
                    timestamp,
                ),
            )
        conn.execute("DELETE FROM invite_codes WHERE participant_id = ? AND code != ?", (participant_id, code))
        updated = get_participant(conn, str(participant["token"]))
        invite = dict(conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone())
        return {"participant": participant_payload(conn, updated), "invite": invite}


@app.get("/api/identity/options")
def identity_options(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        managers = stored_managers(conn, league_id)
        if not managers:
            snapshot = latest_snapshot(conn, league_id)
            if snapshot:
                records = manager_records_from_snapshot(snapshot, league_id)
                store_manager_records(conn, records, now_iso())
                managers = stored_managers(conn, league_id)
        claims = manager_claims(conn, league_id)
        for manager in managers:
            claim = claims.get(manager["user_id"])
            manager["claimed_by"] = claim
            manager["is_claimed"] = bool(claim)
            manager["is_claimed_by_me"] = bool(claim and int(claim["participant_id"]) == int(participant["id"]))
        current = None
        if participant.get("sleeper_user_id"):
            current = next((manager for manager in managers if manager["user_id"] == participant.get("sleeper_user_id")), None)
        return {"participant": participant, "current": current, "managers": managers}


@app.post("/api/identity/claim")
def claim_identity(payload: ClaimIdentityRequest, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        manager = conn.execute(
            "SELECT * FROM league_managers WHERE league_id = ? AND user_id = ?",
            (league_id, payload.user_id),
        ).fetchone()
        if not manager:
            raise HTTPException(status_code=404, detail="Sleeper manager not found")
        claimed = conn.execute(
            """
            SELECT id, display_name
            FROM participants
            WHERE league_id = ? AND sleeper_user_id = ? AND id != ?
            """,
            (league_id, payload.user_id, participant["id"]),
        ).fetchone()
        if claimed:
            raise HTTPException(status_code=409, detail=f"Already claimed by {claimed['display_name']}")
        conn.execute(
            "UPDATE participants SET sleeper_user_id = ?, sleeper_username = ? WHERE id = ?",
            (payload.user_id, manager["username"] or participant.get("sleeper_username") or "", participant["id"]),
        )
        updated = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
        return {"participant": updated, "manager": dict(manager)}


@app.get("/api/markets")
def list_markets(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        positions = participant_position_map(conn, int(participant["id"]))
        markets = []
        environments = visible_market_environments()
        placeholders = ",".join("?" for _ in environments)
        for row in conn.execute(
            f"""
            SELECT id FROM markets
            WHERE league_id = ? AND visibility = 'public' AND environment IN ({placeholders})
            ORDER BY status = 'open' DESC, id DESC
            """,
            (league_id, *environments),
        ).fetchall():
            market = market_with_outcomes(conn, int(row["id"]), league_id)
            market["user_shares"] = sum(positions.get(int(outcome["id"]), 0.0) for outcome in market["outcomes"])
            markets.append(market)
        recent_trades = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT t.*, p.display_name, m.title, o.label AS outcome_label
                FROM trades t
                JOIN participants p ON p.id = t.participant_id
                JOIN markets m ON m.id = t.market_id
                JOIN outcomes o ON o.id = t.outcome_id
                WHERE m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
                ORDER BY t.id DESC LIMIT 12
                """,
                (league_id, *environments),
            ).fetchall()
        ]
        return {"markets": markets, "recent_trades": recent_trades}


@app.get("/api/ticker")
def ticker(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        return {"items": ticker_items(conn, league_id)}


@app.get("/api/events")
async def events(
    request: Request,
    token: str = Query(default=""),
    x_participant_token: Optional[str] = Header(default=None),
):
    execute_schema()
    auth_token = x_participant_token or token
    with db() as conn:
        participant = get_participant(conn, auth_token)
        league_id = normalize_league_id(participant.get("league_id"))

    async def stream():
        last_signature = ""
        heartbeat = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                with db() as conn:
                    revision = realtime_revision(conn, league_id)
                if revision["signature"] != last_signature:
                    last_signature = revision["signature"]
                    yield f"event: market_update\ndata: {json.dumps(revision, separators=(',', ':'))}\n\n"
                elif heartbeat >= 10:
                    heartbeat = 0
                    yield ": heartbeat\n\n"
                heartbeat += 1
            except Exception as error:
                yield f"event: stream_error\ndata: {json.dumps({'detail': str(error)[:200]}, separators=(',', ':'))}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/markets/{market_id}")
def get_market(market_id: int, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        market = market_with_outcomes(conn, market_id, league_id)
        positions = participant_position_map(conn, int(participant["id"]))
        for outcome in market["outcomes"]:
            outcome["user_shares"] = round(positions.get(int(outcome["id"]), 0.0), 4)
            outcome["user_value"] = round(positions.get(int(outcome["id"]), 0.0) * float(outcome["price"]), 2)
            outcome["evidence"] = outcome_evidence(conn, league_id, outcome)
        trades = [
            dict(row)
            for row in conn.execute(
                """
                SELECT t.*, p.display_name, o.label AS outcome_label
                FROM trades t
                JOIN participants p ON p.id = t.participant_id
                JOIN outcomes o ON o.id = t.outcome_id
                WHERE t.market_id = ?
                ORDER BY t.id DESC LIMIT 30
                """,
                (market_id,),
            ).fetchall()
        ]
        return {"market": market, "trades": trades, "price_history": market_price_history(conn, market_id)}


def apply_trade(
    conn: sqlite3.Connection,
    participant: dict,
    market_id: int,
    outcome_id: int,
    shares: float,
    side: str,
    is_demo: bool = False,
    market_revision: Optional[int] = None,
    max_cost: Optional[float] = None,
    min_proceeds: Optional[float] = None,
) -> dict:
    begin_immediate(conn)
    participant = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
    quote = quote_trade(conn, participant, market_id, outcome_id, shares, side)
    if not is_demo and market_revision is None:
        raise HTTPException(status_code=422, detail="A current market revision is required")
    if not is_demo and side == "buy" and max_cost is None:
        raise HTTPException(status_code=422, detail="Buy orders require max_cost")
    if not is_demo and side == "sell" and min_proceeds is None:
        raise HTTPException(status_code=422, detail="Sell orders require min_proceeds")
    current_revision = int(quote["market"].get("trade_revision") or 0)
    if market_revision is not None and market_revision != current_revision:
        raise HTTPException(status_code=409, detail="Market moved after this quote; review the updated price")
    if side == "buy" and max_cost is not None and quote["estimated_cost"] > max_cost + 1e-9:
        raise HTTPException(status_code=409, detail="Order cost exceeds the approved maximum")
    if side == "sell" and min_proceeds is not None and quote["estimated_cost"] + 1e-9 < min_proceeds:
        raise HTTPException(status_code=409, detail="Order proceeds are below the approved minimum")
    market = quote["market"]
    league_id = normalize_league_id(participant.get("league_id"))
    delta = shares if side == "buy" else -shares
    next_quantity = float(quote["outcome"]["quantity"]) + delta
    next_revision = current_revision + 1
    conn.execute("UPDATE outcomes SET quantity = ? WHERE id = ?", (next_quantity, outcome_id))
    conn.execute("UPDATE markets SET trade_revision = ? WHERE id = ?", (next_revision, market_id))
    conn.execute("UPDATE participants SET cash = cash + ? WHERE id = ?", (quote["cash_delta"], participant["id"]))
    conn.execute(
        """
        INSERT INTO positions (participant_id, outcome_id, shares)
        VALUES (?, ?, ?)
        ON CONFLICT(participant_id, outcome_id) DO UPDATE SET shares = shares + excluded.shares
        """,
        (participant["id"], outcome_id, delta),
    )
    conn.execute("DELETE FROM positions WHERE ABS(shares) < 0.000001")
    trade_cursor = conn.execute(
        """
        INSERT INTO trades
          (participant_id, market_id, outcome_id, side, shares, cash_delta, price_before, price_after, is_demo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant["id"],
            market_id,
            outcome_id,
            side,
            shares,
            quote["cash_delta"],
            quote["price_before"],
            quote["price_after"],
            1 if is_demo else 0,
            now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO ledger_entries (participant_id, entry_type, amount, market_id, outcome_id, note, is_demo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (participant["id"], side, quote["cash_delta"], market_id, outcome_id, f"{side} {shares:.4f} shares", 1 if is_demo else 0, now_iso()),
    )
    created_at = now_iso()
    event_cursor = conn.execute(
        """
        INSERT INTO market_events
          (market_id, event_type, actor_type, actor_id, revision, payload_json, created_at)
        VALUES (?, 'trade', 'participant', ?, ?, ?, ?)
        """,
        (
            market_id,
            str(participant["id"]),
            next_revision,
            json.dumps({"trade_id": trade_cursor.lastrowid, "side": side, "shares": shares, "outcome_id": outcome_id}),
            created_at,
        ),
    )
    updated_outcomes = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM outcomes WHERE market_id = ? ORDER BY sort_order, id", (market_id,)
        ).fetchall()
    ]
    updated_priors = [
        float(row.get("prior_probability") or 1.0 / len(updated_outcomes)) for row in updated_outcomes
    ]
    updated_prices = lmsr_prices(
        [float(row["quantity"]) for row in updated_outcomes],
        float(market["liquidity"]),
        float(market["payout"]),
        updated_priors,
    )
    conn.executemany(
        """
        INSERT INTO price_ticks
          (market_id, outcome_id, event_id, market_probability, model_probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                market_id,
                row["id"],
                event_cursor.lastrowid,
                price / float(market["payout"]),
                row.get("model_probability"),
                created_at,
            )
            for row, price in zip(updated_outcomes, updated_prices)
        ],
    )
    updated = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
    return {"market": market_with_outcomes(conn, market_id, league_id), "participant": updated, "cash_delta": round(quote["cash_delta"], 2), "quote": quote_public(quote)}


def quote_public(quote: dict) -> dict:
    return {
        "market_id": quote["market_id"],
        "outcome_id": quote["outcome_id"],
        "side": quote["side"],
        "shares": round(float(quote["shares"]), 4),
        "price_before": round(float(quote["price_before"]), 4),
        "price_after": round(float(quote["price_after"]), 4),
        "probability_before": round(float(quote["price_before"]) / float(quote["payout"]), 4),
        "probability_after": round(float(quote["price_after"]) / float(quote["payout"]), 4),
        "cost": round(float(quote["cost"]), 4),
        "estimated_cost": round(float(quote["estimated_cost"]), 4),
        "average_fill_price": round(
            float(quote["estimated_cost"]) / max(float(quote["shares"]), 1e-12), 4
        ),
        "requested_budget": (
            round(float(quote["requested_budget"]), 2)
            if quote.get("requested_budget") is not None
            else None
        ),
        "cash_delta": round(float(quote["cash_delta"]), 4),
        "cash_available": round(float(quote["cash_available"]), 2),
        "owned_shares": round(float(quote["owned_shares"]), 4),
        "max_payout_if_wins": round(float(quote["max_payout_if_wins"]), 2),
        "payout": round(float(quote["payout"]), 2),
        "market_revision": int(quote["market"].get("trade_revision") or 0),
        "price_impact": round(
            (float(quote["price_after"]) - float(quote["price_before"])) / float(quote["payout"]), 4
        ),
        "model_probability": round(float(quote["outcome"].get("model_probability") or 0), 4),
        "status": quote["market"]["status"],
        "resolution_rule": quote["market"].get("resolution_rule") or "",
        "resolution_source": quote["market"].get("resolution_source") or "Commissioner",
    }


def quote_trade(
    conn: sqlite3.Connection,
    participant: dict,
    market_id: int,
    outcome_id: int,
    shares: Optional[float],
    side: str,
    budget: Optional[float] = None,
) -> dict:
    league_id = normalize_league_id(participant.get("league_id"))
    market = market_with_outcomes(conn, market_id, league_id)
    if market["status"] != "open":
        raise HTTPException(status_code=409, detail="Market is not open")
    outcome_ids = [int(outcome["id"]) for outcome in market["outcomes"]]
    if outcome_id not in outcome_ids:
        raise HTTPException(status_code=400, detail="Outcome is not part of this market")
    quantities = [float(outcome["quantity"]) for outcome in market["outcomes"]]
    index = outcome_ids.index(outcome_id)
    priors = [
        float(outcome.get("prior_probability") or 1.0 / len(market["outcomes"]))
        for outcome in market["outcomes"]
    ]
    if side == "buy" and budget is not None:
        shares = shares_for_budget(
            quantities,
            index,
            float(budget),
            float(market["liquidity"]),
            float(market["payout"]),
            priors,
        )
    elif shares is None:
        detail = "Buy quotes require a credit budget or share quantity" if side == "buy" else "Sell quotes require shares"
        raise HTTPException(status_code=422, detail=detail)
    shares = float(shares)
    before_prices = lmsr_prices(quantities, float(market["liquidity"]), float(market["payout"]), priors)
    delta = shares if side == "buy" else -shares
    owned = conn.execute(
        "SELECT shares FROM positions WHERE participant_id = ? AND outcome_id = ?",
        (participant["id"], outcome_id),
    ).fetchone()
    owned_shares = float(owned["shares"]) if owned else 0.0

    if side == "sell":
        if owned_shares + 1e-9 < shares:
            raise HTTPException(status_code=409, detail="Cannot sell more shares than owned")

    cost = trade_cost(
        quantities,
        index,
        delta,
        float(market["liquidity"]),
        float(market["payout"]),
        priors,
    )
    if side == "buy" and float(participant["cash"]) + 1e-9 < cost:
        raise HTTPException(status_code=409, detail="Insufficient cash")

    cash_delta = -cost
    next_quantities = list(quantities)
    next_quantities[index] += delta
    after_prices = lmsr_prices(
        next_quantities, float(market["liquidity"]), float(market["payout"]), priors
    )
    next_owned = owned_shares + delta
    return {
        "market": market,
        "outcome": market["outcomes"][index],
        "market_id": market_id,
        "outcome_id": outcome_id,
        "side": side,
        "shares": shares,
        "price_before": before_prices[index],
        "price_after": after_prices[index],
        "cost": cost,
        "estimated_cost": abs(cost),
        "cash_delta": cash_delta,
        "cash_available": float(participant["cash"]),
        "owned_shares": owned_shares,
        "max_payout_if_wins": max(0.0, next_owned) * float(market["payout"]),
        "payout": float(market["payout"]),
        "requested_budget": budget,
    }


def demo_trade_plan(conn: sqlite3.Connection, league_id: str) -> list[tuple[int, int, float, str]]:
    rows = conn.execute(
        """
        SELECT id FROM markets
        WHERE league_id = ? AND status = 'open'
        ORDER BY
          CASE
            WHEN title LIKE '%Champion%' THEN 0
            WHEN title LIKE '%Makes Playoffs%' THEN 1
            WHEN title LIKE 'Week %Top Scoring Team%' THEN 2
            WHEN title LIKE 'Week %Lowest Scoring Team%' THEN 3
            ELSE 3
          END,
          id
        LIMIT 10
        """,
        (league_id,),
    ).fetchall()
    plan = []
    buy_sizes = [16, 11, 9, 7, 13, 8, 6, 10, 5, 4]
    for index, row in enumerate(rows):
        outcomes = conn.execute(
            "SELECT id FROM outcomes WHERE market_id = ? ORDER BY sort_order, id",
            (row["id"],),
        ).fetchall()
        if not outcomes:
            continue
        outcome = outcomes[index % len(outcomes)]
        shares = float(buy_sizes[index % len(buy_sizes)])
        plan.append((int(row["id"]), int(outcome["id"]), shares, "buy"))
        if index in (1, 4, 7):
            plan.append((int(row["id"]), int(outcome["id"]), round(shares * 0.35, 2), "sell"))
    return plan


def clear_demo_activity(conn: sqlite3.Connection, participant: dict) -> dict:
    demo_trades = conn.execute(
        """
        SELECT outcome_id, side, shares, cash_delta
        FROM trades
        WHERE participant_id = ? AND is_demo = 1
        ORDER BY id
        """,
        (participant["id"],),
    ).fetchall()
    if not demo_trades:
        return {"cleared_trades": 0, "cash_restored": 0.0, "positions_adjusted": 0}

    outcome_deltas: dict[int, float] = {}
    cash_delta = 0.0
    for trade in demo_trades:
        multiplier = 1 if trade["side"] == "buy" else -1
        outcome_id = int(trade["outcome_id"])
        outcome_deltas[outcome_id] = outcome_deltas.get(outcome_id, 0.0) + multiplier * float(trade["shares"])
        cash_delta += float(trade["cash_delta"])

    adjusted = 0
    for outcome_id, demo_delta in outcome_deltas.items():
        conn.execute("UPDATE outcomes SET quantity = quantity - ? WHERE id = ?", (demo_delta, outcome_id))
        row = conn.execute(
            "SELECT shares FROM positions WHERE participant_id = ? AND outcome_id = ?",
            (participant["id"], outcome_id),
        ).fetchone()
        if not row:
            continue
        remaining = float(row["shares"]) - demo_delta
        if remaining <= 0.000001:
            conn.execute("DELETE FROM positions WHERE participant_id = ? AND outcome_id = ?", (participant["id"], outcome_id))
        else:
            conn.execute(
                "UPDATE positions SET shares = ? WHERE participant_id = ? AND outcome_id = ?",
                (remaining, participant["id"], outcome_id),
            )
        adjusted += 1

    conn.execute("UPDATE participants SET cash = cash - ? WHERE id = ?", (cash_delta, participant["id"]))
    conn.execute("DELETE FROM trades WHERE participant_id = ? AND is_demo = 1", (participant["id"],))
    conn.execute("DELETE FROM ledger_entries WHERE participant_id = ? AND is_demo = 1", (participant["id"],))
    return {
        "cleared_trades": len(demo_trades),
        "cash_restored": round(-cash_delta, 2),
        "positions_adjusted": adjusted,
    }


@app.post("/api/demo/populate")
def populate_demo_activity(
    payload: DemoPopulateRequest = DemoPopulateRequest(),
    x_participant_token: Optional[str] = Header(default=None),
) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        if payload.reset_existing:
            clear_demo_activity(conn, participant)
            participant = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
        has_markets = conn.execute(
            "SELECT 1 FROM markets WHERE league_id = ? AND status = 'open' LIMIT 1",
            (league_id,),
        ).fetchone()
        if not has_markets and payload.reset_seeded_if_empty:
            fixture = PROJECT_DIR / "backend" / "fixtures" / "sleeper_sample.json"
            if league_id == DEFAULT_LEAGUE_ID and fixture.exists():
                sync_snapshot(conn, json.loads(fixture.read_text(encoding="utf-8")), DEFAULT_LEAGUE_ID)
                seed_standard_markets(conn, league_id=league_id, reset_seeded=True)
        plan = demo_trade_plan(conn, league_id)
        if not plan:
            raise HTTPException(status_code=409, detail="Seed markets before loading demo activity")
        completed = 0
        for market_id, outcome_id, shares, side in plan:
            current = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
            try:
                apply_trade(conn, current, market_id, outcome_id, shares, side, is_demo=True)
                completed += 1
            except HTTPException:
                continue
        updated = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
        cash, open_value = cash_and_open_value(conn, updated)
        return {
            "trades": completed,
            "cash": round(cash, 2),
            "open_value": round(open_value, 2),
            "net_worth": round(cash + open_value, 2),
        }


@app.post("/api/demo/clear")
def clear_demo(
    x_participant_token: Optional[str] = Header(default=None),
) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        result = clear_demo_activity(conn, participant)
        updated = dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant["id"],)).fetchone())
        cash, open_value = cash_and_open_value(conn, updated)
        return {
            **result,
            "cash": round(cash, 2),
            "open_value": round(open_value, 2),
            "net_worth": round(cash + open_value, 2),
        }


@app.post("/api/markets/{market_id}/quote")
def quote_market_order(market_id: int, payload: QuoteRequest, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        quote = quote_trade(
            conn,
            participant,
            market_id,
            payload.outcome_id,
            payload.shares,
            payload.side,
            budget=payload.budget,
        )
        return {"quote": quote_public(quote)}


@app.post("/api/markets/{market_id}/buy")
def buy(market_id: int, payload: TradeRequest, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        return apply_trade(
            conn,
            participant,
            market_id,
            payload.outcome_id,
            payload.shares,
            "buy",
            market_revision=payload.market_revision,
            max_cost=payload.max_cost,
        )


@app.post("/api/markets/{market_id}/sell")
def sell(market_id: int, payload: TradeRequest, x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        return apply_trade(
            conn,
            participant,
            market_id,
            payload.outcome_id,
            payload.shares,
            "sell",
            market_revision=payload.market_revision,
            min_proceeds=payload.min_proceeds,
        )


@app.get("/api/portfolio")
def portfolio(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant.get("league_id"))
        environments = visible_market_environments()
        placeholders = ",".join("?" for _ in environments)
        positions = []
        for row in conn.execute(
            f"""
            SELECT pos.shares, o.id AS outcome_id, o.label, o.source_ref, m.id AS market_id, m.title, m.status
            FROM positions pos
            JOIN outcomes o ON o.id = pos.outcome_id
            JOIN markets m ON m.id = o.market_id
            WHERE pos.participant_id = ? AND m.league_id = ? AND m.visibility = 'public'
              AND m.environment IN ({placeholders})
            ORDER BY m.id DESC
            """,
            (participant["id"], league_id, *environments),
        ).fetchall():
            market = market_with_outcomes(conn, int(row["market_id"]), league_id)
            outcome = next(item for item in market["outcomes"] if int(item["id"]) == int(row["outcome_id"]))
            item = dict(row)
            item["price"] = outcome["price"]
            item["probability"] = outcome["probability"]
            item["image_url"] = outcome.get("image_url") or outcome_image_url(outcome)
            item["market_value"] = round(float(row["shares"]) * float(outcome["price"]), 2)
            positions.append(item)
        cash, open_value = cash_and_open_value(conn, participant)
        demo = conn.execute(
            """
            SELECT COUNT(*) AS trade_count, COALESCE(SUM(ABS(cash_delta)), 0) AS volume
            FROM trades
            WHERE participant_id = ? AND is_demo = 1
            """,
            (participant["id"],),
        ).fetchone()
        ledger = [dict(row) for row in conn.execute(
            "SELECT * FROM ledger_entries WHERE participant_id = ? ORDER BY id DESC LIMIT 25", (participant["id"],)
        ).fetchall()]
        return {
            "cash": round(cash, 2),
            "open_value": round(open_value, 2),
            "net_worth": round(cash + open_value, 2),
            "positions": positions,
            "ledger": ledger,
            "demo": {
                "trade_count": int(demo["trade_count"] or 0),
                "volume": round(float(demo["volume"] or 0), 2),
            },
        }


@app.get("/api/fund")
def fund(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        return fund_summary(conn, normalize_league_id(participant.get("league_id")))


@app.post("/api/admin/fund/settings")
def admin_fund_settings(
    payload: FundSettingsRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    allocation_total = payload.core_pct + payload.team_pct + payload.player_pct
    if abs(allocation_total - 1.0) > 0.0001:
        raise HTTPException(status_code=400, detail="Allocation percentages must sum to 100%")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO fund_settings
              (league_id, starting_balance, trophy_reserve, cup_reserve, draft_reserve, safety_buffer, core_pct, team_pct, player_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league_id) DO UPDATE SET
              starting_balance = excluded.starting_balance,
              trophy_reserve = excluded.trophy_reserve,
              cup_reserve = excluded.cup_reserve,
              draft_reserve = excluded.draft_reserve,
              safety_buffer = excluded.safety_buffer,
              core_pct = excluded.core_pct,
              team_pct = excluded.team_pct,
              player_pct = excluded.player_pct,
              updated_at = excluded.updated_at
            """,
            (
                league_id,
                payload.starting_balance,
                payload.trophy_reserve,
                payload.cup_reserve,
                payload.draft_reserve,
                payload.safety_buffer,
                payload.core_pct,
                payload.team_pct,
                payload.player_pct,
                now_iso(),
            ),
        )
        return fund_summary(conn, league_id)


@app.post("/api/admin/fund/manual-entry")
def admin_fund_manual_entry(
    payload: FundManualEntryRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    if abs(payload.amount) < 0.01:
        raise HTTPException(status_code=400, detail="Amount must be non-zero")
    amount = payload.amount
    if payload.entry_type == "expense":
        amount = -abs(amount)
    elif payload.entry_type == "income":
        amount = abs(amount)
    with db() as conn:
        ensure_fund_settings(conn, league_id)
        conn.execute(
            """
            INSERT INTO fund_ledger_entries
              (league_id, entry_type, amount, description, source, source_key, team_roster_id, created_at)
            VALUES (?, ?, ?, ?, 'manual', NULL, ?, ?)
            """,
            (league_id, payload.entry_type, amount, payload.description.strip(), payload.team_roster_id, now_iso()),
        )
        return fund_summary(conn, league_id)


@app.post("/api/admin/fund/sync-sleeper-fees")
def admin_fund_sync_sleeper_fees(
    payload: FundSleeperSyncRequest = FundSleeperSyncRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return sync_sleeper_fee_entries(
            conn,
            normalize_league_id(payload.league_id),
            payload.start_round,
            payload.end_round,
        )


@app.post("/api/admin/fund/allocate-sidequest")
def admin_fund_allocate_sidequest(
    payload: SyncSleeperRequest = SyncSleeperRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return allocate_sidequest(conn, normalize_league_id(payload.league_id))


@app.post("/api/admin/markets/{market_id}/fund-prize")
def admin_assign_market_fund_prize(
    market_id: int,
    payload: FundMarketPrizeRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    with db() as conn:
        prize = assign_market_prize(
            conn,
            market_id,
            league_id,
            payload.category,
            payload.prize_pool,
            payload.payout_mode,
        )
        return {
            "market": market_with_outcomes(conn, market_id, league_id),
            "prize": prize,
            "fund": fund_summary(conn, league_id),
        }


@app.post("/api/admin/markets/{market_id}/payouts/pay")
def admin_mark_market_payouts_paid(
    market_id: int,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        market = market_with_outcomes(conn, market_id)
        result = mark_market_payouts_paid(conn, market_id)
        league_id = normalize_league_id(market["league_id"])
        return {
            "market": market_with_outcomes(conn, market_id, league_id),
            "prize": result["prize"],
            "payouts": result["payouts"],
            "fund": fund_summary(conn, league_id),
        }


@app.get("/api/leaderboard")
def leaderboard(x_participant_token: Optional[str] = Header(default=None)) -> dict:
    execute_schema()
    with db() as conn:
        current = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(current.get("league_id"))
        environments = visible_market_environments()
        placeholders = ",".join("?" for _ in environments)
        candidates = []
        for participant in [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM participants WHERE league_id = ? AND environment IN ({placeholders}) ORDER BY id",
                (league_id, *environments),
            ).fetchall()
        ]:
            open_value = leaderboard_open_value(
                conn, int(participant["id"]), league_id, environments
            )
            live_trades = conn.execute(
                f"""
                SELECT COUNT(*) AS trade_count
                FROM trades t JOIN markets m ON m.id = t.market_id
                WHERE t.participant_id = ? AND t.is_demo = 0
                  AND m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
                """,
                (participant["id"], league_id, *environments),
            ).fetchone()
            live_ledger = conn.execute(
                f"""
                SELECT COUNT(*) AS ledger_count, COALESCE(SUM(l.amount), 0) AS cash_change
                FROM ledger_entries l JOIN markets m ON m.id = l.market_id
                WHERE l.participant_id = ? AND l.is_demo = 0
                  AND m.league_id = ? AND m.visibility = 'public' AND m.environment IN ({placeholders})
                """,
                (participant["id"], league_id, *environments),
            ).fetchone()
            trade_count = int(live_trades["trade_count"] or 0)
            ledger_count = int(live_ledger["ledger_count"] or 0)
            cash = STARTING_BALANCE + float(live_ledger["cash_change"] or 0)
            active = trade_count > 0 or ledger_count > 0 or abs(open_value) > 0.005
            candidates.append({
                "id": participant["id"],
                "display_name": participant["display_name"],
                "role": participant["role"],
                "cash": round(cash, 2),
                "open_value": round(open_value, 2),
                "net_worth": round(cash + open_value, 2),
                "profit": round(cash + open_value - STARTING_BALANCE, 2),
                "trade_count": trade_count,
                "demo_trade_count": 0,
                "is_active": active,
                "identity_key": (
                    f"user:{participant['sleeper_user_id']}"
                    if participant.get("sleeper_user_id")
                    else f"username:{str(participant.get('sleeper_username') or '').strip().lower()}"
                    if str(participant.get("sleeper_username") or "").strip()
                    else f"participant:{participant['id']}"
                ),
                "is_current": int(participant["id"]) == int(current["id"]),
                "is_claimed": bool(participant.get("sleeper_user_id")),
            })
        deduplicated: dict[str, dict] = {}
        for row in candidates:
            existing = deduplicated.get(row["identity_key"])
            priority = (row["is_current"], row["is_claimed"], row["is_active"], row["id"])
            existing_priority = (
                existing["is_current"], existing["is_claimed"], existing["is_active"], existing["id"]
            ) if existing else None
            if existing is None or priority > existing_priority:
                deduplicated[row["identity_key"]] = row
        rows = list(deduplicated.values())
        for row in rows:
            row.pop("identity_key", None)
            row.pop("is_current", None)
            row.pop("is_claimed", None)
        rows.sort(key=lambda row: (row["is_active"], row["profit"], row["open_value"], -row["id"]), reverse=True)
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        return {
            "leaderboard": rows,
            "summary": {
                "active_count": sum(1 for row in rows if row["is_active"]),
                "idle_count": sum(1 for row in rows if not row["is_active"]),
                "starting_balance": STARTING_BALANCE,
            },
        }


@app.get("/api/admin/league-preview")
def admin_league_preview(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    try:
        league = SleeperAdapter.get_league(league_id)
        users = SleeperAdapter.get_users(league_id)
        rosters = SleeperAdapter.get_rosters(league_id)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Sleeper preview failed: {error}") from error
    users_by_id = {user.get("user_id"): user for user in users}
    teams = []
    for roster in rosters:
        user = users_by_id.get(roster.get("owner_id")) or {}
        teams.append({
            "roster_id": roster.get("roster_id"),
            "team_name": (user.get("metadata") or {}).get("team_name") or user.get("display_name") or f"Roster {roster.get('roster_id')}",
            "manager": user.get("display_name") or user.get("username") or "Unassigned",
            "owner_id": roster.get("owner_id"),
        })
    return {
        "league": {
            "league_id": league_id,
            "name": league.get("name") or "Sleeper League",
            "season": str(league.get("season") or ""),
            "total_rosters": league.get("total_rosters") or len(rosters),
        },
        "teams": teams,
        "managers": manager_records_from_snapshot({"users": users, "rosters": rosters}, league_id),
    }


@app.post("/api/admin/sync-sleeper")
def admin_sync_sleeper(
    payload: SyncSleeperRequest = SyncSleeperRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = (payload.league_id or DEFAULT_LEAGUE_ID).strip()
    try:
        snapshot = SleeperAdapter.build_snapshot(league_id)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Sleeper sync failed: {error}") from error
    with db() as conn:
        result = sync_snapshot(conn, snapshot, league_id)
        managers = stored_managers(conn, league_id)
        return {
            "league_id": league_id,
            "league": result["league"],
            "teams": result["teams"],
            "players": result["players"],
            "weeks": result["weeks"],
            "managers": managers,
            "ingestion": result["ingestion"],
        }


@app.post("/api/admin/setup-league")
def admin_setup_league(
    request: Request,
    payload: SetupLeagueRequest = SetupLeagueRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    actor = admin_request_actor(request)
    return run_tracked_job(
        league_id,
        "pipeline",
        "commissioner" if actor["method"] == "session" else "scheduler",
        lambda: execute_live_pipeline(league_id),
    )


@app.post("/api/admin/pipeline")
def admin_run_pipeline(
    request: Request,
    payload: SyncSleeperRequest = SyncSleeperRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    actor = admin_request_actor(request)
    return run_tracked_job(
        league_id,
        "pipeline",
        "commissioner" if actor["method"] == "session" else "scheduler",
        lambda: execute_live_pipeline(league_id),
    )


@app.post("/api/admin/live-scores/run")
def admin_run_live_scores(
    request: Request,
    payload: SyncSleeperRequest = SyncSleeperRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    actor = admin_request_actor(request)
    actor_type = "commissioner" if actor["method"] == "session" else "scheduler"
    return run_tracked_job(
        league_id,
        "live_scores",
        actor_type,
        lambda: run_live_score_mark_operation(league_id, actor_type=actor_type),
    )


@app.get("/api/admin/model-runs/latest")
def admin_latest_model_run(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM model_runs WHERE league_id = ? ORDER BY id DESC LIMIT 1",
            (normalize_league_id(league_id),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No model run is available")
        result = dict(row)
        result["diagnostics"] = json.loads(result.pop("diagnostics_json"))
        result["assumptions"] = json.loads(result.pop("assumptions_json"))
        return {"model": result}


@app.get("/api/admin/metrics")
def admin_operational_metrics(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return operational_metrics(conn, normalize_league_id(league_id))


@app.get("/api/admin/overview")
def admin_overview(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return admin_overview_payload(conn, normalize_league_id(league_id))


@app.get("/api/admin/dashboard")
def admin_dashboard(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return admin_dashboard_payload(conn, normalize_league_id(league_id))


@app.post("/api/feedback")
def submit_feedback(
    request: Request,
    payload: FeedbackRequest,
    x_participant_token: Optional[str] = Header(default=None),
) -> dict:
    execute_schema()
    with db() as conn:
        participant = get_participant(conn, x_participant_token)
        league_id = normalize_league_id(participant["league_id"])
        market_id = payload.market_id
        market_title = (payload.market_title or "").strip()
        if market_id:
            market = conn.execute(
                """
                SELECT id, title
                FROM markets
                WHERE id = ? AND league_id = ? AND visibility = 'public'
                """,
                (market_id, league_id),
            ).fetchone()
            if market:
                market_title = market["title"]
            else:
                market_id = None
        now = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO feedback_items
              (league_id, participant_id, category, message, page, market_id, market_title,
               context_json, user_agent, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                league_id,
                participant["id"],
                payload.category,
                payload.message.strip(),
                (payload.page or "").strip(),
                market_id,
                market_title[:160],
                compact_feedback_context(payload.context),
                request.headers.get("user-agent", "")[:300],
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT f.*, p.display_name
            FROM feedback_items f
            LEFT JOIN participants p ON p.id = f.participant_id
            WHERE f.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return {"feedback": feedback_item_payload(row)}


@app.get("/api/admin/feedback")
def admin_feedback(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    status: str = Query(default="open"),
    category: str = Query(default="all"),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return feedback_items_payload(conn, league_id, status, category)


@app.post("/api/admin/feedback/{feedback_id}/status")
def admin_update_feedback_status(
    feedback_id: int,
    payload: FeedbackStatusRequest,
    request: Request,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        row = conn.execute("SELECT * FROM feedback_items WHERE id = ?", (feedback_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback not found")
        note = payload.note.strip()
        conn.execute(
            """
            UPDATE feedback_items
            SET status = ?, admin_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (payload.status, note, now_iso(), feedback_id),
        )
        updated = conn.execute(
            """
            SELECT f.*, p.display_name
            FROM feedback_items f
            LEFT JOIN participants p ON p.id = f.participant_id
            WHERE f.id = ?
            """,
            (feedback_id,),
        ).fetchone()
        actor = admin_request_actor(request)
        record_admin_event(
            conn,
            "feedback.status",
            league_id=row["league_id"],
            actor=actor,
            entity_type="feedback",
            entity_id=str(feedback_id),
            payload={"status": payload.status},
        )
        return {"feedback": feedback_item_payload(updated)}


@app.get("/api/admin/managers")
def admin_managers(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        league_id = normalize_league_id(league_id)
        managers = stored_managers(conn, league_id)
        if not managers:
            snapshot = latest_snapshot(conn, league_id)
            if snapshot:
                records = manager_records_from_snapshot(snapshot, league_id)
                store_manager_records(conn, records, now_iso())
                managers = stored_managers(conn, league_id)
        return {"league": active_league_meta(conn, league_id), "managers": managers}


@app.get("/api/admin/participants")
def admin_participants(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        league_id = normalize_league_id(league_id)
        return {
            "league": active_league_meta(conn, league_id),
            "participants": participant_admin_rows(conn, league_id),
            "managers": stored_managers(conn, league_id),
        }


@app.post("/api/admin/participants/{participant_id}/role")
def admin_set_participant_role(
    participant_id: int,
    payload: ParticipantRoleRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        row = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Participant not found")
        conn.execute("UPDATE participants SET role = ? WHERE id = ?", (payload.role, participant_id))
        return {"participant": dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone())}


@app.post("/api/admin/participants/{participant_id}/link")
def admin_link_participant(
    participant_id: int,
    payload: ParticipantLinkRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        participant = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
        if not participant:
            raise HTTPException(status_code=404, detail="Participant not found")
        league_id = normalize_league_id(participant["league_id"])
        sleeper_user_id = (payload.sleeper_user_id or "").strip()
        username = participant["sleeper_username"] or ""
        if sleeper_user_id:
            manager = conn.execute(
                "SELECT * FROM league_managers WHERE league_id = ? AND user_id = ?",
                (league_id, sleeper_user_id),
            ).fetchone()
            if not manager:
                raise HTTPException(status_code=404, detail="Sleeper manager not found")
            claimed = conn.execute(
                """
                SELECT id, display_name
                FROM participants
                WHERE league_id = ? AND sleeper_user_id = ? AND id != ?
                """,
                (league_id, sleeper_user_id, participant_id),
            ).fetchone()
            if claimed:
                raise HTTPException(status_code=409, detail=f"Already linked to {claimed['display_name']}")
            username = manager["username"] or username
        conn.execute(
            "UPDATE participants SET sleeper_user_id = ?, sleeper_username = ? WHERE id = ?",
            (sleeper_user_id, username, participant_id),
        )
        return {"participant": dict(conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone())}


@app.get("/api/admin/invites")
def admin_invites(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        league_id = normalize_league_id(league_id)
        return {"league": active_league_meta(conn, league_id), "invites": invite_rows(conn, league_id)}


@app.post("/api/admin/invites")
def admin_create_invite(
    payload: InviteCreateRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    display_name = (payload.display_name or "").strip()
    code = invite_code_slug(payload.code or display_name)
    if len(code) < 2:
        raise HTTPException(status_code=400, detail="Invite code must be at least 2 characters")
    league_id = normalize_league_id(payload.league_id)
    sleeper_user_id = (payload.sleeper_user_id or "").strip()
    sleeper_username = (payload.sleeper_username or "").strip()
    with db() as conn:
        if sleeper_user_id:
            manager = conn.execute(
                "SELECT * FROM league_managers WHERE league_id = ? AND user_id = ?",
                (league_id, sleeper_user_id),
            ).fetchone()
            if not manager:
                raise HTTPException(status_code=404, detail="Sleeper manager not found")
            display_name = display_name or manager["display_name"] or manager["username"] or code
            sleeper_username = sleeper_username or manager["username"] or ""
        else:
            display_name = display_name or display_name_from_code(code)
        try:
            conn.execute(
                """
                INSERT INTO invite_codes
                  (code, league_id, role, uses_remaining, display_name, sleeper_user_id, sleeper_username, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, league_id, payload.role, payload.uses_remaining, display_name, sleeper_user_id, sleeper_username, now_iso()),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="Invite code already exists") from error
        return {"invite": dict(conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone())}


@app.post("/api/admin/invites/manager-codes")
def admin_create_manager_invites(
    payload: ManagerInvitesRequest,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    with db() as conn:
        managers = stored_managers(conn, league_id)
        if not managers:
            snapshot = latest_snapshot(conn, league_id)
            if snapshot:
                store_manager_records(conn, manager_records_from_snapshot(snapshot, league_id), now_iso())
                managers = stored_managers(conn, league_id)
        if not managers:
            raise HTTPException(status_code=409, detail="Load Sleeper managers before generating league codes")
        existing_codes = {
            str(row["code"])
            for row in conn.execute("SELECT code FROM invite_codes WHERE league_id = ?", (league_id,)).fetchall()
        }
        created = []
        existing = []
        for manager in managers:
            user_id = str(manager.get("user_id") or "")
            if not user_id:
                continue
            already = conn.execute(
                "SELECT * FROM invite_codes WHERE league_id = ? AND sleeper_user_id = ?",
                (league_id, user_id),
            ).fetchone()
            if already:
                existing.append(dict(already))
                continue
            base = invite_code_slug(manager.get("display_name") or manager.get("username") or manager.get("team_name") or user_id)
            code = base
            suffix = 2
            while code in existing_codes:
                code = f"{base}-{suffix}"
                suffix += 1
            existing_codes.add(code)
            conn.execute(
                """
                INSERT INTO invite_codes
                  (code, league_id, role, uses_remaining, display_name, sleeper_user_id, sleeper_username, created_at)
                VALUES (?, ?, 'participant', NULL, ?, ?, ?, ?)
                """,
                (
                    code,
                    league_id,
                    manager.get("display_name") or manager.get("username") or display_name_from_code(code),
                    user_id,
                    manager.get("username") or "",
                    now_iso(),
                ),
            )
            created.append(dict(conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()))
        return {"league": active_league_meta(conn, league_id), "created": created, "existing": existing}


@app.get("/api/admin/resolution-center")
def admin_resolution_center(
    league_id: str = Query(default=DEFAULT_LEAGUE_ID),
    status: str = Query(default="open"),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(league_id)
    statuses = ("open", "closed") if status == "actionable" else (status,)
    status_placeholders = ",".join("?" for _ in statuses)
    environments = visible_market_environments()
    environment_placeholders = ",".join("?" for _ in environments)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT id
            FROM markets
            WHERE league_id = ? AND status IN ({status_placeholders}) AND visibility = 'public'
              AND environment IN ({environment_placeholders})
            ORDER BY
              CASE WHEN status = 'closed' THEN 0 ELSE 1 END,
              id DESC
            LIMIT 80
            """,
            (league_id, *statuses, *environments),
        ).fetchall()
        return {
            "league": active_league_meta(conn, league_id),
            "markets": [market_resolution_card(conn, int(row["id"]), league_id) for row in rows],
        }


@app.post("/api/admin/seed-markets")
def admin_seed_markets(
    payload: SeedMarketsRequest = SeedMarketsRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    with db() as conn:
        result = originate_modeled_markets(conn, league_id)
        return {**result, "league": active_league_meta(conn, league_id)}


@app.post("/api/admin/seed-cup-markets")
def admin_seed_cup_markets(
    payload: SeedMarketsRequest = SeedMarketsRequest(),
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    league_id = normalize_league_id(payload.league_id)
    with db() as conn:
        return {
            "created": 0,
            "market_ids": [],
            "status": "deferred",
            "detail": "Cup markets remain in draft until entrants and bracket rules are available.",
            "league": active_league_meta(conn, league_id),
        }


@app.post("/api/admin/markets")
def admin_create_market(payload: ManualMarketRequest, x_admin_code: Optional[str] = Header(default=None)) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    outcomes = [(label.strip(), f"manual:{index}") for index, label in enumerate(payload.outcomes) if label.strip()]
    if len(outcomes) < 2:
        raise HTTPException(status_code=400, detail="At least two outcomes are required")
    with db() as conn:
        league_id = normalize_league_id(payload.league_id)
        market_id = create_market(
            conn,
            league_id,
            payload.title.strip(),
            payload.market_type,
            outcomes,
            payload.close_time or "",
            payload.resolution_source,
            payload.resolution_rule,
            payload.liquidity_label,
            f"manual:{secrets.token_hex(8)}",
        )
        if market_id is None:
            raise HTTPException(status_code=409, detail="Market already exists")
        return {"market": market_with_outcomes(conn, int(market_id), league_id)}


@app.post("/api/admin/markets/{market_id}/close")
def admin_close_market(market_id: int, x_admin_code: Optional[str] = Header(default=None)) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        changed = conn.execute(
            "UPDATE markets SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
            (now_iso(), market_id),
        ).rowcount
        if changed:
            append_market_event_with_ticks(conn, market_id, "closed", "commissioner")
        return {"market": market_with_outcomes(conn, market_id)}


def void_market_state(conn: sqlite3.Connection, market_id: int, actor_type: str) -> dict:
    market = market_with_outcomes(conn, market_id)
    if market["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Resolved markets cannot be voided")
    if market["status"] == "void":
        return {"market": market, "refunded_positions": 0}
    if market["status"] not in {"open", "closed"}:
        raise HTTPException(status_code=409, detail="Only open or closed markets can be voided")
    trades = conn.execute(
        "SELECT participant_id, SUM(-cash_delta) AS spent FROM trades WHERE market_id = ? GROUP BY participant_id",
        (market_id,),
    ).fetchall()
    for row in trades:
        refund = float(row["spent"] or 0)
        conn.execute("UPDATE participants SET cash = cash + ? WHERE id = ?", (refund, row["participant_id"]))
        conn.execute(
            "INSERT INTO ledger_entries (participant_id, entry_type, amount, market_id, note, created_at) VALUES (?, 'void_refund', ?, ?, 'Market voided', ?)",
            (row["participant_id"], refund, market_id, now_iso()),
        )
    cancel_market_prize(conn, market_id)
    conn.execute("UPDATE markets SET status = 'void', resolved_at = ? WHERE id = ?", (now_iso(), market_id))
    append_market_event_with_ticks(conn, market_id, "voided", actor_type)
    return {"market": market_with_outcomes(conn, market_id), "refunded_positions": len(trades)}


def resolve_market_state(
    conn: sqlite3.Connection,
    market_id: int,
    winning_outcome_id: int,
    actor_type: str,
) -> dict:
    market = market_with_outcomes(conn, market_id)
    if market["status"] != "closed":
        raise HTTPException(status_code=409, detail="Only closed markets can be resolved")
    outcome_ids = {int(outcome["id"]) for outcome in market["outcomes"]}
    if winning_outcome_id not in outcome_ids:
        raise HTTPException(status_code=400, detail="Winning outcome is not part of this market")
    winners = conn.execute(
        "SELECT participant_id, shares FROM positions WHERE outcome_id = ? AND shares > 0",
        (winning_outcome_id,),
    ).fetchall()
    for row in winners:
        payout = float(row["shares"]) * float(market["payout"])
        conn.execute("UPDATE participants SET cash = cash + ? WHERE id = ?", (payout, row["participant_id"]))
        conn.execute(
            "INSERT INTO ledger_entries (participant_id, entry_type, amount, market_id, outcome_id, note, created_at) VALUES (?, 'settlement', ?, ?, ?, 'Winning market settlement', ?)",
            (row["participant_id"], payout, market_id, winning_outcome_id, now_iso()),
        )
    conn.execute(
        "UPDATE markets SET status = 'resolved', winning_outcome_id = ?, resolved_at = ? WHERE id = ?",
        (winning_outcome_id, now_iso(), market_id),
    )
    append_market_event_with_ticks(
        conn,
        market_id,
        "resolved",
        actor_type,
        {"winning_outcome_id": winning_outcome_id},
    )
    payout_plan = generate_market_payout_plan(conn, market_id)
    return {
        "market": market_with_outcomes(conn, market_id),
        "settled_positions": len(winners),
        "fund_payouts": payout_plan,
    }


def automated_resolution_family(contract_key: Optional[str]) -> Optional[str]:
    key = str(contract_key or "")
    if key.endswith(":champion"):
        return "champion"
    if ":makes-playoffs:" in key:
        return "makes_playoffs"
    if key.endswith(":week_top"):
        return "week_top"
    if key.endswith(":week_low"):
        return "week_low"
    return None


def resolution_candidate_from_snapshot(
    conn: sqlite3.Connection,
    market: sqlite3.Row,
    snapshot: dict,
    as_of: Optional[datetime] = None,
) -> Optional[tuple[int, str, str, bool]]:
    family = automated_resolution_family(market["contract_key"])
    if not family:
        return None
    checked_at = as_of or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    outcomes = conn.execute(
        "SELECT id, source_ref FROM outcomes WHERE market_id = ?", (market["id"],)
    ).fetchall()
    outcome_by_ref = {str(outcome["source_ref"]): int(outcome["id"]) for outcome in outcomes}

    if family in {"week_top", "week_low"}:
        schedule = weekly_contract_schedule(market["contract_key"])
        if not schedule:
            return None
        season, market_week = schedule
        if checked_at.astimezone(timezone.utc) < weekly_settlement_time(season, market_week):
            return None
        scores = {
            int(entry["roster_id"]): float(entry["points"])
            for entry in (snapshot.get("matchups") or {}).get(str(market_week), [])
            if entry.get("roster_id") is not None and isinstance(entry.get("points"), (int, float))
        }
        roster_refs = {
            int(source_ref.split(":", 1)[1]): outcome_id
            for source_ref, outcome_id in outcome_by_ref.items()
            if source_ref.startswith("roster:") and source_ref.count(":") == 1
        }
        if not roster_refs or any(roster_id not in scores for roster_id in roster_refs):
            return None
        extreme = (max if family == "week_top" else min)(scores[roster_id] for roster_id in roster_refs)
        winners = sorted(roster_id for roster_id in roster_refs if scores[roster_id] == extreme)
        result_key = "tie:" + ",".join(map(str, winners)) if len(winners) != 1 else f"roster:{winners[0]}"
        outcome_id = roster_refs[winners[0]]
        evidence_hash = model_fingerprint({str(key): scores[key] for key in sorted(scores)})
        return outcome_id, result_key, evidence_hash, len(winners) != 1

    bracket = snapshot.get("winners_bracket") or []
    if not bracket:
        return None

    if family == "makes_playoffs":
        try:
            roster_id = int(str(market["contract_key"]).rsplit(":makes-playoffs:", 1)[1])
        except (IndexError, TypeError, ValueError):
            return None
        playoff_rosters = set()
        for matchup in bracket:
            for key in ("t1", "t2", "w", "l"):
                try:
                    value = int(matchup.get(key))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    playoff_rosters.add(value)
        if not playoff_rosters:
            return None
        result_ref = f"roster:{roster_id}:{'yes' if roster_id in playoff_rosters else 'no'}"
        if result_ref not in outcome_by_ref:
            return None
        return (
            outcome_by_ref[result_ref],
            result_ref,
            model_fingerprint({"playoff_rosters": sorted(playoff_rosters)}),
            False,
        )

    final_winners = set()
    for matchup in bracket:
        try:
            is_final = int(matchup.get("p")) == 1
            winner = int(matchup.get("w"))
        except (TypeError, ValueError):
            continue
        if is_final and winner > 0:
            final_winners.add(winner)
    if len(final_winners) != 1:
        return None
    winner = next(iter(final_winners))
    result_ref = f"roster:{winner}"
    if result_ref not in outcome_by_ref:
        return None
    return (
        outcome_by_ref[result_ref],
        result_ref,
        model_fingerprint({"champion": winner}),
        False,
    )


def observe_automatic_resolutions(
    conn: sqlite3.Connection,
    as_of: Optional[datetime] = None,
) -> dict:
    observed = 0
    resolved = []
    voided = []
    sources: dict[str, tuple[sqlite3.Row, dict]] = {}
    rows = conn.execute(
        """
        SELECT id, league_id, contract_key FROM markets
        WHERE status = 'closed' AND origin = 'model' AND visibility = 'public'
        """
    ).fetchall()
    for row in rows:
        league_id = str(row["league_id"])
        if league_id not in sources:
            source_row = conn.execute(
                """
                SELECT payload_json, created_at FROM source_snapshots
                WHERE source IN ('sleeper_resolution', 'sleeper') AND league_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (league_id,),
            ).fetchone()
            if not source_row:
                continue
            sources[league_id] = (source_row, json.loads(source_row["payload_json"]))
        source_row, snapshot = sources[league_id]
        candidate = resolution_candidate_from_snapshot(conn, row, snapshot, as_of)
        if not candidate:
            continue
        outcome_id, result_key, evidence_hash, should_void = candidate
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO resolution_observations
              (market_id, source_snapshot_at, result_key, scores_hash, observed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                source_row["created_at"],
                result_key,
                evidence_hash,
                now_iso(),
            ),
        ).rowcount
        observed += inserted
        family = automated_resolution_family(row["contract_key"])
        if family not in {"week_top", "week_low"}:
            last_two = conn.execute(
                "SELECT result_key FROM resolution_observations WHERE market_id = ? ORDER BY id DESC LIMIT 2",
                (row["id"],),
            ).fetchall()
            if len(last_two) < 2 or last_two[0]["result_key"] != last_two[1]["result_key"]:
                continue
        if should_void:
            void_market_state(conn, int(row["id"]), "scheduler")
            voided.append(int(row["id"]))
        else:
            resolve_market_state(conn, int(row["id"]), outcome_id, "scheduler")
            resolved.append(int(row["id"]))
    return {"observed": observed, "resolved": resolved, "voided": voided}


def observe_weekly_resolutions(conn: sqlite3.Connection) -> dict:
    """Compatibility alias for callers introduced before season contracts were automated."""
    return observe_automatic_resolutions(conn)


def fetch_resolution_sources(
    targets: list[sqlite3.Row],
    as_of: Optional[datetime] = None,
) -> list[tuple[str, dict]]:
    checked_at = as_of or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    requested: dict[str, dict] = {}
    for row in targets:
        family = automated_resolution_family(row["contract_key"])
        if not family:
            continue
        request = requested.setdefault(str(row["league_id"]), {"weeks": set(), "bracket": False})
        if family in {"week_top", "week_low"}:
            schedule = weekly_contract_schedule(row["contract_key"])
            if schedule:
                request["weeks"].add(schedule)
        else:
            request["bracket"] = True
    snapshots = []
    for league_id, request in requested.items():
        due_weeks = sorted(
            week
            for season, week in request["weeks"]
            if checked_at.astimezone(timezone.utc) >= weekly_settlement_time(season, week)
        )
        if not due_weeks and not request["bracket"]:
            continue
        league = SleeperAdapter.get_league(league_id)
        matchups = {str(week): SleeperAdapter.get_matchups(league_id, week) for week in due_weeks}
        bracket = SleeperAdapter.get_winners_bracket(league_id) if request["bracket"] else []
        snapshots.append(
            (
                league_id,
                {
                    "source": "sleeper",
                    "provenance": "live_resolution",
                    "league": league,
                    "matchups": matchups,
                    "winners_bracket": bracket,
                },
            )
        )
    return snapshots


def run_lifecycle_operation(
    actor_type: str = "scheduler",
    as_of: Optional[datetime] = None,
    refresh_sources: Optional[bool] = None,
) -> dict:
    closed = []
    checked_at = as_of or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    current = checked_at.isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM markets
            WHERE status = 'open' AND visibility = 'public' AND close_time IS NOT NULL
              AND close_time != '' AND close_time <= ?
            """,
            (current,),
        ).fetchall()
        for row in rows:
            market_id = int(row["id"])
            conn.execute(
                "UPDATE markets SET status = 'closed', closed_at = ? WHERE id = ?", (current, market_id)
            )
            append_market_event_with_ticks(conn, market_id, "closed", actor_type)
            closed.append(market_id)
        resolution_targets = conn.execute(
            """
            SELECT id, league_id, contract_key FROM markets
            WHERE status = 'closed' AND origin = 'model' AND visibility = 'public'
            """
        ).fetchall()

    refreshed_sources = []
    should_refresh_sources = APP_ENV == "production" if refresh_sources is None else refresh_sources
    if resolution_targets and should_refresh_sources:
        try:
            refreshed_sources = fetch_resolution_sources(resolution_targets, checked_at)
        except Exception as error:
            raise HTTPException(status_code=502, detail=f"Sleeper settlement refresh failed: {error}") from error

    with db() as conn:
        for league_id, snapshot in refreshed_sources:
            conn.execute(
                """
                INSERT INTO source_snapshots (source, league_id, payload_json, created_at)
                VALUES ('sleeper_resolution', ?, ?, ?)
                """,
                (league_id, json.dumps(snapshot, separators=(",", ":")), current),
            )
        settlement = observe_automatic_resolutions(conn, checked_at)
        return {
            "closed": len(closed),
            "market_ids": closed,
            "sources_refreshed": len(refreshed_sources),
            "observed": settlement["observed"],
            "resolved": settlement["resolved"],
            "voided": settlement["voided"],
            "checked_at": current,
        }


@app.post("/api/admin/lifecycle/run")
def admin_run_lifecycle(
    request: Request,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    actor = admin_request_actor(request)
    triggered_by = "commissioner" if actor["method"] == "session" else "scheduler"
    return run_tracked_job(
        DEFAULT_LEAGUE_ID,
        "lifecycle",
        triggered_by,
        lambda: run_lifecycle_operation(triggered_by),
    )


def scheduled_job_due(conn: sqlite3.Connection, league_id: str, job_type: str, interval_hours: float) -> bool:
    row = conn.execute(
        """
        SELECT completed_at FROM job_runs
        WHERE league_id = ? AND job_type = ? AND status = 'succeeded'
        ORDER BY id DESC LIMIT 1
        """,
        (normalize_league_id(league_id), job_type),
    ).fetchone()
    if not row or not row["completed_at"]:
        return True
    try:
        completed_at = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc) >= timedelta(hours=interval_hours)


@app.post("/api/admin/scheduled/run")
def admin_run_scheduled_jobs(
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    if not _SCHEDULE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scheduled automation run is already in progress")
    league_id = normalize_league_id(DEFAULT_LEAGUE_ID)
    try:
        with db() as conn:
            due = {
                "lifecycle": True,
                "live_scores": scheduled_job_due(conn, league_id, "live_scores", LIVE_SCORE_MARK_INTERVAL_HOURS),
                "pipeline": SCHEDULE_PIPELINE and scheduled_job_due(conn, league_id, "pipeline", 12),
                "backup": scheduled_job_due(conn, league_id, "backup", 24),
                "prune": scheduled_job_due(conn, league_id, "prune", 24),
            }
        operations = {
            "lifecycle": lambda: run_lifecycle_operation("scheduler"),
            "live_scores": lambda: run_live_score_mark_operation(league_id, actor_type="scheduler"),
            "pipeline": lambda: execute_live_pipeline(league_id),
            "backup": lambda: {"backup": create_database_backup()},
            "prune": lambda: _prune_artifacts_operation(),
        }
        results = {}
        errors = {}
        for job_type in ("lifecycle", "live_scores", "pipeline", "backup", "prune"):
            if not due[job_type]:
                continue
            try:
                results[job_type] = run_tracked_job(
                    league_id,
                    job_type,
                    "scheduler",
                    operations[job_type],
                )
            except Exception as error:
                errors[job_type] = str(getattr(error, "detail", error))[:2000]
        response = {
            "checked_at": now_iso(),
            "due": [job_type for job_type, is_due in due.items() if is_due],
            "completed": list(results),
            "skipped": [job_type for job_type, is_due in due.items() if not is_due],
            "results": results,
        }
        if errors:
            raise HTTPException(
                status_code=502,
                detail={**response, "message": "One or more scheduled jobs failed", "errors": errors},
            )
        return response
    finally:
        _SCHEDULE_LOCK.release()


@app.post("/api/admin/maintenance/backup")
def admin_backup_database(
    request: Request,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    actor = admin_request_actor(request)
    return run_tracked_job(
        DEFAULT_LEAGUE_ID,
        "backup",
        "commissioner" if actor["method"] == "session" else "scheduler",
        lambda: {"backup": create_database_backup()},
    )


def _prune_artifacts_operation() -> dict:
    with db() as conn:
        return prune_artifacts(conn, RAW_DATA_DIR, retention_days=14)


@app.post("/api/admin/maintenance/prune")
def admin_prune_artifacts(
    request: Request,
    x_admin_code: Optional[str] = Header(default=None),
) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    actor = admin_request_actor(request)

    return run_tracked_job(
        DEFAULT_LEAGUE_ID,
        "prune",
        "commissioner" if actor["method"] == "session" else "scheduler",
        _prune_artifacts_operation,
    )


@app.post("/api/admin/markets/{market_id}/void")
def admin_void_market(market_id: int, x_admin_code: Optional[str] = Header(default=None)) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    with db() as conn:
        return void_market_state(conn, market_id, "commissioner")


@app.post("/api/admin/markets/{market_id}/resolve")
def admin_resolve_market(market_id: int, payload: ResolveRequest, x_admin_code: Optional[str] = Header(default=None)) -> dict:
    require_admin(x_admin_code)
    execute_schema()
    if payload.winning_outcome_id is None:
        raise HTTPException(status_code=400, detail="winning_outcome_id is required")
    with db() as conn:
        return resolve_market_state(conn, market_id, payload.winning_outcome_id, "commissioner")


if __name__ == "__main__":
    import uvicorn

    validate_runtime_config()
    execute_schema()
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
