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

    const deployed = Math.max(0, p.equity - p.cash);
    document.getElementById("kDeployed").textContent = fmtUsd(deployed, 0);
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
      let guard = "";
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
  function renderBoard(board) {
    const tb = document.querySelector("#boardTable tbody");
    if (!board || !board.length) {
      tb.innerHTML = `<tr><td colspan="14" class="empty">Scanning Solana memecoins…</td></tr>`;
      return;
    }
    tb.innerHTML = board.slice(0, 40).map((b) => {
      const t = b.token, d = b.detection, i = b.intel;
      let sig = "";
      if (i.confirmed_multi_sell) sig = `<span class="tag-flag">multi-sell ${i.multi_sell_wallets}w</span>`;
      else if (i.smart_inflow_score > 35) sig = `<span class="badge smart_money">smart in</span>`;
      else if (i.smart_inflow_score < -35) sig = `<span class="badge distribution">smart out</span>`;
      const flags = (d.flags || []).filter(f => ["thin_liquidity","high_churn","rollover","sell_pressure","supply_overhang"].includes(f))
        .slice(0, 2).map(f => `<span class="tag-flag">${f.replace(/_/g," ")}</span>`).join("");
      const ch = (w) => {
        const v = t.price_change?.[w] ?? 0;
        return `<td class="num ${cls(v)}">${fmtPct(v)}</td>`;
      };
      return `<tr>
        <td><div class="tok"><a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.symbol)}</a>
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
    if (!signals || !signals.length) {
      el.innerHTML = `<div class="empty">Waiting for signals…</div>`;
      return;
    }
    el.innerHTML = signals.slice(0, 60).map((s) => `
      <div class="sig ${esc(s.kind)} ${esc(s.severity)}">
        <div class="ic">${SIG_ICON[s.kind] || "•"}</div>
        <div class="body"><div class="msg">${esc(s.message)}</div>
          <div class="time">${ago(s.ts)}</div></div>
      </div>`).join("");
  }

  // ---------- wallets ----------
  function renderWallets(wallets) {
    const tb = document.querySelector("#walletsTable tbody");
    if (!wallets || !wallets.length) {
      tb.innerHTML = `<tr><td colspan="4" class="empty">Discovering wallets…</td></tr>`;
      return;
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
      tb.innerHTML = `<tr><td colspan="8" class="empty">No closed trades yet.</td></tr>`;
      return;
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
        data: { labels, datasets: [{
          data, borderColor: up ? "#19e3a4" : "#ff5267", borderWidth: 2,
          fill: true, backgroundColor: grad, tension: .28, pointRadius: 0,
          pointHoverRadius: 4, pointHoverBackgroundColor: "#fff",
        }]},
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: false },
            tooltip: { mode: "index", intersect: false,
              callbacks: { label: (c) => "Equity " + fmtUsd(c.parsed.y) } } },
          scales: {
            x: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#566077", maxTicksLimit: 8, font: { size: 10 } } },
            y: { grid: { color: "rgba(30,42,61,.4)" }, ticks: { color: "#7d8ba3", font: { size: 10 },
              callback: (v) => "$" + (v / 1000).toFixed(1) + "k" } },
          },
        },
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
    document.getElementById("provider").textContent = "provider: " + (st.wallet_provider || "—");
    document.getElementById("footProvider").textContent =
      `${st.chain} · ${st.wallet_provider} wallet feed`;
    document.getElementById("scanInfo").textContent =
      `scan #${st.scan_count}` + (st.last_error ? " · err" : "");
    const btn = document.getElementById("btnPause");
    btn.textContent = st.paused ? "Resume" : "Pause";
    btn.classList.toggle("active", st.paused);
  }

  // ---------- snapshot dispatch ----------
  function applySnapshot(s) {
    lastSnapshot = s;
    if (s.portfolio) renderKpis(s.portfolio);
    if (s.positions) renderPositions(s.positions);
    if (s.board) renderBoard(s.board);
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
      renderEquity(eq);
      renderTrades(tr);
      renderSignals(sg);
      renderWallets(wl);
    } catch (e) { /* network blip; ignore */ }
  }

  // ---------- websocket ----------
  function setConn(state) {
    const pill = document.getElementById("connStatus");
    const txt = document.getElementById("connText");
    pill.className = "status-pill " + state;
    txt.textContent = state === "live" ? "live" : state === "dead" ? "offline" : "connecting…";
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
        if (msg.type === "snapshot") { applySnapshot(msg.data); refreshAux(); }
      } catch (e) {}
    };
  }

  // ---------- controls ----------
  async function post(path) {
    try { return await fetch(path, { method: "POST" }).then((r) => r.json()); }
    catch (e) { return null; }
  }
  document.getElementById("btnPause").addEventListener("click", async () => {
    const paused = lastSnapshot?.status?.paused;
    await post(paused ? "/api/control/resume" : "/api/control/pause");
  });
  document.getElementById("btnScan").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Scanning…";
    await post("/api/control/scan");
    e.target.disabled = false; e.target.textContent = "Scan now";
  });
  document.getElementById("btnReset").addEventListener("click", async () => {
    if (confirm("Reset paper portfolio to $10,000 and clear all history?")) {
      await post("/api/control/reset");
      await refreshAux();
    }
  });

  // ---------- boot ----------
  async function boot() {
    try { applySnapshot(await fetch("/api/snapshot").then((r) => r.json())); } catch (e) {}
    await refreshAux();
    connect();
    setInterval(refreshAux, 15000); // keep aux panels fresh even between pushes
  }
  boot();
})();
