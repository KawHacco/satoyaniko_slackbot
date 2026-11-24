import calendar
import datetime
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

load_dotenv()

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()
ADMIN_USER_IDS = {
    uid.strip()
    for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
    if uid.strip()
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
DB_PATH = DATA_DIR / "schedules.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("message_sender")

if not BOT_TOKEN.startswith("xoxb-"):
    raise RuntimeError(
        "SLACK_BOT_TOKEN が未設定、または xoxb- 形式ではありません。.env を確認してください。"
    )
if not APP_TOKEN.startswith("xapp-"):
    raise RuntimeError(
        "SLACK_APP_TOKEN が未設定、または xapp- 形式ではありません。.env を確認してください。"
    )

app = App(token=BOT_TOKEN)

MAX_FILES = 10
MAX_FILE_SIZE = 100 * 1024 * 1024  # Slack file_input 上限: 100MB / file
MAX_TOTAL_DOWNLOAD = 200 * 1024 * 1024
SCHEDULER_POLL_SECONDS = 10
LOCK_DURATION_SECONDS = 120
DEFAULT_TIMEZONE = "Asia/Tokyo"

WEEKDAY_LABELS_JA = {
    0: "月",
    1: "火",
    2: "水",
    3: "木",
    4: "金",
    5: "土",
    6: "日",
}

RECURRENCE_TYPE_OPTIONS = [
    {"text": {"type": "plain_text", "text": "毎日"}, "value": "daily"},
    {"text": {"type": "plain_text", "text": "平日のみ（月〜金）"}, "value": "weekdays"},
    {"text": {"type": "plain_text", "text": "毎週"}, "value": "weekly"},
    {"text": {"type": "plain_text", "text": "毎週（曜日指定）"}, "value": "weekly_custom"},
    {"text": {"type": "plain_text", "text": "毎N日"}, "value": "every_n_days"},
    {"text": {"type": "plain_text", "text": "毎N週"}, "value": "every_n_weeks"},
    {"text": {"type": "plain_text", "text": "毎月の指定日（1〜31日）"}, "value": "monthly_day"},
    {"text": {"type": "plain_text", "text": "毎月末日"}, "value": "monthly_last_day"},
    {"text": {"type": "plain_text", "text": "毎月第N曜日"}, "value": "monthly_nth_weekday"},
    {"text": {"type": "plain_text", "text": "毎月最終曜日"}, "value": "monthly_last_weekday"},
    {"text": {"type": "plain_text", "text": "毎Nか月"}, "value": "every_n_months"},
]


# ==============================================================================
# テキスト・リッチテキスト抽出ユーティリティ
# ==============================================================================


def extract_plain_text(node):
    """rich_text_input の値からプレーンテキストを生成する。"""
    if isinstance(node, list):
        return "".join(extract_plain_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "emoji":
        return f":{node.get('name', 'emoji')}:"
    if kind == "link":
        return node.get("text") or node.get("url", "")
    if kind == "user":
        return f"<@{node.get('user_id', '')}>"
    if kind == "channel":
        return f"<#{node.get('channel_id', '')}>"
    return "".join(
        extract_plain_text(value)
        for key, value in node.items()
        if key in ("elements", "element", "text", "fields")
    )


def first_emoji_name(node):
    """rich text または :name: 表記から最初の絵文字名を取得する。"""
    if isinstance(node, list):
        for item in node:
            found = first_emoji_name(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    if node.get("type") == "emoji" and node.get("name"):
        return node["name"]
    if node.get("type") == "text":
        match = re.search(r":([A-Za-z0-9_+\-]+):", node.get("text", ""))
        if match:
            return match.group(1)
    for key in ("elements", "element"):
        found = first_emoji_name(node.get(key))
        if found:
            return found
    return None


def get_rich_text_value(values, block_id, action_id):
    try:
        return values[block_id][action_id].get("rich_text_value")
    except (KeyError, TypeError, AttributeError):
        return None


def get_plain_value(values, block_id, action_id):
    try:
        return values[block_id][action_id].get("value")
    except (KeyError, TypeError, AttributeError):
        return None


def get_selected_option_value(values, block_id, action_id):
    try:
        opt = values[block_id][action_id].get("selected_option")
        return opt.get("value") if isinstance(opt, dict) else None
    except (KeyError, TypeError, AttributeError):
        return None


def get_selected_options_values(values, block_id, action_id):
    try:
        opts = values[block_id][action_id].get("selected_options") or []
        return [o.get("value") for o in opts if isinstance(o, dict) and o.get("value")]
    except (KeyError, TypeError, AttributeError):
        return []


def get_datetimepicker_ts(values, block_id, action_id):
    try:
        val = values[block_id][action_id].get("selected_date_time")
        return int(val) if val is not None else None
    except (KeyError, TypeError, ValueError):
        return None


# ==============================================================================
# 繰り返し条件の計算ロジック（独立関数・単体テスト対象）
# ==============================================================================


def _month_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> int | None:
    days = [
        day
        for day in range(1, _month_last_day(year, month) + 1)
        if datetime.date(year, month, day).weekday() == weekday
    ]
    if 1 <= nth <= len(days):
        return days[nth - 1]
    return None


def _last_weekday_of_month(year: int, month: int, weekday: int) -> int:
    for day in range(_month_last_day(year, month), 0, -1):
        if datetime.date(year, month, day).weekday() == weekday:
            return day
    raise RuntimeError("Unreachable")


def _localize_dt(dt_naive: datetime.datetime, tz: ZoneInfo) -> datetime.datetime:
    return dt_naive.replace(tzinfo=tz, fold=0)


def compute_next_run(
    current_run_utc_ts: int,
    rule: dict | None,
    tz_name: str = DEFAULT_TIMEZONE,
    post_count: int = 0,
    anchor_local_iso: str | None = None,
    holiday_dates: set[str] | None = None,
) -> int | None:
    """
    直近実行の UTC タイムスタンプ（秒）から次回実行の UTC タイムスタンプを計算する。
    単発・上限到達・終了日超過の場合は None を返す。
    """
    if not rule or rule.get("type") in (None, "none", "once"):
        return None

    max_count = rule.get("max_count")
    if max_count is not None and post_count >= int(max_count):
        return None

    tz = ZoneInfo(DEFAULT_TIMEZONE)
    cur_local = datetime.datetime.fromtimestamp(
        current_run_utc_ts, tz=datetime.timezone.utc
    ).astimezone(tz)
    time_of_day = cur_local.time().replace(microsecond=0)

    end_date_str = rule.get("end_date")
    end_date = datetime.date.fromisoformat(end_date_str) if end_date_str else None

    if anchor_local_iso:
        anchor_dt = datetime.datetime.fromisoformat(anchor_local_iso)
        anchor_dt = (
            anchor_dt.replace(tzinfo=tz)
            if anchor_dt.tzinfo is None
            else anchor_dt.astimezone(tz)
        )
    else:
        anchor_dt = cur_local

    rtype = rule.get("type")
    candidate_local: datetime.datetime | None = None

    if rtype == "daily":
        next_date = cur_local.date() + datetime.timedelta(days=1)
        candidate_local = _localize_dt(datetime.datetime.combine(next_date, time_of_day), tz)

    elif rtype == "weekdays":
        d = cur_local.date() + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        candidate_local = _localize_dt(datetime.datetime.combine(d, time_of_day), tz)

    elif rtype in ("weekly", "weekly_custom", "every_n_weeks"):
        if rtype == "every_n_weeks":
            interval = max(1, int(rule.get("interval", 1)))
        else:
            interval = 1
        raw_weekdays = rule.get("weekdays")
        if raw_weekdays:
            weekdays = sorted(set(int(w) for w in raw_weekdays))
        else:
            weekdays = [cur_local.weekday()]

        anchor_monday = anchor_dt.date() - datetime.timedelta(days=anchor_dt.weekday())
        max_days = 14 if interval == 1 else 370
        for offset in range(1, max_days):
            d = cur_local.date() + datetime.timedelta(days=offset)
            if interval > 1:
                d_monday = d - datetime.timedelta(days=d.weekday())
                week_diff = (d_monday - anchor_monday).days // 7
                if week_diff < 0 or (week_diff % interval != 0):
                    continue
            if d.weekday() in weekdays:
                candidate_local = _localize_dt(datetime.datetime.combine(d, time_of_day), tz)
                break

    elif rtype == "every_n_days":
        interval = max(1, int(rule.get("interval", 1)))
        next_date = cur_local.date() + datetime.timedelta(days=interval)
        candidate_local = _localize_dt(datetime.datetime.combine(next_date, time_of_day), tz)

    elif rtype == "monthly_day":
        target_day = max(1, min(31, int(rule.get("day", cur_local.day))))
        for m_offset in range(0, 37):
            total_m = (cur_local.year * 12 + (cur_local.month - 1)) + m_offset
            y, m = divmod(total_m, 12)
            m += 1
            actual_day = min(target_day, _month_last_day(y, m))
            cand_dt = _localize_dt(
                datetime.datetime.combine(datetime.date(y, m, actual_day), time_of_day), tz
            )
            if int(cand_dt.timestamp()) > current_run_utc_ts:
                candidate_local = cand_dt
                break

    elif rtype == "monthly_last_day":
        for m_offset in range(0, 37):
            total_m = (cur_local.year * 12 + (cur_local.month - 1)) + m_offset
            y, m = divmod(total_m, 12)
            m += 1
            cand_dt = _localize_dt(
                datetime.datetime.combine(
                    datetime.date(y, m, _month_last_day(y, m)), time_of_day
                ),
                tz,
            )
            if int(cand_dt.timestamp()) > current_run_utc_ts:
                candidate_local = cand_dt
                break

    elif rtype == "monthly_nth_weekday":
        nth = max(1, min(5, int(rule.get("nth", 1))))
        weekday = int(rule.get("weekday", 0))
        for m_offset in range(0, 61):
            total_m = (cur_local.year * 12 + (cur_local.month - 1)) + m_offset
            y, m = divmod(total_m, 12)
            m += 1
            day_num = _nth_weekday_of_month(y, m, weekday, nth)
            if day_num is not None:
                cand_dt = _localize_dt(
                    datetime.datetime.combine(datetime.date(y, m, day_num), time_of_day), tz
                )
                if int(cand_dt.timestamp()) > current_run_utc_ts:
                    candidate_local = cand_dt
                    break

    elif rtype == "monthly_last_weekday":
        weekday = int(rule.get("weekday", 4))
        for m_offset in range(0, 37):
            total_m = (cur_local.year * 12 + (cur_local.month - 1)) + m_offset
            y, m = divmod(total_m, 12)
            m += 1
            day_num = _last_weekday_of_month(y, m, weekday)
            cand_dt = _localize_dt(
                datetime.datetime.combine(datetime.date(y, m, day_num), time_of_day), tz
            )
            if int(cand_dt.timestamp()) > current_run_utc_ts:
                candidate_local = cand_dt
                break

    elif rtype == "every_n_months":
        interval = max(1, int(rule.get("interval", 1)))
        target_day = int(rule.get("day") or anchor_dt.day)
        anchor_ym = anchor_dt.year * 12 + (anchor_dt.month - 1)
        cur_ym = cur_local.year * 12 + (cur_local.month - 1)
        steps = max(1, ((cur_ym - anchor_ym) // interval) + 1)
        for s in range(steps, steps + 120):
            target_ym = anchor_ym + s * interval
            y, m = divmod(target_ym, 12)
            m += 1
            actual_day = min(target_day, _month_last_day(y, m))
            cand_dt = _localize_dt(
                datetime.datetime.combine(datetime.date(y, m, actual_day), time_of_day), tz
            )
            if int(cand_dt.timestamp()) > current_run_utc_ts:
                candidate_local = cand_dt
                break

    else:
        return None

    if candidate_local is None:
        return None

    if holiday_dates and rule.get("skip_holidays"):
        d_curr = candidate_local.date()
        guard = 0
        while d_curr.isoformat() in holiday_dates and guard < 31:
            d_curr += datetime.timedelta(days=1)
            guard += 1
        candidate_local = _localize_dt(
            datetime.datetime.combine(d_curr, time_of_day), tz
        )

    if end_date is not None and candidate_local.date() > end_date:
        return None

    return int(candidate_local.timestamp())


def format_recurrence_summary(rule: dict | None) -> str:
    if not rule or rule.get("type") in (None, "none", "once"):
        return "単発（繰り返しなし）"
    rtype = rule.get("type")
    parts = []
    if rtype == "daily":
        parts.append("毎日")
    elif rtype == "weekdays":
        parts.append("平日のみ（月〜金）")
    elif rtype == "weekly":
        wds = rule.get("weekdays") or []
        if wds:
            labels = "・".join(WEEKDAY_LABELS_JA.get(int(w), "?") for w in wds)
            parts.append(f"毎週（{labels}）")
        else:
            parts.append("毎週")
    elif rtype == "weekly_custom":
        wds = rule.get("weekdays") or []
        labels = "・".join(WEEKDAY_LABELS_JA.get(int(w), "?") for w in wds) or "初回曜日"
        parts.append(f"毎週（曜日指定: {labels}）")
    elif rtype == "every_n_days":
        parts.append(f"{rule.get('interval', 1)}日ごと")
    elif rtype == "every_n_weeks":
        wds = rule.get("weekdays") or []
        labels = "・".join(WEEKDAY_LABELS_JA.get(int(w), "?") for w in wds)
        suffix = f"（{labels}）" if labels else ""
        parts.append(f"{rule.get('interval', 1)}週間ごと{suffix}")
    elif rtype == "monthly_day":
        parts.append(f"毎月{rule.get('day', 1)}日")
    elif rtype == "monthly_last_day":
        parts.append("毎月末日")
    elif rtype == "monthly_nth_weekday":
        nth = rule.get("nth", 1)
        wd = WEEKDAY_LABELS_JA.get(int(rule.get("weekday", 0)), "?")
        parts.append(f"毎月第{nth}{wd}曜日")
    elif rtype == "monthly_last_weekday":
        wd = WEEKDAY_LABELS_JA.get(int(rule.get("weekday", 4)), "?")
        parts.append(f"毎月最終{wd}曜日")
    elif rtype == "every_n_months":
        parts.append(f"{rule.get('interval', 1)}か月ごと")
    else:
        parts.append(str(rtype))

    if rule.get("end_date"):
        parts.append(f"終了日:{rule['end_date']}")
    return " / ".join(parts)


# ==============================================================================
# SQLite 永続化・添付ファイル管理
# ==============================================================================


def init_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_text TEXT NOT NULL,
            rich_text_json TEXT,
            attachments_json TEXT NOT NULL,
            username TEXT,
            icon_emoji TEXT,
            timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
            anchor_local_iso TEXT NOT NULL,
            next_run_utc INTEGER,
            recurrence_json TEXT,
            post_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            locked_until_utc INTEGER NOT NULL DEFAULT 0,
            created_at_utc INTEGER NOT NULL,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_schedules_due
        ON schedules (status, next_run_utc, locked_until_utc)
        """
    )
    conn.execute("UPDATE schedules SET locked_until_utc = 0 WHERE locked_until_utc > 0")
    conn.close()


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def delete_schedule_attachments(schedule_id: str):
    target_dir = ATTACHMENTS_DIR / schedule_id
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)


def persist_slack_files(client, schedule_id: str, selected_files: list[dict]) -> list[dict]:
    """モーダルで選択された一時ファイルをローカル永続領域へ保存し、メタ情報リストを返す。"""
    if not selected_files:
        return []
    if len(selected_files) > MAX_FILES:
        raise ValueError(f"添付できるファイルは最大{MAX_FILES}個です。")

    dest_dir = ATTACHMENTS_DIR / schedule_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    total_bytes = 0
    try:
        for idx, item in enumerate(selected_files):
            file_id = item.get("id")
            if not file_id:
                raise ValueError("添付ファイルのIDを取得できませんでした。")
            filename, content = download_slack_file(client, file_id)
            total_bytes += len(content)
            if total_bytes > MAX_TOTAL_DOWNLOAD:
                raise ValueError("添付ファイルの合計サイズが200 MBを超えています。")

            safe_name = re.sub(r'[\\/:*?"<>|]+', "_", filename) or f"file_{idx}"
            stored_path = dest_dir / f"{idx:02d}_{safe_name}"
            stored_path.write_bytes(content)
            saved.append(
                {
                    "filename": filename,
                    "stored_path": str(stored_path),
                    "size": len(content),
                }
            )
        return saved
    except Exception:
        delete_schedule_attachments(schedule_id)
        raise


def insert_schedule(record: dict):
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO schedules (
                id, creator_id, channel_id, message_text, rich_text_json,
                attachments_json, username, icon_emoji, timezone, anchor_local_iso,
                next_run_utc, recurrence_json, post_count, status,
                locked_until_utc, created_at_utc, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["creator_id"],
                record["channel_id"],
                record["message_text"],
                record.get("rich_text_json"),
                record.get("attachments_json", "[]"),
                record.get("username"),
                record.get("icon_emoji"),
                DEFAULT_TIMEZONE,
                record["anchor_local_iso"],
                record.get("next_run_utc"),
                record.get("recurrence_json"),
                record.get("post_count", 0),
                record.get("status", "active"),
                0,
                record["created_at_utc"],
                record.get("last_error"),
            ),
        )
    finally:
        conn.close()


def list_user_schedules(user_id: str, channel_id: str | None = None, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    try:
        where_clauses = []
        params = []
        if user_id not in ADMIN_USER_IDS:
            where_clauses.append("creator_id = ?")
            params.append(user_id)
        if channel_id:
            where_clauses.append("channel_id = ?")
            params.append(channel_id)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"SELECT * FROM schedules {where_sql} ORDER BY created_at_utc DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_schedule_for_user(schedule_id: str, user_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        if rec["creator_id"] != user_id and user_id not in ADMIN_USER_IDS:
            raise PermissionError("他の利用者の予約を操作する権限がありません。")
        return rec
    finally:
        conn.close()


def update_schedule_fields(schedule_id: str, user_id: str, updates: dict):
    rec = get_schedule_for_user(schedule_id, user_id)
    if not rec:
        raise ValueError("対象の予約が見つかりません。")
    allowed = {
        "channel_id",
        "message_text",
        "rich_text_json",
        "username",
        "icon_emoji",
        "timezone",
        "anchor_local_iso",
        "next_run_utc",
        "recurrence_json",
        "status",
        "last_error",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    params = list(filtered.values()) + [schedule_id]
    conn = _get_conn()
    try:
        conn.execute(f"UPDATE schedules SET {set_clause} WHERE id = ?", params)
    finally:
        conn.close()


def delete_schedule_record(schedule_id: str, user_id: str):
    rec = get_schedule_for_user(schedule_id, user_id)
    if not rec:
        return
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    finally:
        conn.close()
    delete_schedule_attachments(schedule_id)


# ==============================================================================
# Slack 投稿実行（即時送信・予約投稿 共通）
# ==============================================================================


def download_slack_file(client, file_id: str) -> tuple[str, bytes]:
    info = client.files_info(file=file_id)
    file_info = info.get("file", {})
    filename = file_info.get("name") or "attachment"
    download_url = file_info.get("url_private_download") or file_info.get("url_private")
    size = int(file_info.get("size") or 0)

    if size > MAX_FILE_SIZE:
        raise ValueError(f"{filename}: 1ファイルの上限100 MBを超えています。")
    if not download_url:
        raise ValueError(f"{filename}: ダウンロードURLを取得できませんでした。")

    response = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
        timeout=120,
    )
    response.raise_for_status()
    content = response.content
    if len(content) > MAX_FILE_SIZE:
        raise ValueError(f"{filename}: ダウンロード後のサイズが100 MBを超えています。")
    return filename, content


def post_message_or_files(
    client,
    channel_id: str,
    message_text: str,
    rich_message: dict | None,
    username: str | None,
    icon_emoji: str | None,
    file_payloads: list[dict] | None = None,
):
    """
    添付ファイルがある場合は files_upload_v2 + initial_comment で同一トップレベル投稿にまとめる。
    添付ファイルがない場合は chat_postMessage で rich_text ブロックを維持して投稿する。
    """
    if file_payloads:
        uploads = []
        for fp in file_payloads:
            uploads.append(
                {
                    "file": fp["stream"],
                    "filename": fp["filename"],
                    "title": fp["filename"],
                }
            )
        upload_kwargs = {
            "file_uploads": uploads,
            "channel": channel_id,
        }
        if message_text:
            upload_kwargs["initial_comment"] = message_text[:2000]
        client.files_upload_v2(**upload_kwargs)
    elif message_text:
        post_kwargs = {
            "channel": channel_id,
            "text": message_text[:40000],
        }
        if rich_message:
            post_kwargs["blocks"] = [rich_message]
        if username:
            post_kwargs["username"] = username[:80]
        if icon_emoji:
            post_kwargs["icon_emoji"] = icon_emoji
        client.chat_postMessage(**post_kwargs)


def execute_scheduled_row(client, row: dict, now_ts: int):
    """1件の予約投稿を実行し、次回日時・回数・状態を更新する。"""
    schedule_id = row["id"]
    channel_id = row["channel_id"]
    message_text = row["message_text"] or ""
    rich_message = json.loads(row["rich_text_json"]) if row.get("rich_text_json") else None
    attachments = json.loads(row.get("attachments_json") or "[]")
    rule = json.loads(row["recurrence_json"]) if row.get("recurrence_json") else None
    post_count = int(row.get("post_count") or 0)
    anchor_local_iso = row.get("anchor_local_iso")
    scheduled_for_ts = int(row.get("next_run_utc") or now_ts)

    file_streams = []
    file_payloads = []
    try:
        for att in attachments:
            p = Path(att["stored_path"])
            if not p.exists():
                raise FileNotFoundError(f"保存済み添付ファイルが見つかりません: {att.get('filename')}")
            bio = io.BytesIO(p.read_bytes())
            file_streams.append(bio)
            file_payloads.append({"filename": att["filename"], "stream": bio})

        post_message_or_files(
            client=client,
            channel_id=channel_id,
            message_text=message_text,
            rich_message=rich_message,
            username=row.get("username"),
            icon_emoji=row.get("icon_emoji"),
            file_payloads=file_payloads if file_payloads else None,
        )
    finally:
        for s in file_streams:
            s.close()

    new_count = post_count + 1
    next_run = compute_next_run(
        current_run_utc_ts=scheduled_for_ts,
        rule=rule,
        tz_name=DEFAULT_TIMEZONE,
        post_count=new_count,
        anchor_local_iso=anchor_local_iso,
    )

    while next_run is not None and next_run <= now_ts:
        next_run = compute_next_run(
            current_run_utc_ts=next_run,
            rule=rule,
            tz_name=DEFAULT_TIMEZONE,
            post_count=new_count,
            anchor_local_iso=anchor_local_iso,
        )

    new_status = "active" if next_run is not None else "completed"

    conn = _get_conn()
    try:
        conn.execute(
            """
            UPDATE schedules
            SET post_count = ?, next_run_utc = ?, status = ?, locked_until_utc = 0, last_error = NULL
            WHERE id = ?
            """,
            (new_count, next_run, new_status, schedule_id),
        )
    finally:
        conn.close()

    if new_status == "completed" and attachments:
        delete_schedule_attachments(schedule_id)

    logger.info(
        "Executed schedule=%s channel=%s count=%s next_status=%s next_run=%s",
        schedule_id,
        channel_id,
        new_count,
        new_status,
        next_run,
    )


def mark_schedule_error(schedule_id: str, err_msg: str):
    conn = _get_conn()
    try:
        conn.execute(
            """
            UPDATE schedules
            SET status = 'error', locked_until_utc = 0, last_error = ?
            WHERE id = ?
            """,
            (err_msg[:1000], schedule_id),
        )
    finally:
        conn.close()


def scheduler_loop(stop_event: threading.Event):
    """Socket Modeとは独立したバックグラウンドスケジューラー。"""
    logger.info("Background schedule worker started.")
    while not stop_event.is_set():
        now_ts = int(time.time())
        due_rows = []
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM schedules
                WHERE status = 'active'
                  AND next_run_utc IS NOT NULL
                  AND next_run_utc <= ?
                  AND locked_until_utc <= ?
                ORDER BY next_run_utc ASC
                LIMIT 20
                """,
                (now_ts, now_ts),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE schedules SET locked_until_utc = ? WHERE id = ?",
                    (now_ts + LOCK_DURATION_SECONDS, r["id"]),
                )
                due_rows.append(dict(r))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.exception("Error claiming due schedules")
        finally:
            conn.close()

        for row in due_rows:
            try:
                execute_scheduled_row(app.client, row, int(time.time()))
            except SlackApiError as exc:
                err_detail = json.dumps(
                    getattr(exc.response, "data", {}), ensure_ascii=False, default=str
                )
                logger.error("Schedule %s failed with SlackApiError: %s", row["id"], err_detail)
                mark_schedule_error(row["id"], f"SlackApiError: {err_detail}")
            except Exception as exc:
                logger.exception("Schedule %s failed unexpectedly", row["id"])
                mark_schedule_error(row["id"], f"{type(exc).__name__}: {exc}")

        stop_event.wait(SCHEDULER_POLL_SECONDS)


# ==============================================================================
# モーダル UI 構築（/send 統合・予約一覧・編集）
# ==============================================================================


def build_send_view(channel_id: str, send_mode: str = "now", recurrence_type: str = "daily"):
    """
    セクション区切りと動的入力項目の増減を備えた統合モーダル。
    1. 投稿スケジュール設定（送信モード・初回日時・繰り返し条件・動的詳細項目）
    2. メッセージ・添付ファイル設定（投稿先・本文・添付ファイル）
    3. アイコン・表示名設定（表示名・アイコン絵文字）
    """
    mode_option_map = {
        "now": {"text": {"type": "plain_text", "text": "即時送信"}, "value": "now"},
        "once": {"text": {"type": "plain_text", "text": "単発予約"}, "value": "once"},
        "recurring": {"text": {"type": "plain_text", "text": "繰り返し予約"}, "value": "recurring"},
    }
    initial_mode = mode_option_map.get(send_mode, mode_option_map["now"])

    channel_element = {
        "type": "channels_select",
        "action_id": "target_channel",
    }
    if channel_id and channel_id.startswith("C"):
        channel_element["initial_channel"] = channel_id

    blocks = [
        # --- 予約管理ショートカット ---
        {
            "type": "actions",
            "block_id": "manage_actions_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_schedule_list",
                    "text": {"type": "plain_text", "text": "📋 予約一覧・管理を開く"},
                }
            ],
        },
        {"type": "divider"},
        # ======================================================================
        # セクション1: 投稿スケジュールの設定（送信モードとスケジュール設定を隣接配置）
        # ======================================================================
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🕒 投稿スケジュールの設定"},
        },
        {
            "type": "input",
            "block_id": "send_mode_block",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "送信モード"},
            "element": {
                "type": "static_select",
                "action_id": "send_mode",
                "initial_option": initial_mode,
                "options": list(mode_option_map.values()),
            },
        },
    ]

    if send_mode in ("once", "recurring"):
        blocks.append(
            {
                "type": "input",
                "block_id": "schedule_time_block",
                "label": {"type": "plain_text", "text": "初回投稿日時（日本時間 / Asia/Tokyo）"},
                "element": {
                    "type": "datetimepicker",
                    "action_id": "first_run_dt",
                    "initial_date_time": int(time.time()) + 300,
                },
            }
        )

    if send_mode == "recurring":
        rec_opt = next(
            (o for o in RECURRENCE_TYPE_OPTIONS if o["value"] == recurrence_type),
            RECURRENCE_TYPE_OPTIONS[0],
        )
        blocks.append(
            {
                "type": "input",
                "block_id": "recurrence_type_block",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "繰り返し条件"},
                "element": {
                    "type": "static_select",
                    "action_id": "recurrence_type",
                    "initial_option": rec_opt,
                    "options": RECURRENCE_TYPE_OPTIONS,
                },
            }
        )

        # 選択された繰り返し条件に応じて必要な設定ボックスだけを動的に表示
        if recurrence_type in ("every_n_days", "every_n_weeks", "every_n_months"):
            unit_label = {
                "every_n_days": "何日ごと（例: 2）",
                "every_n_weeks": "何週間ごと（例: 2）",
                "every_n_months": "何か月ごと（例: 3）",
            }[recurrence_type]
            blocks.append(
                {
                    "type": "input",
                    "block_id": "recurrence_interval_block",
                    "label": {"type": "plain_text", "text": f"繰り返し間隔（{unit_label}）"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "interval_input",
                        "placeholder": {"type": "plain_text", "text": "1以上の整数を入力"},
                        "max_length": 4,
                    },
                }
            )

        if recurrence_type == "monthly_day":
            blocks.append(
                {
                    "type": "input",
                    "block_id": "recurrence_day_nth_block",
                    "label": {"type": "plain_text", "text": "毎月の指定日（1〜31）"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "day_or_nth_input",
                        "placeholder": {"type": "plain_text", "text": "例: 15（月末を超える月は自動で末日になります）"},
                        "max_length": 2,
                    },
                }
            )

        if recurrence_type == "monthly_nth_weekday":
            blocks.append(
                {
                    "type": "input",
                    "block_id": "recurrence_day_nth_block",
                    "label": {"type": "plain_text", "text": "第N週の指定（1〜5）"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "day_or_nth_input",
                        "placeholder": {"type": "plain_text", "text": "第2週なら 2 を入力"},
                        "max_length": 1,
                    },
                }
            )

        if recurrence_type in ("weekly_custom", "every_n_weeks", "monthly_nth_weekday", "monthly_last_weekday"):
            is_single_wd = recurrence_type in ("monthly_nth_weekday", "monthly_last_weekday")
            wd_label = (
                "対象曜日（1つ選択）"
                if is_single_wd
                else "対象曜日（複数選択可・未選択なら初回日時と同じ曜日）"
            )
            blocks.append(
                {
                    "type": "input",
                    "block_id": "recurrence_weekdays_block",
                    "optional": recurrence_type == "every_n_weeks",
                    "label": {"type": "plain_text", "text": wd_label},
                    "element": {
                        "type": "checkboxes",
                        "action_id": "weekdays_select",
                        "options": [
                            {"text": {"type": "plain_text", "text": f"{lbl}曜"}, "value": str(idx)}
                            for idx, lbl in WEEKDAY_LABELS_JA.items()
                        ],
                    },
                }
            )

        blocks.append(
            {
                "type": "input",
                "block_id": "recurrence_end_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "終了日（任意・YYYY-MM-DD）"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "end_date_input",
                    "placeholder": {"type": "plain_text", "text": "例: 2027-03-31（空欄なら無期限）"},
                    "max_length": 10,
                },
            }
        )

    blocks.extend(
        [
            {"type": "divider"},
            # ==================================================================
            # セクション2: メッセージ・添付ファイルの設定
            # ==================================================================
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "💬 メッセージ・添付ファイルの設定"},
            },
            {
                "type": "input",
                "block_id": "channel_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "投稿先チャンネル（未選択なら実行チャンネル）"},
                "element": channel_element,
            },
            {
                "type": "input",
                "block_id": "message_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "メッセージ本文"},
                "element": {
                    "type": "rich_text_input",
                    "action_id": "message",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "メッセージを入力。絵文字は :smile: などから候補を選べます。",
                    },
                    "min_lines": 3,
                    "max_lines": 12,
                    "focus_on_load": True,
                },
            },
            {
                "type": "input",
                "block_id": "files_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "添付ファイル（最大10個）"},
                "hint": {
                    "type": "plain_text",
                    "text": "画像・動画・音声・PDF等（各最大100MB）。予約時はサーバーに永続保存されます。",
                },
                "element": {
                    "type": "file_input",
                    "action_id": "attachments",
                    "max_files": MAX_FILES,
                },
            },
            {"type": "divider"},
            # ==================================================================
            # セクション3: アイコン・名前の設定
            # ==================================================================
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🎭 アイコン・名前の設定（任意）"},
            },
            {
                "type": "input",
                "block_id": "name_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "表示名（テキスト投稿用）"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "username",
                    "placeholder": {"type": "plain_text", "text": "例：お知らせ"},
                    "max_length": 80,
                },
            },
            {
                "type": "input",
                "block_id": "icon_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "アイコン絵文字（テキスト投稿用）"},
                "element": {
                    "type": "rich_text_input",
                    "action_id": "icon_emoji",
                    "placeholder": {"type": "plain_text", "text": "絵文字を入力して候補から選択"},
                    "min_lines": 1,
                    "max_lines": 2,
                },
            },
        ]
    )

    return {
        "type": "modal",
        "callback_id": "send_submit",
        "private_metadata": json.dumps(
            {
                "default_channel_id": channel_id,
                "send_mode": send_mode,
                "recurrence_type": recurrence_type,
            }
        ),
        "title": {"type": "plain_text", "text": "メッセージ送信・予約"},
        "submit": {"type": "plain_text", "text": "送信" if send_mode == "now" else "予約登録"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "blocks": blocks[:100],
    }


def build_schedule_list_view(user_id: str, channel_id: str | None = None, notice: str | None = None):
    schedules = list_user_schedules(user_id, channel_id=channel_id, limit=15)
    blocks = []
    if notice:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{notice}*"}}
        )
    if not schedules:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "表示できる予約投稿はありません。"},
            }
        )
    else:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        for rec in schedules:
            sid = rec["id"]
            next_ts = rec.get("next_run_utc")
            if next_ts:
                dt_str = (
                    datetime.datetime.fromtimestamp(int(next_ts), tz=datetime.timezone.utc)
                    .astimezone(tz)
                    .strftime("%Y-%m-%d %H:%M")
                )
            else:
                dt_str = "-"
            rule = json.loads(rec["recurrence_json"]) if rec.get("recurrence_json") else None
            rule_str = format_recurrence_summary(rule)
            status = rec.get("status", "active")
            preview = (rec.get("message_text") or "(添付のみ)").replace("\n", " ")[:60]
            err_line = f"\n⚠️ エラー: `{rec['last_error'][:120]}`" if rec.get("last_error") else ""

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*投稿済:* {rec.get('post_count', 0)}回\n"
                            f"*先:* <#{rec['channel_id']}> | *次回:* `{dt_str}` (JST)\n"
                            f"*周期:* {rule_str}\n"
                            f"*本文:* {preview}{err_line}"
                        ),
                    },
                }
            )

            action_elements = [
                {
                    "type": "button",
                    "action_id": "edit_schedule_btn",
                    "value": sid,
                    "text": {"type": "plain_text", "text": "編集"},
                }
            ]
            if status == "active":
                action_elements.append(
                    {
                        "type": "button",
                        "action_id": "pause_schedule_btn",
                        "value": sid,
                        "text": {"type": "plain_text", "text": "一時停止"},
                    }
                )
            elif status in ("paused", "error"):
                action_elements.append(
                    {
                        "type": "button",
                        "action_id": "resume_schedule_btn",
                        "value": sid,
                        "text": {"type": "plain_text", "text": "再開"},
                    }
                )
            action_elements.append(
                {
                    "type": "button",
                    "action_id": "delete_schedule_btn",
                    "value": sid,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "削除"},
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"act_{sid[:20]}",
                    "elements": action_elements,
                }
            )
            blocks.append({"type": "divider"})

    ch_header = {"type": "header", "text": {"type": "plain_text", "text": f"📌 チャンネル限定表示（対象: {channel_id or '全チャンネル'}）"}}
    blocks = [ch_header, {"type": "divider"}] + blocks
    return {
        "type": "modal",
        "callback_id": "schedule_list_modal",
        "private_metadata": json.dumps({"user_id": user_id, "channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "予約投稿一覧・管理"},
        "close": {"type": "plain_text", "text": "閉じる"},
        "blocks": blocks[:100],
    }


def build_schedule_edit_view(rec: dict, user_id: str, channel_id: str | None = None):
    next_ts = rec.get("next_run_utc") or (int(time.time()) + 300)
    rule = json.loads(rec["recurrence_json"]) if rec.get("recurrence_json") else None
    rule_json_str = json.dumps(rule, ensure_ascii=False) if rule else ""

    channel_el = {
        "type": "channels_select",
        "action_id": "edit_channel",
    }
    if rec.get("channel_id", "").startswith("C"):
        channel_el["initial_channel"] = rec["channel_id"]

    return {
        "type": "modal",
        "callback_id": "schedule_edit_submit",
        "private_metadata": json.dumps({"schedule_id": rec["id"], "user_id": user_id, "channel_id": channel_id or rec.get("channel_id")}),
        "title": {"type": "plain_text", "text": "予約の編集"},
        "submit": {"type": "plain_text", "text": "保存"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "blocks": [
            {
                "type": "input",
                "block_id": "edit_channel_block",
                "label": {"type": "plain_text", "text": "投稿先チャンネル"},
                "element": channel_el,
            },
            {
                "type": "input",
                "block_id": "edit_text_block",
                "label": {"type": "plain_text", "text": "メッセージ本文"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_text",
                    "multiline": True,
                    "initial_value": rec.get("message_text") or "",
                    "max_length": 4000,
                },
            },
            {
                "type": "input",
                "block_id": "edit_time_block",
                "label": {"type": "plain_text", "text": "次回投稿日時（日本時間）"},
                "element": {
                    "type": "datetimepicker",
                    "action_id": "edit_next_dt",
                    "initial_date_time": int(next_ts),
                },
            },
            {
                "type": "input",
                "block_id": "edit_rule_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "繰り返しルール JSON（空欄で単発）"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_rule_json",
                    "initial_value": rule_json_str,
                    "max_length": 1000,
                },
            },
        ],
    }


# ==============================================================================
# コマンド・モーダル・アクション ハンドラー
# ==============================================================================


@app.command("/send")
def send_command(ack, body, command, client, logger):
    ack()
    channel_id = command.get("channel_id", "")
    raw = (command.get("text") or "").strip()
    mention = re.match(r"^<#([A-Z0-9]+)(?:\|[^>]+)?>", raw)
    if mention:
        channel_id = mention.group(1)

    try:
        view = build_send_view(channel_id=channel_id, send_mode="now", recurrence_type="daily")
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except SlackApiError as exc:
        logger.error(
            "Could not open /send modal: %s",
            json.dumps(getattr(exc.response, "data", {}), ensure_ascii=False, default=str),
        )
    except Exception:
        logger.exception("Unexpected error opening /send modal")


@app.action("send_mode")
def handle_send_mode_change(ack, body, client, logger):
    ack()
    view = body.get("view", {})
    meta = json.loads(view.get("private_metadata") or "{}")
    values = view.get("state", {}).get("values", {})
    new_mode = get_selected_option_value(values, "send_mode_block", "send_mode") or "now"
    rec_type = (
        get_selected_option_value(values, "recurrence_type_block", "recurrence_type")
        or meta.get("recurrence_type")
        or "daily"
    )
    ch = meta.get("default_channel_id") or ""
    try:
        client.views_update(
            view_id=view["id"],
            hash=view["hash"],
            view=build_send_view(channel_id=ch, send_mode=new_mode, recurrence_type=rec_type),
        )
    except Exception:
        logger.exception("Failed updating send modal mode")


@app.action("recurrence_type")
def handle_recurrence_type_change(ack, body, client, logger):
    ack()
    view = body.get("view", {})
    meta = json.loads(view.get("private_metadata") or "{}")
    values = view.get("state", {}).get("values", {})
    mode = (
        get_selected_option_value(values, "send_mode_block", "send_mode")
        or meta.get("send_mode")
        or "recurring"
    )
    rec_type = get_selected_option_value(values, "recurrence_type_block", "recurrence_type") or "daily"
    ch = meta.get("default_channel_id") or ""
    try:
        client.views_update(
            view_id=view["id"],
            hash=view["hash"],
            view=build_send_view(channel_id=ch, send_mode=mode, recurrence_type=rec_type),
        )
    except Exception:
        logger.exception("Failed updating recurrence type")


@app.action("open_schedule_list")
def handle_open_schedule_list(ack, body, client, logger):
    ack()
    user_id = body.get("user", {}).get("id", "")
    view = body.get("view", {})
    meta = json.loads(view.get("private_metadata") or "{}")
    values = view.get("state", {}).get("values", {})
    selected_ch = values.get("channel_block", {}).get("target_channel", {}).get("selected_channel")
    target_ch = selected_ch or meta.get("default_channel_id") or None
    try:
        client.views_update(
            view_id=view["id"],
            hash=view["hash"],
            view=build_schedule_list_view(user_id=user_id, channel_id=target_ch),
        )
    except Exception:
        logger.exception("Failed opening schedule list modal")


@app.action("pause_schedule_btn")
def handle_pause_schedule(ack, body, client, logger):
    ack()
    user_id = body.get("user", {}).get("id", "")
    sid = body.get("actions", [{}])[0].get("value", "")
    view = body.get("view", {})
    ch_id = json.loads(view.get("private_metadata") or "{}").get("channel_id")
    try:
        update_schedule_fields(sid, user_id, {"status": "paused"})
        notice = f"予約 `{sid}` を一時停止しました。"
    except PermissionError as exc:
        notice = str(exc)
    except Exception:
        logger.exception("Failed pausing schedule %s", sid)
        notice = "一時停止に失敗しました。"
    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice=notice),
    )


