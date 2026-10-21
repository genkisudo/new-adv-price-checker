"""PM data checker - local web UI.  Run:  streamlit run app.py"""
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import pm_data_checker as pm

st.set_page_config(page_title="PM data checker", page_icon="📊", layout="wide")
COLORS = ["#1f77b4", "#d62728"]          # outcome 0 = blue, outcome 1 = red (used everywhere)
STATUS_BADGE = {"open": "🟢 open", "proposed": "🟣 proposed (UMA)", "disputed": "🟠 disputed",
                "challenged": "🟠 challenged", "resolved": "✅ resolved", "closed": "⚪ closed"}


@st.cache_data(ttl=60, show_spinner=False)
def load(ref, use_uma, use_wallets):
    # wallet track records are additionally cached for 10 min inside pm_data_checker
    return pm.analyze(ref, use_uma=use_uma, use_wallets=use_wallets)


def short(w):
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else (w or "")


def usd(x, nd=0):
    return "n/a" if x is None else f"${x:,.{nd}f}"


def esc(text):
    """Streamlit markdown treats $...$ as LaTeX; escape dollar signs."""
    return text.replace("$", "\\$")


def why_list(r):
    """Plain-English reasons behind the verdict."""
    o, v, out = r["outcomes"], r["verdict"], []
    if v.get("idx") == pm.FIFTY:
        for line in v["void"]["reasons"]:
            out.append(f"⚖️ {line}")
        for n in v["notes"]:
            out.append(f"ℹ️ {n}")
        return out
    for p0, w, lab in v["comps"]:
        lean = o[0] if p0 >= 0.5 else o[1]
        pct = p0 if p0 >= 0.5 else 1 - p0
        if lab == "post-start trades":
            out.append(f"💸 Money traded **after the start** (${w:,.0f}, later trades count more) prices **{lean}** at ~{pct:.0%}.")
        elif lab == "resting book":
            out.append(f"📖 Current **order book** (${w:,.0f} at the top levels) points to **{lean}** (~{pct:.0%}).")
        elif lab.startswith("extreme"):
            out.append(f"🎯 Someone **paid ≥85¢ after the start** for **{lean}**: a near-certain bet (counted 2×, ${w:,.0f}).")
        elif lab.startswith("post-start displayed"):
            out.append(f"📉 No live book any more; the **displayed price after the start** averaged {p0:.0%} for {o[0]} (low weight, $10).")
        elif lab.startswith("pre-start"):
            out.append(f"🕒 Only **pre-start** money exists (${w:,.0f}); it's opinion, not information: ~{pct:.0%} **{lean}**.")
    for n in v["notes"]:
        out.append(f"ℹ️ {n}")
    return out


def verdict_card(r, tz):
    v, o = r["verdict"], r["outcomes"]
    kind, strength = pm.verdict_kind(v)
    if kind == "outcome":
        head = f"Traders expect **{o[v['idx']]}**"
        color = "#1b5e20" if strength == "strong" else "#8d6e00"
    elif kind == "50-50":
        head = "Traders expect **50-50** (likely cancelled / void)"
        color = "#4a148c"
    elif kind == "toss-up":
        strength, head, color = "toss-up", "Traders see a **toss-up**", "#555"
    else:
        strength, head, color = "not enough data", "Traders' view is **unclear** (not enough data)", "#555"
    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"<div style='font-size:2rem;font-weight:700;color:{color}'>"
                        f"{head.replace('**', '')}</div>", unsafe_allow_html=True)
            p0 = v.get("p0")
            st.markdown(esc(f"Strength: **{strength}** · money weight behind it: **{usd(v.get('weight'))}**"
                        + (f" · implied P({o[0]}) = **{p0:.0%}**" if p0 is not None else "")))
            for line in why_list(r):
                st.markdown(esc(f"- {line}"))
            for line in extra_reads(r):
                st.markdown(esc(line))
        with c2:
            st.markdown("**Oracle (UMA)**")
            if r["uma_skipped"]:
                st.caption("UMA lookup skipped (toggle in sidebar). Gamma status: "
                           f"{r['market'].get('umaResolutionStatus') or 'n/a'}")
            elif r.get("uma_note"):
                st.caption(r["uma_note"].capitalize() + ".")
            elif r["uma_err"]:
                st.warning(f"On-chain lookup failed: {r['uma_err']}")
            elif not r["uma"]:
                st.caption("No proposal found on-chain yet.")
            for u in r["uma"]:
                t = datetime.fromtimestamp(u["ts"], tz).strftime("%d %b %H:%M")
                st.markdown(f"- {u['kind'].capitalize()}: **{u['label']}** at {t} "
                            f"([tx](https://polygonscan.com/tx/{u['tx']}))")
            al = r["alignment"]
            if al == "aligned":
                st.success("✅ ALIGNED: proposal matches what traders expect")
            elif al == "not aligned":
                st.error("❌ NOT ALIGNED: proposal disagrees with the market")
            elif al:
                st.info(f"Alignment: {al}")
            if r["resolved"]:
                st.success(f"🏁 Final resolution: **{r['resolved']}**")


