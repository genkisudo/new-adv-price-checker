#!/usr/bin/env python3
"""PM data checker: Polymarket market data, money-weighted verdict and chart.

Usage: python pm_data_checker.py <gamma_market_id> [--out chart.png] [--tz Europe/Madrid] [--full] [--no-uma] [--no-wallets]

Public APIs only (no keys): Gamma (market), CLOB (book, price history),
Data API (trades, open interest, holders), and a public Polygon RPC for the
UMA Optimistic Oracle ProposePrice / DisputePrice / Settle events.
Dependencies: requests, matplotlib.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
POLYGON_RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org", "https://1rpc.io/matic"]
# UMA Optimistic Oracle V2 and Managed OOv2 on Polygon (Polymarket's UMA CTF adapters request from these)
UMA_OO = ["0xee3Afe347D5C74317041E2618C49534dAf887c24", "0x2C0367a9DB231dDeBd88a94b4f6461a6e47C58B1"]

THIN_LIQ = 50.0      # USD
WIDE_SPREAD = 0.30
EXTREME = 0.85       # post-start trades at/above this price are "near-certain" bets
S = requests.Session()
S.headers["User-Agent"] = "pm-data-checker/1.0"


# ---------------------------------------------------------------- keccak-256 (pure python, for UMA questionID matching)
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data) + b"\x01"
    while len(p) % rate:
        p += b"\x00"
    p[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for o in range(0, len(p), rate):
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(p[o + 8 * i:o + 8 * i + 8], "little")
        for rc in _RC:
            C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
            D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
            A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
            B = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
            A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            A[0][0] ^= rc
    return b"".join(A[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def _topic(sig):
    return "0x" + keccak256(sig.encode()).hex()


T_PROPOSE = _topic("ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)")
T_DISPUTE = _topic("DisputePrice(address,address,address,bytes32,uint256,bytes,int256)")
T_SETTLE = _topic("Settle(address,address,address,bytes32,uint256,bytes,int256,uint256)")


# ---------------------------------------------------------------- helpers
def get(url, **params):
    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5)


def parse_ts(s):
    """Gamma timestamps: '2026-10-18 13:30:00+00' or '2026-10-18T13:30:00Z' -> unix seconds."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00").replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def jl(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- data pulls
def fetch_trades(cond, max_pages=10, taker_only=True):
    out, seen = [], set()
    for page in range(max_pages):
        batch = get(f"{DATA}/trades", market=cond, limit=500, offset=page * 500,
                    takerOnly="true" if taker_only else "false") or []
        for t in batch:
            k = (t.get("transactionHash"), t.get("proxyWallet"), t.get("asset"), t.get("size"), t.get("price"), t.get("side"))
            if k not in seen:
                seen.add(k)
                out.append(t)
        if len(batch) < 500:
            break
    return sorted(out, key=lambda t: t["timestamp"])


def fetch_history(token, start_ts=None):
    h = (get(f"{CLOB}/prices-history", market=token, interval="max", fidelity=1) or {}).get("history") or []
    if not h and start_ts:  # fallback: explicit window (API rejects very long windows, so chunk by ~7 days)
        now = int(time.time())
        s = int(start_ts)
        while s < now:
            e = min(s + 7 * 86400, now)
            try:
                h += (get(f"{CLOB}/prices-history", market=token, startTs=s, endTs=e, fidelity=1) or {}).get("history") or []
            except requests.RequestException:
                break
            s = e
    return h


def book_top(token):
    b = get(f"{CLOB}/book", token_id=token)
    if not b:
        return dict(bids=[], asks=[], exists=False)
    bids = sorted(((fnum(x["price"]), fnum(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((fnum(x["price"]), fnum(x["size"])) for x in b.get("asks", []))
    return dict(bids=bids, asks=asks, exists=True)


# ---------------------------------------------------------------- UMA lookup via Polygon RPC
class Rpc:
    def __init__(self):
        self.urls = list(POLYGON_RPCS)

    def call(self, method, params):
        last = None
        for u in self.urls:
            try:
                r = S.post(u, json=dict(jsonrpc="2.0", id=1, method=method, params=params), timeout=30).json()
                if "result" in r and r["result"] is not None:
                    return r["result"]
                last = r.get("error")
            except Exception as e:  # noqa
                last = e
        raise RuntimeError(f"{method} failed on all RPCs: {last}")

    def block(self, n):
        b = self.call("eth_getBlockByNumber", [n if isinstance(n, str) else hex(n), False])
        return int(b["number"], 16), int(b["timestamp"], 16)

    def block_at(self, ts):
        hi, hts = self.block("latest")
        if ts >= hts:
            return hi
        lo = max(1, hi - int((hts - ts) / 1.5) - 500)
        lo, lts = self.block(lo)
        for _ in range(30):  # interpolation search
            if hi - lo <= 2:
                break
            guess = lo + int((ts - lts) * (hi - lo) / max(1, hts - lts))
            guess = min(max(guess, lo + 1), hi - 1)
            g, gts = self.block(guess)
            if abs(gts - ts) <= 3:
                return g
            if gts < ts:
                lo, lts = g, gts
            else:
                hi, hts = g, gts
        return lo


def _decode_uma(log):
    d = bytes.fromhex(log["data"][2:])
    w = lambda i: d[32 * i:32 * i + 32]  # noqa
    t0 = log["topics"][0]
    off = int.from_bytes(w(2), "big")
    ln = int.from_bytes(d[off:off + 32], "big")
    anc = d[off + 32:off + 32 + ln]
    price = int.from_bytes(w(3), "big", signed=True)
    kind = {T_PROPOSE: "proposed", T_DISPUTE: "disputed", T_SETTLE: "settled"}[t0]
    return kind, anc, price


def uma_events(market, start_ts, end_ts, max_blocks=60000, verbose=False):
    """Scan UMA OO logs between start_ts and end_ts for this market's question. Returns list of events."""
    qid = (market.get("questionID") or "").lower()
    mid = str(market.get("id"))
    title = (market.get("question") or "").encode("utf8", "ignore").decode()
    rpc = Rpc()
    b0, b1 = rpc.block_at(start_ts), rpc.block_at(end_ts)
    if b1 - b0 > max_blocks:
        b0 = b1 - max_blocks
    topics = [[T_PROPOSE, T_DISPUTE, T_SETTLE]]
    if market.get("resolvedBy"):
        topics.append("0x" + "0" * 24 + market["resolvedBy"][2:].lower())
    events, step, s = [], 500, b0
    while s <= b1:
        e = min(s + step - 1, b1)
        try:
            logs = rpc.call("eth_getLogs", [dict(fromBlock=hex(s), toBlock=hex(e), address=UMA_OO, topics=topics)])
        except RuntimeError:
            if step > 50:
                step //= 2
                continue
            raise
        for lg in logs:
            kind, anc, price = _decode_uma(lg)
            txt = anc.decode("utf8", "ignore")
            # cheap text pre-filter, then confirm with keccak256(ancillaryData) == questionID
            if not (f"market_id: {mid} " in txt or (title and title in txt)):
                continue
            if ("0x" + keccak256(anc).hex()) == qid or f"market_id: {mid} " in txt:
                events.append(dict(kind=kind, block=int(lg["blockNumber"], 16), price=price,
                                   tx=lg["transactionHash"], anc=txt))
        s = e + 1
    for ev in events:
        ev["ts"] = rpc.block(ev["block"])[1]
    return events


def uma_label(price_raw, anc, outcomes):
    """Map a UMA price (1e18 scaled) to an outcome name using the ancillary 'pN corresponds to X' text."""
    v = price_raw / 1e18
    m = re.search(r"p1 corresponds to ([^,]+), p2 to ([^,]+), p3 to ([^.]+)", anc)
    names = {0.0: "p1", 1.0: "p2", 0.5: "p3"}
    if m:
        p1, p2, p3 = (x.strip() for x in m.groups())
        label = {0.0: p1, 1.0: p2, 0.5: p3}.get(round(v, 2))
    else:
        label = names.get(round(v, 2))
    if label is None:
        return f"price {v}", None
    for i, o in enumerate(outcomes):
        if o.lower() == label.lower() or o.lower() in label.lower() or label.lower() in o.lower():
            return o, i
    return label, None


# ---------------------------------------------------------------- analysis
def best_levels(books):
    """Combine both tokens' books into outcome-0 terms. Returns (bid0, ask0, bid_usd, ask_usd)."""
    b0, b1 = books
    cand_bid = []  # (price_in_outcome0, usd)
    cand_ask = []
    if b0["bids"]:
        p, s = b0["bids"][0]; cand_bid.append((p, p * s))
    if b1["asks"]:
        p, s = b1["asks"][0]; cand_bid.append((1 - p, p * s))   # selling outcome1 at p == bidding outcome0 at 1-p
    if b0["asks"]:
        p, s = b0["asks"][0]; cand_ask.append((p, p * s))
    if b1["bids"]:
        p, s = b1["bids"][0]; cand_ask.append((1 - p, p * s))
    bid = max(cand_bid) if cand_bid else (None, 0)
    ask = min(cand_ask) if cand_ask else (None, 0)
    return bid[0], ask[0], bid[1], ask[1]


def trade_p0(t):
    """Trade price expressed as the implied probability of outcome 0, and USD notional."""
    p, s, idx = fnum(t["price"]), fnum(t["size"]), int(t.get("outcomeIndex", 0))
    return (p if idx == 0 else 1 - p), p * s


def verdict(outcomes, trades, books, start_ts, hist0=None, stop_ts=None):
    notes = []
    post = [t for t in trades if start_ts and t["timestamp"] >= start_ts]
    pre = [t for t in trades if not start_ts or t["timestamp"] < start_ts]

    comps = []  # (p0, weight_usd, label)
    # (a) post-start trades: money-weighted, later trades count more (weight doubles across the in-play window)
    if post:
        t_first, t_last = post[0]["timestamp"], post[-1]["timestamp"]
        span = max(1, t_last - t_first)
        num = den = 0.0
        for t in post:
            p0, usd = trade_p0(t)
            w = usd * (1 + (t["timestamp"] - t_first) / span)
            num += p0 * w; den += w
        comps.append((num / den, den, "post-start trades"))

    # (b) resting book
    bid0, ask0, bid_usd, ask_usd = best_levels(books)
    book_p = None
    if bid0 is not None and ask0 is not None:
        book_p, book_w = (bid0 + ask0) / 2, min(bid_usd, ask_usd) + 0.25 * abs(bid_usd - ask_usd)
        if ask0 - bid0 > WIDE_SPREAD:
            book_w *= 0.5
            notes.append(f"wide spread ({ask0 - bid0:.2f})")
    elif bid0 is not None:       # someone bids outcome0, nobody offers it
        book_p, book_w = (bid0 + 1) / 2, bid_usd
        notes.append(f"{outcomes[0]} bid {bid0:.2f} with no {outcomes[0]} offers")
    elif ask0 is not None:       # outcome0 offered, nobody bids -> outcome1 bid with no offers
        book_p, book_w = ask0 / 2, ask_usd
        notes.append(f"{outcomes[0]} offered at {ask0:.2f} with no bids ({outcomes[1]} favoured)")
    if book_p is not None:
        comps.append((book_p, book_w, "resting book"))

    # (b2) no live book (closed market / empty book): use the time-averaged post-start displayed price, low weight
    if book_p is None and hist0 and start_ts:
        pts = [x for x in hist0 if x["t"] >= start_ts and (not stop_ts or x["t"] <= stop_ts)]
        if len(pts) >= 2:
            tw = sum(b["t"] - a["t"] for a, b in zip(pts, pts[1:])) or 1
            avg = sum(a["p"] * (b["t"] - a["t"]) for a, b in zip(pts, pts[1:])) / tw
            comps.append((avg, 10.0, "post-start displayed price, time-avg (no live book; fixed $10 weight)"))

    # (c) post-start trades at extreme prices
    extreme = {0: 0.0, 1: 0.0}
    for t in post:
        p, idx, side = fnum(t["price"]), int(t.get("outcomeIndex", 0)), t.get("side")
        usd = p * fnum(t["size"])
        if side == "BUY" and p >= EXTREME:
            extreme[idx] += usd
        elif side == "SELL" and p <= 1 - EXTREME:
            extreme[1 - idx] += usd
    for i in (0, 1):
        if extreme[i] > 0:
            comps.append((1.0 if i == 0 else 0.0, 2 * extreme[i], f"extreme post-start {outcomes[i]} buys"))

    used_pre = False
    total_w = sum(w for _, w, _ in comps)
    if total_w < 5 and pre:  # nothing informative after the start: fall back to pre-start opinion
        num = den = 0.0
        for t in pre:
            p0, usd = trade_p0(t); num += p0 * usd; den += usd
        comps.append((num / den, den, "pre-start trades (opinion only)"))
        used_pre = True
        total_w = sum(w for _, w, _ in comps)

    if total_w < 5:
        return dict(text="not enough data", p0=None, idx=None, comps=comps, notes=notes, extreme=extreme)
    p0 = sum(p * w for p, w, _ in comps) / total_w
    idx = 0 if p0 >= 0.5 else 1
    conf = abs(p0 - 0.5) * 2
    one_sided = book_p is not None and (bid0 is None or ask0 is None)
    agree = [c for c in comps if (c[0] >= 0.5) == (idx == 0) and c[0] != 0.5]
    if conf < 0.2:
        text = "not enough data (toss-up)"
        idx = None
    elif conf >= 0.6 and total_w >= 50 and not used_pre and (extreme[idx] > 0 or one_sided) and len(agree) == len(comps):
        text = f"leans {outcomes[idx]} (strong)"
    else:
        text = f"leans {outcomes[idx]} (weak)"
    if used_pre:
        notes.append("no in-play signal; verdict uses pre-start trades only")
    if total_w < 50:
        notes.append(f"only ${total_w:,.0f} of money-weight behind the verdict")
    return dict(text=text, p0=p0, idx=idx, comps=comps, notes=notes, extreme=extreme, weight=total_w)


# ---------------------------------------------------------------- chart
def draw_chart(path, m, outcomes, hist, trades, books, lines, warn, tz, verdict_text, liq, full=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    colors = ["#1f77b4", "#d62728"]
    fig, ax = plt.subplots(figsize=(12, 6.2), dpi=110)
    to_dt = lambda ts: datetime.fromtimestamp(ts, tz)  # noqa
    for i, h in enumerate(hist):
        if h:
            ax.step([to_dt(x["t"]) for x in h], [x["p"] for x in h], where="post", color=colors[i], lw=1.4,
                    alpha=0.75, label=f"{outcomes[i]} price (history, midpoint-style)")
    if trades:
        usd = [fnum(t["price"]) * fnum(t["size"]) for t in trades]
        mx = max(usd) or 1
        for i in (0, 1):
            for side, mk in (("BUY", "o"), ("SELL", "v")):
                sel = [(t, u) for t, u in zip(trades, usd) if int(t.get("outcomeIndex", 0)) == i and t.get("side") == side]
                if not sel:
                    continue
                ax.scatter([to_dt(t["timestamp"]) for t, _ in sel], [fnum(t["price"]) for t, _ in sel],
                           s=[30 + 900 * (u / mx) ** 0.5 for _, u in sel], c=colors[i], marker=mk,
                           alpha=0.45, edgecolors="black", linewidths=0.6,
                           label=f"{outcomes[i]} {side.lower()} fills (size = USD)")
    # current book: bid/ask band for outcome 0 at the right edge
    bid0, ask0, _, _ = best_levels(books)
    now = datetime.now(tz)
    xs = [to_dt(x["t"]) for h in hist for x in h] + [to_dt(t["timestamp"]) for t in trades] + [now]
    x_lo = min(xs)
    lines = sorted((ln for ln in lines if ln[1] <= now.timestamp()), key=lambda ln: ln[1])  # skip future markers
    if not full:  # zoom to the action: a few hours before the first trade / event start
        anchors = [t["timestamp"] for t in trades] + [ln[1] for ln in lines]
        if anchors:
            x_lo = max(x_lo, to_dt(min(anchors) - 3 * 3600))
    for k, (name, ts, col, ls) in enumerate(lines):
        ax.axvline(to_dt(ts), color=col, ls=ls, lw=1.2)
        ax.text(to_dt(ts), 1.012 + 0.035 * (k % 3), name, rotation=0, color=col, fontsize=8, ha="center",
                va="bottom", transform=ax.get_xaxis_transform())
    if bid0 is not None or ask0 is not None:
        lo = bid0 if bid0 is not None else 0.0
        hi = ask0 if ask0 is not None else 1.0
        ax.axhspan(lo, hi, xmin=0.93, xmax=1.0, color=colors[0], alpha=0.15)
        ax.text(1.0, (lo + hi) / 2, f" {outcomes[0]} bid/ask\n {lo:.2f}/{hi:.2f}", transform=ax.get_yaxis_transform(),
                fontsize=8, va="center", ha="left", color=colors[0])
    b0, b1 = books
    fmt = lambda lv: f"{lv[0][0]:.2f} x {lv[0][1]:,.0f}" if lv else "none"  # noqa
    box = (f"Current book\n{outcomes[0]}: bid {fmt(b0['bids'])} | ask {fmt(b0['asks'])}\n"
           f"{outcomes[1]}: bid {fmt(b1['bids'])} | ask {fmt(b1['asks'])}\n"
           f"Liquidity ${liq:,.2f}\nVerdict: {verdict_text}")
    box = box.replace("$", r"\$")
    ax.text(0.01, 0.98, box, transform=ax.transAxes, fontsize=8.5, va="top", family="monospace",
            bbox=dict(boxstyle="round", fc="white", ec="grey", alpha=0.9))
    ax.set_ylim(-0.03, 1.03)
    ax.set_ylabel("price (= implied probability)")
    ax.set_xlim(x_lo, now)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M", tz=tz))
    fig.autofmt_xdate()
    ax.grid(alpha=0.25)
    title = f"{m['question']}  (market {m['id']})"
    ax.set_title(title, fontsize=11, pad=34)
    if warn:
        fig.text(0.5, 0.955, ("WARNING: thin book: displayed price unreliable  (" + warn + ")").replace("$", r"\$"), ha="center",
                 color="white", fontsize=11, weight="bold", bbox=dict(fc="#c62828", ec="none", boxstyle="round"))
    ax.legend(loc="lower left", fontsize=7.5, framealpha=0.9, markerscale=0.5)
    ax.set_xlabel(f"time ({tz.key})")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- conviction-weighted orders
TIMING = {"pre-start": 1.0, "in-play": 2.0, "post-end": 4.0}
RESTING_DISCOUNT = 0.3    # a resting order is intent, not money spent
MM_DISCOUNT = 0.25        # extra discount for book levels matching a known market-maker ladder
EVENT_LEN = 3 * 3600      # assumed event length after start when Gamma has no real end time
# recurring ladder seen on many sports props (price, size) on ONE token's bids; mirrored on the other token's asks
MM_LADDER = [(0.01, 3000), (0.03, 100), (0.12, 100), (0.5, 5), (0.8, 30)]


def extremity(q):
    """Conviction multiplier for paying/bidding q for the side you support.
    q = 0.5 -> 1 (neutral). Above 0.5 it rises convexly: 1 + 9 * ((q-0.5)/0.5)^1.5
    (0.6 -> 1.8, 0.7 -> 3.3, 0.8 -> 5.2, 0.85 -> 6.3, 0.9 -> 7.4, 0.99 -> 9.7).
    Below 0.5 it falls linearly to 0.2 at q = 0 (a 10c lottery ticket -> 0.36): cheap buys say little."""
    q = min(max(q, 0.0), 1.0)
    if q >= 0.5:
        return 1 + 9 * ((q - 0.5) / 0.5) ** 1.5
    return 0.2 + 0.8 * (q / 0.5)


def post_boundary(start_ts, end_ts, proposals):
    """Time after which the event is assumed over: earliest of first UMA proposal, start + 3h, real endDate."""
    c = [u["ts"] for u in proposals]
    if start_ts:
        c.append(start_ts + EVENT_LEN)
        if end_ts and end_ts > start_ts + 600:
            c.append(end_ts)
    return min(c) if c else None


def phase_of(ts, start_ts, post_ts):
    if post_ts and ts >= post_ts:
        return "post-end"
    if start_ts and ts >= start_ts:
        return "in-play"
    return "pre-start"


def mm_ladder_levels(book_levels):
    """Indices of levels matching the known MM ladder (price within 0.005, size within 15%); flagged if >= 3 match."""
    hits = [k for k, (p, s) in enumerate(book_levels)
            if any(abs(p - lp) < 0.005 and abs(s - ls) <= 0.15 * ls for lp, ls in MM_LADDER)]
    return set(hits) if len(hits) >= 3 else set()


def book_mm_flags(books, outcomes):
    """Detect market-maker patterns on the book. Returns list of dicts {kind, outcome_idx, text}."""
    flags = []
    for k, b in enumerate(books):
        if mm_ladder_levels(b["bids"]):
            top = max((p for i, (p, s) in enumerate(b["bids"]) if i in mm_ladder_levels(b["bids"])), default=None)
            flags.append(dict(kind="ladder", outcome_idx=k,
                              text=f"recurring MM ladder (3000@0.01 … 30@0.80) bidding {outcomes[k]}, top {top:.2f}"))
    b = books[0]
    if len(b["bids"]) >= 3 and len(b["asks"]) >= 3:
        bu = sum(p * s for p, s in b["bids"][:5])
        au = sum((1 - p) * s for p, s in b["asks"][:5])
        sp = b["asks"][0][0] - b["bids"][0][0]
        if sp <= 0.1 and min(bu, au) / max(bu, au, 1e-9) >= 0.4:
            flags.append(dict(kind="symmetric", outcome_idx=None,
                              text=f"symmetric two-sided quoting around {(b['asks'][0][0] + b['bids'][0][0]) / 2:.2f} (spread {sp:.2f})"))
    return flags


def conviction(outcomes, trades, books, start_ts, post_ts, now=None):
    """Score every taker fill and resting order: score = USD * extremity(q) * timing * discount.
    q is the price in terms of the side the order SUPPORTS (buy X at p -> X at p; sell X at p -> other at 1-p;
    bid X at b -> X at b; ask X at a -> other at 1-a)."""
    now = now or time.time()
    items = []
    for t in trades:
        p, s, k = fnum(t["price"]), fnum(t["size"]), int(t.get("outcomeIndex", 0))
        sup, q = (k, p) if t.get("side") == "BUY" else (1 - k, 1 - p)
        usd_ = q * s
        ph = phase_of(t["timestamp"], start_ts, post_ts)
        e, tm = extremity(q), TIMING[ph]
        items.append(dict(kind="fill", ts=t["timestamp"], wallet=t.get("proxyWallet"), name=t.get("name") or t.get("pseudonym"),
                          outcome_idx=sup, outcome=outcomes[sup], usd=usd_, q=q, phase=ph, extremity=e, timing=tm,
                          discount=1.0, score=usd_ * e * tm, mm=False,
                          desc=f"{t.get('side', '').lower()} {t.get('outcome')} @ {p:.3f}"))
    # resting orders: books are mirrored across the two tokens, so read one token only
    k0 = 0 if books and books[0]["exists"] else (1 if len(books) > 1 and books[1]["exists"] else None)
    if k0 is not None:
        b = books[k0]
        ph = phase_of(now, start_ts, post_ts)
        ladder = mm_ladder_levels(b["bids"])
        other_ladder = mm_ladder_levels(books[1 - k0]["bids"]) if len(books) > 1 else set()
        other_prices = {round(1 - books[1 - k0]["bids"][i][0], 3) for i in other_ladder} if other_ladder else set()
        for i, (p, s) in enumerate(b["bids"]):
            mm = i in ladder
            d = RESTING_DISCOUNT * (MM_DISCOUNT if mm else 1)
            items.append(dict(kind="resting", ts=now, wallet=None, name="resting order", outcome_idx=k0, outcome=outcomes[k0],
                              usd=p * s, q=p, phase=ph, extremity=extremity(p), timing=TIMING[ph], discount=d,
                              score=p * s * extremity(p) * TIMING[ph] * d, mm=mm, desc=f"bid {outcomes[k0]} @ {p:.3f} x {s:,.0f}"))
        for p, s in b["asks"]:
            sup, q = 1 - k0, 1 - p
            mm = round(p, 3) in other_prices
            d = RESTING_DISCOUNT * (MM_DISCOUNT if mm else 1)
            items.append(dict(kind="resting", ts=now, wallet=None, name="resting order", outcome_idx=sup, outcome=outcomes[sup],
                              usd=q * s, q=q, phase=ph, extremity=extremity(q), timing=TIMING[ph], discount=d,
                              score=q * s * extremity(q) * TIMING[ph] * d, mm=mm,
                              desc=f"ask {outcomes[k0]} @ {p:.3f} x {s:,.0f} (= bid {outcomes[sup]} @ {q:.3f})"))
    score = [sum(x["score"] for x in items if x["outcome_idx"] == i) for i in (0, 1)]
    tot = sum(score)
    p0 = None
    if tot > 0:
        # implied P: a fill at q >= 0.5 says "at least q"; below 0.5 it is treated as 50/50 evidence
        num = sum(x["score"] * (max(x["q"], 0.5) if x["outcome_idx"] == 0 else 1 - max(x["q"], 0.5)) for x in items)
        p0 = num / tot
    items.sort(key=lambda x: -x["score"])
    return dict(items=items, score=score, share0=score[0] / tot if tot else None, p0=p0, total=tot,
                usd=sum(x["usd"] for x in items))


def combine_verdict(base, conv, outcomes, min_score=5.0):
    """Blend the money-weighted verdict (60%) with the conviction-implied probability (40%)."""
    v = dict(base)
    v["notes"] = list(base["notes"])
    if conv["p0"] is None or conv["total"] < min_score:
        v["conviction_used"] = False
        return v
    if abs(conv["p0"] - 0.5) < 0.1:      # conviction is neutral (mostly cheap buys): don't dilute the base read
        v["conviction_used"] = False
        v["notes"].append("conviction-weighted orders are neutral (mostly cheap / near-50c orders)")
        return v
    v["conviction_used"] = True
    p = conv["p0"] if base["p0"] is None else 0.6 * base["p0"] + 0.4 * conv["p0"]
    conv_idx = 0 if conv["p0"] >= 0.5 else 1
    v["p0_base"], v["p0"] = base["p0"], p
    conf = abs(p - 0.5) * 2
    idx = 0 if p >= 0.5 else 1
    if conf < 0.2:
        v["idx"], v["text"] = None, "not enough data (toss-up)"
    else:
        strong = "strong" in base["text"] and base["idx"] == idx == conv_idx and conf >= 0.6
        v["idx"], v["text"] = idx, f"leans {outcomes[idx]} ({'strong' if strong else 'weak'})"
    if base["idx"] is not None and conv_idx != base["idx"]:
        v["notes"].append(f"conviction-weighted orders lean {outcomes[conv_idx]}, against the money-weighted read")
    return v


# ---------------------------------------------------------------- wallets & track record
_CACHE = {}


def _cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def _category(slug):
    return (slug or "").split("-")[0].lower()


def wallet_record(wallet, exclude_cond=None, category=None, pages=4):
    """Track record over the wallet's recent resolved markets. Cached 10 min.
    Wins/losses come from two endpoints because losing positions are usually never redeemed:
      /closed-positions (sortBy=TIMESTAMP desc, up to pages*50): realized; win = realizedPnl > 0
      /positions?redeemable=true (up to 500): resolved but unredeemed; curPrice 1 = win, 0 = loss (cashPnl)
    Unredeemed positions are only counted if their endDate falls inside the closed-positions time window,
    so both halves cover the same period."""
    def load():
        closed = []
        for pg in range(pages):
            b = get(f"{DATA}/closed-positions", user=wallet, limit=50, offset=pg * 50,
                    sortBy="TIMESTAMP", sortDirection="DESC") or []
            closed += b
            if len(b) < 50:
                break
        red = get(f"{DATA}/positions", user=wallet, redeemable="true", limit=500, sizeThreshold=0) or []
        return closed, red
    closed, red = _cached(("rec", wallet, pages), 600, load)
    closed = [r for r in closed if r.get("conditionId") != exclude_cond]
    red = [r for r in red if r.get("conditionId") != exclude_cond]
    if closed and len(closed) >= pages * 50 - 1:
        since = datetime.fromtimestamp(min(r.get("timestamp", 0) for r in closed), timezone.utc).strftime("%Y-%m-%d")
        red = [r for r in red if (r.get("endDate") or "9999") >= since]
    rows = [(r, fnum(r.get("realizedPnl")) > 0, fnum(r.get("realizedPnl"))) for r in closed] + \
           [(r, fnum(r.get("curPrice")) >= 0.99, fnum(r.get("cashPnl"))) for r in red
            if fnum(r.get("curPrice")) >= 0.99 or fnum(r.get("curPrice")) <= 0.01]
    n = len(rows)
    wins = sum(1 for _, w, _ in rows if w)
    cat = [x for x in rows if category and _category(x[0].get("eventSlug") or x[0].get("slug")) == category]
    cw = sum(1 for _, w, _ in cat if w)
    return dict(n=n, wins=wins, win_rate=wins / n if n else None, pnl=sum(p for _, _, p in rows),
                capped=len(closed) >= pages * 50 - 1 or len(red) >= 499, n_unredeemed=len(red),
                cat_n=len(cat), cat_wins=cw, cat_win_rate=cw / len(cat) if cat else None,
                cat_pnl=sum(p for _, _, p in cat))


def credibility(rec, mm=False):
    """1 = neutral. Shrunk win rate (wins+5)/(n+10), scaled by sample size (full effect at n >= 20).
    cred = clamp(1 + 4*(wr_shrunk-0.5)*min(1, n/20), 0.25, 3); x0.75 if PnL < 0 with n >= 20; x0.5 if MM-like."""
    n = rec["n"] if rec else 0
    if not rec or not n:
        c = 1.0
    else:
        wr = (rec["wins"] + 5) / (n + 10)
        c = 1 + 4 * (wr - 0.5) * min(1.0, n / 20)
        if n >= 20 and rec["pnl"] < 0:
            c *= 0.75
        c = min(max(c, 0.25), 3.0)
    return c * (0.5 if mm else 1.0)


def wallet_analysis(m, outcomes, all_trades, holder_groups, start_ts, post_ts, lean_idx, max_wallets=25, workers=12):
    """Per-wallet behaviour in this market + track record, and a track-record weighted implied probability."""
    from concurrent.futures import ThreadPoolExecutor
    W = {}

    def w_(addr):
        return W.setdefault(addr, dict(wallet=addr, name=None, fills=0, buy_usd=[0.0, 0.0], sell_usd=[0.0, 0.0],
                                       pre_buy=0.0, post_start_buy=0.0, after_end=[0.0, 0.0], net_tr=[0.0, 0.0],
                                       cost=[0.0, 0.0], bought=[0.0, 0.0], held=None, involvement=0.0))
    for t in all_trades:
        a = t.get("proxyWallet")
        if not a:
            continue
        x = w_(a)
        x["name"] = x["name"] or t.get("name") or t.get("pseudonym")
        k, p, s = int(t.get("outcomeIndex", 0)), fnum(t["price"]), fnum(t["size"])
        x["fills"] += 1
        x["involvement"] += p * s
        sign = 1 if t.get("side") == "BUY" else -1
        if sign > 0:
            x["buy_usd"][k] += p * s
            x["cost"][k] += p * s
            x["bought"][k] += s
            if start_ts and t["timestamp"] >= start_ts:
                x["post_start_buy"] += p * s
            else:
                x["pre_buy"] += p * s
        else:
            x["sell_usd"][k] += p * s
        x["net_tr"][k] += sign * s
        if post_ts and t["timestamp"] >= post_ts:
            x["after_end"][k] += sign * s
    for g in holder_groups:
        for h in g.get("holders") or []:
            x = w_(h["proxyWallet"])
            x["name"] = x["name"] or h.get("name") or h.get("pseudonym")
            if x["held"] is None:
                x["held"] = [0.0, 0.0]
            x["held"][int(h.get("outcomeIndex", 0))] += fnum(h["amount"])
            x["involvement"] += 0.5 * fnum(h["amount"])
    ranked = sorted(W.values(), key=lambda x: -x["involvement"])[:max_wallets]
    cat = _category(m.get("slug")) if m.get("sportsMarketType") or m.get("gameStartTime") else None
    cond = m.get("conditionId")
    with ThreadPoolExecutor(workers) as ex:
        recs = list(ex.map(lambda x: _safe_record(x["wallet"], cond, cat), ranked))
    rows = []
    num = den = 0.0
    for x, rec in zip(ranked, recs):
        pos = x["held"] if x["held"] is not None else [max(0.0, v) for v in x["net_tr"]]
        net = pos[0] - pos[1]                         # YES/NO pairs cancel (merged = no view)
        side = None if abs(net) < 1e-6 else (0 if net > 0 else 1)
        avg = (x["cost"][side] / x["bought"][side]) if side is not None and x["bought"][side] else 0.5
        exposure = abs(net) * avg
        two_sided = (min(x["buy_usd"]) > 0 and min(x["buy_usd"]) / max(x["buy_usd"]) >= 0.25) or \
                    (any(x["buy_usd"][k] > 0 and x["sell_usd"][k] > 0 for k in (0, 1)) and x["fills"] >= 4)
        flags = []
        if two_sided:
            flags.append("MM-like (two-sided)")
        if rec and rec["n"] >= 20 and (rec["win_rate"] or 0) >= 0.6 and rec["pnl"] > 0:
            flags.append(f"informed ({rec['win_rate']:.0%} of {rec['n']}{'+' if rec['capped'] else ''})")
        added = [k for k in (0, 1) if x["after_end"][k] > 1e-6]
        for k in added:
            flags.append(f"added {outcomes[k]} after end" + (" ✓ winning-looking side" if lean_idx == k else ""))
        reduced = [k for k in (0, 1) if x["after_end"][k] < -1e-6]
        for k in reduced:
            flags.append(f"reduced {outcomes[k]} after end")
        cred = credibility(rec, two_sided)
        if side is not None:
            num += cred * exposure * (1 if side == 0 else 0)
            den += cred * exposure
        rows.append(dict(wallet=x["wallet"], name=x["name"], fills=x["fills"], pre_buy=x["pre_buy"],
                         post_start_buy=x["post_start_buy"], after_end=x["after_end"], net_shares=net,
                         side=outcomes[side] if side is not None else "flat/both", side_idx=side, exposure=exposure,
                         held=x["held"] is not None, record=rec, credibility=cred, flags=flags, mm=two_sided,
                         informed=any(f.startswith("informed") for f in flags),
                         added_winning=lean_idx is not None and lean_idx in added))
    return dict(rows=rows, p0=num / den if den >= 5 else None, weight=den, category=cat,
                n_informed=sum(r["informed"] for r in rows), n_mm=sum(r["mm"] for r in rows),
                n_added_winning=sum(r["added_winning"] for r in rows), n_wallets_total=len(W))


def _safe_record(wallet, cond, cat):
    try:
        return wallet_record(wallet, cond, cat)
    except Exception:  # noqa
        return None


# ---------------------------------------------------------------- importable analysis
def resolve_ref(ref):
    """Accept a Gamma market id, a polymarket.com URL, or a slug. Returns the Gamma market dict (or None).
    Event URLs (several markets) resolve to the event's markets; we return the first and the list in m['_siblings']."""
    ref = str(ref).strip().rstrip("/")
    if re.fullmatch(r"\d+", ref):
        return get(f"{GAMMA}/markets/{ref}")
    slug = ref.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    m = get(f"{GAMMA}/markets/slug/{slug}")
    if isinstance(m, dict) and m.get("id"):
        ev = get(f"{GAMMA}/events/slug/{slug}")  # an event URL whose main market shares the slug
        if isinstance(ev, dict) and len(ev.get("markets") or []) > 1:
            m["_siblings"] = [(x["id"], x.get("question")) for x in ev["markets"]]
        return m
    for closed in ("false", "true"):  # the list endpoint hides closed markets unless asked
        res = get(f"{GAMMA}/markets", slug=slug, closed=closed) or []
        if res:
            return res[0]
    ev = get(f"{GAMMA}/events/slug/{slug}")
    if not (isinstance(ev, dict) and ev.get("markets")):
        evs = get(f"{GAMMA}/events", slug=slug) or get(f"{GAMMA}/events", slug=slug, closed="true") or []
        ev = evs[0] if evs else None
    if ev and ev.get("markets"):
        mk = ev["markets"]
        m = get(f"{GAMMA}/markets/{mk[0]['id']}")
        if m:
            m["_siblings"] = [(x["id"], x.get("question")) for x in mk]
        return m
    return None


def market_status(m):
    prices = [fnum(x) for x in jl(m.get("outcomePrices"))]
    outcomes = jl(m.get("outcomes"))
    if m.get("closed") and prices and max(prices) == 1.0:
        return "resolved", outcomes[prices.index(1.0)]
    st = (m.get("umaResolutionStatus") or "").lower()
    if st in ("proposed", "disputed", "challenged"):
        return st, None
    if m.get("closed"):
        return "closed", None
    return "open", None


def analyze(ref, use_uma=True, use_wallets=True):
    """Pull everything for one market and compute the verdict. Returns a plain dict (JSON-friendly)."""
    m = resolve_ref(ref)
    if not m:
        raise ValueError(f"market {ref!r} not found")
    outcomes = jl(m.get("outcomes"))
    tokens = jl(m.get("clobTokenIds"))
    prices = [fnum(x) for x in jl(m.get("outcomePrices"))]
    cond = m["conditionId"]
    start_ts = parse_ts(m.get("gameStartTime")) or parse_ts(m.get("eventStartTime"))
    end_ts = parse_ts(m.get("endDate"))
    closed_ts = parse_ts(m.get("closedTime"))
    liq = fnum(m.get("liquidity"))
    vol = fnum(m.get("volume"))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        f_books = [ex.submit(book_top, t) for t in tokens]
        f_hist = [ex.submit(fetch_history, t, parse_ts(m.get("startDate"))) for t in tokens]
        f_tr = ex.submit(fetch_trades, cond)
        f_all = ex.submit(fetch_trades, cond, 10, False) if use_wallets else None
        f_oi = ex.submit(get, f"{DATA}/oi", market=cond)
        f_h = ex.submit(get, f"{DATA}/holders", market=cond, limit=10)
        books = [f.result() for f in f_books]
        hist = [f.result() for f in f_hist]
        trades = f_tr.result()
        all_trades = f_all.result() if f_all else []
        oi_raw = f_oi.result() or []
        holders = f_h.result() or []
    oi = fnum(oi_raw[0]["value"]) if oi_raw else None

    uma, uma_err, uma_note = [], None, None
    if use_uma and not (m.get("umaResolutionStatus") or m.get("closed")):
        uma_note = "no proposal yet per Gamma; on-chain scan skipped"
    elif use_uma:
        s = start_ts or (end_ts - 6 * 3600 if end_ts else None) or parse_ts(m.get("startDate"))
        e = (closed_ts + 300) if closed_ts else time.time()
        try:
            if s and e > s:
                uma = uma_events(m, s, e)
            else:
                uma_err = "no usable time window"
        except Exception as ex:  # noqa
            uma_err = str(ex)[:200]
    for u in uma:
        u["label"], u["outcome_idx"] = uma_label(u["price"], u["anc"], outcomes)
    proposals = [u for u in uma if u["kind"] == "proposed"]
    disputes = [u for u in uma if u["kind"] == "disputed"]
    settles = [u for u in uma if u["kind"] == "settled"]

    base = verdict(outcomes, trades, books, start_ts, hist[0] if hist else None, closed_ts)
    post_ts = post_boundary(start_ts, end_ts, proposals)
    conv = conviction(outcomes, trades, books, start_ts, post_ts)
    v = combine_verdict(base, conv, outcomes)
    mm_flags = book_mm_flags(books, outcomes)
    status0, resolved0 = market_status(m)
    lean_idx = outcomes.index(resolved0) if resolved0 in outcomes else v["idx"]
    wallets = None
    if use_wallets:
        try:
            wallets = wallet_analysis(m, outcomes, all_trades, holders, start_ts, post_ts, lean_idx)
        except Exception as ex:  # noqa
            wallets = dict(error=str(ex)[:200], rows=[], p0=None, weight=0, n_informed=0, n_mm=0, n_added_winning=0)
    bid0, ask0, _, _ = best_levels(books)
    spread = (ask0 - bid0) if (bid0 is not None and ask0 is not None) else None
    warn_bits = []
    if liq < THIN_LIQ:
        warn_bits.append(f"liquidity ${liq:,.0f} < ${THIN_LIQ:.0f}")
    if spread is not None and spread > WIDE_SPREAD:
        warn_bits.append(f"spread {spread:.2f} > {WIDE_SPREAD}")
    elif bid0 is None and ask0 is None:
        warn_bits.append("empty order book")
    elif spread is None and liq < THIN_LIQ:
        warn_bits.append("one-sided book")

    # holders / concentration
    hold = []
    for g in holders:
        hs = g.get("holders") or []
        if not hs:
            continue
        i = int(hs[0].get("outcomeIndex", 0))
        total = max(oi or 0, sum(fnum(h["amount"]) for h in hs))  # binary market: shares per outcome == OI pairs
        hold.append(dict(outcome_idx=i, outcome=outcomes[i] if i < len(outcomes) else str(i), total=total,
                         top_share=fnum(hs[0]["amount"]) / total if total else None, holders=hs))
    top_conc = max((h["top_share"] for h in hold if h["top_share"] is not None), default=None)

    # UMA alignment
    align = None
    last = proposals[-1] if proposals else None
    if last:
        if disputes and disputes[-1]["ts"] > last["ts"]:
            align = "disputed"
        elif v["idx"] is None:
            align = "no lean"
        elif last["outcome_idx"] is None:
            align = "cannot compare"
        else:
            align = "aligned" if last["outcome_idx"] == v["idx"] else "not aligned"

    lines = []
    if start_ts:
        lines.append(("start", start_ts, "green", "--"))
    if end_ts and (not start_ts or abs(end_ts - start_ts) > 60):
        lines.append(("endDate", end_ts, "grey", ":"))
    for u in proposals:
        lines.append(("proposal", u["ts"], "purple", "-."))
    for u in disputes:
        lines.append(("dispute", u["ts"], "orange", "-."))
    for u in settles:
        lines.append(("settled", u["ts"], "brown", ":"))
    if closed_ts and not any(abs(u["ts"] - closed_ts) < 120 for u in settles):
        lines.append(("closed", closed_ts, "black", ":"))

    status, resolved = market_status(m)
    return dict(market=m, id=str(m["id"]), question=m.get("question"), outcomes=outcomes, tokens=tokens,
                prices=prices, start_ts=start_ts, end_ts=end_ts, closed_ts=closed_ts, liquidity=liq, volume=vol,
                oi=oi, books=books, hist=hist, trades=trades, holders=hold, top_concentration=top_conc,
                uma=uma, uma_err=uma_err, uma_note=uma_note, uma_skipped=not use_uma, proposals=proposals, disputes=disputes,
                settles=settles, alignment=align, verdict=v, bid0=bid0, ask0=ask0, spread=spread,
                warn=", ".join(warn_bits), lines=lines, status=status, resolved=resolved, base_verdict=base,
                conviction=conv, post_ts=post_ts, mm_flags=mm_flags, wallets=wallets, all_trades=all_trades)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("market_id", help="Gamma market id (e.g. 5649060), polymarket.com URL, or slug")
    ap.add_argument("--out", default=None, help="chart PNG path (default chart_<id>.png)")
    ap.add_argument("--tz", default="Europe/Madrid")
    ap.add_argument("--full", action="store_true", help="chart the whole price history instead of zooming to the action")
    ap.add_argument("--no-uma", action="store_true", help="skip the on-chain UMA proposal lookup")
    ap.add_argument("--no-wallets", action="store_true", help="skip the wallet track-record lookup")
    a = ap.parse_args()
    tz = ZoneInfo(a.tz)
    fmt_t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d %b %H:%M:%S") if ts else "n/a"  # noqa
    try:
        r = analyze(a.market_id, use_uma=not a.no_uma, use_wallets=not a.no_wallets)
    except ValueError as e:
        sys.exit(str(e))
    m, outcomes, trades, start_ts, v = r["market"], r["outcomes"], r["trades"], r["start_ts"], r["verdict"]

    P = print
    P(f"\n=== {r['question']}  (market {r['id']}) ===")
    P(f"Outcomes: {outcomes}  | page prices: {r['prices']} | status: {r['status']}")
    P(f"Event start: {fmt_t(start_ts)} | endDate: {fmt_t(r['end_ts'])} | closed: {fmt_t(r['closed_ts'])} ({a.tz})")
    P(f"UMA status (gamma): {m.get('umaResolutionStatus')} {m.get('umaResolutionStatuses') or ''} | source: {m.get('resolutionSource') or 'n/a'}")
    if r["resolved"]:
        P(f"RESOLVED: {r['resolved']}")
    P(f"OI: ${r['oi']:,.2f}" if r["oi"] is not None else "OI: n/a", end=" | ")
    P(f"Volume: {r['volume']:,.2f} | Liquidity: ${r['liquidity']:,.2f}")
    for i, b in enumerate(r["books"]):
        if not b["exists"]:
            P(f"Book {outcomes[i]}: no order book (closed / not accepting orders)")
        else:
            P(f"Book {outcomes[i]}: bids {b['bids'][:3]} | asks {b['asks'][:3]}")
    if r["warn"]:
        P(f"!! thin book: displayed price unreliable ({r['warn']})")

    def agg(ts_filter):
        out = {}
        for t in trades:
            if ts_filter(t["timestamp"]):
                k = f"{t['outcome']} {t['side']}"
                s_, u_ = out.get(k, (0, 0))
                out[k] = (s_ + fnum(t["size"]), u_ + fnum(t["size"]) * fnum(t["price"]))
        return ", ".join(f"{k}: {s_:,.0f} sh / ${u_:,.0f}" for k, (s_, u_) in sorted(out.items())) or "none"
    P(f"Trades: {len(trades)} total")
    P(f"  pre-start : {agg(lambda x: not start_ts or x < start_ts)}")
    P(f"  post-start: {agg(lambda x: start_ts and x >= start_ts)}")
    for t in trades[-5:]:
        P(f"    {fmt_t(t['timestamp'])} {t['outcome']:>6} {t['side']:<4} {fnum(t['size']):>9,.2f} @ {fnum(t['price']):.3f}")

    P("Top holders:")
    for h in r["holders"]:
        hs, total, top = h["holders"], h["total"], h["holders"][0]
        P(f"  {h['outcome']}: top wallet {top.get('name') or top.get('pseudonym') or top['proxyWallet'][:10]} "
          f"{fnum(top['amount']):,.0f} sh = {100 * fnum(top['amount']) / total:.0f}% of {h['outcome']} shares"
          f" | top-10 hold {100 * sum(fnum(x['amount']) for x in hs) / total:.0f}%")

    if r["resolved"]:
        P(f"FINAL: market resolved {r['resolved']}; the verdict below is what market data alone says")
    P(f"VERDICT: {v['text']}" + (f"  (blended P({outcomes[0]}) = {v['p0']:.2f}, weight ${v.get('weight', 0):,.0f})" if v["p0"] is not None else ""))
    for p0, w, lab in v["comps"]:
        P(f"  - {lab}: P({outcomes[0]})={p0:.2f}, weight ${w:,.0f}")
    for n in v["notes"]:
        P(f"  * {n}")

    c = r["conviction"]
    if c["p0"] is not None:
        P(f"CONVICTION: smart-money share {outcomes[0]} {c['share0']:.0%} / {outcomes[1]} {1 - c['share0']:.0%}, "
          f"conviction-implied P({outcomes[0]}) = {c['p0']:.2f} (score {c['total']:,.0f}; "
          f"{'blended into verdict' if v.get('conviction_used') else 'not blended'})")
        for x in c["items"][:5]:
            P(f"    {x['score']:>8,.1f}  {x['kind']:<7} {x['phase']:<9} {x['desc']}  (${x['usd']:,.2f})")
    for f in r["mm_flags"]:
        P(f"MM on book: {f['text']}")
    w = r["wallets"]
    if w:
        P(f"WALLETS: {len(w['rows'])} analysed of {w.get('n_wallets_total', len(w['rows']))}; informed {w['n_informed']}, "
          f"MM-like {w['n_mm']}, added winning-looking side after end {w['n_added_winning']}"
          + (f"; track-record weighted P({outcomes[0]}) = {w['p0']:.2f} (${w['weight']:,.0f} cred-weighted)" if w["p0"] is not None else ""))
        for x in w["rows"][:6]:
            rec = x["record"] or {}
            wr = f"{rec['win_rate']:.0%} of {rec['n']}" if rec.get("win_rate") is not None else "no record"
            P(f"    {(x['name'] or x['wallet'])[:22]:<22} {x['side']:<12} ${x['exposure']:>8,.0f}  {wr:<12} "
              f"PnL ${rec.get('pnl', 0):>10,.0f}  cred {x['credibility']:.2f}  {'; '.join(x['flags'])}")
    if r["uma_skipped"]:
        P("UMA: lookup skipped (--no-uma)")
    elif r["uma_note"]:
        P(f"UMA: {r['uma_note']}")
    elif r["uma_err"]:
        P(f"UMA: on-chain lookup failed ({r['uma_err']}); gamma status only")
    elif not r["uma"]:
        P("UMA: no ProposePrice event found for this question in the scanned window")
    else:
        for u in r["uma"]:
            P(f"UMA {u['kind']}: {u['label']} at {fmt_t(u['ts'])} (tx {u['tx'][:12]}…)")
        if r["alignment"]:
            last = r["proposals"][-1]
            P(f"UMA vs verdict: {r['alignment'].upper()} (proposed {last['label']}, verdict {v['text']})")

    out = a.out or f"chart_{r['id']}.png"
    draw_chart(out, m, outcomes, r["hist"], trades, r["books"], r["lines"], r["warn"], tz, v["text"], r["liquidity"], a.full)
    P(f"Chart: {out}")


if __name__ == "__main__":
    main()
