/* MemeRadar dashboard client */
(() => {
  "use strict";

  // ---------- formatters ----------
  const fmtUsd = (n, d = 2) =>
    (n < 0 ? "-$" : "$") + Math.abs(Number(n) || 0).toLocaleString("en-US",
      { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtCompact = (n) => {
    n = Number(n) || 0;
    const a = Math.abs(n), s = n < 0 ? "-" : "";
    if (a >= 1e9) return s + "$" + (a / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return s + "$" + (a / 1e3).toFixed(1) + "K";
    return s + "$" + a.toFixed(0);
  };
  const fmtPct = (n) => (Number(n) >= 0 ? "+" : "") + (Number(n) || 0).toFixed(1) + "%";
  const fmtPrice = (n) => {
    n = Number(n) || 0;
    if (n === 0) return "$0";
    if (n < 0.00001) return "$" + n.toExponential(2);
    if (n < 1) return "$" + n.toPrecision(3);
    return "$" + n.toLocaleString("en-US", { maximumFractionDigits: 4 });
  };
  const fmtFollowers = (n) => {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
    return String(n);
  };
  const cls = (n) => (Number(n) > 0 ? "pos" : Number(n) < 0 ? "neg" : "mut");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const ago = (ts) => {
    const s = Math.max(0, Date.now() / 1000 - (ts || 0));
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  };
  const held = (s) => {
    s = Number(s) || 0;
    if (s < 60) return Math.floor(s) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
  };
  const bar = (kind, val) => {
    const v = Math.max(0, Math.min(100, Number(val) || 0));
    return `<span class="bar ${kind}"><span style="width:${v}%"></span><b>${v.toFixed(0)}</b></span>`;
  };

  // ---------- state ----------
  let equityChart = null;
  let lastSnapshot = null;
  let board = [];
  let walletListData = [];
  let chain = "solana";
  let activeTab = "overview";
  let selectedPair = null;

  // ---------- KPI rendering ----------
  function renderKpis(p) {
    const start = p.starting_balance || 10000;
    document.getElementById("kEquity").textContent = fmtUsd(p.equity);
    document.getElementById("kEquitySub").textContent = "starting " + fmtUsd(start, 0);
    const pnl = document.getElementById("kPnl");
    pnl.textContent = (p.total_pnl >= 0 ? "+" : "") + fmtUsd(p.total_pnl);
    pnl.className = "kpi-value " + cls(p.total_pnl);
    const roi = document.getElementById("kRoi");
    roi.textContent = "ROI " + fmtPct(p.roi_pct);
    roi.className = "kpi-sub " + cls(p.roi_pct);
    const rz = document.getElementById("kRealized");
    rz.textContent = (p.realized_pnl >= 0 ? "+" : "") + fmtUsd(p.realized_pnl);
    rz.className = "kpi-value " + cls(p.realized_pnl);
    const ur = document.getElementById("kUnreal");
    ur.textContent = "unrealized " + (p.unrealized_pnl >= 0 ? "+" : "") + fmtUsd(p.unrealized_pnl);
    ur.className = "kpi-sub " + cls(p.unrealized_pnl);
    document.getElementById("kWin").textContent = (p.win_rate || 0).toFixed(0) + "%";
    document.getElementById("kTrades").textContent =
      `${p.total_trades} trades · ${p.wins}W/${p.losses}L`;
    document.getElementById("kOpen").textContent = p.open_positions;
    document.getElementById("kCash").textContent = "cash " + fmtUsd(p.cash, 0);
    document.getElementById("kDeployed").textContent = fmtUsd(Math.max(0, p.equity - p.cash), 0);
    const tc = document.getElementById("tradeCash");
    if (tc) tc.textContent = "cash " + fmtUsd(p.cash, 0);
  }

  // ---------- positions ----------
  function renderPositions(positions) {
    const tb = document.querySelector("#positionsTable tbody");
    document.getElementById("posHint").textContent = `${positions.length} open · paper`;
    if (!positions.length) {
      tb.innerHTML = `<tr><td colspan="8" class="empty">No open positions.</td></tr>`;
      return;
    }
    tb.innerHTML = positions.map((p) => {
      let guard;
      if (p.trailing_armed) {
        const off = p.peak_price ? ((p.peak_price - p.last_price) / p.peak_price * 100) : 0;
        guard = `<span class="guard">trailing · <b>${off.toFixed(1)}%</b> off peak</span>`;
      } else {
        guard = `<span class="guard">arming trail @ +12%</span>`;
      }
      return `<tr>
        <td><div class="tok"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.symbol)}</a>
          <small>${esc((p.entry_reason||"").slice(0,22))} · peak ${fmtPrice(p.peak_price)}</small></div></td>
        <td class="num">${fmtUsd(p.entry_value, 0)}</td>
        <td class="num">${fmtPrice(p.entry_price)}</td>
        <td class="num">${fmtPrice(p.last_price)}</td>
        <td class="num ${cls(p.unrealized_pnl)}">${fmtUsd(p.unrealized_pnl)}<br>
          <small>${fmtPct(p.unrealized_pnl_pct)}</small></td>
        <td>${guard}</td>
        <td class="num mut">${held(p.hold_seconds)}</td>
        <td><div class="sell-btns">
          <button data-sell="${esc(p.address)}" data-frac="0.25">25%</button>
          <button data-sell="${esc(p.address)}" data-frac="0.5">50%</button>
          <button data-sell="${esc(p.address)}" data-frac="1">All</button>
        </div></td>
      </tr>`;
    }).join("");
  }

  // ---------- opportunity board ----------
  function renderBoard(b) {
    const tb = document.querySelector("#boardTable tbody");
    if (!b || !b.length) {
      tb.innerHTML = `<tr><td colspan="14" class="empty">Scanning Solana memecoins…</td></tr>`;
      return;
    }
    tb.innerHTML = b.slice(0, 40).map((row) => {
      const t = row.token, d = row.detection, i = row.intel;
      let sig = "";
      if (i.confirmed_multi_sell) sig = `<span class="tag-flag">multi-sell ${i.multi_sell_wallets}w</span>`;
      else if (i.smart_inflow_score > 35) sig = `<span class="badge smart_money">smart in</span>`;
      else if (i.smart_inflow_score < -35) sig = `<span class="badge distribution">smart out</span>`;
      const flags = (d.flags || []).filter(f => ["mint_authority","freeze_authority","thin_liquidity","high_churn","rollover","sell_pressure","supply_overhang"].includes(f))
        .slice(0, 3).map(f => `<span class="tag-flag">${f.replace(/_/g," ")}</span>`).join("");
      const ch = (w) => {
        const v = t.price_change?.[w] ?? 0;
        return `<td class="num ${cls(v)}">${fmtPct(v)}</td>`;
      };
      return `<tr data-addr="${esc(t.address)}" title="Click to open ${esc(t.symbol)}">
        <td><div class="tok"><span class="link-sym">${esc(t.symbol)}</span>
          <small>${esc((t.name||"").slice(0,18))} · ${Math.round(t.age_minutes)}m</small></div></td>
        <td><span class="badge ${esc(d.phase)}">${esc(d.phase)}</span></td>
        <td class="num">${fmtPrice(t.price_usd)}</td>
        ${ch("m5")}${ch("h1")}${ch("h6")}
        <td class="num mut">${fmtCompact(t.liquidity_usd)}</td>
        <td class="num mut">${fmtCompact(t.volume?.h1 || 0)}</td>
        <td class="num">${bar("pump", d.pump_score)}</td>
        <td class="num">${bar("dump", d.dump_risk)}</td>
        <td class="num">${bar("safe", d.safety)}</td>
        <td class="num ${cls(i.smart_inflow_usd)}">${fmtCompact(i.smart_inflow_usd)}</td>
        <td class="num">${bar("opp", d.opportunity)}</td>
        <td>${sig}${flags}</td>
      </tr>`;
    }).join("");
  }

  // ---------- signals ----------
  const SIG_ICON = { pump: "▲", dump: "▼", multi_sell: "⚠", whale_buy: "◆", entry: "▶", exit: "■", bundle: "📦" };
  function renderSignals(signals) {
    const el = document.getElementById("signalsFeed");
    if (!signals || !signals.length) { el.innerHTML = `<div class="empty">Waiting for signals…</div>`; return; }
    el.innerHTML = signals.slice(0, 60).map((s) => `
      <div class="sig ${esc(s.kind)} ${esc(s.severity)}">
        <div class="ic">${SIG_ICON[s.kind] || "•"}</div>
        <div class="body"><div class="msg">${esc(s.message)}</div>
          <div class="time">${ago(s.ts)}</div></div>
      </div>`).join("");
  }

  // ---------- overview watchlist ----------
  function renderWallets(wallets) {
    const tb = document.querySelector("#walletsTable tbody");
    if (!tb) return;  // overview watchlist removed in the reorg
    if (!wallets || !wallets.length) {
      tb.innerHTML = `<tr><td colspan="4" class="empty">Discovering wallets…</td></tr>`; return;
    }
    tb.innerHTML = wallets.map((w) => `
      <tr class="wallet-link" data-wallet="${esc(w.wallet)}">
        <td><code class="mut">${esc(w.wallet_short)}</code><br><small class="mut">${esc((w.tokens||[]).slice(0,3).join(" "))}</small></td>
        <td><span class="badge ${esc(w.kind)}">${esc(w.kind.replace("_"," "))}</span></td>
        <td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>
        <td class="num mut">${w.events}</td>
      </tr>`).join("");
  }

  // ---------- trades ----------
  function renderTrades(trades) {
    const tb = document.querySelector("#tradesTable tbody");
    document.getElementById("tradeHint").textContent = `${trades.length} closed · paper`;
    if (!trades || !trades.length) {
      tb.innerHTML = `<tr><td colspan="8" class="empty">No closed trades yet.</td></tr>`; return;
    }
    tb.innerHTML = trades.map((t) => `
      <tr>
        <td><div class="tok"><strong>${esc(t.symbol)}</strong><small>${esc(t.entry_reason||"")}</small></div></td>
        <td class="num">${fmtPrice(t.entry_price)}</td>
        <td class="num">${fmtPrice(t.exit_price)}</td>
        <td class="num">${fmtUsd(t.entry_value, 0)}</td>
        <td class="num ${cls(t.pnl)}">${fmtUsd(t.pnl)}</td>
        <td class="num ${cls(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td class="mut">${esc(t.exit_reason)}</td>
        <td class="num mut">${ago(t.closed_at)}</td>
      </tr>`).join("");
  }

  // ---------- equity chart ----------
  function renderEquity(points) {
    const canvas = document.getElementById("equityChart");
    if (!canvas || typeof Chart === "undefined") return;
    const labels = points.map((p) => new Date(p.ts * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }));
    const data = points.map((p) => p.equity);
    const start = (points[0] && points[0].equity) || 10000;
    const last = data[data.length - 1] || start;
    const up = last >= start;
    document.getElementById("equityHint").textContent =
      points.length ? `${fmtUsd(last)} · ${fmtPct((last - start) / start * 100)}` : "";
    if (!equityChart) {
      const ctx = canvas.getContext("2d");
      const grad = ctx.createLinearGradient(0, 0, 0, 240);
      grad.addColorStop(0, "rgba(25,227,164,.28)");
      grad.addColorStop(1, "rgba(25,227,164,0)");
      equityChart = new Chart(ctx, {
        type: "line",
        data: { labels, datasets: [{ data, borderColor: up ? "#19e3a4" : "#ff5267", borderWidth: 2,
          fill: true, backgroundColor: grad, tension: .28, pointRadius: 0, pointHoverRadius: 4 }] },
        options: { responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false,
              callbacks: { label: (c) => "Equity " + fmtUsd(c.parsed.y) } } },
          scales: { x: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#566077", maxTicksLimit: 8, font: { size: 10 } } },
            y: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#7d8ba3", font: { size: 10 }, callback: (v) => "$" + (v / 1000).toFixed(1) + "k" } } } },
      });
    } else {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = data;
      equityChart.data.datasets[0].borderColor = up ? "#19e3a4" : "#ff5267";
      equityChart.update("none");
    }
  }

  // ---------- status ----------
  function renderStatus(st) {
    chain = st.chain || chain;
    if (st.scan_count !== cacheScan) { profileCache.clear(); cacheScan = st.scan_count; }
    document.getElementById("provider").textContent = "provider: " + (st.wallet_provider || "—");
    document.getElementById("footProvider").textContent = `${st.chain} · ${st.wallet_provider} wallet feed`;
    document.getElementById("scanInfo").textContent = `scan #${st.scan_count}` + (st.last_error ? " · err" : "");
    const btn = document.getElementById("btnPause");
    btn.textContent = st.paused ? "Resume" : "Pause";
    btn.classList.toggle("active", st.paused);
  }

  // ================= CHARTS TAB =================
  let selectedCoinAddr = null;
  let chartView = "profile";

  function chartEmbedUrl(ch, pair) {
    return `https://dexscreener.com/${encodeURIComponent(ch)}/${encodeURIComponent(pair)}` +
      `?embed=1&theme=dark&trades=0&info=0`;
  }
  function boardByAddr(addr) { return board.find((b) => b.token.address === addr); }

  function renderCoinList(filter) {
    const el = document.getElementById("coinList");
    const q = (filter || "").trim().toLowerCase();
    let items = board;
    if (q) items = board.filter((b) => (b.token.symbol + " " + b.token.name).toLowerCase().includes(q));
    document.getElementById("coinCount").textContent = `${board.length} coins`;
    if (!items.length) { el.innerHTML = `<div class="empty">No coins.</div>`; return; }
    el.innerHTML = items.slice(0, 80).map((b) => {
      const t = b.token, d = b.detection;
      const chg = t.price_change?.h1 ?? 0;
      return `<div class="coin-row" data-addr="${esc(t.address)}">
        <div class="coin-main"><div class="c-sym">${esc(t.symbol)}</div><div class="c-name">${esc((t.name||"").slice(0,20))}</div></div>
        <div class="coin-right"><div class="c-chg ${cls(chg)}">${fmtPct(chg)}</div>
          <div class="c-pump">P${Math.round(d.pump_score)} · D${Math.round(d.dump_risk)}</div></div>
        <button class="coin-chart-btn" data-chartaddr="${esc(t.address)}" title="Open chart">📈</button>
      </div>`;
    }).join("");
    el.querySelectorAll(".coin-row").forEach((r) => {
      r.addEventListener("click", (e) => {
        if (e.target.closest(".coin-chart-btn")) { selectCoin(r.dataset.addr, "chart"); return; }
        selectCoin(r.dataset.addr, "profile");
      });
      if (r.dataset.addr === selectedCoinAddr) r.classList.add("active");
    });
  }

  function selectCoin(addr, view) {
    const entry = boardByAddr(addr);
    if (!entry) return;
    selectedCoinAddr = addr;
    chartView = view || "profile";
    const t = entry.token;
    document.getElementById("chartTitle").innerHTML =
      `${esc(t.symbol)} <span class="ct-name">${esc((t.name || "").slice(0, 24))}</span>`;
    const toggle = document.getElementById("chartViewToggle");
    toggle.style.display = "";
    document.getElementById("dexLink").href = t.url || "#";
    toggle.querySelectorAll(".vbtn[data-view]").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === chartView));
    document.querySelectorAll("#coinList .coin-row").forEach((r) =>
      r.classList.toggle("active", r.dataset.addr === addr));
    renderChartBody();
  }

  function renderChartBody() {
    const body = document.getElementById("chartBody");
    const entry = selectedCoinAddr ? boardByAddr(selectedCoinAddr) : null;
    if (!entry) return;
    const t = entry.token;
    if (chartView === "chart") {
      body.innerHTML = `<div class="chart-frame-wrap"><iframe src="${esc(chartEmbedUrl(t.chain, t.pair_address))}" allow="clipboard-write" loading="lazy"></iframe></div>`;
    } else {
      body.innerHTML = renderCoinProfile(entry);
    }
  }

  function setChartView(view) {
    chartView = view;
    document.querySelectorAll("#chartViewToggle .vbtn[data-view]").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view));
    renderChartBody();
  }

  function loadChartManual(addr) {
    // no board entry for a pasted address — go straight to the chart
    selectedCoinAddr = null; chartView = "chart";
    document.getElementById("chartTitle").textContent = addr.slice(0, 12) + "…";
    document.getElementById("chartViewToggle").style.display = "none";
    document.getElementById("chartBody").innerHTML =
      `<div class="chart-frame-wrap"><iframe src="${esc(chartEmbedUrl(chain, addr))}" allow="clipboard-write" loading="lazy"></iframe></div>`;
  }

  function renderCoinProfile(b) {
    const t = b.token, d = b.detection, i = b.intel;
    const pc = t.price_change || {}, vol = t.volume || {}, tx = t.txns || {};
    const chgCell = (w) => `<div class="cp-cell"><span class="cp-lbl">${w}</span><span class="${cls(pc[w])}">${fmtPct(pc[w] || 0)}</span></div>`;
    const volCell = (w) => `<div class="cp-cell"><span class="cp-lbl">vol ${w}</span><span class="mut">${fmtCompact(vol[w] || 0)}</span></div>`;
    const txW = (w) => { const x = tx[w] || {}; return `<div class="cp-cell"><span class="cp-lbl">${w} tx</span><span><span class="up">${x.buys||0}</span>/<span class="down">${x.sells||0}</span></span></div>`; };
    const auth = (renounced, label) => renounced === false
      ? `<span class="rug-bad">⛔ ${label} authority live</span>`
      : (renounced === true ? `<span class="rug-ok">✅ ${label} renounced</span>` : `<span class="mut">${label}: ?</span>`);
    const flags = (d.flags || []).map((f) => `<span class="tag-flag">${f.replace(/_/g, " ")}</span>`).join("");
    return `<div class="coin-profile">
      <div class="cp-top">
        <div class="cp-price">${fmtPrice(t.price_usd)}<span class="cp-age">· ${Math.round(t.age_minutes)}m old</span></div>
        <span class="badge ${esc(d.phase)}">${esc(d.phase)}</span>
      </div>
      <div class="cp-grid">${chgCell("m5")}${chgCell("h1")}${chgCell("h6")}${chgCell("h24")}</div>
      <div class="cp-stats">
        <div class="cp-stat"><div class="lbl">Liquidity</div><div class="val">${fmtCompact(t.liquidity_usd)}</div></div>
        <div class="cp-stat"><div class="lbl">Market cap</div><div class="val">${fmtCompact(t.market_cap)}</div></div>
        <div class="cp-stat"><div class="lbl">FDV</div><div class="val">${fmtCompact(t.fdv)}</div></div>
        <div class="cp-stat"><div class="lbl">Vol 24h</div><div class="val">${fmtCompact(vol.h24 || 0)}</div></div>
      </div>
      <div class="cp-grid">${volCell("m5")}${volCell("h1")}${volCell("h6")}${txW("h1")}</div>
      <div class="prof-section">MemeRadar intel</div>
      <div class="cp-bars">
        <div class="cp-bar"><span>Pump</span>${bar("pump", d.pump_score)}</div>
        <div class="cp-bar"><span>Dump risk</span>${bar("dump", d.dump_risk)}</div>
        <div class="cp-bar"><span>Safety</span>${bar("safe", d.safety)}</div>
        <div class="cp-bar"><span>Opportunity</span>${bar("opp", d.opportunity)}</div>
      </div>
      <div class="cp-meta">
        smart $ <b class="${cls(i.smart_inflow_usd)}">${fmtCompact(i.smart_inflow_usd)}</b> ·
        whale conc <b>${Math.round(i.whale_concentration)}%</b> ·
        whales ${i.whale_count} · insiders ${i.insider_count}
        ${i.confirmed_multi_sell ? '· <span class="rug-bad">🚨 multi-sell</span>' : ""}
      </div>
      <div class="cp-rug">${auth(t.mint_renounced, "Mint")} ${auth(t.freeze_renounced, "Freeze")}</div>
      ${flags ? `<div class="cp-flags">${flags}</div>` : ""}
      ${(d.reasons||[]).length ? `<div class="cp-reasons">${esc((d.reasons||[]).slice(0,4).join(" · "))}</div>` : ""}
      <div class="cp-actions">
        <button class="btn" onclick="" data-view="chart" id="cpChartBtn">📈 View chart</button>
        <a class="btn" href="${esc(t.url)}" target="_blank" rel="noopener">DexScreener ↗</a>
      </div>
    </div>`;
  }

  // ================= WALLETS TAB =================
  // whole row is clickable (not just the address chip)
  function walletRow(wallet, cols) {
    return `<tr class="wallet-link" data-wallet="${esc(wallet)}">${cols.join("")}</tr>`;
  }
  function renderCategorized(d) {
    const counts = d.counts || {};
    document.getElementById("walletCounts").innerHTML =
      `<span class="wc">tracked <b>${counts.tracked||0}</b></span>
       <span class="wc">whales <b>${counts.whales||0}</b></span>
       <span class="wc">insiders <b>${counts.insiders||0}</b></span>
       <span class="wc">smart money <b>${counts.smart_money||0}</b></span>
       <span class="wc">pump&amp;dump <b>${counts.pump_dumpers||0}</b></span>`;
    const setN = (id, n) => { const e = document.getElementById(id); if (e) e.textContent = n != null ? `(${n})` : ""; };
    setN("n-whales", counts.whales); setN("n-insiders", counts.insiders);
    setN("n-smart_money", counts.smart_money); setN("n-pump_dumpers", counts.pump_dumpers);

    const code = (w) => `<code class="wallet-link" data-wallet="${esc(w.wallet)}" title="${esc(w.wallet)}">${esc(w.wallet_short)}</code>`;
    const fill = (id, rows, builder) => {
      const tb = document.getElementById(id);
      if (!tb) return;
      tb.innerHTML = rows && rows.length ? rows.map(builder).join("")
        : `<tr><td colspan="5" class="empty">none yet</td></tr>`;
    };
    fill("tb-whales", d.whales, (w) => walletRow(w.wallet, [
      `<td>${code(w)}</td>`, `<td class="num mut">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${fmtCompact(w.buy_usd + w.sell_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`]));
    fill("tb-insiders", d.insiders, (w) => walletRow(w.wallet, [
      `<td>${code(w)}</td>`, `<td class="num mut">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`, `<td class="num mut">${w.events}</td>`]));
    fill("tb-smart_money", d.smart_money, (w) => walletRow(w.wallet, [
      `<td>${code(w)}</td>`, `<td class="num up">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`, `<td class="num mut">${w.events}</td>`]));
    fill("tb-pump_dumpers", d.pump_dumpers, (w) => walletRow(w.wallet, [
      `<td>${code(w)}</td>`, `<td class="num down">${w.dump_hits}</td>`,
      `<td class="num neg">${fmtCompact(w.sell_usd)}</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`]));
    renderCabals(d.cabals);
  }
  function renderCabals(cabals) {
    const el = document.getElementById("cabals");
    if (!cabals || !cabals.length) {
      el.innerHTML = `<div class="empty">No coordinated groups detected yet (needs a few scans of repeated co-selling).</div>`; return;
    }
    el.innerHTML = cabals.map((c) => `
      <div class="cabal-card">
        <div class="ch"><span class="cid cabal-link" data-cabal="${esc(c.id)}">${esc(c.id)} ›</span><span class="csz">${c.size} wallets</span></div>
        <div class="cabal-stats">
          <div class="cs"><b>${fmtCompact(c.sell_usd)}</b><span>dumped</span></div>
          <div class="cs"><b>${c.dump_hits}</b><span>co-dumps</span></div>
          <div class="cs"><b>${c.token_count}</b><span>coins</span></div>
        </div>
        <div class="cabal-members">${c.members.map((m) => `<code class="wallet-link" data-wallet="${esc(m.wallet)}" title="${esc(m.wallet)}">${esc(m.wallet_short)}</code>`).join("")}</div>
        <div class="cabal-tokens">coins: <b>${esc((c.shared_tokens||[]).join(", "))}</b></div>
      </div>`).join("");
  }

  // ---- unified, filterable/searchable wallet list ----
  const WALLET_SORTERS = {
    worth_desc: (a, b) => b.worth_usd - a.worth_usd,
    worth_asc: (a, b) => a.worth_usd - b.worth_usd,
    buy_desc: (a, b) => b.buy_usd - a.buy_usd,
    buy_asc: (a, b) => a.buy_usd - b.buy_usd,
    recent: (a, b) => b.first_seen - a.first_seen,
    oldest: (a, b) => a.first_seen - b.first_seen,
    win_desc: (a, b) => b.win_rate - a.win_rate,
    events_desc: (a, b) => b.events - a.events,
  };
  function applyWalletFilters() {
    const q = (document.getElementById("walletSearch")?.value || "").trim().toLowerCase();
    const cat = document.getElementById("walletCat")?.value || "all";
    const sort = document.getElementById("walletSort")?.value || "worth_desc";
    let rows = walletListData.slice();
    if (cat === "pump_dump") rows = rows.filter((r) => r.dump_hits >= 2);
    else if (cat !== "all") rows = rows.filter((r) => r.kind === cat);
    if (q) rows = rows.filter((r) => r.wallet.toLowerCase().includes(q));
    rows.sort(WALLET_SORTERS[sort] || WALLET_SORTERS.worth_desc);
    renderWalletRows(rows);
  }
  function renderWalletRows(rows) {
    const tb = document.querySelector("#walletListTable tbody");
    const hint = document.getElementById("walletListCount");
    if (hint) hint.textContent = `${rows.length} shown · ${walletListData.length} tracked`;
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8" class="empty">No wallets match.</td></tr>`; return;
    }
    tb.innerHTML = rows.slice(0, 200).map((w) => `
      <tr class="wallet-link" data-wallet="${esc(w.wallet)}">
        <td><code class="mut">${esc(w.wallet_short)}</code><br><small class="mut">${esc((w.tokens||[]).slice(0,3).join(" "))}</small></td>
        <td><span class="badge ${esc(w.kind)}">${esc(w.kind.replace("_"," "))}</span></td>
        <td class="num">${fmtCompact(w.worth_usd)}</td>
        <td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>
        <td class="num mut">${fmtCompact(w.buy_usd)}</td>
        <td class="num mut">${w.win_rate}%</td>
        <td class="num mut">${w.token_count}</td>
        <td class="num mut">${ago(w.first_seen)}</td>
      </tr>`).join("");
  }

  // ---- bundles (coordinated multi-wallet buys) ----
  const BUNDLE_SEV = { critical: "🔴", warning: "🟠", info: "🟡" };
  function renderBundles(list) {
    const el = document.getElementById("bundleList");
    if (!list || !list.length) {
      el.innerHTML = `<div class="empty">No coordinated buys detected yet — watching for wallets bundling into the same coin.</div>`; return;
    }
    el.innerHTML = list.map((b) => {
      const wallets = (b.wallets || []).map((w) =>
        `<code class="wallet-link" data-wallet="${esc(w.wallet)}" title="${esc(w.wallet)}">${esc(w.wallet_short)} ${fmtCompact(w.usd)}</code>`).join("");
      const cabalTag = b.cabal_id ? `<span class="cabal-link team-cabal" data-cabal="${esc(b.cabal_id)}">👥 ${esc(b.cabal_id)}</span>` : "";
      const spanTxt = b.span_seconds < 90 ? `${Math.round(b.span_seconds)}s apart`
        : `${Math.round(b.span_seconds/60)}m apart`;
      return `<div class="bundle-card sev-${esc(b.severity)}">
        <div class="bn-head">
          <div>${BUNDLE_SEV[b.severity]||""} <a href="${esc(b.url)}" target="_blank" rel="noopener" class="bn-sym">${esc(b.symbol)}</a>
            <span class="bn-label">${esc(b.label)}</span></div>
          <span class="bn-count">${b.wallet_count} wallets · ${fmtCompact(b.total_usd)}</span>
        </div>
        <div class="bn-meta">${spanTxt} · ${Math.round(b.age_minutes)}m old · ${esc(b.phase)} · vol1h ${fmtCompact(b.volume_h1)} · pump ${Math.round(b.pump_score)} ${cabalTag}</div>
        <div class="bn-wallets">${wallets}</div>
      </div>`;
    }).join("");
  }

  // ================= INFLUENCERS TAB =================
  function renderInfluencers(list) {
    const el = document.getElementById("influencerGrid");
    if (!list || !list.length) { el.innerHTML = `<div class="empty">No influencers configured. Add them to influencers.yaml.</div>`; return; }
    el.innerHTML = list.map((inf) => {
      const a = inf.activity || {};
      const initial = (inf.name || "?").trim().charAt(0).toUpperCase();
      const demo = inf.demo ? `<span class="demo-badge">DEMO</span>` : "";
      const play = a.symbol ? `<div class="infl-play">last:
        <span class="side-${esc(a.side)}">${esc((a.side||"").toUpperCase())}</span>
        <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.symbol)}</a>
        <span class="mut">${fmtCompact(a.usd)}</span></div>` : "";
      return `<div class="infl-card">
        <div class="infl-top">
          <div class="infl-av">${esc(initial)}</div>
          <div class="infl-id">
            <div class="infl-name">${esc(inf.name)} ${demo}</div>
            <div class="infl-handle">${esc(inf.handle)}</div>
            <div class="infl-foll">${fmtFollowers(inf.followers)} followers</div>
          </div>
        </div>
        <div class="infl-wallet" title="click to copy" data-wallet="${esc(inf.wallet)}">
          <span>${esc(inf.wallet_short)}</span><span>⧉</span></div>
        <div class="infl-stats">
          <div class="infl-stat"><div class="lbl">Holdings</div><div class="val">${a.holdings_usd!=null?fmtCompact(a.holdings_usd):"—"}</div></div>
          <div class="infl-stat"><div class="lbl">30d PnL</div><div class="val ${cls(a.pnl_30d)}">${a.pnl_30d!=null?fmtCompact(a.pnl_30d):"—"}</div></div>
          <div class="infl-stat"><div class="lbl">Win rate</div><div class="val">${a.win_rate!=null?a.win_rate+"%":"—"}</div></div>
          <div class="infl-stat"><div class="lbl">Source</div><div class="val mut" style="font-size:11px">${esc(a.source||"—")}</div></div>
        </div>
        ${play}
      </div>`;
    }).join("");
    el.querySelectorAll(".infl-wallet").forEach((w) => w.addEventListener("click", () => {
      navigator.clipboard && navigator.clipboard.writeText(w.dataset.wallet);
      const s = w.querySelector("span:last-child"); const o = s.textContent; s.textContent = "copied";
      setTimeout(() => (s.textContent = o), 1000);
    }));
  }

  // ================= FRESH LOADOUTS =================
  function renderLoadouts(list) {
    const el = document.getElementById("loadoutGrid");
    if (!list || !list.length) {
      el.innerHTML = `<div class="empty">No fresh loadouts right now — watching for a strong team to load a young coin.</div>`;
      return;
    }
    el.innerHTML = list.map((l) => {
      const conv = ["heavy", "loading", "forming"].includes(l.conviction) ? l.conviction : "forming";
      const team = (l.team || []).map((t) =>
        `<code class="wallet-link" data-wallet="${esc(t.wallet)}" title="${esc(t.wallet)} · ${t.win_rate}%w">${esc(t.wallet_short)}</code>`).join("");
      const cabalTag = l.cabal_id ? `<span class="cabal-link team-cabal" data-cabal="${esc(l.cabal_id)}">👥 ${esc(l.cabal_id)}</span>` : "";
      const auth = (l.mint_renounced && l.freeze_renounced) ? `<span class="ok-tag">✅ renounced</span>` : "";
      return `<div class="loadout-card conv-${conv}">
        <div class="lc-head">
          <div><a href="${esc(l.url)}" target="_blank" rel="noopener" class="lc-sym">${esc(l.symbol)}</a>
            <span class="lc-age">${Math.round(l.age_minutes)}m old</span></div>
          <span class="conv-badge ${conv}">${conv}</span>
        </div>
        <div class="lc-score">${bar("opp", l.loadout_score)}</div>
        <div class="lc-meta">${l.strong_wallet_count} strong wallets · ${fmtCompact(l.smart_inflow_usd)} in · ${esc(l.phase)} ${auth}</div>
        <div class="lc-team">${team} ${cabalTag}</div>
      </div>`;
    }).join("");
  }

  // ================= MODAL / PROFILES =================
  let profileChart = null;
  let currentModal = null;
  const profileCache = new Map();
  let cacheScan = -1;
  const modalStack = [];

  function getModalRoot() {
    let r = document.getElementById("modalRoot");
    if (!r) {  // self-heal if index.html was an older/cached version
      r = document.createElement("div");
      r.id = "modalRoot"; r.className = "modal-root";
      document.body.appendChild(r);
    }
    return r;
  }
  function destroyProfileChart() { if (profileChart) { profileChart.destroy(); profileChart = null; } }
  function openModal(html, kind) {
    const root = getModalRoot();
    root.innerHTML = `<div class="modal-backdrop" data-close></div><div class="modal-panel ${kind}">${html}</div>`;
    root.classList.add("open");
    document.body.classList.add("modal-open");
    root.setAttribute("aria-hidden", "false");
  }
  function closeModal() {
    const root = getModalRoot();
    destroyProfileChart();
    root.classList.remove("open");
    document.body.classList.remove("modal-open");
    root.setAttribute("aria-hidden", "true");
    modalStack.length = 0; currentModal = null;
    setTimeout(() => { if (!root.classList.contains("open")) root.innerHTML = ""; }, 220);
  }

  function drawProfileChart(canvasId, career) {
    destroyProfileChart();
    const cv = document.getElementById(canvasId);
    if (!cv || typeof Chart === "undefined" || !career || !career.length) return;
    const data = career.map((p) => p.equity);
    const labels = career.map((p) => new Date(p.ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }));
    const up = data[data.length - 1] >= data[0];
    const ctx = cv.getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, 200);
    grad.addColorStop(0, up ? "rgba(25,227,164,.28)" : "rgba(255,82,103,.22)");
    grad.addColorStop(1, "rgba(0,0,0,0)");
    profileChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: up ? "#19e3a4" : "#ff5267", borderWidth: 2, fill: true, backgroundColor: grad, tension: .28, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => "Equity " + fmtUsd(c.parsed.y) } } },
        scales: { x: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#566077", maxTicksLimit: 7, font: { size: 9 } } },
          y: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#7d8ba3", font: { size: 9 }, callback: (v) => "$" + (v / 1000).toFixed(0) + "k" } } } },
    });
  }

  async function openWalletProfile(addr) {
    openModal(`<button class="modal-x" data-close>×</button><div class="skeleton">Loading wallet…</div>`, "drawer");
    currentModal = { type: "wallet", id: addr };
    let p = profileCache.get("w:" + addr);
    if (!p) { try { p = await fetch("/api/wallet/" + encodeURIComponent(addr)).then((r) => r.json()); profileCache.set("w:" + addr, p); } catch (e) { return; } }
    renderWalletProfile(p);
  }
  function renderWalletProfile(p) {
    const back = modalStack.length ? `<button class="back-link" id="backBtn">← back</button>` : "";
    const holdings = (p.holdings || []).map((hd) =>
      `<div class="hold-row"><span>${esc(hd.symbol)}</span><span class="num">${fmtCompact(hd.value_usd)}</span><span class="num ${cls(hd.unrealized_pct)}">${fmtPct(hd.unrealized_pct)}</span><span class="src-tag">${esc(hd.source)}</span></div>`).join("");
    const evs = (p.recent_events || []).slice(0, 8).map((e) =>
      `<div class="ev-row"><span class="side-${esc(e.side)}">${esc((e.side || "").toUpperCase())}</span> ${esc(e.symbol)} <span class="mut">${fmtCompact(e.usd)}</span></div>`).join("") || `<div class="mut">no recent activity</div>`;
    const cabalLink = p.cabal_id ? `· <span class="cabal-link" data-cabal="${esc(p.cabal_id)}">${esc(p.cabal_id)}</span>` : "";
    openModal(`
      ${back}<button class="modal-x" data-close>×</button>
      <div class="prof-head">
        <div class="prof-av">${esc((p.wallet || "?").charAt(0))}</div>
        <div><div class="prof-name"><code class="copy-addr" data-copy="${esc(p.wallet)}">${esc(p.wallet_short)} ⧉</code> <span class="badge ${esc(p.kind)}">${esc((p.kind || "").replace("_", " "))}</span></div>
          <div class="prof-sub">${esc(p.source)} ${p.tracked ? "" : "· untracked"} ${cabalLink}</div></div>
      </div>
      <div class="prof-stats">
        <div class="pstat"><div class="lbl">Net PnL</div><div class="val ${cls(p.net_usd)}">${fmtUsd(p.net_usd)}</div></div>
        <div class="pstat"><div class="lbl">ROI</div><div class="val ${cls(p.roi_pct)}">${fmtPct(p.roi_pct)}</div></div>
        <div class="pstat"><div class="lbl">Win rate</div><div class="val">${p.win_rate}%</div></div>
        <div class="pstat"><div class="lbl">Holdings</div><div class="val">${fmtCompact(p.holdings_value_usd)}</div></div>
        <div class="pstat"><div class="lbl">Coins</div><div class="val">${p.token_count}</div></div>
        <div class="pstat"><div class="lbl">Realized</div><div class="val ${cls(p.realized_usd)}">${fmtCompact(p.realized_usd)}</div></div>
      </div>
      <div class="prof-section">Career <span class="hint">modelled equity</span></div>
      <div class="prof-chart"><canvas id="careerChart"></canvas></div>
      <div class="prof-section">Holdings</div>
      <div class="hold-list">${holdings || '<div class="mut">none</div>'}</div>
      <div class="prof-section">Recent activity</div>
      <div class="ev-list">${evs}</div>
      <div class="prof-foot">PnL, holdings &amp; career are modelled from market pressure — not on-chain trade history.</div>
    `, "drawer");
    currentModal = { type: "wallet", id: p.wallet };
    drawProfileChart("careerChart", p.career);
    const bb = document.getElementById("backBtn");
    if (bb) bb.addEventListener("click", () => { const prev = modalStack.pop(); if (prev) prev(); });
  }

  async function openCabalProfile(id) {
    openModal(`<button class="modal-x" data-close>×</button><div class="skeleton">Loading cabal…</div>`, "modal");
    currentModal = { type: "cabal", id };
    let c = profileCache.get("c:" + id);
    if (!c) { try { c = await fetch("/api/cabal/" + encodeURIComponent(id)).then((r) => r.json()); profileCache.set("c:" + id, c); } catch (e) { return; } }
    if (c.error) { openModal(`<button class="modal-x" data-close>×</button><div class="empty">Cabal not found.</div>`, "modal"); return; }
    renderCabalProfile(c);
  }
  function renderCabalProfile(c) {
    const members = (c.members || []).map((m) =>
      `<div class="cmember wallet-link" data-wallet="${esc(m.wallet)}"><div class="prof-av sm">${esc((m.wallet || "?").charAt(0))}</div>
        <div><code>${esc(m.wallet_short)}</code><br><span class="badge ${esc(m.kind)}">${esc((m.kind || "").replace("_", " "))}</span> <small class="mut">${m.win_rate}%w</small></div></div>`).join("");
    const loading = (c.loading_now || []).map((l) =>
      `<div class="load-row"><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.symbol)}</a> <span class="mut">${Math.round(l.age_minutes)}m · score ${l.loadout_score} · ${fmtCompact(l.smart_inflow_usd)} in</span></div>`).join("") || `<div class="mut">not currently loading anything fresh</div>`;
    openModal(`
      <button class="modal-x" data-close>×</button>
      <div class="prof-head"><div class="prof-av cab">${esc((c.id || "cabal-x").slice(6, 7).toUpperCase())}</div>
        <div><div class="prof-name"><span class="cid">${esc(c.id)}</span> <span class="csz">${c.size} wallets</span></div>
        <div class="prof-sub">${esc(c.source)} · coordinated group</div></div></div>
      <div class="prof-stats">
        <div class="pstat"><div class="lbl">Net flow</div><div class="val ${cls(c.combined.net_usd)}">${fmtCompact(c.combined.net_usd)}</div></div>
        <div class="pstat"><div class="lbl">Avg win</div><div class="val">${c.combined.win_rate}%</div></div>
        <div class="pstat"><div class="lbl">Dumped</div><div class="val neg">${fmtCompact(c.combined.sell_usd)}</div></div>
        <div class="pstat"><div class="lbl">Co-dumps</div><div class="val">${c.combined.dump_hits}</div></div>
        <div class="pstat"><div class="lbl">Coins</div><div class="val">${c.token_count}</div></div>
      </div>
      <div class="prof-section">Combined career <span class="hint">modelled</span></div>
      <div class="prof-chart"><canvas id="careerChart"></canvas></div>
      <div class="prof-section">🚀 Currently loading</div>
      <div class="load-list">${loading}</div>
      <div class="prof-section">Members <span class="hint">click to open</span></div>
      <div class="cmember-grid">${members}</div>
      <div class="prof-section">Track record</div>
      <div class="cabal-tokens">coins: <b>${esc((c.shared_tokens || []).join(", ")) || "—"}</b></div>
      <div class="prof-foot">Stats &amp; career are modelled from market pressure — not on-chain trade history.</div>
    `, "modal");
    currentModal = { type: "cabal", id: c.id };
    drawProfileChart("careerChart", c.career);
  }

  // ================= MANUAL TRADE =================
  function setTradeMsg(text, ok) {
    const m = document.getElementById("tradeMsg");
    if (m) { m.textContent = text; m.className = "trade-msg " + (ok ? "ok" : "err"); }
  }
  function renderBuyOptions(b) {
    const sel = document.getElementById("buyCoin");
    if (!sel || !b) return;
    const cur = sel.value;
    sel.innerHTML = `<option value="">— select a scanned coin —</option>` +
      b.map((x) => `<option value="${esc(x.token.address)}">${esc(x.token.symbol)} — ${fmtPrice(x.token.price_usd)} · opp ${Math.round(x.detection.opportunity)}</option>`).join("");
    sel.value = cur || "";
  }
  async function refreshNow() {
    try { applySnapshot(await fetch("/api/snapshot").then((r) => r.json())); } catch (e) {}
    refreshAux();
  }
  async function doBuy() {
    const addr = document.getElementById("buyCoin").value;
    const usd = parseFloat(document.getElementById("buyUsd").value);
    if (!addr) return setTradeMsg("Pick a coin first.", false);
    if (!usd || usd <= 0) return setTradeMsg("Enter a USD amount.", false);
    const btn = document.getElementById("buyBtn"); btn.disabled = true;
    const r = await fetch("/api/trade/buy", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ address: addr, usd }) }).then((x) => x.json()).catch(() => null);
    btn.disabled = false;
    if (r && r.ok) { setTradeMsg(`✅ Bought ${r.symbol} for $${usd.toLocaleString()}.`, true); refreshNow(); }
    else setTradeMsg("⚠️ " + ((r && r.error) || "Buy failed."), false);
  }
  async function doSell(addr, frac) {
    const r = await fetch("/api/trade/sell", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ address: addr, fraction: frac }) }).then((x) => x.json()).catch(() => null);
    if (r && r.ok) { setTradeMsg(`✅ Sold ${Math.round(frac * 100)}% (${r.pnl >= 0 ? "+" : ""}$${r.pnl}).`, true); refreshNow(); }
    else setTradeMsg("⚠️ " + ((r && r.error) || "Sell failed."), false);
  }

  // ---------- snapshot dispatch ----------
  function applySnapshot(s) {
    lastSnapshot = s;
    if (s.portfolio) renderKpis(s.portfolio);
    if (s.positions) renderPositions(s.positions);
    if (s.board) {
      board = s.board; renderBoard(board);
      renderBuyOptions(board);
      if (activeTab === "charts") {
        renderCoinList(document.getElementById("coinSearch").value);
        if (selectedCoinAddr && chartView === "profile") renderChartBody();  // live profile refresh
      }
    }
    if (s.status) renderStatus(s.status);
  }

  async function refreshAux() {
    try {
      const [eq, tr, sg, wl] = await Promise.all([
        fetch("/api/equity").then((r) => r.json()),
        fetch("/api/trades").then((r) => r.json()),
        fetch("/api/signals").then((r) => r.json()),
        fetch("/api/wallets").then((r) => r.json()),
      ]);
      renderEquity(eq); renderTrades(tr); renderSignals(sg); renderWallets(wl);
    } catch (e) { /* ignore */ }
  }
  async function refreshWalletsTab() {
    try {
      const [cat, lo, all, bun] = await Promise.all([
        fetch("/api/wallets/categorized").then((r) => r.json()),
        fetch("/api/loadouts").then((r) => r.json()),
        fetch("/api/wallets/all").then((r) => r.json()),
        fetch("/api/bundles").then((r) => r.json()),
      ]);
      renderCategorized(cat);          // counts + cabals
      renderLoadouts(lo);
      walletListData = all || [];
      applyWalletFilters();
      renderBundles(bun);
    } catch (e) { /* ignore */ }
  }
  async function refreshInfluencers() {
    try { renderInfluencers(await fetch("/api/influencers").then((r) => r.json())); } catch (e) {}
  }

  // ---------- tabs ----------
  const VIEW_TITLES = { overview: "Dashboard", trade: "Trade", charts: "Live Charts",
                        wallets: "Wallet intelligence", influencers: "Influencer wallets" };
  function switchTab(name) {
    activeTab = name;
    document.querySelectorAll(".nav-item").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name));
    const vt = document.getElementById("viewTitle");
    if (vt) vt.textContent = VIEW_TITLES[name] || name;
    if (name === "trade") renderBuyOptions(board);
    if (name === "charts") renderCoinList(document.getElementById("coinSearch").value);
    if (name === "wallets") refreshWalletsTab();
    if (name === "influencers") refreshInfluencers();
  }
  document.querySelectorAll(".nav-item").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab)));

  // board row (Trade tab) → load coin into the buy box
  document.querySelector("#boardTable tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-addr]");
    if (!tr || !tr.dataset.addr) return;
    const sel = document.getElementById("buyCoin");
    if (sel) { sel.value = tr.dataset.addr; document.getElementById("buyUsd").focus(); }
    setTradeMsg("Loaded into the buy box — set an amount and Buy.", true);
  });
  // manual trade controls
  document.getElementById("buyBtn").addEventListener("click", doBuy);
  document.getElementById("buyUsd").addEventListener("keydown", (e) => { if (e.key === "Enter") doBuy(); });
  // charts controls
  document.getElementById("coinSearch").addEventListener("input", (e) => renderCoinList(e.target.value));
  document.getElementById("manualGo").addEventListener("click", () => {
    const v = document.getElementById("manualAddr").value.trim();
    if (v) loadChartManual(v);
  });
  document.getElementById("manualAddr").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("manualGo").click();
  });
  // chart view toggle (Profile / Chart) + the in-profile "View chart" button
  document.getElementById("chartViewToggle").addEventListener("click", (e) => {
    const b = e.target.closest(".vbtn[data-view]");
    if (b) setChartView(b.dataset.view);
  });
  document.getElementById("chartBody").addEventListener("click", (e) => {
    if (e.target.closest("#cpChartBtn")) setChartView("chart");
  });
  // wallet sub-tabs
  document.querySelectorAll("#walletSubtabs .subtab").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll("#walletSubtabs .subtab").forEach((x) => x.classList.toggle("active", x === t));
      document.querySelectorAll("#pane-wallets .subpane").forEach((p) =>
        p.classList.toggle("active", p.id === "sub-" + t.dataset.sub));
    }));
  // wallet filter controls
  ["walletSearch", "walletCat", "walletSort"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(id === "walletSearch" ? "input" : "change", applyWalletFilters);
  });

  // ---------- websocket ----------
  function setConn(state) {
    const pill = document.getElementById("connStatus");
    pill.className = "status-pill " + state;
    document.getElementById("connText").textContent =
      state === "live" ? "live" : state === "dead" ? "offline" : "connecting…";
  }
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => setConn("live");
    ws.onclose = () => { setConn("dead"); setTimeout(connect, 3000); };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          applySnapshot(msg.data); refreshAux();
          if (activeTab === "wallets") refreshWalletsTab();
          if (activeTab === "influencers") refreshInfluencers();
        }
      } catch (e) {}
    };
  }

  // ---------- controls ----------
  async function post(path) {
    try { return await fetch(path, { method: "POST" }).then((r) => r.json()); } catch (e) { return null; }
  }
  document.getElementById("btnPause").addEventListener("click", async () => {
    await post(lastSnapshot?.status?.paused ? "/api/control/resume" : "/api/control/pause");
  });
  document.getElementById("btnScan").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Scanning…";
    await post("/api/control/scan");
    e.target.disabled = false; e.target.textContent = "Scan now";
  });
  document.getElementById("btnReset").addEventListener("click", async () => {
    if (confirm("Reset paper portfolio to $10,000 and clear all history?")) {
      await post("/api/control/reset"); await refreshAux();
    }
  });

  // ---------- interactions (attached FIRST, before any network) ----------
  function setupInteractions() {
    // delegated so it works on every (re-)rendered wallet/cabal element, on any
    // tab, and on touch devices. Also handles modal close + copy.
    const onActivate = (e) => {
      const closeEl = e.target.closest("[data-close]");
      if (closeEl && getModalRoot().contains(closeEl)) { closeModal(); return; }
      const sellEl = e.target.closest("[data-sell]");
      if (sellEl) { e.stopPropagation(); doSell(sellEl.dataset.sell, parseFloat(sellEl.dataset.frac)); return; }
      const amtEl = e.target.closest(".quick-amts button");
      if (amtEl) { document.getElementById("buyUsd").value = amtEl.dataset.amt; return; }
      const copyEl = e.target.closest("[data-copy]");
      if (copyEl) { if (navigator.clipboard) navigator.clipboard.writeText(copyEl.dataset.copy); return; }
      const w = e.target.closest("[data-wallet]");
      if (w && w.dataset.wallet) {
        if (currentModal && currentModal.type === "cabal") {
          const cid = currentModal.id; modalStack.push(() => openCabalProfile(cid));
        }
        openWalletProfile(w.dataset.wallet); return;
      }
      const c = e.target.closest("[data-cabal]");
      if (c && c.dataset.cabal) { openCabalProfile(c.dataset.cabal); return; }
    };
    document.addEventListener("click", onActivate);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  }

  // ---------- boot ----------
  async function boot() {
    setupInteractions();   // network-independent — clicks work even if a fetch hangs
    try {
      const cfg = await fetch("/api/config").then((r) => r.json());
      if (cfg && cfg.auth_enabled) {
        const l = document.getElementById("logoutLink");
        if (l) l.style.display = "";
      }
    } catch (e) {}
    try { applySnapshot(await fetch("/api/snapshot").then((r) => r.json())); } catch (e) {}
    await refreshAux();
    connect();
    setInterval(refreshAux, 15000);
  }
  boot();
})();