@app.action("resume_schedule_btn")
def handle_resume_schedule(ack, body, client, logger):
    ack()
    user_id = body.get("user", {}).get("id", "")
    sid = body.get("actions", [{}])[0].get("value", "")
    view = body.get("view", {})
    try:
        rec = get_schedule_for_user(sid, user_id)
        if rec:
            now_ts = int(time.time())
            next_ts = rec.get("next_run_utc")
            if not next_ts or int(next_ts) < now_ts:
                rule = json.loads(rec["recurrence_json"]) if rec.get("recurrence_json") else None
                if rule:
                    tz = ZoneInfo(DEFAULT_TIMEZONE)
                    local_now = datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc).astimezone(tz)
                    cur_dt = datetime.datetime.fromtimestamp(int(next_ts or now_ts), tz=datetime.timezone.utc).astimezone(tz)
                    aligned = _localize_dt(
                        datetime.datetime.combine(local_now.date(), cur_dt.time().replace(microsecond=0)),
                        tz,
                    )
                    base_ts = int(aligned.timestamp())
                    if base_ts <= now_ts:
                        base_ts = now_ts
                    next_ts = compute_next_run(
                        current_run_utc_ts=base_ts,
                        rule=rule,
                        tz_name=DEFAULT_TIMEZONE,
                        post_count=int(rec.get("post_count") or 0),
                        anchor_local_iso=rec.get("anchor_local_iso"),
                    ) or (now_ts + 60)
                else:
                    next_ts = now_ts + 60
            update_schedule_fields(sid, user_id, {"status": "active", "next_run_utc": next_ts, "last_error": None})
            notice = f"予約 `{sid}` を再開しました。"
        else:
            notice = "対象の予約が見つかりません。"
    except PermissionError as exc:
        notice = str(exc)
    except Exception:
        logger.exception("Failed resuming schedule %s", sid)
        notice = "再開に失敗しました。"
    ch_id = json.loads(view.get("private_metadata") or "{}").get("channel_id")
    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice=notice),
    )


