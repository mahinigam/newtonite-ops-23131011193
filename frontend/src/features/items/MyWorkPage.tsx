/**
 * "My work": everything assigned to the caller, across every team.
 *
 * The operational question a person actually asks is *"what is on my plate?"*, not *"what
 * is on my plate in this one team?"*. Answering it from the browser would mean one request
 * per membership and a merge of results that were each sorted separately; the API instead
 * answers it in one keyset-paged query, scoped to the caller's own memberships.
 *
 * There is no assignee control here. The scope *is* "assigned to me" — that is the
 * endpoint's definition, and the server derives it from the token rather than from a
 * parameter.
 */

import { useInfiniteQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import type { ItemFilters, Membership } from '../../api/types'
import { useSession } from '../../auth/session'
import { Empty, ErrorState, Loading } from '../../components/states'
import { useDebouncedValue } from '../../lib/useDebouncedValue'
import { keys } from '../../query/client'
import {
  EMPTY_FILTERS,
  ItemFiltersBar,
  MY_WORK_PRESETS,
  PresetBar,
  filterKey,
  isFiltered,
} from './ItemFilters'
import { ItemList } from './ItemList'

const PAGE_SIZE = 25

/** A stable empty list, so the memo below does not re-key on every render. */
const NO_MEMBERSHIPS: Membership[] = []

export function MyWorkPage() {
  const session = useSession()
  const memberships = session?.memberships ?? NO_MEMBERSHIPS
  const teams = useMemo(
    () => new Map(memberships.map((membership) => [membership.team_id, membership])),
    [memberships],
  )

  const [filters, setFilters] = useState<ItemFilters>(EMPTY_FILTERS)
  const [searchInput, setSearchInput] = useState('')
  const settledSearch = useDebouncedValue(searchInput, 250)
  const applied = useMemo<ItemFilters>(
    () => ({ ...filters, search: settledSearch }),
    [filters, settledSearch],
  )

  const query = useInfiniteQuery({
    queryKey: keys.myItems(filterKey(applied)),
    queryFn: ({ pageParam, signal }) => api.myItems(applied, pageParam, PAGE_SIZE, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  const items = query.data?.pages.flatMap((page) => page.items) ?? []
  const loadedCount = query.data ? items.length : null

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2 className="page__title">My work</h2>
          <p className="page__subtitle">
            Assigned to you across {memberships.length === 1 ? 'your team' : `${memberships.length} teams`},
            filtered and paged by the server.
          </p>
        </div>
      </header>

      <PresetBar presets={MY_WORK_PRESETS} filters={filters} onSelect={setFilters} />

      <ItemFiltersBar
        filters={filters}
        onChange={setFilters}
        searchInput={searchInput}
        onSearchInput={setSearchInput}
        resultCount={loadedCount}
        showAssignee={false}
        searchPlaceholder="Your work by title and description…"
      />

      {query.isPending ? <Loading label="Loading your work…" /> : null}

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isSuccess && items.length === 0 ? (
        <Empty
          title={isFiltered(applied) ? 'Nothing matches those filters' : 'Nothing is assigned to you'}
          detail={
            isFiltered(applied)
              ? 'Try clearing a filter or searching for something broader.'
              : 'When somebody assigns you work, or you claim an item, it appears here.'
          }
        />
      ) : null}

      {items.length > 0 ? (
        <>
          <ItemList items={items} teams={teams} showTeam />
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
            {query.isFetchingNextPage ? <span className="visually-hidden">Loading more</span> : null}
            {query.isFetchNextPageError ? (
              <ErrorState
                error={query.error}
                onRetry={() => void query.fetchNextPage()}
                retryLabel="Retry"
              />
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  )
}
