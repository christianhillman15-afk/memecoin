"""Pure, network-free command handlers.

Each handler takes ``(scanner, db, cfg, args)`` and returns an HTML reply
string by reading the scanner snapshot / intel / db. No I/O and no mutation, so
they are unit-testable without a network or a running event loop. Control
commands (pause/resume/scan/reset/alerts) live in ``bot.py`` because they
mutate state or touch the bot's own internals.
"""
from __future__ import annotations

from typing import Any, Callable

from .format import h, hold, pct, price, rel_time, usd

KIND_EMOJI = {"whale": "🐋", "insider": "🕵️", "smart_money": "🧠", "retail": "👤"}
SIG_EMOJI = {"pump": "🚀", "dump": "🔻", "multi_sell": "🚨",
             "whale_buy": "🐋", "entry": "🟢", "exit": "🔴", "bundle": "📦"}


def _clamp_int(args: list[str], default: int, lo: int, hi: int) -> int:
    for a in args:
        try:
            return max(lo, min(hi, int(a)))
        except ValueError:
            continue
    return default


def cmd_status(scanner, db, cfg, args) -> str:
    s = scanner.snapshot()
    st, p = s["status"], s["portfolio"]
    health = "⏸ paused" if st["paused"] else ("🟢 running" if st["running"] else "🔴 stopped")
    out = [f"<b>MemeRadar</b> — {health}"]
    if st.get("last_error"):
        out.append(f"⚠️ <b>ERROR:</b> {h(st['last_error'])}")
    out.append(f"scan #{st['scan_count']} · {rel_time(st.get('last_scan_ts', 0))}"
               f" · {h(st['chain'])} · {h(st['wallet_provider'])} wallets")
    out.append(f"equity <b>{usd(p['equity'])}</b> ({pct(p['roi_pct'])}) · "
               f"{p['open_positions']} open")
    return "\n".join(out)


def cmd_portfolio(scanner, db, cfg, args) -> str:
    p = scanner.snapshot()["portfolio"]
    pnl_emoji = "🟢" if p["total_pnl"] >= 0 else "🔴"
    return "\n".join([
        "<b>💼 Portfolio</b>",
        f"Equity: <b>{usd(p['equity'])}</b>  (start {usd(p['starting_balance'],0)})",
        f"{pnl_emoji} P&amp;L: <b>{usd(p['total_pnl'])}</b>  ({pct(p['roi_pct'])})",
        f"Realized {usd(p['realized_pnl'])} · Unrealized {usd(p['unrealized_pnl'])}",
        f"Cash {usd(p['cash'],0)} · {p['open_positions']} open",
        f"Win rate {p['win_rate']:.0f}% · {p['total_trades']} trades "
        f"({p['wins']}W/{p['losses']}L)",
    ])


def cmd_positions(scanner, db, cfg, args) -> str:
    positions = scanner.snapshot()["positions"]
    if not positions:
        return "No open positions."
    # worst first so the at-risk position is on top
    positions = sorted(positions, key=lambda x: x["unrealized_pnl_pct"])
    lines = ["<b>📈 Open positions</b>"]
    for p in positions:
        e = "🟢" if p["unrealized_pnl"] >= 0 else "🔴"
        guard = "🪤 trailing" if p.get("trailing_armed") else "arming"
        lines.append(
            f"{e} <b>{h(p['symbol'])}</b> {pct(p['unrealized_pnl_pct'])} "
            f"({usd(p['unrealized_pnl'])}) · {usd(p['entry_value'],0)} in · "
            f"{hold(p['hold_seconds'])} · {guard}")
    return "\n".join(lines)


