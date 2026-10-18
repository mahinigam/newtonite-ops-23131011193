/**
 * The moves an item can make, and the claim button.
 *
 * Two different writes, two deliberately different strategies
 * ----------------------------------------------------------
 * A **claim is optimistic**. The row is shown as yours the instant you click, and if the
 * server refuses — because somebody else got there first — the change is rolled back and
 * the server's reason is shown. This is the case the optimistic pattern is actually for:
 * claiming is *supposed* to be fast, the likely failure is a lost race rather than a bug,
 * and the rollback is unambiguous because the server's answer replaces the guess. The
 * provisional state says so on screen, so nobody is told something the server has not
 * agreed to yet.
 *
 * A **transition is server-confirmed**. A status move changes what the item *is*, and an
 * optimistic `awaiting_approval` that came back `412` would briefly show a workflow state
 * that never existed, in a UI whose whole point is that approval is controlled. Waiting for
 * the response costs a round trip and buys certainty about the one thing that must not be
 * guessed.
 *
 * Rollback is tested, not assumed: `ItemDetailPage.test.tsx` covers a lost claim race
 * (`409`) and a dropped connection, asserting in both that the optimistic owner disappears
 * and the real state is shown.
 *
 * The transitions offered are `available_transitions` from the item itself, so the buttons
 * and the server's rules cannot disagree.
 *
 * The `ETag` is the item's version in HTTP form, and it comes from the response *header*,
 * not the body — which is why it is threaded through as its own value rather than read off
 * the item. Every conditional write sends it back as `If-Match`. The claim response carries
 * a new one (claiming bumps the version), so the accepted `ETag` is the one the claim
 * returned rather than the one the page loaded with.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../../api/client'
import type { Membership, Status, WorkItemDetail } from '../../api/types'
import { useSession } from '../../auth/session'
import { InlineError } from '../../components/states'
import { transitionLabel } from '../../lib/format'
import { can } from '../../lib/permissions'
import { invalidateItemViews, keys, putItem, refreshItem, type ItemSnapshot } from '../../query/client'

export function ItemActions({
  item,
  etag,
  team,
  onReload,
}: {
  item: WorkItemDetail
  etag: string | null
  team: Membership
  onReload: () => void
}) {
  const queryClient = useQueryClient()
  const session = useSession()
  const [note, setNote] = useState('')
  const [error, setError] = useState<unknown>(null)

  /**
   * Accept the server's answer, then re-read what depends on it.
   *
   * The write's response is the new state of the item, so it goes straight into the cache;
   * the timeline, the discussion and the team's lists are re-read because they describe the
   * same item.
   */
  const accept = async (updated: WorkItemDetail, updatedEtag: string | null) => {
    putItem(queryClient, { item: updated, etag: updatedEtag })
    await invalidateItemViews(queryClient, updated.id, team.team_id)
  }

  /** A refused write leaves the local copy known-stale, so re-read it before the user retries. */
  const reject = async (cause: unknown) => {
    setError(cause)
    await refreshItem(queryClient, item.id)
    await invalidateItemViews(queryClient, item.id, team.team_id)
  }

  const claim = useMutation({
    mutationFn: () => api.claimItem(item.id),
    /**
     * Show the claim immediately, but remember exactly what to put back.
     *
     * `cancelQueries` first, so an in-flight refetch cannot land after the optimistic write
     * and overwrite it with the pre-claim row — the classic optimistic-update race.
     */
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: keys.item(item.id) })
      const previous = queryClient.getQueryData<ItemSnapshot>(keys.item(item.id)) ?? null
      if (previous && session) {
        queryClient.setQueryData<ItemSnapshot>(keys.item(item.id), {
          ...previous,
          item: { ...previous.item, assignee: session.user },
        })
      }
      return { previous }
    },
    /**
     * Put the row back, then ask the server what is actually true.
     *
     * The snapshot is the safety net for the case where the re-read also fails; the
     * re-read is what makes the page correct rather than merely unchanged.
     */
    onError: async (cause, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(keys.item(item.id), context.previous)
      await reject(cause)
    },
    onSuccess: (response) => accept(response.data, response.etag),
  })

  const transition = useMutation({
    mutationFn: (variables: { to: Status; note: string | null }) => {
      if (etag === null) throw new Error('This item has no version to condition on.')
      return api.transitionItem(item.id, variables.to, variables.note, etag)
    },
    onSuccess: (response) => accept(response.data, response.etag),
    onError: reject,
  })

  const busy = claim.isPending || transition.isPending
  const canClaim = can(team.role, 'claim_item') && item.assignee === null
  const canMove = can(team.role, 'transition_work') && etag !== null
  const awaitingApproval = item.status === 'awaiting_approval'
  const noMoves = !canClaim && (!canMove || item.available_transitions.length === 0)

  return (
    <section className="card actions" aria-label="Item actions">
      <h2 className="card__title">Actions</h2>

      {error ? (
        <InlineError error={error} onReload={onReload} onDismiss={() => setError(null)} />
      ) : null}

      {claim.isPending ? (
        <p className="notice" role="status">
          Claiming… this is shown as yours until the server confirms it.
        </p>
      ) : null}

      {claim.isSuccess ? (
        <p className="notice notice--ok">
          The server confirmed the claim
          {claim.data.data.assignee ? ` — now owned by ${claim.data.data.assignee.display_name}` : ''}.
        </p>
      ) : null}

      <div className="actions__row">
        {canClaim ? (
          <button
            type="button"
            className="button button--primary"
            disabled={busy}
            onClick={() => {
              setError(null)
              claim.mutate()
            }}
          >
            {claim.isPending ? 'Claiming…' : 'Claim this item'}
          </button>
        ) : null}

        {canMove
          ? item.available_transitions.map((target) => (
              <button
                key={target}
                type="button"
                className="button"
                disabled={busy}
                onClick={() => {
                  setError(null)
                  transition.mutate({ to: target, note: note.trim() === '' ? null : note.trim() })
                }}
              >
                {transitionLabel(target)}
              </button>
            ))
          : null}

        {noMoves ? (
          <p className="actions__none">
            {item.status === 'closed'
              ? 'This item is closed. There is nothing further to do.'
              : item.assignee === null
                ? 'Nobody has claimed this item yet.'
                : 'No moves are available to you on this item right now.'}
          </p>
        ) : null}
      </div>

      {awaitingApproval && canMove ? (
        <label className="field">
          <span className="field__label">
            Note (optional — a rejection records why the work is coming back)
          </span>
          <input
            name="note"
            maxLength={2000}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
      ) : null}

      <p className="actions__hint">
        Moves between statuses are conditional writes: each carries the version this page was
        loaded with, and the server refuses it if somebody else has moved the item since.
      </p>
    </section>
  )
}
