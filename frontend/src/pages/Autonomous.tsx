import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import {
  Activity,
  AlertTriangle,
  Bot,
  Circle,
  Info,
  PauseCircle,
  Power,
  RefreshCw,
  Square,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { autonomousApi, type StrategyPnl, type StrategyPnlData } from '@/api/autonomous'
import { pythonStrategyApi } from '@/api/python-strategy'
import { tradingApi } from '@/api/trading'
import { useSocketContext } from '@/components/socket/SocketProvider'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import { makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'
import type { PythonStrategy } from '@/types/python-strategy'
import type { Position } from '@/types/trading'
import { showToast } from '@/utils/toast'

const DEFAULT_LAGGARD_THRESHOLD = -500
// Sandbox trades against a fixed ₹1 Crore of simulation capital (see CLAUDE.md /
// docs). Only used to derive a day-P&L percentage in analyzer mode; never shown
// for live mode where the deployed capital is unknown to this view.
const SANDBOX_CAPITAL = 10000000

interface FeedEntry {
  id: string
  time: string
  action: string
  symbol: string
  orderid: string
}

/** Signed en-IN integer, e.g. +15,520 / -540. */
function formatSigned(value: number): string {
  const sign = value < 0 ? '-' : '+'
  return sign + Math.abs(Math.round(value)).toLocaleString('en-IN')
}

/** A strategy is "on" when it is running or armed for its schedule. */
function isStrategyOn(s: PythonStrategy): boolean {
  return (
    Boolean(s.is_running) ||
    Boolean(s.is_scheduled) ||
    s.status === 'running' ||
    s.status === 'scheduled'
  )
}

export default function Autonomous() {
  const { user } = useAuthStore()
  const { mode: themeMode } = useThemeStore()
  const isDarkMode = themeMode === 'dark'
  const { apiKey } = useAuthStore()
  const { socket } = useSocketContext()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])

  const [strategies, setStrategies] = useState<PythonStrategy[]>([])
  const [pnl, setPnl] = useState<StrategyPnlData | null>(null)
  const [positions, setPositions] = useState<Position[]>([])
  const [feed, setFeed] = useState<FeedEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<string | null>(null)
  const [hasCurve, setHasCurve] = useState(true)

  const [engineOff, setEngineOff] = useState(false)
  const [autoPause, setAutoPause] = useState(false)
  const [threshold, setThreshold] = useState<number>(DEFAULT_LAGGARD_THRESHOLD)
  // Inline confirm target: which mass action is awaiting confirmation.
  const [confirming, setConfirming] = useState<'engine-off' | 'flatten' | null>(null)

  // Chart refs (mirrors PnLTracker.tsx)
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null)

  // ---- data loading ----
  const loadStrategies = useCallback(async () => {
    try {
      const data = await pythonStrategyApi.getStrategies()
      setStrategies(data)
    } catch {
      showToast.error('Failed to load strategies', 'pythonStrategy')
    }
  }, [])

  const loadPnl = useCallback(async () => {
    try {
      const res = await autonomousApi.getStrategyPnl()
      if (res.status === 'success' && res.data) {
        setPnl(res.data)
      }
    } catch {
      // Non-fatal — the page still renders the strategy list without P&L.
    }
  }, [])

  const loadPositions = useCallback(async () => {
    if (!apiKey) return
    try {
      const res = await tradingApi.getPositions(apiKey)
      if (res.status === 'success' && Array.isArray(res.data)) {
        setPositions(res.data.filter((p) => p.quantity !== 0))
      }
    } catch {
      // Non-fatal.
    }
  }, [apiKey])

  const loadCurve = useCallback(async () => {
    try {
      const res = await autonomousApi.getPnlSeries()
      const series = res.status === 'success' ? res.data?.pnl_series : undefined
      if (seriesRef.current && series && series.length > 0) {
        const points = series
          .map((p) => ({
            time: Math.floor(p.time / 1000) as UTCTimestamp,
            value: p.value,
          }))
          .sort((a, b) => a.time - b.time)
        seriesRef.current.setData(points)
        chartRef.current?.timeScale().fitContent()
        setHasCurve(true)
      } else {
        setHasCurve(false)
      }
    } catch {
      setHasCurve(false)
    }
  }, [])

  const refetch = useCallback(() => {
    loadStrategies()
    loadPnl()
    loadPositions()
    loadCurve()
  }, [loadStrategies, loadPnl, loadPositions, loadCurve])

  // Initial load
  useEffect(() => {
    setLoading(true)
    Promise.all([loadStrategies(), loadPnl(), loadPositions()]).finally(() => setLoading(false))
  }, [loadStrategies, loadPnl, loadPositions])

  // ---- lightweight-charts init (mirror of PnLTracker) ----
  const initChart = useCallback(() => {
    if (!chartContainerRef.current) return
    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }
    const container = chartContainerRef.current
    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 200,
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
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: number) => {
          const date = new Date(time * 1000)
          const istDate = new Date(date.getTime() + 5.5 * 60 * 60 * 1000)
          const hours = istDate.getUTCHours().toString().padStart(2, '0')
          const minutes = istDate.getUTCMinutes().toString().padStart(2, '0')
          return `${hours}:${minutes}`
        },
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
    loadCurve()
    return () => {
      cleanup?.()
      if (chartRef.current) {
        chartRef.current.remove()
        chartRef.current = null
      }
      seriesRef.current = null
    }
  }, [initChart, loadCurve])

  // ---- live refresh: socket order events (canonical hook) ----
  useOrderEventRefresh(refetch, {
    events: ['order_event', 'analyzer_update', 'close_position_event'],
    enabled: !!user,
  })

  // ---- live refresh: strategy status SSE (mirror of PythonStrategyIndex) ----
  useEffect(() => {
    const eventSource = new EventSource('/python/api/events')
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'connected') return
        if (data.strategy_id && data.status) {
          loadStrategies()
        }
      } catch {
        // Ignore heartbeat / parse errors
      }
    }
    eventSource.onerror = () => {}
    return () => eventSource.close()
  }, [loadStrategies])

  // ---- live activity feed: capture order events while mounted ----
  useEffect(() => {
    if (!socket) return
    const onOrder = (data: { symbol: string; action: string; orderid: string }) => {
      const now = new Date()
      const time = `${now.getHours().toString().padStart(2, '0')}:${now
        .getMinutes()
        .toString()
        .padStart(2, '0')}`
      setFeed((prev) =>
        [
          {
            id: `${data.orderid}-${now.getTime()}`,
            time,
            action: data.action,
            symbol: data.symbol,
            orderid: data.orderid,
          },
          ...prev,
        ].slice(0, 30)
      )
    }
    socket.on('order_event', onOrder)
    return () => {
      socket.off('order_event', onOrder)
    }
  }, [socket])

  // ---- per-strategy P&L join: match by strategy name, fall back to id ----
  const pnlFor = useCallback(
    (s: PythonStrategy): StrategyPnl | undefined => {
      if (!pnl) return undefined
      return pnl.per_strategy[s.name] ?? pnl.per_strategy[s.id]
    },
    [pnl]
  )

  const isLaggard = useCallback(
    (s: PythonStrategy): boolean => {
      if (engineOff || !isStrategyOn(s)) return false
      const p = pnlFor(s)
      return p !== undefined && p.day_pnl < threshold
    },
    [engineOff, pnlFor, threshold]
  )

  const laggards = useMemo(() => strategies.filter(isLaggard), [strategies, isLaggard])

  // ---- auto-pause: stop laggards whenever P&L refreshes and the rule is on ----
  const stopStrategy = useCallback(async (s: PythonStrategy) => {
    try {
      await pythonStrategyApi.stopStrategy(s.id)
    } catch {
      // best-effort; surfaced by the caller's toast/refetch
    }
  }, [])

  const startStrategy = useCallback(async (s: PythonStrategy) => {
    try {
      await pythonStrategyApi.startStrategy(s.id)
    } catch {
      // best-effort
    }
  }, [])

  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally keyed on pnl+autoPause; evaluating laggards each P&L tick
  useEffect(() => {
    if (!autoPause || !pnl) return
    const toStop = strategies.filter(isLaggard)
    if (toStop.length === 0) return
    ;(async () => {
      await Promise.allSettled(toStop.map((s) => stopStrategy(s)))
      showToast.warning(
        `Auto-paused ${toStop.length} strategy(ies) below ${threshold}`,
        'pythonStrategy'
      )
      loadStrategies()
    })()
  }, [pnl, autoPause])

  // ---- row toggle ----
  const handleToggle = async (s: PythonStrategy) => {
    const turningOn = !isStrategyOn(s)
    setRowBusy(s.id)
    // Optimistic
    setStrategies((prev) =>
      prev.map((x) => (x.id === s.id ? { ...x, is_running: turningOn, is_scheduled: false } : x))
    )
    try {
      const res = turningOn
        ? await pythonStrategyApi.startStrategy(s.id)
        : await pythonStrategyApi.stopStrategy(s.id)
      if (res.status === 'success') {
        showToast.success(
          res.message || `${s.name} ${turningOn ? 'started' : 'stopped'}`,
          'pythonStrategy'
        )
      } else {
        showToast.error(res.message || 'Action failed', 'pythonStrategy')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Action failed', 'pythonStrategy')
    } finally {
      setRowBusy(null)
      loadStrategies()
    }
  }

  // ---- master engine ----
  const applyEngineToggle = async () => {
    const goingOff = !engineOff
    setBusy(true)
    setConfirming(null)
    try {
      if (goingOff) {
        const on = strategies.filter(isStrategyOn)
        await Promise.allSettled(on.map((s) => stopStrategy(s)))
        setEngineOff(true)
        showToast.success(`Engine paused — halted ${on.length} strategy(ies)`, 'pythonStrategy')
      } else {
        const off = strategies.filter((s) => !isStrategyOn(s))
        await Promise.allSettled(off.map((s) => startStrategy(s)))
        setEngineOff(false)
        showToast.success(`Engine resumed — starting ${off.length} strategy(ies)`, 'pythonStrategy')
      }
    } finally {
      setBusy(false)
      loadStrategies()
    }
  }

  const handleEngineClick = () => {
    if (!engineOff) {
      // Turning OFF is destructive — confirm inline.
      setConfirming('engine-off')
    } else {
      applyEngineToggle()
    }
  }

  // ---- flatten & halt ----
  const handleFlatten = async () => {
    setBusy(true)
    setConfirming(null)
    try {
      const on = strategies.filter(isStrategyOn)
      await Promise.allSettled(on.map((s) => stopStrategy(s)))
      const res = await tradingApi.closeAllPositions()
      setEngineOff(true)
      if (res.status === 'success') {
        showToast.success('Flattened all positions and halted the engine', 'positions')
      } else {
        showToast.error(res.message || 'Close-all failed; strategies were halted', 'positions')
      }
    } catch {
      showToast.error('Flatten failed; strategies were halted', 'positions')
    } finally {
      setBusy(false)
      refetch()
    }
  }

  const handlePauseLaggards = async () => {
    if (laggards.length === 0) return
    setBusy(true)
    try {
      await Promise.allSettled(laggards.map((s) => stopStrategy(s)))
      showToast.success(`Paused ${laggards.length} laggard(s)`, 'pythonStrategy')
    } finally {
      setBusy(false)
      loadStrategies()
    }
  }

  // ---- derived ----
  const totals = pnl?.totals
  const onCount = strategies.filter(isStrategyOn).length
  const dayPnl = totals?.day_pnl ?? 0
  const contributions = useMemo(() => {
    if (!pnl) return []
    return Object.entries(pnl.per_strategy)
      .filter(([, p]) => p.day_pnl !== 0)
      .sort((a, b) => b[1].day_pnl - a[1].day_pnl)
  }, [pnl])
  const maxAbsContribution = useMemo(
    () => Math.max(1, ...contributions.map(([, p]) => Math.abs(p.day_pnl))),
    [contributions]
  )

  const pnlClass = (v: number) =>
    v > 0 ? 'text-green-500' : v < 0 ? 'text-red-500' : 'text-muted-foreground'

  return (
    <div className="container mx-auto py-6 space-y-6">
      {/* ---- Top status bar ---- */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Bot className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Autonomous Control</h1>
            <p className="text-sm text-muted-foreground">
              Single-switch control over every Python strategy
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {pnl && (
            <Badge variant={pnl.mode === 'live' ? 'default' : 'secondary'} className="uppercase">
              {pnl.mode === 'live' ? 'Live' : 'Analyzer'}
            </Badge>
          )}
          <Button variant="outline" size="sm" onClick={refetch} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>

          {/* Master engine switch */}
          <div
            className={`flex items-center gap-2 rounded-full border px-3 py-1.5 ${
              engineOff ? 'border-border' : 'border-green-500/40 bg-green-500/10'
            }`}
          >
            <Power
              className={`h-4 w-4 ${engineOff ? 'text-muted-foreground' : 'text-green-500'}`}
            />
            <span
              className={`text-sm font-semibold ${engineOff ? 'text-muted-foreground' : 'text-green-500'}`}
            >
              {engineOff ? 'Engine paused' : 'Engine on'}
            </span>
            <Switch
              checked={!engineOff}
              disabled={busy}
              onCheckedChange={handleEngineClick}
              aria-label="Master autonomous engine"
            />
          </div>

          <Button
            variant="destructive"
            size="sm"
            disabled={busy}
            onClick={() => setConfirming('flatten')}
          >
            <Square className="h-4 w-4 mr-2" />
            Flatten &amp; halt
          </Button>
        </div>
      </div>

      {/* Inline confirms */}
      {confirming === 'engine-off' && (
        <Card className="border-yellow-500/40 bg-yellow-500/10">
          <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-yellow-500" />
              Pause the engine? This stops all {onCount} running strategy(ies). Open positions stay
              as-is.
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setConfirming(null)}>
                Cancel
              </Button>
              <Button size="sm" onClick={applyEngineToggle} disabled={busy}>
                Pause engine
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
      {confirming === 'flatten' && (
        <Card className="border-red-500/40 bg-red-500/10">
          <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-red-500" />
              Flatten &amp; halt? This stops every strategy and squares off all open positions.
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setConfirming(null)}>
                Cancel
              </Button>
              <Button variant="destructive" size="sm" onClick={handleFlatten} disabled={busy}>
                Flatten &amp; halt
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Engine-off banner */}
      {engineOff && (
        <Card className="border-yellow-500/40 bg-yellow-500/10">
          <CardContent className="flex items-center gap-2 py-3 text-sm text-yellow-700 dark:text-yellow-400">
            <PauseCircle className="h-4 w-4" />
            Engine paused — all strategies are halted and no new orders will be placed. Open
            positions stay as-is until you resume or flatten.
          </CardContent>
        </Card>
      )}

      {/* ---- Hero: Day P&L + stat tiles ---- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Day P&amp;L · realized + open
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className={`text-4xl font-bold font-mono ${pnlClass(dayPnl)}`}>
              {formatSigned(dayPnl)}
            </div>
            {pnl?.mode === 'analyzer' ? (
              <div className={`text-sm mt-1 ${pnlClass(dayPnl)}`}>
                {((dayPnl / SANDBOX_CAPITAL) * 100).toFixed(3)}% on ₹1 Cr sandbox capital
              </div>
            ) : (
              <div className="text-sm mt-1 text-muted-foreground">
                Realized + open, all strategies
              </div>
            )}
            <div className="mt-4">
              <div ref={chartContainerRef} className="relative" style={{ height: '200px' }} />
              {!hasCurve && (
                <p className="mt-2 text-xs text-muted-foreground">
                  P&amp;L curve populates as trades occur.
                </p>
              )}
            </div>
          </CardContent>
        </Card>

        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:col-span-2">
          <StatTile
            label="Realized"
            value={totals ? formatSigned(totals.realized) : '—'}
            valueClass={totals ? pnlClass(totals.realized) : ''}
            note={totals ? `${totals.trades} trades` : ''}
          />
          <StatTile
            label="Unrealized"
            value={totals ? formatSigned(totals.unrealized) : '—'}
            valueClass={totals ? pnlClass(totals.unrealized) : ''}
            note={totals ? `${totals.open_positions} open` : ''}
          />
          <StatTile
            label="Open positions"
            value={totals ? String(totals.open_positions) : '—'}
            note="live broker positions"
          />
          <StatTile
            label="Win rate"
            value={totals ? `${totals.win_rate}%` : '—'}
            note={totals ? `${totals.wins} W · ${totals.losses} L` : ''}
          />
          <StatTile
            label="Trades"
            value={totals ? String(totals.trades) : '—'}
            note="closed today"
          />
          <StatTile label="Strategies on" value={`${onCount}`} note={`of ${strategies.length}`} />
        </div>
      </div>

      {/* ---- Main: strategies table + side column ---- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="gap-3">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <CardTitle>Strategies</CardTitle>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Switch
                    checked={autoPause}
                    onCheckedChange={setAutoPause}
                    aria-label="Auto-pause laggards"
                  />
                  Auto-pause below
                </label>
                <Input
                  type="number"
                  value={threshold}
                  onChange={(e) => setThreshold(Number(e.target.value))}
                  className="h-8 w-24 font-mono"
                  aria-label="Laggard threshold"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handlePauseLaggards}
                  disabled={busy || laggards.length === 0}
                >
                  <PauseCircle className="h-4 w-4 mr-2" />
                  Pause laggards ({laggards.length})
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {strategies.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                {loading ? 'Loading strategies…' : 'No Python strategies configured yet.'}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-12">On</TableHead>
                      <TableHead>Strategy</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead>Open symbol</TableHead>
                      <TableHead className="text-right">Position P&amp;L</TableHead>
                      <TableHead className="text-right">Day P&amp;L</TableHead>
                      <TableHead className="text-right">Trades</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {strategies.map((s) => {
                      const on = isStrategyOn(s)
                      const p = pnlFor(s)
                      const lag = isLaggard(s)
                      return (
                        <TableRow
                          key={s.id}
                          className={`${!on || engineOff ? 'opacity-50' : ''} ${lag ? 'bg-red-500/5' : ''}`}
                        >
                          <TableCell>
                            <Switch
                              checked={on && !engineOff}
                              disabled={rowBusy === s.id || busy || engineOff}
                              onCheckedChange={() => handleToggle(s)}
                              aria-label={`Toggle ${s.name}`}
                            />
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              <span className="font-medium">{s.name}</span>
                              {lag && (
                                <Badge variant="destructive" className="text-[10px] uppercase">
                                  Laggard
                                </Badge>
                              )}
                            </div>
                            <span className="font-mono text-xs text-muted-foreground">
                              {s.exchange}
                            </span>
                          </TableCell>
                          <TableCell>
                            {engineOff || !on ? (
                              <Badge variant="secondary">Off</Badge>
                            ) : p?.open_symbol ? (
                              <Badge className="bg-blue-500/15 text-blue-600 dark:text-blue-400">
                                In position
                              </Badge>
                            ) : s.status === 'error' ? (
                              <Badge variant="destructive">Error</Badge>
                            ) : (
                              <Badge className="bg-green-500/15 text-green-600 dark:text-green-400">
                                Scanning
                              </Badge>
                            )}
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {p?.open_symbol ? (
                              <span>
                                {p.open_symbol}
                                {p.open_side && (
                                  <span
                                    className={`ml-2 rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                      p.open_side.toUpperCase() === 'LONG'
                                        ? 'bg-green-500/15 text-green-600 dark:text-green-400'
                                        : 'bg-red-500/15 text-red-600 dark:text-red-400'
                                    }`}
                                  >
                                    {p.open_side.toUpperCase()}
                                  </span>
                                )}
                              </span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                          <TableCell
                            className={`text-right font-mono ${p ? pnlClass(p.unrealized) : ''}`}
                          >
                            {p ? (
                              formatSigned(p.unrealized)
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                          <TableCell
                            className={`text-right font-mono ${p ? pnlClass(p.day_pnl) : ''}`}
                          >
                            {p ? (
                              formatSigned(p.day_pnl)
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right font-mono text-muted-foreground">
                            {p ? p.trades : <span>—</span>}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
            <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Info className="h-3 w-3" />
              Per-strategy P&amp;L is attributed from tagged orders; sums may differ from the
              portfolio total (untagged/manual trades).
            </p>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          {/* Open positions */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Open positions</CardTitle>
            </CardHeader>
            <CardContent>
              {positions.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">No open positions.</p>
              ) : (
                <div className="space-y-3">
                  {positions.map((pos) => {
                    const long = pos.quantity > 0
                    return (
                      <div
                        key={`${pos.symbol}-${pos.exchange}-${pos.product}`}
                        className="flex items-center justify-between"
                      >
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-sm font-semibold">{pos.symbol}</span>
                            <span
                              className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                long
                                  ? 'bg-green-500/15 text-green-600 dark:text-green-400'
                                  : 'bg-red-500/15 text-red-600 dark:text-red-400'
                              }`}
                            >
                              {long ? 'LONG' : 'SHORT'}
                            </span>
                          </div>
                          <span className="font-mono text-xs text-muted-foreground">
                            {pos.quantity} @ {pos.average_price} → {pos.ltp}
                          </span>
                        </div>
                        <span className={`font-mono text-sm font-semibold ${pnlClass(pos.pnl)}`}>
                          {formatSigned(pos.pnl)}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Activity */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-base">
                <Activity className="h-4 w-4" />
                Activity
              </CardTitle>
            </CardHeader>
            <CardContent>
              {feed.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  Live order activity appears here as strategies trade.
                </p>
              ) : (
                <div className="max-h-72 space-y-2 overflow-y-auto">
                  {feed.map((f) => {
                    const buy = f.action.toUpperCase() === 'BUY'
                    return (
                      <div key={f.id} className="flex items-baseline gap-2 text-sm">
                        <span className="font-mono text-xs text-muted-foreground">{f.time}</span>
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                            buy
                              ? 'bg-green-500/15 text-green-600 dark:text-green-400'
                              : 'bg-red-500/15 text-red-600 dark:text-red-400'
                          }`}
                        >
                          {f.action}
                        </span>
                        <span className="font-mono">{f.symbol}</span>
                        <span className="ml-auto font-mono text-xs text-muted-foreground">
                          {f.orderid}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* ---- Bottom: contribution bars + watchlist ---- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">P&amp;L contribution by strategy</CardTitle>
          </CardHeader>
          <CardContent>
            {contributions.length === 0 ? (
              <p className="py-4 text-center text-sm text-muted-foreground">
                Contribution appears here once strategies book P&amp;L today.
              </p>
            ) : (
              <div className="space-y-2">
                {contributions.map(([name, p]) => {
                  const pct = (Math.abs(p.day_pnl) / maxAbsContribution) * 100
                  const positive = p.day_pnl >= 0
                  return (
                    <div key={name} className="flex items-center gap-3">
                      <span className="w-40 shrink-0 truncate text-sm" title={name}>
                        {name}
                      </span>
                      <div className="flex flex-1 items-center">
                        <div className="flex w-1/2 justify-end">
                          {!positive && (
                            <div
                              className="h-3 rounded bg-red-500/80"
                              style={{ width: `${pct}%` }}
                            />
                          )}
                        </div>
                        <div className="flex w-1/2 justify-start">
                          {positive && (
                            <div
                              className="h-3 rounded bg-green-500/80"
                              style={{ width: `${pct}%` }}
                            />
                          )}
                        </div>
                      </div>
                      <span
                        className={`w-20 shrink-0 text-right font-mono text-sm ${pnlClass(p.day_pnl)}`}
                      >
                        {formatSigned(p.day_pnl)}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Screened watchlist</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <Circle className="h-6 w-6 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                Screened watchlist appears here once the pre-market screener has run.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
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