@app.action("delete_schedule_btn")
def handle_delete_schedule(ack, body, client, logger):
    ack()
    user_id = body.get("user", {}).get("id", "")
    sid = body.get("actions", [{}])[0].get("value", "")
    view = body.get("view", {})
    ch_id = json.loads(view.get("private_metadata") or "{}").get("channel_id")
    try:
        delete_schedule_record(sid, user_id)
        notice = f"予約 `{sid}` を削除しました。"
    except PermissionError as exc:
        notice = str(exc)
    except Exception:
        logger.exception("Failed deleting schedule %s", sid)
        notice = "削除に失敗しました。"
    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice=notice),
    )


@app.action("edit_schedule_btn")
def handle_edit_schedule(ack, body, client, logger):
    ack()
    user_id = body.get("user", {}).get("id", "")
    sid = body.get("actions", [{}])[0].get("value", "")
    view = body.get("view", {})
    ch_id = json.loads(view.get("private_metadata") or "{}").get("channel_id")
    try:
        rec = get_schedule_for_user(sid, user_id)
        if not rec:
            client.views_update(
                view_id=view["id"],
                hash=view["hash"],
                view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice="対象の予約が見つかりません。"),
            )
            return
        client.views_update(
            view_id=view["id"],
            hash=view["hash"],
            view=build_schedule_edit_view(rec, user_id=user_id, channel_id=ch_id),
        )
    except PermissionError as exc:
        client.views_update(
            view_id=view["id"],
            hash=view["hash"],
            view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice=str(exc)),
        )
    except Exception:
        logger.exception("Failed opening edit view for %s", sid)


