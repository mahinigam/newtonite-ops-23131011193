/**
 * A team's whole activity feed.
 *
 * The same rows as an item's timeline, read through a different filter and paged with its
 * own cursor. It is here because it is the view that makes the history feel like a record
 * of the team's work rather than a property of one item — and because keeping it separate
 * from the item list is what "activity paginates independently" means to a reader.
 *
 * A feed spanning every item would need hundreds of "show older" clicks to reach last
 * week, so the dates are a *server* filter: changing them is a new query key and therefore
 * a new first page, not a filter applied to the events already loaded.
 */

import { useInfiniteQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import type { ActivityFilters, Membership } from '../../api/types'
import { endOfLocalDay, startOfLocalDay } from '../../lib/format'
import { pathFor } from '../../lib/router'
import { keys } from '../../query/client'
import { ActivityList } from '../items/ActivityTimeline'

export function TeamActivityPage({ team }: { team: Membership }) {
  // `YYYY-MM-DD` from the date inputs, or '' for no bound. The conversion to an instant
  // happens here, in the browser's own time zone, because "17 October" is a local day.
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const filters = useMemo<ActivityFilters>(
    () => ({ since: startOfLocalDay(from), until: endOfLocalDay(to) }),
    [from, to],
  )
  const ranged = filters.since != null || filters.until != null

  const query = useInfiniteQuery({
    queryKey: keys.teamActivity(team.team_id, filters),
    queryFn: ({ pageParam, signal }) =>
      api.teamActivity(team.team_id, pageParam, 25, filters, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2 className="page__title">{team.team_name} activity</h2>
          <p className="page__subtitle">
            Everything that has happened in this team, newest first. Read-only.
          </p>
        </div>
      </header>

      <section className="card">
        <div className="activity-range" role="group" aria-label="Date range">
          <label className="field">
            <span className="field__label">From</span>
            <input
              type="date"
              value={from}
              max={to || undefined}
              onChange={(event) => setFrom(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">To</span>
            <input
              type="date"
              value={to}
              min={from || undefined}
              onChange={(event) => setTo(event.target.value)}
            />
          </label>
          {ranged ? (
            <button
              type="button"
              className="button button--quiet"
              onClick={() => {
                setFrom('')
                setTo('')
              }}
            >
              Clear range
            </button>
          ) : null}
        </div>

        <ActivityList
          query={query}
          itemHref={(event) =>
            pathFor({ name: 'item', teamKey: team.team_key, itemId: event.work_item_id })
          }
          emptyTitle={ranged ? 'No events in that range' : 'Nothing has happened yet'}
          emptyDetail={
            ranged
              ? 'Widen the dates, or clear the range to see the whole history.'
              : 'Events appear here as work items are created and moved.'
          }
        />
      </section>
    </section>
  )
}
