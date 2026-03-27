# LP-Sonar 推荐引擎 — 上下文交接文档（完整算法版）
**生成时间：2026-03-27**
**用途：向新 AI 助手传递完整项目背景，用于继续开发。**

---

## 一、项目是什么

**LP-Sonar** 是一个针对 Uniswap V3 / Raydium CLMM / Meteora DLMM 等集中流动性 AMM 的 **LP 仓位推荐引擎**。

用户输入一个池子地址，系统返回：
- 是否推荐现在做 LP
- 推荐的价格区间（conservative / balanced / aggressive 三档）
- 每档的预期 fee APR、IL 成本、区间穿越概率、净 PnL 预测
- 风险标记和推荐可信度

**核心优化目标：**
```
Best Range = argmax_r [ E[Fee(r)] - E[IL(r)] - OutOfRangePenalty(r) - RebalanceCost(r) - MarketQualityPenalty(r) ]
```

这不是一个预测模型，是一个**在约束下的策略优化引擎**。

---

## 二、系统架构（3 层）

```
Layer A: Risk Gating        → pool 是否适合 LP？
Layer 0: History Sufficiency → 历史证据质量评估，决定混合权重
Layer B: Market Regime      → 当前市场状态（range_bound/trend_up/trend_down/chaotic）
Layer C: Range Optimization → 生成候选区间 → replay 打分 → 选出最优 3 档
```

**核心调用链（`recommend_range` 函数）：**
```python
# backend/app/services/range_recommender.py

async def recommend_range(pool_address, chain_index, user_position_usd=None):
    # 1. 拉取池子状态（DexScreener + 协议 API）
    pool = await _fetch_pool_state(pool_address, chain_index)

    # 2. History Sufficiency — Layer 0 证据评估
    sufficiency = assess(pool_age_hours, bars_1h, bars_5m, ...)
    # → history_tier, replay_weight, scenario_weight, uncertainty_penalty

    # 3. Regime 检测
    regime_result = detect_regime(ohlcv_bars)

    # 4. 候选区间生成（8-12 个）
    candidates = generate_candidates(current_price, regime_result,
                                     min_width_floor_pct, fresh_mode)

    # 5. Replay 回测打分
    backtests = backtest_all_candidates(ohlcv_bars, candidates, ...)
    scored = score_all_candidates(candidates, backtests, ...)

    # 6. Scenario 仿真
    scenario_pnl_list = compute_all_scenario_pnl(candidates, ...)

    # 7. 最终效用 = 混合公式（P2.X: replay + scenario + penalty）
    final_utility = replay_weight * replay_utility + scenario_weight * scenario_utility - penalty

    # 8. Regime-aware 置信度校准（P2.X）
    confidence = _apply_confidence_calibration(raw_confidence, regime_result.regime)
    final_actionability, conf_floor_flags = _check_confidence_floor(confidence, actionability)

    # 9. 返回 RangeRecommendation（含 profiles / alt_ranges）
```

---

## 三、目录结构

```
LP-Sonar/
├── backend/
│   ├── app/
│   │   ├── api/v1/endpoints/lp_range.py     # FastAPI 路由（GET + POST）
│   │   ├── core/config.py                   # Settings（含 calibration 字段）
│   │   ├── models/schemas.py                # Pydantic 数据模型
│   │   └── services/
│   │       ├── range_recommender.py         # 主调度器（orchestrator）
│   │       ├── history_sufficiency.py       # Layer 0：证据评估
│   │       ├── regime_detector.py           # Layer B：市场状态检测
│   │       ├── range_generator.py           # Layer C：候选区间生成
│   │       ├── range_backtester.py          # Replay 回测内核
│   │       ├── range_scorer.py              # 效用打分
│   │       ├── range_scenario.py            # Scenario PnL 仿真
│   │       ├── fee_fetcher.py               # Protocol-native fee 获取
│   │       ├── execution_cost.py            # 执行成本模型
│   │       └── market_quality.py            # 市场质量评估
│   ├── scripts/calibrate.py                 # 离线校准脚本
│   ├── data/calibration.json                # 校准输出文件
│   └── validate_backend.py                  # 313 项验证测试
└── frontend/
    ├── components/LPAnalysis.tsx             # 推荐结果展示组件
    └── lib/api.ts                            # 前端类型定义
```

---

## 四、各层算法详解

