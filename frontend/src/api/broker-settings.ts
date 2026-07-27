import { webClient } from './client'

// Generic session-route response envelope (mirrors the Flask blueprints).
export interface ApiResponse<T = void> {
  status: string
  message?: string
  data?: T
}

// ---------------------------------------------------------------------------
// Broker API credentials (GET/POST /api/broker/credentials)
// Shapes mirror blueprints/broker_credentials.py exactly. All secret fields are
// server-masked (fixed-length "prefix + ********"); the *_raw_length fields give
// the true length so the UI can show "not set" vs "set" without leaking it.
// ---------------------------------------------------------------------------
export interface ServerEndpoint {
  host: string
  port: string
}

export interface BrokerCredentials {
  broker_api_key: string
  broker_api_key_raw_length: number
  broker_api_secret: string
  broker_api_secret_raw_length: number
  broker_api_key_market: string
  broker_api_key_market_raw_length: number
  broker_api_secret_market: string
  broker_api_secret_market_raw_length: number
  redirect_url: string
  current_broker: string
  valid_brokers: string[]
  ngrok_allow: boolean
  host_server: string
  websocket_url: string
  server_status: {
    flask: ServerEndpoint
    websocket: ServerEndpoint
    zmq: ServerEndpoint
  }
}

// Only non-empty fields are persisted server-side; an omitted/empty field keeps
// the existing .env value unchanged.
export interface UpdateCredentialsBody {
  broker_api_key?: string
  broker_api_secret?: string
  broker_api_key_market?: string
  broker_api_secret_market?: string
  redirect_url?: string
  ngrok_allow?: boolean
  host_server?: string
  websocket_url?: string
}

export interface UpdateCredentialsResult {
  updated_fields: string[]
  restart_required: boolean
}

// ---------------------------------------------------------------------------
// Automated daily login / TOTP (GET/POST /api/broker/autologin, /run)
// SECURITY: the backend NEVER returns the password or totp_secret value (not
// even masked) — only booleans for whether each is set, plus a masked user_id.
// ---------------------------------------------------------------------------
export interface AutoLoginStatus {
  fields: {
    user_id: boolean
    password: boolean
    totp_secret: boolean
  }
  configured: boolean
  masked: {
    user_id: string
  }
}

// Omitted / empty fields are left unchanged; secret values are write-only.
export interface UpdateAutoLoginBody {
  user_id?: string
  password?: string
  totp_secret?: string
}

export const brokerSettingsApi = {
  /**
   * Masked broker API credentials + server endpoints. Session route (webClient).
   */
  getCredentials: async (): Promise<ApiResponse<BrokerCredentials>> => {
    const response = await webClient.get<ApiResponse<BrokerCredentials>>('/api/broker/credentials')
    return response.data
  },

  /**
   * Update broker API credentials in the server .env. Only non-empty fields are
   * written; empty fields keep the existing value. Session route (webClient) —
   * POST auto-sends the CSRF token.
   */
  updateCredentials: async (
    body: UpdateCredentialsBody
  ): Promise<ApiResponse<UpdateCredentialsResult>> => {
    const response = await webClient.post<ApiResponse<UpdateCredentialsResult>>(
      '/api/broker/credentials',
      body
    )
    return response.data
  },

  /**
   * Auto-login (TOTP) configuration status — booleans + masked user_id only.
   * Session route (webClient).
   */
  getAutoLogin: async (): Promise<ApiResponse<AutoLoginStatus>> => {
    const response = await webClient.get<ApiResponse<AutoLoginStatus>>('/api/broker/autologin')
    return response.data
  },

  /**
   * Persist auto-login credentials. Send only changed, non-empty fields; the
   * password and TOTP secret are write-only and never echoed back. Session
   * route (webClient) — POST auto-sends CSRF.
   */
  updateAutoLogin: async (body: UpdateAutoLoginBody): Promise<ApiResponse<AutoLoginStatus>> => {
    const response = await webClient.post<ApiResponse<AutoLoginStatus>>(
      '/api/broker/autologin',
      body
    )
    return response.data
  },

  /**
   * Trigger a headless auto-login now. Returns a human-readable message on both
   * success and error. Session route (webClient).
   */
  runAutoLogin: async (): Promise<ApiResponse> => {
    const response = await webClient.post<ApiResponse>('/api/broker/autologin/run')
    return response.data
  },
}
