import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ChevronsUpDown,
  Info,
  RefreshCw,
  TrendingUp,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { autonomousApi, type PerfSummary, type StrategyPerformanceData } from '@/api/autonomous'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'

// Range presets — a rolling window of trading days requested from the backend.
const RANGES = [
  { label: '7D', days: 7 },
  { label: '30D', days: 30 },
  { label: '90D', days: 90 },
] as const

/** Signed en-IN integer, e.g. +15,520 / -540. */
function formatSigned(value: number): string {
  const sign = value < 0 ? '-' : '+'
  return sign + Math.abs(Math.round(value)).toLocaleString('en-IN')
}

// Sortable leaderboard columns. `key` selects the comparable value from a row.
type SortKey =
  | 'name'
  | 'total_pnl'
  | 'win_rate'
  | 'trade_count'
  | 'avg_win'
  | 'avg_loss'
  | 'profit_factor'
  | 'max_drawdown'
  | 'best_day'
  | 'worst_day'

interface Row extends PerfSummary {
  name: string
}

function sortValue(row: Row, key: SortKey): number | string {
  switch (key) {
    case 'name':
      return row.name.toLowerCase()
    case 'profit_factor':
      // Null profit factor (no losing trades) sorts as best.
      return row.profit_factor ?? Number.POSITIVE_INFINITY
    case 'best_day':
      return row.best_day?.pnl ?? Number.NEGATIVE_INFINITY
    case 'worst_day':
      return row.worst_day?.pnl ?? Number.POSITIVE_INFINITY
    default:
      return row[key]
  }
}

