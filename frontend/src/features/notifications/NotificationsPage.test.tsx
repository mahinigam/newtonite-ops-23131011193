/**
 * The notifications view.
 *
 * Two things are worth testing, and neither is "a list rendered":
 *
 * * the badge and the list come from two different endpoints, so a page that shows the
 *   right rows with the wrong count is a real failure mode;
 * * acknowledging a notification is a **write**: it must go to the server, and the view
 *   must not pretend a row is read because it was looked at.
 *
 * The page is also the visible consequence of the outbox: rows exist only because a worker
 * delivered them, which is why the copy says so rather than implying a live feed.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { sessionStore } from '../../auth/session'
import { errorResponse, installFetch, jsonResponse } from '../../test/fetch'
import {
  ITEM_ID,
  NINA,
  NOTIFICATION_ID,
  OTHER_ITEM_ID,
  membership,
  notification,
  page,
} from '../../test/fixtures'
import { renderWithClient } from '../../test/render'
import { NotificationsPage } from './NotificationsPage'

const LIST_PATH = '/api/v1/notifications'
const UNREAD_PATH = '/api/v1/notifications/unread-count'

beforeEach(() => {
  sessionStore.start('test-token', {
    user: NINA,
    memberships: [membership()],
    expiresAt: '2099-01-01T00:00:00Z',
  })
})

afterEach(() => {
  sessionStore.clear()
})

function render() {
  return renderWithClient(<NotificationsPage />)
}

/** The list plus a count, which is how the real API answers. */
function stubList(rows: ReturnType<typeof notification>[], unread: number) {
  return installFetch((request) => {
    if (request.path === UNREAD_PATH) return jsonResponse({ unread })
    if (request.path === LIST_PATH) return jsonResponse(page(rows))
    return errorResponse(404, 'not_found', `No stub for ${request.method} ${request.path}`)
  })
}