### 4.1 Layer 0 — History Sufficiency（`history_sufficiency.py`）

**目的：** 评估历史数据质量，决定推荐可信度与混合权重。

#### 4.1.1 Tier 分类

```python
mature:  age >= 24h AND (bars_1h >= 24  OR bars_5m >= 288)
growing: age >=  4h AND (bars_5m >= 48  OR bars_1h >=   4)
fresh:   age >=  1h AND (bars_5m >= 12  OR bars_1m >=  60)
infant:  其余（不满足上述任何条件）
```

| Tier | mode | actionability |
|---|---|---|
| mature | full_replay | standard（若 evidence >= 0.60）|
| growing | blended_replay | standard（若 evidence >= 0.65）|
| fresh | launch_mode | caution |
| infant | observe_only | watch_only |

#### 4.1.2 Evidence Score 公式

```python
# 可用数据分钟数
available_minutes = bars_1h * 60 + bars_5m * 5 + bars_1m * 1
history_coverage  = min(1.0, available_minutes / 1440)   # 目标 24h

# bar_coverage 使用最精细的可用粒度
bar_coverage = min(1.0, bars_1h / 24)   # 首选 1H bars

# source_quality_factor
source_quality = {"pool_specific": 1.0, "pool_candle": 0.7, "token_level": 0.4}
sq_factor = source_quality[source]   # 当前默认 token_level → 0.4

data_quality = max(0.0, 1.0 - missing_bar_ratio)

evidence = (0.35 * history_coverage
          + 0.30 * bar_coverage
          + 0.20 * sq_factor
          + 0.15 * data_quality)
```

#### 4.1.3 Uncertainty Penalty

```python
source_penalty = 1.0 - sq_factor   # token_level → 0.6
penalty = (0.20 * (1.0 - evidence)
         + 0.10 * liquidity_instability
         + 0.10 * source_penalty)
penalty = clamp(penalty, 0.0, 0.40)
```

#### 4.1.4 Replay Weight（来自 calibration.json 的参数）

```python
# rw_lo=0.35, rw_hi=0.65（校准后）
raw_replay    = (evidence - rw_lo) / (rw_hi - rw_lo)
replay_weight = clamp(raw_replay, 0.0, 1.0)
scenario_weight = 1.0 - replay_weight
```

#### 4.1.5 Age-based Width Floor

```python
age < 2h  → min_width_floor = 18%
age < 6h  → min_width_floor = 14%
age < 24h → min_width_floor = 10%
age >= 24h → no floor (0.0)
```

#### 4.1.6 Fee Persistence Factor（防止 launch spike 被错误年化）

```python
fee_persistence_factor(age, jump_ratio, liq_instability):
    age_factor  = clamp(age / 24.0, 0.15, 1.0)
    jump_factor = max(1.0 - jump_ratio, 0.4)
    liq_factor  = max(1.0 - liq_instability, 0.5)
    return clamp(age_factor * jump_factor * liq_factor, 0.15, 1.0)

# age=0h → factor=0.15, age=24h → factor=1.0（无修正）
shrunk_fee_apr = raw_fee_apr * fee_persistence_factor(...)
```

---

### 4.2 Layer B — Regime Detection（`regime_detector.py`）

**目的：** 从 OHLCV 历史数据中识别当前市场状态（4 种）。最小需要 24 根 K 线。

#### 4.2.1 特征提取（5 个特征）

```python
closes = [bar["close"] for bar in ohlcv_bars]
log_returns = diff(log(closes))

# F1: 年化实现波动率
rv_annual = std(log_returns, ddof=1) * sqrt(8760)

# F2: 对数价格漂移斜率（线性回归）
# x = [0, 1, 2, ..., n-1], y = log(closes)
drift_slope = cov(x, log_p) / var(x)
# 单位：每 bar 的 log-price 变化量（1H bars）

# F3: 滞后 1 阶自相关
lag1_autocorr = cov(r[:-1], r[1:]) / var(r)   # Pearson

# F4: Jump Ratio（尖峰比）
sigma_bar = std(log_returns, ddof=1)
jump_ratio = mean(|log_returns| > 3 * sigma_bar)

# F5: 价格驻留 HHI（Herfindahl 指数，20个桶）
bucket_ids = floor((prices - min) / bucket_width)
shares = count[bucket] / n
hhi = sum(shares^2)   # 高 HHI → 价格集中（区间震荡信号）
```

