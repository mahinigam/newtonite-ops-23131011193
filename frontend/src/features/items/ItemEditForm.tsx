/**
 * Editing an item's fields.
 *
 * The form is *only* fields — title, description, type, priority, due date, assignee.
 * Status is not here, and that is the point: the workflow is a state machine on the
 * server, and a generic patch endpoint that could set `status` would be a way around
 * approval and closure. The server refuses `status` in a patch outright, so this form does
 * not offer it.
 *
 * The assignee control is where the role boundary becomes visible. Moving work to or from
 * somebody else needs `assign_others`, which a member does not have; the server enforces
 * that, and the form renders the server's decision rather than deciding for itself.
 *
 * A successful save sends the new `ETag` back, so the next save is conditioned on the
 * version this form just produced.
 */

import { type FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { Membership, Priority, WorkItemDetail, WorkItemType } from '../../api/types'
import { ErrorState, InlineError } from '../../components/states'
import {
  PRIORITIES,
  TYPES,
  fromLocalInputValue,
  priorityLabel,
  toLocalInputValue,
  typeLabel,
} from '../../lib/format'
import { can } from '../../lib/permissions'
import { invalidateItemViews, keys, putItem, refreshItem } from '../../query/client'

interface Draft {
  title: string
  description: string
  type: WorkItemType
  priority: Priority
  dueAt: string
  assigneeId: string
}

function draftFrom(item: WorkItemDetail): Draft {
  return {
    title: item.title,
    description: item.description,
    type: item.type,
    priority: item.priority,
    dueAt: toLocalInputValue(item.due_at),
    assigneeId: item.assignee?.id ?? '',
  }
}

export function ItemEditForm({
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
  const [draft, setDraft] = useState<Draft>(() => draftFrom(item))
  const [seededFrom, setSeededFrom] = useState<WorkItemDetail>(item)
  const [error, setError] = useState<unknown>(null)
  const [saved, setSaved] = useState(false)

  /**
   * Re-seed the form when the server's item changes underneath it.
   *
   * This is the "adjust state when a prop changes" pattern rather than an effect: the
   * comparison happens during render, so there is no frame in which the form shows the old
   * values, and no second render pass triggered by an effect. Without it the form would
   * keep showing values the server has already moved past — the client-side version of the
   * bug this whole design avoids.
   */
  if (seededFrom !== item) {
    setSeededFrom(item)
    setDraft(draftFrom(item))
    setSaved(false)
  }

  const members = useQuery({
    queryKey: keys.members(team.team_id),
    queryFn: ({ signal }) => api.members(team.team_id, signal),
  })

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => {
      if (etag === null) throw new Error('This item has no version to condition on.')
      return api.updateItem(item.id, patch, etag)
    },
    onSuccess: async (response) => {
      setError(null)
      setSaved(true)
      // The response is the new state, and its `ETag` is the version the next write must
      // be conditioned on — so both go into the one cache entry that holds them.
      putItem(queryClient, { item: response.data, etag: response.etag })
      await invalidateItemViews(queryClient, item.id, team.team_id)
    },
    onError: async (cause) => {
      setSaved(false)
      setError(cause)
      // A `412` means the page is stale; re-read so the form re-seeds from the server's
      // current values rather than letting the user save the same losing patch again.
      await refreshItem(queryClient, item.id)
      await invalidateItemViews(queryClient, item.id, team.team_id)
    },
  })

  /**
   * Send only what changed.
   *
   * The API treats an absent field as "no opinion" and an explicit `null` as "clear it",
   * so sending the whole form would erase fields the user did not touch and would create
   * history events for changes that never happened.
   */
  function buildPatch(): Record<string, unknown> {
    const patch: Record<string, unknown> = {}
    if (draft.title !== item.title) patch.title = draft.title
    if (draft.description !== item.description) patch.description = draft.description
    if (draft.type !== item.type) patch.type = draft.type
    if (draft.priority !== item.priority) patch.priority = draft.priority
    if (draft.dueAt !== toLocalInputValue(item.due_at)) patch.due_at = fromLocalInputValue(draft.dueAt)
    if (draft.assigneeId !== (item.assignee?.id ?? '')) {
      patch.assignee_id = draft.assigneeId === '' ? null : draft.assigneeId
    }
    return patch
  }

  const patch = buildPatch()
  const dirty = Object.keys(patch).length > 0
  const canAssignOthers = can(team.role, 'assign_others')
  const assigneeLocked = !canAssignOthers && item.assignee != null && item.assignee.id !== draft.assigneeId

  // A viewer cannot edit anything, so the form is not shown at all. Rendering inputs that
  // the server will refuse would be offering a control that cannot work; the fields are
  // shown as text instead, because reading the description is still useful.
  if (!can(team.role, 'edit_item')) {
    return (
      <section className="card" aria-label="Details">
        <h2 className="card__title">Details</h2>
        <p className="card__hint">
          Your role in this team can read items but not change them.
        </p>
        <dl className="facts">
          <div>
            <dt>Type</dt>
            <dd>{typeLabel(item.type)}</dd>
          </div>
          <div>
            <dt>Priority</dt>
            <dd>{priorityLabel(item.priority)}</dd>
          </div>
          <div>
            <dt>Due</dt>
            <dd>{item.due_at ? toLocalInputValue(item.due_at).replace('T', ' ') : 'No due date'}</dd>
          </div>
        </dl>
        <div>
          <h3 className="card__title">Description</h3>
          <p className="card__body">{item.description || 'No description.'}</p>
        </div>
      </section>
    )
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSaved(false)
    save.mutate(patch)
  }

  return (
    <form className="card" onSubmit={submit} aria-label="Edit item">
      <h2 className="card__title">Details</h2>

      <label className="field">
        <span className="field__label">Title</span>
        <input
          name="title"
          required
          maxLength={200}
          value={draft.title}
          onChange={(event) => setDraft({ ...draft, title: event.target.value })}
        />
      </label>

      <label className="field">
        <span className="field__label">Description</span>
        <textarea
          name="description"
          rows={5}
          maxLength={20000}
          value={draft.description}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
        />
      </label>

      <div className="field-row">
        <label className="field">
          <span className="field__label">Type</span>
          <select
            value={draft.type}
            onChange={(event) => setDraft({ ...draft, type: event.target.value as WorkItemType })}
          >
            {TYPES.map((value) => (
              <option key={value} value={value}>
                {typeLabel(value)}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">Priority</span>
          <select
            value={draft.priority}
            onChange={(event) => setDraft({ ...draft, priority: event.target.value as Priority })}
          >
            {PRIORITIES.map((value) => (
              <option key={value} value={value}>
                {priorityLabel(value)}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">Due</span>
          <input
            type="datetime-local"
            name="due_at"
            value={draft.dueAt}
            onChange={(event) => setDraft({ ...draft, dueAt: event.target.value })}
          />
        </label>

        <label className="field">
          <span className="field__label">Assignee</span>
          <select
            value={draft.assigneeId}
            disabled={members.isPending || !can(team.role, 'claim_item')}
            onChange={(event) => setDraft({ ...draft, assigneeId: event.target.value })}
          >
            <option value="">Unassigned</option>
            {(members.data ?? []).map((member) => (
              <option key={member.user_id} value={member.user_id}>
                {member.display_name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {assigneeLocked ? (
        <p className="notice">
          Reassigning work that belongs to somebody else needs the lead role. You can take it
          yourself by clearing your own assignment, but not move it between other people.
        </p>
      ) : null}

      {error ? (
        <InlineError error={error} onReload={onReload} onDismiss={() => setError(null)} />
      ) : null}

      {save.isError ? <ErrorState error={save.error} /> : null}

      <div className="card__actions">
        <button type="submit" className="button button--primary" disabled={!dirty || save.isPending}>
          {save.isPending ? 'Saving…' : 'Save changes'}
        </button>
        {saved ? <span className="notice notice--ok">Saved.</span> : null}
        {!dirty ? <span className="card__hint">No changes yet.</span> : null}
      </div>
    </form>
  )
}
