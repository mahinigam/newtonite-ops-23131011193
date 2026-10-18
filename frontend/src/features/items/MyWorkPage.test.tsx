/**
 * "My work": the cross-team list.
 *
 * The point of this page is that the scope is the *caller*, not a team, so the things worth
 * testing are the ones that could quietly break that:
 *
 * * it asks `/api/v1/me/items` and sends **no assignee parameter** — the server derives the
 *   scope from the token, and a parameter would turn a personal view into a cross-team one;
 * * a row still knows which team it belongs to, because the link needs the team key;
 * * filters are server query parameters, exactly as they are on a team list;
 * * paging uses the cursor the server issued.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { sessionStore } from '../../auth/session'
import { errorResponse, installFetch, jsonResponse } from '../../test/fetch'
import {
  ITEM_ID,
  NINA,
  OTHER_ITEM_ID,
  SUPPORT_TEAM_ID,
  membership,
  page,
  summary,
} from '../../test/fixtures'
import { renderWithClient } from '../../test/render'
import { MyWorkPage } from './MyWorkPage'

const ME_ITEMS_PATH = '/api/v1/me/items'

beforeEach(() => {
  sessionStore.start({
    user: NINA,
    memberships: [
      membership(),
      membership({
        team_id: SUPPORT_TEAM_ID,
        team_key: 'support',
        team_name: 'Support',
        role: 'viewer',
      }),
    ],
    accessExpiresAt: '2099-01-01T00:00:00Z',
  })
})

afterEach(() => {
  sessionStore.clear()
})

function render() {
  return renderWithClient(<MyWorkPage />)
}

describe('MyWorkPage', () => {
  it('asks the caller-scoped endpoint and sends no assignee', async () => {
    const calls = installFetch((request) => {
      if (request.path !== ME_ITEMS_PATH) {
        return errorResponse(404, 'not_found', `No stub for ${request.path}`)
      }
      return jsonResponse(page([summary({ assignee: NINA })]))
    })

    render()
    await screen.findByText('Refund the duplicate charge')

    const first = calls.find((call) => call.path === ME_ITEMS_PATH)
    expect(first).toBeDefined()
    expect(first?.searchParams.has('assignee')).toBe(false)
  })

  it('says which team each row belongs to and links to that team', async () => {
    installFetch(() =>
      jsonResponse(
        page([
          summary({ id: ITEM_ID, team_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Platform item' }),
          summary({ id: OTHER_ITEM_ID, team_id: SUPPORT_TEAM_ID, title: 'Support item' }),
        ]),
      ),
    )

    render()

    const platformRow = (await screen.findByText('Platform item')).closest('li') as HTMLElement
    expect(within(platformRow).getByText('Platform')).toBeInTheDocument()
    expect(within(platformRow).getByRole('link')).toHaveAttribute(
      'href',
      `#/teams/platform/items/${ITEM_ID}`,
    )

    const supportRow = screen.getByText('Support item').closest('li') as HTMLElement
    expect(within(supportRow).getByText('Support')).toBeInTheDocument()
    // The link uses the team the item is in, not the first team in the session.
    expect(within(supportRow).getByRole('link')).toHaveAttribute(
      'href',
      `#/teams/support/items/${OTHER_ITEM_ID}`,
    )
  })

  it('turns a preset into a server filter and leaves the assignee control out', async () => {
    const calls = installFetch(() => jsonResponse(page([summary({ assignee: NINA })])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    // The scope is already "assigned to me", so there is no assignee control to contradict it.
    expect(screen.queryByRole('combobox', { name: /assignee/i })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'High priority' }))

    await waitFor(() => {
      const last = calls.at(-1)
      const priorityParams = last?.searchParams.getAll('priority')
      if (!priorityParams?.length) {
        throw new Error('No priority params. Last URL: ' + (last?.url || 'none') + ' | Calls: ' + calls.length)
      }
      expect(priorityParams.sort()).toEqual(['high', 'urgent'])
      expect(last?.searchParams.has('assignee')).toBe(false)
    })
  })

  it('shows an empty state that explains where work comes from', async () => {
    installFetch(() => jsonResponse(page([])))

    render()

    expect(await screen.findByText(/nothing is assigned to you/i)).toBeInTheDocument()
    expect(screen.getByText(/you claim an item/i)).toBeInTheDocument()
  })

  it('pages with the cursor the server issued', async () => {
    installFetch((request) => {
      if (request.searchParams.get('cursor') === 'cursor-1') {
        return jsonResponse(page([summary({ id: OTHER_ITEM_ID, title: 'Second page item' })]))
      }
      return jsonResponse(
        page([summary({ title: 'First page item' })], { next_cursor: 'cursor-1', has_more: true }),
      )
    })
    const user = userEvent.setup()

    render()
    await screen.findByText('First page item')

    await user.click(screen.getByRole('button', { name: /load more/i }))

    expect(await screen.findByText('Second page item')).toBeInTheDocument()
    expect(screen.getByText('First page item')).toBeInTheDocument()
  })

  it('reports a failure instead of showing an empty list', async () => {
    installFetch(() => errorResponse(500, 'internal_error', 'Unexpected failure.'))

    render()

    expect(await screen.findByRole('alert')).toHaveTextContent(/something went wrong/i)
  })
})