@app.view("schedule_edit_submit")
def handle_schedule_edit_submit(ack, body, view, logger):
    values = view.get("state", {}).get("values", {})
    meta = json.loads(view.get("private_metadata") or "{}")
    sid = meta.get("schedule_id")
    user_id = meta.get("user_id") or body.get("user", {}).get("id", "")
    ch_id = meta.get("channel_id")

    ch = values.get("edit_channel_block", {}).get("edit_channel", {}).get("selected_channel")
    msg_text = (get_plain_value(values, "edit_text_block", "edit_text") or "").strip()
    next_ts = get_datetimepicker_ts(values, "edit_time_block", "edit_next_dt")
    rule_raw = (get_plain_value(values, "edit_rule_block", "edit_rule_json") or "").strip()

    if not next_ts:
        ack(response_action="errors", errors={"edit_time_block": "次回投稿日時を指定してください。"})
        return

    parsed_rule = None
    if rule_raw:
        try:
            parsed_rule = json.loads(rule_raw)
            if not isinstance(parsed_rule, dict) or "type" not in parsed_rule:
                raise ValueError("type キーを含む JSON オブジェクトを指定してください。")
        except Exception as exc:
            ack(response_action="errors", errors={"edit_rule_block": f"JSON形式エラー: {exc}"})
            return

    ack(
        response_action="update",
        view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice="更新処理中..."),
    )
    try:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        anchor_iso = (
            datetime.datetime.fromtimestamp(int(next_ts), tz=datetime.timezone.utc)
            .astimezone(tz)
            .isoformat()
        )
        update_schedule_fields(
            sid,
            user_id,
            {
                "channel_id": ch,
                "message_text": msg_text,
                "rich_text_json": None,
                "next_run_utc": int(next_ts),
                "timezone": DEFAULT_TIMEZONE,
                "anchor_local_iso": anchor_iso,
                "recurrence_json": json.dumps(parsed_rule, ensure_ascii=False) if parsed_rule else None,
                "last_error": None,
            },
        )
        notice = f"予約 `{sid}` を更新しました。"
    except PermissionError as exc:
        notice = str(exc)
    except Exception:
        logger.exception("Failed updating schedule %s", sid)
        notice = "予約の更新に失敗しました。"

    try:
        app.client.views_update(
            view_id=view["id"],
            view=build_schedule_list_view(user_id=user_id, channel_id=ch_id, notice=notice),
        )
    except Exception:
        logger.exception("Failed refreshing schedule list after edit")