#### 4.2.2 分类决策树

```
1. if jump_ratio >= 0.12  OR  rv_annual >= 4.0 (400%):
       → regime = "chaotic"
       → confidence = min(0.50 + jump_ratio * 2.0, 0.85)

2. elif |drift_slope| >= 0.005 (≈0.5%/h = 12%/day 方向漂移):
       → regime = "trend_up" (drift_slope > 0) or "trend_down"
       → slope_conf = min(|drift_slope| / 0.015, 1.0)
       → autocorr_bonus = max(autocorr, 0.0) * 0.20
       → confidence = 0.45 + 0.30 * slope_conf + autocorr_bonus  [max 0.85]

3. else (|drift_slope| < 0.005):
       → regime = "range_bound"
       → hhi_conf  = min(hhi / 0.05, 1.0) if hhi < 0.05 else 1.0
       → autocorr_conf = min(max(autocorr - 0.10, 0.0) / 0.2, 1.0)
       → confidence = 0.45 + 0.25 * hhi_conf + 0.15 * autocorr_conf  [max 0.85]
```

> **注意**：bars < 24 时，直接返回 chaotic（confidence=0.20）。

---

### 4.3 Layer C 步骤1 — 候选区间生成（`range_generator.py`）

**目的：** 生成 8-12 个候选 LP 区间，覆盖不同风险档位。

#### 4.3.1 Tick 数学

**Uniswap V3（tick 体系）：**
```python
tick    = floor(log(price) / log(1.0001))
price   = 1.0001 ^ tick
# snap: floor(raw_tick / tick_spacing) * tick_spacing
# 常见 tick_spacing: {0.01%:1, 0.05%:10, 0.30%:60, 1.00%:200}
```

**Meteora DLMM（bin 体系）：**
```python
bin_id = floor(log(price) / log(1 + bin_step / 10000))
price  = (1 + bin_step/10000) ^ bin_id
```

#### 4.3.2 Horizon Sigma 计算

```python
sigma = rv_annual * sqrt(horizon_hours / 8760)
# 示例：rv=80%, horizon=48h → sigma = 0.80 * sqrt(48/8760) ≈ 5.9%
# fresh_mode: sigma = max(sigma, 0.08)  (下限 8%)
```

#### 4.3.3 四类候选家族

**A. Volatility-Band（波动率区间）**
```python
multipliers = [0.5, 1.0, 1.5, 2.0]  # fresh_mode: [1.0, 1.5, 2.0]（跳过 0.5×）
for m in multipliers:
    lower = current_price * (1.0 - m * sigma)
    upper = current_price * (1.0 + m * sigma)
```

**B. Volume-Profile（成交量密集区）**
```python
# POC (Point of Control) = 成交量最高的价格桶（50个桶）
poc_bucket = argmax(bucket_vol)
poc_price  = lo + (poc_bucket + 0.5) * bucket_width

# Value Area = 从 POC 向外扩展直到覆盖 70% 总成交量
# 候选1: [VAL, VAH]（价值区间边界）
# 候选2: poc ± 1.0*sigma（POC 为中心的波动率区间）
```

**C. Trend-Biased（趋势偏移，仅 trend_up/trend_down，fresh_mode 跳过）**
```python
# trend_up: 偏向上方 → 下方窄、上方宽
[center*(1-0.5σ), center*(1+1.5σ)]   # 主要
[center*(1-0.3σ), center*(1+2.0σ)]   # 更激进
# trend_down: 对称翻转
[center*(1-1.5σ), center*(1+0.5σ)]
[center*(1-2.0σ), center*(1+0.3σ)]
```

**D. Defensive（防御性宽区间）**
```python
multipliers = [3.0, 4.0]       # 正常模式
multipliers = [3.0, 4.0, 5.0]  # fresh_mode（3个防御候选）
```

最后：对 `(lower_tick, upper_tick)` 对去重。

---

### 4.4 Layer C 步骤2 — Replay 回测（`range_backtester.py`）

**目的：** 对每个候选区间，在最近 `horizon_bars` 根历史 K 线上模拟 LP 表现。

#### 4.4.1 资本效率

```python
# CLMM 集中流动性相比全范围位置的效率提升
capital_efficiency = min(1.0 / width_pct, 20.0)
# 宽度 5% → 效率 20×（cap）；宽度 20% → 效率 5×
```

