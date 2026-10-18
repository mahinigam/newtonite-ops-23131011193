/**
 * The caller's notifications.
 *
 * Every row is something a worker delivered *after* the change that caused it, which is why
 * this page can be a few seconds behind the item it describes — that delay is the visible
 * cost of not making an assignment wait on a notification. The page says so rather than
 * pretending the list is a live feed.
 *
 * Acknowledgement is explicit. Marking a notification read is a write, and a row that is
 * only *looked at* is not the same as one the person has dealt with; auto-marking on render
 * would make the unread count meaningless.
 */

import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

import { api } from '../../api/client'
import type { Membership, Notification } from '../../api/types'
import { useSession } from '../../auth/session'
import { Empty, ErrorState, Loading } from '../../components/states'
import { explainError } from '../../lib/errors'
import { formatRelative } from '../../lib/format'
import { keys } from '../../query/client'
import { useUnreadCount } from './useUnreadCount'

const PAGE_SIZE = 25

/** A stable empty list, so the memo below does not re-key on every render. */
const NO_MEMBERSHIPS: Membership[] = []

const EVENT_LABELS: Record<Notification['event_type'], string> = {
  work_item_assigned: 'Assigned to you',
  work_item_approved: 'Approved',
  work_item_rejected: 'Sent back',
}

export function NotificationsPage() {
  const queryClient = useQueryClient()
  const session = useSession()
  const memberships = session?.memberships ?? NO_MEMBERSHIPS
  const teams = useMemo(
    () => new Map(memberships.map((membership) => [membership.team_id, membership])),
    [memberships],
  )
  const unread = useUnreadCount()

  const query = useInfiniteQuery({
    queryKey: keys.notifications(),
    queryFn: ({ pageParam, signal }) => api.notifications(pageParam, PAGE_SIZE, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: keys.notifications() }),
      queryClient.invalidateQueries({ queryKey: keys.notificationsUnread() }),
    ])
  }

  const markRead = useMutation({
    mutationFn: (notificationId: string) => api.markNotificationRead(notificationId),
    onSuccess: refresh,
  })

  const markAll = useMutation({
    mutationFn: () => api.markAllNotificationsRead(),
    onSuccess: refresh,
  })

  const notifications = query.data?.pages.flatMap((page) => page.items) ?? []
  const acknowledgementError = markRead.error ?? markAll.error

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2 className="page__title">Notifications</h2>
          <p className="page__subtitle">
            {unread === 0
              ? 'Nothing unread. Notifications arrive after the change that caused them.'
              : `${unread} unread. Delivered by the outbox worker, so they can lag a change by a moment.`}
          </p>
        </div>
        <button
          type="button"
          className="button button--quiet"
          disabled={unread === 0 || markAll.isPending}
          onClick={() => markAll.mutate()}
        >
          {markAll.isPending ? 'Marking…' : 'Mark all as read'}
        </button>
      </header>

      {/* A failed acknowledgement is reported next to the list, not swallowed: the row is
          still unread, and the person needs to know the write did not land. */}
      {acknowledgementError ? (
        <div className="notice notice--error" role="alert">
          <div>
            <strong>{explainError(acknowledgementError).title}.</strong>{' '}
            {explainError(acknowledgementError).detail}
          </div>
          <div className="notice__actions">
            <button
              type="button"
              className="button button--small button--quiet"
              onClick={() => {
                markRead.reset()
                markAll.reset()
              }}
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}

      {query.isPending ? <Loading label="Loading notifications…" /> : null}

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isSuccess && notifications.length === 0 ? (
        <Empty
          title="No notifications"
          detail="You are told here when somebody assigns you work, approves it, or sends it back."
        />
      ) : null}

      {notifications.length > 0 ? (
        <>
          <ul className="notification-list">
            {notifications.map((notification) => {
              const team = teams.get(notification.team_id)
              const unreadRow = notification.read_at === null
              return (
                <li
                  key={notification.id}
                  className={`notification${unreadRow ? ' notification--unread' : ''}`}
                >
                  <div className="notification__main">
                    <span className="notification__kind">
                      {EVENT_LABELS[notification.event_type]}
                    </span>
                    <p className="notification__body">{notification.body}</p>
                    <span className="notification__meta">
                      {team ? team.team_name : 'A team you have left'} ·{' '}
                      {formatRelative(notification.created_at)}
                      {unreadRow ? <span className="notification__dot" aria-label="Unread" /> : null}
                    </span>
                  </div>
                  <div className="notification__actions">
                    {team ? (
                      <a
                        className="button button--quiet button--small"
                        href={`#/teams/${team.team_key}/items/${notification.work_item_id}`}
                      >
                        Open item
                      </a>
                    ) : null}
                    {unreadRow ? (
                      <button
                        type="button"
                        className="button button--quiet button--small"
                        disabled={markRead.isPending}
                        onClick={() => markRead.mutate(notification.id)}
                      >
                        Mark as read
                      </button>
                    ) : null}
                  </div>
                </li>
              )
            })}
          </ul>

          <div className="list-footer">
            {query.hasNextPage ? (
              <button
                type="button"
                className="button"
                disabled={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
              >
                {query.isFetchingNextPage ? 'Loading…' : 'Load more'}
              </button>
            ) : (
              <span className="list-footer__end">End of the list</span>
            )}
          </div>
        </>
      ) : null}
    </section>
  )
}
