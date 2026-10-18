/**
 * The HTTP client: one place that knows about the error envelope.
 *
 * Every failure from the API — validation, authorization, a stale version, an unexpected
 * server error — arrives as `{ error: { code, message, details? } }`. This module turns
 * that into a typed `ApiError`, so components branch on `code` rather than on a status
 * number or, worse, on the text of a message.
 *
 * It also owns the bearer token. The access token lives in a module variable (memory
 * only). The refresh token is an HttpOnly cookie managed by the browser. Silent
 * refresh is handled here.
 */

import type {
  ActivityEvent,
  ActivityFilters,
  Comment,
  ItemFilters,
  MeResponse,
  MemberSummary,
  MarkAllReadResult,
  Notification,
  Page,
  Priority,
  Status,
  TeamSummary,
  TokenPairResponse,
  UnreadCount,
  WorkItemDetail,
  WorkItemSummary,
  WorkItemType,
} from './types'

/**
 * Where the API lives.
 *
 * * unset — the local development default;
 * * empty — **the same origin**. The container image is served by nginx, which forwards
 *   `/api` to the API, so the browser needs no cross-origin permission and the image has no
 *   machine-specific host name baked into it;
 * * anything else — that origin.
 *
 * The three cases are distinct, which is why this is not a `||`: an empty string is a
 * decision, not a missing value.
 */
const CONFIGURED_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined

const BASE_URL: string =
  CONFIGURED_BASE_URL === undefined
    ? 'http://localhost:8000'
    : CONFIGURED_BASE_URL === ''
      ? window.location.origin
      : CONFIGURED_BASE_URL

/**
 * The API's stable error codes, as a union.
 *
 * Listing them means a component can switch exhaustively on `code` and the compiler
 * notices when the API grows one.
 */
export type ApiErrorCode =
  | 'validation_failed'
  | 'invalid_credentials'
  | 'not_authenticated'
  | 'invalid_session'
  | 'session_expired'
  | 'forbidden'
  | 'not_found'
  | 'invalid_transition'
  | 'item_already_claimed'
  | 'idempotency_key_required'
  | 'idempotency_key_reuse'
  | 'idempotency_key_in_progress'
  | 'assignee_required'
  | 'last_admin'
  | 'member_has_assigned_items'
  | 'stale_version'
  | 'precondition_required'
  | 'invalid_assignee'
  | 'method_not_allowed'
  | 'http_error'
  | 'internal_error'
  | 'network_error'

export interface FieldError {
  field: string
  message: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: ApiErrorCode
  readonly details: unknown

  constructor(status: number, code: ApiErrorCode, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }

  /** Validation failures name the offending fields; everything else has none. */
  get fieldErrors(): FieldError[] {
    if (!Array.isArray(this.details)) return []
    return this.details.filter(
      (entry): entry is FieldError =>
        typeof entry === 'object' &&
        entry !== null &&
        'field' in entry &&
        'message' in entry &&
        typeof (entry as FieldError).field === 'string' &&
        typeof (entry as FieldError).message === 'string',
    )
  }

  /** True when the session is gone and the only way forward is to sign in again. */
  get isAuthenticationFailure(): boolean {
    return this.status === 401
  }
}

let accessToken: string | null = null
let accessExpiresAt: string | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null

export function setAccessToken(value: string | null): void {
  accessToken = value
}

export function getAccessToken(): string | null {
  return accessToken
}

export function getAccessExpiresAt(): string | null {
  return accessExpiresAt
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  /** Query parameters. `undefined` entries are dropped; arrays repeat the key. */
  params?: Record<string, string | number | boolean | string[] | null | undefined>
  headers?: Record<string, string>
  signal?: AbortSignal
}

