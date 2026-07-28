import {
  CheckCircle2,
  Circle,
  Eye,
  EyeOff,
  Info,
  KeyRound,
  Loader2,
  Plug,
  RefreshCw,
  Save,
  ShieldCheck,
  Wallet,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { autonomousApi } from '@/api/autonomous'
import {
  type AutoLoginStatus,
  type BrokerCredentials,
  brokerSettingsApi,
  type CapitalStatus,
} from '@/api/broker-settings'
import { tradingApi } from '@/api/trading'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import type { MarginData } from '@/types/trading'
import { showToast } from '@/utils/toast'

type ProbeState = 'checking' | 'live' | 'error' | 'idle'

/** A password-type input with a show/hide toggle. Reveal only echoes what the
 * user just typed — masked/secret server values are never loaded into it. */
function SecretInput({
  id,
  value,
  onChange,
  placeholder,
  autoComplete = 'off',
}: {
  id: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  autoComplete?: string
}) {
  const [show, setShow] = useState(false)
  return (
    <div className="relative">
      <Input
        id={id}
        type={show ? 'text' : 'password'}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        className="pr-10 font-mono"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="absolute right-0 top-0 h-full px-3 hover:bg-transparent"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? 'Hide value' : 'Show value'}
      >
        {show ? (
          <EyeOff className="h-4 w-4 text-muted-foreground" />
        ) : (
          <Eye className="h-4 w-4 text-muted-foreground" />
        )}
      </Button>
    </div>
  )
}

/** Green check when set/true, grey circle otherwise. */
function StatusDot({ on, label }: { on: boolean; label: string }) {
  return (
    <span className="flex items-center gap-2 text-sm">
      {on ? (
        <CheckCircle2 className="h-4 w-4 text-green-500" />
      ) : (
        <Circle className="h-4 w-4 text-muted-foreground/40" />
      )}
      <span className={on ? '' : 'text-muted-foreground'}>{label}</span>
    </span>
  )
}

