import Foundation
import PDFKit

struct LineItem {
    let minX: CGFloat
    let maxX: CGFloat
    let minY: CGFloat
    let maxY: CGFloat
    let midY: CGFloat
    let text: String
}

struct LedgerRow: Codable {
    let file: String
    let issueNo: String
    let page: Int
    let rowIdx: Int
    let section: String
    let date: String
    let txType: String
    let rawItem: String
    let stockName: String
    let stockCode: String
    let fxRate: String
    let qty: String
    let grossKrw: String
    let grossUsd: String
    let settleKrw: String
    let settleUsd: String
    let priceKrw: String
    let priceUsd: String
    let commKrw: String
    let commUsd: String
    let tradeTaxKrw: String
    let taxKrw: String
    let taxUsd: String
    let overdue: String
    let holdingQty: String
    let balKrw: String
    let balUsd: String
}

let dateRegex = try! NSRegularExpression(pattern: #"^(\d{4}\.\d{2}\.\d{2})\b\s*(.*)$"#)
let usdParenRegex = try! NSRegularExpression(pattern: #"\(\$\s*([\-0-9,.]+)\)"#)
let codeParenRegex = try! NSRegularExpression(pattern: #"^(.*)\(([A-Z0-9]{6,16})\)$"#)
let issueNoRegex = try! NSRegularExpression(pattern: #"(\d{8}-\d{3}-\d{2}-[A-Z0-9]+)"#)

func cleanNum(_ s: String) -> String {
    return s.replacingOccurrences(of: ",", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
}

func splitNameAndCode(_ raw: String) -> (String, String) {
    let compact = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if compact.isEmpty { return ("", "") }
    let ns = compact as NSString
    if let m = codeParenRegex.firstMatch(in: compact, range: NSRange(location: 0, length: ns.length)) {
        let name = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)
        let code = ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespaces)
        return (name, code)
    }
    return (compact, "")
}

let pdfDirPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "./pdfs"
let outJsonPath = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "/tmp/toss_raw_ledger.json"

let fm = FileManager.default
let pdfDir = URL(fileURLWithPath: pdfDirPath)
let files = ((try? fm.contentsOfDirectory(at: pdfDir, includingPropertiesForKeys: nil)) ?? [])
    .filter { $0.pathExtension.lowercased() == "pdf" }
    .sorted { $0.lastPathComponent.precomposedStringWithCanonicalMapping < $1.lastPathComponent.precomposedStringWithCanonicalMapping }

var allRows: [LedgerRow] = []
var parseErrors: [String] = []

for fileURL in files {
    let fileName = fileURL.lastPathComponent.precomposedStringWithCanonicalMapping
    guard let doc = PDFDocument(url: fileURL) else {
        parseErrors.append("Failed to open \(fileName)")
        continue
    }
    var currentSection = "KRW"
    var docIssueNo = fileName
    if let p0 = doc.page(at: 0)?.string {
        let ns0 = p0 as NSString
        if let m = issueNoRegex.firstMatch(in: p0, range: NSRange(location: 0, length: ns0.length)) {
            docIssueNo = ns0.substring(with: m.range(at: 1))
        }
    }

    for pIdx in 0..<doc.pageCount {
        guard let page = doc.page(at: pIdx),
              let fullSel = page.selection(for: page.bounds(for: .mediaBox)) else { continue }

        var lines: [LineItem] = []
        for lSel in fullSel.selectionsByLine() {
            let b = lSel.bounds(for: page)
            if b.midY > 528 { continue }
            let raw = (lSel.string ?? "").replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ").trimmingCharacters(in: .whitespaces)
            if raw.isEmpty { continue }
            lines.append(LineItem(minX: b.minX, maxX: b.maxX, minY: b.minY, maxY: b.maxY, midY: b.midY, text: raw))
        }

        lines.sort { abs($0.midY - $1.midY) > 2.0 ? $0.midY > $1.midY : $0.minX < $1.minX }

        struct Event {
            let y: CGFloat
            let kind: String
            let dateStr: String
        }
        var events: [Event] = []
        for ln in lines {
            if ln.minX < 130 && ln.text.contains("원화 거래내역") {
                events.append(Event(y: ln.midY, kind: "SEC_KRW", dateStr: ""))
            } else if ln.minX < 130 && (ln.text.contains("달러 거래내역") || ln.text.contains("외화 거래내역")) {
                events.append(Event(y: ln.midY, kind: "SEC_USD", dateStr: ""))
            } else if ln.minX < 75 && ln.text.contains("거래일자") && ln.text.contains("거래구분") {
                if ln.text.contains("거래세") {
                    events.append(Event(y: ln.midY, kind: "SEC_KRW", dateStr: ""))
                } else {
                    events.append(Event(y: ln.midY, kind: "SEC_USD", dateStr: ""))
                }
            } else if ln.minX < 75 {
                let ns = ln.text as NSString
                if let m = dateRegex.firstMatch(in: ln.text, range: NSRange(location: 0, length: ns.length)) {
                    let dt = ns.substring(with: m.range(at: 1))
                    events.append(Event(y: ln.midY, kind: "ROW", dateStr: dt))
                }
            }
        }
        events.sort { $0.y > $1.y }

        var rowAnchors: [(y: CGFloat, section: String, date: String)] = []
        for ev in events {
            if ev.kind == "SEC_KRW" {
                currentSection = "KRW"
            } else if ev.kind == "SEC_USD" {
                currentSection = "USD"
            } else if ev.kind == "ROW" {
                if let last = rowAnchors.last, abs(last.y - ev.y) < 4.0 {
                    continue
                }
                rowAnchors.append((y: ev.y, section: currentSection, date: ev.dateStr))
            }
        }

        for (rIdx, anchor) in rowAnchors.enumerated() {
            let yTop = anchor.y + 6.0
            let nextAnchorY = (rIdx + 1 < rowAnchors.count) ? rowAnchors[rIdx + 1].y : -100.0
            let yBot = max(anchor.y - 16.5, nextAnchorY + 6.5)
            let bandRectHeight = yTop - yBot

            func textInX(_ x0: CGFloat, _ x1: CGFloat) -> String {
                guard let s = page.selection(for: CGRect(x: x0, y: yBot, width: x1 - x0, height: bandRectHeight))?.string else { return "" }
                return s.components(separatedBy: .newlines)
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                    .joined(separator: "")
            }

            func stockTextInX(_ x0: CGFloat, _ x1: CGFloat) -> String {
                let mainS = (page.selection(for: CGRect(x: x0, y: anchor.y - 4.8, width: x1 - x0, height: 10.5))?.string ?? "")
                    .replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespaces)
                let subS = (page.selection(for: CGRect(x: x0, y: yBot, width: x1 - x0, height: max(0.1, (anchor.y - 4.8) - yBot)))?.string ?? "")
                    .replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespaces)
                if subS.isEmpty { return mainS }
                if mainS.isEmpty { return subS }
                return mainS + subS
            }

            let txType = textInX(105, 169.5)
            let rawItem = stockTextInX(170, 289.5)
            let (sName, sCode) = splitNameAndCode(rawItem)

            let numMainRaw = (page.selection(for: CGRect(x: 290, y: anchor.y - 4.8, width: 502, height: 10.5))?.string ?? "")
                .replacingOccurrences(of: "\n", with: " ")
            let mainTokens = numMainRaw.split(whereSeparator: { $0.isWhitespace }).map { String($0) }

            if anchor.section == "KRW" {
                var fx = ""
                var toks = mainTokens
                if toks.count == 11 {
                    fx = cleanNum(toks[0])
                    toks = Array(toks.dropFirst())
                }
                if toks.count != 10 {
                    parseErrors.append("\(fileName) p.\(pIdx+1) r.\(rIdx) KRW tokens=\(mainTokens.count)")
                    continue
                }
                allRows.append(LedgerRow(
                    file: fileName, issueNo: docIssueNo, page: pIdx + 1, rowIdx: rIdx, section: "KRW",
                    date: anchor.date, txType: txType, rawItem: rawItem, stockName: sName, stockCode: sCode,
                    fxRate: fx,
                    qty: cleanNum(toks[0]),
                    grossKrw: cleanNum(toks[1]), grossUsd: "",
                    settleKrw: cleanNum(toks[2]), settleUsd: "",
                    priceKrw: cleanNum(toks[3]), priceUsd: "",
                    commKrw: cleanNum(toks[4]), commUsd: "",
                    tradeTaxKrw: cleanNum(toks[5]),
                    taxKrw: cleanNum(toks[6]), taxUsd: "",
                    overdue: cleanNum(toks[7]),
                    holdingQty: cleanNum(toks[8]),
                    balKrw: cleanNum(toks[9]), balUsd: ""
                ))
            } else {
                var fx = ""
                var toks = mainTokens
                if toks.count == 10 {
                    fx = cleanNum(toks[0])
                    toks = Array(toks.dropFirst())
                }
                let numSubRaw = (page.selection(for: CGRect(x: 290, y: yBot, width: 502, height: max(0.1, (anchor.y - 4.8) - yBot)))?.string ?? "")
                    .replacingOccurrences(of: "\n", with: " ")
                let nsSub = numSubRaw as NSString
                let usdMatches = usdParenRegex.matches(in: numSubRaw, range: NSRange(location: 0, length: nsSub.length))
                let usdVals = usdMatches.map { cleanNum(nsSub.substring(with: $0.range(at: 1))) }

                if toks.count != 9 || usdVals.count != 7 {
                    parseErrors.append("\(fileName) p.\(pIdx+1) r.\(rIdx) USD tokens=\(mainTokens.count)/\(usdVals.count)")
                    continue
                }
                allRows.append(LedgerRow(
                    file: fileName, issueNo: docIssueNo, page: pIdx + 1, rowIdx: rIdx, section: "USD",
                    date: anchor.date, txType: txType, rawItem: rawItem, stockName: sName, stockCode: sCode,
                    fxRate: fx,
                    qty: cleanNum(toks[0]),
                    grossKrw: cleanNum(toks[1]), grossUsd: usdVals[0],
                    settleKrw: cleanNum(toks[2]), settleUsd: usdVals[1],
                    priceKrw: cleanNum(toks[3]), priceUsd: usdVals[2],
                    commKrw: cleanNum(toks[4]), commUsd: usdVals[3],
                    tradeTaxKrw: "0",
                    taxKrw: cleanNum(toks[5]), taxUsd: usdVals[4],
                    overdue: usdVals[5],
                    holdingQty: cleanNum(toks[7]),
                    balKrw: cleanNum(toks[8]), balUsd: usdVals[6]
                ))
            }
        }
    }
}

let resultObj: [String: Any] = [
    "fileCount": files.count,
    "rowCount": allRows.count,
    "errorCount": parseErrors.count,
    "errors": Array(parseErrors.prefix(20)),
]
fputs("Extracted \(allRows.count) rows from \(files.count) files (errors: \(parseErrors.count))\n", stderr)
_ = resultObj

let enc = JSONEncoder()
let data = try! enc.encode(allRows)
try! data.write(to: URL(fileURLWithPath: outJsonPath))