function buildUrl(path: string, params: RequestOptions['params']): string {
  const url = new URL(path, BASE_URL)
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      for (const entry of value) url.searchParams.append(key, entry)
    } else {
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

async function readError(response: Response): Promise<ApiError> {
  let code: ApiErrorCode = 'http_error'
  let message = `The request failed (${response.status}).`
  let details: unknown
  try {
    const payload = (await response.json()) as {
      error?: { code?: string; message?: string; details?: unknown }
    }
    if (payload.error?.code) code = payload.error.code as ApiErrorCode
    if (payload.error?.message) message = payload.error.message
    details = payload.error?.details
  } catch {
    // A non-JSON body (a proxy error page, say) keeps the generic message above. The
    // status is still reported, which is the part a user can act on.
  }
  return new ApiError(response.status, code, message, details)
}

export interface ApiResponse<T> {
  data: T
  /** The `ETag` the server returned. Required for the next conditional write. */
  etag: string | null
}

let isRefreshing = false
let refreshPromise: Promise<TokenPairResponse | null> | null = null

/** Called by the auth store when login/refresh succeeds. */
export function onTokenPairReceived(pair: TokenPairResponse): void {
  accessToken = pair.access_token
  accessExpiresAt = pair.access_expires_at
  scheduleRefresh(pair.access_expires_at)
}

export function clearTokens(): void {
  accessToken = null
  accessExpiresAt = null
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

/** Schedule a silent refresh ~60s before the access token expires. */
function scheduleRefresh(expiresAt: string): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  const expiry = new Date(expiresAt).getTime()
  const now = Date.now()
  const MAX_TIMEOUT = 2147483647 // max 32-bit signed int ~24.8 days
  const delay = Math.min(MAX_TIMEOUT, Math.max(0, expiry - now - 60_000)) // 60s before expiry
  refreshTimer = setTimeout(() => {
    doRefresh()
  }, delay)
}

/** Perform a silent refresh via the HttpOnly cookie. */
async function doRefresh(): Promise<TokenPairResponse | null> {
  if (isRefreshing) return refreshPromise
  isRefreshing = true
  refreshPromise = (async () => {
    try {
      const url = buildUrl('/api/v1/auth/refresh', {})
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'include', // Critical: sends the HttpOnly cookie
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        // Refresh failed (token expired, revoked, or reused) — clear tokens
        clearTokens()
        return null
      }
      const pair = (await response.json()) as TokenPairResponse
      accessToken = pair.access_token
      accessExpiresAt = pair.access_expires_at
      scheduleRefresh(pair.access_expires_at)
      return pair
    } catch {
      clearTokens()
      return null
    } finally {
      isRefreshing = false
      refreshPromise = null
    }
  })()
  return refreshPromise
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  // Don't attach Authorization for the refresh endpoint — it uses the cookie
  const isRefresh = path === '/api/v1/auth/refresh'
  if (accessToken && !isRefresh) headers.Authorization = `Bearer ${accessToken}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.params), {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      credentials: 'include', // Required for Set-Cookie on login and cookie on refresh
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError(
      0,
      'network_error',
      'The server could not be reached. Check that the API is running and try again.',
    )
  }

  if (!response.ok) {
    // If we got a 401 and haven't tried refreshing yet, attempt one silent refresh
    // and retry the original request exactly once.
    if (response.status === 401 && !isRefresh && accessToken) {
      const refreshed = await doRefresh()
      if (refreshed) {
        // Retry the original request with the new access token
        const retryHeaders: Record<string, string> = { Accept: 'application/json', ...options.headers }
        if (options.body !== undefined) retryHeaders['Content-Type'] = 'application/json'
        retryHeaders.Authorization = `Bearer ${accessToken}`
        const retryResponse = await fetch(buildUrl(path, options.params), {
          method: options.method ?? 'GET',
          headers: retryHeaders,
          body: options.body === undefined ? undefined : JSON.stringify(options.body),
          signal: options.signal,
        })
        if (!retryResponse.ok) throw await readError(retryResponse)
        if (retryResponse.status === 204) return { data: undefined as T, etag: null }
        const data = (await retryResponse.json()) as T
        return { data, etag: retryResponse.headers.get('ETag') }
      }
    }
    throw await readError(response)
  }

  if (response.status === 204) return { data: undefined as T, etag: null }
  const data = (await response.json()) as T
  return { data, etag: response.headers.get('ETag') }
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //

export const api = {
  async login(email: string, password: string): Promise<TokenPairResponse> {
    const { data } = await request<TokenPairResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: { email, password },
    })
    onTokenPairReceived(data)
    return data
  },

  async logout(): Promise<void> {
    await request<void>('/api/v1/auth/logout', { method: 'POST' })
    clearTokens()
  },

  async me(signal?: AbortSignal): Promise<MeResponse> {
    const { data } = await request<MeResponse>('/api/v1/auth/me', { signal })
    return data
  },

  async teams(signal?: AbortSignal): Promise<TeamSummary[]> {
    const { data } = await request<TeamSummary[]>('/api/v1/teams', { signal })
    return data
  },

  async members(teamId: string, signal?: AbortSignal): Promise<MemberSummary[]> {
    const { data } = await request<MemberSummary[]>(`/api/v1/teams/${teamId}/members`, { signal })
    return data
  },

  async items(
    teamId: string,
    filters: ItemFilters,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Page<WorkItemSummary>> {
    const { data } = await request<Page<WorkItemSummary>>(`/api/v1/teams/${teamId}/items`, {
      params: {
        status: filters.statuses,
        priority: filters.priorities,
        type: filters.types,
        assignee: filters.assignee,
        q: filters.search,
        cursor,
        limit,
      },
      signal,
    })
    return data
  },

  async item(itemId: string, signal?: AbortSignal): Promise<ApiResponse<WorkItemDetail>> {
    return await request<WorkItemDetail>(`/api/v1/items/${itemId}`, { signal })
  },

  async createItem(
    teamId: string,
    input: {
      title: string
      description: string
      type: WorkItemType
      priority: Priority
      assignee_id: string | null
    },
    idempotencyKey: string,
  ): Promise<WorkItemDetail> {
    const { data } = await request<WorkItemDetail>(`/api/v1/teams/${teamId}/items`, {
      method: 'POST',
      body: input,
      headers: { 'Idempotency-Key': idempotencyKey },
    })
    return data
  },

  async updateItem(
    itemId: string,
    patch: Record<string, unknown>,
    etag: string,
  ): Promise<ApiResponse<WorkItemDetail>> {
    return await request<WorkItemDetail>(`/api/v1/items/${itemId}`, {
      method: 'PATCH',
      body: patch,
      headers: { 'If-Match': etag },
    })
  },

  async claimItem(itemId: string): Promise<ApiResponse<WorkItemDetail>> {
    // The claim bumps the version, so the response's `ETag` is the version the *next*
    // conditional write must use. Returning only the body would leave the page holding
    // the pre-claim version and make the following transition fail with `412`.
    return await request<WorkItemDetail>(`/api/v1/items/${itemId}/claim`, {
      method: 'POST',
    })
  },

  /**
   * Work assigned to the caller, across every team they belong to.
   *
   * No `assignee` parameter: the server derives the scope from the caller's memberships,
   * and sending one would turn this into a general cross-team list.
   */
  async myItems(
    filters: ItemFilters,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Page<WorkItemSummary>> {
    const { data } = await request<Page<WorkItemSummary>>('/api/v1/me/items', {
      params: {
        status: filters.statuses,
        priority: filters.priorities,
        type: filters.types,
        q: filters.search,
        cursor,
        limit,
      },
      signal,
    })
    return data
  },

  async notifications(
    cursor: string | null,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Page<Notification>> {
    const { data } = await request<Page<Notification>>('/api/v1/notifications', {
      params: { cursor, limit },
      signal,
    })
    return data
  },

  async unreadCount(signal?: AbortSignal): Promise<UnreadCount> {
    const { data } = await request<UnreadCount>('/api/v1/notifications/unread-count', { signal })
    return data
  },

  async markNotificationRead(notificationId: string): Promise<void> {
    await request<void>(`/api/v1/notifications/${notificationId}/read`, { method: 'POST' })
  },

  async markAllNotificationsRead(): Promise<MarkAllReadResult> {
    const { data } = await request<MarkAllReadResult>('/api/v1/notifications/read-all', {
      method: 'POST',
    })
    return data
  },

  async transitionItem(
    itemId: string,
    to: Status,
    note: string | null,
    etag: string,
  ): Promise<ApiResponse<WorkItemDetail>> {
    return await request<WorkItemDetail>(`/api/v1/items/${itemId}/transitions`, {
      method: 'POST',
      body: note === null ? { to } : { to, note },
      headers: { 'If-Match': etag },
    })
  },

  async comments(
    itemId: string,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Page<Comment>> {
    const { data } = await request<Page<Comment>>(`/api/v1/items/${itemId}/comments`, {
      params: { cursor, limit },
      signal,
    })
    return data
  },

  async addComment(itemId: string, body: string, idempotencyKey: string): Promise<Comment> {
    const { data } = await request<Comment>(`/api/v1/items/${itemId}/comments`, {
      method: 'POST',
      body: { body },
      headers: { 'Idempotency-Key': idempotencyKey },
    })
    return data
  },

  async itemActivity(
    itemId: string,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Page<ActivityEvent>> {
    const { data } = await request<Page<ActivityEvent>>(`/api/v1/items/${itemId}/activity`, {
      params: { cursor, limit },
      signal,
    })
    return data
  },

  async teamActivity(
    teamId: string,
    cursor: string | null,
    limit: number,
    filters: ActivityFilters = {},
    signal?: AbortSignal,
  ): Promise<Page<ActivityEvent>> {
    const { data } = await request<Page<ActivityEvent>>(`/api/v1/teams/${teamId}/activity`, {
      params: {
        cursor,
        limit,
        since: filters.since,
        until: filters.until,
        actor: filters.actor,
      },
      signal,
    })
    return data
  },
}
