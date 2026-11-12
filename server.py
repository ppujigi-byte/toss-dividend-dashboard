#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토스증권 배당·매매 통합 수익 대시보드 로컬 서버 (127.0.0.1 전용 · 100% 열람 전용)
- 암호화 키 보관: 브라우저 WebCrypto(PBKDF2-SHA256 600,000회 + AES-256-GCM)로 암호화된 데이터만 data/encrypted_config.json에 보관
- 포트폴리오 저장소: 로컬 SQLite (data/portfolio.db)
- 과거 원장 + 증분 동기화 하이브리드:
  * PDF 기준일 이전: 거래내역서 PDF 확정 원장(매매·타사대체입고·분할병합·실수령 배당·세금재정산) 고정 보관
  * 기준일 이후: 토스증권 공식 Open API 조회 전용 증분 동기화 + 신규 배당 자동 산출
- 타사증권사 이관 전 누적 배당금 '통입력(PRIOR_BROKER)' 영구 합산 지원
"""

import base64
import concurrent.futures
import datetime
import gzip
import html.parser
import http.server
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import import_pdfs

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
PDF_DIR = BASE_DIR / "pdfs"
ENCRYPTED_CONFIG_PATH = DATA_DIR / "encrypted_config.json"
DB_PATH = DATA_DIR / "portfolio.db"

TOSS_DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
HOST = "127.0.0.1"
PORT = int(os.environ.get("TOSS_DASHBOARD_PORT", "8765"))


class _UsEasternFallbackTz(datetime.tzinfo):
    """Windows 기본 Python(tzdata 미설치 환경) 호환용 미국 동부시간(EST/EDT) 폴백"""

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> int:
        first_wd = datetime.date(year, month, 1).weekday()  # Mon=0..Sun=6
        first_sun = 1 + ((6 - first_wd) % 7)
        return first_sun + (n - 1) * 7

    def _is_dst(self, dt: Optional[datetime.datetime]) -> bool:
        if dt is None:
            return False
        yr = dt.year
        dst_start = datetime.datetime(yr, 3, self._nth_sunday(yr, 3, 2), 2, 0)
        dst_end = datetime.datetime(yr, 11, self._nth_sunday(yr, 11, 1), 2, 0)
        naive = dt.replace(tzinfo=None)
        return dst_start <= naive < dst_end

    def utcoffset(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return datetime.timedelta(hours=-4 if self._is_dst(dt) else -5)

    def dst(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return datetime.timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: Optional[datetime.datetime]) -> str:
        return "EDT" if self._is_dst(dt) else "EST"


def _safe_zoneinfo(key: str) -> datetime.tzinfo:
    try:
        return ZoneInfo(key)
    except Exception:
        if key == "Asia/Seoul":
            return datetime.timezone(datetime.timedelta(hours=9), name="KST")
        if key == "America/New_York":
            return _UsEasternFallbackTz()
        return datetime.timezone.utc


KST_TZ = _safe_zoneinfo("Asia/Seoul")
NY_TZ = _safe_zoneinfo("America/New_York")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
DB_LOCK = threading.Lock()

PRIOR_YEARLY_SYMBOLS: Dict[str, Tuple[str, str, str]] = {
    "PRIOR_YEARLY_KR": ("타사 연도별 배당 합산 (종목 미상 · 국내)", "KR", "KRW"),
    "PRIOR_YEARLY_US": ("타사 연도별 배당 합산 (종목 미상 · 미국)", "US", "USD"),
}

# 수동·타사 입력분 등 환율 미기록 미국 배당용 연평균 USD/KRW 기준환율 폴백 테이블
ANNUAL_AVG_USDKRW: Dict[int, float] = {
    2018: 1100.6,
    2019: 1165.7,
    2020: 1180.1,
    2021: 1144.4,
    2022: 1292.0,
    2023: 1305.4,
    2024: 1363.5,
    2025: 1415.0,
    2026: 1378.0,
}


def _fallback_usdkrw_rate(pay_date: str) -> float:
    try:
        yr = int(str(pay_date or "")[:4])
        return ANNUAL_AVG_USDKRW.get(yr, 1378.0)
    except ValueError:
        return 1378.0


def mask_account(val: str) -> str:
    if not val:
        return ""
    s = str(val).strip()
    if len(s) <= 4:
        return "****"
    return f"{s[:3]}••••{s[-2:]}"


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        cleaned = re.sub(r"[^\d.\-+eE]", "", str(val).replace(",", ""))
        if cleaned in ("", "-", "+", "."):
            return default
        return float(cleaned)
    except (ValueError, TypeError):
        return default


def _infer_market_and_currency(symbol: str, market_hint: str = "", currency_hint: str = "") -> Tuple[str, str]:
    mkt = (market_hint or "").upper().strip()
    cur = (currency_hint or "").upper().strip()
    sym = (symbol or "").strip().upper()
    if mkt in ("KR", "KRX", "KOSPI", "KOSDAQ", "KONEX", "KOREA") or cur == "KRW":
        return "KR", "KRW"
    if mkt in ("US", "NASDAQ", "NYSE", "AMEX", "USA") or cur == "USD":
        return "US", "USD"
    clean_kr = sym[1:] if (len(sym) == 7 and sym.startswith("A")) else sym
    if re.fullmatch(r"\d{6}", clean_kr) or re.fullmatch(r"\d{2}[0-9A-Z]{4}", clean_kr):
        return "KR", "KRW"
    return "US", "USD"


def _normalize_trade_date(raw_dt: str, market: str) -> str:
    s = (raw_dt or "").strip()
    if not s:
        return datetime.date.today().isoformat()
    if len(s) == 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    try:
        iso_s = s.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso_s)
        if dt.tzinfo is not None:
            target_tz = NY_TZ if market == "US" else KST_TZ
            return dt.astimezone(target_tz).date().isoformat()
        return dt.date().isoformat()
    except Exception:
        return s[:10] if len(s) >= 10 else datetime.date.today().isoformat()


def _add_business_days(iso_date: str, days: int = 2) -> str:
    try:
        dt = datetime.date.fromisoformat(iso_date[:10])
    except ValueError:
        return iso_date[:10]
    added = 0
    while added < days:
        dt += datetime.timedelta(days=1)
        if dt.weekday() < 5:
            added += 1
    return dt.isoformat()


def _subtract_business_days(iso_date: str, days: int = 5) -> str:
    try:
        dt = datetime.date.fromisoformat(iso_date[:10])
    except ValueError:
        return iso_date[:10]
    removed = 0
    while removed < days:
        dt -= datetime.timedelta(days=1)
        if dt.weekday() < 5:
            removed += 1
    return dt.isoformat()


def _estimate_settlement_date(exec_date_iso: str, market: str) -> str:
    """체결일(OpenAPI 기준)로부터 한국시간 거래내역서 PDF상 결제일(+2영업일) 추정"""
    return _add_business_days(exec_date_iso, 2)


def _match_order_in_pdf_rows(
    od_sym: str,
    od_side: str,
    tr_dt: str,
    od_qty: float,
    od_price: float,
    cur: str,
    boundary_pdf_rows: List[Dict[str, Any]],
    matched_pdf_ids: set,
) -> bool:
    """
    체결일(tr_dt) 기준 주문이 거래내역서 PDF 경계 구간(결제일 tr_dt ~ tr_dt+6일)에
    이미 결제 반영되어 있는지 정밀 대조 (단건 일치 및 동일 결제일 분할체결 합산 지원)
    """
    try:
        od_dt_obj = datetime.date.fromisoformat(tr_dt[:10])
    except ValueError:
        return False

    qty_tol = max(1e-5, od_qty * 1e-4)
    price_tol = max(0.03 if cur == "USD" else 10.0, od_price * 0.008) if od_price > 0 else 0.0

    cands: List[Dict[str, Any]] = []
    for pr in boundary_pdf_rows:
        if pr["id"] in matched_pdf_ids:
            continue
        if pr["symbol"] != od_sym or pr["side"] != od_side:
            continue
        try:
            pr_dt_obj = datetime.date.fromisoformat(str(pr["trade_date"])[:10])
        except ValueError:
            continue
        diff_days = (pr_dt_obj - od_dt_obj).days
        if 0 <= diff_days <= 6:
            cands.append(pr)

    if not cands:
        return False

    for pr in cands:
        pr_qty = float(pr["quantity"])
        pr_price = float(pr["price"])
        if abs(pr_qty - od_qty) <= qty_tol:
            if od_price <= 0 or pr_price <= 0 or abs(pr_price - od_price) <= price_tol:
                matched_pdf_ids.add(pr["id"])
                return True

    by_settle_dt: Dict[str, List[Dict[str, Any]]] = {}
    for pr in cands:
        by_settle_dt.setdefault(str(pr["trade_date"])[:10], []).append(pr)

    for _, day_rows in sorted(by_settle_dt.items()):
        if len(day_rows) < 2 or len(day_rows) > 12:
            continue
        n_rows = len(day_rows)

        def _dfs(start_idx: int, picked: List[Dict[str, Any]], q_sum: float, amt_sum: float) -> Optional[List[Dict[str, Any]]]:
            if len(picked) >= 2 and abs(q_sum - od_qty) <= qty_tol:
                avg_p = (amt_sum / q_sum) if q_sum > 0 else 0.0
                if od_price <= 0 or avg_p <= 0 or abs(avg_p - od_price) <= price_tol:
                    return list(picked)
            if len(picked) >= 6 or q_sum > od_qty + qty_tol:
                return None
            for i in range(start_idx, n_rows):
                r_item = day_rows[i]
                rq = float(r_item["quantity"])
                rp = float(r_item["price"])
                picked.append(r_item)
                found = _dfs(i + 1, picked, q_sum + rq, amt_sum + rq * rp)
                if found is not None:
                    return found
                picked.pop()
            return None

        matched_subset = _dfs(0, [], 0.0, 0.0)
        if matched_subset:
            for mr in matched_subset:
                matched_pdf_ids.add(mr["id"])
            return True

    return False


def _calc_kr_dividend_tax(gross_krw: float) -> int:
    """국내 배당소득세(소득세 14% + 지방소득세 1.4%, 각 10원 미만 절사·소액부징수 반영)"""
    g = int(round(gross_krw))
    if g <= 0:
        return 0
    income_tax = int((g * 0.14) // 10 * 10)
    if income_tax < 10:
        return 0
    local_tax = int((income_tax * 0.1) // 10 * 10)
    return income_tax + local_tax


# ============================================================================
# 1. SQLite 데이터베이스 관리 & 심볼·계좌키 정규화
# ============================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with DB_LOCK:
        conn = get_db()
        try:
            import_pdfs.init_schema(conn)
            _backfill_initial_sync_logs_if_empty(conn)
            conn.commit()
        finally:
            conn.close()


def append_sync_log(
    account_key: str,
    action_type: str,
    status: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> int:
    ts = created_at or datetime.datetime.now(KST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    payload_str = json.dumps(details or {}, ensure_ascii=False)
    with DB_LOCK:
        conn = get_db()
        try:
            eff_key = resolve_and_migrate_account_key(conn, account_key)
            cur = conn.execute(
                """
                INSERT INTO sync_logs (account_key, action_type, status, created_at, summary, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (eff_key, action_type, status, ts, summary, payload_str),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def _utc_sql_ts_to_kst(raw_ts: str) -> str:
    s = (raw_ts or "").strip()
    if not s:
        return datetime.datetime.now(KST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt_utc = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return dt_utc.astimezone(KST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s[:19]


def _backfill_initial_sync_logs_if_empty(conn: sqlite3.Connection) -> None:
    """로그 기능 도입 전에 이미 동기화되어 있던 API 체결 및 AUTO 배당 내역을 초기 로그로 복원"""
    existing_cnt = conn.execute("SELECT COUNT(*) FROM sync_logs WHERE account_key != 'DEMO'").fetchone()[0]
    if existing_cnt > 0:
        return

    api_trades = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM trades WHERE source = 'API' AND account_key != 'DEMO' ORDER BY created_at ASC, id ASC"
        ).fetchall()
    ]
    auto_divs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM dividends WHERE source = 'AUTO' AND account_key != 'DEMO' ORDER BY created_at ASC, id ASC"
        ).fetchall()
    ]
    if not api_trades and not auto_divs:
        return

    pdf_cutoff = get_pdf_cutoff_date(conn)
    grouped_trades: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for tr in api_trades:
        kst_min = _utc_sql_ts_to_kst(str(tr.get("created_at") or ""))[:16]
        grouped_trades.setdefault((str(tr["account_key"]), kst_min), []).append(tr)

    events_to_insert: List[Tuple[str, str, str, str, Dict[str, Any]]] = []
    for (acc_k, _), tr_list in sorted(grouped_trades.items(), key=lambda kv: kv[0][1]):
        kst_full = _utc_sql_ts_to_kst(str(tr_list[-1].get("created_at") or ""))
        h_cnt = conn.execute("SELECT COUNT(*) FROM holdings_snapshot WHERE account_key = ?", (acc_k,)).fetchone()[0]
        formatted_trs = [
            {
                "symbol": t["symbol"],
                "name": t["name"],
                "market": t["market"],
                "currency": t["currency"],
                "side": t["side"],
                "tradeDate": t["trade_date"],
                "quantity": float(t["quantity"]),
                "price": float(t["price"]),
                "grossAmount": float(t["gross_amount"]),
                "commission": float(t["commission"]),
                "tax": float(t["tax"]),
                "memo": t["memo"] or "",
            }
            for t in tr_list
        ]
        summary_txt = f"토스 API 증분 동기화 완료 · 실시간 보유 {h_cnt}종목 · 신규 체결 {len(formatted_trs)}건 추가"
        events_to_insert.append(
            (
                kst_full,
                acc_k,
                "TOSS_SYNC",
                summary_txt,
                {
                    "holdingsCount": h_cnt,
                    "newTradesSaved": len(formatted_trs),
                    "updatedTrades": 0,
                    "skippedAlreadyInPdf": 0,
                    "pdfCutoffDate": pdf_cutoff,
                    "addedTrades": formatted_trs,
                    "updatedTradesList": [],
                    "newlyAddedDividends": [],
                    "retainedAutoDividends": [],
                },
            )
        )

    if auto_divs:
        acc_k = str(auto_divs[-1]["account_key"])
        last_div_meta = conn.execute("SELECT value FROM app_meta WHERE key = ?", (f"last_div_sync_{acc_k}",)).fetchone()
        kst_div_ts = str(last_div_meta["value"]) if (last_div_meta and last_div_meta["value"]) else _utc_sql_ts_to_kst(str(auto_divs[-1].get("created_at") or ""))
        formatted_divs = [
            {
                "symbol": d["symbol"],
                "name": d["name"],
                "market": d["market"],
                "currency": d["currency"],
                "payDate": d["pay_date"],
                "grossAmount": float(d["gross_amount"]),
                "taxAmount": float(d["tax_amount"]),
                "netAmount": float(d["net_amount"]),
                "krwNet": float(d["krw_net"]) if d.get("krw_net") is not None else None,
                "perShare": float(d["per_share"]) if d.get("per_share") is not None else None,
                "sharesHeld": float(d["shares_held"]) if d.get("shares_held") is not None else None,
                "memo": d["memo"] or "",
            }
            for d in auto_divs
        ]
        div_names = ", ".join(f"{x['name']}({format(x['netAmount'], '.2f') if x['currency']=='USD' else int(x['netAmount'])}{'달러' if x['currency']=='USD' else '원'})" for x in formatted_divs[:3])
        summary_txt = f"이후 배당 갱신 완료 ({pdf_cutoff or '기준일'} 이후 지급분) · 자동산출 배당 {len(formatted_divs)}건 확인 [{div_names}]"
        events_to_insert.append(
            (
                kst_div_ts,
                acc_k,
                "DIV_SYNC",
                summary_txt,
                {
                    "cutoffDate": pdf_cutoff,
                    "newlyAddedCount": len(formatted_divs),
                    "dividendsGenerated": len(formatted_divs),
                    "newlyAddedDividends": formatted_divs,
                    "retainedAutoDividends": [],
                },
            )
        )

    for ts_s, acc_k, act_t, sum_s, det_obj in sorted(events_to_insert, key=lambda x: x[0]):
        conn.execute(
            "INSERT INTO sync_logs (account_key, action_type, status, created_at, summary, details_json) VALUES (?, ?, 'SUCCESS', ?, ?, ?)",
            (acc_k, act_t, ts_s, sum_s, json.dumps(det_obj, ensure_ascii=False)),
        )


def _load_alias_map(conn: sqlite3.Connection) -> Dict[str, str]:
    alias_map = dict(import_pdfs.ISIN_TO_TICKER)
    alias_map.update(import_pdfs.EXTRA_TICKER_ALIASES)
    for r in conn.execute("SELECT alias, canonical_symbol FROM symbol_aliases").fetchall():
        alias_map[str(r["alias"]).upper()] = str(r["canonical_symbol"]).upper()
    return alias_map


