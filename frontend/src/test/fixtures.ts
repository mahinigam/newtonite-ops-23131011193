/**
 * Fixtures.
 *
 * Small builders rather than large literals, so a test says only what it is about. The
 * ids are fixed strings: a test that asserts on an id should not depend on a random one,
 * and a UUID-shaped string is required because the router validates the shape.
 */

import type {
  ActivityEvent,
  Comment,
  Membership,
  Notification,
  Page,
  UserRef,
  WorkItemDetail,
  WorkItemSummary,
} from '../api/types'

export const NINA: UserRef = {
  id: '11111111-1111-4111-8111-111111111111',
  email: 'nina.member@newtonite.dev',
  display_name: 'Nina Member',
}

export const LENA: UserRef = {
  id: '22222222-2222-4222-8222-222222222222',
  email: 'lena.lead@newtonite.dev',
  display_name: 'Lena Lead',
}

export const PLATFORM_TEAM_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
export const SUPPORT_TEAM_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
export const ITEM_ID = '33333333-3333-4333-8333-333333333333'
export const OTHER_ITEM_ID = '44444444-4444-4444-8444-444444444444'

export function membership(overrides: Partial<Membership> = {}): Membership {
  return {
    team_id: PLATFORM_TEAM_ID,
    team_key: 'platform',
    team_name: 'Platform',
    role: 'member',
    ...overrides,
  }
}

export function summary(overrides: Partial<WorkItemSummary> = {}): WorkItemSummary {
  return {
    id: ITEM_ID,
    team_id: PLATFORM_TEAM_ID,
    title: 'Refund the duplicate charge',
    type: 'payment',
    status: 'open',
    priority: 'urgent',
    assignee: null,
    due_at: null,
    version: 1,
    created_at: '2026-10-15T09:00:00Z',
    updated_at: '2026-10-15T09:00:00Z',
    available_transitions: ['in_progress'],
    ...overrides,
  }
}

export function detail(overrides: Partial<WorkItemDetail> = {}): WorkItemDetail {
  return {
    ...summary(),
    description: 'The customer was charged twice.',
    created_by: LENA,
    ...overrides,
  }
}

export function comment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: '55555555-5555-4555-8555-555555555555',
    work_item_id: ITEM_ID,
    body: 'Looks like a bad deploy.',
    author: NINA,
    created_at: '2026-10-15T10:00:00Z',
    ...overrides,
  }
}

export function event(overrides: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id: 1,
    work_item_id: ITEM_ID,
    event_type: 'status_changed',
    from_value: 'open',
    to_value: 'in_progress',
    meta: {},
    actor: NINA,
    created_at: '2026-10-15T10:00:00Z',
    ...overrides,
  }
}

export const NOTIFICATION_ID = '66666666-6666-4666-8666-666666666666'

export function notification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: NOTIFICATION_ID,
    team_id: PLATFORM_TEAM_ID,
    work_item_id: ITEM_ID,
    event_type: 'work_item_assigned',
    body: 'Lena Lead assigned you “Refund the duplicate charge”.',
    created_at: '2026-10-15T10:00:00Z',
    read_at: null,
    ...overrides,
  }
}

export function page<T>(
  items: T[],
  overrides: Partial<Page<T>> = {},
): Page<T> {
  return {
    items,
    next_cursor: null,
    has_more: false,
    limit: 25,
    ...overrides,
  }
}