def metrics_row(r):
    trades = r["trades"]
    c = st.columns(6)
    c[0].metric("Open interest", usd(r["oi"]))
    c[1].metric("Volume", f"{r['volume']:,.0f}")
    c[2].metric("Liquidity", usd(r["liquidity"], 2))
    c[3].metric("Spread", "n/a" if r["spread"] is None else f"{r['spread']:.2f}",
                help="Best ask minus best bid (outcome 0 terms). n/a = one-sided or empty book")
    c[4].metric("# trades", len(trades), f"{sum(1 for t in trades if r['start_ts'] and t['timestamp'] >= r['start_ts'])} after start",
                delta_color="off")
    c[5].metric("Top-wallet share", "n/a" if r["top_concentration"] is None else f"{r['top_concentration']:.0%}",
                help="Largest share of one outcome held by a single wallet")
    if r["warn"]:
        st.error(esc(f"⚠️ **Thin book: displayed price unreliable** ({r['warn']}). Use the verdict and fills, not the page price."))


def chart(r, tz, full, by_conv=False):
    o, fig = r["outcomes"], go.Figure()
    to_dt = lambda ts: datetime.fromtimestamp(ts, tz)  # noqa
    for i, h in enumerate(r["hist"]):
        if h:
            fig.add_trace(go.Scatter(x=[to_dt(p["t"]) for p in h], y=[p["p"] for p in h], mode="lines",
                                     line=dict(color=COLORS[i], shape="hv", width=2),
                                     name=f"{o[i]} price (line)", legendgroup=str(i),
                                     hovertemplate=f"{o[i]} %{{y:.3f}}<br>%{{x}}<extra></extra>"))
    trades = r["trades"]
    def conv_score(t):
        p, s, k = pm.fnum(t["price"]), pm.fnum(t["size"]), int(t.get("outcomeIndex", 0))
        q = p if t.get("side") == "BUY" else 1 - p
        return q * s * pm.extremity(q) * pm.TIMING[pm.phase_of(t["timestamp"], r["start_ts"], r.get("post_ts"))]
    metric = conv_score if by_conv else (lambda t: pm.fnum(t["price"]) * pm.fnum(t["size"]))
    if trades:
        mx = max(metric(t) for t in trades) or 1
        for i in range(len(o)):
            for side, sym in (("BUY", "circle"), ("SELL", "triangle-down")):
                sel = [t for t in trades if int(t.get("outcomeIndex", 0)) == i and t.get("side") == side]
                if not sel:
                    continue
                u = [pm.fnum(t["price"]) * pm.fnum(t["size"]) for t in sel]
                cs = [conv_score(t) for t in sel]
                sz = [metric(t) for t in sel]
                fig.add_trace(go.Scatter(
                    x=[to_dt(t["timestamp"]) for t in sel], y=[pm.fnum(t["price"]) for t in sel], mode="markers",
                    name=f"{o[i]} {side.lower()} fills (dot size = {'conviction' if by_conv else 'USD'})", legendgroup=str(i),
                    marker=dict(color=COLORS[i], symbol=sym, size=[8 + 40 * (x / mx) ** 0.5 for x in sz],
                                opacity=0.6, line=dict(color="black", width=1)),
                    customdata=[[o[i], side, pm.fnum(t["size"]), pm.fnum(t["price"]), x, short(t.get("proxyWallet")),
                                 to_dt(t["timestamp"]).strftime("%d %b %H:%M:%S"), c_] for t, x, c_ in zip(sel, u, cs)],
                    hovertemplate="%{customdata[6]}<br><b>%{customdata[0]} %{customdata[1]}</b><br>"
                                  "%{customdata[2]:,.2f} sh @ %{customdata[3]:.3f} = $%{customdata[4]:,.2f}"
                                  "<br>wallet %{customdata[5]}<br>conviction %{customdata[7]:,.1f}<extra></extra>"))
    now = datetime.now(tz)
    lines = sorted((ln for ln in r["lines"] if ln[1] <= now.timestamp()), key=lambda x: x[1])
    cmap = {"green": "green", "grey": "grey", "purple": "purple", "orange": "orange", "brown": "brown", "black": "black"}
    for k, (name, ts, col, _) in enumerate(lines):
        x = to_dt(ts)
        fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, yref="paper", line=dict(color=cmap[col], dash="dash", width=1.5))
        fig.add_annotation(x=x, y=1.02 + 0.06 * (k % 3), yref="paper", text=name, showarrow=False,
                           font=dict(color=cmap[col], size=11))
    if r["bid0"] is not None or r["ask0"] is not None:
        lo = r["bid0"] if r["bid0"] is not None else 0
        hi = r["ask0"] if r["ask0"] is not None else 1
        fig.add_hrect(y0=lo, y1=hi, fillcolor=COLORS[0], opacity=0.08, line_width=0,
                      annotation_text=f"current {o[0]} bid/ask {lo:.2f}/{hi:.2f}", annotation_position="top left")
    if not full:  # zoom: 3 h before the first trade/marker to 1 h after the last data point or marker
        anchors = [t["timestamp"] for t in trades] + [ln[1] for ln in lines]
        last = [h[-1]["t"] for h in r["hist"] if h] + anchors
        if anchors:
            fig.update_xaxes(range=[to_dt(min(anchors) - 3 * 3600), min(now, to_dt(max(last) + 3600))])
    fig.update_layout(height=480, margin=dict(t=70, b=30, l=10, r=10), yaxis=dict(range=[-0.03, 1.03], title="price = implied probability"),
                      xaxis_title=f"time ({tz.key})", legend=dict(orientation="h", y=-0.18), hovermode="closest",
                      title=dict(text=f"<span style='color:{COLORS[0]}'>■ {o[0]}</span>   "
                                      f"<span style='color:{COLORS[1]}'>■ {o[1]}</span>", x=0.01, y=0.98))
    st.plotly_chart(fig, width="stretch")