def _build_recurrence_rule_from_values(values: dict, recurrence_type: str) -> dict:
    rule: dict = {"type": recurrence_type}
    interval_str = (get_plain_value(values, "recurrence_interval_block", "interval_input") or "").strip()
    day_nth_str = (get_plain_value(values, "recurrence_day_nth_block", "day_or_nth_input") or "").strip()
    end_date_str = (get_plain_value(values, "recurrence_end_block", "end_date_input") or "").strip()
    selected_wds = [int(x) for x in get_selected_options_values(values, "recurrence_weekdays_block", "weekdays_select")]

    if recurrence_type in ("every_n_days", "every_n_weeks", "every_n_months"):
        if not interval_str.isdigit() or int(interval_str) < 1:
            raise ValueError("間隔Nには1以上の整数を入力してください。")
        rule["interval"] = int(interval_str)

    if recurrence_type in ("weekly", "weekly_custom", "every_n_weeks"):
        if selected_wds:
            rule["weekdays"] = sorted(set(selected_wds))
        elif recurrence_type == "weekly_custom":
            raise ValueError("曜日を1つ以上選択してください。")

    if recurrence_type == "monthly_day":
        if not day_nth_str.isdigit() or not (1 <= int(day_nth_str) <= 31):
            raise ValueError("毎月の指定日には 1〜31 の整数を入力してください。")
        rule["day"] = int(day_nth_str)

    if recurrence_type == "monthly_nth_weekday":
        if not day_nth_str.isdigit() or not (1 <= int(day_nth_str) <= 5):
            raise ValueError("第N週には 1〜5 の整数を入力してください。")
        if len(selected_wds) != 1:
            raise ValueError("対象曜日を1つだけ選択してください。")
        rule["nth"] = int(day_nth_str)
        rule["weekday"] = selected_wds[0]

    if recurrence_type == "monthly_last_weekday":
        if len(selected_wds) != 1:
            raise ValueError("対象曜日を1つだけ選択してください。")
        rule["weekday"] = selected_wds[0]

    if end_date_str:
        try:
            datetime.date.fromisoformat(end_date_str)
            rule["end_date"] = end_date_str
        except ValueError as exc:
            raise ValueError("終了日は YYYY-MM-DD 形式で入力してください。") from exc

    return rule