def cmd_board(scanner, db, cfg, args) -> str:
    n = _clamp_int(args, 8, 1, 15)
    board = scanner.snapshot()["board"][:n]
    if not board:
        return "Scanner warming up…"
    lines = [f"<b>🎯 Top {len(board)} opportunities</b>"]
    for b in board:
        t, d, i = b["token"], b["detection"], b["intel"]
        tags = []
        if i.get("confirmed_multi_sell"):
            tags.append("🚨multi-sell")
        for f in ("mint_authority", "freeze_authority"):
            if f in d.get("flags", []):
                tags.append("⛔" + f.split("_")[0])
        tag = (" " + " ".join(tags)) if tags else ""
        lines.append(
            f"<b>{h(t['symbol'])}</b> opp {d['opportunity']:.0f} · "
            f"P{d['pump_score']:.0f}/D{d['dump_risk']:.0f} · {h(d['phase'])} · "
            f"{usd(t['liquidity_usd'],0)} liq{tag}")
    return "\n".join(lines)


def cmd_token(scanner, db, cfg, args) -> str:
    if not args:
        return "Usage: /token &lt;symbol&gt;"
    q = " ".join(args)[:32].strip().lower()
    board = scanner.snapshot()["board"]
    match = None
    for b in board:
        t = b["token"]
        if q in t["symbol"].lower() or q in t["address"].lower():
            match = b
            break
    if not match:
        return f"No scanned coin matching “{h(q)}”. Try /board."
    t, d, i = match["token"], match["detection"], match["intel"]
    holding = any(p["address"] == t["address"] for p in scanner.snapshot()["positions"])
    pc = t.get("price_change", {})
    auth = []
    if t.get("mint_renounced") is False:
        auth.append("⛔ mint authority LIVE")
    if t.get("freeze_renounced") is False:
        auth.append("⛔ freeze authority LIVE")
    if t.get("mint_renounced") and t.get("freeze_renounced"):
        auth.append("✅ authorities renounced")
    lines = [
        f"<b>{h(t['symbol'])}</b> — {h(t.get('name',''))}{' · 📌 HELD' if holding else ''}",
        f"{price(t['price_usd'])} · {pct(pc.get('m5',0))}/5m {pct(pc.get('h1',0))}/1h "
        f"{pct(pc.get('h6',0))}/6h",
        f"liq {usd(t['liquidity_usd'],0)} · mc {usd(t.get('market_cap',0),0)} · "
        f"{t['age_minutes']:.0f}m old",
        f"pump <b>{d['pump_score']:.0f}</b> · dump <b>{d['dump_risk']:.0f}</b> · "
        f"safety {d['safety']:.0f} · {h(d['phase'])}",
        f"smart$ {usd(i['smart_inflow_usd'])} · whales {i['whale_count']} "
        f"insiders {i['insider_count']} · conc {i['whale_concentration']:.0f}%",
    ]
    if auth:
        lines.append(" · ".join(auth))
    if i.get("confirmed_multi_sell"):
        lines.append(f"🚨 {i['multi_sell_wallets']} wallets dumping")
    if d.get("reasons"):
        lines.append("<i>" + h(", ".join(d["reasons"][:3])) + "</i>")
    if t.get("url"):
        lines.append(f'<a href="{h(t["url"])}">chart ↗</a>')
    return "\n".join(lines)


def cmd_wallets(scanner, db, cfg, args) -> str:
    n = _clamp_int(args, 10, 1, 15)
    rows = scanner.intel.top_wallets(n)
    if not rows:
        return "No tracked wallets yet."
    lines = ["<b>🐋 Top tracked wallets</b>"]
    for w in rows:
        arrow = "▲" if w["net_usd"] >= 0 else "▼"
        lines.append(
            f"{KIND_EMOJI.get(w['kind'],'•')} <code>{h(w['wallet_short'])}</code> "
            f"{w['win_rate']:.0f}%w · {arrow}{usd(abs(w['net_usd']))} · "
            f"{w['token_count']} coins")
    return "\n".join(lines)


def cmd_cabals(scanner, db, cfg, args) -> str:
    data = scanner.intel.categorized(12)
    cabals = data.get("cabals", [])
    counts = data.get("counts", {})
    lines = [f"<b>👥 Cabal groups</b> · {counts.get('pump_dumpers',0)} pump&amp;dumpers tracked"]
    if not cabals:
        lines.append("<i>none detected yet (needs repeated co-selling)</i>")
    for c in cabals[:6]:
        lines.append(
            f"<b>{h(c['id'])}</b> · {c['size']} wallets · {usd(c['sell_usd'])} dumped · "
            f"{c['dump_hits']} co-dumps · {c['token_count']} coins")
        if c.get("shared_tokens"):
            lines.append("  <i>" + h(", ".join(c["shared_tokens"][:6])) + "</i>")
    return "\n".join(lines)


