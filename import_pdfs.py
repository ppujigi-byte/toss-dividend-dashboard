#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토스증권 거래내역서 PDF 전수 파서 및 로컬 SQLite(portfolio.db) 원장 구축 스크립트
- pdfs/ 폴더의 모든 거래내역서 PDF를 Windows·macOS 호환 파이썬 파서로 정밀 추출
- 매수·매도, 타사대체입고(입고일 단가 기준 매수 반영), 무상/이벤트 입고, 주식분할·병합·종목교환·단주매각 원가 보존 처리
- 외화증권배당금입금·외화배당취소·외화세금환급·원화배당세출금(세금재정산 반영) 및 국내 배당·분배금(종목코드 미표기분 자동 역추적 포함) 반영
- ISIN ↔ 미국 티커 / 국내 6자리 단축코드 표준화 매핑 저장
"""

import datetime
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = BASE_DIR / "pdfs"
DB_PATH = DATA_DIR / "portfolio.db"
SWIFT_SRC = BASE_DIR / "pdf_extractor.swift"
SWIFT_BIN = DATA_DIR / "pdf_extractor_bin"

# 주요 해외 주식·ETF ISIN -> 표준 티커 매핑 (병합 전후 ISIN 통합 지원)
ISIN_TO_TICKER: Dict[str, str] = {
    "BMG491BT1088": "IVZ",
    "CA7063271034": "PBA",
    "US00287Y1091": "ABBV",
    "US0079031078": "AMD",
    "US02079K3059": "GOOGL",
    "US02209S1033": "MO",
    "US0231351067": "AMZN",
    "US0321085245": "QDVO",
    "US0378331005": "AAPL",
    "US0636795595": "BULZ",
    "US09258G1040": "BST",
    "US11135F1012": "AVGO",
    "US1912161007": "KO",
    "US22266T1097": "CPNG",
    "US25459W4583": "SOXL",
    "US25461A3876": "KORU",
    "US26884U1097": "EPR",
    "US26922A4206": "QTUM",
    "US26923N5389": "AIPI",
    "US26923N7443": "FEPI",
    "US30233Q1085": "XOM",
    "US30303M1027": "META",
    "US3765461070": "GAIN",
    "US37954Y4750": "XYLD",
    "US37954Y4834": "QYLD",
    "US38149W6223": "GPIX",
    "US38149W6306": "GPIQ",
    "US4592001014": "IBM",
    "US46090E1038": "QQQ",
    "US4642872000": "IVV",
    "US4642874329": "TLT",
    "US4642887602": "ITA",
    "US46436E7186": "SGOV",
    "US46625H1005": "JPM",
    "US46641Q3323": "JEPI",
    "US46654Q2030": "JEPQ",
    "US4781601046": "JNJ",
    "US5398301094": "LMT",
    "US56035L1044": "MAIN",
    "US5719032022": "MAR",
    "US5949181045": "MSFT",
    "US64828T2015": "RITM",
    "US6541061031": "NKE",
    "US67066G1040": "NVDA",
    "US6745991058": "OXY",
    "US68389X1054": "ORCL",
    "US74347G4405": "BITO",
    "US74347R1077": "SSO",
    "US74347R2067": "QLD",
    "US74347X8314": "TQQQ",
    "US74347X8645": "UPRO",
    "US77926X2053": "XDTE",
    "US77926X3044": "QDTE",
    "US77926X5023": "YBTC",
    "US77926X5858": "TOPW",
    "US77926X7599": "GOOW",
    "US77926X8258": "RDTE",
    "US77926X8415": "YETH",
    "US7802593050": "SHEL",
    "US78433H6751": "QQQI",
    "US78464A4094": "SPYG",
    "US8085247976": "SCHD",
    "US8288061091": "SPG",
    "US8552441094": "SBUX",
    "US88160R1014": "TSLA",
    "US88634T4774": "AMDY",
    "US88634T7744": "NVDY",
    "US88634T8247": "CONY",
    "US88636J1549": "QQQY",
    "US88636J4444": "TSLY",
    "US88636J6423": "YMAG",
    "US88636J6597": "YMAX",
    "US88636R4790": "AIPO",
    "US88636X7241": "AMDY",
    "US88636X7324": "MSTY",
    "US88636X8561": "CONY",
    "US88636X8801": "TSLY",
    "US92189F6768": "SMH",
    "US92864M3016": "BITX",
    "US94845U1051": "WBTN",
    "USN070592100": "ASML",
}

EXTRA_TICKER_ALIASES: Dict[str, str] = {
    "WPAY": "TOPW",  # Roundhill WeeklyPay Universe -> Top WeeklyPay 티커 변경 대응
}


def fnum(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(str(val).replace(",", "").strip())
    except ValueError:
        return 0.0


def clean_stock_name(raw_name: str) -> str:
    s = (raw_name or "").strip()
    if "," in s:
        left, right = s.split(",", 1)
        if "[" in left or "]" in left or "대체" in left or "NGS" in left:
            s = right.strip()
    # 줄바꿈으로 붙은 공백 보정
    s = re.sub(r"전\s*략", "전략", s)
    s = re.sub(r"인\s*컴", "인컴", s)
    s = re.sub(r"인컴ETF$", "인컴 ETF", s)
    s = re.sub(r"전략ETF$", "전략 ETF", s)
    return s.strip()


def canonicalize_code(raw_code: str, section_hint: str = "") -> Tuple[str, str, str]:
    """(canonical_symbol, market, currency) 반환"""
    c = (raw_code or "").strip().upper()
    if c in ISIN_TO_TICKER:
        return ISIN_TO_TICKER[c], "US", "USD"
    if c in EXTRA_TICKER_ALIASES:
        return EXTRA_TICKER_ALIASES[c], "US", "USD"
    if len(c) == 7 and c.startswith("A") and re.fullmatch(r"[0-9A-Z]{6}", c[1:]):
        return c[1:], "KR", "KRW"
    if re.fullmatch(r"\d{6}", c) or re.fullmatch(r"\d{2}[0-9A-Z]{4}", c):
        return c, "KR", "KRW"
    if c.startswith(("US", "CA", "BM")):
        return c, "US", "USD"
    if section_hint == "KRW":
        return c, "KR", "KRW"
    return c, "US", "USD"


def extract_rows_from_pdfs(pdf_dir: Path = PDF_DIR) -> List[Dict[str, Any]]:
    import pdf_extractor_py

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    raw_rows = pdf_extractor_py.extract_rows_from_pdf_dir_python(pdf_dir)

    # 서로 다른 발급번호(issueNo) PDF 간 기간 중복 업로드 시 자동 중복 제거
    # 동일 발급번호 내에서는 모든 행을 순서대로 보존
    seen_signatures = set()
    issue_occurrence: Dict[Tuple[str, ...], int] = defaultdict(int)
    deduped: List[Dict[str, Any]] = []

    for r in raw_rows:
        issue_no = r.get("issueNo") or r.get("file") or ""
        base_sig = (
            r["section"],
            r["date"],
            r["txType"],
            r["stockCode"],
            r["qty"],
            r["grossUsd"] if r["section"] == "USD" else r["grossKrw"],
            r["settleUsd"] if r["section"] == "USD" else r["settleKrw"],
            r["holdingQty"],
            r["balUsd"] if r["section"] == "USD" else r["balKrw"],
        )
        occ_key = (issue_no,) + base_sig
        occ_idx = issue_occurrence[occ_key]
        issue_occurrence[occ_key] += 1

        global_sig = base_sig + (occ_idx,)
        if global_sig in seen_signatures:
            continue
        seen_signatures.add(global_sig)
        deduped.append(r)

    return deduped


def resolve_missing_kr_dividend_codes(rows: List[Dict[str, Any]], kr_names: Dict[str, str]) -> None:
    """
    2025년 12월 중순 이후 토스증권 거래내역서 PDF에서 종목명이 빈칸으로 출력되는
    국내 주식 배당금입금·ETF 분배금 행을 (오름차순 종목코드 정렬 규칙 + 기준일 보유수량 약수 매칭)으로 정밀 복원
    """
    in_txs = {"구매", "1주이벤트입고"}
    kr_trades_by_code: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for r in rows:
        if r["section"] == "KRW" and r["stockCode"].startswith("A"):
            tx_t = str(r["txType"] or "")
            if (
                tx_t in in_txs
                or tx_t.startswith("타사대체입고")
                or (any(k in tx_t for k in ("유상", "무상", "청약", "공모", "주식배당")) and "출고" not in tx_t and "단주" not in tx_t)
            ):
                kr_trades_by_code[r["stockCode"]].append((r["date"], fnum(r["qty"])))
            elif tx_t == "판매":
                kr_trades_by_code[r["stockCode"]].append((r["date"], -fnum(r["qty"])))

    def qty_as_of(code: str, dt_dot: str) -> int:
        return int(round(sum(q for d, q in kr_trades_by_code[code] if d <= dt_dot)))

    # 1) 월중배당 ETF (매월 15일 기준 -> 17~20일경 지급, 오름차순 코드)
    mid_month_etfs = ["A441640", "A490600", "A498400"]
    # 2) 월말배당/연배당 ETF (말일 기준 -> 익월 초 2~6일경 지급, 오름차순 코드)
    month_end_etfs = [
        "A0036R0",
        "A0199C0",
        "A395160",
        "A441680",
        "A472150",
        "A475720",
        "A486290",
        "A489030",
        "A490590",
        "A490600",
        "A491620",
        "A498410",
    ]
    # 전액 비과세(국내 주식/코스피200 옵션 프리미엄 100%) ETF
    always_tax_free: set = set()

    # 날짜+구분 그룹별로 일괄 매칭
    grouped_indices: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        if r["section"] == "KRW" and r["txType"] in ("배당금입금", "분배금") and not r["stockCode"]:
            grouped_indices[(r["date"], r["txType"])].append(idx)

    for (dt_dot, tx_type), idx_list in sorted(grouped_indices.items()):
        y, m, d = map(int, dt_dot.split("."))
        pay_dt = datetime.date(y, m, d)

        if tx_type == "배당금입금":
            # 개별 국내 주식 배당금 (NAVER A035420, KB금융 A105560, 신한지주 A055550, 삼성전자 A005930 등)
            div_stocks = ["A035420", "A105560", "A055550", "A005930", "A086790", "A003410", "A012330", "A049720"]
            for idx in idx_list:
                gross = int(round(fnum(rows[idx]["grossKrw"])))
                matched_code = None
                for c in div_stocks:
                    qs = {
                        qty_as_of(c, (pay_dt - datetime.timedelta(days=delta)).strftime("%Y.%m.%d"))
                        for delta in range(2, 125)
                    }
                    qs = {q for q in qs if q > 0}
                    for q in sorted(qs, reverse=True):
                        if gross % q == 0 and 100 <= (gross // q) <= 5000:
                            matched_code = c
                            break
                    if matched_code:
                        break
                if matched_code:
                    rows[idx]["stockCode"] = matched_code
                    rows[idx]["stockName"] = kr_names.get(matched_code, matched_code)
            continue

        # ETF 분배금: 지급일이 13일~25일 사이면 월중배당, 1~12일이면 월말배당
        pool = mid_month_etfs if 13 <= d <= 25 else month_end_etfs

        # 각 후보 종목별 가능한 (보유수량, 주당분배금) 계산 (기준일은 지급일 1~12일 전)
        cand_per_item: List[List[Tuple[str, int, int]]] = []
        for idx in idx_list:
            gross = int(round(fnum(rows[idx]["grossKrw"])))
            tax = int(round(fnum(rows[idx]["taxKrw"])))
            item_cands: List[Tuple[str, int, int]] = []
            for c in pool:
                if tax > 0 and c in always_tax_free:
                    continue
                # A490600은 2026년 4월 중순부터 월중배당으로 전환됨 (2026.05 이후 월초 배당 제외)
                if c == "A490600" and d < 13 and dt_dot >= "2026.05.01":
                    continue
                # A0036R0, A395160은 연 1~2회 소액 분배금(주당 15~120원 수준)
                qs = {
                    qty_as_of(c, (pay_dt - datetime.timedelta(days=delta)).strftime("%Y.%m.%d"))
                    for delta in range(1, 13)
                }
                qs = {q for q in qs if q > 0}
                best_match = None
                for q in sorted(qs, reverse=True):
                    if gross % q == 0:
                        dps = gross // q
                        if c in ("A0036R0", "A395160"):
                            if 10 <= dps <= 150:
                                best_match = (c, q, dps)
                                break
                        else:
                            if 50 <= dps <= 650:
                                best_match = (c, q, dps)
                                break
                if best_match:
                    item_cands.append(best_match)
            cand_per_item.append(item_cands)

        # 오름차순 종목코드 제약을 만족하는 최적 시퀀스 탐색 (Backtracking)
        best_seq: Optional[List[Tuple[str, int, int]]] = None

        def dfs(pos: int, prev_code: str, acc: List[Tuple[str, int, int]]) -> bool:
            nonlocal best_seq
            if pos == len(idx_list):
                best_seq = list(acc)
                return True
            for cand in cand_per_item[pos]:
                if cand[0] > prev_code:
                    acc.append(cand)
                    if dfs(pos + 1, cand[0], acc):
                        return True
                    acc.pop()
            return False

        if dfs(0, "", []):
            for idx, (c, q, dps) in zip(idx_list, best_seq):
                rows[idx]["stockCode"] = c
                rows[idx]["stockName"] = kr_names.get(c, c)
                rows[idx]["_matched_shares"] = q
                rows[idx]["_matched_dps"] = dps
        else:
            # 혹시라도 엄격 조건에서 못 찾은 개별 항목이 있으면 단독 후보 매칭
            for idx, cands in zip(idx_list, cand_per_item):
                if cands:
                    c, q, dps = cands[0]
                    rows[idx]["stockCode"] = c
                    rows[idx]["stockName"] = kr_names.get(c, c)
                    rows[idx]["_matched_shares"] = q
                    rows[idx]["_matched_dps"] = dps


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stocks (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT 'KR',
            currency TEXT NOT NULL DEFAULT 'KRW',
            isin TEXT DEFAULT '',
            last_price REAL NOT NULL DEFAULT 0,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS symbol_aliases (
            alias TEXT PRIMARY KEY,
            canonical_symbol TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS holdings_snapshot (
            account_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            currency TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            avg_purchase_price REAL NOT NULL DEFAULT 0,
            last_price REAL NOT NULL DEFAULT 0,
            purchase_amount REAL NOT NULL DEFAULT 0,
            eval_amount REAL NOT NULL DEFAULT 0,
            eval_amount_after_cost REAL NOT NULL DEFAULT 0,
            pnl_amount REAL NOT NULL DEFAULT 0,
            pnl_amount_after_cost REAL NOT NULL DEFAULT 0,
            pnl_rate REAL NOT NULL DEFAULT 0,
            pnl_rate_after_cost REAL NOT NULL DEFAULT 0,
            commission REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            synced_at TEXT,
            PRIMARY KEY (account_key, symbol)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL DEFAULT 'LIVE',
            external_order_id TEXT,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT 'KR',
            currency TEXT NOT NULL DEFAULT 'KRW',
            side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'SPLIT_IN', 'SPLIT_OUT')),
            trade_date TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            gross_amount REAL NOT NULL,
            commission REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'MANUAL',
            memo TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_key, external_order_id)
        );

        CREATE TABLE IF NOT EXISTS dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL DEFAULT 'LIVE',
            symbol TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT 'KR',
            currency TEXT NOT NULL DEFAULT 'KRW',
            pay_date TEXT NOT NULL,
            gross_amount REAL NOT NULL,
            tax_amount REAL NOT NULL DEFAULT 0,
            net_amount REAL NOT NULL,
            fx_rate REAL DEFAULT NULL,
            krw_gross REAL DEFAULT NULL,
            krw_tax REAL DEFAULT NULL,
            krw_net REAL DEFAULT NULL,
            per_share REAL DEFAULT NULL,
            shares_held REAL DEFAULT NULL,
            source TEXT NOT NULL DEFAULT 'MANUAL',
            memo TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS dividend_events_cache (
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            events_json TEXT NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            fetched_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, market)
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL DEFAULT 'LIVE',
            action_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'SUCCESS',
            created_at TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_sync_logs_acc_id ON sync_logs(account_key, id DESC);
        """
    )
    # 기존 테이블 컬럼 자동 마이그레이션
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()}
    if "isin" not in cols:
        conn.execute("ALTER TABLE stocks ADD COLUMN isin TEXT DEFAULT ''")

    div_cols = {r[1] for r in conn.execute("PRAGMA table_info(dividends)").fetchall()}
    for col_nm in ("fx_rate", "krw_gross", "krw_tax", "krw_net"):
        if col_nm not in div_cols:
            conn.execute(f"ALTER TABLE dividends ADD COLUMN {col_nm} REAL DEFAULT NULL")

    tbl_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
    if tbl_sql_row and "SPLIT_IN" not in (tbl_sql_row[0] or ""):
        conn.executescript(
            """
            ALTER TABLE trades RENAME TO trades_old;
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_key TEXT NOT NULL DEFAULT 'LIVE',
                external_order_id TEXT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'KR',
                currency TEXT NOT NULL DEFAULT 'KRW',
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'SPLIT_IN', 'SPLIT_OUT')),
                trade_date TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                gross_amount REAL NOT NULL,
                commission REAL NOT NULL DEFAULT 0,
                tax REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'MANUAL',
                memo TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_key, external_order_id)
            );
            INSERT INTO trades (id, account_key, external_order_id, symbol, name, market, currency, side, trade_date, quantity, price, gross_amount, commission, tax, source, memo, created_at)
            SELECT id, account_key, external_order_id, symbol, name, market, currency, side, trade_date, quantity, price, gross_amount, commission, tax, source, memo, created_at FROM trades_old;
            DROP TABLE trades_old;
            """
        )