def canonicalize_symbol_with_db(
    conn: sqlite3.Connection,
    raw_symbol: str,
    stock_name: str = "",
    market_hint: str = "",
    alias_cache: Optional[Dict[str, str]] = None,
) -> str:
    s = (raw_symbol or "").strip().upper()
    if not s:
        return ""
    amap = alias_cache if alias_cache is not None else _load_alias_map(conn)
    if s in amap:
        return amap[s]
    if len(s) == 7 and s.startswith("A") and re.fullmatch(r"[0-9A-Z]{6}", s[1:]):
        return s[1:]
    # 만약 신규 해외 종목이 과거 PDF에 ISIN 형태로만 저장되어 있고 한글 종목명이 동일하다면 자동 매핑
    if stock_name:
        norm_nm = re.sub(r"\s+", "", import_pdfs.clean_stock_name(stock_name)).upper()
        for r in conn.execute("SELECT symbol, name FROM stocks").fetchall():
            db_sym = str(r["symbol"]).upper()
            db_nm = re.sub(r"\s+", "", str(r["name"] or "")).upper()
            if db_nm and db_nm == norm_nm and db_sym != s:
                # API 티커(짧은 영문)를 표준으로 삼거나 기존 심볼로 통합
                if db_sym.startswith(("US", "CA", "BM")) and not s.startswith(("US", "CA", "BM")):
                    # 기존 ISIN 심볼 레코드를 새 공식 티커 s로 승격 통합
                    conn.execute("UPDATE stocks SET symbol = ? WHERE symbol = ?", (s, db_sym))
                    conn.execute("UPDATE trades SET symbol = ? WHERE symbol = ?", (s, db_sym))
                    conn.execute("UPDATE dividends SET symbol = ? WHERE symbol = ?", (s, db_sym))
                    conn.execute("UPDATE holdings_snapshot SET symbol = ? WHERE symbol = ?", (s, db_sym))
                    conn.execute("INSERT OR REPLACE INTO symbol_aliases (alias, canonical_symbol) VALUES (?, ?)", (db_sym, s))
                    if alias_cache is not None:
                        alias_cache[db_sym] = s
                    return s
                return db_sym
    return s


def resolve_and_migrate_account_key(conn: sqlite3.Connection, requested_key: str) -> str:
    """
    PDF 원장 초기 적재 키('LIVE')와 토스 Open API 계좌 키('ACC_<seq>')를 자동 연결·통합.
    - PIN 해제 후 'ACC_<seq>'로 접근 시 'LIVE'에 적재된 PDF 원장/타사배당을 즉시 'ACC_<seq>'로 이관
    - 반대로 아직 'LIVE'로 조회 중인데 단일 'ACC_<seq>'로 이미 이관된 경우 해당 키 반환
    """
    req = (requested_key or "LIVE").strip()
    if req == "DEMO":
        return "DEMO"

    if req.startswith("ACC_"):
        live_cnt = (
            conn.execute("SELECT COUNT(*) FROM trades WHERE account_key='LIVE'").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM dividends WHERE account_key='LIVE'").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM holdings_snapshot WHERE account_key='LIVE'").fetchone()[0]
        )
        if live_cnt > 0:
            conn.execute("UPDATE OR IGNORE trades SET account_key = ? WHERE account_key = 'LIVE'", (req,))
            conn.execute("DELETE FROM trades WHERE account_key = 'LIVE'")
            conn.execute("UPDATE dividends SET account_key = ? WHERE account_key = 'LIVE'", (req,))
            conn.execute("UPDATE sync_logs SET account_key = ? WHERE account_key = 'LIVE'", (req,))
            conn.execute("UPDATE OR IGNORE holdings_snapshot SET account_key = ? WHERE account_key = 'LIVE'", (req,))
            conn.execute("DELETE FROM holdings_snapshot WHERE account_key = 'LIVE'")
            for meta_prefix in ("last_sync_", "last_div_sync_"):
                old_k = f"{meta_prefix}LIVE"
                new_k = f"{meta_prefix}{req}"
                r_meta = conn.execute("SELECT value FROM app_meta WHERE key = ?", (old_k,)).fetchone()
                if r_meta:
                    conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)", (new_k, r_meta["value"]))
                    conn.execute("DELETE FROM app_meta WHERE key = ?", (old_k,))
            conn.commit()
        return req

    # req == 'LIVE' 인 경우
    live_tr_cnt = conn.execute("SELECT COUNT(*) FROM trades WHERE account_key='LIVE'").fetchone()[0]
    if live_tr_cnt > 0:
        return "LIVE"

    acc_rows = conn.execute(
        "SELECT DISTINCT account_key FROM trades WHERE account_key LIKE 'ACC_%' "
        "UNION SELECT DISTINCT account_key FROM holdings_snapshot WHERE account_key LIKE 'ACC_%'"
    ).fetchall()
    if len(acc_rows) == 1:
        return str(acc_rows[0][0])
    return req