#### 4.4.2 Fee Proxy 累积

```python
for bar in replay_bars:
    if lower_price <= bar["close"] <= upper_price:   # 在区间内
        cumulative_fee += bar["volume"] * fee_rate * capital_efficiency

cumulative_fee_proxy = cumulative_fee / TVL_USD   # 归一化到每 $1 资本
```

#### 4.4.3 CLMM IL 公式（V2 近似）

```python
# k = P_final / P_entry
# V2 IL 公式作为 in-range 部分的近似
base_il = 2 * sqrt(k) / (1 + k) - 1   # 返回负数，例如 50% 价格移动 → -5.7%
```

**Edge Correction（P2.2.1a）：** 当终价靠近区间边界时放大 IL
```python
# r = (final_price - lower) / (upper - lower)  ∈ (0, 1)
edge_dist = |r - 0.5| * 2.0   # 0 at center, 1 at boundary
edge_weight = 1.0 + edge_dist * 0.40   # max 1.40 at boundary
base_il_weighted = base_il * edge_weight
```

**OOR 放大（当 > 30% 时间在区间外时）：**
```python
single_leg_il = -(|price_ratio - 1.0|)  # 单边敞口 = 全方向性暴露
if oor_ratio > 0.30:
    il_cost = base_il_weighted * in_range_ratio + single_leg_il * oor_ratio
else:
    il_cost = base_il_weighted
```

#### 4.4.4 再平衡触发

```python
# 连续 3 根 K 线在区间外 → 触发 1 次再平衡事件
if oor_streak == 3:
    rebalance_count += 1
```

---

### 4.5 Layer C 步骤3 — 效用打分（`range_scorer.py`）

**目的：** 将回测指标合并为单一效用分数 [0, 1]。

#### 4.5.1 5 个分量公式

```python
# 1. FeeScore（越高越好）
apr_proxy  = cumulative_fee_proxy * (8760 / horizon_bars)
fee_score  = min(apr_proxy / 3.0, 1.0)
# 300% APR proxy → FeeScore = 1.0

# 2. ILScore（越高越差）
backtest_il  = min(|il_cost_proxy|, 1.0)     # 来自回测
heuristic_il = il_result.score / 100.0       # 来自 il_risk 模块
il_score     = 0.60 * backtest_il + 0.40 * heuristic_il

# 3. BreachRisk（越高越差）
blended_oor    = replay_weight * replay_oor + (1 - replay_weight) * analytical_oor
breach_penalty = min(breach_count / 10.0, 1.0)
jump_penalty   = min(jump_ratio * 5.0, 1.0)
breach_risk    = 0.60 * blended_oor + 0.25 * breach_penalty + 0.15 * jump_penalty

# 4. RebalanceCost（越高越差）
# 5% 总成本 = 完整惩罚
rebalance_cost = min(total_execution_cost_fraction / 0.05, 1.0)

# 5. QualityPenalty（越高越差）
quality_penalty = 0.70 * wash_score + 0.30 * jump_penalty
```

#### 4.5.2 效用分数合并（权重可外部配置）

```python
# 默认权重（config.py 可配置）
weights = {"fee":0.30, "il":0.25, "breach":0.25, "rebalance":0.10, "quality":0.10}

utility = (w_fee   * fee_score
         - w_il    * il_score
         - w_breach * breach_risk
         - w_rebalance * rebalance_cost
         - w_quality * quality_penalty)
utility = clamp(utility, 0.0, 1.0)
```

#### 4.5.3 GBM 解析终端 OOR（Breach Probability 下界）

```python
# 用于年轻池（replay 历史不足时），作为 breach_risk 的保守下界
sigma_T = (rv_annual / sqrt(8760)) * sqrt(horizon_bars)
mu_T    = drift_slope * horizon_bars

z_upper = (log(upper / entry) - mu_T) / sigma_T
z_lower = (log(lower / entry) - mu_T) / sigma_T
p_in    = Phi(z_upper) - Phi(z_lower)
analytical_oor = 1.0 - p_in   # 终端 OOR 概率（非首次穿越，保守下界）
```

> **重要**：这是终端价格概率（GBM terminal），不是首次穿越概率。真实 breach 概率 ≥ 此值。

#### 4.5.4 Profile 选择逻辑

