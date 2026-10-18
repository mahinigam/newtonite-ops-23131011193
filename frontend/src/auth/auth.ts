/**
 * Sign in and sign out.
 *
 * `signIn` deliberately makes **two** calls: `login` returns the token pair and the user, and
 * `me` returns the memberships. The roles are read from the server after the session
 * exists rather than being inferred, because every role check in the interface is a
 * *rendering* decision and the server's answer is the one worth rendering.
 */

import { api } from '../api/client'
import { sessionStore } from './session'

export async function signIn(email: string, password: string): Promise<void> {
  const pair = await api.login(email, password)
  // The access token is installed by api.login via onTokenPairReceived.
  // Now install the session with memberships.
  sessionStore.start({
    user: pair.user,
    memberships: [],
    accessExpiresAt: pair.access_expires_at,
  })
  try {
    const me = await api.me()
    sessionStore.start({
      user: me.user,
      memberships: me.memberships,
      accessExpiresAt: pair.access_expires_at,
    })
  } catch (error) {
    // A token that cannot read its own memberships is not usable; do not leave it behind.
    sessionStore.clear()
    throw error
  }
}

export async function signOut(): Promise<void> {
  try {
    await api.logout()
  } catch {
    // Signing out locally must succeed even if the server cannot be reached or the
    // session had already expired. The token is discarded either way; the server-side
    // row is revoked on the next successful call, and it expires on its own regardless.
  } finally {
    sessionStore.clear()
  }
}