def book_tables(r):
    o = r["outcomes"]
    cols = st.columns(len(o))
    for i, b in enumerate(r["books"]):
        with cols[i]:
            st.markdown(f"<span style='color:{COLORS[i]};font-weight:700'>■ {o[i]}</span>", unsafe_allow_html=True)
            if not b["exists"]:
                st.caption("No order book (market closed / not accepting orders).")
                continue
            if b["bids"] and not b["asks"]:
                st.success(f"Bids for {o[i]} with **no asks**: buyers want {o[i]}, nobody sells it.")
            elif b["asks"] and not b["bids"]:
                st.warning(f"{o[i]} offered with **no bids**: nobody wants to buy {o[i]}.")
            rows = [dict(side="bid", price=p, shares=s, usd=p * s) for p, s in b["bids"][:10]] + \
                   [dict(side="ask", price=p, shares=s, usd=p * s) for p, s in b["asks"][:10]]
            if rows:
                df = pd.DataFrame(rows)
                st.dataframe(df.style.format({"price": "{:.3f}", "shares": "{:,.2f}", "usd": "${:,.2f}"})
                             .apply(lambda row: ["background-color:#e8f5e9" if row.side == "bid" else
                                                 "background-color:#ffebee"] * len(row), axis=1),
                             hide_index=True, width="stretch")
            else:
                st.caption("Book is empty.")