```python
aggressive   = narrowest width_pct（优先排除 defensive 家族）
conservative = widest width_pct
balanced     = highest utility_score（不重复 aggressive/conservative 时选次优）
```

---

### 4.6 Scenario 仿真（`range_scenario.py`）

**目的：** 在 5 种合成未来场景下估算每个候选区间的净 PnL。

#### 4.6.1 标准 5 种场景（成熟池）

```python
# sigma_bar = rv_annual / sqrt(8760)  （per-bar sigma）
# noise     = sigma_bar * 0.30        （bar 内高斯噪声）

"sideways":        drift=0, noise=sigma_bar*0.30
"slow_up":         drift=+sigma_bar/horizon_bars（全程 +1σ）
"slow_down":       drift=-sigma_bar/horizon_bars（全程 -1σ）
"breakout_up":     前25% bars: drift=+2σ/(0.25*n), noise*1.5；后75%: drift=0
"breakdown_down":  前25% bars: drift=-2σ/(0.25*n), noise*1.5；后75%: drift=0

# 价格更新（乘法 GBM）
price *= exp(drift + shock)
```

#### 4.6.2 Launch 模式 5 种场景（年轻池）

```python
"discovery_sideways":     drift=0, noise=sigma_bar*0.15（最紧）
"grind_up":               drift=+0.5σ/n（温和积累）
"fade_down":              drift=-0.5σ/n（上市热情消退）
"spike_then_mean_revert": 前20%: +2σ spike, 后80%: -1σ 部分回归（终价约 +1σ）
"pump_and_dump":          前25%: +3σ pump, 后75%: -3.5σ dump（终价跌破起点）
```

#### 4.6.3 Scenario Utility 聚合

```python
sorted_pnl = sorted(pnl_values)
median_pnl = sorted_pnl[n//2]
min_pnl    = sorted_pnl[0]
mean_pnl   = mean(pnl_values)

raw = 0.60 * median_pnl + 0.25 * min_pnl + 0.15 * mean_pnl
# raw=0 → 0.5, raw=+10% → 1.0, raw=-10% → 0.0
scenario_utility = clamp(raw / 0.10 * 0.5 + 0.5, 0.0, 1.0)
```

---

### 4.7 执行成本模型（`execution_cost.py`）

#### 4.7.1 Gas 成本（静态估计）

```python
gas_cost_usd = {
    "1":     15.0,   # Ethereum
    "10":     0.5,   # Optimism
    "8453":   0.3,   # Base
    "42161":  0.5,   # Arbitrum
    "56":     0.2,   # BSC
    "137":    0.05,  # Polygon
    "501":    0.01,  # Solana
    "default": 1.0,
}
```

#### 4.7.2 Slippage 模型

```python
depth_ratio = position_usd / tvl_usd
slippage    = min(0.0005 + 0.005 * depth_ratio, 0.02)
# depth_ratio=0%:  0.05%（底线）
# depth_ratio=1%:  0.055%
# depth_ratio=10%: 0.55%
# depth_ratio≥400%: cap 2%
```

#### 4.7.3 代表性仓位（用户未提供时）

```python
position_usd = min(10_000, max(tvl_usd * 0.01, 100.0))
# $10k 或 TVL 的 1%，取较小值；最低 $100 防止除零
```

#### 4.7.4 总执行成本

```python
gas_fraction = gas_cost_usd[chain] / position_usd
cost_per_rebalance = min(gas_fraction + slippage, 1.0)
total_cost_fraction = min(rebalance_count * cost_per_rebalance, 1.0)
```

---

### 4.8 最终效用混合 + 置信度校准（`range_recommender.py`）

#### 4.8.1 最终效用公式

```python
final_utility = (replay_weight * replay_utility
               + scenario_weight * scenario_utility
               - uncertainty_penalty)
```

#### 4.8.2 Regime-aware 置信度校准（P2.X）

```python
def _apply_confidence_calibration(confidence: float, regime: str) -> float:
    # 从 calibration.json 读取 scale（calibration 后）
    scales = {
        "range_bound": 1.00,
        "trend_up":    0.50,   # 趋势/混沌场景下置信度打折
        "trend_down":  0.50,
        "chaotic":     0.50,
    }
    scale = clamp(scales[regime], 0.5, 1.0)   # 边界 [0.5, 1.0]
    return clamp(confidence * scale, 0.0, 1.0)
```

