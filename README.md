# PM data checker

Find out what traders actually believe about a Polymarket market, especially **low-OI / low-volume** markets,
where the price on the page is often just the midpoint of an empty order book.

There are two ways to use it:
- **Web UI** (`app.py`, Streamlit + Plotly): paste ids or links and get an interactive dashboard.
- **CLI** (`pm_data_checker.py`): prints a text report and saves a PNG chart. It is also the importable module the UI uses.

Every API it calls is public and needs no key: Gamma (markets), CLOB (book and price history), the Data API
(trades, OI, holders), and public Polygon RPCs for the UMA oracle events.

## Install
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                       # Python 3.9+
```

## Run the web UI
```bash
streamlit run app.py
```
Streamlit opens http://localhost:8501. You can enter one or more Gamma market ids, `polymarket.com` URLs, or slugs,
separated by commas (for example `5649060, 5862530, 5313440`). You can also link to the page directly with
`http://localhost:8501/?ids=5649060,5313440`.

- **Header**: question, outcomes in their chart colours (outcome 1 is blue, outcome 2 is red), status
  (open / proposed / resolved), resolution source, and start/end times in the chosen time zone (default Europe/Madrid).
- **Verdict card**: "Traders expect X", its strength (strong / weak / not enough data), a plain-English list of
  reasons, the USD weight behind the verdict, each UMA proposal/dispute/settlement with a tx link, an
  ALIGNED / NOT ALIGNED badge, and the final resolution once the market settles.
- **Metrics**: OI, volume, liquidity, spread, number of trades (and how many came after the start), and top-wallet
  concentration. A red **thin book** banner appears when liquidity is under $50, the spread is over 0.30, or the
  book is empty.
- **Interactive chart**: price lines for each outcome, labelled in the legend. Trade fills are dots sized by USD
  (circles are buys, triangles are sells); hover shows time, outcome, side, size, price, USD and a shortened wallet.
  Vertical lines mark the start, end, proposal, dispute and settlement, and a band shows the current bid/ask.
- **Tabs**: the order book for each outcome (bids in green, asks in red, USD per level, with "bids with no asks"
  highlighted), **Conviction** (the top conviction contributors), **Wallets** (behaviour and track record), all
  trades split into pre- and post-start, top holders with their share of each outcome, and the raw verdict
  components.
- The verdict card also shows a **smart money (conviction)** line, a **track-record read** line, and any
  market-maker patterns found on the book.
- **Compare**: when you enter several ids, a summary table (expected outcome, strength, weight, OI, volume,
  liquidity, UMA proposal, alignment, resolution) appears above one detail tab per market.
- **Sidebar**: time zone, full history vs a zoomed view, sizing chart dots by conviction instead of USD, switches
  to skip the UMA lookup and the wallet track records (both make it faster), and a button to clear the cache.
  Market data is cached for 60 s and wallet records for 10 min.

## CLI
```bash
python pm_data_checker.py <id|url|slug> [--out chart.png] [--tz Europe/Madrid] [--full] [--no-uma] [--no-wallets]
```
As a module: `import pm_data_checker as pm; r = pm.analyze("5649060", use_uma=True)` returns a dict with market,
books, hist, trades, holders, oi, verdict, uma, proposals, alignment, warn, status, resolved, and more.

## How the verdict works
Each signal becomes an implied probability for outcome 1, with a USD weight:
1. **Post-start trades**: the USD-weighted average fill price, with sells converted to the other outcome. Later
   trades count up to 2x.
2. **Resting book**: the two books are combined, since an ask on outcome 2 at p is the same as a bid on outcome 1
   at 1-p. A bid with no offers gives P = (bid+1)/2, an offer with no bids gives P = ask/2, and both together give
   the midpoint. The weight is the USD sitting at the top levels. Once the market is closed and the book is gone,
   the time-averaged post-start displayed price is used instead, at a fixed $10 weight.
3. **Extreme fills**: post-start buys at 0.85 or higher count 2x toward that outcome.
4. **Pre-start trades** are used only when there is no in-play signal, because before the start they are opinion,
   not information.

The verdict is **strong** when |P - 0.5| ≥ 0.3, at least $50 of weight is behind it, every signal agrees, and there
is either an extreme fill or a one-sided book. It is **not enough data** when less than $5 of weight is behind it,
and a **toss-up** when P falls between 0.4 and 0.6. Otherwise it is **weak**. The 50-50 check below can override
any of these.