def get_pdf_cutoff_date(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_meta WHERE key='pdf_ledger_cutoff_date'").fetchone()
    return str(row["value"]).strip() if (row and row["value"]) else None


def seed_demo_data_if_needed() -> None:
    with DB_LOCK:
        conn = get_db()
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM trades WHERE account_key='DEMO'").fetchone()[0]
            if cnt > 0:
                return

            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            demo_stocks = [
                ("088980", "맥쿼리인프라", "KR", "KRW", 11850),
                ("017670", "SK텔레콤", "KR", "KRW", 57400),
                ("105560", "KB금융", "KR", "KRW", 94200),
                ("458730", "TIGER 미국배당다우존스", "KR", "KRW", 12180),
                ("SCHD", "슈왑 미국 배당주 ETF (SCHD)", "US", "USD", 29.15),
                ("O", "리얼티 인컴 (O)", "US", "USD", 58.40),
                ("JEPI", "JP모건 에쿼티 프리미엄 인컴 ETF (JEPI)", "US", "USD", 59.10),
                ("MAIN", "메인 스트리트 캐피탈 (MAIN)", "US", "USD", 54.80),
            ]
            for sym, name, mkt, cur, lp in demo_stocks:
                conn.execute(
                    "INSERT OR IGNORE INTO stocks (symbol, name, market, currency, last_price, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (sym, name, mkt, cur, lp, now_str),
                )

            demo_holdings = [
                ("088980", "맥쿼리인프라", "KR", "KRW", 500, 11400, 11850, 5700000, 5925000, 5915224, 225000, 215224, 0.03947, 0.03775, 888, 8888),
                ("017670", "SK텔레콤", "KR", "KRW", 80, 52100, 57400, 4168000, 4592000, 4584423, 424000, 416423, 0.10172, 0.09991, 689, 6888),
                ("458730", "TIGER 미국배당다우존스", "KR", "KRW", 300, 11050, 12180, 3315000, 3654000, 3653452, 339000, 338452, 0.10226, 0.10209, 548, 0),
                ("SCHD", "슈왑 미국 배당주 ETF (SCHD)", "US", "USD", 200, 26.40, 29.15, 5280.00, 5830.00, 5824.00, 550.00, 544.00, 0.10416, 0.10303, 5.83, 0.17),
                ("O", "리얼티 인컴 (O)", "US", "USD", 85, 55.20, 58.40, 4692.00, 4964.00, 4958.89, 272.00, 266.89, 0.05797, 0.05688, 4.96, 0.15),
                ("JEPI", "JP모건 에쿼티 프리미엄 인컴 ETF (JEPI)", "US", "USD", 60, 56.50, 59.10, 3390.00, 3546.00, 3542.35, 156.00, 152.35, 0.04601, 0.04494, 3.55, 0.10),
            ]
            for h in demo_holdings:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO holdings_snapshot (
                        account_key, symbol, name, market, currency, quantity, avg_purchase_price,
                        last_price, purchase_amount, eval_amount, eval_amount_after_cost,
                        pnl_amount, pnl_amount_after_cost, pnl_rate, pnl_rate_after_cost,
                        commission, tax, synced_at
                    ) VALUES ('DEMO', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*h, now_str),
                )

            demo_trades = [
                ("DEMO", "dm-1", "088980", "맥쿼리인프라", "KR", "KRW", "BUY", "2025-02-14", 400, 11250, 4500000, 675, 0, "PDF_TRANSFER", "타사증권사 대체입고 (입고일 매수 반영)"),
                ("DEMO", "dm-2", "088980", "맥쿼리인프라", "KR", "KRW", "BUY", "2025-08-20", 200, 11700, 2340000, 351, 0, "PDF", "거래내역서 매수"),
                ("DEMO", "dm-3", "088980", "맥쿼리인프라", "KR", "KRW", "SELL", "2026-03-11", 100, 12300, 1230000, 185, 1845, "PDF", "거래내역서 매도"),
                ("DEMO", "dm-8", "SCHD", "슈왑 미국 배당주 ETF (SCHD)", "US", "USD", "BUY", "2025-03-05", 200, 26.40, 5280.00, 5.28, 0.0, "PDF", "거래내역서 매수"),
            ]
            for t in demo_trades:
                conn.execute(
                    """
                    INSERT INTO trades (
                        account_key, external_order_id, symbol, name, market, currency,
                        side, trade_date, quantity, price, gross_amount, commission, tax, source, memo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    t,
                )

            demo_dividends = [
                ("DEMO", "088980", "맥쿼리인프라", "KR", "KRW", "2025-02-14", 380000, 58520, 321480, None, None, "PRIOR_BROKER", "타사증권사 이관 전 누적 배당금"),
                ("DEMO", "088980", "맥쿼리인프라", "KR", "KRW", "2026-08-28", 195000, 30030, 164970, 390, 500, "PDF", "거래내역서 배당금입금"),
                ("DEMO", "SCHD", "슈왑 미국 배당주 ETF (SCHD)", "US", "USD", "2026-09-28", 53.30, 8.00, 45.30, 0.2665, 200, "PDF", "외화배당 입금 (200주)"),
            ]
            for d in demo_dividends:
                conn.execute(
                    """
                    INSERT INTO dividends (
                        account_key, symbol, name, market, currency, pay_date,
                        gross_amount, tax_amount, net_amount, per_share, shares_held, source, memo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    d,
                )
            conn.commit()
        finally:
            conn.close()


# ============================================================================
# 2. 공개 시세·배당 데이터 기반 증분 배당 산출 엔진 (PDF 기준일 이후분 전용)
# ============================================================================

class _SimpleTableParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cur_table: List[List[str]] = []
        self._cur_row: List[str] = []
        self._cur_cell: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        t = tag.lower()
        if t == "table":
            self._in_table = True
            self._cur_table = []
        elif t == "tr" and self._in_table:
            self._in_row = True
            self._cur_row = []
        elif t in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            txt = " ".join("".join(self._cur_cell).split())
            self._cur_row.append(txt)
        elif t == "tr" and self._in_row:
            self._in_row = False
            if self._cur_row:
                self._cur_table.append(self._cur_row)
        elif t == "table" and self._in_table:
            self._in_table = False
            if self._cur_table:
                self.tables.append(self._cur_table)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_cell.append(data)


_EN_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_flexible_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if not s or s in ("-", "N/A", "n/a", "TBD", "미정"):
        return None
    m = re.search(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.search(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{2,4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        yr_raw, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = (2000 + yr_raw if yr_raw < 70 else 1900 + yr_raw) if yr_raw < 100 else yr_raw
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.search(r"^(\d{1,2})\s*월\s*(\d{1,2})\s*일$", s)
    if m:
        try:
            return datetime.date(datetime.date.today().year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        mo = _EN_MONTHS.get(m.group(1).lower())
        if mo:
            try:
                return datetime.date(int(m.group(3)), mo, int(m.group(2))).isoformat()
            except ValueError:
                return None
    return None


def _http_get_text(url: str, extra_headers: Optional[Dict[str, str]] = None, timeout: float = 12.0) -> str:
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", errors="replace")


class AutoDividendSyncService:
    """
    PDF 원장 기준일(pdf_ledger_cutoff_date) 이후에 지급일이 도래한
    신규 배당·분배금만 증분 산출하여 추가 (과거 PDF 확정 배당 및 타사 배당 통입력분은 영구 보존)
    """

    CACHE_TTL_SEC = 12 * 3600

    def fetch_symbol_dividend_events(
        self, symbol: str, market: str, force_refresh: bool = False
    ) -> Tuple[List[Dict[str, Any]], str]:
        sym = symbol.strip().upper()
        mkt = market.strip().upper()

        if not force_refresh:
            with DB_LOCK:
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT events_json, source_label, fetched_at FROM dividend_events_cache WHERE symbol=? AND market=?",
                        (sym, mkt),
                    ).fetchone()
                finally:
                    conn.close()
            if row and (time.time() - float(row["fetched_at"])) < self.CACHE_TTL_SEC:
                try:
                    cached_events = json.loads(row["events_json"])
                    if isinstance(cached_events, list):
                        return cached_events, str(row["source_label"])
                except Exception:
                    pass

        events, src_label = self._fetch_kr_dividends(sym) if mkt == "KR" else self._fetch_us_dividends(sym)

        with DB_LOCK:
            conn = get_db()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dividend_events_cache (symbol, market, events_json, source_label, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sym, mkt, json.dumps(events, ensure_ascii=False), src_label, time.time()),
                )
                conn.commit()
            finally:
                conn.close()

        return events, src_label

    def _fetch_kr_dividends(self, symbol: str) -> Tuple[List[Dict[str, Any]], str]:
        clean_sym = symbol[1:] if (len(symbol) == 7 and symbol.startswith("A")) else symbol
        merged: Dict[str, Dict[str, Any]] = {}
        sources_used: List[str] = []

        toss_events = self._fetch_from_toss_public_page(clean_sym)
        if toss_events:
            for ev in toss_events:
                merged[ev["ex_date"]] = ev
            sources_used.append("토스증권 종목정보")

        for suffix in (".KS", ".KQ"):
            yf_events = self._fetch_from_yahoo_chart(f"{clean_sym}{suffix}", KST_TZ)
            if yf_events:
                added = 0
                for ev in yf_events:
                    if not self._has_nearby_ex_date(merged, ev["ex_date"], 2):
                        merged[ev["ex_date"]] = ev
                        added += 1
                if added > 0 and "Yahoo Finance" not in sources_used:
                    sources_used.append("Yahoo Finance")
                break

        return sorted(merged.values(), key=lambda x: x["ex_date"]), "+".join(sources_used) or "NONE"

    def _fetch_from_toss_public_page(self, clean_kr_sym: str) -> List[Dict[str, Any]]:
        toss_code = f"A{clean_kr_sym}"
        url = f"https://www.tossinvest.com/stocks/{urllib.parse.quote(toss_code)}/order"
        try:
            html_text = _http_get_text(url, extra_headers={"Referer": "https://www.tossinvest.com/"}, timeout=12.0)
        except Exception:
            return []

        events: Dict[str, Dict[str, Any]] = {}
        parser = _SimpleTableParser()
        try:
            parser.feed(html_text)
        except Exception:
            pass

        for tbl in parser.tables:
            if len(tbl) < 2:
                continue
            header = [c.replace(" ", "") for c in tbl[0]]
            ex_idx = next((i for i, h in enumerate(header) if "배당락일" in h or "기준일" in h), -1)
            pay_idx = next((i for i, h in enumerate(header) if "지급일" in h), -1)
            amt_idx = next((i for i, h in enumerate(header) if "주당배당금" in h or "주당분배금" in h or h == "배당금"), -1)
            if ex_idx < 0 or amt_idx < 0:
                continue
            for row in tbl[1:]:
                if max(ex_idx, amt_idx) >= len(row):
                    continue
                ex_dt = _parse_flexible_date(row[ex_idx])
                pay_dt = _parse_flexible_date(row[pay_idx]) if (0 <= pay_idx < len(row)) else None
                amt = _to_float(row[amt_idx], 0.0)
                if ex_dt and amt > 0:
                    events[ex_dt] = {"ex_date": ex_dt, "pay_date": pay_dt or _add_business_days(ex_dt, 3), "per_share": amt}

        if not events:
            unescaped = html_text.replace('\\"', '"')
            pattern = re.compile(
                r'"(?:exDividendDate|exDate|baseDate)"\s*:\s*"([^"]+)"[^{}]{0,220}?'
                r'"(?:paymentDate|payDate)"\s*:\s*"([^"]*)"[^{}]{0,220}?'
                r'"(?:dividendPerShare|perShareDividend|amount|dps)"\s*:\s*([0-9.]+)'
            )
            for m in pattern.finditer(unescaped):
                ex_dt = _parse_flexible_date(m.group(1))
                pay_dt = _parse_flexible_date(m.group(2))
                amt = _to_float(m.group(3), 0.0)
                if ex_dt and amt > 0:
                    events[ex_dt] = {"ex_date": ex_dt, "pay_date": pay_dt or _add_business_days(ex_dt, 3), "per_share": amt}

        return sorted(events.values(), key=lambda x: x["ex_date"])

    def _fetch_us_dividends(self, symbol: str) -> Tuple[List[Dict[str, Any]], str]:
        merged: Dict[str, Dict[str, Any]] = {}
        sources_used: List[str] = []

        sa_events = self._fetch_from_stockanalysis(symbol)
        if sa_events:
            for ev in sa_events:
                merged[ev["ex_date"]] = ev
            sources_used.append("StockAnalysis")

        nq_events = self._fetch_from_nasdaq_api(symbol)
        if nq_events:
            added = 0
            for ev in nq_events:
                if not self._has_nearby_ex_date(merged, ev["ex_date"], 1):
                    merged[ev["ex_date"]] = ev
                    added += 1
            if added > 0:
                sources_used.append("Nasdaq")

        yahoo_sym = symbol.replace(".", "-").replace("/", "-")
        yf_events = self._fetch_from_yahoo_chart(yahoo_sym, NY_TZ)
        if yf_events:
            added = 0
            for ev in yf_events:
                if not self._has_nearby_ex_date(merged, ev["ex_date"], 1):
                    merged[ev["ex_date"]] = ev
                    added += 1
            if added > 0:
                sources_used.append("Yahoo Finance")

        return sorted(merged.values(), key=lambda x: x["ex_date"]), "+".join(sources_used) or "NONE"

    def _fetch_from_stockanalysis(self, symbol: str) -> List[Dict[str, Any]]:
        sa_sym = symbol.lower().replace("/", ".")
        events: Dict[str, Dict[str, Any]] = {}
        for kind in ("etf", "stocks"):
            url = f"https://stockanalysis.com/{kind}/{urllib.parse.quote(sa_sym)}/dividend/"
            try:
                html_text = _http_get_text(url, extra_headers={"Referer": "https://stockanalysis.com/"}, timeout=10.0)
            except Exception:
                continue
            parser = _SimpleTableParser()
            try:
                parser.feed(html_text)
            except Exception:
                continue
            for tbl in parser.tables:
                if len(tbl) < 2:
                    continue
                header = [c.lower() for c in tbl[0]]
                ex_idx = next((i for i, h in enumerate(header) if "ex-dividend" in h or "ex date" in h), -1)
                amt_idx = next((i for i, h in enumerate(header) if "amount" in h), -1)
                pay_idx = next((i for i, h in enumerate(header) if "pay date" in h or "payment" in h), -1)
                if ex_idx < 0 or amt_idx < 0:
                    continue
                for row in tbl[1:]:
                    if max(ex_idx, amt_idx) >= len(row):
                        continue
                    ex_dt = _parse_flexible_date(row[ex_idx])
                    pay_dt = _parse_flexible_date(row[pay_idx]) if (0 <= pay_idx < len(row)) else None
                    amt = _to_float(row[amt_idx], 0.0)
                    if ex_dt and amt > 0:
                        events[ex_dt] = {"ex_date": ex_dt, "pay_date": pay_dt or _add_business_days(ex_dt, 3), "per_share": amt}
            if events:
                break
        return sorted(events.values(), key=lambda x: x["ex_date"])

    def _fetch_from_nasdaq_api(self, symbol: str) -> List[Dict[str, Any]]:
        nq_sym = symbol.upper().replace("-", ".")
        events: Dict[str, Dict[str, Any]] = {}
        for asset_cls in ("etf", "stocks"):
            url = f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(nq_sym)}/dividends?assetclass={asset_cls}"
            try:
                raw_json = _http_get_text(
                    url,
                    extra_headers={
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://www.nasdaq.com",
                        "Referer": "https://www.nasdaq.com/",
                    },
                    timeout=9.0,
                )
                parsed = json.loads(raw_json)
            except Exception:
                continue
            rows = (((parsed.get("data") or {}).get("dividends") or {}).get("rows")) or []
            if not isinstance(rows, list) or not rows:
                continue
            for r in rows:
                ex_dt = _parse_flexible_date(str(r.get("exOrEffDate") or ""))
                pay_dt = _parse_flexible_date(str(r.get("paymentDate") or ""))
                amt = _to_float(r.get("amount"), 0.0)
                if ex_dt and amt > 0:
                    events[ex_dt] = {"ex_date": ex_dt, "pay_date": pay_dt or _add_business_days(ex_dt, 3), "per_share": amt}
            if events:
                break
        return sorted(events.values(), key=lambda x: x["ex_date"])

    def _fetch_from_yahoo_chart(self, yahoo_symbol: str, tz: ZoneInfo) -> List[Dict[str, Any]]:
        events: Dict[str, Dict[str, Any]] = {}
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}?events=div&range=2y&interval=1d"
            try:
                raw_json = _http_get_text(url, extra_headers={"Accept": "application/json"}, timeout=10.0)
                payload = json.loads(raw_json)
            except Exception:
                continue
            results = ((payload.get("chart") or {}).get("result")) or []
            if not results:
                continue
            divs_obj = ((results[0].get("events") or {}).get("dividends")) or {}
            if not isinstance(divs_obj, dict):
                continue
            for _, item in divs_obj.items():
                ts = item.get("date")
                amt = _to_float(item.get("amount"), 0.0)
                if ts is None or amt <= 0:
                    continue
                ex_dt = datetime.datetime.fromtimestamp(int(ts), tz=tz).date().isoformat()
                events[ex_dt] = {"ex_date": ex_dt, "pay_date": _add_business_days(ex_dt, 3), "per_share": amt}
            if events:
                break
        return sorted(events.values(), key=lambda x: x["ex_date"])

    @staticmethod
    def _has_nearby_ex_date(existing_map: Dict[str, Dict[str, Any]], candidate_iso: str, max_days: int = 1) -> bool:
        if candidate_iso in existing_map:
            return True
        try:
            cand_dt = datetime.date.fromisoformat(candidate_iso)
        except ValueError:
            return False
        for k in existing_map.keys():
            try:
                if abs((datetime.date.fromisoformat(k) - cand_dt).days) <= max_days:
                    return True
            except ValueError:
                continue
        return False

    def sync_account_dividends(
        self,
        account_key: str,
        force_refresh: bool = False,
        from_date_override: Optional[str] = None,
        record_log: bool = True,
    ) -> Dict[str, Any]:
        if account_key == "DEMO":
            seed_demo_data_if_needed()
            return {"symbolsChecked": 8, "symbolsWithDividends": 2, "dividendsGenerated": 0, "newlyAddedCount": 0, "cutoffDate": None}

        with DB_LOCK:
            conn = get_db()
            try:
                eff_key = resolve_and_migrate_account_key(conn, account_key)
                pdf_cutoff = get_pdf_cutoff_date(conn)
                stocks_map = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM stocks").fetchall()}
                holdings_rows = [
                    dict(r) for r in conn.execute("SELECT * FROM holdings_snapshot WHERE account_key=?", (eff_key,)).fetchall()
                ]
                trades_rows = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT * FROM trades
                        WHERE account_key=?
                        ORDER BY trade_date ASC,
                                 CASE side WHEN 'BUY' THEN 1 WHEN 'SPLIT_OUT' THEN 2 WHEN 'SPLIT_IN' THEN 3 WHEN 'SELL' THEN 4 ELSE 5 END ASC,
                                 id ASC
                        """,
                        (eff_key,),
                    ).fetchall()
                ]
                fixed_div_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT symbol, pay_date, source FROM dividends WHERE account_key=? AND source NOT IN ('AUTO', 'PRIOR_BROKER', 'PRIOR_YEARLY')",
                        (eff_key,),
                    ).fetchall()
                ]
                prev_auto_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM dividends WHERE account_key=? AND source='AUTO'",
                        (eff_key,),
                    ).fetchall()
                ]
            finally:
                conn.close()

        prev_auto_map = {(str(r["symbol"]), str(r["pay_date"])): r for r in prev_auto_rows}
        today_iso = datetime.date.today().isoformat()
        # PDF 기준일이 오늘 이상이면 오늘까지는 이미 거래내역서 확정 배당으로 100% 반영되어 있으므로 불필요 외부 크롤링 생략
        if pdf_cutoff and pdf_cutoff >= today_iso and not force_refresh:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            retained_items = [
                {
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "market": r["market"],
                    "currency": r["currency"],
                    "payDate": r["pay_date"],
                    "grossAmount": float(r["gross_amount"]),
                    "taxAmount": float(r["tax_amount"]),
                    "netAmount": float(r["net_amount"]),
                    "krwNet": float(r["krw_net"]) if r.get("krw_net") is not None else None,
                    "memo": r["memo"] or "",
                }
                for r in prev_auto_rows
            ]
            return {
                "symbolsChecked": len(holdings_rows),
                "symbolsWithDividends": len({r["symbol"] for r in prev_auto_rows}),
                "dividendsGenerated": len(prev_auto_rows),
                "newlyAddedCount": 0,
                "newlyAddedDividends": [],
                "retainedAutoDividends": retained_items,
                "cutoffDate": pdf_cutoff,
                "syncedAt": now_str,
            }

        holdings_by_sym = {h["symbol"]: h for h in holdings_rows}
        trades_by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for tr in trades_rows:
            trades_by_sym.setdefault(tr["symbol"], []).append(tr)

        # PDF 확정 원장이 존재하는 경우, 현재 보유 중이거나 기준일 최근 매도한 종목만 점검해도 충분 (속도 최적화)
        if pdf_cutoff:
            recent_cutoff = (datetime.date.fromisoformat(pdf_cutoff) - datetime.timedelta(days=45)).isoformat()
            target_symbols = sorted(
                set(holdings_by_sym.keys())
                | {
                    sym
                    for sym, trs in trades_by_sym.items()
                    if any(t["trade_date"] >= recent_cutoff for t in trs)
                }
            )
        else:
            target_symbols = sorted(set(holdings_by_sym.keys()) | set(trades_by_sym.keys()))

        if not target_symbols:
            return {"symbolsChecked": 0, "symbolsWithDividends": 0, "dividendsGenerated": 0, "cutoffDate": pdf_cutoff}

        fixed_dates_by_sym: Dict[str, List[datetime.date]] = {}
        for fd in fixed_div_rows:
            try:
                fixed_dates_by_sym.setdefault(fd["symbol"], []).append(datetime.date.fromisoformat(fd["pay_date"][:10]))
            except ValueError:
                pass

        sym_meta_map: Dict[str, Tuple[str, str, str]] = {}
        for sym in target_symbols:
            h = holdings_by_sym.get(sym)
            st = stocks_map.get(sym, {})
            trs = trades_by_sym.get(sym, [])
            name = (h and h["name"]) or st.get("name") or (trs[-1]["name"] if trs else sym)
            raw_mkt = (h and h["market"]) or st.get("market") or (trs[0]["market"] if trs else "")
            raw_cur = (h and h["currency"]) or st.get("currency") or (trs[0]["currency"] if trs else "")
            mkt, cur = _infer_market_and_currency(sym, raw_mkt, raw_cur)
            sym_meta_map[sym] = (name, mkt, cur)

        fetched_events_map: Dict[str, Tuple[List[Dict[str, Any]], str]] = {}

        def _worker(sym_code: str) -> Tuple[str, List[Dict[str, Any]], str]:
            _, mkt_code, _ = sym_meta_map[sym_code]
            evs, label = self.fetch_symbol_dividend_events(sym_code, mkt_code, force_refresh=force_refresh)
            return sym_code, evs, label

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_worker, s) for s in target_symbols]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    s_code, ev_list, s_label = fut.result()
                    fetched_events_map[s_code] = (ev_list, s_label)
                except Exception:
                    pass

        generated_rows: List[Tuple[Any, ...]] = []
        symbols_with_divs = 0

        for sym in target_symbols:
            events, _ = fetched_events_map.get(sym, ([], "NONE"))
            if not events:
                continue

            name, mkt, cur = sym_meta_map[sym]
            sym_trades = trades_by_sym.get(sym, [])
            sym_fixed_dates = fixed_dates_by_sym.get(sym, [])
            sym_div_added = 0

            for ev in events:
                ex_date = ev["ex_date"]
                pay_date = ev["pay_date"]
                per_share = float(ev["per_share"])

                # 1) 미래 미지급 배당 제외
                if pay_date > today_iso:
                    continue
                # 2) PDF 원장 기준일 이하 배당은 이미 PDF 실수령 원장에 확정 반영되어 있으므로 제외!
                if pdf_cutoff and pay_date <= pdf_cutoff:
                    continue
                if from_date_override and pay_date < from_date_override:
                    continue

                # 배당락일 전 영업일까지 보유한 수량 계산 (분할·병합 반영)
                # 주의: PDF 구간의 거래일자는 결제일이므로 배당기준일(락일+1영업일) 결제분까지 매칭
                settle_cutoff_for_ex = _estimate_settlement_date(ex_date, mkt)
                delta_q = 0.0
                for tr in sym_trades:
                    t_src = str(tr.get("source") or "")
                    # PDF 원장 거래는 결제일 기준이므로 settle_cutoff_for_ex 미만, API 체결은 ex_date 미만
                    is_eligible = (
                        (tr["trade_date"] < settle_cutoff_for_ex)
                        if t_src.startswith("PDF")
                        else (tr["trade_date"] < ex_date)
                    )
                    if is_eligible:
                        side_s = tr["side"]
                        q_v = float(tr["quantity"])
                        if side_s in ("BUY", "SPLIT_IN"):
                            delta_q += q_v
                        elif side_s in ("SELL", "SPLIT_OUT"):
                            delta_q = max(0.0, delta_q - q_v)

                eligible_qty = round(delta_q, 6)
                if eligible_qty <= 1e-6:
                    continue

                try:
                    pay_dt_obj = datetime.date.fromisoformat(pay_date)
                    if any(abs((pay_dt_obj - fd_dt).days) <= 4 for fd_dt in sym_fixed_dates):
                        continue
                except ValueError:
                    pass

                if cur == "KRW":
                    gross = float(round(eligible_qty * per_share))
                    if gross < 1:
                        continue
                    tax = float(_calc_kr_dividend_tax(gross))
                    net = gross - tax
                    fx_val = 1.0
                    krw_g, krw_t, krw_n = gross, tax, net
                    dps_str = f"{int(round(per_share)):,}원" if abs(per_share - round(per_share)) < 1e-4 else f"{per_share:,.2f}원"
                else:
                    gross = round(eligible_qty * per_share, 2)
                    if gross < 0.01:
                        continue
                    tax = round(gross * 0.15, 2)
                    net = round(gross - tax, 2)
                    fx_val = self._fetch_usdkrw_on_date(pay_date)
                    krw_n = float(int(round(net * fx_val)))
                    krw_t = float(int(round(tax * fx_val)))
                    krw_g = krw_n + krw_t
                    dps_str = f"${per_share:.4f}".rstrip("0").rstrip(".")

                qty_str = f"{int(eligible_qty):,}주" if abs(eligible_qty - round(eligible_qty)) < 1e-6 else f"{eligible_qty:g}주"
                memo = f"증분 자동계산 (배당락 {ex_date} · {qty_str} × {dps_str})"

                generated_rows.append(
                    (eff_key, sym, name, mkt, cur, pay_date, gross, tax, net, fx_val, krw_g, krw_t, krw_n, per_share, eligible_qty, "AUTO", memo)
                )
                sym_div_added += 1

            if sym_div_added > 0:
                symbols_with_divs += 1

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        newly_added_divs: List[Dict[str, Any]] = []
        retained_auto_divs: List[Dict[str, Any]] = []
        for gr in generated_rows:
            item_dict = {
                "symbol": gr[1],
                "name": gr[2],
                "market": gr[3],
                "currency": gr[4],
                "payDate": gr[5],
                "grossAmount": float(gr[6]),
                "taxAmount": float(gr[7]),
                "netAmount": float(gr[8]),
                "krwNet": float(gr[12]) if gr[12] is not None else None,
                "perShare": float(gr[13]),
                "sharesHeld": float(gr[14]),
                "memo": gr[16],
            }
            prev_r = prev_auto_map.get((gr[1], gr[5]))
            if prev_r is None or abs(float(prev_r["net_amount"]) - float(gr[8])) > 0.005:
                newly_added_divs.append(item_dict)
            else:
                retained_auto_divs.append(item_dict)

        with DB_LOCK:
            conn = get_db()
            try:
                conn.execute("DELETE FROM dividends WHERE account_key = ? AND source = 'AUTO'", (eff_key,))
                conn.executemany(
                    """
                    INSERT INTO dividends (
                        account_key, symbol, name, market, currency, pay_date,
                        gross_amount, tax_amount, net_amount,
                        fx_rate, krw_gross, krw_tax, krw_net,
                        per_share, shares_held, source, memo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    generated_rows,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                    (f"last_div_sync_{eff_key}", now_str),
                )
                conn.commit()
            finally:
                conn.close()

        out_res = {
            "symbolsChecked": len(target_symbols),
            "symbolsWithDividends": symbols_with_divs,
            "dividendsGenerated": len(generated_rows),
            "newlyAddedCount": len(newly_added_divs),
            "newlyAddedDividends": newly_added_divs,
            "retainedAutoDividends": retained_auto_divs,
            "cutoffDate": pdf_cutoff,
            "syncedAt": now_str,
        }
        if record_log:
            cutoff_txt = f"{pdf_cutoff} 이후 지급분" if pdf_cutoff else "전체 기간"
            if newly_added_divs:
                names_preview = ", ".join(f"{d['name']}({d['symbol']})" for d in newly_added_divs[:4])
                sum_msg = f"이후 배당 갱신 완료 ({cutoff_txt}) · 신규 배당 {len(newly_added_divs)}건 추가 [{names_preview}]"
            elif retained_auto_divs:
                sum_msg = f"이후 배당 점검 완료 ({cutoff_txt} · {len(target_symbols)}종목 확인) · 새로 추가된 배당 0건 (기반영 {len(retained_auto_divs)}건 유지)"
            else:
                sum_msg = f"이후 배당 점검 완료 ({cutoff_txt} · {len(target_symbols)}종목 확인) · 신규 지급 도래 배당 0건"
            append_sync_log(
                account_key=eff_key,
                action_type="DIV_SYNC",
                status="SUCCESS",
                summary=sum_msg,
                details={
                    "symbolsChecked": len(target_symbols),
                    "cutoffDate": pdf_cutoff,
                    "newlyAddedCount": len(newly_added_divs),
                    "dividendsGenerated": len(generated_rows),
                    "newlyAddedDividends": newly_added_divs,
                    "retainedAutoDividends": retained_auto_divs,
                },
                created_at=now_str,
            )
        return out_res

    def _fetch_usdkrw_on_date(self, date_iso: str) -> float:
        """지급일(pay_date) 당일 공식 USD/KRW 환율 조회 (실패 시 연평균 환율 폴백)"""
        try:
            dt_obj = datetime.date.fromisoformat(date_iso[:10])
            p1 = int(datetime.datetime.combine(dt_obj - datetime.timedelta(days=5), datetime.time.min, tzinfo=datetime.timezone.utc).timestamp())
            p2 = int(datetime.datetime.combine(dt_obj + datetime.timedelta(days=2), datetime.time.max, tzinfo=datetime.timezone.utc).timestamp())
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?period1={p1}&period2={p2}&interval=1d"
            raw_json = _http_get_text(url, extra_headers={"Accept": "application/json"}, timeout=6.0)
            payload = json.loads(raw_json)
            res_list = ((payload.get("chart") or {}).get("result")) or []
            if res_list:
                closes = (((res_list[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
                valid_closes = [float(c) for c in closes if c is not None and 800 < float(c) < 2500]
                if valid_closes:
                    return round(valid_closes[-1], 2)
        except Exception:
            pass
        return _fallback_usdkrw_rate(date_iso)


AUTO_DIV_SYNC = AutoDividendSyncService()


# ============================================================================
# 3. 토스증권 공식 Open API 클라이언트 (100% 열람 전용 · 기준일 이후 증분 동기화)
# ============================================================================

class TossApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


READ_ONLY_ALLOWED_TOSS_PATHS = frozenset(
    {
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/orders",
        "/api/v1/stocks",
        "/api/v1/prices",
    }
)


class TossOpenApiSyncService:
    def __init__(self) -> None:
        self._group_last_call: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._token_cache: Dict[str, Tuple[str, float]] = {}

    def _pace(self, group: str, min_interval: float) -> None:
        with self._lock:
            now = time.time()
            prev = self._group_last_call.get(group, 0.0)
            wait = min_interval - (now - prev)
            if wait > 0:
                time.sleep(wait)
            self._group_last_call[group] = time.time()

    def obtain_token(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = TOSS_DEFAULT_BASE_URL,
        cached_token: Optional[str] = None,
        token_expires_at: float = 0.0,
        force_refresh: bool = False,
    ) -> Tuple[str, float]:
        if not force_refresh:
            if cached_token and time.time() < (token_expires_at - 300):
                return cached_token, token_expires_at
            mem = self._token_cache.get(client_id)
            if mem and time.time() < (mem[1] - 300):
                return mem

        self._pace("AUTH", 0.25)
        token_url = f"{base_url.rstrip('/')}/oauth2/token"
        form_data = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
        ).encode("utf-8")

        req = urllib.request.Request(
            token_url,
            data=form_data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            req_id = e.headers.get("X-Request-Id", "")
            code, msg = f"http-{e.code}", f"토큰 발급 실패 (HTTP {e.code})"
            try:
                parsed = json.loads(raw_err)
                err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
                code = str(err_obj.get("error") or err_obj.get("code") or code)
                msg = str(err_obj.get("error_description") or err_obj.get("message") or msg)
            except Exception:
                pass
            if e.code == 403 or "access_denied" in code or "edge-blocked" in code:
                msg = "허용되지 않은 공인 IP입니다. 토스증권 PC 웹(WTS) > 설정 > Open API > 허용 IP 관리에서 현재 공인 IP를 등록해주세요."
            elif e.code == 401 or "invalid_client" in code:
                msg = "Client ID 또는 Client Secret이 올바르지 않습니다."
            raise TossApiError(e.code, code, msg, req_id)
        except urllib.error.URLError as e:
            raise TossApiError(503, "network-error", f"토스증권 API 서버 연결에 실패했습니다: {e.reason}")

        access_token = body.get("access_token") or (body.get("result") or {}).get("access_token")
        expires_in = int(body.get("expires_in") or (body.get("result") or {}).get("expires_in") or 86400)
        if not access_token:
            raise TossApiError(500, "missing-token", "토큰 응답에 access_token이 없습니다.")

        new_exp = time.time() + expires_in
        self._token_cache[client_id] = (access_token, new_exp)
        return access_token, new_exp

    def request_get(
        self,
        client_id: str,
        client_secret: str,
        token_state: List[Any],
        path: str,
        params: Optional[Dict[str, Any]] = None,
        account_seq: Optional[Any] = None,
        base_url: str = TOSS_DEFAULT_BASE_URL,
        group: str = "DEFAULT",
        min_interval: float = 0.22,
    ) -> Any:
        if path not in READ_ONLY_ALLOWED_TOSS_PATHS:
            raise RuntimeError(f"[열람 전용 보안 차단] 허용되지 않은 API 경로 호출이 차단되었습니다: {path}")
        if path == "/api/v1/orders" and (not params or params.get("status") != "CLOSED"):
            raise RuntimeError("[열람 전용 보안 차단] 주문 조회는 종료된 체결 내역(status=CLOSED) 열람만 허용됩니다.")

        for attempt in range(4):
            self._pace(group, min_interval)
            url = f"{base_url.rstrip('/')}{path}"
            if params:
                clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
                if clean_params:
                    url += "?" + urllib.parse.urlencode(clean_params)

            headers = {"Authorization": f"Bearer {token_state[0]}", "Accept": "application/json"}
            if account_seq is not None:
                headers["X-Tossinvest-Account"] = str(account_seq)

            req = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and _to_float(remaining, 5) <= 1:
                        time.sleep(0.35)
                    return payload.get("result", payload)
            except urllib.error.HTTPError as e:
                raw_err = e.read().decode("utf-8", errors="replace")
                req_id = e.headers.get("X-Request-Id", "")
                code, msg = f"http-{e.code}", f"API 호출 오류 (HTTP {e.code})"
                try:
                    parsed = json.loads(raw_err)
                    err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
                    code = str(err_obj.get("code") or code)
                    msg = str(err_obj.get("message") or msg)
                except Exception:
                    pass
                if e.code == 401 and attempt == 0:
                    new_tok, new_exp = self.obtain_token(client_id, client_secret, base_url, force_refresh=True)
                    token_state[0], token_state[1] = new_tok, new_exp
                    continue
                if e.code == 429 and attempt < 3:
                    retry_after = _to_float(e.headers.get("Retry-After"), 0)
                    time.sleep(min(max(retry_after, (2 ** attempt) + 0.25), 8.0))
                    continue
                if e.code == 403:
                    msg = "허용 IP 미등록 또는 계좌 접근 권한 오류(403)입니다. 토스증권 WTS > 설정 > Open API > 허용 IP 관리 설정을 확인해주세요."
                raise TossApiError(e.code, code, msg, req_id)
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(1.0)
                    continue
                raise TossApiError(503, "network-error", f"네트워크 연결 실패: {e.reason}")

        raise TossApiError(429, "rate-limit-exceeded", "호출 한도 초과로 요청을 완료하지 못했습니다.")

    def run_sync(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = TOSS_DEFAULT_BASE_URL,
        cached_token: Optional[str] = None,
        token_expires_at: float = 0.0,
        target_account_seq: Optional[Any] = None,
        from_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        tok, exp = self.obtain_token(client_id, client_secret, base_url, cached_token, token_expires_at)
        token_state: List[Any] = [tok, exp]

        # 1) 계좌 목록 조회
        acc_res = self.request_get(
            client_id, client_secret, token_state, "/api/v1/accounts", base_url=base_url, group="ACCOUNT", min_interval=1.05
        )
        raw_accs = acc_res if isinstance(acc_res, list) else (acc_res.get("accounts") or acc_res.get("items") or [])
        accounts = []
        for row in raw_accs:
            seq = row.get("accountSeq")
            if seq is None:
                continue
            acc_no = str(row.get("accountNo") or row.get("accountNumber") or seq)
            accounts.append(
                {
                    "accountSeq": seq,
                    "maskedAccountNo": mask_account(acc_no),
                    "accountType": row.get("accountType") or "BROKERAGE",
                    "name": row.get("accountName") or row.get("name") or f"종합매매계좌 ({mask_account(acc_no)})",
                }
            )
        if not accounts:
            raise TossApiError(404, "account-not-found", "조회 가능한 토스증권 종합매매계좌가 없습니다.")

        account_seq = target_account_seq if any(str(a["accountSeq"]) == str(target_account_seq) for a in accounts) else accounts[0]["accountSeq"]
        account_key = f"ACC_{account_seq}"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # DB에서 LIVE 원장 자동 연결 + PDF 커트오프 기준일 및 별칭 맵 로드
        with DB_LOCK:
            conn = get_db()
            try:
                account_key = resolve_and_migrate_account_key(conn, account_key)
                pdf_cutoff = get_pdf_cutoff_date(conn)
                last_api_dt_row = conn.execute(
                    "SELECT value FROM app_meta WHERE key = ?", (f"last_api_order_date_{account_key}",)
                ).fetchone()
                last_api_dt = str(last_api_dt_row["value"]).strip() if (last_api_dt_row and last_api_dt_row["value"]) else None
                boundary_rec_row = conn.execute(
                    "SELECT value FROM app_meta WHERE key = ?", (f"boundary_v2_reconciled_{account_key}",)
                ).fetchone()
                boundary_reconciled_cutoff = (
                    str(boundary_rec_row["value"]).strip() if (boundary_rec_row and boundary_rec_row["value"]) else None
                )
                alias_map = _load_alias_map(conn)
            finally:
                conn.close()

        # 증분 조회 시작일 산출:
        # PDF 기준일 경계의 미결제 체결분(미국 한국결제 +2영업일·휴일 포함)을 빠짐없이 스캔하도록 보정
        effective_from = from_date
        if not effective_from:
            candidates_from: List[str] = []
            if last_api_dt:
                candidates_from.append((datetime.date.fromisoformat(last_api_dt) - datetime.timedelta(days=6)).isoformat())
            if pdf_cutoff and boundary_reconciled_cutoff != pdf_cutoff:
                candidates_from.append((datetime.date.fromisoformat(pdf_cutoff) - datetime.timedelta(days=7)).isoformat())
            elif pdf_cutoff and not last_api_dt:
                candidates_from.append((datetime.date.fromisoformat(pdf_cutoff) - datetime.timedelta(days=7)).isoformat())
            if candidates_from:
                effective_from = min(candidates_from)

        # 2) 보유 종목 실시간 조회 (/api/v1/holdings)
        holdings_res = self.request_get(
            client_id, client_secret, token_state, "/api/v1/holdings", account_seq=account_seq, base_url=base_url, group="ASSET", min_interval=0.25
        )
        raw_holdings = holdings_res.get("items") or holdings_res.get("holdings") or [] if isinstance(holdings_res, dict) else holdings_res

        parsed_holdings = []
        known_symbols: Dict[str, Dict[str, Any]] = {}

        with DB_LOCK:
            conn = get_db()
            try:
                for row in raw_holdings:
                    raw_sym = str(row.get("symbol") or "").strip()
                    if not raw_sym:
                        continue
                    raw_name = str(row.get("name") or row.get("stockName") or raw_sym).strip()
                    sym = canonicalize_symbol_with_db(conn, raw_sym, raw_name, alias_cache=alias_map)
                    mkt, cur = _infer_market_and_currency(sym, row.get("marketCountry") or row.get("market"), row.get("currency"))
                    qty = _to_float(row.get("quantity"))
                    avg_p = _to_float(row.get("averagePurchasePrice") or row.get("averagePrice"))
                    last_p = _to_float(row.get("lastPrice") or row.get("currentPrice"))

                    mv = row.get("marketValue") if isinstance(row.get("marketValue"), dict) else {}
                    pl = row.get("profitLoss") if isinstance(row.get("profitLoss"), dict) else {}
                    cost = row.get("cost") if isinstance(row.get("cost"), dict) else {}

                    purchase_amt = _to_float(mv.get("purchaseAmount"), qty * avg_p)
                    eval_amt = _to_float(mv.get("amount"), qty * last_p)
                    comm = _to_float(cost.get("commission"))
                    tax = _to_float(cost.get("tax"))
                    eval_after = _to_float(mv.get("amountAfterCost"), eval_amt - comm - tax)
                    pnl_amt = _to_float(pl.get("amount"), eval_amt - purchase_amt)
                    pnl_after = _to_float(pl.get("amountAfterCost"), pnl_amt - comm - tax)
                    pnl_rate = _to_float(pl.get("rate"))
                    pnl_rate_after = _to_float(pl.get("rateAfterCost"))

                    parsed_holdings.append(
                        (
                            account_key, sym, raw_name, mkt, cur, qty, avg_p, last_p,
                            purchase_amt, eval_amt, eval_after, pnl_amt, pnl_after,
                            pnl_rate, pnl_rate_after, comm, tax, now_str,
                        )
                    )
                    known_symbols[sym] = {"name": raw_name, "market": mkt, "currency": cur, "last_price": last_p}
                conn.commit()
            finally:
                conn.close()

        # 3) 기준일 이후 증분 체결 내역 조회 (/api/v1/orders?status=CLOSED)
        closed_orders = self._fetch_incremental_closed_orders(
            client_id, client_secret, token_state, account_seq, base_url, effective_from
        )
        parsed_orders = []
        missing_symbols = set()

        with DB_LOCK:
            conn = get_db()
            try:
                for od in closed_orders:
                    po = self._parse_closed_order(conn, od, known_symbols, alias_map)
                    if not po:
                        continue
                    parsed_orders.append(po)
                    if po["symbol"] not in known_symbols:
                        missing_symbols.add(po["symbol"])
                conn.commit()
            finally:
                conn.close()

        # 4) 신규 종목 메타 보완
        if missing_symbols:
            sym_list = sorted(missing_symbols)
            for i in range(0, len(sym_list), 150):
                chunk = ",".join(sym_list[i : i + 150])
                try:
                    st_res = self.request_get(
                        client_id, client_secret, token_state, "/api/v1/stocks", params={"symbols": chunk}, base_url=base_url, group="STOCK", min_interval=0.25
                    )
                    st_items = st_res if isinstance(st_res, list) else (st_res.get("stocks") or st_res.get("items") or [])
                    for st in st_items:
                        s_code = alias_map.get(str(st.get("symbol") or "").strip().upper(), str(st.get("symbol") or "").strip().upper())
                        if len(s_code) == 7 and s_code.startswith("A"):
                            s_code = s_code[1:]
                        if s_code:
                            mkt, cur = _infer_market_and_currency(s_code, st.get("marketCountry") or st.get("market"), st.get("currency"))
                            known_symbols[s_code] = {
                                "name": str(st.get("name") or st.get("koreanName") or s_code),
                                "market": mkt,
                                "currency": cur,
                                "last_price": 0.0,
                            }
                except TossApiError:
                    pass

        # 5) SQLite에 보유종목 스냅샷 갱신 및 경계 중복 제거 후 증분 체결 저장
        new_trades = 0
        updated_trades = 0
        skipped_in_pdf = 0
        newest_order_date = last_api_dt or pdf_cutoff or ""
        added_trades_list: List[Dict[str, Any]] = []
        updated_trades_list: List[Dict[str, Any]] = []

        with DB_LOCK:
            conn = get_db()
            try:
                for sym, meta in known_symbols.items():
                    conn.execute(
                        """
                        INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            name = CASE WHEN excluded.name != excluded.symbol THEN excluded.name ELSE stocks.name END,
                            market = excluded.market,
                            currency = excluded.currency,
                            last_price = CASE WHEN excluded.last_price > 0 THEN excluded.last_price ELSE stocks.last_price END,
                            updated_at = excluded.updated_at
                        """,
                        (sym, meta["name"], meta["market"], meta["currency"], meta["last_price"], now_str),
                    )

                conn.execute("DELETE FROM holdings_snapshot WHERE account_key = ?", (account_key,))
                for h_tuple in parsed_holdings:
                    conn.execute(
                        """
                        INSERT INTO holdings_snapshot (
                            account_key, symbol, name, market, currency, quantity, avg_purchase_price,
                            last_price, purchase_amount, eval_amount, eval_amount_after_cost,
                            pnl_amount, pnl_amount_after_cost, pnl_rate, pnl_rate_after_cost,
                            commission, tax, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        h_tuple,
                    )

                # PDF 경계 구간 중복 판정용 기존 PDF 거래 로드
                boundary_pdf_rows = []
                hard_settled_cutoff = ""
                if pdf_cutoff:
                    hard_settled_cutoff = _subtract_business_days(pdf_cutoff, 5)
                    boundary_from = (datetime.date.fromisoformat(pdf_cutoff) - datetime.timedelta(days=12)).isoformat()
                    boundary_pdf_rows = [
                        dict(r)
                        for r in conn.execute(
                            """
                            SELECT id, symbol, side, trade_date, quantity, price
                            FROM trades
                            WHERE account_key = ? AND source = 'PDF' AND trade_date >= ?
                            ORDER BY trade_date ASC, id ASC
                            """,
                            (account_key, boundary_from),
                        ).fetchall()
                    ]
                matched_pdf_ids = set()

                for od in sorted(parsed_orders, key=lambda x: x["trade_date"]):
                    sym = od["symbol"]
                    meta = known_symbols.get(sym, {})
                    name = meta.get("name") or od["name"] or sym
                    mkt = meta.get("market") or od["market"]
                    cur = meta.get("currency") or od["currency"]
                    tr_dt = od["trade_date"]
                    if tr_dt > newest_order_date:
                        newest_order_date = tr_dt

                    existing = conn.execute(
                        "SELECT id, quantity, price, gross_amount FROM trades WHERE account_key = ? AND external_order_id = ?",
                        (account_key, od["order_id"]),
                    ).fetchone()
                    if existing:
                        changed = (
                            abs(float(existing["quantity"]) - float(od["quantity"])) > 1e-6
                            or abs(float(existing["gross_amount"]) - float(od["gross_amount"])) > 0.01
                        )
                        conn.execute(
                            """
                            UPDATE trades
                            SET symbol=?, name=?, market=?, currency=?, quantity=?, price=?, gross_amount=?, commission=?, tax=?, trade_date=?
                            WHERE id=?
                            """,
                            (sym, name, mkt, cur, od["quantity"], od["price"], od["gross_amount"], od["commission"], od["tax"], tr_dt, existing["id"]),
                        )
                        updated_trades += 1
                        if changed:
                            updated_trades_list.append(
                                {
                                    "symbol": sym,
                                    "name": name,
                                    "market": mkt,
                                    "currency": cur,
                                    "side": od["side"],
                                    "tradeDate": tr_dt,
                                    "quantity": float(od["quantity"]),
                                    "price": float(od["price"]),
                                    "grossAmount": float(od["gross_amount"]),
                                }
                            )
                        continue

                    # PDF 원장 커트오프 경계 중복 차단:
                    # 1) pdf_cutoff 대비 5영업일 이상 이전 체결분은 휴일 포함 100% PDF에 결제 반영 완료 -> 스킵
                    # 2) 경계 구간(hard_settled_cutoff < tr_dt <= pdf_cutoff)은 실제 PDF 결제 행(단건·동일결제일 분할합산)에 매칭될 때만 스킵하고,
                    #    아직 미결제(예: 미국장 체결 후 한국시간 +2영업일 결제로 PDF에 미포함)인 주문은 누락 없이 API 체결로 반영!
                    if pdf_cutoff:
                        if hard_settled_cutoff and tr_dt <= hard_settled_cutoff:
                            skipped_in_pdf += 1
                            continue
                        if tr_dt <= pdf_cutoff and _match_order_in_pdf_rows(
                            sym,
                            od["side"],
                            tr_dt,
                            float(od["quantity"]),
                            float(od["price"]),
                            cur,
                            boundary_pdf_rows,
                            matched_pdf_ids,
                        ):
                            skipped_in_pdf += 1
                            continue

                    conn.execute(
                        """
                        INSERT INTO trades (
                            account_key, external_order_id, symbol, name, market, currency,
                            side, trade_date, quantity, price, gross_amount, commission, tax, source, memo
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'API', ?)
                        """,
                        (account_key, od["order_id"], sym, name, mkt, cur, od["side"], tr_dt, od["quantity"], od["price"], od["gross_amount"], od["commission"], od["tax"], od["memo"]),
                    )
                    new_trades += 1
                    added_trades_list.append(
                        {
                            "symbol": sym,
                            "name": name,
                            "market": mkt,
                            "currency": cur,
                            "side": od["side"],
                            "tradeDate": tr_dt,
                            "quantity": float(od["quantity"]),
                            "price": float(od["price"]),
                            "grossAmount": float(od["gross_amount"]),
                            "commission": float(od["commission"]),
                            "tax": float(od["tax"]),
                            "memo": od["memo"],
                        }
                    )

                if newest_order_date:
                    conn.execute(
                        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                        (f"last_api_order_date_{account_key}", newest_order_date),
                    )
                if pdf_cutoff:
                    conn.execute(
                        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                        (f"boundary_v2_reconciled_{account_key}", pdf_cutoff),
                    )
                conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (f"last_sync_{account_key}", now_str))
                conn.commit()
            finally:
                conn.close()

        # 6) PDF 기준일 이후 도래한 신규 배당만 증분 산출 (단독 로그 중복 방지를 위해 record_log=False)
        div_sync_res = AUTO_DIV_SYNC.sync_account_dividends(account_key, force_refresh=False, record_log=False)
        newly_added_divs = div_sync_res.get("newlyAddedDividends") or []
        retained_auto_divs = div_sync_res.get("retainedAutoDividends") or []

        log_summary = (
            f"토스 API 증분 동기화 완료 · 실시간 보유 {len(parsed_holdings)}종목 · "
            f"신규 체결 {new_trades}건 추가 (기반영 중복차단 {skipped_in_pdf + (updated_trades - len(updated_trades_list))}건) · "
            f"신규 이후배당 {len(newly_added_divs)}건 추가"
        )
        append_sync_log(
            account_key=account_key,
            action_type="TOSS_SYNC",
            status="SUCCESS",
            summary=log_summary,
            details={
                "incrementalFrom": effective_from,
                "pdfCutoffDate": pdf_cutoff,
                "holdingsCount": len(parsed_holdings),
                "closedOrdersFetched": len(parsed_orders),
                "newTradesSaved": new_trades,
                "updatedTrades": len(updated_trades_list),
                "skippedAlreadyInPdf": skipped_in_pdf,
                "dividendsGenerated": div_sync_res.get("dividendsGenerated", 0),
                "newlyAddedDividendCount": len(newly_added_divs),
                "addedTrades": added_trades_list,
                "updatedTradesList": updated_trades_list,
                "newlyAddedDividends": newly_added_divs,
                "retainedAutoDividends": retained_auto_divs,
            },
            created_at=now_str,
        )

        return {
            "accounts": accounts,
            "selectedAccountSeq": account_seq,
            "accountKey": account_key,
            "accessToken": token_state[0],
            "tokenExpiresAt": token_state[1],
            "sync": {
                "syncedAt": now_str,
                "incrementalFrom": effective_from,
                "pdfCutoffDate": pdf_cutoff,
                "holdingsCount": len(parsed_holdings),
                "closedOrdersFetched": len(parsed_orders),
                "newTradesSaved": new_trades,
                "updatedTrades": len(updated_trades_list),
                "skippedAlreadyInPdf": skipped_in_pdf,
                "dividendsGenerated": div_sync_res.get("dividendsGenerated", 0),
                "newlyAddedDividendCount": len(newly_added_divs),
                "addedTrades": added_trades_list,
                "newlyAddedDividends": newly_added_divs,
            },
        }

    def _fetch_incremental_closed_orders(
        self,
        client_id: str,
        client_secret: str,
        token_state: List[Any],
        account_seq: Any,
        base_url: str,
        from_date: Optional[str],
    ) -> List[Dict[str, Any]]:
        today_kst = datetime.datetime.now(KST_TZ).date().isoformat()
        tomorrow_kst = (datetime.datetime.now(KST_TZ).date() + datetime.timedelta(days=1)).isoformat()
        collected: Dict[str, Dict[str, Any]] = {}

        if from_date:
            # 증분 모드: 당일 심야(미국 정규장) 체결분이 to 자정 경계에 잘리지 않도록 to 없이 우선 조회 후 폴백
            for to_candidate in (None, tomorrow_kst, today_kst):
                try:
                    self._paginate_closed_orders(
                        client_id, client_secret, token_state, account_seq, base_url,
                        from_date, to_candidate, collected, stop_before_date=from_date,
                    )
                    return list(collected.values())
                except TossApiError as e:
                    if e.status in (400, 422):
                        collected.clear()
                        continue
                    raise
            # 서버가 기간 파라미터 없이 커서 조회만 허용하는 경우 최신 페이지부터 from_date 이전 도달 시 즉시 중단
            self._paginate_closed_orders(
                client_id, client_secret, token_state, account_seq, base_url,
                None, None, collected, stop_before_date=from_date,
            )
            return list(collected.values())

        self._paginate_closed_orders(client_id, client_secret, token_state, account_seq, base_url, None, None, collected)
        return list(collected.values())

    def _paginate_closed_orders(
        self,
        client_id: str,
        client_secret: str,
        token_state: List[Any],
        account_seq: Any,
        base_url: str,
        from_str: Optional[str],
        to_str: Optional[str],
        collected: Dict[str, Dict[str, Any]],
        stop_before_date: Optional[str] = None,
    ) -> None:
        cursor = None
        for _ in range(300):
            params: Dict[str, Any] = {"status": "CLOSED"}
            if from_str:
                params["from"] = from_str
            if to_str:
                params["to"] = to_str
            if cursor:
                params["cursor"] = cursor

            res = self.request_get(
                client_id, client_secret, token_state, "/api/v1/orders", params=params, account_seq=account_seq, base_url=base_url, group="ORDER_HISTORY", min_interval=0.22
            )
            orders = res if isinstance(res, list) else (res.get("orders") or res.get("items") or [])
            next_cursor = None if isinstance(res, list) else (res.get("nextCursor") or res.get("cursor") or res.get("next"))
            if not orders:
                break

            reached_older = False
            for od in orders:
                oid = str(od.get("orderId") or od.get("id") or "")
                exec_info = od.get("execution") if isinstance(od.get("execution"), dict) else {}
                raw_dt = str(
                    exec_info.get("executedAt")
                    or od.get("executedAt")
                    or od.get("orderedAt")
                    or od.get("createdAt")
                    or ""
                )[:10]
                if stop_before_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_dt) and raw_dt < stop_before_date:
                    reached_older = True
                    continue
                if oid:
                    collected[oid] = od

            if reached_older or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

    def _parse_closed_order(
        self,
        conn: sqlite3.Connection,
        od: Dict[str, Any],
        known_symbols: Dict[str, Dict[str, Any]],
        alias_map: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        oid = str(od.get("orderId") or od.get("id") or "").strip()
        raw_sym = str(od.get("symbol") or "").strip()
        side = str(od.get("side") or od.get("orderSide") or "").upper().strip()
        if not oid or not raw_sym or side not in ("BUY", "SELL"):
            return None

        raw_name = str(od.get("name") or od.get("stockName") or "")
        sym = canonicalize_symbol_with_db(conn, raw_sym, raw_name, alias_cache=alias_map)

        exec_info = od.get("execution") if isinstance(od.get("execution"), dict) else {}
        cost_info = od.get("cost") if isinstance(od.get("cost"), dict) else (
            exec_info.get("cost") if isinstance(exec_info.get("cost"), dict) else {}
        )

        filled_qty = _to_float(
            exec_info.get("filledQuantity")
            or exec_info.get("executedQuantity")
            or od.get("filledQuantity")
            or od.get("executedQuantity")
            or (od.get("quantity") if str(od.get("status", "")).upper() == "FILLED" else 0)
        )
        if filled_qty <= 0:
            return None

        exec_price = _to_float(
            exec_info.get("averagePrice")
            or exec_info.get("filledPrice")
            or exec_info.get("executionPrice")
            or od.get("averageExecutionPrice")
            or od.get("averagePrice")
            or od.get("filledPrice")
            or od.get("price")
        )
        gross_amt = _to_float(
            exec_info.get("filledAmount")
            or exec_info.get("executedAmount")
            or exec_info.get("amount")
            or od.get("filledAmount")
            or od.get("executedAmount"),
            filled_qty * exec_price,
        )
        if exec_price <= 0 and gross_amt > 0 and filled_qty > 0:
            exec_price = gross_amt / filled_qty

        meta = known_symbols.get(sym, {})
        mkt, cur = _infer_market_and_currency(
            sym,
            od.get("marketCountry") or od.get("market") or meta.get("market", ""),
            od.get("currency") or exec_info.get("currency") or meta.get("currency", ""),
        )
        name = raw_name or meta.get("name") or sym
        commission = _to_float(cost_info.get("commission") or exec_info.get("commission") or od.get("commission"))
        tax = _to_float(cost_info.get("tax") or exec_info.get("tax") or od.get("tax"))

        raw_dt = str(
            exec_info.get("executedAt")
            or exec_info.get("filledAt")
            or od.get("executedAt")
            or od.get("orderedAt")
            or od.get("createdAt")
            or datetime.date.today().isoformat()
        )
        trade_date = _normalize_trade_date(raw_dt, mkt)
        order_type = str(od.get("orderType") or "")
        memo = f"OpenAPI 증분동기화 ({order_type})" if order_type else "OpenAPI 증분동기화"

        return {
            "order_id": oid,
            "symbol": sym,
            "name": name,
            "market": mkt,
            "currency": cur,
            "side": side,
            "trade_date": trade_date,
            "quantity": filled_qty,
            "price": exec_price,
            "gross_amount": gross_amt,
            "commission": commission,
            "tax": tax,
            "memo": memo,
        }


TOSS_SYNC = TossOpenApiSyncService()


def _fetch_public_live_quote(symbol: str, market: str) -> Optional[float]:
    """공개 실시간 시세 조회 (국내: 네이버증권 모바일 API + Yahoo 폴백 / 미국: Yahoo 1분봉 실시간·프리·애프터장 시세)"""
    sym = symbol.strip().upper()
    mkt = market.strip().upper()

    if mkt == "KR":
        clean_sym = sym[1:] if (len(sym) == 7 and sym.startswith("A")) else sym
        for naver_url in (
            f"https://m.stock.naver.com/api/stock/{urllib.parse.quote(clean_sym)}/basic",
            f"https://m.stock.naver.com/api/etf/{urllib.parse.quote(clean_sym)}/basic",
        ):
            try:
                raw = _http_get_text(
                    naver_url,
                    extra_headers={"Accept": "application/json", "Referer": "https://m.stock.naver.com/"},
                    timeout=6.0,
                )
                obj = json.loads(raw)
                over_info = obj.get("overMarketPriceInfo") if isinstance(obj.get("overMarketPriceInfo"), dict) else {}
                if str(over_info.get("overMarketStatus") or "").upper() == "OPEN":
                    over_p = _to_float(over_info.get("overPrice"), 0.0)
                    if over_p > 0:
                        return over_p
                close_p = _to_float(obj.get("closePrice"), 0.0)
                if close_p > 0:
                    return close_p
            except Exception:
                continue

        for suffix in (".KS", ".KQ"):
            yf_p = _fetch_yahoo_live_price(f"{clean_sym}{suffix}")
            if yf_p and yf_p > 0:
                return float(round(yf_p))
        return None

    yahoo_sym = sym.replace(".", "-").replace("/", "-")
    return _fetch_yahoo_live_price(yahoo_sym)


def _fetch_yahoo_live_price(yahoo_symbol: str) -> Optional[float]:
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = (
            f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}"
            "?interval=1m&range=1d&includePrePost=true"
        )
        try:
            raw_json = _http_get_text(url, extra_headers={"Accept": "application/json"}, timeout=6.5)
            payload = json.loads(raw_json)
            results = ((payload.get("chart") or {}).get("result")) or []
            if not results:
                continue
            res0 = results[0]
            quotes = ((res0.get("indicators") or {}).get("quote")) or []
            if quotes and isinstance(quotes[0], dict):
                closes = quotes[0].get("close") or []
                for c in reversed(closes):
                    if c is not None and float(c) > 0:
                        return round(float(c), 4)
            meta = res0.get("meta") or {}
            reg_p = _to_float(meta.get("regularMarketPrice"), 0.0)
            if reg_p > 0:
                return round(reg_p, 4)
        except Exception:
            continue
    return None


def refresh_portfolio_live_prices(
    requested_account_key: str,
    client_id: str = "",
    client_secret: str = "",
    base_url: str = TOSS_DEFAULT_BASE_URL,
    cached_token: Optional[str] = None,
    token_expires_at: float = 0.0,
    target_account_seq: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    보유 종목 현재가 전용 초고속 새로고침 (100% 열람 전용):
    1) API 잠금 해제 상태면 토스 공식 GET /api/v1/holdings 로 실시간 잔고·현재가·평가액을 즉시 갱신
    2) 원장 단독 열람 모드이거나 토스 응답에서 누락/실패한 종목은 네이버·야후 실시간 시세로 즉시 보완
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    used_toss = False
    used_public = False
    new_tok: Optional[str] = None
    new_exp: float = 0.0

    with DB_LOCK:
        conn = get_db()
        try:
            eff_key = resolve_and_migrate_account_key(conn, requested_account_key)
            alias_map = _load_alias_map(conn)
        finally:
            conn.close()

    missing_public_targets: Dict[str, str] = {}
    held_count = 0
    updated_count = 0

    # 1단계: 토스 Open API 자격증명이 있으면 공식 실시간 잔고·현재가 1회 조회
    if client_id and client_secret and eff_key != "DEMO":
        try:
            tok, exp = TOSS_SYNC.obtain_token(client_id, client_secret, base_url, cached_token, token_expires_at)
            token_state: List[Any] = [tok, exp]
            acc_seq = target_account_seq
            if acc_seq is None or str(acc_seq).strip() == "":
                acc_res = TOSS_SYNC.request_get(
                    client_id, client_secret, token_state, "/api/v1/accounts", base_url=base_url, group="ACCOUNT", min_interval=0.5
                )
                raw_accs = acc_res if isinstance(acc_res, list) else (acc_res.get("accounts") or acc_res.get("items") or [])
                if raw_accs:
                    acc_seq = raw_accs[0].get("accountSeq")

            if acc_seq is not None:
                eff_key = f"ACC_{acc_seq}"
                holdings_res = TOSS_SYNC.request_get(
                    client_id, client_secret, token_state, "/api/v1/holdings",
                    account_seq=acc_seq, base_url=base_url, group="ASSET", min_interval=0.2,
                )
                raw_holdings = (
                    holdings_res.get("items") or holdings_res.get("holdings") or []
                    if isinstance(holdings_res, dict)
                    else holdings_res
                )
                parsed_holdings = []
                stock_price_updates = []
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, eff_key)
                        for row in raw_holdings:
                            raw_sym = str(row.get("symbol") or "").strip()
                            if not raw_sym:
                                continue
                            raw_name = str(row.get("name") or row.get("stockName") or raw_sym).strip()
                            sym = canonicalize_symbol_with_db(conn, raw_sym, raw_name, alias_cache=alias_map)
                            mkt, cur = _infer_market_and_currency(
                                sym, row.get("marketCountry") or row.get("market"), row.get("currency")
                            )
                            qty = _to_float(row.get("quantity"))
                            if qty <= 1e-9:
                                continue
                            avg_p = _to_float(row.get("averagePurchasePrice") or row.get("averagePrice"))
                            last_p = _to_float(row.get("lastPrice") or row.get("currentPrice"))

                            mv = row.get("marketValue") if isinstance(row.get("marketValue"), dict) else {}
                            pl = row.get("profitLoss") if isinstance(row.get("profitLoss"), dict) else {}
                            cost = row.get("cost") if isinstance(row.get("cost"), dict) else {}

                            purchase_amt = _to_float(mv.get("purchaseAmount"), qty * avg_p)
                            eval_amt = _to_float(mv.get("amount"), qty * last_p)
                            if last_p <= 0 and eval_amt > 0 and qty > 0:
                                last_p = eval_amt / qty
                            comm = _to_float(cost.get("commission"))
                            tax = _to_float(cost.get("tax"))
                            eval_after = _to_float(mv.get("amountAfterCost"), eval_amt - comm - tax)
                            pnl_amt = _to_float(pl.get("amount"), eval_amt - purchase_amt)
                            pnl_after = _to_float(pl.get("amountAfterCost"), pnl_amt - comm - tax)
                            pnl_rate = _to_float(pl.get("rate"))
                            pnl_rate_after = _to_float(pl.get("rateAfterCost"))

                            parsed_holdings.append(
                                (
                                    eff_key, sym, raw_name, mkt, cur, qty, avg_p, last_p,
                                    purchase_amt, eval_amt, eval_after, pnl_amt, pnl_after,
                                    pnl_rate, pnl_rate_after, comm, tax, now_str,
                                )
                            )
                            stock_price_updates.append((sym, raw_name, mkt, cur, last_p, now_str))
                            if last_p > 0:
                                updated_count += 1
                            else:
                                missing_public_targets[sym] = mkt

                        conn.execute("DELETE FROM holdings_snapshot WHERE account_key = ?", (eff_key,))
                        for h_tuple in parsed_holdings:
                            conn.execute(
                                """
                                INSERT INTO holdings_snapshot (
                                    account_key, symbol, name, market, currency, quantity, avg_purchase_price,
                                    last_price, purchase_amount, eval_amount, eval_amount_after_cost,
                                    pnl_amount, pnl_amount_after_cost, pnl_rate, pnl_rate_after_cost,
                                    commission, tax, synced_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                h_tuple,
                            )
                        for sym, nm, mkt, cur, lp, ts in stock_price_updates:
                            conn.execute(
                                """
                                INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(symbol) DO UPDATE SET
                                    last_price = CASE WHEN excluded.last_price > 0 THEN excluded.last_price ELSE stocks.last_price END,
                                    updated_at = excluded.updated_at
                                """,
                                (sym, nm, mkt, cur, lp, ts),
                            )
                        conn.execute(
                            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                            (f"last_price_refresh_{eff_key}", now_str),
                        )
                        conn.commit()
                    finally:
                        conn.close()

                held_count = len(parsed_holdings)
                used_toss = True
                new_tok, new_exp = token_state[0], token_state[1]
        except Exception:
            used_toss = False

    # 2단계: 원장 단독 모드이거나 토스 API 미사용/누락 종목이 있으면 공개 실시간 시세 병렬 조회
    if not used_toss:
        analytics_cur = compute_portfolio_analytics(eff_key)
        held_items = [
            it for it in analytics_cur["items"]
            if it["status"] == "HELD" and not it.get("isPriorYearlyBucket")
        ]
        held_count = len(held_items)
        for it in held_items:
            missing_public_targets[it["symbol"]] = it["market"]

    if missing_public_targets:
        fetched_prices: Dict[str, float] = {}

        def _q_worker(item_kv: Tuple[str, str]) -> Tuple[str, Optional[float]]:
            s_code, m_code = item_kv
            return s_code, _fetch_public_live_quote(s_code, m_code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(_q_worker, kv) for kv in missing_public_targets.items()]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    s_code, p_val = fut.result()
                    if p_val is not None and p_val > 0:
                        fetched_prices[s_code] = p_val
                except Exception:
                    pass

        if fetched_prices:
            used_public = True
            with DB_LOCK:
                conn = get_db()
                try:
                    for s_code, p_val in fetched_prices.items():
                        conn.execute(
                            "UPDATE stocks SET last_price = ?, updated_at = ? WHERE symbol = ?",
                            (p_val, now_str, s_code),
                        )
                        h_row = conn.execute(
                            "SELECT quantity, purchase_amount, commission, tax, currency FROM holdings_snapshot WHERE account_key = ? AND symbol = ?",
                            (eff_key, s_code),
                        ).fetchone()
                        if h_row:
                            q_v = float(h_row["quantity"])
                            p_amt = float(h_row["purchase_amount"])
                            c_v = float(h_row["commission"])
                            t_v = float(h_row["tax"])
                            ev_amt = float(round(q_v * p_val)) if h_row["currency"] == "KRW" else (q_v * p_val)
                            ev_after = ev_amt - c_v - t_v
                            pnl_a = ev_amt - p_amt
                            pnl_af = pnl_a - c_v - t_v
                            pnl_r = (pnl_a / p_amt) if p_amt > 0 else 0.0
                            pnl_rf = (pnl_af / p_amt) if p_amt > 0 else 0.0
                            conn.execute(
                                """
                                UPDATE holdings_snapshot
                                SET last_price = ?, eval_amount = ?, eval_amount_after_cost = ?,
                                    pnl_amount = ?, pnl_amount_after_cost = ?,
                                    pnl_rate = ?, pnl_rate_after_cost = ?, synced_at = ?
                                WHERE account_key = ? AND symbol = ?
                                """,
                                (p_val, ev_amt, ev_after, pnl_a, pnl_af, pnl_r, pnl_rf, now_str, eff_key, s_code),
                            )
                    conn.execute(
                        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                        (f"last_price_refresh_{eff_key}", now_str),
                    )
                    conn.commit()
                finally:
                    conn.close()
            updated_count += len(fetched_prices)

    src_mode = "HYBRID" if (used_toss and used_public) else ("TOSS_API" if used_toss else "PUBLIC_QUOTE")
    out: Dict[str, Any] = {
        "accountKey": eff_key,
        "heldCount": held_count,
        "updatedCount": updated_count,
        "refreshedAt": now_str,
        "source": src_mode,
    }
    if new_tok:
        out["accessToken"] = new_tok
        out["tokenExpiresAt"] = new_exp
    return out


# ============================================================================
# 4. 종목별 매수·매도·타사입고·분할병합 + 배당 합산 수익 계산 엔진
# ============================================================================

def compute_portfolio_analytics(requested_account_key: str) -> Dict[str, Any]:
    with DB_LOCK:
        conn = get_db()
        try:
            account_key = resolve_and_migrate_account_key(conn, requested_account_key)
            pdf_cutoff = get_pdf_cutoff_date(conn)
            pdf_imported_row = conn.execute("SELECT value FROM app_meta WHERE key='pdf_imported_at'").fetchone()
            pdf_imported_at = pdf_imported_row["value"] if pdf_imported_row else None

            stocks_map = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM stocks").fetchall()}
            holdings_rows = [
                dict(r) for r in conn.execute("SELECT * FROM holdings_snapshot WHERE account_key = ?", (account_key,)).fetchall()
            ]
            trades_rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM trades
                    WHERE account_key = ?
                    ORDER BY trade_date ASC,
                             CASE WHEN external_order_id LIKE 'PDF_MERGE_FRAC_%' THEN 0
                                  WHEN side = 'BUY' THEN 1
                                  WHEN side = 'SPLIT_OUT' THEN 2
                                  WHEN side = 'SPLIT_IN' THEN 3
                                  WHEN side = 'SELL' THEN 4
                                  ELSE 5 END ASC,
                             id ASC
                    """,
                    (account_key,),
                ).fetchall()
            ]
            dividends_rows = [
                dict(r) for r in conn.execute("SELECT * FROM dividends WHERE account_key = ? ORDER BY pay_date DESC, id DESC", (account_key,)).fetchall()
            ]
            last_sync_row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (f"last_sync_{account_key}",)).fetchone()
            last_sync = last_sync_row["value"] if last_sync_row else None
            last_div_sync_row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (f"last_div_sync_{account_key}",)).fetchone()
            last_div_sync = last_div_sync_row["value"] if last_div_sync_row else None
        finally:
            conn.close()

    holdings_by_sym = {h["symbol"]: h for h in holdings_rows}
    all_symbols = set(holdings_by_sym.keys()) | {t["symbol"] for t in trades_rows} | {d["symbol"] for d in dividends_rows}

    trades_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades_rows:
        trades_by_sym.setdefault(t["symbol"], []).append(t)

    divs_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for d in dividends_rows:
        divs_by_sym.setdefault(d["symbol"], []).append(d)

    items: List[Dict[str, Any]] = []

    for sym in sorted(all_symbols):
        h = holdings_by_sym.get(sym)
        st = stocks_map.get(sym, {})
        sym_trades = trades_by_sym.get(sym, [])
        sym_divs = divs_by_sym.get(sym, [])

        name = (
            (h and h["name"])
            or (st.get("name") if st.get("name") and st.get("name") != sym else None)
            or next((x["name"] for x in reversed(sym_trades) if x.get("name")), None)
            or next((x["name"] for x in sym_divs if x.get("name")), None)
            or sym
        )
        raw_mkt = (h and h["market"]) or st.get("market") or (sym_trades[0]["market"] if sym_trades else "") or (sym_divs[0]["market"] if sym_divs else "")
        raw_cur = (h and h["currency"]) or st.get("currency") or (sym_trades[0]["currency"] if sym_trades else "") or (sym_divs[0]["currency"] if sym_divs else "")
        market, currency = _infer_market_and_currency(sym, raw_mkt, raw_cur)
        last_price = (h and h["last_price"]) or st.get("last_price") or 0.0

        running_qty = 0.0
        running_avg_cost = 0.0
        pending_split_cost = 0.0

        total_buy_gross = 0.0
        total_buy_qty = 0.0
        total_sell_gross = 0.0
        total_sell_qty = 0.0

        transfer_in_qty = 0.0
        transfer_in_gross = 0.0
        first_transfer_date: Optional[str] = None

        realized_pnl_gross = 0.0
        trade_commissions = 0.0
        trade_taxes = 0.0
        unmatched_sell_qty = 0.0

        enriched_trades = []
        for tr in sym_trades:
            side = tr["side"]
            qty = float(tr["quantity"])
            gross = float(tr["gross_amount"])
            comm = float(tr["commission"])
            tax = float(tr["tax"])
            trade_commissions += comm
            trade_taxes += tax

            if tr.get("source") == "PDF_TRANSFER":
                transfer_in_qty += qty
                transfer_in_gross += gross
                if first_transfer_date is None or tr["trade_date"] < first_transfer_date:
                    first_transfer_date = tr["trade_date"]

            row_realized_gross = None
            row_realized_net = None

            if side == "BUY":
                new_cost = running_qty * running_avg_cost + gross
                running_qty += qty
                running_avg_cost = (new_cost / running_qty) if running_qty > 1e-9 else 0.0
                total_buy_gross += gross
                total_buy_qty += qty
            elif side == "SPLIT_OUT":
                take_q = min(qty, running_qty)
                pending_split_cost += take_q * running_avg_cost
                running_qty = max(0.0, running_qty - take_q)
                if running_qty <= 1e-9:
                    running_qty = 0.0
            elif side == "SPLIT_IN":
                new_cost = running_qty * running_avg_cost + pending_split_cost
                pending_split_cost = 0.0
                running_qty += qty
                running_avg_cost = (new_cost / running_qty) if running_qty > 1e-9 else 0.0
            elif side == "SELL":
                total_sell_gross += gross
                total_sell_qty += qty
                if running_qty >= qty - 1e-9:
                    cost_basis = running_avg_cost * qty
                    row_realized_gross = gross - cost_basis
                    row_realized_net = row_realized_gross - comm - tax
                    realized_pnl_gross += row_realized_gross
                    running_qty = max(0.0, running_qty - qty)
                    if running_qty <= 1e-9:
                        running_qty = 0.0
                        running_avg_cost = 0.0
                elif running_qty > 1e-9:
                    fallback_avg = (h["avg_purchase_price"] if h and h["avg_purchase_price"] > 0 else running_avg_cost)
                    cost_basis = running_qty * running_avg_cost + (qty - running_qty) * fallback_avg
                    row_realized_gross = gross - cost_basis
                    row_realized_net = row_realized_gross - comm - tax
                    realized_pnl_gross += row_realized_gross
                    unmatched_sell_qty += (qty - running_qty)
                    running_qty = 0.0
                    running_avg_cost = 0.0
                else:
                    if h and h["avg_purchase_price"] > 0:
                        cost_basis = qty * float(h["avg_purchase_price"])
                        row_realized_gross = gross - cost_basis
                        row_realized_net = row_realized_gross - comm - tax
                        realized_pnl_gross += row_realized_gross
                    else:
                        unmatched_sell_qty += qty

            enriched_trades.append(
                {
                    **tr,
                    "realizedPnlGross": row_realized_gross,
                    "realizedPnlNet": row_realized_net,
                }
            )

        # 보유 상태 및 미실현 평가손익 계산:
        # API 동기화 후 holdings_snapshot이 있으면 실시간 현재가(last_price)를 반영하되,
        # 타사대체입고 반영 원가(running_avg_cost) 기준으로 일관되게 평가손익 계산
        if h is not None and float(h["quantity"]) > 1e-9:
            is_held = True
            current_qty = float(h["quantity"])
            if last_price <= 0 and float(h["last_price"]) > 0:
                last_price = float(h["last_price"])
            # PDF 원장에서 타사대체입고를 입고일 매수로 산출한 이동평균 원가가 있으면 우선 활용, 없으면 API 평단 활용
            avg_price = running_avg_cost if (running_qty > 1e-6 and running_avg_cost > 0) else float(h["avg_purchase_price"])
            current_invested = current_qty * avg_price
            eval_amount = (current_qty * last_price) if last_price > 0 else float(h["eval_amount"])
            if currency == "KRW":
                eval_amount = float(round(eval_amount))
            holding_comm = float(h["commission"])
            holding_tax = float(h["tax"])
            unrealized_gross = eval_amount - current_invested
            unrealized_net = unrealized_gross - holding_comm - holding_tax
        elif h is None and running_qty > 1e-6:
            is_held = True
            current_qty = running_qty
            avg_price = running_avg_cost
            current_invested = current_qty * avg_price
            eval_amount = current_qty * last_price if last_price > 0 else current_invested
            unrealized_gross = eval_amount - current_invested
            holding_comm = 0.0
            holding_tax = 0.0
            unrealized_net = unrealized_gross
        else:
            is_held = False
            current_qty = 0.0
            avg_price = 0.0
            current_invested = 0.0
            eval_amount = 0.0
            unrealized_gross = 0.0
            holding_comm = 0.0
            holding_tax = 0.0
            unrealized_net = 0.0

        realized_pnl_after_tax = realized_pnl_gross - trade_taxes - trade_commissions
        realized_pnl_pre_tax = realized_pnl_gross - trade_commissions
        unrealized_pre_tax = unrealized_gross - holding_comm

        is_prior_yearly_bucket = sym in PRIOR_YEARLY_SYMBOLS
        if is_prior_yearly_bucket:
            if not sym_divs:
                continue
            bucket_meta = PRIOR_YEARLY_SYMBOLS[sym]
            name = bucket_meta[0]
            market = bucket_meta[1]
            currency = bucket_meta[2]

        enriched_divs = []
        div_gross_total = 0.0
        div_tax_total = 0.0
        div_net_total = 0.0
        div_krw_gross_total = 0.0
        div_krw_tax_total = 0.0
        div_krw_net_total = 0.0

        for d in sym_divs:
            dg = float(d["gross_amount"])
            dt = float(d["tax_amount"])
            dn = float(d["net_amount"])
            div_gross_total += dg
            div_tax_total += dt
            div_net_total += dn

            if currency == "KRW":
                fx_r = 1.0
                kg, kt, kn = round(dg), round(dt), round(dn)
                fx_exact = True
            else:
                has_stored_krw = d.get("krw_net") is not None
                fx_r = float(d["fx_rate"]) if (d.get("fx_rate") is not None and float(d["fx_rate"]) > 0) else _fallback_usdkrw_rate(d.get("pay_date", ""))
                if has_stored_krw:
                    kn = float(round(float(d["krw_net"])))
                    kt = float(round(float(d["krw_tax"] or 0.0)))
                    kg = float(round(float(d["krw_gross"] if d.get("krw_gross") is not None else (kn + kt))))
                    fx_exact = True
                else:
                    kn = float(round(dn * fx_r))
                    kt = float(round(dt * fx_r))
                    kg = kn + kt
                    fx_exact = False

            div_krw_gross_total += kg
            div_krw_tax_total += kt
            div_krw_net_total += kn
            enriched_divs.append(
                {
                    **d,
                    "fx_rate": fx_r,
                    "krw_gross": kg,
                    "krw_tax": kt,
                    "krw_net": kn,
                    "fx_exact": fx_exact,
                }
            )

        # 배당 지급 횟수(세금재정산 단독 조정행 제외한 실지급 건수)
        payout_div_count = sum(
            1
            for d in enriched_divs
            if float(d["gross_amount"]) > 0 or d.get("source") in ("PRIOR_BROKER", "PRIOR_YEARLY")
        )

        prior_div_row = next((d for d in enriched_divs if d.get("source") == "PRIOR_BROKER"), None)
        has_ext_div = any(d.get("source") == "EXT_DIV" for d in enriched_divs)
        ext_div_net = sum(float(d["net_amount"]) for d in enriched_divs if d.get("source") == "EXT_DIV")
        prior_broker_div = (
            {
                "id": prior_div_row["id"],
                "payDate": prior_div_row["pay_date"],
                "grossAmount": float(prior_div_row["gross_amount"]),
                "taxAmount": float(prior_div_row["tax_amount"]),
                "netAmount": float(prior_div_row["net_amount"]),
                "memo": prior_div_row["memo"] or "",
            }
            if prior_div_row
            else None
        )

        capital_pnl_net = realized_pnl_after_tax + unrealized_net
        capital_pnl_gross = realized_pnl_pre_tax + unrealized_pre_tax

        total_return_net = capital_pnl_net + div_net_total
        total_return_gross = capital_pnl_gross + div_gross_total

        total_tax_paid = div_tax_total + trade_taxes + holding_tax
        total_commission_paid = trade_commissions + holding_comm

        denominator = max(total_buy_gross, current_invested)
        if denominator <= 0 and total_sell_gross > 0:
            denominator = total_sell_gross

        items.append(
            {
                "symbol": sym,
                "isin": "" if is_prior_yearly_bucket else (st.get("isin") or ""),
                "name": name,
                "market": market,
                "currency": currency,
                "status": "PRIOR_YEARLY" if is_prior_yearly_bucket else ("HELD" if is_held else "SOLD"),
                "isPriorYearlyBucket": is_prior_yearly_bucket,
                "currentQty": current_qty,
                "avgPrice": avg_price,
                "lastPrice": last_price,
                "currentInvested": current_invested,
                "evalAmount": eval_amount,
                "totalBuyGross": total_buy_gross,
                "totalBuyQty": total_buy_qty,
                "totalSellGross": total_sell_gross,
                "totalSellQty": total_sell_qty,
                "hasTransferIn": transfer_in_qty > 0,
                "transferInQty": round(transfer_in_qty, 6),
                "transferInGross": transfer_in_gross,
                "firstTransferDate": first_transfer_date,
                "priorBrokerDiv": prior_broker_div,
                "hasExtDiv": has_ext_div,
                "extDivNet": round(ext_div_net, 2),
                "costBasisDenominator": denominator,
                "realizedPnlNet": realized_pnl_after_tax,
                "realizedPnlGross": realized_pnl_pre_tax,
                "unrealizedPnlNet": unrealized_net,
                "unrealizedPnlGross": unrealized_pre_tax,
                "capitalPnlNet": capital_pnl_net,
                "capitalPnlGross": capital_pnl_gross,
                "dividendNet": div_net_total,
                "dividendGross": div_gross_total,
                "dividendTax": div_tax_total,
                "dividendNetKrw": div_krw_net_total,
                "dividendGrossKrw": div_krw_gross_total,
                "dividendTaxKrw": div_krw_tax_total,
                "dividendCount": payout_div_count,
                "dividendYieldNet": (div_net_total / denominator * 100.0) if denominator > 0 else 0.0,
                "dividendYieldGross": (div_gross_total / denominator * 100.0) if denominator > 0 else 0.0,
                "totalReturnNet": total_return_net,
                "totalReturnGross": total_return_gross,
                "returnRateNet": (total_return_net / denominator * 100.0) if denominator > 0 else 0.0,
                "returnRateGross": (total_return_gross / denominator * 100.0) if denominator > 0 else 0.0,
                "tradeTax": trade_taxes + holding_tax,
                "totalTax": total_tax_paid,
                "totalCommission": total_commission_paid,
                "unmatchedSellQty": unmatched_sell_qty,
                "trades": list(reversed(enriched_trades)),
                "dividends": enriched_divs,
            }
        )

    summary: Dict[str, Dict[str, Any]] = {}
    for cur_code in ("KRW", "USD"):
        sub = [x for x in items if x["currency"] == cur_code]
        stock_sub = [x for x in sub if not x.get("isPriorYearlyBucket")]
        yearly_sub = [x for x in sub if x.get("isPriorYearlyBucket")]
        held_sub = [x for x in stock_sub if x["status"] == "HELD"]
        sold_sub = [x for x in stock_sub if x["status"] == "SOLD"]
        denom_sum = sum(x["costBasisDenominator"] for x in stock_sub)
        tot_ret_net = sum(x["totalReturnNet"] for x in sub)
        tot_ret_gross = sum(x["totalReturnGross"] for x in sub)
        sum_div_net = sum(x["dividendNet"] for x in sub)
        sum_div_gross = sum(x["dividendGross"] for x in sub)
        sum_div_net_krw = sum(x["dividendNetKrw"] for x in sub)
        sum_div_gross_krw = sum(x["dividendGrossKrw"] for x in sub)
        sum_div_tax_krw = sum(x["dividendTaxKrw"] for x in sub)
        avg_fx = (sum_div_net_krw / sum_div_net) if (cur_code == "USD" and sum_div_net > 0) else 1.0

        summary[cur_code] = {
            "currency": cur_code,
            "totalStocks": len(stock_sub),
            "heldStocks": len(held_sub),
            "soldStocks": len(sold_sub),
            "currentInvested": sum(x["currentInvested"] for x in held_sub),
            "evalAmount": sum(x["evalAmount"] for x in held_sub),
            "unrealizedPnlNet": sum(x["unrealizedPnlNet"] for x in sub),
            "unrealizedPnlGross": sum(x["unrealizedPnlGross"] for x in sub),
            "realizedPnlNet": sum(x["realizedPnlNet"] for x in sub),
            "realizedPnlGross": sum(x["realizedPnlGross"] for x in sub),
            "capitalPnlNet": sum(x["capitalPnlNet"] for x in sub),
            "capitalPnlGross": sum(x["capitalPnlGross"] for x in sub),
            "dividendNet": sum_div_net,
            "dividendGross": sum_div_gross,
            "dividendTax": sum(x["dividendTax"] for x in sub),
            "dividendNetKrw": sum_div_net_krw,
            "dividendGrossKrw": sum_div_gross_krw,
            "dividendTaxKrw": sum_div_tax_krw,
            "avgDividendFxRate": round(avg_fx, 2),
            "priorYearlyDividendNet": sum(x["dividendNet"] for x in yearly_sub),
            "priorYearlyDividendGross": sum(x["dividendGross"] for x in yearly_sub),
            "priorYearlyDividendTax": sum(x["dividendTax"] for x in yearly_sub),
            "priorYearlyDividendNetKrw": sum(x["dividendNetKrw"] for x in yearly_sub),
            "priorYearlyDividendGrossKrw": sum(x["dividendGrossKrw"] for x in yearly_sub),
            "tradeTax": sum(x["tradeTax"] for x in sub),
            "totalTax": sum(x["totalTax"] for x in sub),
            "totalCommission": sum(x["totalCommission"] for x in sub),
            "totalReturnNet": tot_ret_net,
            "totalReturnGross": tot_ret_gross,
            "returnRateNet": (tot_ret_net / denom_sum * 100.0) if denom_sum > 0 else 0.0,
            "returnRateGross": (tot_ret_gross / denom_sum * 100.0) if denom_sum > 0 else 0.0,
        }

    prior_yearly_by_year: Dict[int, Dict[str, Any]] = {}
    for d in dividends_rows:
        if d.get("source") == "PRIOR_YEARLY" and len(str(d.get("pay_date") or "")) >= 4:
            try:
                yr = int(str(d["pay_date"])[:4])
            except ValueError:
                continue
            bucket = prior_yearly_by_year.setdefault(yr, {"year": yr, "KRW": None, "USD": None})
            cur_k = "KRW" if d.get("currency") == "KRW" else "USD"
            bucket[cur_k] = {
                "id": d["id"],
                "payDate": d["pay_date"],
                "grossAmount": float(d["gross_amount"]),
                "taxAmount": float(d["tax_amount"]),
                "netAmount": float(d["net_amount"]),
                "memo": d["memo"] or "",
            }

    with DB_LOCK:
        conn_meta = get_db()
        try:
            lp_row = conn_meta.execute("SELECT value FROM app_meta WHERE key = ?", (f"last_price_refresh_{account_key}",)).fetchone()
            last_price_refresh = lp_row["value"] if lp_row else None
        finally:
            conn_meta.close()

    return {
        "accountKey": account_key,
        "pdfCutoffDate": pdf_cutoff,
        "pdfImportedAt": pdf_imported_at,
        "lastSync": last_sync,
        "lastPriceRefresh": last_price_refresh,
        "lastDivSync": last_div_sync,
        "summary": summary,
        "priorYearly": [prior_yearly_by_year[y] for y in sorted(prior_yearly_by_year.keys())],
        "items": items,
    }


# ============================================================================
# 5. HTTP 요청 핸들러
# ============================================================================

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _resolve_account_key(self, parsed_url: urllib.parse.ParseResult, body: Optional[Dict[str, Any]] = None) -> str:
        qs = urllib.parse.parse_qs(parsed_url.query)
        if qs.get("mode", [""])[0] == "demo" or self.headers.get("X-Demo-Mode") == "true":
            seed_demo_data_if_needed()
            return "DEMO"
        acc_key = (
            (body or {}).get("accountKey")
            or qs.get("accountKey", [""])[0]
            or self.headers.get("X-Account-Key")
            or "LIVE"
        )
        return str(acc_key).strip() or "LIVE"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/encrypted-config":
                if ENCRYPTED_CONFIG_PATH.is_file():
                    envelope = json.loads(ENCRYPTED_CONFIG_PATH.read_text(encoding="utf-8"))
                    self._send_json(200, {"ok": True, "data": {"exists": True, "envelope": envelope}})
                else:
                    self._send_json(200, {"ok": True, "data": {"exists": False, "envelope": None}})
                return

            if path == "/api/portfolio":
                req_key = self._resolve_account_key(parsed)
                analytics = compute_portfolio_analytics(req_key)
                analytics["isDemo"] = req_key == "DEMO"
                self._send_json(200, {"ok": True, "data": analytics})
                return

            if path == "/api/pdf/status":
                PDF_DIR.mkdir(parents=True, exist_ok=True)
                pdf_files = sorted(
                    [p for p in PDF_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
                    key=lambda p: unicodedata.normalize("NFC", p.name),
                )
                files_meta = [
                    {
                        "name": unicodedata.normalize("NFC", p.name),
                        "sizeBytes": p.stat().st_size,
                        "modifiedAt": datetime.datetime.fromtimestamp(p.stat().st_mtime, tz=KST_TZ).strftime("%Y-%m-%d %H:%M"),
                    }
                    for p in pdf_files
                ]
                with DB_LOCK:
                    conn = get_db()
                    try:
                        cutoff = get_pdf_cutoff_date(conn)
                    finally:
                        conn.close()
                self._send_json(
                    200,
                    {"ok": True, "data": {"count": len(files_meta), "files": files_meta, "pdfCutoffDate": cutoff}},
                )
                return

            if path == "/api/sync-logs":
                req_key = self._resolve_account_key(parsed)
                qs = urllib.parse.parse_qs(parsed.query)
                limit_n = min(max(int(_to_float(qs.get("limit", ["100"])[0], 100)), 1), 300)
                with DB_LOCK:
                    conn = get_db()
                    try:
                        _backfill_initial_sync_logs_if_empty(conn)
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        rows = conn.execute(
                            "SELECT id, account_key, action_type, status, created_at, summary, details_json FROM sync_logs WHERE account_key = ? ORDER BY id DESC LIMIT ?",
                            (eff_key, limit_n),
                        ).fetchall()
                        conn.commit()
                    finally:
                        conn.close()
                logs_out = []
                for r in rows:
                    try:
                        det = json.loads(r["details_json"] or "{}")
                    except Exception:
                        det = {}
                    logs_out.append(
                        {
                            "id": r["id"],
                            "accountKey": r["account_key"],
                            "actionType": r["action_type"],
                            "status": r["status"],
                            "createdAt": r["created_at"],
                            "summary": r["summary"],
                            "details": det,
                        }
                    )
                self._send_json(200, {"ok": True, "data": {"logs": logs_out}})
                return

            if path == "/api/export":
                req_key = self._resolve_account_key(parsed)
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE account_key=? ORDER BY trade_date DESC", (eff_key,)).fetchall()]
                        divs = [dict(r) for r in conn.execute("SELECT * FROM dividends WHERE account_key=? ORDER BY pay_date DESC", (eff_key,)).fetchall()]
                    finally:
                        conn.close()
                self._send_json(
                    200,
                    {"ok": True, "data": {"exportedAt": datetime.datetime.now().isoformat(), "accountKey": eff_key, "trades": trades, "dividends": divs}},
                )
                return

            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            file_path = (STATIC_DIR / rel).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.is_file():
                self._send_json(404, {"ok": False, "error": "페이지를 찾을 수 없습니다."})
                return

            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", MIME_TYPES.get(file_path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        except TossApiError as e:
            self._send_json(e.status, {"ok": False, "error": e.message, "code": e.code})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"서버 오류: {e}"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json()

            if path == "/api/encrypted-config":
                envelope = body.get("envelope")
                if not isinstance(envelope, dict) or not envelope.get("ciphertext") or not envelope.get("salt") or not envelope.get("iv"):
                    self._send_json(400, {"ok": False, "error": "유효한 암호화 볼트 형식이 아닙니다."})
                    return
                ENCRYPTED_CONFIG_PATH.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
                self._send_json(200, {"ok": True})
                return

            if path == "/api/encrypted-config/reset":
                if ENCRYPTED_CONFIG_PATH.exists():
                    ENCRYPTED_CONFIG_PATH.unlink()
                self._send_json(200, {"ok": True})
                return

            # 토스증권 공식 Open API 증분 조회 + 기준일 이후 신규 배당 자동 산출
            if path == "/api/toss/sync":
                client_id = str(body.get("clientId") or "").strip()
                client_secret = str(body.get("clientSecret") or "").strip()
                base_url = str(body.get("baseUrl") or TOSS_DEFAULT_BASE_URL).strip()
                cached_token = body.get("accessToken") or None
                token_expires_at = _to_float(body.get("tokenExpiresAt"))
                target_seq = body.get("accountSeq")
                from_date = body.get("fromDate") or None

                if not client_id or not client_secret:
                    self._send_json(400, {"ok": False, "error": "잠금 해제된 API 자격증명이 필요합니다."})
                    return

                try:
                    res = TOSS_SYNC.run_sync(
                        client_id=client_id,
                        client_secret=client_secret,
                        base_url=base_url,
                        cached_token=cached_token,
                        token_expires_at=token_expires_at,
                        target_account_seq=target_seq,
                        from_date=from_date,
                    )
                except Exception as sync_err:
                    err_msg = sync_err.message if isinstance(sync_err, TossApiError) else str(sync_err)
                    fallback_acc = f"ACC_{target_seq}" if target_seq is not None else "LIVE"
                    append_sync_log(
                        account_key=fallback_acc,
                        action_type="TOSS_SYNC",
                        status="ERROR",
                        summary=f"토스 API 증분 동기화 실패: {err_msg}",
                        details={"error": err_msg},
                    )
                    raise
                self._send_json(200, {"ok": True, "data": res})
                return

            # 실시간 현재가 전용 빠른 새로고침 (보유종목 현재가·평가금액·수익률 즉시 재계산)
            if path == "/api/prices/refresh":
                req_key = self._resolve_account_key(parsed, body)
                res = refresh_portfolio_live_prices(
                    requested_account_key=req_key,
                    client_id=str(body.get("clientId") or "").strip(),
                    client_secret=str(body.get("clientSecret") or "").strip(),
                    base_url=str(body.get("baseUrl") or TOSS_DEFAULT_BASE_URL).strip(),
                    cached_token=body.get("accessToken") or None,
                    token_expires_at=_to_float(body.get("tokenExpiresAt")),
                    target_account_seq=body.get("accountSeq"),
                )
                self._send_json(200, {"ok": True, "data": res})
                return

            # PDF 기준일 이후 도래한 신규 배당만 증분 업데이트
            if path == "/api/dividends/auto-sync":
                req_key = self._resolve_account_key(parsed, body)
                force_refresh = bool(body.get("forceRefresh", True))
                from_date = body.get("fromDate") or None
                try:
                    res = AUTO_DIV_SYNC.sync_account_dividends(
                        account_key=req_key,
                        force_refresh=force_refresh,
                        from_date_override=from_date,
                        record_log=True,
                    )
                except Exception as div_err:
                    append_sync_log(
                        account_key=req_key,
                        action_type="DIV_SYNC",
                        status="ERROR",
                        summary=f"이후 배당 갱신 오류: {div_err}",
                        details={"error": str(div_err)},
                    )
                    raise
                self._send_json(200, {"ok": True, "data": res})
                return

            # 타사증권사 이관 전 누적 배당금 '통으로 입력' (종목별 단일 또는 다건 일괄 Upsert)
            if path == "/api/dividends/prior-broker":
                req_key = self._resolve_account_key(parsed, body)
                entries = body.get("items") or []
                if not isinstance(entries, list):
                    self._send_json(400, {"ok": False, "error": "올바른 항목 목록이 아닙니다."})
                    return

                saved_cnt = 0
                deleted_cnt = 0
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        alias_map = _load_alias_map(conn)
                        for ent in entries:
                            raw_sym = str(ent.get("symbol") or "").strip().upper()
                            if not raw_sym:
                                continue
                            name = str(ent.get("name") or raw_sym).strip()
                            sym = canonicalize_symbol_with_db(conn, raw_sym, name, alias_cache=alias_map)
                            mkt, cur = _infer_market_and_currency(sym, ent.get("market", ""), ent.get("currency", ""))
                            gross = _to_float(ent.get("grossAmount"))
                            tax = _to_float(ent.get("taxAmount"))
                            net = _to_float(ent.get("netAmount"), gross - tax)
                            pay_date = str(ent.get("payDate") or "").strip() or "2022-01-01"
                            memo = str(ent.get("memo") or "").strip() or "타사증권사 이관 전 누적 배당금 (통입력)"

                            # 0 입력 시 기존 타사 배당 항목 삭제
                            conn.execute(
                                "DELETE FROM dividends WHERE account_key=? AND symbol=? AND source='PRIOR_BROKER'",
                                (eff_key, sym),
                            )
                            if gross <= 0 and net <= 0:
                                deleted_cnt += 1
                                continue

                            if gross <= 0 and net > 0:
                                gross = net + tax

                            conn.execute(
                                """
                                INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at)
                                VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                                ON CONFLICT(symbol) DO UPDATE SET
                                    name = CASE WHEN stocks.name = stocks.symbol AND excluded.name != excluded.symbol THEN excluded.name ELSE stocks.name END
                                """,
                                (sym, name, mkt, cur),
                            )
                            conn.execute(
                                """
                                INSERT INTO dividends (
                                    account_key, symbol, name, market, currency, pay_date,
                                    gross_amount, tax_amount, net_amount, source, memo
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PRIOR_BROKER', ?)
                                """,
                                (eff_key, sym, name, mkt, cur, pay_date, gross, tax, net, memo),
                            )
                            saved_cnt += 1
                        conn.commit()
                    finally:
                        conn.close()

                self._send_json(200, {"ok": True, "data": {"savedCount": saved_cnt, "deletedCount": deleted_cnt}})
                return

            # 종목 상세내역이 없는 타사증권사의 '연도별 배당금 합계(2019년~)' 일괄 통입력
            if path == "/api/dividends/prior-yearly":
                req_key = self._resolve_account_key(parsed, body)
                year_rows = body.get("years") or []
                if not isinstance(year_rows, list):
                    self._send_json(400, {"ok": False, "error": "올바른 연도별 배당 목록이 아닙니다."})
                    return

                saved_cnt = 0
                deleted_cnt = 0
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        for sym_k, (nm_k, mkt_k, cur_k) in PRIOR_YEARLY_SYMBOLS.items():
                            conn.execute(
                                """
                                INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at)
                                VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                                ON CONFLICT(symbol) DO UPDATE SET name = excluded.name
                                """,
                                (sym_k, nm_k, mkt_k, cur_k),
                            )

                        for yr_ent in year_rows:
                            yr_val = int(_to_float(yr_ent.get("year"), 0))
                            if yr_val < 1990 or yr_val > 2100:
                                continue
                            pay_dt = f"{yr_val:04d}-12-31"
                            yr_prefix = f"{yr_val:04d}-"

                            for cur_k, sym_k in (("KRW", "PRIOR_YEARLY_KR"), ("USD", "PRIOR_YEARLY_US")):
                                nm_k, mkt_k, _ = PRIOR_YEARLY_SYMBOLS[sym_k]
                                sub_obj = yr_ent.get(cur_k) if isinstance(yr_ent.get(cur_k), dict) else None
                                if sub_obj is None:
                                    continue
                                gross = _to_float(sub_obj.get("grossAmount"))
                                tax = _to_float(sub_obj.get("taxAmount"))
                                net = _to_float(sub_obj.get("netAmount"), gross - tax)
                                raw_memo = str(sub_obj.get("memo") or yr_ent.get("memo") or "").strip()
                                memo = raw_memo or f"{yr_val}년 타사증권사 연간 배당 합산 (통입력)"

                                existed = conn.execute(
                                    "SELECT COUNT(*) FROM dividends WHERE account_key=? AND symbol=? AND source='PRIOR_YEARLY' AND pay_date LIKE ?",
                                    (eff_key, sym_k, f"{yr_prefix}%"),
                                ).fetchone()[0]
                                conn.execute(
                                    "DELETE FROM dividends WHERE account_key=? AND symbol=? AND source='PRIOR_YEARLY' AND pay_date LIKE ?",
                                    (eff_key, sym_k, f"{yr_prefix}%"),
                                )
                                if gross <= 0 and net <= 0:
                                    if existed > 0:
                                        deleted_cnt += 1
                                    continue
                                if gross <= 0 and net > 0:
                                    gross = net + tax

                                conn.execute(
                                    """
                                    INSERT INTO dividends (
                                        account_key, symbol, name, market, currency, pay_date,
                                        gross_amount, tax_amount, net_amount, source, memo
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PRIOR_YEARLY', ?)
                                    """,
                                    (eff_key, sym_k, nm_k, mkt_k, cur_k, pay_dt, gross, tax, net, memo),
                                )
                                saved_cnt += 1
                        conn.commit()
                    finally:
                        conn.close()

                self._send_json(200, {"ok": True, "data": {"savedCount": saved_cnt, "deletedCount": deleted_cnt}})
                return

            # 웹 브라우저에서 거래내역서 PDF 파일 직접 업로드 + 즉시 원장 반영 (Windows·Mac 공통)
            if path == "/api/pdf/upload":
                req_key = self._resolve_account_key(parsed, body)
                raw_files = body.get("files") or []
                auto_import = bool(body.get("autoImport", True))
                if not isinstance(raw_files, list) or not raw_files:
                    self._send_json(400, {"ok": False, "error": "업로드할 거래내역서 PDF 파일을 선택해주세요."})
                    return

                PDF_DIR.mkdir(parents=True, exist_ok=True)
                saved_names: List[str] = []
                for f_obj in raw_files:
                    if not isinstance(f_obj, dict):
                        continue
                    orig_name = unicodedata.normalize("NFC", Path(str(f_obj.get("name") or "")).name.strip())
                    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", orig_name)
                    if not safe_name.lower().endswith(".pdf"):
                        safe_name += ".pdf"
                    b64_str = str(f_obj.get("contentBase64") or "").strip()
                    if "," in b64_str and b64_str.startswith("data:"):
                        b64_str = b64_str.split(",", 1)[1]
                    if not b64_str:
                        continue
                    try:
                        raw_bytes = base64.b64decode(b64_str)
                    except Exception:
                        raise RuntimeError(f"PDF 파일 디코딩에 실패했습니다: {safe_name}")
                    if not raw_bytes.startswith(b"%PDF-"):
                        raise RuntimeError(f"유효한 PDF 파일 형식이 아닙니다: {safe_name}")
                    if b"/Encrypt" in raw_bytes[:4096] or re.search(rb"/Encrypt\s+\d+\s+0\s+R", raw_bytes):
                        raise RuntimeError(
                            f"비밀번호가 걸린 암호화 PDF({safe_name})입니다. 토스증권에서 비밀번호 없는 일반 거래내역서 PDF로 발급받아 올려주세요."
                        )
                    (PDF_DIR / safe_name).write_bytes(raw_bytes)
                    saved_names.append(safe_name)

                if not saved_names:
                    self._send_json(400, {"ok": False, "error": "저장된 PDF 파일이 없습니다."})
                    return

                import_res = None
                if auto_import:
                    with DB_LOCK:
                        conn = get_db()
                        try:
                            eff_key = resolve_and_migrate_account_key(conn, req_key)
                        finally:
                            conn.close()
                        import_res = import_pdfs.import_pdf_ledger_to_db(PDF_DIR, target_account_key=eff_key)
                    append_sync_log(
                        account_key=import_res.get("accountKey", req_key),
                        action_type="PDF_IMPORT",
                        status="SUCCESS",
                        summary=(
                            f"거래내역서 PDF 업로드·반영 완료 ({len(saved_names)}개 업로드 · "
                            f"확정 기준일 ~{import_res['cutoffDate']} · 종목 {import_res['totalCanonicalStocks']}개 · "
                            f"거래 {import_res['tradesInserted']}건 · 배당 {import_res['dividendsInserted']}건)"
                        ),
                        details={"uploadedFiles": saved_names, **import_res},
                    )

                self._send_json(200, {"ok": True, "data": {"savedFiles": saved_names, "importResult": import_res}})
                return

            # pdfs/ 폴더의 거래내역서 PDF 재반영 (타사 배당·수동 입력·기준일 이후 API 체결 보존)
            if path == "/api/pdf/reimport":
                req_key = self._resolve_account_key(parsed, body)
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                    finally:
                        conn.close()
                    res = import_pdfs.import_pdf_ledger_to_db(PDF_DIR, target_account_key=eff_key)
                append_sync_log(
                    account_key=res.get("accountKey", req_key),
                    action_type="PDF_IMPORT",
                    status="SUCCESS",
                    summary=(
                        f"PDF 폴더 재반영 완료 (확정 기준일 ~{res['cutoffDate']} · "
                        f"종목 {res['totalCanonicalStocks']}개 · 거래 {res['tradesInserted']}건 · 배당 {res['dividendsInserted']}건)"
                    ),
                    details=res,
                )
                self._send_json(200, {"ok": True, "data": res})
                return

            if path == "/api/dividends":
                req_key = self._resolve_account_key(parsed, body)
                raw_sym = str(body.get("symbol") or "").strip().upper()
                name = str(body.get("name") or raw_sym).strip()
                pay_date = str(body.get("payDate") or "").strip()
                gross = _to_float(body.get("grossAmount"))
                tax = _to_float(body.get("taxAmount"))
                net = _to_float(body.get("netAmount"), gross - tax)
                src = "PRIOR_BROKER" if body.get("isPriorBroker") else "MANUAL"
                memo = str(body.get("memo") or "").strip()
                if src == "PRIOR_BROKER" and not memo:
                    memo = "타사증권사 이관 전 누적 배당금 (통입력)"

                if not raw_sym or not pay_date or (gross <= 0 and net <= 0):
                    self._send_json(400, {"ok": False, "error": "종목코드, 기준일, 배당 금액을 확인해주세요."})
                    return

                if gross <= 0 and net > 0:
                    gross = net + tax

                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        sym = canonicalize_symbol_with_db(conn, raw_sym, name)
                        mkt, cur = _infer_market_and_currency(sym, body.get("market", ""), body.get("currency", ""))
                        conn.execute(
                            """
                            INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at)
                            VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                            ON CONFLICT(symbol) DO UPDATE SET
                                name = CASE WHEN stocks.name = stocks.symbol AND excluded.name != excluded.symbol THEN excluded.name ELSE stocks.name END
                            """,
                            (sym, name, mkt, cur),
                        )
                        if src == "PRIOR_BROKER":
                            conn.execute(
                                "DELETE FROM dividends WHERE account_key=? AND symbol=? AND source='PRIOR_BROKER'",
                                (eff_key, sym),
                            )
                        conn.execute(
                            """
                            INSERT INTO dividends (
                                account_key, symbol, name, market, currency, pay_date,
                                gross_amount, tax_amount, net_amount, source, memo
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (eff_key, sym, name, mkt, cur, pay_date, gross, tax, net, src, memo),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                self._send_json(200, {"ok": True})
                return

            if path == "/api/import/batch":
                req_key = self._resolve_account_key(parsed, body)
                div_rows = body.get("dividends") or []
                inserted_divs = 0

                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        alias_map = _load_alias_map(conn)
                        for d in div_rows:
                            raw_sym = str(d.get("symbol") or "").strip().upper()
                            pay_date = str(d.get("payDate") or "").strip()
                            gross = _to_float(d.get("grossAmount"))
                            tax = _to_float(d.get("taxAmount"))
                            net = _to_float(d.get("netAmount"), gross - tax)
                            if not raw_sym or not pay_date or gross <= 0:
                                continue
                            name = str(d.get("name") or raw_sym).strip()
                            sym = canonicalize_symbol_with_db(conn, raw_sym, name, alias_cache=alias_map)
                            mkt, cur = _infer_market_and_currency(sym, d.get("market", ""), d.get("currency", ""))
                            conn.execute(
                                "INSERT INTO stocks (symbol, name, market, currency, last_price, updated_at) VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP) ON CONFLICT(symbol) DO NOTHING",
                                (sym, name, mkt, cur),
                            )
                            conn.execute(
                                "INSERT INTO dividends (account_key, symbol, name, market, currency, pay_date, gross_amount, tax_amount, net_amount, source, memo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'IMPORT', ?)",
                                (eff_key, sym, name, mkt, cur, pay_date, gross, tax, net, str(d.get("memo") or "일괄 등록")),
                            )
                            inserted_divs += 1
                        conn.commit()
                    finally:
                        conn.close()

                self._send_json(200, {"ok": True, "data": {"insertedDividends": inserted_divs}})
                return

            self._send_json(404, {"ok": False, "error": "존재하지 않는 API 경로입니다."})

        except TossApiError as e:
            self._send_json(e.status, {"ok": False, "error": e.message, "code": e.code})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"요청 처리 오류: {e}"})

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/dividends":
            self._send_json(404, {"ok": False, "error": "존재하지 않는 경로입니다."})
            return
        try:
            body = self._read_json()
            req_key = self._resolve_account_key(parsed, body)
            div_id = int(body.get("id") or 0)
            gross = _to_float(body.get("grossAmount"))
            tax = _to_float(body.get("taxAmount"))
            net = _to_float(body.get("netAmount"), gross - tax)
            memo = str(body.get("memo") or "").strip()
            if div_id <= 0:
                self._send_json(400, {"ok": False, "error": "수정할 배당 ID를 확인해주세요."})
                return
            with DB_LOCK:
                conn = get_db()
                try:
                    eff_key = resolve_and_migrate_account_key(conn, req_key)
                    cur_row = conn.execute(
                        "SELECT source, currency, fx_rate FROM dividends WHERE id=? AND account_key=?",
                        (div_id, eff_key),
                    ).fetchone()
                    keep_src = cur_row["source"] if (cur_row and cur_row["source"] in ("PRIOR_BROKER", "PRIOR_YEARLY", "PDF", "EXT_DIV")) else "MANUAL"
                    row_cur = str(cur_row["currency"] or "KRW") if cur_row else "KRW"
                    row_fx = float(cur_row["fx_rate"] or 0.0) if cur_row else 0.0
                    if row_cur == "KRW":
                        kg, kt, kn = float(round(gross)), float(round(tax)), float(round(net))
                    elif row_fx > 0:
                        kg, kt, kn = float(round(gross * row_fx)), float(round(tax * row_fx)), float(round(net * row_fx))
                    else:
                        kg, kt, kn = None, None, None
                    conn.execute(
                        """
                        UPDATE dividends
                        SET gross_amount=?, tax_amount=?, net_amount=?,
                            krw_gross=?, krw_tax=?, krw_net=?, source=?,
                            memo = CASE WHEN ? != '' THEN ? ELSE memo END
                        WHERE id=? AND account_key=?
                        """,
                        (gross, tax, net, kg, kt, kn, keep_src, memo, memo, div_id, eff_key),
                    )
                    conn.commit()
                finally:
                    conn.close()
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            req_key = self._resolve_account_key(parsed)
            item_id = int(qs.get("id", ["0"])[0])
            if parsed.path == "/api/pdf/file":
                raw_nm = unicodedata.normalize("NFC", Path(str(qs.get("name", [""])[0])).name.strip())
                if not raw_nm or not raw_nm.lower().endswith(".pdf"):
                    self._send_json(400, {"ok": False, "error": "삭제할 PDF 파일명을 확인해주세요."})
                    return
                target_p = (PDF_DIR / raw_nm).resolve()
                if str(target_p).startswith(str(PDF_DIR.resolve())) and target_p.is_file():
                    target_p.unlink()
                self._send_json(200, {"ok": True})
                return

            if parsed.path == "/api/sync-logs":
                clear_all = qs.get("all", [""])[0] == "1"
                with DB_LOCK:
                    conn = get_db()
                    try:
                        eff_key = resolve_and_migrate_account_key(conn, req_key)
                        if clear_all:
                            conn.execute("DELETE FROM sync_logs WHERE account_key = ?", (eff_key,))
                        else:
                            conn.execute("DELETE FROM sync_logs WHERE id = ? AND account_key = ?", (item_id, eff_key))
                        conn.commit()
                    finally:
                        conn.close()
                self._send_json(200, {"ok": True})
                return

            if parsed.path != "/api/dividends":
                self._send_json(404, {"ok": False, "error": "존재하지 않는 경로입니다."})
                return
            with DB_LOCK:
                conn = get_db()
                try:
                    eff_key = resolve_and_migrate_account_key(conn, req_key)
                    conn.execute("DELETE FROM dividends WHERE id = ? AND account_key = ?", (item_id, eff_key))
                    conn.commit()
                finally:
                    conn.close()
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def _is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main() -> None:
    open_browser = "--open-browser" in sys.argv
    url = f"http://{HOST}:{PORT}"

    if _is_port_in_use(HOST, PORT):
        print(f"이미 대시보드 서버가 실행 중입니다: {url}")
        if open_browser:
            webbrowser.open(url)
        return

    init_db()
    server = ThreadedHTTPServer((HOST, PORT), DashboardRequestHandler)
    print(f"토스증권 배당·매매 통합 수익 대시보드 실행 중: {url}")
    print("브라우저에서 대시보드를 열람하신 뒤, 종료하려면 이 창을 닫거나 Ctrl+C를 누르세요.")
    sys.stdout.flush()

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