#### 4.8.3 Confidence Floor（运营参数）

```python
def _check_confidence_floor(confidence, current_actionability):
    floor = settings.confidence_floor   # 默认 0.0 = 关闭
    if floor <= 0.0 or confidence >= floor:
        return current_actionability, []
    # 触发：降级 + 添加 risk_flag
    downgraded = "caution" if current_actionability == "standard" else current_actionability
    flag = f"Confidence below calibrated floor ({confidence:.2f} < {floor:.2f})"
    return downgraded, [flag]
```

---

## 五、已完成的内容

### Phase 1（基础引擎）✓

| 模块 | 内容 |
|---|---|
| `regime_detector.py` | 4 种市场状态分类，5 个特征 |
| `range_generator.py` | 生成候选区间：4 个家族，tick 数学，volume profile |
| `range_backtester.py` | 历史 replay 回测，CLMM IL，edge correction |
| `range_scorer.py` | 效用打分：5 分量，GBM terminal OOR |
| `range_scenario.py` | 10 种 scenario PnL（5 标准 + 5 launch） |
| `range_recommender.py` | 完整调度器，输出 3 档 + alt_ranges |
| `lp_range.py` | REST API（GET + POST） |

### Phase 1.5（年轻池 / 短历史支持）✓

- 证据自适应降级替代硬 gate
- Width floor / fee persistence factor
- 混合效用公式（replay × rw + scenario × sw - penalty）

### Phase 2.X（可信度增强）✓

- P2.1 协议原生 fee tier 获取（Raydium/Meteora/Uniswap V3 subgraph）
- P2.X 置信度校准（regime scale + confidence floor）
- P2.X 校准管道（calibrate.py → calibration.json → config.py 三层优先级）

**当前 calibration.json（2026-03-27，171 个真实样本）：**
```json
{
  "calibration_growing_standard_threshold": 0.65,
  "calibration_mature_standard_threshold":  0.60,
  "replay_weight_lower_bound":  0.35,
  "replay_weight_upper_bound":  0.65,
  "confidence_regime_scales": {
    "range_bound": 1.0,
    "trend_up":    0.5,
    "trend_down":  0.5,
    "chaotic":     0.5
  }
}
```

**相比代码默认值的变化：**
- `mature_threshold`：0.55 → 0.60（证据要求更严格）
- `rw_lower_bound`：0.25 → 0.35（低证据池更快切到纯 scenario 模式）
- `rw_upper_bound`：0.75 → 0.65（最小宽度保证 0.30）
- `chaotic/trend scale`：0.70/0.85 → 0.50（走前向校准后更激进压缩）

---

## 六、API 契约（当前生产接口）

```
POST /api/v1/lp-range/recommend
Body: { "pool_address": "0x...", "chain": "8453", "position_usd": 5000 }

GET /api/v1/lp-range/{chain}/{pool_address}?position_usd=5000
```

**返回结构（RangeRecommendation）：**
```python
{
    "is_recommended": bool,
    "recommendation_confidence": float,   # 0–1，regime-aware 校准后
    "regime": str,                        # range_bound/trend_up/trend_down/chaotic
    "holding_horizon": str,               # e.g. "4–12h"
    "actionability": str,                 # standard/caution/watch_only
    "history_tier": str,                  # mature/growing/fresh/infant
    "recommendation_mode": str,           # full_replay/blended_replay/launch_mode/observe_only
    "effective_evidence_score": float,
    "replay_weight": float,
    "scenario_weight": float,
    "profiles": {
        "conservative": RangeProfile,
        "balanced": RangeProfile,
        "aggressive": RangeProfile,
    },
    "alternative_ranges": [RangeProfile, ...],   # 最多 5 个
    "pool_quality_summary": str,
    "no_recommendation_reason": str | None,
}
```

**每个 RangeProfile：**
```python
{
    "lower_price": float,
    "upper_price": float,
    "lower_tick": int,
    "upper_tick": int,
    "width_pct": float,
    "fee_apr": float,           # 年化手续费（注意：仍是 token-level 聚合值，偏高）
    "shrunk_fee_apr": float,    # 年轻池 fee APR 收缩后的值
    "il_cost": float,
    "breach_probability": float,
    "utility_score": float,     # replay 效用
    "final_utility": float,     # 混合效用
    "scenario_pnl": dict,       # 5 种场景下的 PnL 预测
    "risk_flags": list[str],    # 包括 confidence_floor 触发信息
    "execution_cost_fraction": float,
    "expected_net_pnl": float,
}
```

