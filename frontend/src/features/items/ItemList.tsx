/**
 * The list of work items, as returned by the server.
 *
 * Each row is a link to the item, and the whole row is the target rather than a small
 * "open" link — an operations list is scanned quickly, and the thing being scanned should
 * be the thing you can click.
 *
 * Nothing here sorts or filters. The order is the server's `(created_at, id)` descending
 * keyset order and the filters are the server's; the component's job is to render what it
 * was given and to ask for the next page.
 *
 * The list is given the *teams the caller belongs to* rather than one team key, because
 * "my work" spans teams. A row whose team is not in that map cannot be linked: the caller's
 * memberships are the server's own authorization set, so a missing entry means the local
 * session is out of date. Rendering it without a link is the honest result — the row is
 * real, but this page can no longer open it.
 */

import type { Membership, WorkItemSummary } from '../../api/types'
import { formatRelative, priorityLabel, statusLabel, typeLabel } from '../../lib/format'

function Badge({ kind, label }: { kind: string; label: string }) {
  return <span className={`badge badge--${kind}`}>{label}</span>
}

function RowBody({ item, teamName }: { item: WorkItemSummary; teamName: string | null }) {
  return (
    <>
      <div className="item-row__main">
        <span className="item-row__title">{item.title}</span>
        <span className="item-row__meta">
          <Badge kind={`status-${item.status}`} label={statusLabel(item.status)} />
          <Badge kind={`priority-${item.priority}`} label={priorityLabel(item.priority)} />
          <span className="item-row__type">{typeLabel(item.type)}</span>
          {teamName ? <span className="item-row__team">{teamName}</span> : null}
        </span>
      </div>
      <div className="item-row__side">
        <span className="item-row__assignee">
          {item.assignee ? item.assignee.display_name : 'Unassigned'}
        </span>
        <span className="item-row__updated">updated {formatRelative(item.updated_at)}</span>
      </div>
    </>
  )
}

export function ItemRow({
  item,
  teamKey,
  teamName = null,
}: {
  item: WorkItemSummary
  teamKey: string | null
  teamName?: string | null
}) {
  if (teamKey === null) {
    return (
      <li className="item-row item-row--unlinked">
        <div className="item-row__link">
          <RowBody item={item} teamName={teamName} />
        </div>
      </li>
    )
  }

  return (
    <li className="item-row">
      <a className="item-row__link" href={`#/teams/${teamKey}/items/${item.id}`}>
        <RowBody item={item} teamName={teamName} />
      </a>
    </li>
  )
}

export function ItemList({
  items,
  teams,
  showTeam = false,
}: {
  items: WorkItemSummary[]
  /** The caller's memberships, keyed by team id. */
  teams: Map<string, Membership>
  /** Say which team each row belongs to. Off for a single team's list. */
  showTeam?: boolean
}) {
  return (
    <ul className="item-list">
      {items.map((item) => {
        const team = teams.get(item.team_id)
        return (
          <ItemRow
            key={item.id}
            item={item}
            teamKey={team?.team_key ?? null}
            teamName={showTeam ? (team?.team_name ?? null) : null}
          />
        )
      })}
    </ul>
  )
}