describe('NotificationsPage', () => {
  it('shows the notifications the server returned, with the team and the unread count', async () => {
    stubList([notification()], 1)

    render()

    const body = await screen.findByText(/assigned you/i)
    const row = body.closest('li') as HTMLElement
    // The meta line is "Platform · 3 days ago", so match the line rather than the word.
    expect(within(row).getByText(/^Platform ·/)).toBeInTheDocument()
    // "Unread" is spoken, not only coloured.
    expect(within(row).getByLabelText('Unread')).toBeInTheDocument()
    expect(screen.getByText(/^1 unread/i)).toBeInTheDocument()
  })

  it('links back to the item in its own team', async () => {
    stubList([notification()], 1)

    render()

    const link = await screen.findByRole('link', { name: /open item/i })
    expect(link).toHaveAttribute('href', `#/teams/platform/items/${ITEM_ID}`)
  })

  it('marks one notification read on the server and then shows it as read', async () => {
    let read = false
    const calls = installFetch((request) => {
      if (request.path === `${LIST_PATH}/${NOTIFICATION_ID}/read` && request.method === 'POST') {
        read = true
        return new Response(null, { status: 204 })
      }
      if (request.path === UNREAD_PATH) return jsonResponse({ unread: read ? 0 : 1 })
      if (request.path === LIST_PATH) {
        return jsonResponse(
          page([notification({ read_at: read ? '2026-10-15T11:00:00Z' : null })]),
        )
      }
      return errorResponse(404, 'not_found', `No stub for ${request.method} ${request.path}`)
    })
    const user = userEvent.setup()

    render()
    await user.click(await screen.findByRole('button', { name: 'Mark as read' }))

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.path === `${LIST_PATH}/${NOTIFICATION_ID}/read` && call.method === 'POST',
        ),
      ).toBe(true),
    )

    // The row is re-read from the server, so the button goes away because the server said so.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Mark as read' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByLabelText('Unread')).not.toBeInTheDocument()
    expect(screen.getByText(/nothing unread/i)).toBeInTheDocument()
  })

  it('marks everything read in one request', async () => {
    let read = false
    const calls = installFetch((request) => {
      if (request.path === `${LIST_PATH}/read-all` && request.method === 'POST') {
        read = true
        return jsonResponse({ marked: 2 })
      }
      if (request.path === UNREAD_PATH) return jsonResponse({ unread: read ? 0 : 2 })
      if (request.path === LIST_PATH) {
        return jsonResponse(
          page([
            notification({ read_at: read ? '2026-10-15T11:00:00Z' : null }),
            notification({
              id: OTHER_ITEM_ID,
              work_item_id: OTHER_ITEM_ID,
              body: 'Lena Lead approved your work.',
              event_type: 'work_item_approved',
              read_at: read ? '2026-10-15T11:00:00Z' : null,
            }),
          ]),
        )
      }
      return errorResponse(404, 'not_found', `No stub for ${request.method} ${request.path}`)
    })
    const user = userEvent.setup()

    render()
    await user.click(await screen.findByRole('button', { name: 'Mark all as read' }))

    await waitFor(() =>
      expect(calls.some((call) => call.path === `${LIST_PATH}/read-all`)).toBe(true),
    )
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Mark as read' })).not.toBeInTheDocument(),
    )
    expect(screen.getByText(/nothing unread/i)).toBeInTheDocument()
  })

  it('reports a failed acknowledgement rather than leaving the row looking read', async () => {
    installFetch((request) => {
      if (request.path === `${LIST_PATH}/${NOTIFICATION_ID}/read`) {
        return errorResponse(500, 'internal_error', 'Unexpected failure.')
      }
      if (request.path === UNREAD_PATH) return jsonResponse({ unread: 1 })
      if (request.path === LIST_PATH) return jsonResponse(page([notification()]))
      return errorResponse(404, 'not_found', 'No stub')
    })
    const user = userEvent.setup()

    render()
    await user.click(await screen.findByRole('button', { name: 'Mark as read' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/something went wrong/i)
    // Still unread: the write did not land, so the interface must not claim it did.
    expect(screen.getByLabelText('Unread')).toBeInTheDocument()
  })

  it('does not link to a team the session no longer includes', async () => {
    stubList(
      [
        notification({
          team_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
          body: 'You were assigned some work.',
        }),
      ],
      1,
    )

    render()

    const row = (await screen.findByText('You were assigned some work.')).closest('li') as HTMLElement
    expect(within(row).getByText(/^A team you have left ·/)).toBeInTheDocument()
    expect(within(row).queryByRole('link', { name: /open item/i })).not.toBeInTheDocument()
  })

  it('says so when there is nothing to show', async () => {
    stubList([], 0)

    render()

    expect(await screen.findByText(/no notifications/i)).toBeInTheDocument()
    expect(screen.getByText(/when somebody assigns you work/i)).toBeInTheDocument()
  })

  it('reports a failure and offers to re-request', async () => {
    let attempt = 0
    const calls = installFetch((request) => {
      if (request.path === UNREAD_PATH) return jsonResponse({ unread: 0 })
      if (request.path === LIST_PATH) {
        attempt += 1
        if (attempt === 1) return errorResponse(500, 'internal_error', 'Unexpected failure.')
        return jsonResponse(page([notification({ read_at: '2026-10-15T11:00:00Z' })]))
      }
      return errorResponse(404, 'not_found', 'No stub')
    })
    const user = userEvent.setup()

    render()

    expect(await screen.findByRole('alert')).toHaveTextContent(/something went wrong/i)
    await user.click(screen.getByRole('button', { name: /try again/i }))

    expect(await screen.findByText(/assigned you/i)).toBeInTheDocument()
    expect(calls.filter((call) => call.path === LIST_PATH)).toHaveLength(2)
  })

  it('pages with the cursor the server issued', async () => {
    installFetch((request) => {
      if (request.path === UNREAD_PATH) return jsonResponse({ unread: 2 })
      if (request.path === LIST_PATH) {
        if (request.searchParams.get('cursor') === 'cursor-1') {
          return jsonResponse(
            page([notification({ id: OTHER_ITEM_ID, body: 'A second page notification.' })]),
          )
        }
        return jsonResponse(
          page([notification()], { next_cursor: 'cursor-1', has_more: true }),
        )
      }
      return errorResponse(404, 'not_found', 'No stub')
    })
    const user = userEvent.setup()

    render()
    await screen.findByText(/assigned you/i)

    await user.click(screen.getByRole('button', { name: /load more/i }))

    expect(await screen.findByText('A second page notification.')).toBeInTheDocument()
    expect(screen.getByText(/assigned you/i)).toBeInTheDocument()
  })
})
