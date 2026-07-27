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
}
