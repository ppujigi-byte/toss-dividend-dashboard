(() => {
  "use strict";

  /**
   * 100% 조회·열람 전용 프론트엔드 (매수·매도 주문 기능 일체 없음)
   * - PDF 기준일 이전: 거래내역서 PDF 확정 원장(매매·타사대체입고·분할병합·실수령배당·세금재정산) 표시
   * - PDF 기준일 이후: 토스 Open API 증분 동기화 + 신규 배당 증분 산출
   * - 타사증권사 이관 전 수령 배당금 '통입력(PRIOR_BROKER)' 지원
   */
  const SESSION_UNLOCKED_CACHE_KEY = "toss_unlocked_vault_session";
  const LOCAL_ENCRYPTED_BACKUP_KEY = "toss_encrypted_vault_envelope";
  const TAX_PREF_STORAGE_KEY = "toss_exclude_tax_pref";
  const US_DIV_CUR_STORAGE_KEY = "toss_us_div_currency_pref";
  const PBKDF2_ITERATIONS = 600000;

  const state = {
    hasVault: false,
    unlocked: false,
    isLedgerOnlyPreview: false,
    envelope: null,
    decryptedCreds: null,
    currentPin: "",
    excludeTax: localStorage.getItem(TAX_PREF_STORAGE_KEY) !== "false",
    usDivInKrw: localStorage.getItem(US_DIV_CUR_STORAGE_KEY) === "KRW",
    showCombinedKrwChart: false,
    statusFilter: "ALL",
    marketFilter: "ALL",
    searchQuery: "",
    sortBy: "totalReturnDesc",
    portfolio: null,
    activeDetailSymbol: null,
    activeDetailTab: "dividends",
    detailTradeFilter: "ALL",
    pinBuffer: "",
    failedPinCount: 0,
    lockedUntil: 0,
    priorModalTab: "byStock",
    priorModalExtraSymbols: [],
    yearlyMinYear: 2019,
    growthCollapsed: false,
    growthShowTable: false,
    growthIncludeStockPrior: true,
    selectedGrowthYear: { KRW: null, USD: null, TOTAL_KRW: null },
    lastGrowthSeries: { KRW: [], USD: [], TOTAL_KRW: [] },
    syncLogFilter: "ALL",
    syncLogs: [],
  };

  // =========================================================================
  // 1. 브라우저 표준 WebCrypto (PBKDF2-SHA256 600,000회 + AES-256-GCM) 암호화
  // =========================================================================
  function bufToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function base64ToBuf(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  async function deriveAesGcmKey(pin, saltBuffer) {
    const enc = new TextEncoder();
    const keyMaterial = await window.crypto.subtle.importKey(
      "raw",
      enc.encode(pin),
      { name: "PBKDF2" },
      false,
      ["deriveKey"]
    );
    return window.crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt: saltBuffer,
        iterations: PBKDF2_ITERATIONS,
        hash: "SHA-256",
      },
      keyMaterial,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  }

  async function encryptCredentialsWithPin(pin, payloadObj) {
    const salt = window.crypto.getRandomValues(new Uint8Array(16));
    const iv = window.crypto.getRandomValues(new Uint8Array(12));
    const key = await deriveAesGcmKey(pin, salt.buffer);
    const encoded = new TextEncoder().encode(JSON.stringify(payloadObj));
    const cipherBuf = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, encoded);
    return {
      version: 1,
      kdf: "PBKDF2-SHA256",
      iterations: PBKDF2_ITERATIONS,
      cipher: "AES-256-GCM",
      salt: bufToBase64(salt.buffer),
      iv: bufToBase64(iv.buffer),
      ciphertext: bufToBase64(cipherBuf),
      updatedAt: new Date().toISOString(),
    };
  }

  async function decryptCredentialsWithPin(pin, envelope) {
    const saltBuf = base64ToBuf(envelope.salt);
    const ivBuf = base64ToBuf(envelope.iv);
    const cipherBuf = base64ToBuf(envelope.ciphertext);
    const key = await deriveAesGcmKey(pin, saltBuf);
    const plainBuf = await window.crypto.subtle.decrypt(
      { name: "AES-GCM", iv: new Uint8Array(ivBuf) },
      key,
      cipherBuf
    );
    return JSON.parse(new TextDecoder().decode(plainBuf));
  }

  async function persistEncryptedEnvelope(envelope) {
    state.envelope = envelope;
    localStorage.setItem(LOCAL_ENCRYPTED_BACKUP_KEY, JSON.stringify(envelope));
    await apiFetch("/api/encrypted-config", {
      method: "POST",
      body: JSON.stringify({ envelope }),
    });
  }

  // =========================================================================
  // 2. 유틸리티 & 통화 포맷터
  // =========================================================================
  const $ = (id) => document.getElementById(id);

  function showToast(msg, isError = false, options = {}) {
    const box = $("toastContainer");
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " error" : "");
    const span = document.createElement("span");
    span.textContent = msg;
    el.appendChild(span);
    if (options.actionLabel && typeof options.onAction === "function") {
      const actBtn = document.createElement("button");
      actBtn.type = "button";
      actBtn.className = "toast-action-btn";
      actBtn.textContent = options.actionLabel;
      actBtn.addEventListener("click", () => {
        el.remove();
        options.onAction();
      });
      el.appendChild(actBtn);
    }
    box.appendChild(el);
    const ttl = options.durationMs || (options.actionLabel ? 8000 : 4800);
    setTimeout(() => el.remove(), ttl);
  }

  function formatMoney(val, currency, showSign = false) {
    const num = Number(val) || 0;
    const absNum = Math.abs(num);
    const sign = num > 0 ? (showSign ? "+" : "") : num < 0 ? "-" : "";
    if (currency === "USD") {
      return `${sign}$${absNum.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;
    }
    return `${sign}${Math.round(absNum).toLocaleString("ko-KR")}원`;
  }

  function formatPrice(val, currency) {
    const num = Number(val) || 0;
    if (currency === "USD") {
      const digits = num > 0 && num < 10 ? 4 : 2;
      return `$${num.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: digits,
      })}`;
    }
    return `${Math.round(num).toLocaleString("ko-KR")}원`;
  }

  function formatQty(val) {
    const num = Number(val) || 0;
    if (Math.abs(num - Math.round(num)) < 1e-7) return `${Math.round(num).toLocaleString("ko-KR")}주`;
    return `${num.toLocaleString("en-US", { maximumFractionDigits: 6 })}주`;
  }

  function formatRate(val, showSign = true) {
    const num = Number(val) || 0;
    const sign = num > 0 && showSign ? "+" : "";
    return `${sign}${num.toFixed(2)}%`;
  }

  function pnlClass(val) {
    const n = Number(val) || 0;
    if (n > 0.0001) return "val-pos";
    if (n < -0.0001) return "val-neg";
    return "val-zero";
  }

  function rateBadgeClass(val) {
    const n = Number(val) || 0;
    if (n > 0.0001) return "bg-pos";
    if (n < -0.0001) return "bg-neg";
    return "bg-zero";
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function getActiveAccountKey() {
    const seq = state.decryptedCreds?.selectedAccountSeq;
    return seq !== undefined && seq !== null ? `ACC_${seq}` : "LIVE";
  }

  async function apiFetch(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    headers["X-Account-Key"] = getActiveAccountKey();
    const res = await fetch(path, { ...options, headers });
    const json = await res.json().catch(() => ({ ok: false, error: "응답 파싱 오류" }));
    if (!res.ok || !json.ok) {
      throw new Error(json.error || `오류 (${res.status})`);
    }
    return json.data;
  }

  // =========================================================================
  // 3. 화면 라우팅 및 초기 볼트 점검
  // =========================================================================
  async function initVaultState() {
    try {
      const serverCfg = await apiFetch("/api/encrypted-config");
      if (serverCfg.exists && serverCfg.envelope) {
        state.envelope = serverCfg.envelope;
        localStorage.setItem(LOCAL_ENCRYPTED_BACKUP_KEY, JSON.stringify(serverCfg.envelope));
        state.hasVault = true;
      } else {
        const localBackup = localStorage.getItem(LOCAL_ENCRYPTED_BACKUP_KEY);
        if (localBackup) {
          state.envelope = JSON.parse(localBackup);
          state.hasVault = true;
          await persistEncryptedEnvelope(state.envelope).catch(() => {});
        } else {
          state.hasVault = false;
        }
      }
    } catch {
      state.hasVault = false;
    }

    const tabSession = sessionStorage.getItem(SESSION_UNLOCKED_CACHE_KEY);
    if (state.hasVault && tabSession) {
      try {
        const parsed = JSON.parse(tabSession);
        if (parsed && parsed.clientId) {
          state.decryptedCreds = parsed;
          state.unlocked = true;
        }
      } catch {
        sessionStorage.removeItem(SESSION_UNLOCKED_CACHE_KEY);
      }
    }

    routeScreen();
  }

  function routeScreen() {
    $("setupScreen").classList.add("hidden");
    $("pinScreen").classList.add("hidden");
    $("dashboardScreen").classList.add("hidden");

    if (state.unlocked || state.isLedgerOnlyPreview) {
      $("dashboardScreen").classList.remove("hidden");
      $("demoBanner").classList.toggle("hidden", !state.isLedgerOnlyPreview);
      $("syncApiBtn").classList.toggle("hidden", state.isLedgerOnlyPreview);
      $("lockVaultBtn").textContent = state.isLedgerOnlyPreview ? "API 설정으로" : "🔒 잠금";
      renderAccountSelector();
      loadPortfolio();
      return;
    }

    if (state.hasVault) {
      state.pinBuffer = "";
      updatePinDots();
      $("pinErrorMsg").textContent = "";
      $("pinScreen").classList.remove("hidden");
    } else {
      $("setupScreen").classList.remove("hidden");
    }
  }

  // =========================================================================
  // 4. 6자리 PIN 입력 패드 & 복호화
  // =========================================================================
  function updatePinDots() {
    const dots = document.querySelectorAll("#pinDots .pin-dot");
    dots.forEach((dot, idx) => {
      dot.classList.toggle("filled", idx < state.pinBuffer.length);
    });
  }

  async function handlePinDigit(digit) {
    if (Date.now() < state.lockedUntil) {
      const sec = Math.ceil((state.lockedUntil - Date.now()) / 1000);
      $("pinErrorMsg").textContent = `오류 횟수 초과로 잠겼습니다. ${sec}초 후 다시 시도해주세요.`;
      return;
    }
    if (state.pinBuffer.length >= 6) return;
    state.pinBuffer += digit;
    $("pinErrorMsg").textContent = "";
    updatePinDots();

    if (state.pinBuffer.length === 6) {
      const pinToTry = state.pinBuffer;
      try {
        const creds = await decryptCredentialsWithPin(pinToTry, state.envelope);
        state.decryptedCreds = creds;
        state.currentPin = pinToTry;
        state.unlocked = true;
        state.isLedgerOnlyPreview = false;
        state.failedPinCount = 0;
        state.pinBuffer = "";
        sessionStorage.setItem(SESSION_UNLOCKED_CACHE_KEY, JSON.stringify(creds));
        showToast("PIN 인증 완료! 포트폴리오 열람 잠금이 해제되었습니다.");
        routeScreen();
      } catch {
        state.failedPinCount += 1;
        if (state.failedPinCount >= 5) {
          state.lockedUntil = Date.now() + 20000;
          $("pinErrorMsg").textContent = "5회 연속 오류로 20초간 입력이 제한됩니다.";
        } else {
          $("pinErrorMsg").textContent = `PIN 번호가 일치하지 않습니다. (남은 시도: ${5 - state.failedPinCount}회)`;
        }
        const dotsWrap = $("pinDots");
        dotsWrap.classList.remove("shake");
        void dotsWrap.offsetWidth;
        dotsWrap.classList.add("shake");
        setTimeout(() => {
          state.pinBuffer = "";
          updatePinDots();
        }, 280);
      }
    }
  }

  function handlePinBackspace() {
    if (state.pinBuffer.length > 0) {
      state.pinBuffer = state.pinBuffer.slice(0, -1);
      updatePinDots();
    }
  }

  // =========================================================================
  // 5. 토스증권 Open API 증분 동기화 & 이후 배당 갱신
  // =========================================================================
  async function triggerTossSync(showSuccessToast = true) {
    if (!state.decryptedCreds) return;
    const btn = $("syncApiBtn");
    const divBtn = $("autoSyncDivBtn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "증분 동기화 중...";
    }
    if (divBtn) divBtn.disabled = true;

    try {
      const res = await apiFetch("/api/toss/sync", {
        method: "POST",
        body: JSON.stringify({
          clientId: state.decryptedCreds.clientId,
          clientSecret: state.decryptedCreds.clientSecret,
          baseUrl: state.decryptedCreds.baseUrl || "https://openapi.tossinvest.com",
          accessToken: state.decryptedCreds.accessToken || null,
          tokenExpiresAt: state.decryptedCreds.tokenExpiresAt || 0,
          accountSeq: state.decryptedCreds.selectedAccountSeq ?? null,
        }),
      });

      state.decryptedCreds.accounts = res.accounts || [];
      state.decryptedCreds.selectedAccountSeq = res.selectedAccountSeq;
      state.decryptedCreds.accessToken = res.accessToken;
      state.decryptedCreds.tokenExpiresAt = res.tokenExpiresAt;
      sessionStorage.setItem(SESSION_UNLOCKED_CACHE_KEY, JSON.stringify(state.decryptedCreds));

      if (state.currentPin) {
        const updatedEnv = await encryptCredentialsWithPin(state.currentPin, state.decryptedCreds);
        await persistEncryptedEnvelope(updatedEnv);
      }

      renderAccountSelector();
      await loadPortfolio();
      markNewSyncLogBadge();
      if (showSuccessToast) {
        const s = res.sync;
        const newDivCnt = s.newlyAddedDividendCount !== undefined ? s.newlyAddedDividendCount : (s.dividendsGenerated || 0);
        showToast(
          `증분 동기화 완료! 보유 ${s.holdingsCount}종목 · 신규 체결 ${s.newTradesSaved}건 · 신규 이후배당 ${newDivCnt}건 추가`,
          false,
          {
            actionLabel: "상세 로그 보기 →",
            onAction: () => openSyncLogModal("ALL"),
          }
        );
      }
    } catch (err) {
      markNewSyncLogBadge();
      showToast(err.message, true, {
        actionLabel: "오류 로그 보기",
        onAction: () => openSyncLogModal("ALL"),
      });
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "↻ 토스 API 증분 동기화";
      }
      if (divBtn) divBtn.disabled = false;
    }
  }

  async function triggerAutoDividendSync(forceRefresh = true) {
    const btn = $("autoSyncDivBtn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "💸 이후 배당 확인 중...";
    }
    try {
      const res = await apiFetch("/api/dividends/auto-sync", {
        method: "POST",
        body: JSON.stringify({
          accountKey: getActiveAccountKey(),
          forceRefresh,
        }),
      });
      await loadPortfolio();
      markNewSyncLogBadge();
      const cutoffMsg = res.cutoffDate ? `(${res.cutoffDate} 이후)` : "";
      const newCnt = res.newlyAddedCount !== undefined ? res.newlyAddedCount : (res.dividendsGenerated || 0);
      const msg =
        newCnt > 0
          ? `이후 배당 갱신 완료! ${cutoffMsg} 신규 배당 ${newCnt}건 추가됨`
          : `이후 배당 점검 완료! ${cutoffMsg} 새로 추가된 배당 0건 (기존 자동배당 ${res.dividendsGenerated || 0}건 유지)`;
      showToast(msg, false, {
        actionLabel: "상세 로그 보기 →",
        onAction: () => openSyncLogModal("DIV_SYNC"),
      });
    } catch (err) {
      markNewSyncLogBadge();
      showToast(err.message, true, {
        actionLabel: "오류 로그 보기",
        onAction: () => openSyncLogModal("DIV_SYNC"),
      });
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "💸 이후 배당 갱신";
      }
    }
  }

  async function triggerRefreshPrices() {
    const btn = $("refreshPricesBtn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⚡ 시세 갱신 중...";
    }
    try {
      const payload = {
        accountKey: getActiveAccountKey(),
      };
      if (state.decryptedCreds && state.decryptedCreds.clientId && state.decryptedCreds.clientSecret) {
        payload.clientId = state.decryptedCreds.clientId;
        payload.clientSecret = state.decryptedCreds.clientSecret;
        payload.baseUrl = state.decryptedCreds.baseUrl || "https://openapi.tossinvest.com";
        payload.accessToken = state.decryptedCreds.accessToken || null;
        payload.tokenExpiresAt = state.decryptedCreds.tokenExpiresAt || 0;
        payload.accountSeq = state.decryptedCreds.selectedAccountSeq ?? null;
      }
      const res = await apiFetch("/api/prices/refresh", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (state.decryptedCreds && res.accessToken) {
        state.decryptedCreds.accessToken = res.accessToken;
        state.decryptedCreds.tokenExpiresAt = res.tokenExpiresAt;
        sessionStorage.setItem(SESSION_UNLOCKED_CACHE_KEY, JSON.stringify(state.decryptedCreds));
      }
      await loadPortfolio();
      const srcLabel = res.source === "TOSS_API" ? "토스 실시간 시세" : res.source === "HYBRID" ? "토스+시장 실시간 시세" : "시장 실시간 시세";
      showToast(
        `현재가 새로고침 완료! (${srcLabel} · 보유 ${res.updatedCount}/${res.heldCount}종목 현재가 및 수익률 재계산 반영)`
      );
    } catch (err) {
      showToast(err.message, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⚡ 현재가 새로고침";
      }
    }
  }

  async function loadPortfolio() {
    try {
      const qs = `?accountKey=${encodeURIComponent(getActiveAccountKey())}`;
      const data = await apiFetch(`/api/portfolio${qs}`);
      state.portfolio = data;
      renderDashboard();
      if (state.activeDetailSymbol) {
        renderDetailDrawer(state.activeDetailSymbol);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderAccountSelector() {
    const sel = $("accountSelector");
    const accs = state.decryptedCreds?.accounts || [];
    if (state.isLedgerOnlyPreview || accs.length === 0) {
      sel.classList.add("hidden");
      return;
    }
    sel.classList.remove("hidden");
    sel.innerHTML = accs
      .map(
        (acc) =>
          `<option value="${escapeHtml(acc.accountSeq)}" ${
            String(acc.accountSeq) === String(state.decryptedCreds.selectedAccountSeq) ? "selected" : ""
          }>${escapeHtml(acc.name)}</option>`
      )
      .join("");
  }

  function renderDashboard() {
    if (!state.portfolio) return;
    const { summary, items, lastSync, lastPriceRefresh, pdfCutoffDate } = state.portfolio;
    const exTax = state.excludeTax;
    const usInKrw = state.usDivInKrw;

    $("excludeTaxToggle").checked = exTax;
    const badge = $("taxModeBadge");
    badge.textContent = exTax ? "세후 기준 ON (기본)" : "세전 기준 OFF";
    badge.classList.toggle("pre-tax", !exTax);

    $("taxModeDesc").textContent = exTax
      ? "배당소득세(외화세금환급·원화징수 재정산 반영)와 매수·매도 제세금·수수료를 모두 제외한 실질 순수익을 표시합니다."
      : "세금(배당소득세·거래세)을 차감하기 전의 세전 수익 기준으로 표시합니다.";

    document.querySelectorAll("#usDivCurrencyGroup .seg-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.usdivcur === (usInKrw ? "KRW" : "USD"));
    });

    const priceTag = lastPriceRefresh ? ` · 현재가 갱신: ${lastPriceRefresh}` : "";
    const bannerEl = $("ledgerBannerText");
    if (pdfCutoffDate) {
      $("syncStatusText").textContent = lastSync
        ? `PDF 확정 원장: ~${pdfCutoffDate} · 최근 동기화: ${lastSync}${priceTag}`
        : `PDF 확정 원장: ~${pdfCutoffDate}${priceTag || " · 이후 내역 증분 동기화 대기 중"}`;
      if (bannerEl) {
        bannerEl.innerHTML = `💡 <strong>PDF 확정 원장 + 이후 증분 동기화:</strong> <code>~ ${escapeHtml(pdfCutoffDate)}</code> 기간은 토스증권 거래내역서 PDF의 실제 체결·타사대체입고·실수령 배당금·세금재정산 내역으로 확정 보관되며, 이후 신규 체결·배당은 <strong>토스 API 증분 동기화</strong>로 중복 없이 이어서 저장됩니다.`;
      }
    } else if (lastSync || lastPriceRefresh) {
      $("syncStatusText").textContent = `${lastSync ? `마지막 동기화: ${lastSync}` : ""}${priceTag}`;
      if (bannerEl) {
        bannerEl.innerHTML = `💡 <strong>과거 전체 매매·배당 원장 안내:</strong> 토스 앱에서 <strong>거래내역서 PDF</strong>를 발급받아 등록하시면 과거 전체 매수·매도 단가와 실제 입금 배당금(입금환율·세금재정산 포함)이 빠짐없이 합산됩니다.`;
      }
    } else {
      $("syncStatusText").textContent = "먼저 '📄 PDF 등록 · 관리'에서 토스증권 거래내역서 PDF를 올리거나 API 동기화를 실행하세요.";
      if (bannerEl) {
        bannerEl.innerHTML = `📌 <strong>시작 안내:</strong> 토스 앱에서 <strong>거래내역서를 PDF 파일로 발급</strong>받아 우측 버튼으로 업로드하면 과거 모든 매수·매도·실수령 배당 내역이 자동 구축됩니다.`;
      }
    }

    $("thCapitalPnl").textContent = exTax ? "매매 순손익 (세후)" : "매매 손익 (세전)";
    $("thDividend").textContent = usInKrw
      ? exTax
        ? "누적 배당금 (세후 · 원화환산)"
        : "누적 배당금 (세전 · 원화환산)"
      : exTax
      ? "누적 배당금 (세후)"
      : "누적 배당금 (세전)";
    $("thTotalReturn").textContent = exTax ? "합산 총수익 (세후)" : "합산 총수익 (세전)";

    const krwSum = summary.KRW || {};
    const usdSum = summary.USD || {};
    $("krwTotalTaxText").textContent = formatMoney(krwSum.totalTax || 0, "KRW");
    $("usdTotalTaxText").textContent = formatMoney(usdSum.totalTax || 0, "USD");

    const krwDivPart = exTax ? (krwSum.dividendNetKrw ?? krwSum.dividendNet ?? 0) : (krwSum.dividendGrossKrw ?? krwSum.dividendGross ?? 0);
    const usdDivKrwPart = exTax ? (usdSum.dividendNetKrw || 0) : (usdSum.dividendGrossKrw || 0);
    const combinedKrwDiv = krwDivPart + usdDivKrwPart;
    const combChip = $("combinedKrwDivChip");
    if (combChip) {
      combChip.classList.toggle("hidden", !(usInKrw || state.showCombinedKrwChart));
      $("combinedKrwDivText").textContent = formatMoney(combinedKrwDiv, "KRW", combinedKrwDiv > 0);
    }

    renderCurrencyCard("krw", krwSum, "KRW", exTax);
    renderCurrencyCard("usd", usdSum, "USD", exTax);
    renderDividendGrowthSection(items, exTax);
    renderStockTable(items, exTax);
  }

  function renderCurrencyCard(prefix, sumObj, cur, exTax) {
    const totalRet = exTax ? sumObj.totalReturnNet : sumObj.totalReturnGross;
    const totalRate = exTax ? sumObj.returnRateNet : sumObj.returnRateGross;
    const capPnl = exTax ? sumObj.capitalPnlNet : sumObj.capitalPnlGross;
    const unrealPnl = exTax ? sumObj.unrealizedPnlNet : sumObj.unrealizedPnlGross;
    const realPnl = exTax ? sumObj.realizedPnlNet : sumObj.realizedPnlGross;
    const divAmt = exTax ? sumObj.dividendNet : sumObj.dividendGross;

    $(`${prefix}StockCountTag`).textContent = `보유 ${sumObj.heldStocks || 0}종목 · 매도완료 ${sumObj.soldStocks || 0}종목`;
    $(`${prefix}HeroLabel`).textContent = exTax
      ? "매매 + 배당 합산 순수익 (세금 제외)"
      : "매매 + 배당 합산 총수익 (세전)";

    const retEl = $(`${prefix}TotalReturnVal`);
    retEl.textContent = formatMoney(totalRet, cur, true);
    retEl.className = `hero-return-amount ${pnlClass(totalRet)}`;

    const rateEl = $(`${prefix}TotalReturnRate`);
    rateEl.textContent = formatRate(totalRate);
    rateEl.className = `hero-return-rate ${rateBadgeClass(totalRet)}`;

    const capEl = $(`${prefix}CapitalPnlVal`);
    capEl.textContent = formatMoney(capPnl, cur, true);
    capEl.className = `sub-metric-val ${pnlClass(capPnl)}`;
    $(`${prefix}CapitalPnlSub`).textContent = `평가 ${formatMoney(unrealPnl, cur, true)} · 실현 ${formatMoney(realPnl, cur, true)}`;

    if (cur === "USD" && state.usDivInKrw) {
      const divKrw = exTax ? (sumObj.dividendNetKrw || 0) : (sumObj.dividendGrossKrw || 0);
      const yearlyKrw = exTax ? (sumObj.priorYearlyDividendNetKrw || 0) : (sumObj.priorYearlyDividendGrossKrw || 0);
      const yearlyNoteKrw = yearlyKrw > 0 ? ` · 연도별합산 ${formatMoney(yearlyKrw, "KRW")} 포함` : "";
      const avgFx = Number(sumObj.avgDividendFxRate) || 0;
      const fxTxt = avgFx > 1 ? ` · 평균환율 ${avgFx.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}원` : "";
      $(`${prefix}DividendVal`).textContent = formatMoney(divKrw, "KRW", divKrw > 0);
      $(`${prefix}DividendSub`).textContent =
        `입금시점 확정원화 (원래 ${formatMoney(divAmt, "USD", divAmt > 0)}${fxTxt} · 순세금 ${formatMoney(sumObj.dividendTaxKrw || 0, "KRW")})` +
        yearlyNoteKrw;
    } else {
      const yearlyPart = exTax ? (sumObj.priorYearlyDividendNet || 0) : (sumObj.priorYearlyDividendGross || 0);
      const yearlyNote = yearlyPart > 0 ? ` · 연도별합산 ${formatMoney(yearlyPart, cur)} 포함` : "";
      $(`${prefix}DividendVal`).textContent = formatMoney(divAmt, cur, divAmt > 0);
      $(`${prefix}DividendSub`).textContent =
        (exTax
          ? `세전 ${formatMoney(sumObj.dividendGross, cur)} (순세금 ${formatMoney(sumObj.dividendTax, cur)} 차감)`
          : `원천징수 순세금 ${formatMoney(sumObj.dividendTax, cur)} 포함`) + yearlyNote;
    }

    $(`${prefix}EvalVal`).textContent = formatMoney(sumObj.evalAmount, cur);
    $(`${prefix}InvestedSub`).textContent = `보유 매입원금 ${formatMoney(sumObj.currentInvested, cur)}`;

    $(`${prefix}TaxFeeVal`).textContent = formatMoney((sumObj.totalTax || 0) + (sumObj.totalCommission || 0), cur);
    $(`${prefix}TaxFeeSub`).textContent = `세금 ${formatMoney(sumObj.totalTax, cur)} · 수수료 ${formatMoney(sumObj.totalCommission, cur)}`;
  }

  // =========================================================================
  // 5-B. 연도별 국내·미국·원화합산 배당 성장 그래프 렌더링
  // =========================================================================
  function formatCompactChartAmount(val, cur) {
    const n = Number(val) || 0;
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    if (cur === "KRW") {
      if (abs >= 100000000) return `${sign}${(abs / 100000000).toFixed(2).replace(/\.00$/, "")}억`;
      if (abs >= 100000) return `${sign}${Math.round(abs / 10000).toLocaleString("ko-KR")}만`;
      if (abs >= 10000) return `${sign}${(abs / 10000).toFixed(1).replace(/\.0$/, "")}만`;
      return `${sign}${Math.round(abs).toLocaleString("ko-KR")}원`;
    }
    if (abs >= 100000) return `${sign}$${(abs / 1000).toFixed(0)}k`;
    if (abs >= 1000) return `${sign}$${Math.round(abs).toLocaleString("en-US")}`;
    return `${sign}$${abs.toFixed(abs >= 100 ? 0 : 2)}`;
  }

  function buildYearlyDividendSeries(items, exTax, includeStockPrior, usInKrw) {
    const rawBySlot = { KRW: new Map(), USD: new Map(), TOTAL_KRW: new Map() };
    let hasAnyStockPrior = false;
    const pdfCutoff = state.portfolio?.pdfCutoffDate || "";
    const cutoffYear = pdfCutoff ? parseInt(pdfCutoff.slice(0, 4), 10) : 0;
    const cutoffMonth = pdfCutoff ? parseInt(pdfCutoff.slice(5, 7), 10) : 12;
    const nowYear = new Date().getFullYear();

    const ensureYearBucket = (slotMap, yr) => {
      if (!slotMap.has(yr)) {
        slotMap.set(yr, {
          year: yr,
          tossVal: 0,
          priorVal: 0,
          krPartVal: 0,
          usPartVal: 0,
          origUsdVal: 0,
          krwEquivVal: 0,
          totalVal: 0,
          taxVal: 0,
          count: 0,
          maxMonth: 1,
        });
      }
      return slotMap.get(yr);
    };

    (items || []).forEach((it) => {
      const cur = it.currency === "KRW" ? "KRW" : "USD";
      (it.dividends || []).forEach((d) => {
        const src = d.source || "";
        if (src === "PRIOR_BROKER") {
          hasAnyStockPrior = true;
          if (!includeStockPrior) return;
        }
        const dtStr = String(d.pay_date || "");
        if (dtStr.length < 4) return;
        const yr = parseInt(dtStr.slice(0, 4), 10);
        if (isNaN(yr) || yr < 1990 || yr > 2100) return;
        const mo = dtStr.length >= 7 ? parseInt(dtStr.slice(5, 7), 10) || 12 : 12;

        const isPrior = src === "PRIOR_YEARLY" || src === "PRIOR_BROKER" || src === "EXT_DIV";
        const isPayout = (Number(d.gross_amount) || 0) > 0 || src === "PRIOR_YEARLY" || src === "PRIOR_BROKER";
        const nativeVal = Number(exTax ? d.net_amount : d.gross_amount) || 0;
        const nativeTax = Number(d.tax_amount) || 0;
        const krwVal = Number(exTax ? d.krw_net : d.krw_gross) || 0;
        const krwTax = Number(d.krw_tax) || 0;

        if (cur === "KRW") {
          const b = ensureYearBucket(rawBySlot.KRW, yr);
          if (isPrior) b.priorVal += nativeVal;
          else b.tossVal += nativeVal;
          b.totalVal += nativeVal;
          b.taxVal += nativeTax;
          if (isPayout) b.count += 1;
          if (mo > b.maxMonth) b.maxMonth = mo;
        } else {
          const b = ensureYearBucket(rawBySlot.USD, yr);
          const dispVal = usInKrw ? krwVal : nativeVal;
          const dispTax = usInKrw ? krwTax : nativeTax;
          if (isPrior) b.priorVal += dispVal;
          else b.tossVal += dispVal;
          b.totalVal += dispVal;
          b.origUsdVal += nativeVal;
          b.krwEquivVal += krwVal;
          b.taxVal += dispTax;
          if (isPayout) b.count += 1;
          if (mo > b.maxMonth) b.maxMonth = mo;
        }

        const tb = ensureYearBucket(rawBySlot.TOTAL_KRW, yr);
        if (isPrior) {
          tb.priorVal += krwVal;
        } else if (cur === "KRW") {
          tb.krPartVal += krwVal;
          tb.tossVal += krwVal;
        } else {
          tb.usPartVal += krwVal;
          tb.tossVal += krwVal;
        }
        if (cur === "USD") tb.origUsdVal += nativeVal;
        tb.totalVal += krwVal;
        tb.taxVal += krwTax;
        if (isPayout) tb.count += 1;
        if (mo > tb.maxMonth) tb.maxMonth = mo;
      });
    });

    const buildContinuous = (slotKey, dispCur) => {
      const m = rawBySlot[slotKey];
      const activeYrs = Array.from(m.keys())
        .filter((y) => Math.abs(m.get(y).totalVal) > 0.0001)
        .sort((a, b) => a - b);
      if (activeYrs.length === 0) return [];
      const minY = activeYrs[0];
      const maxY = activeYrs[activeYrs.length - 1];
      const minMeaningful = dispCur === "KRW" ? 10000 : 10;

      const list = [];
      for (let y = minY; y <= maxY; y++) {
        const rec = m.get(y) || {
          year: y,
          tossVal: 0,
          priorVal: 0,
          krPartVal: 0,
          usPartVal: 0,
          origUsdVal: 0,
          krwEquivVal: 0,
          totalVal: 0,
          taxVal: 0,
          count: 0,
          maxMonth: 12,
        };
        const isPartial =
          (y === nowYear && new Date().getMonth() < 11) ||
          (y === cutoffYear && cutoffMonth < 12 && y >= nowYear);
        const elapsedMonths = isPartial ? Math.max(1, cutoffYear === y ? cutoffMonth : new Date().getMonth() + 1) : 12;
        list.push({
          ...rec,
          isPartial,
          elapsedMonths,
          monthlyAvg: rec.totalVal / elapsedMonths,
          yoyDiff: null,
          yoyPct: null,
          yoyBadge: "",
        });
      }

      for (let i = 0; i < list.length; i++) {
        const curRow = list[i];
        if (i === 0) {
          curRow.yoyBadge = curRow.totalVal > 0 ? "첫해" : "-";
          continue;
        }
        const prevVal = list[i - 1].totalVal;
        curRow.yoyDiff = curRow.totalVal - prevVal;
        if (prevVal <= 0) {
          curRow.yoyBadge = curRow.totalVal > 0 ? "첫해" : "-";
        } else if (prevVal < minMeaningful && curRow.totalVal >= minMeaningful) {
          curRow.yoyBadge = "본격시작";
        } else {
          const pct = ((curRow.totalVal - prevVal) / prevVal) * 100;
          curRow.yoyPct = pct;
          curRow.yoyBadge = `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
        }
      }
      return list;
    };

    return {
      KRW: buildContinuous("KRW", "KRW"),
      USD: buildContinuous("USD", usInKrw ? "KRW" : "USD"),
      TOTAL_KRW: buildContinuous("TOTAL_KRW", "KRW"),
      hasAnyStockPrior,
    };
  }

  function computeCagrInfo(series, dispCur) {
    const minMeaningful = dispCur === "KRW" ? 10000 : 10;
    const meaningful = series.filter((s) => s.totalVal >= minMeaningful);
    if (meaningful.length < 2) return null;
    const start = meaningful[0];
    let end = meaningful[meaningful.length - 1];
    if (end.isPartial && meaningful.length >= 3) {
      const prevFull = meaningful[meaningful.length - 2];
      if (end.totalVal < prevFull.totalVal) {
        end = prevFull;
      }
    }
    const yearsDiff = end.year - start.year;
    if (yearsDiff <= 0 || start.totalVal <= 0 || end.totalVal <= 0) return null;
    const cagr = (Math.pow(end.totalVal / start.totalVal, 1 / yearsDiff) - 1) * 100;
    return {
      startYear: start.year,
      endYear: end.year,
      cagr,
    };
  }

  function getSlotPrefix(slotKey) {
    if (slotKey === "KRW") return "krw";
    if (slotKey === "TOTAL_KRW") return "totalKrw";
    return "usd";
  }

  function renderSingleGrowthCard(slotKey, dispCur, series, exTax) {
    const prefix = getSlotPrefix(slotKey);
    const svgWrap = $(`${prefix}GrowthSvgWrap`);
    const metaSub = $(`${prefix}GrowthMetaSub`);
    const cagrBadge = $(`${prefix}GrowthCagrBadge`);
    const inspector = $(`${prefix}GrowthInspector`);
    if (!svgWrap || !metaSub || !cagrBadge || !inspector) return;

    if (!series || series.length === 0) {
      metaSub.textContent = "집계된 배당 내역 없음";
      cagrBadge.textContent = "데이터 없음";
      cagrBadge.className = "badge badge-sold";
      svgWrap.innerHTML = `<div class="empty-state" style="padding:36px 12px;font-size:13px;">아직 연도별 배당 기록이 없습니다.</div>`;
      inspector.textContent = "배당 데이터가 추가되면 연도별 성장 그래프가 자동으로 표시됩니다.";
      return;
    }

    const peak = series.reduce((best, r) => (r.totalVal > best.totalVal ? r : best), series[0]);
    const totalAllYears = series.reduce((acc, r) => acc + r.totalVal, 0);
    metaSub.textContent = `${series[0].year}~${series[series.length - 1].year}년 누적 ${formatMoney(totalAllYears, dispCur)} · 최고 ${peak.year}년 (${formatMoney(peak.totalVal, dispCur)})`;

    const cagrInfo = computeCagrInfo(series, dispCur);
    if (cagrInfo) {
      const sign = cagrInfo.cagr >= 0 ? "+" : "";
      cagrBadge.textContent = `${cagrInfo.startYear}→${cagrInfo.endYear} 연평균 ${sign}${cagrInfo.cagr.toFixed(1)}%`;
      cagrBadge.className = `badge ${cagrInfo.cagr >= 0 ? "badge-auto" : "badge-sell"}`;
    } else {
      cagrBadge.textContent = `${series.length}개년 집계`;
      cagrBadge.className = "badge badge-held";
    }

    if (
      !state.selectedGrowthYear[slotKey] ||
      !series.some((s) => s.year === state.selectedGrowthYear[slotKey])
    ) {
      state.selectedGrowthYear[slotKey] = series[series.length - 1].year;
    }
    const selYr = state.selectedGrowthYear[slotKey];

    const W = slotKey === "TOTAL_KRW" ? 760 : 560;
    const H = 228;
    const padL = 14;
    const padR = 14;
    const padTop = 44;
    const padBot = 40;
    const plotW = W - padL - padR;
    const plotH = H - padTop - padBot;
    const baseY = padTop + plotH;

    const maxVal = Math.max(...series.map((s) => s.totalVal), 1);
    const n = series.length;
    const slotW = plotW / n;
    const barW = Math.min(54, Math.max(20, slotW * 0.52));
    const mainColor = slotKey === "KRW" ? "#00a86b" : "#3182f6";
    const priorColor = "#8b5cf6";

    const gridLines = [0, 0.5, 1]
      .map((ratio) => {
        const gy = baseY - plotH * ratio;
        return `<line x1="${padL}" y1="${gy.toFixed(1)}" x2="${W - padR}" y2="${gy.toFixed(1)}" stroke="#e5e8eb" stroke-width="1" ${ratio > 0 ? 'stroke-dasharray="3 3"' : ""} />`;
      })
      .join("");

    const points = [];
    const barGroups = series
      .map((row, idx) => {
        const cx = padL + slotW * idx + slotW / 2;
        const bx = cx - barW / 2;
        const totH = row.totalVal > 0 ? Math.max(3, (row.totalVal / maxVal) * plotH) : 0;
        const topY = baseY - totH;
        points.push({ cx, topY, row });

        const isSel = row.year === selYr;
        const yoyColor =
          row.yoyPct === null
            ? "#6b7684"
            : row.yoyPct >= 0
            ? "#f04452"
            : "#3182f6";

        let rectsHtml = "";
        if (totH > 0) {
          if (slotKey === "TOTAL_KRW") {
            const posSum = Math.max(0, row.krPartVal) + Math.max(0, row.usPartVal) + Math.max(0, row.priorVal);
            const krH = posSum > 0 ? totH * (Math.max(0, row.krPartVal) / posSum) : 0;
            const usH = posSum > 0 ? totH * (Math.max(0, row.usPartVal) / posSum) : 0;
            const prH = Math.max(0, totH - krH - usH);
            let curBottom = baseY;
            if (krH > 0.4) {
              rectsHtml += `<rect x="${bx.toFixed(1)}" y="${(curBottom - krH).toFixed(1)}" width="${barW.toFixed(1)}" height="${krH.toFixed(1)}" rx="5" fill="#00a86b" opacity="0.95" />`;
              curBottom -= krH;
            }
            if (usH > 0.4) {
              rectsHtml += `<rect x="${bx.toFixed(1)}" y="${(curBottom - usH).toFixed(1)}" width="${barW.toFixed(1)}" height="${usH.toFixed(1)}" rx="5" fill="#3182f6" opacity="0.93" />`;
              curBottom -= usH;
            }
            if (prH > 0.4) {
              rectsHtml += `<rect x="${bx.toFixed(1)}" y="${topY.toFixed(1)}" width="${barW.toFixed(1)}" height="${prH.toFixed(1)}" rx="5" fill="${priorColor}" opacity="0.92" />`;
            }
          } else {
            const tossRatio = row.totalVal > 0 ? Math.max(0, Math.min(1, row.tossVal / row.totalVal)) : 0;
            const tossH = totH * tossRatio;
            const priorH = Math.max(0, totH - tossH);
            if (priorH > 0.5 && tossH > 0.5) {
              rectsHtml = `
                <rect x="${bx.toFixed(1)}" y="${(baseY - tossH).toFixed(1)}" width="${barW.toFixed(1)}" height="${tossH.toFixed(1)}" rx="5" fill="${mainColor}" opacity="${row.isPartial ? "0.78" : "0.95"}" />
                <rect x="${bx.toFixed(1)}" y="${topY.toFixed(1)}" width="${barW.toFixed(1)}" height="${priorH.toFixed(1)}" rx="5" fill="${priorColor}" opacity="0.92" />
              `;
            } else {
              const fillCol = priorH > tossH ? priorColor : mainColor;
              rectsHtml = `<rect x="${bx.toFixed(1)}" y="${topY.toFixed(1)}" width="${barW.toFixed(1)}" height="${totH.toFixed(1)}" rx="6" fill="${fillCol}" opacity="${row.isPartial ? "0.76" : "0.95"}" />`;
            }
          }
        }

        const amtTxt = formatCompactChartAmount(row.totalVal, dispCur);
        return `
          <g class="growth-bar-group ${isSel ? "selected" : ""}" data-growth-cur="${slotKey}" data-growth-yr="${row.year}">
            <rect class="bar-hit" x="${(padL + slotW * idx + 2).toFixed(1)}" y="6" width="${Math.max(10, slotW - 4).toFixed(1)}" height="${H - 10}" rx="8" />
            ${rectsHtml}
            <text x="${cx.toFixed(1)}" y="${(topY - 19).toFixed(1)}" text-anchor="middle" font-size="11" font-weight="800" fill="${yoyColor}">${escapeHtml(row.yoyBadge)}</text>
            <text x="${cx.toFixed(1)}" y="${(topY - 6).toFixed(1)}" text-anchor="middle" font-size="11.5" font-weight="800" fill="#191f28">${escapeHtml(amtTxt)}</text>
            <text x="${cx.toFixed(1)}" y="${baseY + 17}" text-anchor="middle" font-size="12" font-weight="${isSel ? "800" : "700"}" fill="${isSel ? "#191f28" : "#4e5968"}">${row.year}</text>
            ${
              row.isPartial
                ? `<text x="${cx.toFixed(1)}" y="${baseY + 30}" text-anchor="middle" font-size="10" font-weight="700" fill="#8b95a1">진행중</text>`
                : ""
            }
          </g>
        `;
      })
      .join("");

    const polyPts = points.map((p) => `${p.cx.toFixed(1)},${p.topY.toFixed(1)}`).join(" ");
    const trendLine =
      points.length >= 2
        ? `<polyline points="${polyPts}" fill="none" stroke="#f04452" stroke-width="2" stroke-opacity="0.72" stroke-linecap="round" stroke-linejoin="round" pointer-events="none" />` +
          points
            .map(
              (p) =>
                `<circle cx="${p.cx.toFixed(1)}" cy="${p.topY.toFixed(1)}" r="3.3" fill="#ffffff" stroke="#f04452" stroke-width="2" pointer-events="none" />`
            )
            .join("")
        : "";

    svgWrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${slotKey} 연도별 배당 성장 그래프">${gridLines}${barGroups}${trendLine}</svg>`;
    renderGrowthInspector(slotKey, dispCur, selYr, exTax);
  }

  function renderGrowthInspector(slotKey, dispCur, year, exTax) {
    const series = state.lastGrowthSeries[slotKey] || [];
    const row = series.find((s) => s.year === Number(year)) || series[series.length - 1];
    const el = $(`${getSlotPrefix(slotKey)}GrowthInspector`);
    if (!row || !el) return;

    let yoyHtml = `<span style="color:var(--text-muted);">비교 기준 첫해</span>`;
    if (row.yoyDiff !== null) {
      const cls = pnlClass(row.yoyDiff);
      const pctPart = row.yoyPct !== null ? ` (${row.yoyPct >= 0 ? "+" : ""}${row.yoyPct.toFixed(1)}%)` : ` (${row.yoyBadge})`;
      yoyHtml = `전년 대비 <strong class="${cls}">${formatMoney(row.yoyDiff, dispCur, true)}${escapeHtml(pctPart)}</strong>`;
    }

    let detailSub = "";
    if (slotKey === "TOTAL_KRW") {
      const priorTxt = row.priorVal > 0 ? ` + 타사 ${formatMoney(row.priorVal, "KRW")}` : "";
      detailSub = ` <span style="color:var(--text-secondary);">(국내 ${formatMoney(row.krPartVal, "KRW")} + 미국원화 ${formatMoney(row.usPartVal, "KRW")}${priorTxt})</span>`;
    } else {
      if (slotKey === "USD" && state.usDivInKrw && row.origUsdVal !== 0) {
        detailSub += ` <span style="color:var(--text-secondary);">(원래 ${formatMoney(row.origUsdVal, "USD")})</span>`;
      }
      if (row.priorVal > 0) {
        detailSub += ` <span style="color:var(--prior-purple);">(토스 ${formatMoney(row.tossVal, dispCur)} + 타사 ${formatMoney(row.priorVal, dispCur)})</span>`;
      }
    }
    const moLabel = row.isPartial ? `월평균(${row.elapsedMonths}개월 기준)` : "월평균";

    el.innerHTML = `
      <div>
        <strong style="color:var(--text-primary);">${row.year}년${row.isPartial ? " (진행중)" : ""}:</strong>
        <strong class="val-div" style="margin-left:4px;">${formatMoney(row.totalVal, dispCur)}</strong>${detailSub}
      </div>
      <div>
        ${moLabel} <strong>${formatMoney(row.monthlyAvg, dispCur)}</strong> · ${yoyHtml} · ${row.count}건
      </div>
    `;
  }

  function renderDividendGrowthSection(items, exTax) {
    const usInKrw = state.usDivInKrw;
    const usDispCur = usInKrw ? "KRW" : "USD";

    $("growthTaxBasisBadge").textContent = exTax ? "세후 실수령 기준" : "세전 총액 기준";
    $("growthTaxBasisBadge").className = `badge ${exTax ? "badge-auto" : "badge-transfer"}`;

    const usdTitleEl = $("usdGrowthCardTitle");
    if (usdTitleEl) {
      usdTitleEl.textContent = usInKrw
        ? "🇺🇸 미국 배당금 연도별 성장 (원화 환산 · 입금당시 확정환율)"
        : "🇺🇸 미국 배당금 연도별 성장 (달러 USD)";
    }

    const combCard = $("combinedKrwGrowthCard");
    const combBtn = $("toggleCombinedKrwChartBtn");
    if (combCard && combBtn) {
      combCard.classList.toggle("hidden", !state.showCombinedKrwChart);
      combBtn.textContent = state.showCombinedKrwChart
        ? "🇰🇷+🇺🇸 원화 합산 그래프 닫기"
        : "🇰🇷+🇺🇸 원화 합산 그래프 보기";
    }

    const built = buildYearlyDividendSeries(items, exTax, state.growthIncludeStockPrior, usInKrw);
    state.lastGrowthSeries = { KRW: built.KRW, USD: built.USD, TOTAL_KRW: built.TOTAL_KRW };

    $("growthIncludePriorStockWrap").classList.toggle("hidden", !built.hasAnyStockPrior);
    $("growthIncludePriorStockChk").checked = state.growthIncludeStockPrior;

    renderSingleGrowthCard("KRW", "KRW", built.KRW, exTax);
    renderSingleGrowthCard("USD", usDispCur, built.USD, exTax);
    if (state.showCombinedKrwChart) {
      renderSingleGrowthCard("TOTAL_KRW", "KRW", built.TOTAL_KRW, exTax);
    }

    const usTh = document.querySelector("#growthComparisonTableWrap thead th:nth-child(5)");
    if (usTh) {
      usTh.textContent = usInKrw ? "🇺🇸 미국 연간 배당금 (원화 · 입금환율)" : "🇺🇸 미국 연간 배당금 (USD)";
    }

    const allYrs = Array.from(
      new Set([...built.KRW.map((x) => x.year), ...built.USD.map((x) => x.year)])
    ).sort((a, b) => b - a);
    const krMap = new Map(built.KRW.map((x) => [x.year, x]));
    const usMap = new Map(built.USD.map((x) => [x.year, x]));

    const compTbody = $("growthComparisonTbody");
    if (allYrs.length === 0) {
      compTbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="padding:24px;">집계된 연도별 배당 데이터가 없습니다.</td></tr>`;
    } else {
      compTbody.innerHTML = allYrs
        .map((y) => {
          const k = krMap.get(y);
          const u = usMap.get(y);
          const isPart = (k && k.isPartial) || (u && u.isPartial);
          const kYoy =
            k && k.yoyDiff !== null
              ? `<span class="${pnlClass(k.yoyDiff)}" style="font-weight:700;">${formatMoney(k.yoyDiff, "KRW", true)} (${escapeHtml(k.yoyBadge)})</span>`
              : `<span style="color:var(--text-muted);">${k ? escapeHtml(k.yoyBadge) : "-"}</span>`;
          const uYoy =
            u && u.yoyDiff !== null
              ? `<span class="${pnlClass(u.yoyDiff)}" style="font-weight:700;">${formatMoney(u.yoyDiff, usDispCur, true)} (${escapeHtml(u.yoyBadge)})</span>`
              : `<span style="color:var(--text-muted);">${u ? escapeHtml(u.yoyBadge) : "-"}</span>`;
          const uValHtml = u
            ? usInKrw
              ? `${formatMoney(u.totalVal, "KRW")}<div class="cell-sub">${formatMoney(u.origUsdVal, "USD")}</div>`
              : formatMoney(u.totalVal, "USD")
            : "-";
          return `
            <tr>
              <td style="padding-left:16px;font-weight:800;">${y}년${isPart ? ' <span class="badge badge-sold">진행중</span>' : ""}</td>
              <td class="val-div" style="font-weight:800;">${k ? formatMoney(k.totalVal, "KRW") : "-"}</td>
              <td>${kYoy}</td>
              <td>${k ? formatMoney(k.monthlyAvg, "KRW") : "-"}</td>
              <td class="val-div" style="font-weight:800;">${uValHtml}</td>
              <td>${uYoy}</td>
              <td style="padding-right:16px;">${u ? formatMoney(u.monthlyAvg, usDispCur) : "-"}</td>
            </tr>
          `;
        })
        .join("");
    }
  }

  function renderStockTable(items, exTax) {
    const tbody = $("stockTableBody");
    const q = state.searchQuery.trim().toLowerCase();
    const usInKrw = state.usDivInKrw;

    let filtered = (items || []).filter((it) => {
      if (state.statusFilter === "HELD" && it.status !== "HELD") return false;
      if (state.statusFilter === "SOLD" && it.status !== "SOLD") return false;
      if (state.statusFilter === "TRANSFER" && !it.hasTransferIn && !it.priorBrokerDiv && !it.hasExtDiv && !it.isPriorYearlyBucket) return false;
      if (state.marketFilter !== "ALL" && it.market !== state.marketFilter) return false;
      if (q) {
        const matchPriorTag = (it.hasExtDiv || it.symbol === "PRIOR_YEARLY_US") && "타사배당합산".includes(q.replace(/[\[\]\s]/g, ""));
        if (
          !matchPriorTag &&
          !it.name.toLowerCase().includes(q) &&
          !it.symbol.toLowerCase().includes(q) &&
          !(it.isin || "").toLowerCase().includes(q)
        ) {
          return false;
        }
      }
      return true;
    });

    filtered.sort((a, b) => {
      const aRet = exTax ? a.totalReturnNet : a.totalReturnGross;
      const bRet = exTax ? b.totalReturnNet : b.totalReturnGross;
      const aDiv = usInKrw && a.currency === "USD"
        ? (exTax ? (a.dividendNetKrw || 0) : (a.dividendGrossKrw || 0))
        : (exTax ? a.dividendNet : a.dividendGross);
      const bDiv = usInKrw && b.currency === "USD"
        ? (exTax ? (b.dividendNetKrw || 0) : (b.dividendGrossKrw || 0))
        : (exTax ? b.dividendNet : b.dividendGross);
      const aRate = exTax ? a.returnRateNet : a.returnRateGross;
      const bRate = exTax ? b.returnRateNet : b.returnRateGross;

      switch (state.sortBy) {
        case "dividendDesc":
          return bDiv - aDiv;
        case "returnRateDesc":
          return bRate - aRate;
        case "evalDesc":
          return b.evalAmount - a.evalAmount;
        case "nameAsc":
          return a.name.localeCompare(b.name, "ko");
        case "totalReturnDesc":
        default:
          return bRet - aRet;
      }
    });

    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">조건에 맞는 종목이 없습니다.</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered
      .map((it) => {
        const cur = it.currency;
        const isYearlyBucket = Boolean(it.isPriorYearlyBucket);
        const isHeld = it.status === "HELD";
        const capPnl = exTax ? it.capitalPnlNet : it.capitalPnlGross;
        const unrealPnl = exTax ? it.unrealizedPnlNet : it.unrealizedPnlGross;
        const realPnl = exTax ? it.realizedPnlNet : it.realizedPnlGross;
        const divAmt = exTax ? it.dividendNet : it.dividendGross;
        const divKrwAmt = exTax ? (it.dividendNetKrw || 0) : (it.dividendGrossKrw || 0);
        const showDivAsKrw = cur === "USD" && usInKrw;
        const totalRet = exTax ? it.totalReturnNet : it.totalReturnGross;
        const totalRate = exTax ? it.returnRateNet : it.returnRateGross;

        if (isYearlyBucket) {
          return `
            <tr class="stock-row" style="background:#fcfaff;" data-symbol="${escapeHtml(it.symbol)}">
              <td>
                <div class="stock-identity">
                  <div class="market-avatar ${it.market === "KR" ? "kr" : "us"}">${it.market === "KR" ? "국내" : "미국"}</div>
                  <div>
                    <div class="stock-name-line">
                      <span>${escapeHtml(it.name)}</span>
                      <span class="badge badge-prior">연도별 통입력</span>
                    </div>
                    <div class="stock-sub-line">종목 상세 미제공 타사증권사 연간 배당 합산 · ${cur === "KRW" ? "원화(KRW)" : "달러(USD)"}</div>
                  </div>
                </div>
              </td>
              <td>
                <div class="cell-main" style="color:var(--prior-purple);">연도별 합산</div>
                <div class="cell-sub">총 ${it.dividendCount}개년 반영</div>
              </td>
              <td>
                <div class="cell-main">-</div>
                <div class="cell-sub">배당 전용 항목</div>
              </td>
              <td>
                <div class="cell-main">-</div>
                <div class="cell-sub">매매 내역 없음</div>
              </td>
              <td>
                <div class="cell-main val-div">${showDivAsKrw ? formatMoney(divKrwAmt, "KRW", divKrwAmt > 0) : formatMoney(divAmt, cur, divAmt > 0)}</div>
                <div class="cell-sub">${showDivAsKrw ? `원래 ${formatMoney(divAmt, "USD", divAmt > 0)} · ` : ""}${it.dividendCount}개년 누적</div>
              </td>
              <td>
                <div class="cell-main ${pnlClass(totalRet)}" style="font-size:15.5px;">${formatMoney(totalRet, cur, true)}</div>
                <div class="cell-sub"><span class="badge badge-prior">배당 수익 합산</span></div>
              </td>
              <td>
                <div class="cell-main" style="color:var(--text-secondary);">${formatMoney(it.totalTax, cur)}</div>
                <div class="cell-sub">배당세 ${formatMoney(it.dividendTax, cur)}</div>
              </td>
            </tr>
          `;
        }

        const transferBadge = it.hasTransferIn
          ? `<span class="badge badge-transfer" title="타사대체입고 ${formatQty(it.transferInQty)} 매수 반영">타사입고</span>`
          : "";
        const priorBadge = it.priorBrokerDiv
          ? `<span class="badge badge-prior" title="타사증권사 누적 배당 합산됨">타사배당</span>`
          : "";
        const extDivBadge = it.hasExtDiv
          ? `<span class="badge badge-prior" title="타사 실수령 배당 $${Number(it.extDivNet || 0).toFixed(2)} 합산됨">타사배당</span>`
          : "";

        return `
          <tr class="stock-row ${isHeld ? "" : "sold-row"}" data-symbol="${escapeHtml(it.symbol)}">
            <td>
              <div class="stock-identity">
                <div class="market-avatar ${it.market === "KR" ? "kr" : "us"}">${it.market === "KR" ? "국내" : "미국"}</div>
                <div>
                  <div class="stock-name-line">
                    <span>${escapeHtml(it.name)}</span>
                    <span class="badge ${isHeld ? "badge-held" : "badge-sold"}">${isHeld ? "보유중" : "매도완료"}</span>
                    ${transferBadge}
                    ${priorBadge}
                    ${extDivBadge}
                  </div>
                  <div class="stock-sub-line">${escapeHtml(it.symbol)} · ${cur === "KRW" ? "원화(KRW)" : "달러(USD)"}</div>
                </div>
              </div>
            </td>
            <td>
              <div class="cell-main">${isHeld ? formatQty(it.currentQty) : "0주 (전량매도)"}</div>
              <div class="cell-sub">${isHeld ? `평단 ${formatPrice(it.avgPrice, cur)}` : `누적매도 ${formatQty(it.totalSellQty)}`}</div>
            </td>
            <td>
              <div class="cell-main">${isHeld ? formatMoney(it.evalAmount, cur) : "-"}</div>
              <div class="cell-sub">${isHeld && it.lastPrice > 0 ? `기준가 ${formatPrice(it.lastPrice, cur)}` : "보유분 없음"}</div>
            </td>
            <td>
              <div class="cell-main ${pnlClass(capPnl)}">${formatMoney(capPnl, cur, true)}</div>
              <div class="cell-sub">${
                isHeld
                  ? `평가 ${formatMoney(unrealPnl, cur, true)} / 실현 ${formatMoney(realPnl, cur, true)}`
                  : `확정 실현손익`
              }</div>
            </td>
            <td>
              <div class="cell-main ${(showDivAsKrw ? divKrwAmt : divAmt) > 0 ? "val-div" : pnlClass(showDivAsKrw ? divKrwAmt : divAmt)}">${showDivAsKrw ? formatMoney(divKrwAmt, "KRW", divKrwAmt > 0) : formatMoney(divAmt, cur, divAmt > 0)}</div>
              <div class="cell-sub">${
                showDivAsKrw
                  ? `원래 ${formatMoney(divAmt, "USD", divAmt > 0)} · ${it.dividendCount}회 (${formatRate(exTax ? it.dividendYieldNet : it.dividendYieldGross, false)})`
                  : `배당 ${it.dividendCount}회 · 원금대비 ${formatRate(exTax ? it.dividendYieldNet : it.dividendYieldGross, false)}`
              }</div>
            </td>
            <td>
              <div class="cell-main ${pnlClass(totalRet)}" style="font-size:15.5px;">${formatMoney(totalRet, cur, true)}</div>
              <div class="cell-sub"><span class="badge ${rateBadgeClass(totalRet)}">${formatRate(totalRate)}</span></div>
            </td>
            <td>
              <div class="cell-main" style="color:var(--text-secondary);">${formatMoney(it.totalTax, cur)}</div>
              <div class="cell-sub">배당세 ${formatMoney(it.dividendTax, cur)}</div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  // =========================================================================
  // 6. 종목별 상세 드로어 (배당 내역 + 거래 원장)
  // =========================================================================
  function openDetailDrawer(symbol) {
    if (state.activeDetailSymbol !== symbol) {
      state.detailTradeFilter = "ALL";
    }
    state.activeDetailSymbol = symbol;
    $("detailBackdrop").classList.remove("hidden");
    const today = new Date().toISOString().slice(0, 10);
    if (!$("qDivDate").value) $("qDivDate").value = today;
    $("qDivIsPrior").checked = false;
    renderDetailDrawer(symbol);
  }

  function closeDetailDrawer() {
    state.activeDetailSymbol = null;
    $("detailBackdrop").classList.add("hidden");
  }

  function classifyTradeCategory(t) {
    if (t.source === "PDF_TRANSFER" || t.source === "PDF_EVENT") return "INBOUND";
    if (t.side === "SPLIT_IN" || t.side === "SPLIT_OUT" || t.source === "PDF_CORP") return "CORP";
    if (t.side === "SELL") return "SELL";
    return "BUY";
  }

  function getDivBadgeInfo(src, gross) {
    if (src === "PRIOR_YEARLY") return { cls: "badge-prior", label: "연도별 타사배당" };
    if (src === "PRIOR_BROKER") return { cls: "badge-prior", label: "종목별 타사배당" };
    if (src === "EXT_DIV") {
      if (Number(gross) === 0) return { cls: "badge-prior", label: "타사 세금환급" };
      return { cls: "badge-prior", label: "타사 배당" };
    }
    if (src === "PDF") {
      if (Number(gross) === 0) return { cls: "badge-transfer", label: "세금재정산" };
      if (Number(gross) < 0) return { cls: "badge-sold", label: "배당취소차감" };
      return { cls: "badge-auto", label: "거래내역서 확정" };
    }
    if (src === "AUTO") return { cls: "badge-held", label: "이후 자동산출" };
    return { cls: "badge-held", label: "직접입력" };
  }

  function getTradeBadgeInfo(t) {
    if (t.source === "PDF_TRANSFER") return { cls: "badge-transfer", label: "타사입고(매수)" };
    if (t.source === "PDF_EVENT") return { cls: "badge-auto", label: "이벤트입고" };
    if (t.side === "SPLIT_IN") return { cls: "badge-sold", label: "분할·병합입고" };
    if (t.side === "SPLIT_OUT") return { cls: "badge-sold", label: "분할·병합출고" };
    if (t.side === "BUY") return { cls: "badge-buy", label: "매수체결" };
    return { cls: "badge-sell", label: "매도체결" };
  }

  function renderDetailDrawer(symbol) {
    if (!state.portfolio) return;
    const item = (state.portfolio.items || []).find((x) => x.symbol === symbol);
    if (!item) {
      closeDetailDrawer();
      return;
    }

    const cur = item.currency;
    const exTax = state.excludeTax;
    const isYearlyBucket = Boolean(item.isPriorYearlyBucket);
    const isHeld = item.status === "HELD";

    $("detailMarketBadge").textContent = item.market === "KR" ? "🇰🇷 국내주식 (KRW)" : "🇺🇸 미국주식 (USD)";
    $("detailStatusBadge").textContent = isYearlyBucket
      ? `연도별 통입력 (${item.dividendCount}개년)`
      : isHeld
      ? `보유중 (${formatQty(item.currentQty)})`
      : "매도완료 (0주)";
    $("detailStatusBadge").className = `badge ${isYearlyBucket ? "badge-prior" : isHeld ? "badge-held" : "badge-sold"}`;
    $("openSinglePriorBtn").textContent = isYearlyBucket ? "📅 연도별 타사배당 편집" : "🏦 이 종목 타사 배당 통입력";

    const trBadge = $("detailTransferBadge");
    if (item.hasTransferIn) {
      trBadge.classList.remove("hidden");
      trBadge.textContent = `타사입고 ${formatQty(item.transferInQty)} 포함`;
    } else {
      trBadge.classList.add("hidden");
    }

    $("detailSymbolText").textContent = isYearlyBucket ? "종목 미분류 연간 합산" : item.isin ? `${item.symbol} (${item.isin})` : item.symbol;
    $("detailStockName").textContent = item.name;
    $("detailPriceLine").textContent = isYearlyBucket
      ? "종목별 내역을 제공하지 않는 이전 증권사의 연도별 배당금 합계 내역입니다."
      : isHeld
      ? `평균 매입단가 ${formatPrice(item.avgPrice, cur)} · 기준가 ${formatPrice(item.lastPrice, cur)} · 현재 매입원금 ${formatMoney(item.currentInvested, cur)}`
      : `누적 매수(타사입고 포함) ${formatMoney(item.totalBuyGross, cur)} · 누적 매도 ${formatMoney(item.totalSellGross, cur)}`;

    const totalRet = exTax ? item.totalReturnNet : item.totalReturnGross;
    const totalRate = exTax ? item.returnRateNet : item.returnRateGross;
    const capPnl = exTax ? item.capitalPnlNet : item.capitalPnlGross;
    const unrealPnl = exTax ? item.unrealizedPnlNet : item.unrealizedPnlGross;
    const realPnl = exTax ? item.realizedPnlNet : item.realizedPnlGross;
    const divAmt = exTax ? item.dividendNet : item.dividendGross;

    $("detailTotalReturnLabel").textContent = exTax ? "합산 총수익 (세금 제외)" : "합산 총수익 (세전)";
    const totEl = $("detailTotalReturnVal");
    totEl.textContent = formatMoney(totalRet, cur, true);
    totEl.className = `kpi-box-val ${pnlClass(totalRet)}`;
    $("detailTotalReturnRate").textContent = `투입원금 대비 ${formatRate(totalRate)}`;

    const capEl = $("detailCapitalVal");
    capEl.textContent = formatMoney(capPnl, cur, true);
    capEl.className = `kpi-box-val ${pnlClass(capPnl)}`;
    $("detailCapitalSub").textContent = `평가 ${formatMoney(unrealPnl, cur, true)} · 실현 ${formatMoney(realPnl, cur, true)}`;

    const showDetailDivInKrw = cur === "USD" && state.usDivInKrw;
    const detailDivKrw = exTax ? (item.dividendNetKrw || 0) : (item.dividendGrossKrw || 0);
    $("detailDividendVal").textContent = showDetailDivInKrw
      ? formatMoney(detailDivKrw, "KRW", detailDivKrw > 0)
      : formatMoney(divAmt, cur, divAmt > 0);
    $("detailDividendSub").textContent = showDetailDivInKrw
      ? `원래 ${formatMoney(divAmt, "USD", divAmt > 0)} · ${item.dividendCount}회 (${formatRate(exTax ? item.dividendYieldNet : item.dividendYieldGross, false)})`
      : `지급 ${item.dividendCount}회 · 원금대비 ${formatRate(exTax ? item.dividendYieldNet : item.dividendYieldGross, false)}`;

    $("detailTaxVal").textContent = formatMoney(item.totalTax + item.totalCommission, cur);
    $("detailTaxSub").textContent = `배당세 ${formatMoney(item.dividendTax, cur)} · 매매세 ${formatMoney(item.tradeTax, cur)}`;

    $("detailDivCount").textContent = String(item.dividends.length);
    $("detailTradeCount").textContent = String(item.trades.length);

    const divTbody = $("detailDividendsTbody");
    if (item.dividends.length === 0) {
      divTbody.innerHTML = `<tr><td colspan="5" class="empty-state" style="padding:28px;">지급된 배당 이력이 없습니다. 타사증권사에서 받은 배당이 있다면 상단 '이 종목 타사 배당 통입력'을 눌러 추가해주세요.</td></tr>`;
    } else {
      divTbody.innerHTML = item.dividends
        .map((d) => {
          const bInfo = getDivBadgeInfo(d.source, d.gross_amount);
          const g = Number(d.gross_amount) || 0;
          const tx = Number(d.tax_amount) || 0;
          const nt = Number(d.net_amount) || 0;
          const kg = Number(d.krw_gross) || 0;
          const ktx = Number(d.krw_tax) || 0;
          const knt = Number(d.krw_net) || 0;
          const fxR = Number(d.fx_rate) || 0;

          const fxTagHtml =
            cur === "USD" && fxR > 1
              ? g === 0
                ? `<span style="font-size:11px;color:var(--text-muted);font-weight:600;">원화 세금정산</span>`
                : `<span style="font-size:11px;color:var(--text-muted);font-weight:600;">${d.fx_exact ? "입금환율" : "기준환율"} ${fxR.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}원</span>`
              : "";

          let grossCellHtml = "";
          let taxCellHtml = "";
          let netCellHtml = "";

          if (showDetailDivInKrw) {
            grossCellHtml =
              g === 0
                ? "-"
                : `<div>${formatMoney(kg, "KRW", kg < 0)}</div><div class="cell-sub">${formatMoney(g, "USD", g < 0)}</div>`;
            if (g > 0 && tx >= 0) {
              const effRate = ((tx / g) * 100).toFixed(1);
              taxCellHtml = `<div>${formatMoney(ktx, "KRW")}</div><div class="cell-sub">${formatMoney(tx, "USD")} (${effRate}%)</div>`;
            } else if (ktx < 0 || tx < 0) {
              taxCellHtml = `<div class="val-div">환급 ${formatMoney(Math.abs(ktx), "KRW")}</div><div class="cell-sub">환급 ${formatMoney(Math.abs(tx), "USD")}</div>`;
            } else {
              taxCellHtml = `<div>${formatMoney(ktx, "KRW")}</div><div class="cell-sub">${formatMoney(tx, "USD")}</div>`;
            }
            netCellHtml = `<div class="${knt >= 0 ? "val-div" : "val-neg"}" style="font-weight:800;">${formatMoney(knt, "KRW", true)}</div><div class="cell-sub" style="font-weight:600;">${formatMoney(nt, "USD", true)}</div>`;
          } else {
            grossCellHtml = g === 0 ? "-" : formatMoney(g, cur, g < 0);
            if (g > 0 && tx >= 0) {
              const effRate = ((tx / g) * 100).toFixed(1);
              taxCellHtml = `${formatMoney(tx, cur)} <span style="font-size:11px;color:var(--text-muted);">(${effRate}%)</span>`;
            } else if (tx < 0) {
              taxCellHtml = `<span class="val-div">환급 ${formatMoney(Math.abs(tx), cur)}</span>`;
            } else {
              taxCellHtml = formatMoney(tx, cur);
            }
            const krwSubHint =
              cur === "USD" && knt !== 0
                ? `<div class="cell-sub">입금원화 ${formatMoney(knt, "KRW", true)}</div>`
                : "";
            netCellHtml = `<div class="${nt >= 0 ? "val-div" : "val-neg"}" style="font-weight:800;">${formatMoney(nt, cur, true)}</div>${krwSubHint}`;
          }

          return `
            <tr>
              <td>
                <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                  <span style="font-weight:700;">${escapeHtml(d.pay_date)}</span>
                  <span class="badge ${bInfo.cls}" style="font-size:10.5px;">${bInfo.label}</span>
                  ${fxTagHtml}
                </div>
                <div class="cell-sub">${escapeHtml(d.memo || "배당금 입금")}</div>
              </td>
              <td>${grossCellHtml}</td>
              <td style="color:var(--text-secondary);">${taxCellHtml}</td>
              <td>${netCellHtml}</td>
              <td style="white-space:nowrap;">
                <button type="button" class="btn btn-outline btn-sm" style="padding:4px 8px;font-size:11.5px;" data-edit-div="${d.id}" data-gross="${g}" data-tax="${tx}">보정</button>
                <button type="button" class="btn btn-danger-ghost" data-del-div="${d.id}">삭제</button>
              </td>
            </tr>
          `;
        })
        .join("");
    }

    const tradeCounts = { ALL: item.trades.length, BUY: 0, SELL: 0, INBOUND: 0, CORP: 0 };
    item.trades.forEach((t) => {
      tradeCounts[classifyTradeCategory(t)] += 1;
    });
    if (state.detailTradeFilter === "CORP" && tradeCounts.CORP === 0) {
      state.detailTradeFilter = "ALL";
    }
    const activeTf = state.detailTradeFilter || "ALL";

    $("dtfCntAll").textContent = String(tradeCounts.ALL);
    $("dtfCntBuy").textContent = String(tradeCounts.BUY);
    $("dtfCntSell").textContent = String(tradeCounts.SELL);
    $("dtfCntInbound").textContent = String(tradeCounts.INBOUND);
    $("dtfCntCorp").textContent = String(tradeCounts.CORP);
    $("dtfBtnCorp").classList.toggle("hidden", tradeCounts.CORP === 0);
    document.querySelectorAll("#detailTradeFilterGroup .seg-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tfilter === activeTf);
    });

    const visibleTrades =
      activeTf === "ALL" ? item.trades : item.trades.filter((t) => classifyTradeCategory(t) === activeTf);

    const sumEl = $("detailTradeFilterSummary");
    if (activeTf === "ALL") {
      sumEl.textContent = `매수 ${tradeCounts.BUY}건 · 매도 ${tradeCounts.SELL}건 · 입고 ${tradeCounts.INBOUND}건` +
        (tradeCounts.CORP > 0 ? ` · 분할·병합 ${tradeCounts.CORP}건` : "");
    } else if (activeTf === "BUY") {
      const qSum = visibleTrades.reduce((acc, t) => acc + (Number(t.quantity) || 0), 0);
      const gSum = visibleTrades.reduce((acc, t) => acc + (Number(t.gross_amount) || 0), 0);
      sumEl.textContent = `매수 합계 ${formatQty(qSum)} · ${formatMoney(gSum, cur)}`;
    } else if (activeTf === "SELL") {
      const qSum = visibleTrades.reduce((acc, t) => acc + (Number(t.quantity) || 0), 0);
      const gSum = visibleTrades.reduce((acc, t) => acc + (Number(t.gross_amount) || 0), 0);
      const pnlSum = visibleTrades.reduce((acc, t) => acc + (Number(exTax ? t.realizedPnlNet : t.realizedPnlGross) || 0), 0);
      sumEl.textContent = `매도 합계 ${formatQty(qSum)} · ${formatMoney(gSum, cur)} · 실현 ${formatMoney(pnlSum, cur, true)}`;
    } else if (activeTf === "INBOUND") {
      const qSum = visibleTrades.reduce((acc, t) => acc + (Number(t.quantity) || 0), 0);
      const gSum = visibleTrades.reduce((acc, t) => acc + (Number(t.gross_amount) || 0), 0);
      sumEl.textContent = `입고 합계 ${formatQty(qSum)} · 반영원가 ${formatMoney(gSum, cur)}`;
    } else {
      sumEl.textContent = `분할·병합 ${visibleTrades.length}건 (기존 매입원가 연속 보존)`;
    }

    const trTbody = $("detailTradesTbody");
    if (visibleTrades.length === 0) {
      const emptyMsgMap = {
        ALL: "조회된 거래 내역이 없습니다.",
        BUY: "이 종목의 일반 매수 체결 내역이 없습니다.",
        SELL: "이 종목의 매도 체결 내역이 없습니다.",
        INBOUND: "이 종목의 입고(타사대체입고·이벤트입고) 내역이 없습니다.",
        CORP: "이 종목의 분할·병합 내역이 없습니다.",
      };
      trTbody.innerHTML = `<tr><td colspan="5" class="empty-state" style="padding:28px;">${emptyMsgMap[activeTf] || emptyMsgMap.ALL}</td></tr>`;
    } else {
      trTbody.innerHTML = visibleTrades
        .map((t) => {
          const bInfo = getTradeBadgeInfo(t);
          const rowRealized = exTax ? t.realizedPnlNet : t.realizedPnlGross;
          return `
            <tr>
              <td>
                <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                  <span class="badge ${bInfo.cls}">${bInfo.label}</span>
                  <span style="font-weight:700;">${escapeHtml(t.trade_date)}</span>
                </div>
                <div class="cell-sub">${escapeHtml(t.memo || t.source)}</div>
              </td>
              <td>
                <div style="font-weight:600;">${formatQty(t.quantity)}</div>
                <div class="cell-sub">${t.price > 0 ? `@ ${formatPrice(t.price, cur)}` : "-"}</div>
              </td>
              <td>${t.gross_amount > 0 ? formatMoney(t.gross_amount, cur) : "-"}</td>
              <td style="color:var(--text-secondary);">
                <div>세금 ${formatMoney(t.tax, cur)}</div>
                <div class="cell-sub">수수료 ${formatMoney(t.commission, cur)}</div>
              </td>
              <td class="${rowRealized !== null ? pnlClass(rowRealized) : ""}" style="font-weight:700;">
                ${rowRealized !== null ? formatMoney(rowRealized, cur, true) : "-"}
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }

  function switchDetailTab(tabName) {
    state.activeDetailTab = tabName;
    $("dtabBtnDividends").classList.toggle("active", tabName === "dividends");
    $("dtabBtnTrades").classList.toggle("active", tabName === "trades");
    $("dtabPanelDividends").classList.toggle("hidden", tabName !== "dividends");
    $("dtabPanelTrades").classList.toggle("hidden", tabName !== "trades");
  }

  function getActiveDetailItem() {
    if (!state.portfolio || !state.activeDetailSymbol) return null;
    return state.portfolio.items.find((x) => x.symbol === state.activeDetailSymbol) || null;
  }

  // =========================================================================
  // 7. 타사증권사 배당금 '통입력' 전용 모달 제어
  // =========================================================================
  function switchPriorModalTab(tabName) {
    state.priorModalTab = tabName === "byYear" ? "byYear" : "byStock";
    $("pmTabBtnStock").classList.toggle("active", state.priorModalTab === "byStock");
    $("pmTabBtnYear").classList.toggle("active", state.priorModalTab === "byYear");
    $("priorPanelByStock").classList.toggle("hidden", state.priorModalTab !== "byStock");
    $("priorPanelByYear").classList.toggle("hidden", state.priorModalTab !== "byYear");
    if (state.priorModalTab === "byYear") {
      const hasStoredTax = (state.portfolio?.priorYearly || []).some(
        (r) => (r.KRW && r.KRW.taxAmount > 0) || (r.USD && r.USD.taxAmount > 0)
      );
      if (hasStoredTax && $("yearlyTaxCalcMode").value === "netOnly") {
        $("yearlyTaxCalcMode").value = "directGrossTax";
      }
      renderYearlyPriorModal();
    } else {
      renderPriorBrokerModal();
    }
  }

  function openPriorBrokerModal(focusSymbol = null, initialTab = "byStock") {
    if (!state.portfolio) return;
    if (focusSymbol && (focusSymbol === "PRIOR_YEARLY_KR" || focusSymbol === "PRIOR_YEARLY_US")) {
      initialTab = "byYear";
      focusSymbol = null;
    }
    if (focusSymbol && !state.priorModalExtraSymbols.includes(focusSymbol)) {
      const targetIt = state.portfolio.items.find((x) => x.symbol === focusSymbol);
      if (targetIt && !targetIt.isPriorYearlyBucket && !targetIt.hasTransferIn && !targetIt.priorBrokerDiv) {
        state.priorModalExtraSymbols.push(focusSymbol);
      }
    }
    switchPriorModalTab(initialTab);
    if (initialTab === "byStock" && focusSymbol) {
      renderPriorBrokerModal(focusSymbol);
    }
    $("priorBrokerModal").classList.remove("hidden");
  }

  function renderPriorBrokerModal(focusSymbol = null) {
    if (!state.portfolio) return;
    const allItems = (state.portfolio.items || []).filter((it) => !it.isPriorYearlyBucket);
    const mode = $("priorTaxCalcMode").value;

    // 1) 타사대체입고 종목 + 이미 타사배당 입력된 종목 + 사용자가 추가 선택한 종목
    const tableItems = allItems
      .filter(
        (it) =>
          it.hasTransferIn ||
          it.priorBrokerDiv !== null ||
          state.priorModalExtraSymbols.includes(it.symbol)
      )
      .sort((a, b) => {
        if (a.market !== b.market) return a.market === "KR" ? -1 : 1;
        return a.name.localeCompare(b.name, "ko");
      });

    const tableSyms = new Set(tableItems.map((x) => x.symbol));
    const remainingItems = allItems
      .filter((it) => !tableSyms.has(it.symbol))
      .sort((a, b) => a.name.localeCompare(b.name, "ko"));

    const extraSel = $("priorExtraStockSelect");
    extraSel.innerHTML =
      `<option value="">+ 대체입고 외 다른 보유·매도 종목 선택해서 추가 (${remainingItems.length}종목)...</option>` +
      remainingItems
        .map(
          (it) =>
            `<option value="${escapeHtml(it.symbol)}">[${it.market === "KR" ? "국내" : "미국"}] ${escapeHtml(it.name)} (${escapeHtml(it.symbol)})</option>`
        )
        .join("");

    const tbody = $("priorBrokerTableBody");
    if (tableItems.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state" style="padding:28px;">위 선택창에서 종목을 추가해 타사 배당 총액을 입력해주세요.</td></tr>`;
      return;
    }

    tbody.innerHTML = tableItems
      .map((it) => {
        const pb = it.priorBrokerDiv;
        let initVal = "";
        if (pb) {
          initVal = mode === "grossWithTax" ? pb.grossAmount : pb.netAmount;
        }
        const unitPlaceholder = it.currency === "KRW" ? "원 (예: 350000)" : "USD (예: 240.50)";
        const subDesc = it.hasTransferIn
          ? `타사입고 ${formatQty(it.transferInQty)} (${it.firstTransferDate || "이관"})`
          : "직접 선택 종목";
        const defaultDate = pb?.payDate || it.firstTransferDate || "2022-01-01";
        const memoVal = pb?.memo || "";

        return `
          <tr data-prior-row="${escapeHtml(it.symbol)}" data-market="${it.market}" data-currency="${it.currency}" data-name="${escapeHtml(it.name)}" data-paydate="${escapeHtml(defaultDate)}" data-had-existing="${pb ? "1" : "0"}">
            <td style="padding-left:14px;">
              <div style="font-weight:700;">${escapeHtml(it.name)} <span style="font-size:12px;color:var(--text-muted);">(${escapeHtml(it.symbol)})</span></div>
              <div class="cell-sub">${escapeHtml(subDesc)}</div>
            </td>
            <td>
              <span class="badge ${it.market === "KR" ? "badge-held" : "badge-buy"}">${it.currency === "KRW" ? "원화 KRW" : "달러 USD"}</span>
            </td>
            <td>
              <input type="number" step="any" min="0" class="prior-input" data-prior-amt="${escapeHtml(it.symbol)}" value="${initVal !== "" ? initVal : ""}" placeholder="${unitPlaceholder}" />
            </td>
            <td style="padding-right:14px;">
              <input type="text" class="form-input" style="padding:7px 10px;font-size:12.5px;min-width:160px;" data-prior-memo="${escapeHtml(it.symbol)}" value="${escapeHtml(memoVal)}" placeholder="타사증권사 누적 배당금" />
            </td>
          </tr>
        `;
      })
      .join("");

    if (focusSymbol) {
      setTimeout(() => {
        const inp = tbody.querySelector(`[data-prior-amt="${CSS.escape(focusSymbol)}"]`);
        if (inp) {
          inp.focus();
          inp.select();
        }
      }, 60);
    }
  }

  async function savePriorBrokerBatch() {
    const mode = $("priorTaxCalcMode").value;
    const rows = document.querySelectorAll("#priorBrokerTableBody tr[data-prior-row]");
    const payloadItems = [];

    rows.forEach((tr) => {
      const sym = tr.dataset.priorRow;
      const mkt = tr.dataset.market;
      const cur = tr.dataset.currency;
      const name = tr.dataset.name;
      const payDate = tr.dataset.paydate || "2022-01-01";
      const hadExisting = tr.dataset.hadExisting === "1";

      const amtInput = tr.querySelector(`[data-prior-amt="${CSS.escape(sym)}"]`);
      const memoInput = tr.querySelector(`[data-prior-memo="${CSS.escape(sym)}"]`);
      const rawStr = (amtInput?.value || "").trim();
      const memo = (memoInput?.value || "").trim() || "타사증권사 이관 전 누적 배당금 (통입력)";

      if (rawStr === "" || Number(rawStr) === 0) {
        if (hadExisting) {
          payloadItems.push({ symbol: sym, name, market: mkt, currency: cur, payDate, grossAmount: 0, taxAmount: 0, netAmount: 0, memo });
        }
        return;
      }

      const val = parseFloat(rawStr);
      if (isNaN(val) || val < 0) return;

      const rate = mkt === "KR" ? 0.154 : 0.15;
      let gross = 0;
      let tax = 0;
      let net = 0;

      if (mode === "netOnly") {
        net = cur === "KRW" ? Math.round(val) : Number(val.toFixed(2));
        tax = 0;
        gross = net;
      } else if (mode === "netWithTax") {
        net = cur === "KRW" ? Math.round(val) : Number(val.toFixed(2));
        gross = cur === "KRW" ? Math.round(net / (1 - rate)) : Number((net / (1 - rate)).toFixed(2));
        tax = cur === "KRW" ? Math.max(0, gross - net) : Number(Math.max(0, gross - net).toFixed(2));
      } else {
        gross = cur === "KRW" ? Math.round(val) : Number(val.toFixed(2));
        tax = cur === "KRW" ? Math.floor((gross * rate) / 10) * 10 : Number((gross * rate).toFixed(2));
        net = cur === "KRW" ? Math.round(gross - tax) : Number((gross - tax).toFixed(2));
      }

      payloadItems.push({
        symbol: sym,
        name,
        market: mkt,
        currency: cur,
        payDate,
        grossAmount: gross,
        taxAmount: tax,
        netAmount: net,
        memo,
      });
    });

    if (payloadItems.length === 0) {
      showToast("입력되거나 변경된 타사 배당 금액이 없습니다.", true);
      return;
    }

    const btn = $("savePriorBrokerBatchBtn");
    btn.disabled = true;
    btn.textContent = "저장 중...";
    try {
      const res = await apiFetch("/api/dividends/prior-broker", {
        method: "POST",
        body: JSON.stringify({
          accountKey: getActiveAccountKey(),
          items: payloadItems,
        }),
      });
      $("priorBrokerModal").classList.add("hidden");
      showToast(`타사 누적 배당 반영 완료! (${res.savedCount}종목 저장)`);
      await loadPortfolio();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "종목별 타사 배당 일괄 저장";
    }
  }

  function computeYearlyEntryAmounts(mode, cur, rawAmtStr, rawTaxStr) {
    const amtVal = parseFloat((rawAmtStr || "").trim());
    if (isNaN(amtVal) || amtVal <= 0) {
      return { gross: 0, tax: 0, net: 0, active: false };
    }
    const rate = cur === "KRW" ? 0.154 : 0.15;
    const roundCur = (v) => (cur === "KRW" ? Math.round(v) : Number(v.toFixed(2)));

    if (mode === "netOnly") {
      const net = roundCur(amtVal);
      return { gross: net, tax: 0, net, active: true };
    }
    if (mode === "netWithTax") {
      const net = roundCur(amtVal);
      const gross = roundCur(net / (1 - rate));
      const tax = roundCur(Math.max(0, gross - net));
      return { gross, tax, net, active: true };
    }
    if (mode === "directGrossTax") {
      const gross = roundCur(amtVal);
      const parsedTax = parseFloat((rawTaxStr || "").trim());
      const tax = !isNaN(parsedTax) && parsedTax >= 0 ? roundCur(parsedTax) : 0;
      const net = roundCur(Math.max(0, gross - tax));
      return { gross, tax, net, active: true };
    }
    const gross = roundCur(amtVal);
    const tax = cur === "KRW" ? Math.floor((gross * rate) / 10) * 10 : Number((gross * rate).toFixed(2));
    const net = roundCur(Math.max(0, gross - tax));
    return { gross, tax, net, active: true };
  }

  function renderYearlyPriorModal() {
    if (!state.portfolio) return;
    const mode = $("yearlyTaxCalcMode").value;
    const isDirect = mode === "directGrossTax";
    const existingList = state.portfolio.priorYearly || [];
    const existingMap = new Map();
    let minYr = state.yearlyMinYear || 2019;
    const curYr = new Date().getFullYear();
    let maxYr = Math.max(curYr, 2026);

    existingList.forEach((row) => {
      const y = Number(row.year);
      if (y > 0) {
        existingMap.set(y, row);
        if (y < minYr) minYr = y;
        if (y > maxYr) maxYr = y;
      }
    });
    state.yearlyMinYear = minYr;

    $("thYearlyKrw").textContent = isDirect
      ? "🇰🇷 국내 세전 배당 (원) + 세금 (원)"
      : mode === "grossWithTax"
      ? "🇰🇷 국내 세전 배당 합계 (원)"
      : "🇰🇷 국내 실수령 배당 합계 (원)";
    $("thYearlyUsd").textContent = isDirect
      ? "🇺🇸 미국 세전 배당 ($) + 세금 ($)"
      : mode === "grossWithTax"
      ? "🇺🇸 미국 세전 배당 합계 ($)"
      : "🇺🇸 미국 실수령 배당 합계 ($)";

    const years = [];
    for (let y = minYr; y <= maxYr; y++) years.push(y);

    const tbody = $("yearlyPriorTableBody");
    tbody.innerHTML = years
      .map((y) => {
        const ex = existingMap.get(y) || {};
        const krwObj = ex.KRW || null;
        const usdObj = ex.USD || null;
        const krwInit =
          krwObj !== null
            ? mode === "grossWithTax" || isDirect
              ? krwObj.grossAmount
              : krwObj.netAmount
            : "";
        const krwTaxInit = krwObj !== null ? krwObj.taxAmount : "";
        const usdInit =
          usdObj !== null
            ? mode === "grossWithTax" || isDirect
              ? usdObj.grossAmount
              : usdObj.netAmount
            : "";
        const usdTaxInit = usdObj !== null ? usdObj.taxAmount : "";
        const rawMemo = (krwObj && krwObj.memo) || (usdObj && usdObj.memo) || "";
        const defaultLabel = `${y}년 타사증권사 연간 배당 합산 (통입력)`;
        const cleanMemo = rawMemo === defaultLabel ? "" : rawMemo;
        const hadExisting = krwObj || usdObj ? "1" : "0";

        return `
          <tr data-yearly-row="${y}" data-had-existing="${hadExisting}">
            <td style="padding-left:14px;font-weight:800;white-space:nowrap;">${y}년</td>
            <td>
              <div style="display:inline-flex;gap:6px;align-items:center;justify-content:flex-end;flex-wrap:wrap;">
                <input type="number" step="1" min="0" class="prior-input" style="width:${isDirect ? "130px" : "155px"};" data-yr-krw="${y}" value="${krwInit !== "" ? krwInit : ""}" placeholder="원 (예: 480000)" />
                ${
                  isDirect
                    ? `<input type="number" step="1" min="0" class="prior-input" style="width:105px;font-weight:600;" data-yr-krw-tax="${y}" value="${krwTaxInit !== "" ? krwTaxInit : ""}" placeholder="세금(원)" />`
                    : ""
                }
              </div>
            </td>
            <td>
              <div style="display:inline-flex;gap:6px;align-items:center;justify-content:flex-end;flex-wrap:wrap;">
                <input type="number" step="any" min="0" class="prior-input" style="width:${isDirect ? "120px" : "145px"};" data-yr-usd="${y}" value="${usdInit !== "" ? usdInit : ""}" placeholder="USD (예: 320.50)" />
                ${
                  isDirect
                    ? `<input type="number" step="any" min="0" class="prior-input" style="width:95px;font-weight:600;" data-yr-usd-tax="${y}" value="${usdTaxInit !== "" ? usdTaxInit : ""}" placeholder="세금($)" />`
                    : ""
                }
              </div>
            </td>
            <td style="padding-right:14px;">
              <input type="text" class="form-input" style="padding:7px 10px;font-size:12.5px;min-width:150px;" data-yr-memo="${y}" value="${escapeHtml(cleanMemo)}" placeholder="${y}년 타사 연간 배당" />
            </td>
          </tr>
        `;
      })
      .join("");

    updateYearlyLivePreview();
  }

  function updateYearlyLivePreview() {
    const mode = $("yearlyTaxCalcMode").value;
    const rows = document.querySelectorAll("#yearlyPriorTableBody tr[data-yearly-row]");
    let krwGross = 0;
    let krwNet = 0;
    let usdGross = 0;
    let usdNet = 0;
    let activeYears = 0;

    rows.forEach((tr) => {
      const y = tr.dataset.yearlyRow;
      const krwStr = tr.querySelector(`[data-yr-krw="${y}"]`)?.value || "";
      const krwTaxStr = tr.querySelector(`[data-yr-krw-tax="${y}"]`)?.value || "";
      const usdStr = tr.querySelector(`[data-yr-usd="${y}"]`)?.value || "";
      const usdTaxStr = tr.querySelector(`[data-yr-usd-tax="${y}"]`)?.value || "";

      const kRes = computeYearlyEntryAmounts(mode, "KRW", krwStr, krwTaxStr);
      const uRes = computeYearlyEntryAmounts(mode, "USD", usdStr, usdTaxStr);
      if (kRes.active || uRes.active) activeYears += 1;
      krwGross += kRes.gross;
      krwNet += kRes.net;
      usdGross += uRes.gross;
      usdNet += uRes.net;
    });

    $("yearlyLivePreviewBar").textContent =
      activeYears === 0
        ? "합산 미리보기: 입력된 연도별 배당 금액이 없습니다. (2019년부터 해당되는 연도에만 금액을 입력해주세요)"
        : `합산 미리보기 (${activeYears}개년): 국내 실수령 ${formatMoney(krwNet, "KRW", true)} (세전 ${formatMoney(krwGross, "KRW")}) · 미국 실수령 ${formatMoney(usdNet, "USD", true)} (세전 ${formatMoney(usdGross, "USD")})`;
  }

  async function saveYearlyPriorBatch() {
    const mode = $("yearlyTaxCalcMode").value;
    const defaultBroker = ($("yearlyDefaultBrokerMemo").value || "").trim();
    const rows = document.querySelectorAll("#yearlyPriorTableBody tr[data-yearly-row]");
    const yearsPayload = [];

    rows.forEach((tr) => {
      const y = Number(tr.dataset.yearlyRow);
      const hadExisting = tr.dataset.hadExisting === "1";
      const krwStr = tr.querySelector(`[data-yr-krw="${y}"]`)?.value || "";
      const krwTaxStr = tr.querySelector(`[data-yr-krw-tax="${y}"]`)?.value || "";
      const usdStr = tr.querySelector(`[data-yr-usd="${y}"]`)?.value || "";
      const usdTaxStr = tr.querySelector(`[data-yr-usd-tax="${y}"]`)?.value || "";
      const rowMemoRaw = (tr.querySelector(`[data-yr-memo="${y}"]`)?.value || "").trim();

      const kRes = computeYearlyEntryAmounts(mode, "KRW", krwStr, krwTaxStr);
      const uRes = computeYearlyEntryAmounts(mode, "USD", usdStr, usdTaxStr);

      if (!kRes.active && !uRes.active && !hadExisting) return;

      const finalMemo =
        rowMemoRaw ||
        (defaultBroker ? `${y}년 ${defaultBroker} 연간 배당 합산` : `${y}년 타사증권사 연간 배당 합산 (통입력)`);

      yearsPayload.push({
        year: y,
        memo: finalMemo,
        KRW: { grossAmount: kRes.gross, taxAmount: kRes.tax, netAmount: kRes.net, memo: finalMemo },
        USD: { grossAmount: uRes.gross, taxAmount: uRes.tax, netAmount: uRes.net, memo: finalMemo },
      });
    });

    if (yearsPayload.length === 0) {
      showToast("입력되거나 변경된 연도별 배당 내역이 없습니다.", true);
      return;
    }

    const btn = $("saveYearlyPriorBatchBtn");
    btn.disabled = true;
    btn.textContent = "저장 중...";
    try {
      const res = await apiFetch("/api/dividends/prior-yearly", {
        method: "POST",
        body: JSON.stringify({
          accountKey: getActiveAccountKey(),
          years: yearsPayload,
        }),
      });
      $("priorBrokerModal").classList.add("hidden");
      showToast(`연도별 타사 배당 반영 완료! (${res.savedCount}건 저장${res.deletedCount ? ` · ${res.deletedCount}건 삭제` : ""})`);
      await loadPortfolio();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "연도별 배당 일괄 저장";
    }
  }

  function autoCalcQuickDivFromGross(taxRateOverride = null) {
    const item = getActiveDetailItem();
    if (!item) return;
    const gross = parseFloat($("qDivGross").value);
    if (isNaN(gross) || gross <= 0) return;
    const defaultRate = item.market === "KR" ? 0.154 : 0.15;
    const rate = taxRateOverride !== null ? taxRateOverride : defaultRate;
    const tax = item.currency === "KRW"
      ? Math.floor((gross * rate) / 10) * 10
      : Number((gross * rate).toFixed(2));
    const net = item.currency === "KRW" ? Math.round(gross - tax) : Number((gross - tax).toFixed(2));
    $("qDivTax").value = tax;
    $("qDivNet").value = net;
  }

  function autoCalcQuickDivFromNet() {
    const item = getActiveDetailItem();
    if (!item) return;
    const net = parseFloat($("qDivNet").value);
    if (isNaN(net) || net <= 0) return;
    if ($("qDivIsPrior").checked) {
      $("qDivGross").value = net;
      $("qDivTax").value = 0;
      return;
    }
    const rate = item.market === "KR" ? 0.154 : 0.15;
    const gross = item.currency === "KRW"
      ? Math.round(net / (1 - rate))
      : Number((net / (1 - rate)).toFixed(2));
    const tax = item.currency === "KRW"
      ? Math.max(0, gross - net)
      : Number(Math.max(0, gross - net).toFixed(2));
    $("qDivGross").value = gross;
    $("qDivTax").value = tax;
  }

  function parseBulkDividendLines(rawText) {
    const lines = rawText
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#") && !l.startsWith("//"));

    const dividends = [];
    for (const line of lines) {
      const cols = line.includes("\t")
        ? line.split("\t").map((c) => c.trim())
        : line.split(",").map((c) => c.trim());
      if (cols.length < 5) continue;

      const kind = cols[0].toUpperCase();
      if (kind.includes("배당") || kind === "DIV" || kind === "DIVIDEND") {
        const dt = cols[1].replace(/\./g, "-").replace(/\//g, "-").slice(0, 10);
        const sym = cols[2].toUpperCase();
        const name = cols[3] || sym;
        const gross = parseFloat(cols[4].replace(/,/g, "")) || 0;
        const tax = cols[5] !== undefined ? parseFloat(cols[5].replace(/,/g, "")) || 0 : 0;
        if (gross > 0) {
          dividends.push({ payDate: dt, symbol: sym, name, grossAmount: gross, taxAmount: tax, netAmount: gross - tax });
        }
      }
    }
    return { dividends };
  }

  // =========================================================================
  // 8. 이벤트 연결
  // =========================================================================
  function bindEvents() {
    $("toggleSecretVisBtn").addEventListener("click", () => {
      const inp = $("setupClientSecret");
      const isPw = inp.type === "password";
      inp.type = isPw ? "text" : "password";
      $("toggleSecretVisBtn").textContent = isPw ? "숨기기" : "표시";
    });

    $("setupForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const clientId = $("setupClientId").value.trim();
      const clientSecret = $("setupClientSecret").value.trim();
      const pin = $("setupPin").value.trim();
      const pinConfirm = $("setupPinConfirm").value.trim();

      if (!/^\d{6}$/.test(pin)) {
        showToast("PIN 번호는 숫자 6자리로 입력해주세요.", true);
        return;
      }
      if (pin !== pinConfirm) {
        showToast("PIN 번호 확인이 일치하지 않습니다.", true);
        return;
      }

      const btn = $("setupSubmitBtn");
      btn.disabled = true;
      btn.textContent = "AES-256 암호화 저장 및 증분 동기화 중...";
      try {
        const creds = {
          clientId,
          clientSecret,
          baseUrl: "https://openapi.tossinvest.com",
          accessToken: null,
          tokenExpiresAt: 0,
          accounts: [],
          selectedAccountSeq: null,
        };
        const envelope = await encryptCredentialsWithPin(pin, creds);
        await persistEncryptedEnvelope(envelope);

        state.hasVault = true;
        state.unlocked = true;
        state.isLedgerOnlyPreview = false;
        state.decryptedCreds = creds;
        state.currentPin = pin;
        sessionStorage.setItem(SESSION_UNLOCKED_CACHE_KEY, JSON.stringify(creds));
        $("setupForm").reset();

        routeScreen();
        await triggerTossSync(true);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "암호화 저장 후 계좌 불러오기";
      }
    });

    $("enterLedgerPreviewFromSetupBtn")?.addEventListener("click", () => {
      state.isLedgerOnlyPreview = true;
      routeScreen();
    });
    $("exitDemoToAuthBtn")?.addEventListener("click", () => {
      state.isLedgerOnlyPreview = false;
      routeScreen();
    });

    $("pinKeypad").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.dataset.digit !== undefined) {
        handlePinDigit(btn.dataset.digit);
      } else if (btn.dataset.action === "backspace") {
        handlePinBackspace();
      } else if (btn.dataset.action === "clear") {
        state.pinBuffer = "";
        updatePinDots();
      }
    });

    document.addEventListener("keydown", (e) => {
      if ($("pinScreen").classList.contains("hidden")) return;
      if (!$("changePinModal").classList.contains("hidden")) return;
      if (e.key >= "0" && e.key <= "9") {
        e.preventDefault();
        handlePinDigit(e.key);
      } else if (e.key === "Backspace") {
        e.preventDefault();
        handlePinBackspace();
      } else if (e.key === "Escape") {
        state.pinBuffer = "";
        updatePinDots();
      }
    });

    $("lockVaultBtn").addEventListener("click", () => {
      if (state.isLedgerOnlyPreview) {
        state.isLedgerOnlyPreview = false;
        routeScreen();
        return;
      }
      state.unlocked = false;
      state.decryptedCreds = null;
      state.currentPin = "";
      sessionStorage.removeItem(SESSION_UNLOCKED_CACHE_KEY);
      closeDetailDrawer();
      showToast("포트폴리오가 잠겼습니다.");
      routeScreen();
    });

    $("openChangePinModalBtn").addEventListener("click", () => {
      $("changePinModal").classList.remove("hidden");
    });
    $("changePinForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const curPin = $("curPinInput").value.trim();
      const newPin = $("newPinInput").value.trim();
      if (!/^\d{6}$/.test(newPin)) {
        showToast("새 PIN은 숫자 6자리여야 합니다.", true);
        return;
      }
      try {
        const creds = await decryptCredentialsWithPin(curPin, state.envelope);
        const newEnv = await encryptCredentialsWithPin(newPin, creds);
        await persistEncryptedEnvelope(newEnv);
        $("changePinForm").reset();
        $("changePinModal").classList.add("hidden");
        showToast("6자리 PIN 번호가 변경되었습니다.");
      } catch {
        showToast("현재 PIN 번호가 일치하지 않습니다.", true);
      }
    });

    $("resetVaultBtn").addEventListener("click", async () => {
      const ok = window.confirm(
        "암호화된 API 키 설정을 초기화하고 새로 등록하시겠습니까?\n(로컬 DB에 적재된 PDF 원장과 배당 내역은 그대로 유지됩니다.)"
      );
      if (!ok) return;
      await apiFetch("/api/encrypted-config/reset", { method: "POST" }).catch(() => {});
      localStorage.removeItem(LOCAL_ENCRYPTED_BACKUP_KEY);
      sessionStorage.removeItem(SESSION_UNLOCKED_CACHE_KEY);
      state.hasVault = false;
      state.unlocked = false;
      state.envelope = null;
      state.decryptedCreds = null;
      showToast("API 키 볼트가 초기화되었습니다.");
      routeScreen();
    });

    $("openSyncLogModalBtn").addEventListener("click", () => openSyncLogModal("ALL"));
    $("refreshPricesBtn").addEventListener("click", () => triggerRefreshPrices());
    $("syncApiBtn").addEventListener("click", () => triggerTossSync(true));
    $("autoSyncDivBtn").addEventListener("click", () => triggerAutoDividendSync(true));

    $("openPriorBrokerModalBtn").addEventListener("click", () => openPriorBrokerModal(null, "byStock"));
    $("openYearlyPriorModalBtn").addEventListener("click", () => openPriorBrokerModal(null, "byYear"));
    $("priorModalTabGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".drawer-tab");
      if (!btn || !btn.dataset.pmtab) return;
      switchPriorModalTab(btn.dataset.pmtab);
    });
    $("openSinglePriorBtn").addEventListener("click", () => {
      if (state.activeDetailSymbol) {
        openPriorBrokerModal(state.activeDetailSymbol);
      }
    });
    $("priorTaxCalcMode").addEventListener("change", () => renderPriorBrokerModal());
    $("addExtraStockToPriorTableBtn").addEventListener("click", () => {
      const sym = $("priorExtraStockSelect").value;
      if (!sym) return;
      if (!state.priorModalExtraSymbols.includes(sym)) {
        state.priorModalExtraSymbols.push(sym);
      }
      renderPriorBrokerModal(sym);
    });
    $("savePriorBrokerBatchBtn").addEventListener("click", savePriorBrokerBatch);

    $("yearlyTaxCalcMode").addEventListener("change", () => renderYearlyPriorModal());
    $("addEarlierYearRowBtn").addEventListener("click", () => {
      state.yearlyMinYear = Math.max(1990, (state.yearlyMinYear || 2019) - 1);
      renderYearlyPriorModal();
    });
    $("yearlyPriorTableBody").addEventListener("input", () => updateYearlyLivePreview());
    $("saveYearlyPriorBatchBtn").addEventListener("click", saveYearlyPriorBatch);

    $("reimportPdfFolderBtn").addEventListener("click", async () => {
      const btn = $("reimportPdfFolderBtn");
      btn.disabled = true;
      btn.textContent = "PDF 파싱 중...";
      try {
        const res = await apiFetch("/api/pdf/reimport", {
          method: "POST",
          body: JSON.stringify({ accountKey: getActiveAccountKey() }),
        });
        markNewSyncLogBadge();
        showToast(
          `PDF 폴더 재반영 완료! (~${res.cutoffDate} · ${res.totalCanonicalStocks}종목 · 거래 ${res.tradesInserted}건 · 배당 ${res.dividendsInserted}건)`
        );
        await refreshPdfFolderStatus();
        if (!$("dashboardScreen").classList.contains("hidden")) {
          await loadPortfolio();
        }
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "↻ PDF 폴더 재반영";
      }
    });

    $("accountSelector").addEventListener("change", async (e) => {
      if (!state.decryptedCreds) return;
      state.decryptedCreds.selectedAccountSeq = e.target.value;
      sessionStorage.setItem(SESSION_UNLOCKED_CACHE_KEY, JSON.stringify(state.decryptedCreds));
      if (state.currentPin) {
        const env = await encryptCredentialsWithPin(state.currentPin, state.decryptedCreds);
        await persistEncryptedEnvelope(env).catch(() => {});
      }
      await loadPortfolio();
    });

    $("excludeTaxToggle").addEventListener("change", (e) => {
      state.excludeTax = e.target.checked;
      localStorage.setItem(TAX_PREF_STORAGE_KEY, String(state.excludeTax));
      renderDashboard();
      if (state.activeDetailSymbol) {
        renderDetailDrawer(state.activeDetailSymbol);
      }
    });

    $("usDivCurrencyGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.usdivcur) return;
      state.usDivInKrw = btn.dataset.usdivcur === "KRW";
      localStorage.setItem(US_DIV_CUR_STORAGE_KEY, state.usDivInKrw ? "KRW" : "USD");
      renderDashboard();
      if (state.activeDetailSymbol) {
        renderDetailDrawer(state.activeDetailSymbol);
      }
    });

    $("toggleCombinedKrwChartBtn").addEventListener("click", () => {
      state.showCombinedKrwChart = !state.showCombinedKrwChart;
      renderDashboard();
    });

    $("openYearlyModalFromChartBtn").addEventListener("click", () => openPriorBrokerModal(null, "byYear"));
    $("growthIncludePriorStockChk").addEventListener("change", (e) => {
      state.growthIncludeStockPrior = e.target.checked;
      if (state.portfolio) {
        renderDividendGrowthSection(state.portfolio.items, state.excludeTax);
      }
    });
    $("toggleGrowthTableBtn").addEventListener("click", () => {
      state.growthShowTable = !state.growthShowTable;
      $("growthComparisonTableWrap").classList.toggle("hidden", !state.growthShowTable);
      $("toggleGrowthTableBtn").textContent = state.growthShowTable ? "표 접기" : "표로 함께 보기";
    });
    $("toggleGrowthCollapseBtn").addEventListener("click", () => {
      state.growthCollapsed = !state.growthCollapsed;
      $("growthSectionBody").classList.toggle("hidden", state.growthCollapsed);
      $("toggleGrowthCollapseBtn").textContent = state.growthCollapsed ? "펼치기" : "접기";
    });

    const handleGrowthBarInteract = (e) => {
      const grp = e.target.closest(".growth-bar-group");
      if (!grp) return;
      const slotKey = grp.dataset.growthCur;
      const yr = Number(grp.dataset.growthYr);
      if (!slotKey || !yr || state.selectedGrowthYear[slotKey] === yr) return;
      state.selectedGrowthYear[slotKey] = yr;
      document.querySelectorAll(`.growth-bar-group[data-growth-cur="${slotKey}"]`).forEach((el) => {
        el.classList.toggle("selected", Number(el.dataset.growthYr) === yr);
      });
      const dispCur = slotKey === "USD" && !state.usDivInKrw ? "USD" : "KRW";
      renderGrowthInspector(slotKey, dispCur, yr, state.excludeTax);
    };
    $("krwGrowthSvgWrap").addEventListener("mouseover", handleGrowthBarInteract);
    $("krwGrowthSvgWrap").addEventListener("click", handleGrowthBarInteract);
    $("usdGrowthSvgWrap").addEventListener("mouseover", handleGrowthBarInteract);
    $("usdGrowthSvgWrap").addEventListener("click", handleGrowthBarInteract);
    $("totalKrwGrowthSvgWrap").addEventListener("mouseover", handleGrowthBarInteract);
    $("totalKrwGrowthSvgWrap").addEventListener("click", handleGrowthBarInteract);

    $("statusFilterGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn) return;
      document.querySelectorAll("#statusFilterGroup .seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.statusFilter = btn.dataset.status;
      renderDashboard();
    });

    $("marketFilterGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn) return;
      document.querySelectorAll("#marketFilterGroup .seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.marketFilter = btn.dataset.market;
      renderDashboard();
    });

    $("stockSearchInput").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderDashboard();
    });

    $("sortSelect").addEventListener("change", (e) => {
      state.sortBy = e.target.value;
      renderDashboard();
    });

    $("stockTableBody").addEventListener("click", (e) => {
      const row = e.target.closest(".stock-row");
      if (!row) return;
      openDetailDrawer(row.dataset.symbol);
    });

    $("closeDetailBtn").addEventListener("click", closeDetailDrawer);
    $("detailBackdrop").addEventListener("click", (e) => {
      if (e.target === $("detailBackdrop")) closeDetailDrawer();
    });

    $("dtabBtnDividends").addEventListener("click", () => switchDetailTab("dividends"));
    $("dtabBtnTrades").addEventListener("click", () => switchDetailTab("trades"));

    $("detailTradeFilterGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.tfilter) return;
      state.detailTradeFilter = btn.dataset.tfilter;
      if (state.activeDetailSymbol) {
        renderDetailDrawer(state.activeDetailSymbol);
      }
    });

    $("qDivGross").addEventListener("input", () => {
      if ($("qDivIsPrior").checked) {
        const g = parseFloat($("qDivGross").value) || 0;
        const t = parseFloat($("qDivTax").value) || 0;
        $("qDivNet").value = g - t;
      } else {
        autoCalcQuickDivFromGross();
      }
    });
    $("qDivTax").addEventListener("input", () => {
      const g = parseFloat($("qDivGross").value) || 0;
      const t = parseFloat($("qDivTax").value) || 0;
      const item = getActiveDetailItem();
      if (g > 0 && item) {
        $("qDivNet").value = item.currency === "KRW" ? Math.round(g - t) : Number((g - t).toFixed(2));
      }
    });
    $("qDivNet").addEventListener("input", () => autoCalcQuickDivFromNet());
    $("presetTaxDefaultBtn").addEventListener("click", () => autoCalcQuickDivFromGross());
    $("presetTaxZeroBtn").addEventListener("click", () => autoCalcQuickDivFromGross(0));

    $("quickDividendForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const item = getActiveDetailItem();
      if (!item) return;
      const gross = parseFloat($("qDivGross").value) || 0;
      const tax = parseFloat($("qDivTax").value) || 0;
      const net = parseFloat($("qDivNet").value) || gross - tax;
      const isPrior = $("qDivIsPrior").checked;
      try {
        await apiFetch("/api/dividends", {
          method: "POST",
          body: JSON.stringify({
            accountKey: getActiveAccountKey(),
            symbol: item.symbol,
            name: item.name,
            market: item.market,
            currency: item.currency,
            payDate: $("qDivDate").value,
            grossAmount: gross,
            taxAmount: tax,
            netAmount: net,
            isPriorBroker: isPrior,
            memo: $("qDivMemo").value.trim(),
          }),
        });
        $("qDivGross").value = "";
        $("qDivTax").value = "";
        $("qDivNet").value = "";
        $("qDivMemo").value = "";
        $("qDivIsPrior").checked = false;
        showToast(`${item.name} 배당 내역이 저장되었습니다.`);
        await loadPortfolio();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("detailDividendsTbody").addEventListener("click", async (e) => {
      const editBtn = e.target.closest("[data-edit-div]");
      if (editBtn) {
        const item = getActiveDetailItem();
        if (!item) return;
        const curGross = parseFloat(editBtn.dataset.gross) || 0;
        const curTax = parseFloat(editBtn.dataset.tax) || 0;
        const unit = item.currency === "KRW" ? "원" : "USD";
        const newGrossStr = window.prompt(`세전 배당금(${unit})을 확인·수정해주세요:`, String(curGross));
        if (newGrossStr === null) return;
        const newTaxStr = window.prompt(
          `배당 세액(${unit})을 입력해주세요 (비과세·세금미반영은 0):`,
          String(curTax)
        );
        if (newTaxStr === null) return;

        const g = parseFloat(newGrossStr.replace(/,/g, ""));
        const t = parseFloat(newTaxStr.replace(/,/g, ""));
        if (isNaN(g) || isNaN(t)) {
          showToast("올바른 숫자를 입력해주세요.", true);
          return;
        }
        const n = item.currency === "KRW" ? Math.round(g - t) : Number((g - t).toFixed(2));
        try {
          await apiFetch("/api/dividends", {
            method: "PUT",
            body: JSON.stringify({
              accountKey: getActiveAccountKey(),
              id: Number(editBtn.dataset.editDiv),
              grossAmount: g,
              taxAmount: t,
              netAmount: n,
            }),
          });
          showToast("배당금·세액이 수정 저장되었습니다.");
          await loadPortfolio();
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const btn = e.target.closest("[data-del-div]");
      if (!btn) return;
      if (!window.confirm("이 배당 기록을 삭제하시겠습니까?")) return;
      try {
        await apiFetch(`/api/dividends?id=${btn.dataset.delDiv}&accountKey=${encodeURIComponent(getActiveAccountKey())}`, {
          method: "DELETE",
        });
        showToast("배당 기록이 삭제되었습니다.");
        await loadPortfolio();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("openAddRecordModalBtn").addEventListener("click", () => {
      $("gRecDate").value = new Date().toISOString().slice(0, 10);
      $("gDivTax").value = "0";
      $("addRecordModal").classList.remove("hidden");
    });
    $("openBulkImportModalBtn").addEventListener("click", () => openPdfManagerModal());
    $("openPdfUploadFromSetupBtn")?.addEventListener("click", () => openPdfManagerModal());
    $("bannerUploadPdfBtn")?.addEventListener("click", () => openPdfManagerModal());

    const dropZone = $("pdfDropZone");
    const pdfInput = $("pdfUploadInput");
    if (dropZone && pdfInput) {
      dropZone.addEventListener("click", () => pdfInput.click());
      dropZone.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pdfInput.click();
        }
      });
      dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
      });
      dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
      dropZone.addEventListener("drop", async (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer?.files?.length) {
          await handlePdfFilesUpload(e.dataTransfer.files);
        }
      });
      pdfInput.addEventListener("change", async () => {
        if (pdfInput.files?.length) {
          await handlePdfFilesUpload(pdfInput.files);
          pdfInput.value = "";
        }
      });
    }

    $("pdfFileChipsWrap")?.addEventListener("click", async (e) => {
      const delBtn = e.target.closest("[data-del-pdf-name]");
      if (!delBtn) return;
      const fName = delBtn.dataset.delPdfName;
      if (!window.confirm(`'${fName}' 파일을 pdfs/ 폴더에서 삭제하시겠습니까?\n(삭제 후 필요 시 'PDF 폴더 재반영'을 눌러 원장을 갱신하세요.)`)) return;
      try {
        await apiFetch(`/api/pdf/file?name=${encodeURIComponent(fName)}`, { method: "DELETE" });
        showToast(`'${fName}' 파일이 삭제되었습니다.`);
        await refreshPdfFolderStatus();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    document.querySelectorAll("[data-close-modal]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $(btn.dataset.closeModal).classList.add("hidden");
      });
    });

    $("gDivGross").addEventListener("input", () => {
      if ($("gRecIsPrior").checked) return;
      const g = parseFloat($("gDivGross").value) || 0;
      if (g <= 0) return;
      const mkt = $("gRecMarket").value;
      const rate = mkt === "KR" ? 0.154 : 0.15;
      $("gDivTax").value = mkt === "KR" ? Math.floor((g * rate) / 10) * 10 : Number((g * rate).toFixed(2));
    });

    $("gRecIsPrior").addEventListener("change", (e) => {
      if (e.target.checked) {
        $("gDivTax").value = "0";
      }
    });

    $("globalRecordForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const sym = $("gRecSymbol").value.trim().toUpperCase();
      const name = $("gRecName").value.trim();
      const mkt = $("gRecMarket").value;
      const cur = mkt === "KR" ? "KRW" : "USD";
      const dt = $("gRecDate").value;
      const isPrior = $("gRecIsPrior").checked;
      const memo = $("gRecMemo").value.trim();
      const gross = parseFloat($("gDivGross").value) || 0;
      const tax = parseFloat($("gDivTax").value) || 0;

      try {
        await apiFetch("/api/dividends", {
          method: "POST",
          body: JSON.stringify({
            accountKey: getActiveAccountKey(),
            symbol: sym,
            name,
            market: mkt,
            currency: cur,
            payDate: dt,
            grossAmount: gross,
            taxAmount: tax,
            netAmount: gross - tax,
            isPriorBroker: isPrior,
            memo,
          }),
        });
        $("globalRecordForm").reset();
        $("addRecordModal").classList.add("hidden");
        showToast(`${name} 배당 내역이 저장되었습니다.`);
        await loadPortfolio();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("runBulkImportBtn").addEventListener("click", async () => {
      const raw = $("bulkPasteTextarea").value.trim();
      if (!raw) {
        showToast("붙여넣을 배당 텍스트를 입력해주세요.", true);
        return;
      }
      const parsed = parseBulkDividendLines(raw);
      if (parsed.dividends.length === 0) {
        showToast("인식된 배당 줄이 없습니다. 예시 형식을 확인해주세요.", true);
        return;
      }
      try {
        const res = await apiFetch("/api/import/batch", {
          method: "POST",
          body: JSON.stringify({ accountKey: getActiveAccountKey(), ...parsed }),
        });
        $("bulkPasteTextarea").value = "";
        $("bulkImportModal").classList.add("hidden");
        showToast(`배당 일괄 등록 완료: ${res.insertedDividends}건`);
        await loadPortfolio();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("exportJsonBtn").addEventListener("click", async () => {
      try {
        const qs = `?accountKey=${encodeURIComponent(getActiveAccountKey())}`;
        const data = await apiFetch(`/api/export${qs}`);
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `toss-dividend-backup-${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("syncLogFilterGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.logfilter) return;
      state.syncLogFilter = btn.dataset.logfilter;
      document.querySelectorAll("#syncLogFilterGroup .seg-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.logfilter === state.syncLogFilter);
      });
      renderSyncLogList();
    });

    $("clearAllSyncLogsBtn").addEventListener("click", async () => {
      if (!window.confirm("저장된 동기화·배당 갱신 로그 이력을 모두 비우시겠습니까?\n(포트폴리오 원장과 배당·체결 데이터는 삭제되지 않습니다.)")) return;
      try {
        await apiFetch(`/api/sync-logs?all=1&accountKey=${encodeURIComponent(getActiveAccountKey())}`, {
          method: "DELETE",
        });
        state.syncLogs = [];
        renderSyncLogList();
        showToast("동기화 로그 이력이 비워졌습니다.");
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("syncLogListWrap").addEventListener("click", async (e) => {
      const symBtn = e.target.closest("[data-open-sym-from-log]");
      if (symBtn) {
        const sym = symBtn.dataset.openSymFromLog;
        const targetTab = symBtn.dataset.openTab || "dividends";
        $("syncLogModal").classList.add("hidden");
        openDetailDrawer(sym);
        switchDetailTab(targetTab);
        return;
      }
      const delBtn = e.target.closest("[data-del-synclog]");
      if (delBtn) {
        try {
          const logId = delBtn.dataset.delSynclog;
          await apiFetch(`/api/sync-logs?id=${logId}&accountKey=${encodeURIComponent(getActiveAccountKey())}`, {
            method: "DELETE",
          });
          state.syncLogs = state.syncLogs.filter((x) => String(x.id) !== String(logId));
          renderSyncLogList();
        } catch (err) {
          showToast(err.message, true);
        }
      }
    });
  }

  function markNewSyncLogBadge() {
    const dot = $("syncLogNewBadge");
    if (dot) dot.classList.remove("hidden");
  }

  async function openSyncLogModal(initialFilter = null) {
    if (initialFilter) {
      state.syncLogFilter = initialFilter;
      document.querySelectorAll("#syncLogFilterGroup .seg-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.logfilter === state.syncLogFilter);
      });
    }
    const dot = $("syncLogNewBadge");
    if (dot) dot.classList.add("hidden");
    $("syncLogModal").classList.remove("hidden");
    $("syncLogListWrap").innerHTML = `<div class="empty-state" style="padding:32px;">로그 불러오는 중...</div>`;
    try {
      const qs = `?accountKey=${encodeURIComponent(getActiveAccountKey())}&limit=100`;
      const data = await apiFetch(`/api/sync-logs${qs}`);
      state.syncLogs = data.logs || [];
      renderSyncLogList();
    } catch (err) {
      $("syncLogListWrap").innerHTML = `<div class="empty-state" style="padding:32px;">${escapeHtml(err.message)}</div>`;
    }
  }

  function renderSyncLogDivTable(divList, titleText) {
    if (!divList || divList.length === 0) return "";
    const rowsHtml = divList
      .map((d) => {
        const cur = d.currency || "USD";
        const krwHint =
          cur === "USD" && d.krwNet
            ? ` <span style="font-size:11.5px;color:var(--text-muted);">(${formatMoney(d.krwNet, "KRW")})</span>`
            : "";
        return `
          <tr>
            <td style="white-space:nowrap;font-weight:700;">${escapeHtml(d.payDate)}</td>
            <td>
              <button type="button" class="log-sym-link" data-open-sym-from-log="${escapeHtml(d.symbol)}" data-open-tab="dividends">
                ${escapeHtml(d.name || d.symbol)} <span style="font-size:11.5px;color:var(--text-muted);">(${escapeHtml(d.symbol)})</span>
              </button>
            </td>
            <td>${formatMoney(d.grossAmount, cur)}</td>
            <td style="color:var(--text-secondary);">${formatMoney(d.taxAmount, cur)}</td>
            <td class="val-div" style="font-weight:800;">${formatMoney(d.netAmount, cur, true)}${krwHint}</td>
            <td style="text-align:left;font-size:12px;color:var(--text-secondary);">${escapeHtml(d.memo || "")}</td>
          </tr>
        `;
      })
      .join("");

    return `
      <div class="sync-log-subbox">
        <div class="sync-log-subbox-title">${escapeHtml(titleText)} (${divList.length}건)</div>
        <div style="overflow-x:auto;">
          <table class="history-table" style="font-size:12.5px;">
            <thead>
              <tr>
                <th>지급일</th>
                <th style="text-align:left;">종목 (클릭 시 상세)</th>
                <th>세전</th>
                <th>세금</th>
                <th>세후 실수령액</th>
                <th style="text-align:left;">산출 내역</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderSyncLogTradeTable(trList, titleText) {
    if (!trList || trList.length === 0) return "";
    const rowsHtml = trList
      .map((t) => {
        const cur = t.currency || "USD";
        const isBuy = t.side === "BUY";
        return `
          <tr>
            <td style="white-space:nowrap;font-weight:700;">${escapeHtml(t.tradeDate)}</td>
            <td><span class="badge ${isBuy ? "badge-buy" : "badge-sell"}">${isBuy ? "매수" : "매도"}</span></td>
            <td style="text-align:left;">
              <button type="button" class="log-sym-link" data-open-sym-from-log="${escapeHtml(t.symbol)}" data-open-tab="trades">
                ${escapeHtml(t.name || t.symbol)} <span style="font-size:11.5px;color:var(--text-muted);">(${escapeHtml(t.symbol)})</span>
              </button>
            </td>
            <td>${formatQty(t.quantity)}</td>
            <td>${formatPrice(t.price, cur)}</td>
            <td style="font-weight:700;">${formatMoney(t.grossAmount, cur)}</td>
          </tr>
        `;
      })
      .join("");

    return `
      <div class="sync-log-subbox">
        <div class="sync-log-subbox-title">${escapeHtml(titleText)} (${trList.length}건)</div>
        <div style="overflow-x:auto;">
          <table class="history-table" style="font-size:12.5px;">
            <thead>
              <tr>
                <th>체결일</th>
                <th>구분</th>
                <th style="text-align:left;">종목 (클릭 시 상세)</th>
                <th>체결수량</th>
                <th>체결단가</th>
                <th>체결금액</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderSyncLogList() {
    const wrap = $("syncLogListWrap");
    const filtered = (state.syncLogs || []).filter(
      (x) => state.syncLogFilter === "ALL" || x.actionType === state.syncLogFilter
    );

    if (filtered.length === 0) {
      wrap.innerHTML = `<div class="empty-state" style="padding:36px;">기록된 동기화·배당 갱신 로그가 없습니다.</div>`;
      return;
    }

    wrap.innerHTML = filtered
      .map((log) => {
        const isErr = log.status === "ERROR";
        const typeBadge =
          log.actionType === "TOSS_SYNC"
            ? `<span class="badge badge-held">↻ 토스 API 증분 동기화</span>`
            : log.actionType === "PDF_IMPORT"
            ? `<span class="badge badge-transfer">📄 PDF 원장 반영</span>`
            : `<span class="badge badge-auto">💸 이후 배당 갱신</span>`;
        const statusBadge = isErr ? `<span class="badge badge-buy">오류</span>` : "";
        const det = log.details || {};
        const addedTr = det.addedTrades || [];
        const updatedTr = det.updatedTradesList || [];
        const addedDiv = det.newlyAddedDividends || [];
        const retainedDiv = det.retainedAutoDividends || [];
        const uploadedFiles = det.uploadedFiles || [];

        let bodyHtml = "";
        if (isErr && det.error) {
          bodyHtml += `<div class="sync-log-subbox" style="color:var(--up-red);font-size:12.5px;font-weight:600;">${escapeHtml(det.error)}</div>`;
        }
        if (log.actionType === "PDF_IMPORT" && uploadedFiles.length > 0) {
          bodyHtml += `<div class="sync-log-subbox" style="font-size:12.5px;color:var(--text-secondary);"><strong>업로드 파일:</strong> ${uploadedFiles.map((f) => escapeHtml(f)).join(", ")}</div>`;
        }
        bodyHtml += renderSyncLogTradeTable(addedTr, "📈 신규 반영된 매매 체결 내역");
        bodyHtml += renderSyncLogTradeTable(updatedTr, "🔄 수량·금액 변경 반영된 체결 내역");
        bodyHtml += renderSyncLogDivTable(addedDiv, "💸 새로 추가된 이후 배당 내역");

        if (retainedDiv.length > 0) {
          bodyHtml += `
            <details style="margin-top:8px;">
              <summary style="cursor:pointer;font-size:12px;font-weight:700;color:var(--text-secondary);padding:4px 2px;">
                📌 기존 반영되어 유지 중인 이후 자동배당 (${retainedDiv.length}건) 펼쳐보기
              </summary>
              ${renderSyncLogDivTable(retainedDiv, "유지 중인 기준일 이후 자동배당")}
            </details>
          `;
        }

        if (!isErr && log.actionType !== "PDF_IMPORT" && addedTr.length === 0 && updatedTr.length === 0 && addedDiv.length === 0 && retainedDiv.length === 0) {
          bodyHtml += `<div style="font-size:12.5px;color:var(--text-muted);">새로 추가되거나 변경된 체결·배당 항목이 없습니다.</div>`;
        }

        return `
          <div class="sync-log-card ${isErr ? "is-error" : ""}">
            <div class="sync-log-card-top">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                ${typeBadge}
                ${statusBadge}
                <span style="font-size:12.5px;color:var(--text-muted);font-weight:700;">${escapeHtml(log.createdAt)}</span>
              </div>
              <button type="button" class="btn btn-danger-ghost" style="padding:2px 8px;font-size:11.5px;" data-del-synclog="${log.id}" title="이 로그 삭제">✕</button>
            </div>
            <div class="sync-log-summary">${escapeHtml(log.summary)}</div>
            ${bodyHtml}
          </div>
        `;
      })
      .join("");
  }

  async function openPdfManagerModal() {
    $("bulkImportModal").classList.remove("hidden");
    await refreshPdfFolderStatus();
  }

  async function refreshPdfFolderStatus() {
    const countEl = $("pdfFolderCountLabel");
    const chipsEl = $("pdfFileChipsWrap");
    if (!countEl || !chipsEl) return;
    try {
      const st = await apiFetch("/api/pdf/status");
      const cnt = st.count || 0;
      const cutoffTxt = st.pdfCutoffDate ? ` · 확정 기준일 ~${st.pdfCutoffDate}` : "";
      countEl.textContent = cnt > 0
        ? `📁 현재 등록된 거래내역서 PDF: ${cnt}개${cutoffTxt}`
        : "📁 현재 등록된 거래내역서 PDF: 0개 (위 영역을 눌러 PDF를 업로드해주세요)";
      if (cnt === 0) {
        chipsEl.innerHTML = "";
      } else {
        chipsEl.innerHTML = (st.files || [])
          .map((f) => {
            const kb = Math.max(1, Math.round((f.sizeBytes || 0) / 1024));
            return `<span class="pdf-file-chip">📄 ${escapeHtml(f.name)} <span style="color:var(--text-muted);">(${kb.toLocaleString("ko-KR")}KB)</span><button type="button" data-del-pdf-name="${escapeHtml(f.name)}" title="파일 삭제">✕</button></span>`;
          })
          .join("");
      }
    } catch {
      countEl.textContent = "📁 거래내역서 PDF 폴더 (pdfs/)";
    }
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const res = String(reader.result || "");
        const commaIdx = res.indexOf(",");
        resolve(commaIdx >= 0 ? res.slice(commaIdx + 1) : res);
      };
      reader.onerror = () => reject(new Error(`파일 읽기 실패: ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function handlePdfFilesUpload(fileList) {
    const pdfFiles = Array.from(fileList || []).filter(
      (f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf"
    );
    if (pdfFiles.length === 0) {
      showToast("PDF(.pdf) 형식의 거래내역서 파일만 업로드할 수 있습니다.", true);
      return;
    }

    const titleEl = $("pdfDropZoneTitle");
    const origTitle = titleEl ? titleEl.textContent : "";
    if (titleEl) {
      titleEl.textContent = `⏳ 거래내역서 PDF ${pdfFiles.length}개 업로드 및 원장 분석 중... 잠시만 기다려주세요`;
    }
    try {
      const encodedFiles = [];
      for (const f of pdfFiles) {
        const contentBase64 = await readFileAsBase64(f);
        encodedFiles.push({ name: f.name, contentBase64 });
      }
      const res = await apiFetch("/api/pdf/upload", {
        method: "POST",
        body: JSON.stringify({
          accountKey: getActiveAccountKey(),
          autoImport: true,
          files: encodedFiles,
        }),
      });
      await refreshPdfFolderStatus();
      markNewSyncLogBadge();
      const imp = res.importResult;
      if (imp) {
        const fromSetup = !$("setupScreen").classList.contains("hidden");
        showToast(
          `거래내역서 PDF 반영 완료! (~${imp.cutoffDate} · ${imp.totalCanonicalStocks}종목 · 거래 ${imp.tradesInserted}건 · 배당 ${imp.dividendsInserted}건)`,
          false,
          fromSetup
            ? {
                actionLabel: "대시보드 바로 열기 →",
                onAction: () => {
                  $("bulkImportModal").classList.add("hidden");
                  state.isLedgerOnlyPreview = true;
                  routeScreen();
                },
              }
            : undefined
        );
        if (!$("dashboardScreen").classList.contains("hidden")) {
          await loadPortfolio();
        }
      } else {
        showToast(`PDF 파일 ${res.savedFiles.length}개가 저장되었습니다.`);
      }
    } catch (err) {
      showToast(err.message, true);
    } finally {
      if (titleEl) {
        titleEl.textContent = origTitle || "📂 클릭하거나 토스증권 거래내역서 PDF 파일들을 여기에 끌어다 놓으세요";
      }
    }
  }

  bindEvents();
  initVaultState();
})();