---

## 七、当前已知的数据质量局限

| 字段 | 问题 | 严重程度 |
|---|---|---|
| `fee_apr` | OKX volume API 返回 token 级别聚合，跨所有 DEX。一个 $7M TVL 池可能显示 282% APR，实际可能 < 50% | **高** |
| `recommendation_confidence` | = final_utility × regime_scale，不是真实历史胜率 | 中 |
| `breach_probability` | blended = replay OOR 率 + GBM 终端概率，不是首次穿越，低估真实穿越率 | 中 |
| `confidence_regime_scales` | chaotic/trend 均为 0.5（BOUNDS 下限），无法区分"轻度趋势"和"极度混沌" | 低 |

---

## 八、已完成 vs 待办（截至 2026-03-27 最新 commit）

**Demo-ready 基线：`a074e73`（branch `main`）**

**完整 commit 链（main 分支，从旧到新，仅 P2 阶段）：**
```
52adb7b docs(phase1.5): implementation status, validation report, release notes
860737e feat(p2.1.1): pool-specific volume correction via DexScreener volume.h24
4c80681 feat(p2.1.2): fee tier resolution with feeTier-first priority
04d54be P2.1.3: promote source_quality to pool_candle when pool volume exists
2992b1b feat(p2.2.3): blended breach probability via GBM terminal OOR proxy
34297a0 fix(frontend): shrunk_fee_apr display value + execution_cost_fraction rendering
3c0b5f7 feat(frontend): evidence strength info card for young/blended pools
6aede3b feat(backend/infra): execution cost model, fee fetcher, calibration config
a4aeefa feat(backend/algo): P2.2.1a IL edge correction, P2.3.1 execution cost, confidence calibration
9e8ed40 test(backend): expand validate_backend.py for P2.x features
793771d feat(scripts): offline calibration script + sample calibration.json
c68badd docs: update phase2 plan; add handoff context
a074e73 chore: gitignore AI session files  ← DEMO-READY BASELINE
```

### ✅ 已完成（P2 阶段，截至 a074e73）

**P2.1.1 Pool-specific volume correction（已完成）**
```python
# range_recommender.py — _pool_volume_fraction()
# pool["volume_24h"] = DexScreener vol.get("h24") → 已是 pool-specific
# OKX token-level bar volumes 通过 volume_fraction 缩放
volume_fraction = _pool_volume_fraction(pool["volume_24h"], ohlcv_1h)
# volume_fraction = pool_vol_24h / sum(last24_okx_bars_vol), clamped [0,1]

# backtest 和 scenario 都传了 volume_scale=volume_fraction
backtests = backtest_all_candidates(..., volume_scale=volume_fraction)
scenario_pnl_list = compute_all_scenario_pnl(..., volume_scale=volume_fraction)
```

**P2.1.2 Fee tier via feeTier field（已完成）**
- `_infer_fee_rate()` 现优先读取 DexScreener `feeTier` 字段；再 fallback 到静态查表
- Raydium CLMM / Meteora DLMM / Uniswap V3 subgraph 链均有 native fee 获取

**P2.1.3 Source quality promotion（已完成）**
```python
# pool["volume_24h"] > 0 AND len(ohlcv_1h) >= 12 → "pool_candle" (sq=0.7)
# 否则 → "token_level" (sq=0.4)
_source_quality = (
    "pool_candle"
    if pool["volume_24h"] > 0 and len(ohlcv_1h) >= 12
    else "token_level"
)
```
影响：成熟池的 evidence_score +0.06，uncertainty_penalty −0.03

**P2.2.3 Blended breach probability（已完成）**
- `breach_probability` = `replay_weight * replay_oor + (1-replay_weight) * analytical_oor`
- `analytical_oor` = GBM 终端 OOR 概率（非首次穿越，保守下界）
- 年轻池（replay_weight 低）更多依赖 GBM 分析解

**P2.2.1a IL edge correction heuristic（已完成）**
- `_il_edge_weight()` 在 final price 靠近区间边界时放大 IL（最多 1.4×）
- 仅作用于 in-range IL 路径；OOR 路径不受影响
- 注：这是终端价格启发式，不是真实 tick 流动性模型