def cmd_influencers(scanner, db, cfg, args) -> str:
    rows = scanner.influencers.snapshot()
    if not rows:
        return "No influencers configured (edit influencers.yaml)."
    lines = ["<b>📣 Influencer wallets</b>"]
    for inf in rows[:10]:
        a = inf.get("activity") or {}
        tag = "[sim]" if a.get("source") != "helius" else "[live]"
        play = ""
        if a.get("symbol"):
            play = (f" · {('🟢' if a.get('side')=='buy' else '🔴')}"
                    f"{h(a.get('side','').upper())} {h(a['symbol'])} {usd(a.get('usd',0))}")
        demo = " ⚠️demo" if inf.get("demo") else ""
        lines.append(f"<b>{h(inf['name'])}</b> {h(inf.get('handle',''))}{demo} {tag}{play}")
    return "\n".join(lines)


def cmd_signals(scanner, db, cfg, args) -> str:
    kind_filter = next((a.lower() for a in args if not a.isdigit()), None)
    n = _clamp_int(args, 10, 1, 20)
    sigs = db.recent_signals(60)
    if kind_filter:
        sigs = [s for s in sigs if s.get("kind") == kind_filter]
    sigs = sigs[:n]
    if not sigs:
        return "No signals yet."
    lines = ["<b>📡 Recent signals</b>"]
    for s in sigs:
        lines.append(f"{SIG_EMOJI.get(s['kind'],'•')} {h(s['message'])} "
                     f"<i>{rel_time(s['ts'])}</i>")
    return "\n".join(lines)


def cmd_trades(scanner, db, cfg, args) -> str:
    n = _clamp_int(args, 10, 1, 20)
    trades = db.recent_trades(n)
    if not trades:
        return "No closed trades yet."
    lines = ["<b>🧾 Recent trades</b>"]
    for t in trades:
        e = "🟢" if t["pnl"] >= 0 else "🔴"
        lines.append(f"{e} <b>{h(t['symbol'])}</b> {pct(t['pnl_pct'])} "
                     f"({usd(t['pnl'])}) — {h(t['exit_reason'])} <i>{rel_time(t['closed_at'])}</i>")
    return "\n".join(lines)


# canonical name -> handler
READ_HANDLERS: dict[str, Callable[..., str]] = {
    "status": cmd_status, "portfolio": cmd_portfolio, "positions": cmd_positions,
    "board": cmd_board, "token": cmd_token, "wallets": cmd_wallets,
    "cabals": cmd_cabals, "influencers": cmd_influencers, "signals": cmd_signals,
    "trades": cmd_trades,
}

# alias -> canonical
ALIASES: dict[str, str] = {
    "pnl": "portfolio", "port": "portfolio", "pos": "positions",
    "scanner": "board", "top": "board", "opps": "board",
    "coin": "token", "c": "token", "smart": "wallets", "whales": "cabals",
    "kol": "influencers", "myid": "start",
}

# help metadata: name -> (usage, description, control)
HELP: list[tuple[str, str, bool]] = [
    ("/status", "desk health + equity", False),
    ("/portfolio", "full P&amp;L breakdown", False),
    ("/positions", "open positions (worst first)", False),
    ("/board [N]", "top opportunities", False),
    ("/token &lt;sym&gt;", "per-coin detail card", False),
    ("/wallets", "top tracked smart wallets", False),
    ("/cabals", "coordinated dump groups", False),
    ("/influencers", "KOL wallet activity", False),
    ("/signals [N] [kind]", "recent signals", False),
    ("/trades [N]", "recent closed trades", False),
    ("/alerts ...", "tune push alerts", False),
    ("/pause", "stop opening new entries", True),
    ("/resume", "re-enable entries", True),
    ("/scan", "force a scan now", True),
    ("/reset &lt;token&gt;", "wipe paper book (destructive)", True),
]
