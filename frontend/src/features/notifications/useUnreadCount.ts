/**
 * The unread badge's data, in one place.
 *
 * Notifications are delivered **asynchronously** by the worker, so the browser cannot know
 * the moment one arrives — there is no push channel, and the brief rules one out. Polling a
 * count is the honest alternative: it is one indexed `COUNT` over unread rows, it needs no
 * connection held open, and a 20-second delay on a badge is not a correctness problem.
 *
 * The count is deliberately separate from the list. The badge is asked for on every page,
 * the list only on its own; sharing a query key would make every page fetch the first page
 * of notifications just to render a number.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import { keys } from '../../query/client'

export function useUnreadCount(): number {
  const query = useQuery({
    queryKey: keys.notificationsUnread(),
    queryFn: ({ signal }) => api.unreadCount(signal),
    refetchInterval: 20_000,
  })
  return query.data?.unread ?? 0
}
