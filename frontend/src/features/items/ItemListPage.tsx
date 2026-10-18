/**
 * A team's work items.
 *
 * The list is an infinite query over the server's keyset pages: the first page arrives
 * with the view, and "Load more" asks for the next cursor. `hasNextPage` comes from the
 * server's `has_more`, so the button disappears exactly when there is nothing left rather
 * than after a request that returns an empty page.
 *
 * Filtering and search change the query key, which is what makes them a *new request*
 * rather than a client-side filter — the requirement that the browser never holds the
 * whole dataset is met by construction here, not by discipline.
 */

import { useInfiniteQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import type { ItemFilters, Membership } from '../../api/types'
import { Empty, ErrorState, Loading } from '../../components/states'
import { navigate } from '../../lib/router'
import { useDebouncedValue } from '../../lib/useDebouncedValue'
import { keys } from '../../query/client'
import { CreateItemForm } from './CreateItemForm'
import {
  EMPTY_FILTERS,
  ItemFiltersBar,
  PresetBar,
  TEAM_PRESETS,
  filterKey,
  isFiltered,
} from './ItemFilters'
import { ItemList } from './ItemList'

const PAGE_SIZE = 25

export function ItemListPage({ team }: { team: Membership }) {
  const [filters, setFilters] = useState<ItemFilters>(EMPTY_FILTERS)
  const [searchInput, setSearchInput] = useState('')
  const settledSearch = useDebouncedValue(searchInput, 250)

  // The debounced box is the source of truth for the request; `filters` carries the
  // checkboxes and the assignee select, which change on a click and need no delay.
  const applied = useMemo<ItemFilters>(
    () => ({ ...filters, search: settledSearch }),
    [filters, settledSearch],
  )

  const query = useInfiniteQuery({
    queryKey: keys.items(team.team_id, filterKey(applied)),
    queryFn: ({ pageParam, signal }) => api.items(team.team_id, applied, pageParam, PAGE_SIZE, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  })

  const items = query.data?.pages.flatMap((page) => page.items) ?? []
  const loadedCount = query.data ? items.length : null
  const teams = useMemo(() => new Map([[team.team_id, team]]), [team])

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2 className="page__title">{team.team_name}</h2>
          <p className="page__subtitle">
            Work items, newest first, filtered and paged by the server.
          </p>
        </div>
        <CreateItemForm
          teamId={team.team_id}
          role={team.role}
          onCreated={(itemId) => {
            navigate({ name: 'item', teamKey: team.team_key, itemId })
          }}
        />
      </header>

      <PresetBar presets={TEAM_PRESETS} filters={filters} onSelect={setFilters} />

      <ItemFiltersBar
        filters={filters}
        onChange={setFilters}
        searchInput={searchInput}
        onSearchInput={setSearchInput}
        resultCount={loadedCount}
      />

      {query.isPending ? <Loading label="Loading work items…" /> : null}

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isSuccess && items.length === 0 ? (
        <Empty
          title={isFiltered(applied) ? 'Nothing matches those filters' : 'No work items yet'}
          detail={
            isFiltered(applied)
              ? 'Try clearing a filter or searching for something broader.'
              : 'Create the first one to get started.'
          }
        />
      ) : null}

      {items.length > 0 ? (
        <>
          <ItemList items={items} teams={teams} />
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
