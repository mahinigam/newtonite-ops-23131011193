/**
 * A very small hash router.
 *
 * There are five views: the team's item list, one item, the team's activity feed, the
 * caller's cross-team work, and the caller's notifications. Four of them are reached from
 * another. A routing library would be a framework for a problem this application does not
 * have; this is the whole of what it needs, and it is a pure function plus a subscription,
 * so it can be tested without a DOM.
 *
 * Hash routing rather than the History API because it needs no server rewrite rule: the
 * built `dist/` can be served by any static file server, including `vite preview`.
 */

import { useSyncExternalStore } from 'react'

export type Route =
  | { name: 'items'; teamKey: string }
  | { name: 'item'; teamKey: string; itemId: string }
  | { name: 'teamActivity'; teamKey: string }
  | { name: 'myWork' }
  | { name: 'notifications' }
  | { name: 'home' }
  | { name: 'notFound'; path: string }

const UUID = /^[0-9a-fA-F-]{36}$/

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, '')
  const segments = path.split('/').filter((segment) => segment.length > 0)

  if (segments.length === 0) return { name: 'home' }
  // The two person-scoped views sit outside a team, because neither is about one team:
  // "my work" spans the caller's memberships and a notification is addressed to a person.
  if (segments.length === 1 && segments[0] === 'my-work') return { name: 'myWork' }
  if (segments.length === 1 && segments[0] === 'notifications') return { name: 'notifications' }
  if (segments[0] !== 'teams' || segments.length < 2) return { name: 'notFound', path }

  const teamKey = segments[1] as string
  if (segments.length === 2) return { name: 'items', teamKey }

  const section = segments[2]
  if (section === 'activity' && segments.length === 3) return { name: 'teamActivity', teamKey }

  const itemId = segments[3]
  if (section === 'items' && segments.length === 4 && itemId && UUID.test(itemId)) {
    return { name: 'item', teamKey, itemId }
  }

  return { name: 'notFound', path }
}

export function pathFor(route: Route): string {
  switch (route.name) {
    case 'home':
      return '#/'
    case 'items':
      return `#/teams/${route.teamKey}`
    case 'item':
      return `#/teams/${route.teamKey}/items/${route.itemId}`
    case 'teamActivity':
      return `#/teams/${route.teamKey}/activity`
    case 'myWork':
      return '#/my-work'
    case 'notifications':
      return '#/notifications'
    case 'notFound':
      return `#${route.path}`
  }
}

export function navigate(route: Route): void {
  window.location.hash = pathFor(route)
}

function subscribe(listener: () => void): () => void {
  window.addEventListener('hashchange', listener)
  return () => {
    window.removeEventListener('hashchange', listener)
  }
}

function getSnapshot(): string {
  return window.location.hash
}

export function useRoute(): Route {
  return parseHash(useSyncExternalStore(subscribe, getSnapshot, getSnapshot))
}
