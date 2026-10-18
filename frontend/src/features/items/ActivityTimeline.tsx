/**
 * The history, as a list of events.
 *
 * One component serves both views — an item's timeline and a team's feed — because they
 * are the same rows read through two filters. The caller supplies the query, so the two
 * views keep their own cursors: a timeline is not a page of a feed, and the pagination
 * position of one must not be used for the other.
 *
 * What differs is the phrasing. An item's page already says which item it is, so a row
 * reads "Nina Petrova claimed this item". A team feed spans many items, so the caller
 * passes `itemHref` and the row names the item and links to it instead — otherwise the
 * feed is a wall of "this item" rows with no way to tell them apart.
 *
 * Events are read-only. There is no control on this component that writes anything,
 * because the API has no route that writes an event — history is produced by the
 * operations it describes.
 *
 * `meta` is rendering help and is treated as optional throughout: the event's meaning comes
 * from `event_type`, `from_value` and `to_value`, which are the stored facts. If the server
 * stopped sending `meta`, this view would still be correct, just terser.
 */

import type { InfiniteData, UseInfiniteQueryResult } from '@tanstack/react-query'

import type { ActivityEvent, Page } from '../../api/types'
import { Empty, ErrorState, Loading } from '../../components/states'
import { explainError } from '../../lib/errors'
import { activityAction, activityLabel, formatDateTime, formatRelative } from '../../lib/format'

/** `meta` values are `unknown` on the wire; read them defensively. */
function metaString(event: ActivityEvent, key: string): string | null {
  const value = event.meta[key]
  return typeof value === 'string' ? value : null
}

/** A user id in `to_value` is meaningless to a reader, so `meta` may name the person. */
function describe(event: ActivityEvent): string | null {
  switch (event.event_type) {
    case 'status_changed':
      return `${event.from_value ?? 'unknown'} → ${event.to_value ?? 'unknown'}`
    case 'priority_changed':
    case 'type_changed':
      return `${event.from_value ?? 'none'} → ${event.to_value ?? 'none'}`
    case 'due_date_changed':
      return event.to_value === null ? 'cleared' : `set to ${event.to_value}`
    case 'assigned':
      return metaString(event, 'assignee_name') ?? 'assigned'
    case 'unassigned':
      return metaString(event, 'assignee_name') ?? 'unassigned'
    case 'rejected':
      return metaString(event, 'note') ?? 'sent back'
    case 'commented':
      return metaString(event, 'preview') ?? 'comment'
    case 'title_changed':
    case 'description_changed':
      return event.to_value === null ? 'cleared' : null
    default:
      return null
  }
}

/** The linked title on a team feed, or the shortest thing that identifies the item. */
function itemText(event: ActivityEvent): string {
  return event.work_item_title
    ? `“${event.work_item_title}”`
    : `item ${event.work_item_id.slice(0, 8)}`
}

/**
 * What the rail colours mean.
 *
 * The label on each row already says what happened, so this is a reading aid rather than
 * the only signal — which is why the swatches themselves are hidden from assistive tech
 * and the words are not.
 */
function Legend() {
  return (
    <ul className="activity__legend" aria-label="Colour key">
      <li>
        <span className="activity__swatch activity__swatch--ok" aria-hidden="true" />
        Approved
      </li>
      <li>
        <span className="activity__swatch activity__swatch--warn" aria-hidden="true" />
        Sent back
      </li>
      <li>
        <span className="activity__swatch activity__swatch--info" aria-hidden="true" />
        Status change
      </li>
    </ul>
  )
}

export function ActivityList({
  query,
  emptyTitle,
  emptyDetail,
  itemHref,
}: {
  query: UseInfiniteQueryResult<InfiniteData<Page<ActivityEvent>>, Error>
  emptyTitle: string
  emptyDetail: string
  /** Present on a feed that spans items: where the row's item link goes. */
  itemHref?: (event: ActivityEvent) => string
}) {
  const events = query.data?.pages.flatMap((page) => page.items) ?? []
  // A failed *next* page is not a failed view: the events already read are still good, so
  // it gets an inline retry instead of replacing the list. Retrying it with
  // `fetchNextPage` also re-requests one page rather than every page loaded so far.
  const firstPageFailed = query.isError && !query.isFetchNextPageError
  const nextPageFailed = query.isFetchNextPageError

  return (
    <div className="activity">
      {query.isPending ? <Loading label="Loading history…" /> : null}
      {firstPageFailed ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {query.isSuccess && events.length === 0 ? (
        <Empty title={emptyTitle} detail={emptyDetail} />
      ) : null}

      {events.length > 0 ? <Legend /> : null}

      {events.length > 0 ? (
        <ol className="activity__list">
          {events.map((event) => {
            const detail = describe(event)
            return (
              <li key={event.id} className={`activity__item activity__item--${event.event_type}`}>
                <div className="activity__head">
                  <span className="activity__actor">{event.actor.display_name}</span>{' '}
                  {itemHref ? (
                    <>
                      <span className="activity__what">{activityAction(event.event_type)}</span>{' '}
                      <a className="activity__item-link" href={itemHref(event)}>
                        {itemText(event)}
                      </a>
                    </>
                  ) : (
                    <span className="activity__what">{activityLabel(event.event_type)}</span>
                  )}
                </div>
                {detail ? <p className="activity__detail">{detail}</p> : null}
                <time className="activity__time" dateTime={event.created_at}>
                  {formatRelative(event.created_at)} · {formatDateTime(event.created_at)}
                </time>
              </li>
            )
          })}
        </ol>
      ) : null}

      {nextPageFailed ? (
        <div className="state state--error" role="alert">
          <p className="state__title">Could not load older events</p>
          <p className="state__detail">{explainError(query.error).detail}</p>
          <button
            type="button"
            className="button"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            Try again
          </button>
        </div>
      ) : null}

      {query.hasNextPage && !nextPageFailed ? (
        <button
          type="button"
          className="button button--quiet"
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {query.isFetchingNextPage ? 'Loading…' : 'Show older events'}
        </button>
      ) : null}
    </div>
  )
}