def trades_table(r, tz):
    if not r["trades"]:
        st.caption("No trades.")
        return
    rows = []
    for t in reversed(r["trades"]):
        p, s = pm.fnum(t["price"]), pm.fnum(t["size"])
        rows.append(dict(time=datetime.fromtimestamp(t["timestamp"], tz).strftime("%d %b %H:%M:%S"),
                         phase="post-start" if r["start_ts"] and t["timestamp"] >= r["start_ts"] else "pre-start",
                         outcome=t.get("outcome"), side=t.get("side"), shares=s, price=p, usd=s * p,
                         wallet=t.get("name") or short(t.get("proxyWallet"))))
    st.dataframe(pd.DataFrame(rows).style.format({"shares": "{:,.2f}", "price": "{:.3f}", "usd": "${:,.2f}"}),
                 hide_index=True, width="stretch")


def holders_table(r):
    if not r["holders"]:
        st.caption("No holders (positions closed or redeemed).")
        return
    cols = st.columns(len(r["holders"]))
    for c, h in zip(cols, r["holders"]):
        with c:
            st.markdown(f"<span style='color:{COLORS[h['outcome_idx'] % 2]};font-weight:700'>■ {h['outcome']}</span>"
                        f" · top wallet holds **{h['top_share']:.0%}**", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([dict(wallet=x.get("name") or short(x["proxyWallet"]), shares=pm.fnum(x["amount"]),
                                            share_of_outcome=pm.fnum(x["amount"]) / h["total"] if h["total"] else None)
                                       for x in h["holders"]])
                         .style.format({"shares": "{:,.2f}", "share_of_outcome": "{:.0%}"}),
                         hide_index=True, width="stretch")


def extra_reads(r):
    o, out = r["outcomes"], []
    c, v = r["conviction"], r["verdict"]
    if v.get("idx") == pm.FIFTY:
        out.append("_The two reads below describe positioning before the event was voided; "
                   "with a 50-50 payout expected they are context only._")
    if c["p0"] is not None:
        out.append(f"🧠 **Smart money (conviction-weighted)**: {o[0]} {c['share0']:.0%} / {o[1]} {1 - c['share0']:.0%} of conviction · "
                   f"implied P({o[0]}) = **{c['p0']:.0%}**"
                   + (f" (post-start + resting only: **{c['p0_post']:.0%}**)" if c.get("p0_post") is not None else "")
                   + f" · {'blended 40% into the verdict' if v.get('conviction_used') else 'not blended'}"
                   + (f" (money-only P was {v['p0_base']:.0%})" if v.get("p0_base") is not None else ""))
    w = r.get("wallets")
    if w and not w.get("error"):
        line = f"🏅 **Track-record read**: {len(w['rows'])} wallets"
        if w["p0"] is not None:
            line += f" → P({o[0]}) = **{w['p0']:.0%}** (${w['weight']:,.0f} credibility-weighted exposure)"
        else:
            line += " → not enough positions"
        line += f" · informed {w['n_informed']} · MM-like {w['n_mm']} · added winning side after end {w['n_added_winning']}"
        out.append(line)
    elif w and w.get("error"):
        out.append(f"🏅 Track-record lookup failed: {w['error']}")
    for f in r.get("mm_flags") or []:
        out.append(f"🤖 Market maker on book: {f['text']} (kept, discounted)")
    return out


def conviction_table(r, tz):
    c, o = r["conviction"], r["outcomes"]
    if not c["items"]:
        st.caption("No fills or resting orders.")
        return
    st.markdown(esc(f"Score = USD × extremity × timing × discount. Timing: pre-start 1×, in-play 2×, post-end 4×; "
                    f"resting orders ×{pm.RESTING_DISCOUNT} (×{pm.MM_DISCOUNT} more if they match an MM ladder). "
                    f"Total: **{o[0]} {c['score'][0]:,.0f}** vs **{o[1]} {c['score'][1]:,.0f}**."))
    rows = [dict(time=datetime.fromtimestamp(x["ts"], tz).strftime("%d %b %H:%M:%S") if x["kind"] == "fill" else "now",
                 who=(x["name"] or short(x["wallet"])) if x["kind"] == "fill" else ("resting order (MM ladder)" if x["mm"] else "resting order"),
                 supports=x["outcome"], order=x["desc"], usd=x["usd"], price_for_side=x["q"], phase=x["phase"],
                 extremity=x["extremity"], timing=x["timing"], discount=x["discount"], score=x["score"],
                 share=x["score"] / c["total"] if c["total"] else 0) for x in c["items"][:40]]
    st.markdown("**Top conviction contributors**")
    st.dataframe(pd.DataFrame(rows).style.format({"usd": "${:,.2f}", "price_for_side": "{:.3f}", "extremity": "{:.2f}",
                                                  "timing": "{:.0f}×", "discount": "{:.2f}", "score": "{:,.1f}", "share": "{:.0%}"})
                 .apply(lambda row: [f"color:{COLORS[o.index(row['supports']) % 2]}" if c_ == "supports" else "" for c_ in row.index], axis=1),
                 hide_index=True, width="stretch")


