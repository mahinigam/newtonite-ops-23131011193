/**
 * The session, held in memory only.
 *
 * A plain store rather than a React context, for two reasons: the HTTP client needs to
 * clear the session from outside React (any `401` ends it), and a store is directly
 * testable without rendering anything.
 *
 * The **access token** is held in memory (in `api/client.ts`). The **refresh token**
 * is an HttpOnly cookie managed by the browser — we never see it. Silent refresh is
 * handled by the HTTP client when a 401 is encountered.
 */

import { useSyncExternalStore } from 'react'

import { clearTokens } from '../api/client'
import type { Membership, UserRef } from '../api/types'

export interface Session {
  user: UserRef
  memberships: Membership[]
  accessExpiresAt: string
}

type Listener = () => void

let current: Session | null = null
const listeners = new Set<Listener>()

function emit(): void {
  for (const listener of listeners) listener()
}

export const sessionStore = {
  get(): Session | null {
    return current
  },

  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  },

  /** Adopt a token pair and the caller it belongs to. */
  start(pair: { user: UserRef; memberships: Membership[]; accessExpiresAt: string }): void {
    // The access token is already set by api.login() via onTokenPairReceived.
    // We only need to record the session metadata here.
    current = {
      user: pair.user,
      memberships: pair.memberships,
      accessExpiresAt: pair.accessExpiresAt,
    }
    emit()
  },

  /**
   * Forget the session. Called by sign-out and by any `401`, so an expired or revoked
   * token cannot leave the interface looking signed in.
   */
  clear(): void {
    clearTokens()
    current = null
    emit()
  },
}

export function useSession(): Session | null {
  return useSyncExternalStore(sessionStore.subscribe, sessionStore.get, sessionStore.get)
}

/** The caller's role in one team, or `null` when they are not a member of it. */
export function roleIn(session: Session | null, teamId: string): Membership['role'] | null {
  return session?.memberships.find((membership) => membership.team_id === teamId)?.role ?? null
}