### Possible verdicts
A verdict is one of four things: **an outcome** (`leans X (strong|weak)`), **50-50** (`expects 50-50, likely
cancelled/void`), **toss-up** (enough money, but P is within 0.4-0.6), or **not enough data** (less than $5 of
weight).

### 50-50 (cancelled / void) detection
When an event is cancelled, Polymarket markets usually resolve 50-50 and both outcomes pay $0.50. Traders who know
this price both sides at ~0.50, and on a thin book that pattern used to be misread as a lean.

- **UMA decoding**: a proposed price of `0.5e18` (5e17) for a YES_OR_NO_QUERY is "p3 = unknown / 50-50". It is
  decoded as the outcome **`50-50 (cancel/void)`**. When the verdict is also 50-50, alignment compares the two
  and reports ALIGNED. A market that closed with prices 0.5/0.5 shows as *resolved 50-50 (cancel/void)*.
- **One-sided books at 0.50**: an outcome offered at exactly ~0.50 with no bids (which mirrors to the other
  outcome bid at 0.50 with no asks) is **consistent with 50-50**. It counts as P = 0.50, not as "the other side is
  favoured". Its one-sided midpoint (bid none / ask 0.50 → 0.25 on the page) is an artifact; it is noted and never
  used. Any other one-sided quote counts only when it says something: a bid above 0.53 with no offers, or an
  offer below 0.47 with no bids. Everything else is noted as uninformative.
- **Detector from price action.** It only runs after the event should be over (the post-end boundary). It needs
  at least one of:
  - **A**: at least 70% of post-start fill USD **and** at least 70% of resting-order USD priced within 0.47-0.53;
  - **B**: the post-start VWAP is within 0.47-0.53 **and** every best quote is exactly 0.50;
  - **C**: the book is already empty, at least 70% of post-start fill USD is within the band, the VWAP is within
    the band, and there are at least 2 post-start fills worth at least $20.

  It also requires **no conviction toward either side**. Post-start conviction (post-start fills and resting
  orders) must imply a probability within 0.40-0.60, and no post-start fill can be at 0.85 or higher. **Strong**
  means A and B both hold with at least $50 of post-start fills and at least $50 resting; anything else is
  **weak**. While the event may still be live, a 0.50 cluster is only noted (it could be a real coin-flip).
- With a 50-50 verdict, the conviction and track-record reads stay visible but are labelled as context. They
  describe positioning *before* the cancellation.
- Example: market 5591225 (a MODUS darts match that was cancelled). It used to read "leans Sander" because of a
  lone 0.50 ask. It now says *expects 50-50, likely cancelled/void* and is ALIGNED with the 50-50 UMA proposal.
  It was strong while the 0.50 order was on the book, weak once the book emptied, and it later settled 50-50.

### Conviction-weighted orders
Every taker fill and every resting order on the book gets a score:

`score = USD × extremity(q) × timing × discount`

- **q** is the price expressed for the side the order *supports*. Buying X at p supports X at p, selling X at p
  supports the other side at 1-p, a bid for X at b supports X at b, and an ask on X at a supports the other side
  at 1-a. USD is q × shares.
- **extremity(q)** is 1 at 50¢. Above 50¢ it rises convexly as `1 + 9·((q-0.5)/0.5)^1.5`: 0.6 → 1.8, 0.7 → 3.3,
  0.8 → 5.2, 0.85 → 6.3, 0.9 → 7.4, 0.99 → 9.7. Below 50¢ it falls linearly to 0.2 (a 10¢ ticket scores 0.36).
  Paying 85¢ for No, or bidding 80¢ for Yes, is a high-conviction statement; a 10¢ lottery ticket is not.
- **timing** is 1× pre-start, 2× in-play, and 4× post-end. "Post-end" begins at the earliest of the first UMA
  proposal, the start + 3 h, or a real Gamma endDate.
- **discount** is 1 for fills and 0.3 for resting orders, because an unfilled order is intent, not money spent.
  Book levels that match the recurring market-maker ladder (3000@0.01, 100@0.03, 100@0.12, 5@0.50, 30@0.80) get a
  further 0.25×. The two token books mirror each other, so only one is read.
- **Aggregation**: the "smart-money share" is each outcome's share of the total score. The conviction-implied
  P(outcome 1) is the score-weighted average of max(q, 0.5) for that outcome's side. A buy below 50¢ only says
  "not as unlikely as the price", so it counts as 50/50 evidence.