def wallets_table(r):
    w, o = r.get("wallets"), r["outcomes"]
    if not w:
        st.caption("Wallet lookup skipped (sidebar toggle).")
        return
    if w.get("error"):
        st.warning(w["error"])
    st.markdown(esc(
        f"{len(w['rows'])} of {w.get('n_wallets_total', len(w['rows']))} wallets (top holders + traders, ranked by involvement). "
        f"Track record = recent resolved markets (closed positions + unredeemed resolved positions), same-category = slug prefix "
        f"`{w.get('category') or 'n/a'}`. Credibility 1 = neutral; MM-like wallets are kept but halved."
        + (f" **Track-record weighted P({o[0]}) = {w['p0']:.0%}.**" if w["p0"] is not None else "")))
    rows = []
    for x in w["rows"]:
        rec = x["record"] or {}
        rows.append(dict(wallet=x["name"] or short(x["wallet"]), address=short(x["wallet"]), side=x["side"],
                         net_shares=abs(x["net_shares"]), exposure=x["exposure"], fills=x["fills"],
                         buy_pre_start=x["pre_buy"], buy_after_start=x["post_start_buy"],
                         after_end=", ".join(f"{'+' if v > 0 else ''}{v:,.0f} {o[k]}" for k, v in enumerate(x["after_end"]) if abs(v) > 1e-6) or "-",
                         resolved=rec.get("n"), win_rate=rec.get("win_rate"), pnl=rec.get("pnl"),
                         cat_n=rec.get("cat_n"), cat_win_rate=rec.get("cat_win_rate"), credibility=x["credibility"],
                         flags="; ".join(x["flags"])))
    if not rows:
        st.caption("No wallets.")
        return
    df = pd.DataFrame(rows)
    for col in ("resolved", "win_rate", "pnl", "cat_n", "cat_win_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    def hl(row):
        if "informed" in row["flags"] or "winning-looking" in row["flags"]:
            return ["background-color:#fff8e1"] * len(row)
        if "MM-like" in row["flags"]:
            return ["background-color:#eceff1"] * len(row)
        return [""] * len(row)
    st.dataframe(df.style.format({"net_shares": "{:,.1f}", "exposure": "${:,.0f}", "buy_pre_start": "${:,.0f}",
                                  "buy_after_start": "${:,.0f}", "win_rate": "{:.0%}", "pnl": "${:,.0f}",
                                  "cat_win_rate": "{:.0%}", "credibility": "{:.2f}", "resolved": "{:.0f}",
                                  "cat_n": "{:.0f}"}, na_rep="-").apply(hl, axis=1),
                 hide_index=True, width="stretch")
    st.caption("Yellow = informed (≥20 resolved, ≥60% wins, positive PnL) or added the winning-looking side after the end. "
               "Grey = MM-like (bought both sides / bought and sold the same side). Win = realized PnL > 0 or resolved at $1.")


def detail(r, tz, full, by_conv=False):
    m, o = r["market"], r["outcomes"]
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%a %d %b %Y %H:%M") if ts else "n/a"  # noqa
    st.subheader(r["question"])
    src = m.get("resolutionSource")
    st.markdown(" · ".join([
        f"**{STATUS_BADGE.get(r['status'], r['status'])}**",
        "outcomes: " + " / ".join(f"<span style='color:{COLORS[i % 2]};font-weight:700'>{x}</span>" for i, x in enumerate(o)),
        f"start **{fmt(r['start_ts'])}**", f"end {fmt(r['end_ts'])}" + (f" · closed {fmt(r['closed_ts'])}" if r["closed_ts"] else ""),
        f"[resolution source]({src})" if src else "no resolution source",
        f"[Polymarket](https://polymarket.com/market/{m.get('slug')})", f"id `{r['id']}`"]), unsafe_allow_html=True)
    if m.get("_siblings"):
        with st.expander(f"This link is an event with {len(m['_siblings'])} markets; showing {r['id']}. "
                         "Paste one of these ids to check another:"):
            st.dataframe(pd.DataFrame(m["_siblings"], columns=["id", "question"]), hide_index=True,
                         width="stretch")
    verdict_card(r, tz)
    metrics_row(r)
    chart(r, tz, full, by_conv)
    t1, t5, t6, t2, t3, t4 = st.tabs(["Order book", "Conviction", "Wallets", "Trades", "Top holders", "Raw verdict components"])
    with t1:
        book_tables(r)
    with t5:
        conviction_table(r, tz)
    with t6:
        wallets_table(r)
    with t2:
        trades_table(r, tz)
    with t3:
        holders_table(r)
    with t4:
        st.dataframe(pd.DataFrame([dict(signal=lab, **{f"P({o[0]})": p0}, usd_weight=w) for p0, w, lab in r["verdict"]["comps"]]),
                     hide_index=True, width="stretch")


# ---------------------------------------------------------------- page
st.title("📊 PM data checker")
st.caption("What do traders actually believe? Built for low-OI / low-volume Polymarket markets, where the page price is often just an empty-book midpoint.")
with st.sidebar:
    st.header("Settings")
    tzs = sorted(available_timezones())
    tz_name = st.selectbox("Time zone", tzs, index=tzs.index("Europe/Madrid"))
    full = st.toggle("Full price history (otherwise zoom to the action)", value=False)
    by_conv = st.toggle("Size chart dots by conviction (instead of USD)", value=False)
    skip_uma = st.toggle("Skip UMA on-chain lookup (faster)", value=False)
    skip_wallets = st.toggle("Skip wallet track records (faster)", value=False)
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()
    st.caption("Data cached 60 s. All APIs are public, no keys.")
tz = ZoneInfo(tz_name)

default = st.query_params.get("ids", "5313440")
refs_text = st.text_input("Market id(s), polymarket.com URL(s) or slug(s), comma-separated", value=default)
refs = [x.strip() for x in refs_text.split(",") if x.strip()]

results, errors = [], []
for ref in refs:
    with st.spinner(f"Loading {ref}…"):
        try:
            results.append(load(ref, not skip_uma, not skip_wallets))
        except Exception as e:  # noqa
            errors.append(f"{ref}: {e}")
for e in errors:
    st.error(e)

if len(results) > 1:
    st.markdown("### Summary")
    rows = []
    for r in results:
        v = r["verdict"]
        rows.append(dict(id=r["id"], question=r["question"], status=r["status"],
                         expects=(r["outcomes"][v["idx"]] if v["idx"] in (0, 1) else
                                  "50-50 (void)" if v["idx"] == pm.FIFTY else pm.verdict_kind(v)[0]),
                         strength=pm.verdict_kind(v)[1] or "-",
                         usd_weight=v.get("weight"), oi=r["oi"], volume=r["volume"], liquidity=r["liquidity"],
                         uma_proposal=r["proposals"][-1]["label"] if r["proposals"] else ("skipped" if r["uma_skipped"] else "-"),
                         conviction_p=r["conviction"]["p0"],
                         track_record_p=(r["wallets"] or {}).get("p0"),
                         alignment=r["alignment"] or "-", resolved=r["resolved"] or "-", thin_book="⚠️" if r["warn"] else ""))
    st.dataframe(pd.DataFrame(rows).style.format({"usd_weight": "${:,.0f}", "oi": "${:,.0f}", "volume": "{:,.0f}",
                                                  "liquidity": "${:,.0f}", "conviction_p": "{:.0%}", "track_record_p": "{:.0%}"}, na_rep="n/a"),
                 hide_index=True, width="stretch")
    tabs = st.tabs([f"{r['id']}: {r['question'][:40]}" for r in results])
    for tab, r in zip(tabs, results):
        with tab:
            detail(r, tz, full, by_conv)
elif results:
    detail(results[0], tz, full, by_conv)