export default function BrokerSettings() {
  const { user, apiKey } = useAuthStore()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])

  // ---- Section 1: connection status ----
  const [mode, setMode] = useState<'live' | 'analyzer' | null>(null)
  const [probe, setProbe] = useState<ProbeState>(apiKey ? 'checking' : 'idle')
  const [funds, setFunds] = useState<MarginData | null>(null)

  // ---- Section 2: broker API credentials ----
  const [creds, setCreds] = useState<BrokerCredentials | null>(null)
  const [credsError, setCredsError] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [apiSecretInput, setApiSecretInput] = useState('')
  const [redirectInput, setRedirectInput] = useState('')
  const [savingCreds, setSavingCreds] = useState(false)

  // ---- Section 3: automated daily login (TOTP) ----
  const [autoLogin, setAutoLogin] = useState<AutoLoginStatus | null>(null)
  const [autoLoginError, setAutoLoginError] = useState(false)
  const [userIdInput, setUserIdInput] = useState('')
  const [passwordInput, setPasswordInput] = useState('')
  const [totpInput, setTotpInput] = useState('')
  const [savingAutoLogin, setSavingAutoLogin] = useState(false)
  const [runningAutoLogin, setRunningAutoLogin] = useState(false)

  // ---- Section 4: daily capital allocation (sourced from the broker) ----
  const [capital, setCapital] = useState<CapitalStatus | null>(null)
  const [capitalError, setCapitalError] = useState(false)
  const [capitalMode, setCapitalMode] = useState<'percent' | 'amount'>('percent')
  const [percentInput, setPercentInput] = useState('')
  const [amountInput, setAmountInput] = useState('')
  const [savingCapital, setSavingCapital] = useState(false)

  const loadProbe = useCallback(async () => {
    if (!apiKey) {
      setProbe('idle')
      return
    }
    setProbe('checking')
    try {
      const res = await tradingApi.getFunds(apiKey)
      if (res.status === 'success' && res.data) {
        setFunds(res.data)
        setProbe('live')
      } else {
        setProbe('error')
      }
    } catch {
      setProbe('error')
    }
  }, [apiKey])

  const loadCapital = useCallback(async () => {
    setCapitalError(false)
    try {
      const res = await brokerSettingsApi.getCapital()
      if (res.status === 'success' && res.data) {
        setCapital(res.data)
        setCapitalMode(res.data.capital_mode)
        setPercentInput(String(res.data.percent))
        setAmountInput(String(res.data.amount))
      } else {
        setCapitalError(true)
      }
    } catch {
      setCapitalError(true)
    }
  }, [])

  const saveCapital = useCallback(async () => {
    setSavingCapital(true)
    try {
      const body: { mode: 'percent' | 'amount'; percent?: number; amount?: number } = {
        mode: capitalMode,
      }
      // Send only the field that governs the chosen basis, so a stale value in
      // the other input can never be silently persisted as the allocation.
      if (capitalMode === 'percent') {
        const pct = Number(percentInput)
        if (!Number.isFinite(pct) || pct < 0 || pct > 100) {
          showToast.error('Percent must be between 0 and 100')
          return
        }
        body.percent = pct
      } else {
        const amt = Number(amountInput)
        if (!Number.isFinite(amt) || amt < 0) {
          showToast.error('Amount must be 0 or more')
          return
        }
        body.amount = amt
      }
      const res = await brokerSettingsApi.updateCapital(body)
      if (res.status === 'success') {
        showToast.success('Capital allocation saved')
        await loadCapital()
      } else {
        showToast.error(res.message || 'Could not save capital allocation')
      }
    } catch {
      showToast.error('Could not save capital allocation')
    } finally {
      setSavingCapital(false)
    }
  }, [capitalMode, percentInput, amountInput, loadCapital])

  const loadMode = useCallback(async () => {
    try {
      const res = await autonomousApi.getStrategyPnl()
      if (res.status === 'success' && res.data) {
        setMode(res.data.mode)
      }
    } catch {
      // Non-fatal — mode badge falls back to "unknown".
    }
  }, [])

  const loadCreds = useCallback(async () => {
    try {
      const res = await brokerSettingsApi.getCredentials()
      if (res.status === 'success' && res.data) {
        setCreds(res.data)
        setRedirectInput(res.data.redirect_url || '')
        setCredsError(false)
      } else {
        setCredsError(true)
      }
    } catch {
      setCredsError(true)
    }
  }, [])

  const loadAutoLogin = useCallback(async () => {
    try {
      const res = await brokerSettingsApi.getAutoLogin()
      if (res.status === 'success' && res.data) {
        setAutoLogin(res.data)
        setAutoLoginError(false)
      } else {
        setAutoLoginError(true)
      }
    } catch {
      setAutoLoginError(true)
    }
  }, [])

  useEffect(() => {
    loadProbe()
    loadMode()
    loadCreds()
    loadAutoLogin()
    loadCapital()
  }, [loadProbe, loadMode, loadCreds, loadAutoLogin, loadCapital])

  // ---- save handlers ----
  const handleSaveCreds = async () => {
    const body: Record<string, string> = {}
    if (apiKeyInput.trim()) body.broker_api_key = apiKeyInput.trim()
    if (apiSecretInput.trim()) body.broker_api_secret = apiSecretInput.trim()
    if (redirectInput.trim() && redirectInput.trim() !== (creds?.redirect_url || '')) {
      body.redirect_url = redirectInput.trim()
    }
    if (Object.keys(body).length === 0) {
      showToast.info('No changes to save')
      return
    }
    setSavingCreds(true)
    try {
      const res = await brokerSettingsApi.updateCredentials(body)
      if (res.status === 'success') {
        showToast.success(res.message || 'Broker credentials saved. Restart required.')
        setApiKeyInput('')
        setApiSecretInput('')
        loadCreds()
      } else {
        showToast.error(res.message || 'Failed to save credentials')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Failed to save credentials')
    } finally {
      setSavingCreds(false)
    }
  }

  const handleSaveAutoLogin = async () => {
    const body: Record<string, string> = {}
    if (userIdInput.trim()) body.user_id = userIdInput.trim()
    if (passwordInput) body.password = passwordInput
    if (totpInput.trim()) body.totp_secret = totpInput.trim()
    if (Object.keys(body).length === 0) {
      showToast.info('No changes to save')
      return
    }
    setSavingAutoLogin(true)
    try {
      const res = await brokerSettingsApi.updateAutoLogin(body)
      if (res.status === 'success' && res.data) {
        setAutoLogin(res.data)
        showToast.success('Auto-login credentials saved')
        setUserIdInput('')
        setPasswordInput('')
        setTotpInput('')
      } else {
        showToast.error(res.message || 'Failed to save auto-login credentials')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Failed to save auto-login credentials')
    } finally {
      setSavingAutoLogin(false)
    }
  }

  const handleRunAutoLogin = async () => {
    setRunningAutoLogin(true)
    try {
      const res = await brokerSettingsApi.runAutoLogin()
      if (res.status === 'success') {
        showToast.success(res.message || 'Auto-login completed')
        loadProbe()
        loadMode()
      } else {
        showToast.error(res.message || 'Auto-login failed')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Auto-login failed')
    } finally {
      setRunningAutoLogin(false)
    }
  }

  const brokerName = user?.broker || null
  const notConnected = probe === 'error' || !brokerName
  const callbackUrl = `${window.location.origin}/zerodha/callback`

  return (
    <div className="container mx-auto max-w-4xl py-6 space-y-6">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Plug className="h-5 w-5" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Broker Settings</h1>
          <p className="text-sm text-muted-foreground">
            Connection status, API credentials, and automated daily login
          </p>
        </div>
      </div>

      {/* ---- Section 1: Connection status ---- */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4" />
            Connection status
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
            <div className="flex items-center gap-2">
              <span className="text-muted-foreground">Broker</span>
              {brokerName ? (
                <Badge variant="outline" className="uppercase">
                  {brokerName}
                </Badge>
              ) : (
                <span className="text-muted-foreground">Not connected</span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <span className="text-muted-foreground">Mode</span>
              {mode ? (
                <Badge variant={mode === 'live' ? 'default' : 'secondary'}>
                  {mode === 'live' ? 'Live' : 'Analyzer (sandbox)'}
                </Badge>
              ) : (
                <span className="text-muted-foreground">Unknown</span>
              )}
            </div>

            <div className="flex items-center gap-2">
              {probe === 'live' ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-green-500" />
                  <span>Session active</span>
                  {funds && (
                    <span className="font-mono text-muted-foreground">
                      {formatCurrency(funds.availablecash)}
                    </span>
                  )}
                </>
              ) : probe === 'error' ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-amber-500" />
                  <span className="text-amber-600 dark:text-amber-400">
                    Session not live — login needed
                  </span>
                </>
              ) : probe === 'checking' ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-muted-foreground/40" />
                  <span className="text-muted-foreground">Checking session…</span>
                </>
              ) : (
                <>
                  <span className="h-2 w-2 rounded-full bg-muted-foreground/40" />
                  <span className="text-muted-foreground">No API key — session unknown</span>
                </>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button asChild variant={notConnected ? 'default' : 'outline'}>
              <a href="/broker">
                <Plug className="h-4 w-4 mr-2" />
                {notConnected ? 'Connect broker' : 'Re-login'}
              </a>
            </Button>
            <Button variant="ghost" size="sm" onClick={loadProbe} disabled={probe === 'checking'}>
              <RefreshCw className={`h-4 w-4 mr-2 ${probe === 'checking' ? 'animate-spin' : ''}`} />
              Re-check
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* ---- Section 2: Broker API credentials ---- */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <KeyRound className="h-4 w-4" />
            Broker API credentials
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {credsError ? (
            <p className="flex items-start gap-1.5 text-sm text-amber-600 dark:text-amber-400">
              <Info className="mt-0.5 h-4 w-4 shrink-0" />
              Credentials unavailable — could not load from the server.
            </p>
          ) : (
            <>
              <div className="space-y-2">
                <Label htmlFor="broker_api_key">API Key</Label>
                <SecretInput
                  id="broker_api_key"
                  value={apiKeyInput}
                  onChange={setApiKeyInput}
                  placeholder={
                    creds?.broker_api_key_raw_length
                      ? `${creds.broker_api_key} (set — leave blank to keep)`
                      : 'Not set'
                  }
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="broker_api_secret">API Secret</Label>
                <SecretInput
                  id="broker_api_secret"
                  value={apiSecretInput}
                  onChange={setApiSecretInput}
                  placeholder={
                    creds?.broker_api_secret_raw_length
                      ? `${creds.broker_api_secret} (set — leave blank to keep)`
                      : 'Not set'
                  }
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="redirect_url">Redirect URL</Label>
                <SecretInput
                  id="redirect_url"
                  value={redirectInput}
                  onChange={setRedirectInput}
                  placeholder="https://<host>/zerodha/callback"
                />
                <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <Info className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>
                    This redirect URL must be registered in the Kite developer console as{' '}
                    <span className="font-mono">{callbackUrl}</span>.
                  </span>
                </p>
              </div>

              <div className="flex items-center gap-3">
                <Button onClick={handleSaveCreds} disabled={savingCreds}>
                  {savingCreds ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4 mr-2" />
                  )}
                  Save credentials
                </Button>
                <span className="text-xs text-muted-foreground">
                  Changes are written to the server .env; a restart is required to take effect.
                </span>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {/* ---- Section 3: Automated daily login (TOTP) ---- */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <RefreshCw className="h-4 w-4" />
            Automated daily login (TOTP)
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {autoLoginError ? (
            <p className="flex items-start gap-1.5 text-sm text-amber-600 dark:text-amber-400">
              <Info className="mt-0.5 h-4 w-4 shrink-0" />
              Auto-login status unavailable — could not load from the server.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border bg-muted/30 p-3">
                <StatusDot on={!!autoLogin?.fields.user_id} label="User ID" />
                <StatusDot on={!!autoLogin?.fields.password} label="Password" />
                <StatusDot on={!!autoLogin?.fields.totp_secret} label="TOTP secret" />
                <span className="ml-auto">
                  {autoLogin?.configured ? (
                    <Badge className="bg-green-500/15 text-green-600 dark:text-green-400">
                      Configured
                    </Badge>
                  ) : (
                    <Badge variant="secondary">Incomplete</Badge>
                  )}
                </span>
              </div>

              <div className="space-y-2">
                <Label htmlFor="zerodha_user_id">User ID</Label>
                <SecretInput
                  id="zerodha_user_id"
                  value={userIdInput}
                  onChange={setUserIdInput}
                  placeholder={
                    autoLogin?.masked.user_id
                      ? `${autoLogin.masked.user_id} (set — leave blank to keep)`
                      : 'Not set'
                  }
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="zerodha_password">Password</Label>
                <SecretInput
                  id="zerodha_password"
                  value={passwordInput}
                  onChange={setPasswordInput}
                  placeholder={autoLogin?.fields.password ? 'Set — leave blank to keep' : 'Not set'}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="zerodha_totp_secret">TOTP secret</Label>
                <SecretInput
                  id="zerodha_totp_secret"
                  value={totpInput}
                  onChange={setTotpInput}
                  placeholder={
                    autoLogin?.fields.totp_secret ? 'Set — leave blank to keep' : 'Not set'
                  }
                />
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={handleSaveAutoLogin} disabled={savingAutoLogin}>
                  {savingAutoLogin ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4 mr-2" />
                  )}
                  Save
                </Button>
                <Button
                  variant="outline"
                  onClick={handleRunAutoLogin}
                  disabled={runningAutoLogin || !autoLogin?.configured}
                >
                  {runningAutoLogin ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4 mr-2" />
                  )}
                  Run auto-login now
                </Button>
              </div>

              <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                <Info className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  The TOTP secret is the base32 SEED from Kite&apos;s External-TOTP setup (not a
                  6-digit code). Credentials are stored in the server .env and are never displayed
                  back — keep the server secured.
                </span>
              </p>
            </>
          )}
        </CardContent>
      </Card>

      {/* ---- Section 4: Daily capital allocation (from the broker) ---- */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Wallet className="h-4 w-4" />
            Daily capital allocation
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {capitalError ? (
            <p className="text-sm text-red-600 dark:text-red-400">
              Could not load capital settings. Use Refresh to retry.
            </p>
          ) : !capital ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : (
            <>
              {/* Live broker funds — the source of truth, never typed in. */}
              {capital.funds_error ? (
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
                  <p className="flex items-start gap-1.5 text-sm text-amber-700 dark:text-amber-400">
                    <Info className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>
                      Capital could not be read from the broker ({capital.funds_error}). Connect the
                      broker above — no allocation is shown rather than a misleading zero.
                    </span>
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <FundTile
                    label="Available cash"
                    value={formatCurrency(capital.funds.availablecash)}
                  />
                  <FundTile label="Utilised" value={formatCurrency(capital.funds.utiliseddebits)} />
                  <FundTile label="Collateral" value={formatCurrency(capital.funds.collateral)} />
                  <FundTile
                    label="Allocated today"
                    value={formatCurrency(capital.allocated)}
                    highlight
                  />
                </div>
              )}

              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Badge variant={capital.mode === 'live' ? 'default' : 'secondary'}>
                  {capital.mode === 'live' ? 'LIVE' : 'ANALYZER'}
                </Badge>
                <span>
                  {capital.mode === 'live'
                    ? 'Figures come from your live broker account.'
                    : 'Figures come from the sandbox account (analyzer mode).'}
                </span>
              </div>

              {/* Allocation basis */}
              <div className="space-y-3">
                <Label>Allocate</Label>
                <div className="inline-flex rounded-md border p-0.5">
                  <Button
                    type="button"
                    variant={capitalMode === 'percent' ? 'default' : 'ghost'}
                    size="sm"
                    className="h-7 px-3"
                    onClick={() => setCapitalMode('percent')}
                  >
                    % of available
                  </Button>
                  <Button
                    type="button"
                    variant={capitalMode === 'amount' ? 'default' : 'ghost'}
                    size="sm"
                    className="h-7 px-3"
                    onClick={() => setCapitalMode('amount')}
                  >
                    Fixed amount
                  </Button>
                </div>

                {capitalMode === 'percent' ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="capital-percent">Percent of available cash</Label>
                    <Input
                      id="capital-percent"
                      type="number"
                      min={0}
                      max={100}
                      step="1"
                      value={percentInput}
                      onChange={(e) => setPercentInput(e.target.value)}
                      className="max-w-[200px] font-mono"
                    />
                  </div>
                ) : (
                  <div className="space-y-1.5">
                    <Label htmlFor="capital-amount">Fixed amount (INR)</Label>
                    <Input
                      id="capital-amount"
                      type="number"
                      min={0}
                      step="1000"
                      value={amountInput}
                      onChange={(e) => setAmountInput(e.target.value)}
                      className="max-w-[220px] font-mono"
                    />
                  </div>
                )}
              </div>

              {capital.clamped && (
                <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
                  <Info className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>
                    The saved amount exceeds available cash, so it was reduced to{' '}
                    {formatCurrency(capital.allocated)}. Allocation can never exceed what the broker
                    reports.
                  </span>
                </p>
              )}

              <div className="flex flex-wrap gap-2">
                <Button onClick={saveCapital} disabled={savingCapital}>
                  {savingCapital ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4 mr-2" />
                  )}
                  Save allocation
                </Button>
                <Button variant="outline" onClick={loadCapital} disabled={savingCapital}>
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Refresh from broker
                </Button>
              </div>

              <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                <Info className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  Capital is read from your broker account, never typed in, and the allocation is
                  capped at available cash. This records how much of the account you intend to
                  commit today and is shown here and via the API — it does <strong>not</strong> by
                  itself block orders. The automatic stop is the daily loss limit on the{' '}
                  <a href="/autonomous" className="underline">
                    autonomous dashboard
                  </a>
                  .
                </span>
              </p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/** A small labelled figure tile for the broker funds row. */
function FundTile({
  label,
  value,
  highlight = false,
}: {
  label: string
  value: string
  highlight?: boolean
}) {
  return (
    <div className={`rounded-md border p-3 ${highlight ? 'border-primary/50 bg-primary/5' : ''}`}>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="font-mono text-lg font-semibold">{value}</div>
    </div>
  )
}