- **Effect on the verdict**: the verdict uses the conviction read from **post-start fills and resting orders**
  (falling back to all fills when there isn't any). Pre-start fills are opinion, so a pre-game 99¢ buy can't
  outvote in-play evidence. If that score is at least 5 and its P is outside 0.4-0.6, the final P = 0.6 ×
  money-weighted P + 0.4 × conviction P. A **strong** verdict also requires conviction to agree.
  If conviction leans the other way, a note says so and strong drops to weak.

### Wallet behaviour and track record (Wallets tab)
- The tab covers up to **25 wallets**: top holders plus every wallet that traded (maker and taker fills), ranked
  by involvement. Records are fetched in parallel (12 threads) and cached for 10 min.
- **In this market**, each wallet shows its USD bought before and after the start, net shares it added (+) or
  reduced (-) after the end, its net position (current holdings, or net from trades once redeemed; Yes/No pairs
  cancel out), and its exposure in USD at its average entry price.
- **Track record** covers recent resolved markets: `/closed-positions?user=` (sortBy=TIMESTAMP desc, up to
  200; a win means realizedPnl > 0) **plus** `/positions?user=&redeemable=true` (resolved but unredeemed; curPrice
  1 is a win and 0 is a loss, scored by cashPnl), restricted to the same time window. The second source matters
  because losing positions are almost never redeemed, so `/closed-positions` alone shows inflated win rates of
  90%+. It also reports realized PnL and a same-category record, where the category is the slug prefix (e.g.
  `nfl`, `dota2`). This market itself is excluded from the record.
- **Credibility**: a shrunk win rate `wr = (wins+5)/(n+10)`, then `cred = clamp(1 + 4·(wr-0.5)·min(1, n/20),
  0.25, 3)`, times 0.75 if PnL < 0 with n ≥ 20, and times 0.5 if the wallet is MM-like.
- **Track-record weighted P(outcome 1)** = Σ cred·exposure on outcome 1 / Σ cred·exposure. It is shown next to
  the verdict, not blended into it.
- **Flags**:
  - **informed**: at least 20 resolved markets, a win rate of 60% or more, and positive PnL.
  - **added X after end**: marked ✓ when X is the winning-looking side (the resolved outcome, otherwise the
    verdict's lean).
  - **reduced X after end**.
  - **MM-like (two-sided)**: bought both outcomes (the smaller side at least 25% of the larger), or bought and sold
    the same outcome over 4+ fills.
- **Book-level MM detection** flags the recurring ladder and symmetric two-sided quoting (≥3 levels on each side,
  spread ≤ 0.10, balanced USD) and shows which side the ladder bids. Market makers are **kept and discounted**,
  never filtered out.

UMA lookup: the checker scans Polygon for the UMA Optimistic Oracle's `ProposePrice` / `DisputePrice` / `Settle`
events, keeps those from the market's adapter (Gamma `resolvedBy`), and matches them by
`keccak256(ancillaryData) == questionID`. The outcome mapping comes from the ancillary text
("p1 corresponds to No, p2 to Yes"). The scan only runs when Gamma reports a UMA status or the market is closed.

## Caveats
- On thin markets a single $10 order moves the displayed price, so check the dollar weight before trusting a verdict.
- Price history comes as midpoint-style samples taken about every 10 minutes, not as trades. The spread band shows
  only the current book, because the public API has no historical spread.
- Holder concentration divides by OI and only counts the top holders the API returns. Once positions are redeemed,
  OI and holders drop to 0.
- The UMA scan uses free public RPCs with block-range limits and takes about 15-25 s, from the event start to the
  close (at most about 60k blocks). Turn it off with the toggle or `--no-uma`. negRisk or non-standard adapters may
  not match.
- A full event URL resolves to its main market, and the other markets are listed so you can pick one. Gamma
  `volume` is counted in shares for some markets.
- Conviction and credibility curves are hand-tuned heuristics, not fitted to data. A 60% win rate is easy to
  reach by buying heavy favourites, which is why PnL must be positive and sample sizes are shrunk.
- Win rates cover only the most recent ~200 closed positions plus up to 500 unredeemed resolved ones. Very active
  wallets are sampled (marked `+`), and the endpoints don't let you sort unredeemed positions by date.
- Wallet-level market-maker detection only sees fills. Resting orders are anonymous on the public book, so the
  book-level MM flags can't name a wallet.
- "Post-end" uses a 3 h default event length when Gamma has no real end time, so long events can be mislabeled.
- The verdict is a heuristic, not advice. The resolution source is the authority.
