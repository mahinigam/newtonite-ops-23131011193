/**
 * The shell: a header that says who you are and which team you are looking at, and a
 * navigation bar.
 *
 * The navigation has two halves. **My work** and **Notifications** belong to the person,
 * not to a team, so they are always available; the team's views appear once a team is open.
 * That split is the same one the API makes with `/me/items` and `/notifications`, where the
 * scope comes from the caller's token rather than from a team in the path.
 *
 * The team switcher lists only the teams the caller is a member of — which is the same
 * set the server will authorize against, so the interface cannot offer a team that would
 * then 404. It is a convenience, not a boundary: the server decides.
 *
 * `aria-current` comes from the route rather than from "a team is open", so the highlight
 * names the page actually being shown.
 */

import type { ReactNode } from 'react'

import type { Membership } from '../api/types'
import { navigate, useRoute } from '../lib/router'
import { roleLabel } from '../lib/format'
import { useSession } from '../auth/session'
import { useUnreadCount } from '../features/notifications/useUnreadCount'

export function Layout({
  memberships,
  activeTeamKey,
  onSignOut,
  children,
}: {
  memberships: Membership[]
  activeTeamKey: string | null
  onSignOut: () => void
  children: ReactNode
}) {
  const session = useSession()
  const route = useRoute()
  const unread = useUnreadCount()
  const active = memberships.find((membership) => membership.team_key === activeTeamKey) ?? null

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__mark" aria-hidden="true">
            N
          </span>
          <div>
            <h1 className="app__title">Newtonite</h1>
            <p className="app__subtitle">Operations under pressure</p>
          </div>
        </div>

        <div className="app__identity">
          <label className="app__team">
            <span className="visually-hidden">Team</span>
            <select
              value={activeTeamKey ?? ''}
              onChange={(event) => {
                navigate({ name: 'items', teamKey: event.target.value })
              }}
            >
              {memberships.map((membership) => (
                <option key={membership.team_key} value={membership.team_key}>
                  {membership.team_name}
                </option>
              ))}
            </select>
          </label>
          <span className="app__user">
            {session?.user.display_name}
            {active ? <span className="app__role"> · {roleLabel(active.role)}</span> : null}
          </span>
          <button type="button" className="button button--quiet" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      <nav className="app__nav" aria-label="Views">
        <a href="#/my-work" aria-current={route.name === 'myWork' ? 'page' : undefined}>
          My work
        </a>
        <a
          href="#/notifications"
          aria-current={route.name === 'notifications' ? 'page' : undefined}
        >
          Notifications
          {unread > 0 ? (
            <span className="nav__count" aria-label={`${unread} unread`}>
              {unread}
            </span>
          ) : null}
        </a>

        {active ? (
          <>
            <span className="app__nav-divider" aria-hidden="true" />
            <a
              href={`#/teams/${active.team_key}`}
              aria-current={
                route.name === 'items' || route.name === 'item' ? 'page' : undefined
              }
            >
              Work items
            </a>
            <a
              href={`#/teams/${active.team_key}/activity`}
              aria-current={route.name === 'teamActivity' ? 'page' : undefined}
            >
              Team activity
            </a>
          </>
        ) : null}
      </nav>

      <main className="app__main">{children}</main>
    </div>
  )
}
