#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
크로스플랫폼(Windows · macOS · Linux) 순수 Python 표준 라이브러리 기반
토스증권 거래내역서 PDF 정밀 좌표 파서 (외부 pip 패키지 설치 불필요)
- PDF 1.x Content Stream(BT/ET, Tf, Tm/Td/TD, Tj/TJ) 및 ToUnicode CMap 직접 해석
- 원화 거래내역 / 달러(외화) 거래내역 표의 행·열 좌표를 정밀 분리하여 JSON/List 반환
"""

import json
import os
import re
import sys
import unicodedata
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATE_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2})\b\s*(.*)$")
USD_PAREN_RE = re.compile(r"\(\$\s*([\-0-9,.]+)\)")
CODE_PAREN_RE = re.compile(r"^(.*)\(([A-Z0-9]{6,16})\)$")
ISSUE_NO_RE = re.compile(r"(\d{8}-\d{3}-\d{2}-[A-Z0-9]+)")


def _clean_num(s: str) -> str:
    return s.replace(",", "").strip()


def _split_name_and_code(raw: str) -> Tuple[str, str]:
    compact = raw.strip()
    if not compact:
        return "", ""
    m = CODE_PAREN_RE.match(compact)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return compact, ""


def _decode_utf16be_hex(hex_str: str) -> str:
    b = bytes.fromhex(hex_str)
    return b.decode("utf-16-be", errors="replace")


def _parse_tounicode_cmap(cmap_bytes: bytes) -> Dict[int, str]:
    text = cmap_bytes.decode("latin1", errors="replace")
    mapping: Dict[int, str] = {}

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src_h, dst_h in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            cid = int(src_h, 16)
            mapping[cid] = _decode_utf16be_hex(dst_h)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            if not line or not line.startswith("<"):
                continue
            m_arr = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*)\]", line)
            if m_arr:
                start_cid = int(m_arr.group(1), 16)
                end_cid = int(m_arr.group(2), 16)
                targets = re.findall(r"<([0-9A-Fa-f]+)>", m_arr.group(3))
                for offset, dst_h in enumerate(targets):
                    if start_cid + offset <= end_cid:
                        mapping[start_cid + offset] = _decode_utf16be_hex(dst_h)
                continue
            m_rng = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", line)
            if m_rng:
                start_cid = int(m_rng.group(1), 16)
                end_cid = int(m_rng.group(2), 16)
                dst_hex = m_rng.group(3)
                if len(dst_hex) <= 4:
                    base_cp = int(dst_hex, 16)
                    for offset in range(end_cid - start_cid + 1):
                        mapping[start_cid + offset] = chr(base_cp + offset)
                else:
                    base_str = _decode_utf16be_hex(dst_hex)
                    for offset in range(end_cid - start_cid + 1):
                        mapping[start_cid + offset] = base_str[:-1] + chr(ord(base_str[-1]) + offset)

    return mapping


def _parse_pdf_literal_bytes(buf: bytes, start_idx: int) -> Tuple[bytes, int]:
    """buf[start_idx] == ord('(') 위치에서 PDF literal string 바이트열과 닫는 괄호 다음 인덱스를 반환"""
    n = len(buf)
    i = start_idx + 1
    depth = 1
    out = bytearray()
    while i < n and depth > 0:
        ch = buf[i]
        if ch == 0x5C:  # backslash '\'
            i += 1
            if i >= n:
                break
            esc = buf[i]
            if esc == ord("n"):
                out.append(0x0A)
                i += 1
            elif esc == ord("r"):
                out.append(0x0D)
                i += 1
            elif esc == ord("t"):
                out.append(0x09)
                i += 1
            elif esc == ord("b"):
                out.append(0x08)
                i += 1
            elif esc == ord("f"):
                out.append(0x0C)
                i += 1
            elif esc in (ord("("), ord(")"), 0x5C):
                out.append(esc)
                i += 1
            elif esc == 0x0D:  # line continuation \r or \r\n
                i += 1
                if i < n and buf[i] == 0x0A:
                    i += 1
            elif esc == 0x0A:  # line continuation \n
                i += 1
            elif 0x30 <= esc <= 0x37:  # octal \ddd (1~3 digits)
                oct_digits = bytes([esc])
                i += 1
                for _ in range(2):
                    if i < n and 0x30 <= buf[i] <= 0x37:
                        oct_digits += bytes([buf[i]])
                        i += 1
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
            else:
                out.append(esc)
                i += 1
        elif ch == ord("("):
            depth += 1
            out.append(ch)
            i += 1
        elif ch == ord(")"):
            depth -= 1
            if depth > 0:
                out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return bytes(out), i


def _decode_cid_bytes(raw_bytes: bytes, cmap: Optional[Dict[int, str]], is_two_byte: bool = True) -> str:
    if not raw_bytes:
        return ""
    if cmap:
        if is_two_byte and len(raw_bytes) % 2 == 0:
            chars = []
            for k in range(0, len(raw_bytes), 2):
                cid = (raw_bytes[k] << 8) | raw_bytes[k + 1]
                ch = cmap.get(cid, "")
                if ch == "\xa0":
                    ch = " "
                chars.append(ch)
            return "".join(chars)
        chars = [cmap.get(b, chr(b)) for b in raw_bytes]
        return "".join(" " if c == "\xa0" else c for c in chars)
    return raw_bytes.decode("latin1", errors="replace").replace("\xa0", " ")


def _extract_stream_bytes(obj_body: bytes) -> Optional[bytes]:
    m = re.search(rb"stream(?:\r\n|\n)(.*?)(?:\r\n|\n)?endstream", obj_body, re.S)
    if not m:
        return None
    raw = m.group(1)
    hdr = obj_body.split(b"stream", 1)[0]
    if b"/FlateDecode" in hdr:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw.strip(b"\r\n"))
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _matmul(m1: List[float], m2: List[float]) -> List[float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]


def _tokenize_content_stream(stream_bytes: bytes) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    n = len(stream_bytes)
    i = 0
    ws = b" \t\r\n\x00\x0c"
    delims = b"()<>[]{}/%"

    while i < n:
        ch = stream_bytes[i]
        if ch in ws:
            i += 1
            continue
        if ch == ord("%"):
            while i < n and stream_bytes[i] not in (0x0A, 0x0D):
                i += 1
            continue
        if ch == ord("("):
            lit, nxt = _parse_pdf_literal_bytes(stream_bytes, i)
            tokens.append(("BYTES", lit))
            i = nxt
            continue
        if ch == ord("<"):
            if i + 1 < n and stream_bytes[i + 1] == ord("<"):
                tokens.append(("OP", "<<"))
                i += 2
                continue
            j = i + 1
            while j < n and stream_bytes[j] != ord(">"):
                j += 1
            hex_clean = re.sub(rb"\s+", b"", stream_bytes[i + 1 : j])
            if len(hex_clean) % 2 == 1:
                hex_clean += b"0"
            try:
                tokens.append(("BYTES", bytes.fromhex(hex_clean.decode("ascii"))))
            except ValueError:
                tokens.append(("BYTES", b""))
            i = j + 1
            continue
        if ch == ord(">") and i + 1 < n and stream_bytes[i + 1] == ord(">"):
            tokens.append(("OP", ">>"))
            i += 2
            continue
        if ch == ord("["):
            i += 1
            arr_items: List[Tuple[str, Any]] = []
            while i < n:
                c2 = stream_bytes[i]
                if c2 in ws:
                    i += 1
                    continue
                if c2 == ord("]"):
                    i += 1
                    break
                if c2 == ord("("):
                    lit, i = _parse_pdf_literal_bytes(stream_bytes, i)
                    arr_items.append(("BYTES", lit))
                elif c2 == ord("<"):
                    j = i + 1
                    while j < n and stream_bytes[j] != ord(">"):
                        j += 1
                    hex_clean = re.sub(rb"\s+", b"", stream_bytes[i + 1 : j])
                    if len(hex_clean) % 2 == 1:
                        hex_clean += b"0"
                    try:
                        arr_items.append(("BYTES", bytes.fromhex(hex_clean.decode("ascii"))))
                    except ValueError:
                        pass
                    i = j + 1
                else:
                    j = i
                    while j < n and stream_bytes[j] not in ws and stream_bytes[j] not in delims:
                        j += 1
                    tok_s = stream_bytes[i:j].decode("latin1")
                    try:
                        arr_items.append(("NUM", float(tok_s)))
                    except ValueError:
                        arr_items.append(("OTHER", tok_s))
                    i = j
            tokens.append(("ARRAY", arr_items))
            continue
        if ch == ord("/"):
            j = i + 1
            while j < n and stream_bytes[j] not in ws and stream_bytes[j] not in delims:
                j += 1
            tokens.append(("NAME", stream_bytes[i + 1 : j].decode("latin1")))
            i = j
            continue

        j = i
        while j < n and stream_bytes[j] not in ws and stream_bytes[j] not in delims:
            j += 1
        word = stream_bytes[i:j].decode("latin1")
        if word:
            try:
                tokens.append(("NUM", float(word)))
            except ValueError:
                tokens.append(("OP", word))
        i = j

    return tokens


def _extract_page_text_runs(
    stream_bytes: bytes,
    font_cmaps: Dict[str, Tuple[Dict[int, str], bool]],
) -> List[Dict[str, Any]]:
    """페이지 내 각 텍스트 조각의 (x, midY, baselineY, fontSize, text) 리스트 반환"""
    tokens = _tokenize_content_stream(stream_bytes)
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    ctm_stack: List[List[float]] = []

    tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    cur_font = "F1"
    cur_size = 8.0
    leading = 0.0

    operands: List[Tuple[str, Any]] = []
    runs: List[Dict[str, Any]] = []

    def emit_text(txt: str) -> None:
        cleaned = txt.replace("\n", " ").replace("\r", " ")
        if not cleaned.strip():
            return
        full_m = _matmul(tm, ctm)
        x = full_m[4]
        base_y = full_m[5]
        eff_size = abs(cur_size * full_m[3]) if abs(full_m[3]) > 1e-6 else abs(cur_size)
        mid_y = base_y + eff_size * 0.32
        runs.append(
            {
                "x": x,
                "baseY": base_y,
                "midY": mid_y,
                "size": eff_size,
                "text": cleaned,
            }
        )

    for t_kind, t_val in tokens:
        if t_kind != "OP":
            operands.append((t_kind, t_val))
            continue

        op = t_val
        if op == "q":
            ctm_stack.append(list(ctm))
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == "cm" and len(operands) >= 6:
            vals = [float(x[1]) for x in operands[-6:] if x[0] == "NUM"]
            if len(vals) == 6:
                ctm = _matmul(vals, ctm)
        elif op == "BT":
            tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif op == "Tf" and len(operands) >= 2:
            if operands[-2][0] == "NAME":
                cur_font = operands[-2][1]
            if operands[-1][0] == "NUM":
                cur_size = float(operands[-1][1])
        elif op == "TL" and len(operands) >= 1 and operands[-1][0] == "NUM":
            leading = float(operands[-1][1])
        elif op == "Tm" and len(operands) >= 6:
            vals = [float(x[1]) for x in operands[-6:] if x[0] == "NUM"]
            if len(vals) == 6:
                tm = list(vals)
                tlm = list(vals)
        elif op in ("Td", "TD") and len(operands) >= 2:
            if operands[-2][0] == "NUM" and operands[-1][0] == "NUM":
                tx, ty = float(operands[-2][1]), float(operands[-1][1])
                if op == "TD":
                    leading = -ty
                tlm = _matmul([1.0, 0.0, 0.0, 1.0, tx, ty], tlm)
                tm = list(tlm)
        elif op == "T*":
            tlm = _matmul([1.0, 0.0, 0.0, 1.0, 0.0, -leading], tlm)
            tm = list(tlm)
        elif op == "Tj" and len(operands) >= 1:
            cmap, is_2b = font_cmaps.get(cur_font, (None, True))
            if operands[-1][0] == "BYTES":
                emit_text(_decode_cid_bytes(operands[-1][1], cmap, is_2b))
        elif op == "TJ" and len(operands) >= 1 and operands[-1][0] == "ARRAY":
            cmap, is_2b = font_cmaps.get(cur_font, (None, True))
            parts: List[str] = []
            for ak, av in operands[-1][1]:
                if ak == "BYTES":
                    parts.append(_decode_cid_bytes(av, cmap, is_2b))
                elif ak == "NUM" and float(av) < -180:
                    parts.append(" ")
            emit_text("".join(parts))
        elif op in ("'", '"') and len(operands) >= 1:
            tlm = _matmul([1.0, 0.0, 0.0, 1.0, 0.0, -leading], tlm)
            tm = list(tlm)
            cmap, is_2b = font_cmaps.get(cur_font, (None, True))
            if operands[-1][0] == "BYTES":
                emit_text(_decode_cid_bytes(operands[-1][1], cmap, is_2b))

        operands.clear()

    return runs


def _collect_pages_ordered(objs: Dict[int, bytes]) -> List[int]:
    """Catalog -> Pages 트리를 순회하여 페이지 객체 번호를 순서대로 반환"""
    root_pages_id = None
    for body in objs.values():
        if re.search(rb"/Type\s*/Catalog\b", body):
            m = re.search(rb"/Pages\s+(\d+)\s+0\s+R", body)
            if m:
                root_pages_id = int(m.group(1))
                break

    ordered: List[int] = []

    def visit(oid: int, visited: set) -> None:
        if oid in visited or oid not in objs:
            return
        visited.add(oid)
        body = objs[oid]
        if re.search(rb"/Type\s*/Page\b", body):
            ordered.append(oid)
            return
        kids_m = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if kids_m:
            for km in re.finditer(rb"(\d+)\s+0\s+R", kids_m.group(1)):
                visit(int(km.group(1)), visited)

    if root_pages_id is not None:
        visit(root_pages_id, set())

    if not ordered:
        for oid in sorted(objs.keys()):
            if re.search(rb"/Type\s*/Page\b", objs[oid]):
                ordered.append(oid)
    return ordered


def _build_page_font_cmaps(
    page_body: bytes,
    objs: Dict[int, bytes],
    cmap_cache: Dict[int, Dict[int, str]],
) -> Dict[str, Tuple[Dict[int, str], bool]]:
    res_body = page_body
    rm = re.search(rb"/Resources\s+(\d+)\s+0\s+R", page_body)
    if rm and int(rm.group(1)) in objs:
        res_body = objs[int(rm.group(1))]

    font_dict_body = res_body
    fm_indirect = re.search(rb"/Font\s+(\d+)\s+0\s+R", res_body)
    if fm_indirect and int(fm_indirect.group(1)) in objs:
        font_dict_body = objs[int(fm_indirect.group(1))]

    result: Dict[str, Tuple[Dict[int, str], bool]] = {}
    for fm in re.finditer(rb"/([A-Za-z0-9_.\-]+)\s+(\d+)\s+0\s+R", font_dict_body):
        fname = fm.group(1).decode("latin1")
        f_oid = int(fm.group(2))
        if fname == "Font" or f_oid not in objs:
            continue
        f_body = objs[f_oid]
        is_two_byte = b"/Identity-H" in f_body or b"/Type0" in f_body
        tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", f_body)
        if tu:
            tu_oid = int(tu.group(1))
            if tu_oid not in cmap_cache and tu_oid in objs:
                tu_bytes = _extract_stream_bytes(objs[tu_oid])
                cmap_cache[tu_oid] = _parse_tounicode_cmap(tu_bytes) if tu_bytes else {}
            result[fname] = (cmap_cache.get(tu_oid, {}), is_two_byte)
    return result


def _get_page_content_bytes(page_body: bytes, objs: Dict[int, bytes]) -> bytes:
    m_arr = re.search(rb"/Contents\s*\[(.*?)\]", page_body, re.S)
    if m_arr:
        chunks = []
        for cm in re.finditer(rb"(\d+)\s+0\s+R", m_arr.group(1)):
            c_oid = int(cm.group(1))
            if c_oid in objs:
                sb = _extract_stream_bytes(objs[c_oid])
                if sb:
                    chunks.append(sb)
        return b"\n".join(chunks)

    m_single = re.search(rb"/Contents\s+(\d+)\s+0\s+R", page_body)
    if m_single:
        c_oid = int(m_single.group(1))
        if c_oid in objs:
            return _extract_stream_bytes(objs[c_oid]) or b""
    return b""


def extract_rows_from_pdf_dir_python(pdf_dir: Path) -> List[Dict[str, Any]]:
    if not pdf_dir.exists():
        return []

    files = sorted(
        [p for p in pdf_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: unicodedata.normalize("NFC", p.name),
    )

    all_rows: List[Dict[str, Any]] = []
    parse_errors: List[str] = []

    for file_path in files:
        file_name = unicodedata.normalize("NFC", file_path.name)
        try:
            pdf_bytes = file_path.read_bytes()
        except Exception as e:
            parse_errors.append(f"Failed to read {file_name}: {e}")
            continue

        if b"/Encrypt" in pdf_bytes[:4096] or re.search(rb"/Encrypt\s+\d+\s+0\s+R", pdf_bytes):
            raise RuntimeError(
                f"비밀번호가 설정된 암호화 PDF 파일({file_name})입니다. 토스증권에서 비밀번호 없는 일반 거래내역서 PDF로 저장해 올려주세요."
            )

        objs: Dict[int, bytes] = {
            int(m.group(1)): m.group(2)
            for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)endobj", pdf_bytes, re.S)
        }
        page_oids = _collect_pages_ordered(objs)
        if not page_oids:
            continue

        cmap_cache: Dict[int, Dict[int, str]] = {}
        current_section = "KRW"
        doc_issue_no = file_name

        # 첫 페이지에서 발급번호 추출
        p0_fonts = _build_page_font_cmaps(objs[page_oids[0]], objs, cmap_cache)
        p0_runs = _extract_page_text_runs(_get_page_content_bytes(objs[page_oids[0]], objs), p0_fonts)
        p0_full_text = " ".join(r["text"].strip() for r in p0_runs)
        m_issue = ISSUE_NO_RE.search(p0_full_text)
        if m_issue:
            doc_issue_no = m_issue.group(1)

        for p_idx, p_oid in enumerate(page_oids):
            p_body = objs[p_oid]
            p_fonts = _build_page_font_cmaps(p_body, objs, cmap_cache)
            runs = _extract_page_text_runs(_get_page_content_bytes(p_body, objs), p_fonts)

            # 상단 여백(midY > 528) 제외 및 y 내림차순, x 오름차순 정렬
            valid_runs = [r for r in runs if r["midY"] <= 528.0]
            valid_runs.sort(key=lambda r: (-round(r["midY"] / 2.0), r["x"]))

            # 동일 y줄(±2.0pt)의 텍스트들을 줄 단위로 병합하여 섹션 헤더와 거래일자 앵커 탐지
            line_groups: List[List[Dict[str, Any]]] = []
            for r in sorted(valid_runs, key=lambda x: -x["midY"]):
                if line_groups and abs(line_groups[-1][0]["midY"] - r["midY"]) <= 2.2:
                    line_groups[-1].append(r)
                else:
                    line_groups.append([r])

            events: List[Tuple[float, str, str]] = []
            for grp in line_groups:
                grp.sort(key=lambda x: x["x"])
                min_x = grp[0]["x"]
                mid_y = sum(x["midY"] for x in grp) / len(grp)
                line_txt = " ".join(x["text"].strip() for x in grp if x["text"].strip())
                compact_txt = "".join(x["text"].strip() for x in grp if x["text"].strip())

                if min_x < 130 and "원화거래내역" in compact_txt.replace(" ", ""):
                    events.append((mid_y, "SEC_KRW", ""))
                elif min_x < 130 and ("달러거래내역" in compact_txt.replace(" ", "") or "외화거래내역" in compact_txt.replace(" ", "")):
                    events.append((mid_y, "SEC_USD", ""))
                elif min_x < 75 and "거래일자" in compact_txt and "거래구분" in compact_txt:
                    if "거래세" in compact_txt:
                        events.append((mid_y, "SEC_KRW", ""))
                    else:
                        events.append((mid_y, "SEC_USD", ""))
                elif min_x < 75:
                    first_run_txt = grp[0]["text"].strip()
                    m_dt = DATE_RE.match(first_run_txt) or DATE_RE.match(line_txt)
                    if m_dt:
                        # 날짜 셀 자체의 midY를 앵커로 사용
                        events.append((grp[0]["midY"], "ROW", m_dt.group(1)))

            events.sort(key=lambda ev: -ev[0])

            row_anchors: List[Tuple[float, str, str]] = []
            for ev_y, ev_kind, ev_dt in events:
                if ev_kind == "SEC_KRW":
                    current_section = "KRW"
                elif ev_kind == "SEC_USD":
                    current_section = "USD"
                elif ev_kind == "ROW":
                    if row_anchors and abs(row_anchors[-1][0] - ev_y) < 4.0:
                        continue
                    row_anchors.append((ev_y, current_section, ev_dt))

            for r_idx, (anchor_y, sec, dt_str) in enumerate(row_anchors):
                y_top = anchor_y + 6.0
                next_anchor_y = row_anchors[r_idx + 1][0] if (r_idx + 1 < len(row_anchors)) else -100.0
                y_bot = max(anchor_y - 16.5, next_anchor_y + 6.5)
                main_bot = anchor_y - 4.8

                def runs_in_box(x0: float, x1: float, yb: float, yt: float) -> List[Dict[str, Any]]:
                    matched = [
                        it for it in valid_runs
                        if (x0 - 1.5) <= it["x"] < x1 and yb <= it["midY"] <= yt
                    ]
                    matched.sort(key=lambda it: (-round(it["midY"] / 2.0), it["x"]))
                    return matched

                tx_runs = runs_in_box(104.0, 169.5, y_bot, y_top)
                tx_type = "".join(it["text"].strip() for it in tx_runs)

                stock_main = "".join(it["text"].strip() for it in runs_in_box(169.5, 289.5, main_bot, y_top))
                stock_sub = "".join(it["text"].strip() for it in runs_in_box(169.5, 289.5, y_bot, main_bot))
                raw_item = (stock_main + stock_sub).strip()
                s_name, s_code = _split_name_and_code(raw_item)

                num_main_runs = runs_in_box(289.5, 810.0, main_bot, y_top)
                num_main_runs.sort(key=lambda it: it["x"])
                num_main_raw = " ".join(it["text"].strip() for it in num_main_runs if it["text"].strip())
                main_tokens = num_main_raw.split()

                if sec == "KRW":
                    fx = ""
                    toks = list(main_tokens)
                    if len(toks) == 11:
                        fx = _clean_num(toks[0])
                        toks = toks[1:]
                    if len(toks) != 10:
                        parse_errors.append(f"{file_name} p.{p_idx+1} r.{r_idx} KRW tokens={len(main_tokens)}")
                        continue
                    all_rows.append(
                        {
                            "file": file_name,
                            "issueNo": doc_issue_no,
                            "page": p_idx + 1,
                            "rowIdx": r_idx,
                            "section": "KRW",
                            "date": dt_str,
                            "txType": tx_type,
                            "rawItem": raw_item,
                            "stockName": s_name,
                            "stockCode": s_code,
                            "fxRate": fx,
                            "qty": _clean_num(toks[0]),
                            "grossKrw": _clean_num(toks[1]),
                            "grossUsd": "",
                            "settleKrw": _clean_num(toks[2]),
                            "settleUsd": "",
                            "priceKrw": _clean_num(toks[3]),
                            "priceUsd": "",
                            "commKrw": _clean_num(toks[4]),
                            "commUsd": "",
                            "tradeTaxKrw": _clean_num(toks[5]),
                            "taxKrw": _clean_num(toks[6]),
                            "taxUsd": "",
                            "overdue": _clean_num(toks[7]),
                            "holdingQty": _clean_num(toks[8]),
                            "balKrw": _clean_num(toks[9]),
                            "balUsd": "",
                        }
                    )
                else:
                    fx = ""
                    toks = list(main_tokens)
                    if len(toks) == 10:
                        fx = _clean_num(toks[0])
                        toks = toks[1:]
                    num_sub_runs = runs_in_box(289.5, 810.0, y_bot, main_bot)
                    num_sub_runs.sort(key=lambda it: it["x"])
                    num_sub_raw = " ".join(it["text"].strip() for it in num_sub_runs if it["text"].strip())
                    usd_vals = [_clean_num(m.group(1)) for m in USD_PAREN_RE.finditer(num_sub_raw)]

                    if len(toks) != 9 or len(usd_vals) != 7:
                        parse_errors.append(
                            f"{file_name} p.{p_idx+1} r.{r_idx} USD tokens={len(main_tokens)}/{len(usd_vals)}"
                        )
                        continue
                    all_rows.append(
                        {
                            "file": file_name,
                            "issueNo": doc_issue_no,
                            "page": p_idx + 1,
                            "rowIdx": r_idx,
                            "section": "USD",
                            "date": dt_str,
                            "txType": tx_type,
                            "rawItem": raw_item,
                            "stockName": s_name,
                            "stockCode": s_code,
                            "fxRate": fx,
                            "qty": _clean_num(toks[0]),
                            "grossKrw": _clean_num(toks[1]),
                            "grossUsd": usd_vals[0],
                            "settleKrw": _clean_num(toks[2]),
                            "settleUsd": usd_vals[1],
                            "priceKrw": _clean_num(toks[3]),
                            "priceUsd": usd_vals[2],
                            "commKrw": _clean_num(toks[4]),
                            "commUsd": usd_vals[3],
                            "tradeTaxKrw": "0",
                            "taxKrw": _clean_num(toks[5]),
                            "taxUsd": usd_vals[4],
                            "overdue": usd_vals[5],
                            "holdingQty": _clean_num(toks[7]),
                            "balKrw": _clean_num(toks[8]),
                            "balUsd": usd_vals[6],
                        }
                    )

    if parse_errors:
        sys.stderr.write(
            f"[pdf_extractor_py] Extracted {len(all_rows)} rows from {len(files)} files (warnings: {len(parse_errors)})\n"
        )
    return all_rows


if __name__ == "__main__":
    in_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (Path(__file__).resolve().parent / "pdfs")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    extracted = extract_rows_from_pdf_dir_python(in_dir)
    if out_path:
        out_path.write_text(json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
    else:
        print(json.dumps({"rowCount": len(extracted)}, ensure_ascii=False))