@app.view("send_submit")
def handle_send_submit(ack, body, view, client, logger):
    values = view.get("state", {}).get("values", {})
    metadata = json.loads(view.get("private_metadata") or "{}")
    user_id = body.get("user", {}).get("id", "unknown")

    send_mode = (
        get_selected_option_value(values, "send_mode_block", "send_mode")
        or metadata.get("send_mode")
        or "now"
    )
    selected_ch = values.get("channel_block", {}).get("target_channel", {}).get("selected_channel")
    channel_id = selected_ch or metadata.get("default_channel_id")

    rich_message = get_rich_text_value(values, "message_block", "message")
    message_text = extract_plain_text(rich_message).strip() if rich_message else ""
    username = (get_plain_value(values, "name_block", "username") or "").strip() or None

    icon_rich = get_rich_text_value(values, "icon_block", "icon_emoji")
    icon_name = first_emoji_name(icon_rich)
    icon_emoji = f":{icon_name}:" if icon_name else None

    file_state = values.get("files_block", {}).get("attachments", {})
    selected_files = file_state.get("files", []) or []

    if not channel_id:
        ack(response_action="errors", errors={"message_block": "投稿先チャンネルを確認できません。"})
        return
    if not message_text and not selected_files:
        ack(
            response_action="errors",
            errors={"message_block": "メッセージを入力するか、ファイルを添付してください。"},
        )
        return
    if len(selected_files) > MAX_FILES:
        ack(
            response_action="errors",
            errors={"files_block": f"添付できるファイルは最大{MAX_FILES}個です。"},
        )
        return

    first_run_ts = None
    recurrence_rule = None

    if send_mode in ("once", "recurring"):
        first_run_ts = get_datetimepicker_ts(values, "schedule_time_block", "first_run_dt")
        if not first_run_ts:
            ack(
                response_action="errors",
                errors={"schedule_time_block": "初回投稿日時を選択してください。"},
            )
            return
        if first_run_ts <= int(time.time()) + 15:
            ack(
                response_action="errors",
                errors={"schedule_time_block": "初回投稿日時は現在より15秒以上先にしてください。"},
            )
            return

    if send_mode == "recurring":
        rec_type = (
            get_selected_option_value(values, "recurrence_type_block", "recurrence_type")
            or metadata.get("recurrence_type")
            or "daily"
        )
        try:
            recurrence_rule = _build_recurrence_rule_from_values(values, rec_type)
        except ValueError as exc:
            err_msg = str(exc)
            if "間隔" in err_msg and "recurrence_interval_block" in values:
                ack(response_action="errors", errors={"recurrence_interval_block": err_msg})
            elif ("指定日" in err_msg or "第N週" in err_msg) and "recurrence_day_nth_block" in values:
                ack(response_action="errors", errors={"recurrence_day_nth_block": err_msg})
            elif "曜日" in err_msg and "recurrence_weekdays_block" in values:
                ack(response_action="errors", errors={"recurrence_weekdays_block": err_msg})
            elif "終了日" in err_msg and "recurrence_end_block" in values:
                ack(response_action="errors", errors={"recurrence_end_block": err_msg})
            else:
                ack(response_action="errors", errors={"recurrence_type_block": err_msg})
            return

    ack()

    if send_mode == "now":
        streams = []
        try:
            file_payloads = None
            if selected_files:
                file_payloads = []
                total_bytes = 0
                for item in selected_files:
                    fid = item.get("id")
                    if not fid:
                        raise ValueError("添付ファイルのIDを取得できませんでした。")
                    fname, content = download_slack_file(client, fid)
                    total_bytes += len(content)
                    if total_bytes > MAX_TOTAL_DOWNLOAD:
                        raise ValueError("添付ファイルの合計サイズが200 MBを超えています。")
                    bio = io.BytesIO(content)
                    streams.append(bio)
                    file_payloads.append({"filename": fname, "stream": bio})

            post_message_or_files(
                client=client,
                channel_id=channel_id,
                message_text=message_text,
                rich_message=rich_message,
                username=username,
                icon_emoji=icon_emoji,
                file_payloads=file_payloads,
            )
            logger.info("Immediate post completed in %s by %s", channel_id, user_id)
        except ValueError as exc:
            logger.warning("Immediate post rejected: %s", exc)
            try:
                client.chat_postMessage(channel=user_id, text=f"投稿できませんでした: {exc}")
            except Exception:
                logger.exception("Could not notify user")
        except SlackApiError as exc:
            logger.error(
                "Immediate post failed: %s",
                json.dumps(getattr(exc.response, "data", {}), ensure_ascii=False, default=str),
            )
        except Exception:
            logger.exception("Unexpected error during immediate post")
        finally:
            for s in streams:
                s.close()
        return

    # 単発予約 または 繰り返し予約（タイムゾーンは Asia/Tokyo 固定）
    schedule_id = f"sch_{uuid.uuid4().hex[:10]}"
    now_ts = int(time.time())
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    anchor_iso = (
        datetime.datetime.fromtimestamp(int(first_run_ts), tz=datetime.timezone.utc)
        .astimezone(tz)
        .isoformat()
    )

    try:
        saved_attachments = persist_slack_files(client, schedule_id, selected_files)
        record = {
            "id": schedule_id,
            "creator_id": user_id,
            "channel_id": channel_id,
            "message_text": message_text,
            "rich_text_json": json.dumps(rich_message, ensure_ascii=False) if rich_message else None,
            "attachments_json": json.dumps(saved_attachments, ensure_ascii=False),
            "username": username,
            "icon_emoji": icon_emoji,
            "timezone": DEFAULT_TIMEZONE,
            "anchor_local_iso": anchor_iso,
            "next_run_utc": int(first_run_ts),
            "recurrence_json": json.dumps(recurrence_rule, ensure_ascii=False) if recurrence_rule else None,
            "post_count": 0,
            "status": "active",
            "created_at_utc": now_ts,
            "last_error": None,
        }
        insert_schedule(record)
        logger.info("Registered schedule=%s by user=%s channel=%s", schedule_id, user_id, channel_id)
    except Exception as exc:
        logger.exception("Failed registering schedule %s", schedule_id)
        delete_schedule_attachments(schedule_id)
        try:
            client.chat_postMessage(
                channel=user_id,
                text=f"予約投稿の登録に失敗しました: {exc}",
            )
        except Exception:
            logger.exception("Could not notify user about schedule failure")


@app.command("/image")
def deprecated_image_command(ack, respond):
    ack()
    respond(
        response_type="ephemeral",
        text="画像投稿は `/send` に統合されました。今後は `/send` を使ってください。",
    )


if __name__ == "__main__":
    init_storage()
    stop_event = threading.Event()
    worker_thread = threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True)
    worker_thread.start()
    logger.info("Starting Slack Message Sender with Socket Mode and background scheduler.")
    try:
        SocketModeHandler(app, APP_TOKEN).start()
    finally:
        stop_event.set()