**P2.3.1 Execution cost model（已完成）**
- `execution_cost.py`：链感知 gas + 滑点模型
- `execution_cost_fraction` 字段在 API 响应和前端中均可见
- `position_usd` 参数支持（API POST/GET 均支持，缓存按 position-specific 绕过）

**前端 Bug 修复（已完成）**
- `shrunk_fee_apr` 显示值错误（年轻池显示 `expected` 而非 `shrunk`）已修复
- `execution_cost_fraction` 未渲染已修复
- Evidence strength 信息卡（`effective_evidence_score` / `replay_weight` / `scenario_weight`）已添加

### 🟡 待办（中优先级）

**P2.1.2 真实 fee tier（部分完成，需配置）**
- `fee_fetcher.py` 已实现 Uniswap V3 subgraph 查询
- 只需在 `.env` 配置 `UNISWAP_V3_SUBGRAPH_ETHEREUM` / `UNISWAP_V3_SUBGRAPH_BASE`
- DexScreener `feeTier` 字段不存在（已确认），subgraph 是唯一可靠来源

**P2.2.2 LP 仓位竞争因子（crowding）**
- 当前：fee 捕获 ∝ position/pool TVL（线性假设）
- 改进：考虑流动性集中度，更窄区间的竞争更激烈

**P2.3.2 CEX/DEX price divergence signal**
- OKX CEX 价格 spread > 1% 时强制标记 chaotic（需 CEX 价格 API 集成）

### 🟢 低优先级

**GBM terminal → first-passage 升级**
- 当前 breach_prob 是终端概率（lower bound），可升级为 double-barrier first-passage 解析解

---

## 九、验证状态

```bash
cd backend && python3 validate_backend.py
```

**当前结果：313 PASS / 0 FAIL / 3 WARN**

WARN 均为预期行为（DexScreener `feeTier` 字段不存在，自动 fallback 到静态查表）。

测试分组：
- A1-A18：纯逻辑单元测试（history sufficiency / regime / scoring / calibration）
- B1-B5：真实网络集成测试（BSC/Base/Solana/Ethereum/Raydium 原生 fee）
- C1-C3：年轻池仿真测试
- D1-D2：拒绝场景测试

---

## 十、校准管道使用方式

```bash
cd /path/to/LP-Sonar/backend

# 预检（不写文件）
python3 -m scripts.calibrate --dry-run

# 正式校准（需要 OKX API 可达）
python3 -m scripts.calibrate --out data/calibration.json

# 验证加载
python3 -c "
import logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
from app.core.config import settings
print('growing_threshold:', settings.calibration_growing_standard_threshold)
print('rw_bounds:', settings.replay_weight_lower_bound, settings.replay_weight_upper_bound)
"
```

**校准方法（calibrate.py 内部）：**
- 9 个参考对：USDC-USDT（超低波动）→ BTC/ETH/BNB/LINK → SOL → DOGE → PEPE/WIF（极高波动 meme）
- ±10% 固定宽度窗口（非自适应，防止不同 regime 的穿越率被归一化）
- Walk-forward + GBM 合成路径（各 regime 等比例采样，避免 chaotic 被排除）
- 总计 171 个真实样本，输出参数受 BOUNDS 约束（`[0.30, 0.90]` 等）

**三层优先级：**
```
ENV 变量（highest） > calibration.json > Python 默认值（lowest）
# confidence_floor 不接受 calibration.json 覆盖（运营参数，ENV only）
```

---

## 十一、下一步推荐

**P2.1.1 / P2.1.3 / P2.2.1a / P2.2.3 / P2.3.1 均已完成**（截至 `a074e73`）。

当前最高价值的下一步是 **P2.1.2 真实 fee tier（配置激活）**：
- 代码已经写好（`fee_fetcher.py` + `config.py` 中的 subgraph URL 字段）
- 只需要找到一个可用的 Uniswap V3 subgraph endpoint 并配置 ENV
- 影响：Uniswap V3 非标准档位（0.05%/1%）不再默认 0.3%，fee APR 误差可达 6×
- 无需算法改动

其次是 **P2.3.2 CEX/DEX price divergence signal**：
- OKX CEX API 已有 key，只需新增一个端点调用
- 高波动事件时 regime 检测更准确