def import_pdf_ledger_to_db(pdf_dir: Path = PDF_DIR, target_account_key: Optional[str] = None) -> Dict[str, Any]:
    pdf_files = [p for p in pdf_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"] if pdf_dir.exists() else []
    if not pdf_files:
        raise RuntimeError("등록된 거래내역서 PDF 파일이 없습니다. 먼저 토스증권에서 발급받은 거래내역서 PDF 파일을 업로드해주세요.")
    rows = extract_rows_from_pdfs(pdf_dir)
    if not rows:
        raise RuntimeError("PDF에서 추출된 거래내역 행이 없습니다. 토스증권 공식 '거래내역서' PDF 파일인지 확인해주세요.")

    # 종목명 사전 구축
    kr_names_by_acode: Dict[str, str] = {}
    stock_meta: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = dict(EXTRA_TICKER_ALIASES)

    for isin_code, ticker in ISIN_TO_TICKER.items():
        aliases[isin_code] = ticker

    for r in rows:
        raw_code = r["stockCode"]
        if not raw_code:
            continue
        cleaned_nm = clean_stock_name(r["stockName"])
        if raw_code.startswith("A"):
            kr_names_by_acode[raw_code] = cleaned_nm
        sym, mkt, cur = canonicalize_code(raw_code, r["section"])
        aliases[raw_code] = sym
        if mkt == "KR":
            aliases[f"A{sym}"] = sym
        prev = stock_meta.get(sym)
        isin_val = raw_code if raw_code.startswith(("US", "CA", "BM")) else (prev["isin"] if prev else "")
        price_val = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
        stock_meta[sym] = {
            "symbol": sym,
            "name": cleaned_nm or (prev["name"] if prev else sym),
            "market": mkt,
            "currency": cur,
            "isin": isin_val,
            "last_price": price_val if price_val > 0 else (prev["last_price"] if prev else 0.0),
        }

    # 종목코드 누락 국내 배당·분배금 정밀 복원
    resolve_missing_kr_dividend_codes(rows, kr_names_by_acode)

    # 병합 단주 매각(외화주식병합단주대금입금) 매칭 전처리:
    # 단주 병합출고와 단주 매각대금을 병합 직전 일자의 단주 SELL로 결합해 병합 전후 매입원가를 보존
    fractional_merge_cashouts: List[Tuple[int, str, str, float]] = []
    for idx, r in enumerate(rows):
        if r["txType"] == "외화주식병합단주대금입금" and r["stockCode"]:
            sym, _, _ = canonicalize_code(r["stockCode"], r["section"])
            fractional_merge_cashouts.append((idx, sym, r["date"], fnum(r["settleUsd"])))

    skip_row_indices = set()
    synthetic_pre_split_sells: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

    for cash_idx, sym, cash_dt_dot, cash_usd in fractional_merge_cashouts:
        # 직전 10일 이내 동일 종목의 소수점(또는 짝 없는) 주식병합출고 탐색
        c_dt = datetime.date(*map(int, cash_dt_dot.split(".")))
        matched_out_idx = None
        first_split_dt_dot = None
        for j in range(cash_idx - 1, -1, -1):
            rj = rows[j]
            if not rj["stockCode"]:
                continue
            sj, _, _ = canonicalize_code(rj["stockCode"], rj["section"])
            if sj != sym:
                continue
            j_dt = datetime.date(*map(int, rj["date"].split(".")))
            if (c_dt - j_dt).days > 10:
                break
            if rj["txType"] in ("주식병합출고", "주식병합입고"):
                first_split_dt_dot = rj["date"]
            if rj["txType"] == "주식병합출고" and matched_out_idx is None:
                q_val = fnum(rj["qty"])
                if abs(q_val - round(q_val)) > 1e-6 or fnum(rj["holdingQty"]) == 0:
                    matched_out_idx = j
        if matched_out_idx is not None:
            skip_row_indices.add(matched_out_idx)
            skip_row_indices.add(cash_idx)
            out_row = rows[matched_out_idx]
            q_frac = fnum(out_row["qty"])
            anchor_dt = first_split_dt_dot or out_row["date"]
            synthetic_pre_split_sells[(sym, anchor_dt)].append(
                {
                    "symbol": sym,
                    "trade_date": anchor_dt.replace(".", "-"),
                    "quantity": q_frac,
                    "price": round(cash_usd / q_frac, 6) if q_frac > 0 else 0.0,
                    "gross_amount": cash_usd,
                    "commission": 0.0,
                    "tax": 0.0,
                    "source": "PDF",
                    "memo": f"주식병합 단주 매각대금 입금 ({cash_dt_dot.replace('.', '-')})",
                }
            )

    event_in_txs = {
        "1주이벤트입고",
        "미션이벤트주식입고",
        "소수점이벤트입고",
        "주식퀴즈이벤트입고",
        "출석체크이벤트입고",
        "토스뱅크주식캐시백입고",
        "해외주식이벤트입고",
    }
    split_in_txs = {
        "주식분할입고",
        "주식병합입고",
        "종목교환입고",
        "종목코드변경입고",
        "합병입고",
        "분할합병입고",
        "감자입고",
        "액면분할입고",
        "액면병합입고",
    }
    split_out_txs = {
        "주식분할출고",
        "주식병합출고",
        "종목교환출고",
        "종목코드변경출고",
        "합병출고",
        "분할합병출고",
        "감자출고",
        "액면분할출고",
        "액면병합출고",
    }
    paid_rights_keywords = ("유상", "청약", "공모", "신주인수권행사")
    bonus_rights_keywords = ("무상", "주식배당")

    trade_records: List[Tuple[Any, ...]] = []
    dividend_records: List[Tuple[Any, ...]] = []

    # 세금재정산(외화세금환급 + 원화 배당세출금) 정밀 반영용 버킷
    usd_refund_by_date_sym: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    usd_refund_krw_by_date_sym: Dict[Tuple[str, str], float] = defaultdict(float)
    usd_refund_krw_by_date: Dict[str, float] = defaultdict(float)
    usd_refund_usd_by_date: Dict[str, float] = defaultdict(float)
    krw_divtax_by_date_sym: Dict[Tuple[str, str], float] = defaultdict(float)
    krw_divtax_unassigned_by_date: Dict[str, float] = defaultdict(float)

    emitted_pre_split_keys = set()
    max_trade_date_iso = ""
    last_krw_balance = 0.0
    last_usd_balance = 0.0

    for idx, r in enumerate(rows):
        dt_dot = r["date"]
        dt_iso = dt_dot.replace(".", "-")
        if dt_iso > max_trade_date_iso:
            max_trade_date_iso = dt_iso

        sec = r["section"]
        tx = r["txType"]
        if sec == "KRW" and r["balKrw"] != "":
            # 대차거래 등 잔액이 0으로만 찍히는 비현금성 행 제외
            if fnum(r["settleKrw"]) > 0 or fnum(r["balKrw"]) > 0:
                last_krw_balance = fnum(r["balKrw"])
        elif sec == "USD" and r["balUsd"] != "":
            if fnum(r["settleUsd"]) > 0 or fnum(r["balUsd"]) > 0:
                last_usd_balance = fnum(r["balUsd"])

        if idx in skip_row_indices:
            continue

        raw_code = r["stockCode"]
        sym, mkt, cur = canonicalize_code(raw_code, sec) if raw_code else ("", "KR" if sec == "KRW" else "US", sec)
        meta = stock_meta.get(sym, {})
        name = meta.get("name") or clean_stock_name(r["stockName"]) or sym

        # 병합 직전 단주 매각 합성 행 먼저 삽입
        pre_key = (sym, dt_dot)
        if tx in split_out_txs and pre_key in synthetic_pre_split_sells and pre_key not in emitted_pre_split_keys:
            emitted_pre_split_keys.add(pre_key)
            for syn in synthetic_pre_split_sells[pre_key]:
                ext_id = f"PDF_MERGE_FRAC_{sym}_{syn['trade_date']}_{len(trade_records)}"
                trade_records.append(
                    (
                        ext_id,
                        sym,
                        name,
                        mkt,
                        cur,
                        "SELL",
                        syn["trade_date"],
                        syn["quantity"],
                        syn["price"],
                        syn["gross_amount"],
                        0.0,
                        0.0,
                        "PDF",
                        syn["memo"],
                    )
                )

        qty = fnum(r["qty"])
        ext_id = f"PDF_{idx}"

        # 1) 매수 / 매도
        if tx in ("구매", "판매") and sym and qty > 0:
            side = "BUY" if tx == "구매" else "SELL"
            if cur == "USD":
                price = fnum(r["priceUsd"])
                gross = fnum(r["grossUsd"])
                comm = fnum(r["commUsd"])
                tax = fnum(r["taxUsd"])
            else:
                price = fnum(r["priceKrw"])
                gross = fnum(r["grossKrw"])
                comm = fnum(r["commKrw"])
                tax = fnum(r["tradeTaxKrw"]) + fnum(r["taxKrw"])

            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price

            memo = "거래내역서 매수" if side == "BUY" else "거래내역서 매도"
            trade_records.append((ext_id, sym, name, mkt, cur, side, dt_iso, qty, price, gross, comm, tax, "PDF", memo))

        # 2) 타사대체입고 -> 입고일 단가 기준 매수 처리
        elif tx.startswith("타사대체입고") and sym and qty > 0:
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            gross = round(qty * price, 4 if cur == "USD" else 0)
            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price
            m_brk = re.search(r"\(([^)]+)\)", tx)
            broker_label = m_brk.group(1).strip() if m_brk else "타사증권사"
            memo = f"{broker_label} 대체입고 (입고일 매수 반영)"
            trade_records.append((ext_id, sym, name, mkt, cur, "BUY", dt_iso, qty, price, gross, 0.0, 0.0, "PDF_TRANSFER", memo))

        # 3) 이벤트 무상 입고 -> 원금 0원 매수 입고 처리
        elif tx in event_in_txs and sym and qty > 0:
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price
            memo = f"{tx} (무상입고)"
            trade_records.append((ext_id, sym, name, mkt, cur, "BUY", dt_iso, qty, price, 0.0, 0.0, 0.0, "PDF_EVENT", memo))

        # 3-B) 유상증자·유상청약·공모주 신주 입고 -> 청약 납입원금 기준 매수 처리
        elif (
            sym
            and qty > 0
            and any(k in tx for k in paid_rights_keywords)
            and "출고" not in tx
            and "단주" not in tx
            and "배당" not in tx
        ):
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            raw_gross = fnum(r["grossUsd"] if cur == "USD" else r["grossKrw"])
            gross = raw_gross if raw_gross > 0 else round(qty * price, 4 if cur == "USD" else 0)
            if price <= 0 and gross > 0:
                price = round(gross / qty, 4 if cur == "USD" else 0)
            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price
            memo = f"{tx} (유상·청약 신주입고)"
            trade_records.append((ext_id, sym, name, mkt, cur, "BUY", dt_iso, qty, price, gross, 0.0, 0.0, "PDF_CORP", memo))

        # 3-C) 무상증자·주식배당 신주 입고 -> 원금 0원 매수 처리 (이동평균 평단가 자동 희석)
        elif (
            sym
            and qty > 0
            and any(k in tx for k in bonus_rights_keywords)
            and "출고" not in tx
            and "단주" not in tx
        ):
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price
            memo = f"{tx} (무상·주식배당 입고)"
            trade_records.append((ext_id, sym, name, mkt, cur, "BUY", dt_iso, qty, price, 0.0, 0.0, 0.0, "PDF_CORP", memo))

        # 4) 주식분할·병합·합병·감자·종목교환·코드변경 입고/출고 (원가 총액 보존)
        elif (
            tx in split_out_txs
            or (tx.endswith("출고") and any(k in tx for k in ("분할", "병합", "합병", "교환", "코드변경", "감자")))
        ) and sym and qty > 0:
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            comm = fnum(r["commUsd"] if cur == "USD" else r["commKrw"])
            trade_records.append((ext_id, sym, name, mkt, cur, "SPLIT_OUT", dt_iso, qty, price, 0.0, comm, 0.0, "PDF_CORP", tx))

        elif (
            tx in split_in_txs
            or (tx.endswith("입고") and any(k in tx for k in ("분할", "병합", "합병", "교환", "코드변경", "감자")))
        ) and sym and qty > 0:
            price = fnum(r["priceUsd"] if cur == "USD" else r["priceKrw"])
            comm = fnum(r["commUsd"] if cur == "USD" else r["commKrw"])
            if price > 0 and sym in stock_meta:
                stock_meta[sym]["last_price"] = price
            trade_records.append((ext_id, sym, name, mkt, cur, "SPLIT_IN", dt_iso, qty, price, 0.0, comm, 0.0, "PDF_CORP", tx))

        # 5) 국내 원화 배당금입금 · 분배금
        elif sec == "KRW" and tx in ("배당금입금", "분배금") and sym:
            gross = fnum(r["grossKrw"])
            net = fnum(r["settleKrw"])
            tax = fnum(r["taxKrw"])
            if tax == 0 and gross > net:
                tax = gross - net
            sh_held = r.get("_matched_shares") or (qty if qty > 0 else None)
            dps = r.get("_matched_dps") or None
            memo = f"거래내역서 {tx}"
            if sh_held and dps:
                memo = f"거래내역서 {tx} ({int(sh_held):,}주 × {int(dps):,}원)"
            dividend_records.append((sym, name, "KR", "KRW", dt_iso, gross, tax, net, 1.0, gross, tax, net, dps, sh_held, "PDF", memo))

        # 6) 미국 외화증권배당금입금 · 외화배당단주대금입금 · 외화배당금취소출금 (토스 입금시점 실제 원화 정산액 + 적용환율 보존)
        elif sec == "USD" and tx == "외화증권배당금입금" and sym:
            net = fnum(r["settleUsd"])
            tax = fnum(r["taxUsd"])
            gross = round(net + tax, 2)
            fx = fnum(r["fxRate"])
            raw_krw_net = fnum(r["settleKrw"])
            raw_krw_tax = fnum(r["taxKrw"])
            if fx <= 0 and net > 0 and raw_krw_net > 0:
                fx = round(raw_krw_net / net, 2)
            krw_net = float(int(round(raw_krw_net if raw_krw_net > 0 else (net * fx))))
            krw_tax = float(int(round(raw_krw_tax if raw_krw_tax > 0 else (tax * fx))))
            krw_gross = krw_net + krw_tax
            sh_held = qty if qty > 0 else None
            dps = round(gross / sh_held, 6) if (sh_held and sh_held > 0) else None
            memo = f"외화배당 입금 ({sh_held:g}주)" if sh_held else "외화배당 입금"
            dividend_records.append((sym, name, "US", "USD", dt_iso, gross, tax, net, fx if fx > 0 else None, krw_gross, krw_tax, krw_net, dps, sh_held, "PDF", memo))

        elif sec == "USD" and tx == "외화배당단주대금입금" and sym:
            net = fnum(r["settleUsd"])
            tax = fnum(r["taxUsd"])
            gross = round(net + tax, 2)
            fx = fnum(r["fxRate"])
            raw_krw_net = fnum(r["settleKrw"])
            raw_krw_tax = fnum(r["taxKrw"])
            if fx <= 0 and net > 0 and raw_krw_net > 0:
                fx = round(raw_krw_net / net, 2)
            krw_net = float(int(round(raw_krw_net if raw_krw_net > 0 else (net * fx))))
            krw_tax = float(int(round(raw_krw_tax if raw_krw_tax > 0 else (tax * fx))))
            krw_gross = krw_net + krw_tax
            dividend_records.append((sym, name, "US", "USD", dt_iso, gross, tax, net, fx if fx > 0 else None, krw_gross, krw_tax, krw_net, None, None, "PDF", "외화배당 단주대금 입금"))

        elif sec == "USD" and tx == "외화배당금취소출금" and sym:
            net = -fnum(r["settleUsd"])
            tax = -fnum(r["taxUsd"])
            gross = round(net + tax, 2)
            fx = fnum(r["fxRate"])
            raw_krw_net = fnum(r["settleKrw"])
            raw_krw_tax = fnum(r["taxKrw"])
            krw_net = -float(int(round(raw_krw_net if raw_krw_net > 0 else (abs(net) * fx))))
            krw_tax = -float(int(round(raw_krw_tax if raw_krw_tax > 0 else (abs(tax) * fx))))
            krw_gross = krw_net + krw_tax
            dividend_records.append((sym, name, "US", "USD", dt_iso, gross, tax, net, fx if fx > 0 else None, krw_gross, krw_tax, krw_net, None, None, "PDF", "외화배당금 취소출금 차감"))

        elif sec == "USD" and tx == "외국납부세액출금" and sym:
            add_tax = fnum(r["settleUsd"])
            fx = fnum(r["fxRate"])
            raw_krw = fnum(r["settleKrw"])
            if add_tax > 0:
                add_tax_krw = float(int(round(raw_krw if raw_krw > 0 else (add_tax * fx))))
                dividend_records.append((sym, name, "US", "USD", dt_iso, 0.0, add_tax, -add_tax, fx if fx > 0 else None, 0.0, add_tax_krw, -add_tax_krw, None, None, "PDF", "외국납부세액 추가출금"))

        elif sec == "USD" and tx == "외화세금환급" and sym:
            ref_usd = fnum(r["settleUsd"])
            ref_krw = fnum(r["settleKrw"])
            fx = fnum(r["fxRate"])
            if ref_usd > 0:
                eff_ref_krw = ref_krw if ref_krw > 0 else (ref_usd * fx)
                usd_refund_by_date_sym[(dt_iso, sym)].append(ref_usd)
                usd_refund_krw_by_date_sym[(dt_iso, sym)] += eff_ref_krw
                usd_refund_usd_by_date[dt_iso] += ref_usd
                usd_refund_krw_by_date[dt_iso] += eff_ref_krw

        elif sec == "KRW" and tx == "배당세출금":
            tax_krw = fnum(r["settleKrw"])
            if tax_krw > 0:
                if sym:
                    krw_divtax_by_date_sym[(dt_iso, sym)] += tax_krw
                else:
                    krw_divtax_unassigned_by_date[dt_iso] += tax_krw

    # 7) 세금재정산(외화세금환급 USD 입금 vs 원화 배당세출금 KRW 출금) 통합 정산
    all_tax_adj_dates = sorted(
        set(k[0] for k in usd_refund_by_date_sym.keys())
        | set(k[0] for k in krw_divtax_by_date_sym.keys())
        | set(krw_divtax_unassigned_by_date.keys())
    )
    for dt_iso in all_tax_adj_dates:
        tot_ref_usd = usd_refund_usd_by_date.get(dt_iso, 0.0)
        tot_ref_krw = usd_refund_krw_by_date.get(dt_iso, 0.0)
        implied_fx = (tot_ref_krw / tot_ref_usd) if (tot_ref_usd > 0 and tot_ref_krw > 0) else 1380.0
        unassigned_krw = krw_divtax_unassigned_by_date.get(dt_iso, 0.0)

        syms_on_dt = sorted(
            {s for (d, s) in usd_refund_by_date_sym.keys() if d == dt_iso}
            | {s for (d, s) in krw_divtax_by_date_sym.keys() if d == dt_iso}
        )
        for sym in syms_on_dt:
            meta = stock_meta.get(sym, {"name": sym, "market": "US", "currency": "USD"})
            ref_usd_sum = round(sum(usd_refund_by_date_sym.get((dt_iso, sym), [])), 2)
            ref_krw_sum = usd_refund_krw_by_date_sym.get((dt_iso, sym), 0.0)
            direct_krw_tax = krw_divtax_by_date_sym.get((dt_iso, sym), 0.0)
            alloc_krw_tax = 0.0
            if unassigned_krw > 0 and tot_ref_usd > 0 and ref_usd_sum > 0:
                alloc_krw_tax = unassigned_krw * (ref_usd_sum / tot_ref_usd)
            total_krw_tax_for_sym = direct_krw_tax + alloc_krw_tax

            if meta["currency"] == "KRW":
                if total_krw_tax_for_sym > 0:
                    krw_t = float(int(round(total_krw_tax_for_sym)))
                    dividend_records.append(
                        (sym, meta["name"], "KR", "KRW", dt_iso, 0.0, krw_t, -krw_t, 1.0, 0.0, krw_t, -krw_t, None, None, "PDF", "배당소득세 추가출금")
                    )
            else:
                krw_tax_in_usd = round(total_krw_tax_for_sym / implied_fx, 2) if total_krw_tax_for_sym > 0 else 0.0
                net_tax_delta = round(krw_tax_in_usd - ref_usd_sum, 2)
                net_cash_delta = round(-net_tax_delta, 2)
                krw_net_tax_delta = float(int(round(total_krw_tax_for_sym - ref_krw_sum)))
                krw_net_cash_delta = -krw_net_tax_delta
                if abs(net_cash_delta) >= 0.01 or abs(krw_net_cash_delta) >= 1:
                    if ref_usd_sum > 0 and total_krw_tax_for_sym > 0:
                        memo = f"배당 세금재정산 (외화환급 +${ref_usd_sum:.2f} / 원화징수 -{int(round(total_krw_tax_for_sym)):,}원 상계)"
                    elif ref_usd_sum > 0:
                        memo = f"외화 배당세금 환급 (+${ref_usd_sum:.2f})"
                    else:
                        memo = f"해외배당 국내 원화징수세 반영 (-{int(round(total_krw_tax_for_sym)):,}원)"
                    dividend_records.append(
                        (sym, meta["name"], "US", "USD", dt_iso, 0.0, net_tax_delta, net_cash_delta, round(implied_fx, 2), 0.0, krw_net_tax_delta, krw_net_cash_delta, None, None, "PDF", memo)
                    )

    # 8) 종목별 현재 보유수량 · 이동평균 매입단가 산출하여 holdings_snapshot 초기 세팅
    def _trade_sort_key(tr: Tuple[Any, ...]) -> Tuple[str, int]:
        dt_s = str(tr[6])
        ext_s = str(tr[0])
        side_s = str(tr[5])
        if ext_s.startswith("PDF_MERGE_FRAC_"):
            return (dt_s, 0)
        order_map = {"BUY": 1, "SPLIT_OUT": 2, "SPLIT_IN": 3, "SELL": 4}
        return (dt_s, order_map.get(side_s, 5))

    trade_records.sort(key=_trade_sort_key)

    trades_by_sym: Dict[str, List[Tuple[Any, ...]]] = defaultdict(list)
    for tr in trade_records:
        trades_by_sym[tr[1]].append(tr)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    initial_holdings: List[Tuple[Any, ...]] = []

    for sym, tr_list in sorted(trades_by_sym.items()):
        running_qty = 0.0
        running_avg = 0.0
        pending_split_cost = 0.0
        for tr in tr_list:
            side, qty, price, gross = tr[5], float(tr[7]), float(tr[8]), float(tr[9])
            if side == "BUY":
                cost_pool = running_qty * running_avg + gross
                running_qty += qty
                running_avg = (cost_pool / running_qty) if running_qty > 1e-9 else 0.0
            elif side == "SELL":
                running_qty = max(0.0, running_qty - qty)
                if running_qty <= 1e-8:
                    running_qty = 0.0
                    running_avg = 0.0
            elif side == "SPLIT_OUT":
                take_q = min(qty, running_qty)
                pending_split_cost += take_q * running_avg
                running_qty = max(0.0, running_qty - take_q)
                if running_qty <= 1e-8:
                    running_qty = 0.0
            elif side == "SPLIT_IN":
                cost_pool = running_qty * running_avg + pending_split_cost
                pending_split_cost = 0.0
                running_qty += qty
                running_avg = (cost_pool / running_qty) if running_qty > 1e-9 else 0.0

        running_qty = round(running_qty, 6)
        if running_qty > 1e-6:
            meta = stock_meta[sym]
            mkt, cur, name = meta["market"], meta["currency"], meta["name"]
            lp = float(meta["last_price"]) if float(meta["last_price"]) > 0 else running_avg
            purch_amt = round(running_qty * running_avg, 2 if cur == "USD" else 0)
            eval_amt = round(running_qty * lp, 2 if cur == "USD" else 0)
            est_tax = round(eval_amt * 0.0018) if (mkt == "KR" and not re.search(r"ETF|KODEX|TIGER|RISE|ACE|PLUS|SOL", name.upper())) else 0.0
            pnl_gross = round(eval_amt - purch_amt, 2 if cur == "USD" else 0)
            pnl_net = round(pnl_gross - est_tax, 2 if cur == "USD" else 0)
            rate_g = (pnl_gross / purch_amt) if purch_amt > 0 else 0.0
            rate_n = (pnl_net / purch_amt) if purch_amt > 0 else 0.0
            initial_holdings.append(
                (
                    sym,
                    name,
                    mkt,
                    cur,
                    running_qty,
                    running_avg,
                    lp,
                    purch_amt,
                    eval_amt,
                    eval_amt - est_tax,
                    pnl_gross,
                    pnl_net,
                    rate_g,
                    rate_n,
                    0.0,
                    est_tax,
                    now_str,
                )
            )

    # 9) SQLite 저장 (기존 타사 수령 배당 PRIOR_BROKER 및 MANUAL 항목은 보존!)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        init_schema(conn)

        # 활성 계좌 키 결정: 이미 ACC_*가 존재하면 그 키 사용, 없으면 LIVE
        if target_account_key:
            acc_key = target_account_key
        else:
            row_acc = conn.execute(
                "SELECT account_key FROM holdings_snapshot WHERE account_key LIKE 'ACC_%' LIMIT 1"
            ).fetchone() or conn.execute(
                "SELECT account_key FROM trades WHERE account_key LIKE 'ACC_%' LIMIT 1"
            ).fetchone()
            acc_key = row_acc["account_key"] if row_acc else "LIVE"

        for alias_k, canon_v in aliases.items():
            conn.execute(
                "INSERT OR REPLACE INTO symbol_aliases (alias, canonical_symbol) VALUES (?, ?)",
                (alias_k, canon_v),
            )

        for sym, meta in stock_meta.items():
            conn.execute(
                """
                INSERT INTO stocks (symbol, name, market, currency, isin, last_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name,
                    market = excluded.market,
                    currency = excluded.currency,
                    isin = CASE WHEN excluded.isin != '' THEN excluded.isin ELSE stocks.isin END,
                    last_price = CASE WHEN stocks.last_price > 0 THEN stocks.last_price ELSE excluded.last_price END,
                    updated_at = excluded.updated_at
                """,
                (sym, meta["name"], meta["market"], meta["currency"], meta["isin"], meta["last_price"], now_str),
            )

        # 이미 Open API 실시간 동기화가 수행된 계좌인지 확인 (실시간 보유 잔고 및 API 체결 내역 보호)
        api_trades_cnt = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE account_key = ? AND source = 'API'", (acc_key,)
        ).fetchone()[0]
        last_sync_meta = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (f"last_sync_{acc_key}",)
        ).fetchone()
        has_live_api_holdings = bool(
            api_trades_cnt > 0
            or (last_sync_meta and last_sync_meta["value"] and "PDF 원장" not in str(last_sync_meta["value"]))
        )

        # 기존 PDF 유래 데이터만 교체 (API, PRIOR_BROKER, PRIOR_YEARLY, MANUAL 보존)
        conn.execute(
            "DELETE FROM trades WHERE account_key = ? AND source IN ('PDF', 'PDF_TRANSFER', 'PDF_EVENT', 'PDF_CORP')",
            (acc_key,),
        )
        conn.execute(
            "DELETE FROM dividends WHERE account_key = ? AND source = 'PDF'",
            (acc_key,),
        )

        conn.executemany(
            """
            INSERT INTO trades (
                account_key, external_order_id, symbol, name, market, currency,
                side, trade_date, quantity, price, gross_amount, commission, tax, source, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(acc_key, *tr) for tr in trade_records],
        )

        conn.executemany(
            """
            INSERT INTO dividends (
                account_key, symbol, name, market, currency, pay_date,
                gross_amount, tax_amount, net_amount,
                fx_rate, krw_gross, krw_tax, krw_net,
                per_share, shares_held, source, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(acc_key, *dv) for dv in dividend_records],
        )

        # 새 PDF 기준일(max_trade_date_iso) 이하에 이미 결제 반영된 기존 API 체결 및 AUTO 배당 자동 정리 (중복 원천 방지)
        if max_trade_date_iso:
            conn.execute(
                "DELETE FROM dividends WHERE account_key = ? AND source = 'AUTO' AND pay_date <= ?",
                (acc_key, max_trade_date_iso),
            )
            try:
                cutoff_dt_obj = datetime.date.fromisoformat(max_trade_date_iso[:10])
                rem = 0
                hard_dt = cutoff_dt_obj
                while rem < 5:
                    hard_dt -= datetime.timedelta(days=1)
                    if hard_dt.weekday() < 5:
                        rem += 1
                hard_cutoff_iso = hard_dt.isoformat()
                conn.execute(
                    "DELETE FROM trades WHERE account_key = ? AND source = 'API' AND trade_date <= ?",
                    (acc_key, hard_cutoff_iso),
                )
                rem_api_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, symbol, side, trade_date, quantity, price, currency FROM trades WHERE account_key = ? AND source = 'API' AND trade_date <= ? ORDER BY trade_date ASC, id ASC",
                        (acc_key, max_trade_date_iso),
                    ).fetchall()
                ]
                if rem_api_rows:
                    b_from_iso = (cutoff_dt_obj - datetime.timedelta(days=12)).isoformat()
                    b_pdf_rows = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id, symbol, side, trade_date, quantity, price FROM trades WHERE account_key = ? AND source = 'PDF' AND trade_date >= ? ORDER BY trade_date ASC, id ASC",
                            (acc_key, b_from_iso),
                        ).fetchall()
                    ]
                    used_pdf_ids = set()
                    api_ids_to_delete: List[int] = []
                    for ar in rem_api_rows:
                        a_dt = datetime.date.fromisoformat(str(ar["trade_date"])[:10])
                        a_qty = float(ar["quantity"])
                        a_price = float(ar["price"])
                        q_tol = max(1e-5, a_qty * 1e-4)
                        p_tol = max(0.03 if ar["currency"] == "USD" else 10.0, a_price * 0.008) if a_price > 0 else 0.0
                        matched_pr = next(
                            (
                                pr
                                for pr in b_pdf_rows
                                if pr["id"] not in used_pdf_ids
                                and pr["symbol"] == ar["symbol"]
                                and pr["side"] == ar["side"]
                                and 0 <= (datetime.date.fromisoformat(str(pr["trade_date"])[:10]) - a_dt).days <= 6
                                and abs(float(pr["quantity"]) - a_qty) <= q_tol
                                and (a_price <= 0 or float(pr["price"]) <= 0 or abs(float(pr["price"]) - a_price) <= p_tol)
                            ),
                            None,
                        )
                        if matched_pr:
                            used_pdf_ids.add(matched_pr["id"])
                            api_ids_to_delete.append(int(ar["id"]))
                    if api_ids_to_delete:
                        conn.executemany("DELETE FROM trades WHERE id = ?", [(i,) for i in api_ids_to_delete])
            except Exception:
                pass

        if not has_live_api_holdings:
            conn.execute("DELETE FROM holdings_snapshot WHERE account_key = ?", (acc_key,))
            conn.executemany(
                """
                INSERT INTO holdings_snapshot (
                    account_key, symbol, name, market, currency, quantity, avg_purchase_price,
                    last_price, purchase_amount, eval_amount, eval_amount_after_cost,
                    pnl_amount, pnl_amount_after_cost, pnl_rate, pnl_rate_after_cost,
                    commission, tax, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(acc_key, *h) for h in initial_holdings],
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                (f"last_sync_{acc_key}", f"{now_str} (PDF 원장 ~{max_trade_date_iso})"),
            )

        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('pdf_ledger_cutoff_date', ?)",
            (max_trade_date_iso,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('pdf_imported_at', ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
            (f"last_div_sync_{acc_key}", f"{now_str} (PDF 실수령·환율 반영)"),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "accountKey": acc_key,
        "cutoffDate": max_trade_date_iso,
        "rawRowsParsed": len(rows),
        "totalCanonicalStocks": len(stock_meta),
        "heldStocksCount": len(initial_holdings),
        "soldStocksCount": len(stock_meta) - len(initial_holdings),
        "tradesInserted": len(trade_records),
        "dividendsInserted": len(dividend_records),
        "lastKrwBalance": last_krw_balance,
        "lastUsdBalance": last_usd_balance,
    }


if __name__ == "__main__":
    summary = import_pdf_ledger_to_db()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
