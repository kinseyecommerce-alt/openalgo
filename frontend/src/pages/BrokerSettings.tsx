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
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { autonomousApi } from '@/api/autonomous'
import {
  type AutoLoginStatus,
  type BrokerCredentials,
  brokerSettingsApi,
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
  }, [loadProbe, loadMode, loadCreds, loadAutoLogin])

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
    </div>
  )
}
