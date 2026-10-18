/**
 * The team feed.
 *
 * Three things separate this view from an item's timeline, and each is tested here because
 * each was wrong before:
 *
 * * a row says *which* item it is about, and links to it — a feed of "this item" rows is
 *   not usable;
 * * the date range is a server query with inclusive bounds, and changing it starts a new
 *   first page rather than continuing an old cursor;
 * * a failed *next* page is retried as one page, not by re-walking every page loaded so
 *   far, while the events already read stay on screen.
 */

import { describe, expect, it } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { callsTo, errorResponse, installFetch, jsonResponse } from '../../test/fetch'
import { ITEM_ID, PLATFORM_TEAM_ID, event, membership, page } from '../../test/fixtures'
import { renderWithClient } from '../../test/render'
import { TeamActivityPage } from './TeamActivityPage'

const FEED_PATH = `/api/v1/teams/${PLATFORM_TEAM_ID}/activity`

function render() {
  return renderWithClient(<TeamActivityPage team={membership()} />)
}

describe('TeamActivityPage', () => {
  it('names and links the item on every row, and spells out the colour key', async () => {
    installFetch((request) =>
      request.path === FEED_PATH
        ? jsonResponse(
            page([
              event({
                id: 2,
                event_type: 'assigned',
                work_item_title: 'Refund the duplicate charge',
                meta: { assignee_name: 'Nina Member' },
              }),
            ]),
          )
        : errorResponse(404, 'not_found', `No stub for ${request.path}`),
    )

    render()

    const link = await screen.findByRole('link', { name: /refund the duplicate charge/i })
    expect(link).toHaveAttribute('href', `#/teams/platform/items/${ITEM_ID}`)
    // The verb is phrased for a feed: the item is the object of the sentence.
    expect(screen.getByText('assigned')).toBeInTheDocument()
    // The rail colour repeats what the text already says, so the legend names it in words.
    const legend = screen.getByRole('list', { name: /colour key/i })
    expect(legend).toHaveTextContent('Approved')
    expect(legend).toHaveTextContent('Sent back')
    expect(legend).toHaveTextContent('Status change')
  })

  it('turns the date range into inclusive server bounds, and clearing it removes them', async () => {
    const calls = installFetch((request) =>
      request.path === FEED_PATH
        ? jsonResponse(page([]))
        : errorResponse(404, 'not_found', `No stub for ${request.path}`),
    )
    render()

    await waitFor(() => expect(callsTo(calls, FEED_PATH)).toHaveLength(1))

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-17' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-10-18' } })

    // "To 18 Oct" means through the end of the 18th, in the browser's own time zone.
    const start = new Date('2026-10-17T00:00:00')
    const nextDay = new Date('2026-10-18T00:00:00')
    nextDay.setDate(nextDay.getDate() + 1)
    const end = new Date(nextDay.getTime() - 1)

    await waitFor(() => {
      const latest = callsTo(calls, FEED_PATH).at(-1)
      expect(latest?.searchParams.get('since')).toBe(start.toISOString())
      expect(latest?.searchParams.get('until')).toBe(end.toISOString())
    })
    // A new range is a new first page: the old cursor must not carry into it.
    expect(callsTo(calls, FEED_PATH).at(-1)?.searchParams.get('cursor')).toBeNull()
    expect(await screen.findByText(/no events in that range/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /clear range/i }))

    await waitFor(() => {
      const latest = callsTo(calls, FEED_PATH).at(-1)
      expect(latest?.searchParams.get('since')).toBeNull()
      expect(latest?.searchParams.get('until')).toBeNull()
    })
  })

  it('retries a failed next page once, keeping the pages already read', async () => {
    let call = 0
    const calls = installFetch((request) => {
      if (request.path !== FEED_PATH) {
        return errorResponse(404, 'not_found', `No stub for ${request.path}`)
      }
      call += 1
      switch (call) {
        case 1:
          return jsonResponse(
            page([event({ id: 3 })], { next_cursor: 'cursor-1', has_more: true }),
          )
        case 2:
          return jsonResponse(
            page([event({ id: 2 })], { next_cursor: 'cursor-2', has_more: true }),
          )
        case 3:
          return errorResponse(500, 'internal_error', 'The server could not finish the request.')
        default:
          return jsonResponse(page([event({ id: 1 })]))
      }
    })

    render()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: /show older events/i }))
    await waitFor(() => expect(callsTo(calls, FEED_PATH)).toHaveLength(2))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /show older events/i })).toBeEnabled(),
    )

    await user.click(screen.getByRole('button', { name: /show older events/i }))
    await waitFor(() => expect(callsTo(calls, FEED_PATH)).toHaveLength(3))

    expect(await screen.findByText(/could not load older events/i)).toBeInTheDocument()
    // The events that were read are still shown; only the next page failed.
    expect(screen.getAllByText(/changed the status/i).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: /try again/i }))

    await waitFor(() => expect(callsTo(calls, FEED_PATH)).toHaveLength(4))
    // One request for the failed page, not one per page already loaded.
    expect(callsTo(calls, FEED_PATH)[3]?.searchParams.get('cursor')).toBe('cursor-2')
    await waitFor(() =>
      expect(screen.queryByText(/could not load older events/i)).not.toBeInTheDocument(),
    )
  })
})
