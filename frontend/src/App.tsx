/**
 * The application shell: who is signed in, which team is open, and which view is showing.
 *
 * Three decisions are visible here.
 *
 * * **No session, no application.** The sign-in screen is not a route; it is what the
 *   application *is* until a token exists. There is no half-signed-in state where a page
 *   renders with empty data because a request failed for authorization reasons.
 * * **The team comes from the URL.** `#/teams/:teamKey` is the address, so a view can be
 *   shared and reloaded. The membership is looked up from the session's own list, which is
 *   the same set the server will authorize against — the URL names a team, it does not
 *   prove access to one.
 * * **A team the caller is not in is not a team.** An unknown or foreign `teamKey` gets a
 *   plain "not found" rather than an empty list, because the server would refuse every
 *   request for it anyway.
 */

import { useEffect } from 'react'

import { signOut } from './auth/auth'
import { LoginPage } from './auth/LoginPage'
import { useSession } from './auth/session'
import type { Membership } from './api/types'
import { Layout } from './components/Layout'
import { Empty } from './components/states'
import { navigate, useRoute } from './lib/router'
import { ItemDetailPage } from './features/items/ItemDetailPage'
import { ItemListPage } from './features/items/ItemListPage'
import { MyWorkPage } from './features/items/MyWorkPage'
import { NotificationsPage } from './features/notifications/NotificationsPage'
import { TeamActivityPage } from './features/activity/TeamActivityPage'

/** A stable empty list, so `memberships` does not change identity on every render. */
const NO_MEMBERSHIPS: Membership[] = []

export function App() {
  const session = useSession()
  const route = useRoute()

  const memberships = session?.memberships ?? NO_MEMBERSHIPS
  const requestedTeamKey =
    route.name === 'items' || route.name === 'item' || route.name === 'teamActivity'
      ? route.teamKey
      : null
  const activeTeam = memberships.find((membership) => membership.team_key === requestedTeamKey) ?? null

  // Landing on `#/` with teams to choose from goes to the first one. Done in an effect
  // rather than during render so the URL is the only thing that decides what is shown.
  useEffect(() => {
    if (session && route.name === 'home' && memberships.length > 0) {
      const first = memberships[0]
      if (first) navigate({ name: 'items', teamKey: first.team_key })
    }
  }, [session, route.name, memberships])

  if (!session) return <LoginPage />

  if (memberships.length === 0) {
    return (
      <div className="app">
        <main className="app__main">
          <Empty
            title="You are not a member of any team"
            detail="This account has no team memberships, so there is nothing to show. Ask an admin to add you to one."
          />
          <button type="button" className="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </main>
      </div>
    )
  }

  function renderView() {
    switch (route.name) {
      case 'item':
        return activeTeam ? (
          <ItemDetailPage team={activeTeam} itemId={route.itemId} />
        ) : (
          <UnknownTeam teamKey={route.teamKey} />
        )
      case 'teamActivity':
        return activeTeam ? <TeamActivityPage team={activeTeam} /> : <UnknownTeam teamKey={route.teamKey} />
      case 'items':
        return activeTeam ? <ItemListPage team={activeTeam} /> : <UnknownTeam teamKey={route.teamKey} />
      case 'myWork':
        return <MyWorkPage />
      case 'notifications':
        return <NotificationsPage />
      case 'notFound':
        return (
          <Empty
            title="That page does not exist"
            detail={`Nothing is routed at “${route.path}”. Pick a team from the header to get back.`}
          />
        )
      case 'home':
        return <Empty title="Choose a team" detail="Pick a team from the header." />
    }
  }

  return (
    <Layout
      memberships={memberships}
      activeTeamKey={activeTeam?.team_key ?? null}
      onSignOut={() => void signOut()}
    >
      {renderView()}
    </Layout>
  )
}

function UnknownTeam({ teamKey }: { teamKey: string }) {
  return (
    <Empty
      title="That team is not one of yours"
      detail={`You are not a member of “${teamKey}”. Pick one of your own teams from the header.`}
    />
  )
}
