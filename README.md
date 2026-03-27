# LP-Sonar

Concentrated-liquidity LP range recommendation engine for Uniswap V3 / Raydium CLMM / Meteora DLMM.

Given a pool address, the system returns:
- Whether to LP the pool right now
- Conservative / balanced / aggressive price range recommendations
- Expected fee APR, IL cost, breach probability, net PnL per range
- Recommendation confidence with regime-aware calibration

**Demo-ready baseline:** `a074e73` (branch `main`)

---

## Quick Start

### Prerequisites
- Python 3.11+, Node.js 18+
- Redis 7 (via Docker or local install)
- OKX API key (for price/volume data)

### 1. Start Redis

```bash
# Option A: Docker (recommended)
docker compose up -d redis

# Option B: local
redis-server --daemonize yes
redis-cli ping   # should return PONG
```

### 2. Backend

```bash
cd backend
cp .env.example .env
# Edit .env: set OKX_ACCESS_KEY=<your_key>

pip install -e .
uvicorn app.main:app --reload --port 8000
```

Verify: `curl http://localhost:8000/health`
API docs: http://localhost:8000/docs

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

---

## Demo

### Mature pool (stable, always reproducible)

| Field | Value |
|-------|-------|
| Chain | `501` (Solana) |
| Pool address | `8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj` |
| Pool | SOL/USDC · Meteora DLMM |
| Expected signals | `history_tier=mature`, `actionability=standard`, `regime=range_bound` |

```bash
curl "http://localhost:8000/api/v1/lp-range/501/8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj" | python3 -m json.tool
```

With explicit position size (enables `execution_cost_fraction`):
```bash
curl "http://localhost:8000/api/v1/lp-range/501/8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj?position_usd=1000" | python3 -m json.tool
```

### Young pool (find at demo time)

Pools age over time, so young-pool demos require a recently-created pool:
1. Open https://dexscreener.com/new-pairs
2. Filter: chain = Solana or BSC, created < 6 hours ago, TVL > $30k
3. Copy pool address and chain ID
4. Quick check: `curl "http://localhost:8000/api/v1/lp-range/<chain>/<pool>" | python3 -m json.tool | grep history_tier`

Expected signals: `history_tier=fresh/growing`, `actionability=caution`, `shrunk_fee_apr` non-null.

### UI areas to watch during demo

| Area | What to show |
|------|-------------|
| Summary header | `regime` badge, `recommendation_confidence` bar, pool quality summary |
| Evidence card | `effective_evidence_score`, `replay_weight`, `scenario_weight` (young pools only) |
| Balanced profile (expanded) | Fee APR (adjusted label for young pools), scenario PnL table |
| Aggressive profile (expanded) | `execution_cost` row (~0.2% when position_usd is set) |

---

## API Reference

### POST `/api/v1/lp-range/recommend`

```json
{
  "pool_address": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",
  "chain": "501",
  "position_usd": 1000
}
```

### GET `/api/v1/lp-range/{chain}/{pool_address}`

```
GET /api/v1/lp-range/501/8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj?position_usd=1000
```

Supported chain IDs: `501` (Solana), `8453` (Base), `56` (BSC), `1` (Ethereum)

---

## Calibration

The scoring engine loads calibration parameters from `backend/data/calibration.json` on startup.
A sample file (from 171 real pool samples) is committed and used by default.

To re-run calibration:
```bash
cd backend
python3 -m scripts.calibrate --dry-run          # preview without writing
python3 -m scripts.calibrate --out data/calibration.json
```

---

## Known Issues (non-blocking)

| Issue | Impact | Status |
|-------|--------|--------|
| OKX provides token-level volume (not pool-specific) | Fee APR may be inflated on high-volume tokens; `volume_fraction` correction applied | Known, P2.1.1 partially mitigates |
| Uniswap V3 fee tier: DexScreener doesn't expose `feeTier` | Non-standard tiers (0.05%, 1%) default to 0.3% | `fee_fetcher.py` ready; needs subgraph URL in `.env` |
| Low recommendation confidence (~10–20%) | OKX token-level data reduces evidence score | Expected behavior, not a bug |
| `calibration: loaded from ...` on startup | INFO log on every cold start | Intentional, confirms calibration is active |

---

## Project Structure

```
LP-Sonar/
├── backend/
│   ├── app/
│   │   ├── api/v1/endpoints/lp_range.py   # HTTP layer
│   │   ├── core/config.py                 # Settings + calibration loader
│   │   ├── models/schemas.py              # Pydantic response models
│   │   └── services/
│   │       ├── range_recommender.py       # Main pipeline orchestrator
│   │       ├── range_generator.py         # Candidate range generation
│   │       ├── range_backtester.py        # Historical replay
│   │       ├── range_scorer.py            # Utility scoring
│   │       ├── range_scenario.py          # Scenario simulation
│   │       ├── history_sufficiency.py     # Evidence assessment (Layer 0)
│   │       ├── regime_detector.py         # Market regime (Layer B)
│   │       ├── execution_cost.py          # Gas + slippage model
│   │       └── fee_fetcher.py             # Protocol-native fee resolver
│   ├── scripts/calibrate.py               # Offline calibration tool
│   ├── data/calibration.json              # Active calibration values
│   └── validate_backend.py                # Test suite (313 tests)
├── frontend/
│   ├── app/page.tsx                       # Main page
│   ├── components/LPAnalysis.tsx          # Core UI component
│   └── lib/api.ts                         # API client + TypeScript types
├── docs/
│   ├── handoff_context_2026_03_27.md      # Full algorithmic handoff doc
│   ├── lp_recommendation_engine_spec.md   # Original spec
│   └── phase2_planning.md                 # Roadmap
└── docker-compose.yml                     # Redis service
```
