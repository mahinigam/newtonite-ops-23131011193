/**
 * Filtering the list, on the server.
 *
 * Every control here changes the *request*. Nothing filters a loaded page, because the
 * browser never holds the whole dataset: a team's items are paged 25 at a time and the
 * filter is applied by PostgreSQL. That is the requirement, and it is also what makes
 * "show me every unassigned urgent item" correct rather than "show me the unassigned
 * urgent items that happen to be in the first page I already have".
 */

import type { ItemFilters, Priority, Status, WorkItemType } from '../../api/types'
import {
  PRIORITIES,
  STATUS_ORDER,
  TYPES,
  priorityLabel,
  statusLabel,
  typeLabel,
} from '../../lib/format'

export const EMPTY_FILTERS: ItemFilters = {
  statuses: [],
  priorities: [],
  types: [],
  assignee: null,
  search: '',
}

/** A stable identity for a filter set, so the query key changes only when it should. */
export function filterKey(filters: ItemFilters): string {
  return JSON.stringify({
    statuses: [...filters.statuses].sort(),
    priorities: [...filters.priorities].sort(),
    types: [...filters.types].sort(),
    assignee: filters.assignee,
    search: filters.search.trim(),
  })
}

export function isFiltered(filters: ItemFilters): boolean {
  return (
    filters.statuses.length > 0 ||
    filters.priorities.length > 0 ||
    filters.types.length > 0 ||
    filters.assignee !== null ||
    filters.search.trim() !== ''
  )
}

/**
 * The *structured* part of a filter set, ignoring the search box.
 *
 * A preset is a set of structured filters; the search box is the operator's own text and
 * is not part of a preset. Comparing only the structured part is what lets "Assigned to
 * me" stay highlighted while the user also types a search term — and what stops the
 * highlight from flickering off on every keystroke.
 */
function structuredKey(filters: ItemFilters): string {
  return JSON.stringify({
    statuses: [...filters.statuses].sort(),
    priorities: [...filters.priorities].sort(),
    types: [...filters.types].sort(),
    assignee: filters.assignee,
  })
}

export interface Preset {
  id: string
  label: string
  /** Spoken by a tooltip, and the reason the view exists. */
  description: string
  filters: ItemFilters
}

/**
 * The three "what needs attention?" queues the brief names, plus a way back to everything.
 *
 * Each one is a set of **server** filters, not a client-side pass over loaded rows, so
 * "unassigned urgent" means every unassigned urgent item in the team rather than the few
 * that happen to be in the page already fetched.
 */
export const TEAM_PRESETS: Preset[] = [
  {
    id: 'all',
    label: 'All work',
    description: 'Everything in this team, newest first.',
    filters: EMPTY_FILTERS,
  },
  {
    id: 'mine',
    label: 'Assigned to me',
    description: 'Work in this team that is assigned to you.',
    filters: { ...EMPTY_FILTERS, assignee: 'me' },
  },
  {
    id: 'unassigned-urgent',
    label: 'Unassigned urgent',
    description: 'Open work at high or urgent priority that nobody owns yet.',
    filters: {
      ...EMPTY_FILTERS,
      assignee: 'none',
      statuses: ['open'],
      priorities: ['high', 'urgent'],
    },
  },
  {
    id: 'awaiting-approval',
    label: 'Awaiting approval',
    description: 'Submitted work waiting for a lead to approve or send back.',
    filters: { ...EMPTY_FILTERS, statuses: ['awaiting_approval'] },
  },
]

/** The same idea for the cross-team view, where "assigned to me" is the whole page. */
export const MY_WORK_PRESETS: Preset[] = [
  {
    id: 'all',
    label: 'All my work',
    description: 'Everything assigned to you, across every team.',
    filters: EMPTY_FILTERS,
  },
  {
    id: 'urgent',
    label: 'High priority',
    description: 'Your high and urgent work.',
    filters: { ...EMPTY_FILTERS, priorities: ['high', 'urgent'] },
  },
  {
    id: 'awaiting-approval',
    label: 'Awaiting approval',
    description: 'Your work that has been submitted and is waiting for a lead.',
    filters: { ...EMPTY_FILTERS, statuses: ['awaiting_approval'] },
  },
]

