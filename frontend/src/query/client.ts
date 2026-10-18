/**
 * The TanStack Query client, with the retry policy written down.
 *
 * Two decisions are worth stating, because both are about correctness rather than taste.
 *
 * **A `401` ends the session, wherever it comes from.** Handled once, in the cache's
 * `onError`, so an expired token returns the user to the sign-in screen instead of
 * leaving a page showing a confusing error it can never recover from.
 *
 * **Retrying a mutation is only safe when the request carries an `Idempotency-Key`.**
 * A retried create with the same key returns the first result; a retried transition
 * would come back `412` because the first attempt already moved the version. So the
 * default is no retry, and the two mutations that are genuinely retry-safe opt in. The
 * key is part of the mutation's *variables*, not generated inside the request function,
 * which is what makes a retry reuse it.
 */

import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'

import { ApiError } from '../api/client'
import type { WorkItemDetail } from '../api/types'
import { sessionStore } from '../auth/session'

/** 4xx responses will not improve on their own; only transport and server faults retry. */
function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.isAuthenticationFailure) return false
    if (error.status >= 400 && error.status < 500) return false
  }
  return failureCount < 2
}

function handleError(error: unknown): void {
  if (error instanceof ApiError && error.isAuthenticationFailure) {
    sessionStore.clear()
  }
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({ onError: handleError }),
    mutationCache: new MutationCache({ onError: handleError }),
    defaultOptions: {
      queries: {
        // Server state is authoritative, so a short staleness window is enough to avoid
        // refetching on every navigation without ever showing data the server has moved
        // past. Mutations invalidate explicitly, which is the part that matters.
        staleTime: 15_000,
        retry: shouldRetryQuery,
        refetchOnWindowFocus: false,
      },
      mutations: {
        // Not retry-safe in general: see the note above. `CreateItemForm` and
        // `CommentThread` override this for the two operations that are.
        retry: 0,
      },
    },
  })
}

/**
 * Query keys, in one place so invalidation cannot miss a cache entry.
 *
 * The filters are part of the item list's key, which is what makes changing a filter a
 * new server query rather than a client-side filter over a page that happens to be
 * loaded.
 */
export const keys = {
  me: () => ['me'] as const,
  teams: () => ['teams'] as const,
  members: (teamId: string) => ['teams', teamId, 'members'] as const,
  items: (teamId: string, filters: unknown) => ['teams', teamId, 'items', filters] as const,
  myItems: (filters: unknown) => ['me', 'items', filters] as const,
  item: (itemId: string) => ['items', itemId] as const,
  comments: (itemId: string) => ['items', itemId, 'comments'] as const,
  itemActivity: (itemId: string) => ['items', itemId, 'activity'] as const,
  teamActivity: (teamId: string, filters?: unknown) =>
    filters === undefined
      ? (['teams', teamId, 'activity'] as const)
      : (['teams', teamId, 'activity', filters] as const),
  notifications: () => ['notifications'] as const,
  notificationsUnread: () => ['notifications', 'unread'] as const,
}

/**
 * The item cache entry: the item and the `ETag` that conditions the next write.
 *
 * They are stored together because they are the same fact — the `ETag` is the version of
 * *this* item, and keeping them apart is how a page ends up sending yesterday's version
 * with today's data.
 */
export interface ItemSnapshot {
  item: WorkItemDetail
  etag: string | null
}

/**
 * Store the server's own answer, rather than guessing it.
 *
 * A mutation's response *is* the new state, so writing it into the cache avoids a
 * redundant read and — more importantly — means the interface can never show a locally
 * invented result. The next conditional write then uses the `ETag` from that same
 * response, which is the version the server just produced.
 */
export function putItem(client: QueryClient, snapshot: ItemSnapshot): void {
  client.setQueryData(keys.item(snapshot.item.id), snapshot)
}

/** Re-read the item. Used when a write was refused and the local copy is known to be stale. */
export async function refreshItem(client: QueryClient, itemId: string): Promise<void> {
  await client.invalidateQueries({ queryKey: keys.item(itemId) })
}

/**
 * Re-read everything derived from the item.
 *
 * The timeline, the discussion and the team's lists all describe the item, so a write to
 * it makes all of them potentially wrong. Invalidating them together is what keeps the
 * views from disagreeing: the server has one answer and each view re-reads it.
 *
 * The item's own entry is deliberately *not* in this list — the mutation already supplied
 * it through `putItem`. A refused write calls `refreshItem` instead.
 */
export async function invalidateItemViews(
  client: QueryClient,
  itemId: string,
  teamId: string,
): Promise<void> {
  await Promise.all([
    client.invalidateQueries({ queryKey: keys.itemActivity(itemId) }),
    client.invalidateQueries({ queryKey: keys.comments(itemId) }),
    client.invalidateQueries({ queryKey: keys.teamActivity(teamId) }),
    client.invalidateQueries({ queryKey: ['teams', teamId, 'items'] }),
    // "My work" spans teams, so a change in one team can change it too.
    client.invalidateQueries({ queryKey: ['me', 'items'] }),
  ])
}

