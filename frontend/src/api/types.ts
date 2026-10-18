/**
 * Response shapes, mirrored from the API's own schemas.
 *
 * These are hand-written rather than generated from the OpenAPI document: there are eight
 * shapes, and a hand-written file is easier to read and to keep honest than a generator
 * plus its configuration. The union types are the API's enum values verbatim — if the
 * backend adds one, `npm run typecheck` is what notices.
 */

export type Role = 'viewer' | 'member' | 'lead' | 'admin'

export type Status = 'open' | 'in_progress' | 'awaiting_approval' | 'resolved' | 'closed'

export type Priority = 'low' | 'medium' | 'high' | 'urgent'

export type WorkItemType = 'incident' | 'customer_issue' | 'payment' | 'compliance' | 'task'

export type ActivityType =
  | 'created'
  | 'claimed'
  | 'assigned'
  | 'unassigned'
  | 'status_changed'
  | 'approved'
  | 'rejected'
  | 'priority_changed'
  | 'due_date_changed'
  | 'type_changed'
  | 'title_changed'
  | 'description_changed'
  | 'commented'

export interface UserRef {
  id: string
  email: string
  display_name: string
}

export interface Membership {
  team_id: string
  team_key: string
  team_name: string
  role: Role
}

export interface MeResponse {
  user: UserRef
  memberships: Membership[]
}

export interface LoginResponse {
  token: string
  token_type: string
  expires_at: string
  user: UserRef
}

export interface TeamSummary {
  team_id: string
  team_key: string
  team_name: string
  description: string
  role: Role
}

export interface MemberSummary {
  user_id: string
  email: string
  display_name: string
  role: Role
}

/** A page of anything. `next_cursor` is handed back verbatim; never build one. */
export interface Page<T> {
  items: T[]
  next_cursor: string | null
  has_more: boolean
  limit: number
}

export interface WorkItemSummary {
  id: string
  team_id: string
  title: string
  type: WorkItemType
  status: Status
  priority: Priority
  assignee: UserRef | null
  due_at: string | null
  version: number
  created_at: string
  updated_at: string
  /**
   * Computed by the server for this caller's role. The UI renders these rather than
   * deciding which moves are legal — a control the interface shows and the server's
   * answer are then the same thing.
   */
  available_transitions: Status[]
}

export interface WorkItemDetail extends WorkItemSummary {
  description: string
  created_by: UserRef
}

export interface Comment {
  id: string
  work_item_id: string
  body: string
  author: UserRef
  created_at: string
}

export type NotificationType =
  | 'work_item_assigned'
  | 'work_item_approved'
  | 'work_item_rejected'

/**
 * A delivered notification. `body` is rendering-ready text the worker built when it
 * delivered the event, and `team_id` plus `work_item_id` are enough to link back to the
 * item because the session already carries the caller's memberships.
 */
export interface Notification {
  id: string
  team_id: string
  work_item_id: string
  event_type: NotificationType
  body: string
  created_at: string
  read_at: string | null
}

export interface UnreadCount {
  unread: number
}

export interface MarkAllReadResult {
  marked: number
}

export interface ActivityEvent {
  id: number
  work_item_id: string
  event_type: ActivityType
  from_value: string | null
  to_value: string | null
  /** Rendering help only. The UI must work without it, and does. */
  meta: Record<string, unknown>
  actor: UserRef
  created_at: string
}

/** The filters the item list sends to the server. Every one is applied in PostgreSQL. */
export interface ItemFilters {
  statuses: Status[]
  priorities: Priority[]
  types: WorkItemType[]
  assignee: string | null
  search: string
}