export default function StrategyPerformance() {
  const { user } = useAuthStore()
  const { mode: themeMode } = useThemeStore()
  const isDarkMode = themeMode === 'dark'
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])

  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<StrategyPerformanceData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  // Client-side leaderboard sort state (default: total P&L descending).
  const [sortKey, setSortKey] = useState<SortKey>('total_pnl')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  // Chart refs (mirror of PnLTracker.tsx / Autonomous.tsx).
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      const res = await autonomousApi.getStrategyPerformance({ days })
      if (res.status === 'success' && res.data) {
        setData(res.data)
      } else {
        setError(true)
        setData(null)
      }
    } catch {
      setError(true)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    load()
  }, [load])

  // ---- lightweight-charts init (mirror of PnLTracker / Autonomous) ----
  const initChart = useCallback(() => {
    if (!chartContainerRef.current) return
    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }
    const container = chartContainerRef.current
    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 260,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#a6adbb' : '#333',
      },
      grid: {
        vertLines: { visible: false },
        horzLines: {
          color: isDarkMode ? 'rgba(166, 173, 187, 0.1)' : 'rgba(0, 0, 0, 0.08)',
          style: 1,
          visible: true,
        },
      },
      rightPriceScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        scaleMargins: { top: 0.15, bottom: 0.1 },
      },
      timeScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        timeVisible: false,
        secondsVisible: false,
      },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#570df8',
      topColor: 'rgba(87, 13, 248, 0.4)',
      bottomColor: 'rgba(87, 13, 248, 0.0)',
      lineWidth: 2,
      priceScaleId: 'right',
      priceFormat: {
        type: 'custom',
        formatter: (price: number) => formatCurrency(price),
      },
    })
    chartRef.current = chart
    seriesRef.current = series

    const handleResize = () => {
      if (chartRef.current && container) {
        chartRef.current.applyOptions({ width: container.offsetWidth })
      }
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [isDarkMode, formatCurrency])

  useEffect(() => {
    const cleanup = initChart()
    return () => {
      cleanup?.()
      if (chartRef.current) {
        chartRef.current.remove()
        chartRef.current = null
      }
      seriesRef.current = null
    }
  }, [initChart])

  // Push the portfolio cumulative equity curve into the chart whenever data or
  // the chart instance changes. Dates map to midnight-UTC timestamps.
  useEffect(() => {
    const series = seriesRef.current
    const daily = data?.totals.daily
    if (!series) return
    if (daily && daily.length > 0) {
      const points = daily
        .map((d) => ({
          time: (Date.parse(`${d.date}T00:00:00Z`) / 1000) as UTCTimestamp,
          value: d.cumulative,
        }))
        .sort((a, b) => a.time - b.time)
      series.setData(points)
      chartRef.current?.timeScale().fitContent()
    } else {
      series.setData([])
    }
  }, [data])

  const totals = data?.totals
  const hasData = !!totals && totals.trade_count > 0

  const pnlClass = (v: number) =>
    v > 0 ? 'text-green-500' : v < 0 ? 'text-red-500' : 'text-muted-foreground'

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      // Names default to A→Z; numeric columns default to high→low.
      setSortDir(key === 'name' ? 'asc' : 'desc')
    }
  }

  const rows = useMemo<Row[]>(() => {
    if (!data) return []
    const list: Row[] = Object.entries(data.per_strategy).map(([name, s]) => ({ ...s, name }))
    list.sort((a, b) => {
      const av = sortValue(a, sortKey)
      const bv = sortValue(b, sortKey)
      let cmp = 0
      if (typeof av === 'string' || typeof bv === 'string') {
        cmp = String(av).localeCompare(String(bv))
      } else {
        cmp = av - bv
      }
      return sortDir === 'asc' ? cmp : -cmp
    })
    return list
  }, [data, sortKey, sortDir])

  const SortHeader = ({
    label,
    col,
    align = 'left',
  }: {
    label: string
    col: SortKey
    align?: 'left' | 'right'
  }) => {
    const active = sortKey === col
    const Icon = !active ? ChevronsUpDown : sortDir === 'asc' ? ArrowUp : ArrowDown
    return (
      <TableHead className={align === 'right' ? 'text-right' : ''}>
        <button
          type="button"
          onClick={() => toggleSort(col)}
          className={`inline-flex items-center gap-1 hover:text-foreground ${
            align === 'right' ? 'flex-row-reverse' : ''
          } ${active ? 'text-foreground' : ''}`}
        >
          {label}
          <Icon className="h-3 w-3 opacity-70" />
        </button>
      </TableHead>
    )
  }

  return (
    <div className="container mx-auto py-6 space-y-6">
      {/* ---- Header ---- */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <TrendingUp className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Strategy Performance</h1>
            <p className="text-sm text-muted-foreground">Historical per-strategy track record</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {data && (
            <Badge variant={data.mode === 'live' ? 'default' : 'secondary'} className="uppercase">
              {data.mode === 'live' ? 'Live' : 'Analyzer'}
            </Badge>
          )}
          {data && (
            <span className="text-sm text-muted-foreground">
              {data.range.start} → {data.range.end} · {data.range.trading_days} trading day
              {data.range.trading_days === 1 ? '' : 's'}
            </span>
          )}
          <div className="inline-flex rounded-md border p-0.5">
            {RANGES.map((r) => (
              <Button
                key={r.days}
                variant={days === r.days ? 'default' : 'ghost'}
                size="sm"
                className="h-7 px-3"
                onClick={() => setDays(r.days)}
              >
                {r.label}
              </Button>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ---- Live-mode history limitation (honest) ---- */}
      {data?.live_partial && (
        <Card className="border-amber-500/40 bg-amber-500/10">
          <CardContent className="flex items-start gap-2 py-4 text-sm text-amber-700 dark:text-amber-400">
            <Info className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              Live mode shows the current trading day only. Broker fills are available for today
              and are not stored historically, so this range reflects at most today&apos;s trades.
              Switch to Analyzer (sandbox) mode for a full multi-day track record.
            </span>
          </CardContent>
        </Card>
      )}

      {/* ---- Error / unavailable state ---- */}
      {error && (
        <Card className="border-red-500/40 bg-red-500/10">
          <CardContent className="flex items-center gap-2 py-4 text-sm text-red-700 dark:text-red-400">
            <AlertTriangle className="h-4 w-4" />
            Performance data is unavailable right now. Use Refresh to retry.
          </CardContent>
        </Card>
      )}

      {/* ---- Empty state ---- */}
      {!error && !loading && !hasData && (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <TrendingUp className="h-8 w-8 text-muted-foreground" />
            <p className="max-w-md text-sm text-muted-foreground">
              No completed trades in this range — performance appears here as strategies trade. Run
              in sandbox to build a track record.
            </p>
          </CardContent>
        </Card>
      )}

      {/* ---- Portfolio summary + equity curve ---- */}
      {!error && hasData && totals && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5">
            <StatTile
              label="Total P&L"
              value={formatSigned(totals.total_pnl)}
              valueClass={pnlClass(totals.total_pnl)}
              note={`${totals.win_days} win · ${totals.loss_days} loss days`}
            />
            <StatTile
              label="Win rate"
              value={`${totals.win_rate}%`}
              note={`${totals.win_trades} W · ${totals.loss_trades} L`}
            />
            <StatTile label="Trades" value={String(totals.trade_count)} note="closed in range" />
            <StatTile
              label="Max drawdown"
              value={formatCurrency(Math.abs(totals.max_drawdown))}
              valueClass={totals.max_drawdown < 0 ? 'text-red-500' : ''}
              note="peak to trough"
            />
            <StatTile
              label="Trading days"
              value={String(totals.trading_days)}
              note="with activity"
            />
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Portfolio cumulative P&amp;L</CardTitle>
            </CardHeader>
            <CardContent>
              <div ref={chartContainerRef} className="relative" style={{ height: '260px' }} />
              {data && data.untagged_pnl !== 0 && (
                <p className="mt-2 flex items-start gap-1.5 text-xs text-muted-foreground">
                  <Info className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>
                    Portfolio includes {formatSigned(data.untagged_pnl)} of untagged / manual
                    P&amp;L not attributed to any strategy below.
                  </span>
                </p>
              )}
            </CardContent>
          </Card>

          {/* ---- Leaderboard ---- */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Strategy leaderboard</CardTitle>
            </CardHeader>
            <CardContent>
              {rows.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No tagged strategy trades in this range.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <SortHeader label="Strategy" col="name" />
                        <SortHeader label="Total P&L" col="total_pnl" align="right" />
                        <SortHeader label="Win rate" col="win_rate" align="right" />
                        <SortHeader label="Trades" col="trade_count" align="right" />
                        <SortHeader label="Avg win" col="avg_win" align="right" />
                        <SortHeader label="Avg loss" col="avg_loss" align="right" />
                        <SortHeader label="Profit factor" col="profit_factor" align="right" />
                        <SortHeader label="Max DD" col="max_drawdown" align="right" />
                        <SortHeader label="Best day" col="best_day" align="right" />
                        <SortHeader label="Worst day" col="worst_day" align="right" />
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rows.map((s) => (
                        <TableRow key={s.name}>
                          <TableCell className="font-medium">{s.name}</TableCell>
                          <TableCell className={`text-right font-mono ${pnlClass(s.total_pnl)}`}>
                            {formatSigned(s.total_pnl)}
                          </TableCell>
                          <TableCell className="text-right font-mono">{s.win_rate}%</TableCell>
                          <TableCell className="text-right font-mono text-muted-foreground">
                            {s.trade_count}
                          </TableCell>
                          <TableCell className="text-right font-mono text-green-500">
                            {formatCurrency(s.avg_win)}
                          </TableCell>
                          <TableCell className="text-right font-mono text-red-500">
                            {formatCurrency(Math.abs(s.avg_loss))}
                          </TableCell>
                          <TableCell className="text-right font-mono">
                            {s.profit_factor == null ? (
                              <span className="text-muted-foreground">—</span>
                            ) : (
                              s.profit_factor.toFixed(2)
                            )}
                          </TableCell>
                          <TableCell className="text-right font-mono text-red-500">
                            {s.max_drawdown < 0 ? formatCurrency(Math.abs(s.max_drawdown)) : '—'}
                          </TableCell>
                          <TableCell className="text-right font-mono">
                            {s.best_day ? (
                              <span className="text-green-500">{formatSigned(s.best_day.pnl)}</span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right font-mono">
                            {s.worst_day ? (
                              <span className={pnlClass(s.worst_day.pnl)}>
                                {formatSigned(s.worst_day.pnl)}
                              </span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
              <p className="mt-3 flex items-start gap-1.5 text-xs text-muted-foreground">
                <Info className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  Performance is attributed from tagged orders and reflects actual executed trades
                  (live or sandbox). Profit factor shown as &quot;—&quot; means no losing trades.
                  Past performance does not guarantee future results.
                </span>
              </p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}

interface StatTileProps {
  label: string
  value: string
  note?: string
  valueClass?: string
}

function StatTile({ label, value, note, valueClass = '' }: StatTileProps) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1 pt-4">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
        <span className={`font-mono text-xl font-semibold ${valueClass}`}>{value}</span>
        {note && <span className="text-xs text-muted-foreground">{note}</span>}
      </CardContent>
    </Card>
  )
}
