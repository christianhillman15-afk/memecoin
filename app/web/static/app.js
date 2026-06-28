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
          <small>peak ${fmtPrice(p.peak_price)}</small></div></td>
        <td class="mut">${esc(p.entry_reason)}</td>
        <td class="num">${fmtUsd(p.entry_value, 0)}</td>
        <td class="num">${fmtPrice(p.entry_price)}</td>
        <td class="num">${fmtPrice(p.last_price)}</td>
        <td class="num ${cls(p.unrealized_pnl)}">${fmtUsd(p.unrealized_pnl)}<br>
          <small>${fmtPct(p.unrealized_pnl_pct)}</small></td>
        <td>${guard}</td>
        <td class="num mut">${held(p.hold_seconds)}</td>
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
      return `<tr data-pair="${esc(t.pair_address)}" data-chain="${esc(t.chain)}" data-sym="${esc(t.symbol)}" title="Click to chart ${esc(t.symbol)}">
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
  const SIG_ICON = { pump: "▲", dump: "▼", multi_sell: "⚠", whale_buy: "◆", entry: "▶", exit: "■" };
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
    if (!wallets || !wallets.length) {
      tb.innerHTML = `<tr><td colspan="4" class="empty">Discovering wallets…</td></tr>`; return;
    }
    tb.innerHTML = wallets.map((w) => `
      <tr>
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
    document.getElementById("provider").textContent = "provider: " + (st.wallet_provider || "—");
    document.getElementById("footProvider").textContent = `${st.chain} · ${st.wallet_provider} wallet feed`;
    document.getElementById("scanInfo").textContent = `scan #${st.scan_count}` + (st.last_error ? " · err" : "");
    const btn = document.getElementById("btnPause");
    btn.textContent = st.paused ? "Resume" : "Pause";
    btn.classList.toggle("active", st.paused);
  }

  // ================= CHARTS TAB =================
  function chartEmbedUrl(ch, pair) {
    return `https://dexscreener.com/${encodeURIComponent(ch)}/${encodeURIComponent(pair)}` +
      `?embed=1&theme=dark&trades=0&info=0`;
  }
  function loadChart(ch, pair, label) {
    if (!pair) return;
    selectedPair = pair;
    const wrap = document.getElementById("chartFrameWrap");
    document.getElementById("chartTitle").textContent = label || "Chart";
    wrap.innerHTML = `<iframe src="${esc(chartEmbedUrl(ch, pair))}" allow="clipboard-write" loading="lazy"></iframe>`;
    document.querySelectorAll(".coin-row").forEach((r) =>
      r.classList.toggle("active", r.dataset.pair === pair));
  }
  function renderCoinList(filter) {
    const el = document.getElementById("coinList");
    const q = (filter || "").trim().toLowerCase();
    let items = board;
    if (q) items = board.filter((b) => (b.token.symbol + " " + b.token.name).toLowerCase().includes(q));
    if (!items.length) { el.innerHTML = `<div class="empty">No coins.</div>`; return; }
    el.innerHTML = items.slice(0, 60).map((b) => {
      const t = b.token, d = b.detection;
      const chg = t.price_change?.h1 ?? 0;
      return `<div class="coin-row" data-pair="${esc(t.pair_address)}" data-chain="${esc(t.chain)}" data-label="${esc(t.symbol)} · ${esc(t.name)}">
        <div><div class="c-sym">${esc(t.symbol)}</div><div class="c-name">${esc((t.name||"").slice(0,20))}</div></div>
        <div style="text-align:right"><div class="c-chg ${cls(chg)}">${fmtPct(chg)}</div>
          <div class="c-pump">P${Math.round(d.pump_score)} · D${Math.round(d.dump_risk)}</div></div>
      </div>`;
    }).join("");
    document.querySelectorAll("#coinList .coin-row").forEach((r) => {
      r.addEventListener("click", () => loadChart(r.dataset.chain, r.dataset.pair, r.dataset.label));
      if (r.dataset.pair === selectedPair) r.classList.add("active");
    });
  }

  // ================= WALLETS TAB =================
  function walletRow(cols) { return `<tr>${cols.join("")}</tr>`; }
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

    const code = (w) => `<code title="${esc(w.wallet)}">${esc(w.wallet_short)}</code>`;
    const fill = (id, rows, builder) => {
      const tb = document.getElementById(id);
      if (!tb) return;
      tb.innerHTML = rows && rows.length ? rows.map(builder).join("")
        : `<tr><td colspan="5" class="empty">none yet</td></tr>`;
    };
    fill("tb-whales", d.whales, (w) => walletRow([
      `<td>${code(w)}</td>`, `<td class="num mut">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${fmtCompact(w.buy_usd + w.sell_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`]));
    fill("tb-insiders", d.insiders, (w) => walletRow([
      `<td>${code(w)}</td>`, `<td class="num mut">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`, `<td class="num mut">${w.events}</td>`]));
    fill("tb-smart_money", d.smart_money, (w) => walletRow([
      `<td>${code(w)}</td>`, `<td class="num up">${w.win_rate}%</td>`,
      `<td class="num ${cls(w.net_usd)}">${fmtCompact(w.net_usd)}</td>`,
      `<td class="num mut">${w.token_count}</td>`, `<td class="num mut">${w.events}</td>`]));
    fill("tb-pump_dumpers", d.pump_dumpers, (w) => walletRow([
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
        <div class="ch"><span class="cid">${esc(c.id)}</span><span class="csz">${c.size} wallets</span></div>
        <div class="cabal-stats">
          <div class="cs"><b>${fmtCompact(c.sell_usd)}</b><span>dumped</span></div>
          <div class="cs"><b>${c.dump_hits}</b><span>co-dumps</span></div>
          <div class="cs"><b>${c.token_count}</b><span>coins</span></div>
        </div>
        <div class="cabal-members">${c.members.map((m) => `<code title="${esc(m.wallet)}">${esc(m.wallet_short)}</code>`).join("")}</div>
        <div class="cabal-tokens">coins: <b>${esc((c.shared_tokens||[]).join(", "))}</b></div>
      </div>`).join("");
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

  // ---------- snapshot dispatch ----------
  function applySnapshot(s) {
    lastSnapshot = s;
    if (s.portfolio) renderKpis(s.portfolio);
    if (s.positions) renderPositions(s.positions);
    if (s.board) { board = s.board; renderBoard(board); if (activeTab === "charts") renderCoinList(document.getElementById("coinSearch").value); }
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
    try { renderCategorized(await fetch("/api/wallets/categorized").then((r) => r.json())); } catch (e) {}
  }
  async function refreshInfluencers() {
    try { renderInfluencers(await fetch("/api/influencers").then((r) => r.json())); } catch (e) {}
  }

  // ---------- tabs ----------
  function switchTab(name) {
    activeTab = name;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name));
    if (name === "charts") renderCoinList(document.getElementById("coinSearch").value);
    if (name === "wallets") refreshWalletsTab();
    if (name === "influencers") refreshInfluencers();
  }
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab)));

  // board row → chart
  document.querySelector("#boardTable tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-pair]");
    if (!tr || !tr.dataset.pair) return;
    switchTab("charts");
    loadChart(tr.dataset.chain, tr.dataset.pair, tr.dataset.sym);
  });
  // charts controls
  document.getElementById("coinSearch").addEventListener("input", (e) => renderCoinList(e.target.value));
  document.getElementById("manualGo").addEventListener("click", () => {
    const v = document.getElementById("manualAddr").value.trim();
    if (v) loadChart(chain, v, v.slice(0, 10) + "…");
  });
  document.getElementById("manualAddr").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("manualGo").click();
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

  // ---------- boot ----------
  async function boot() {
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
