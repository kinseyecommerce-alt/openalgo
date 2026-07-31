import type { ApiResponse } from '@/types/trading'
import { webClient } from './client'

// Per-strategy P&L attributed from tagged orders (the `strategy` order tag).
// Sums may differ from the portfolio total because untagged / manual trades
// are not attributed to any strategy.
export interface StrategyPnl {
  realized: number
  unrealized: number
  day_pnl: number
  trades: number
  open_symbol: string | null
  open_qty: number
  open_side: string | null
}

export interface StrategyPnlTotals {
  day_pnl: number
  realized: number
  unrealized: number
  open_positions: number
  trades: number
  wins: number
  losses: number
  win_rate: number
}

export interface StrategyPnlData {
  per_strategy: Record<string, StrategyPnl>
  totals: StrategyPnlTotals
  mode: 'live' | 'analyzer'
}

// Intraday MTM series from the PnL Tracker (real, trade-derived).
export interface PnlSeriesPoint {
  time: number
  value: number
}

export interface PnlTrackerData {
  current_mtm: number
  max_mtm: number
  max_mtm_time: string
  min_mtm: number
  min_mtm_time: string
  max_drawdown: number
  pnl_series: PnlSeriesPoint[]
  drawdown_series: PnlSeriesPoint[]
}

// Screened watchlist written by the pre-market screener / MCX resolver to
// strategies/watchlists/<EXCHANGE>.txt and read back for display.
export interface ScreenedWatchlist {
  exchange: string
  symbols: string[]
  count: number
  generated: string | null
  updated_at: string | null
}

export interface WatchlistsData {
  watchlists: ScreenedWatchlist[]
}

// Server-side (authoritative) portfolio circuit breaker. Runs on the server
// every minute and halts all strategies even when this dashboard is closed.
export interface RiskStatus {
  enabled: boolean
  daily_loss_limit: number
  flatten_on_halt: boolean
  halted: boolean
  halted_at: string | null
  halted_reason: string | null
  halted_pnl: number | null
  day_pnl: number
  breaching: boolean
}

export interface RiskUpdate {
  enabled?: boolean
  daily_loss_limit?: number
  flatten_on_halt?: boolean
}

// Historical per-strategy track-record analytics (Strategy Performance page).
// Attributed from tagged orders — reflects actual executed trades (live or
// sandbox). profit_factor is null when there are no losing trades.
export interface PerfSummary {
  total_pnl: number
  trading_days: number
  trade_count: number
  win_trades: number
  loss_trades: number
  win_rate: number
  avg_win: number
  avg_loss: number
  profit_factor: number | null
  win_days: number
  loss_days: number
  best_day: { date: string; pnl: number } | null
  worst_day: { date: string; pnl: number } | null
  max_drawdown: number
  cumulative: number
  daily: Array<{ date: string; pnl: number; cumulative: number; trades: number }>
}

export interface StrategyPerformanceData {
  range: { start: string; end: string; trading_days: number }
  mode: 'live' | 'analyzer'
  // True in live mode when the requested range is wider than the current
  // trading day. Live fills exist only for today (the broker tradebook resets
  // daily and live fill prices are not persisted historically), so a multi-day
  // live range reflects, at most, today. Analyzer/sandbox mode has the full
  // history and never sets this. The UI shows an honest note when true.
  live_partial: boolean
  untagged_pnl: number
  totals: PerfSummary
  per_strategy: Record<string, PerfSummary>
}

export const autonomousApi = {
  /**
   * Per-strategy and portfolio P&L for the logged-in user.
   * Session route (webClient) — detects live vs analyzer mode server-side.
   */
  getStrategyPnl: async (): Promise<ApiResponse<StrategyPnlData>> => {
    const response = await webClient.get<ApiResponse<StrategyPnlData>>('/python/api/strategy-pnl')
    return response.data
  },

  /**
   * Intraday MTM P&L curve (shared with the PnL Tracker page).
   * Session route (webClient) — POST auto-sends the CSRF token.
   */
  getPnlSeries: async (): Promise<ApiResponse<PnlTrackerData>> => {
    const response = await webClient.post<ApiResponse<PnlTrackerData>>('/pnltracker/api/pnl', {})
    return response.data
  },

  /**
   * Screened watchlists (NSE / BSE / MCX / ...) the pre-market screener and
   * MCX resolver wrote to disk. Session route (webClient). Only exchanges
   * whose file exists are returned.
   */
  getWatchlists: async (): Promise<ApiResponse<WatchlistsData>> => {
    const response = await webClient.get<ApiResponse<WatchlistsData>>('/python/api/watchlists')
    return response.data
  },

  /**
   * Server-side (authoritative) circuit-breaker status. Session route
   * (webClient). The breaker runs on the server every minute — it halts even
   * when this dashboard is closed.
   */
  getRisk: async (): Promise<ApiResponse<RiskStatus>> => {
    const response = await webClient.get<ApiResponse<RiskStatus>>('/python/api/risk')
    return response.data
  },

  /**
   * Update the server-side circuit-breaker config (enable, loss limit,
   * flatten-on-halt). Session route (webClient) — POST auto-sends CSRF.
   */
  updateRisk: async (body: RiskUpdate): Promise<ApiResponse<RiskStatus>> => {
    const response = await webClient.post<ApiResponse<RiskStatus>>('/python/api/risk', body)
    return response.data
  },

  /**
   * Clear a tripped server-side breaker so strategies can be started again.
   * Session route (webClient).
   */
  resetRisk: async (): Promise<ApiResponse<RiskStatus>> => {
    const response = await webClient.post<ApiResponse<RiskStatus>>('/python/api/risk/reset', {})
    return response.data
  },

  /**
   * Historical per-strategy track record over a date range. Session route
   * (webClient). Pass either `days` (rolling window) or an explicit
   * `start`/`end` (YYYY-MM-DD). Detects live vs analyzer mode server-side.
   */
  getStrategyPerformance: async (params?: {
    days?: number
    start?: string
    end?: string
  }): Promise<ApiResponse<StrategyPerformanceData>> => {
    const response = await webClient.get<ApiResponse<StrategyPerformanceData>>(
      '/python/api/strategy-performance',
      { params }
    )
    return response.data
  },
}