export function PresetBar({
  presets,
  filters,
  onSelect,
}: {
  presets: Preset[]
  filters: ItemFilters
  onSelect: (filters: ItemFilters) => void
}) {
  const active = structuredKey(filters)

  return (
    <div className="presets" role="group" aria-label="Saved views">
      {presets.map((preset) => {
        const selected = structuredKey(preset.filters) === active
        return (
          <button
            key={preset.id}
            type="button"
            className={`presets__button${selected ? ' presets__button--active' : ''}`}
            aria-pressed={selected}
            title={preset.description}
            onClick={() => onSelect(preset.filters)}
          >
            {preset.label}
          </button>
        )
      })}
    </div>
  )
}

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value]
}

export function ItemFiltersBar({
  filters,
  onChange,
  searchInput,
  onSearchInput,
  resultCount,
  showAssignee = true,
  searchPlaceholder = 'Title and description…',
}: {
  filters: ItemFilters
  onChange: (next: ItemFilters) => void
  searchInput: string
  onSearchInput: (value: string) => void
  resultCount: number | null
  /** Off for the cross-team view, where the scope *is* "assigned to me". */
  showAssignee?: boolean
  searchPlaceholder?: string
}) {
  return (
    <section className="filters" aria-label="Filters">
      <div className="filters__row">
        <label className="field field--grow">
          <span className="field__label">Search</span>
          <input
            type="search"
            name="q"
            placeholder={searchPlaceholder}
            value={searchInput}
            onChange={(event) => onSearchInput(event.target.value)}
          />
        </label>

        {showAssignee ? (
          <label className="field">
            <span className="field__label">Assignee</span>
            <select
              value={filters.assignee ?? ''}
              onChange={(event) =>
                onChange({
                  ...filters,
                  assignee: event.target.value === '' ? null : event.target.value,
                })
              }
            >
              <option value="">Anyone</option>
              <option value="me">Assigned to me</option>
              <option value="none">Unassigned</option>
            </select>
          </label>
        ) : null}
      </div>

      <fieldset className="filters__group">
        <legend>Status</legend>
        <div className="filters__chips">
          {STATUS_ORDER.map((status: Status) => (
            <label key={status} className="chip">
              <input
                type="checkbox"
                checked={filters.statuses.includes(status)}
                onChange={() => onChange({ ...filters, statuses: toggle(filters.statuses, status) })}
              />
              <span>{statusLabel(status)}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="filters__group">
        <legend>Priority</legend>
        <div className="filters__chips">
          {PRIORITIES.map((priority: Priority) => (
            <label key={priority} className="chip">
              <input
                type="checkbox"
                checked={filters.priorities.includes(priority)}
                onChange={() =>
                  onChange({ ...filters, priorities: toggle(filters.priorities, priority) })
                }
              />
              <span>{priorityLabel(priority)}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="filters__group">
        <legend>Type</legend>
        <div className="filters__chips">
          {TYPES.map((type: WorkItemType) => (
            <label key={type} className="chip">
              <input
                type="checkbox"
                checked={filters.types.includes(type)}
                onChange={() => onChange({ ...filters, types: toggle(filters.types, type) })}
              />
              <span>{typeLabel(type)}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="filters__footer">
        <span className="filters__count">
          {resultCount === null ? 'Counting…' : `${resultCount} loaded`}
        </span>
        <button
          type="button"
          className="button button--quiet button--small"
          onClick={() => {
            onSearchInput('')
            onChange(EMPTY_FILTERS)
          }}
        >
          Clear filters
        </button>
      </div>
    </section>
  )
}
