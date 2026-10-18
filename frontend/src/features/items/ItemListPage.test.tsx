/**
 * The work-item list.
 *
 * The behaviours worth testing here are the ones the brief asks a reviewer to check:
 * loading, empty and error states are all present, the error state offers a retry that
 * actually re-requests, a filter change produces a *server* query with the filter as a
 * parameter rather than filtering a loaded page, and the "what needs attention?" presets
 * are one click that produces that same kind of server query.
 */

import { describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { errorResponse, installFetch, jsonResponse } from '../../test/fetch'
import { ITEM_ID, OTHER_ITEM_ID, membership, page, summary } from '../../test/fixtures'
import { renderWithClient } from '../../test/render'
import { ItemListPage } from './ItemListPage'

function render() {
  return renderWithClient(<ItemListPage team={membership()} />)
}

describe('ItemListPage', () => {
  it('shows a loading state, then the items the server returned', async () => {
    installFetch(() =>
      jsonResponse(
        page([summary({ title: 'Refund the duplicate charge' }), summary({ id: OTHER_ITEM_ID, title: 'Rotate the signing key' })]),
      ),
    )

    render()

    expect(screen.getByRole('status')).toHaveTextContent(/loading work items/i)

    expect(await screen.findByText('Refund the duplicate charge')).toBeInTheDocument()
    expect(screen.getByText('Rotate the signing key')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says so when there is nothing to show', async () => {
    installFetch(() => jsonResponse(page([])))

    render()

    expect(await screen.findByText(/no work items yet/i)).toBeInTheDocument()
    expect(screen.getByText(/create the first one/i)).toBeInTheDocument()
  })

  it('reports a failure and re-requests when the retry is used', async () => {
    let attempt = 0
    const calls = installFetch(() => {
      attempt += 1
      if (attempt === 1) return errorResponse(500, 'internal_error', 'Unexpected failure.')
      return jsonResponse(page([summary({ title: 'Recovered item' })]))
    })

    render()

    expect(await screen.findByRole('alert')).toHaveTextContent(/something went wrong/i)

    await userEvent.setup().click(screen.getByRole('button', { name: /try again/i }))

    expect(await screen.findByText('Recovered item')).toBeInTheDocument()
    expect(calls).toHaveLength(2)
  })

  it('turns a filter into a server query rather than a client-side filter', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('checkbox', { name: 'Open' }))
    await user.click(screen.getByRole('checkbox', { name: 'Urgent' }))

    await waitFor(() => {
      const last = calls.at(-1)
      expect(last?.searchParams.getAll('status')).toEqual(['open'])
      expect(last?.searchParams.getAll('priority')).toEqual(['urgent'])
    })

    // The request carried the filter; nothing filtered the already-loaded rows.
    expect(screen.getByText('Refund the duplicate charge')).toBeInTheDocument()
  })

  it('clearing the filters asks for the unfiltered list again', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('checkbox', { name: 'Open' }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.has('status')).toBe(true))

    await user.click(screen.getByRole('button', { name: /clear filters/i }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.has('status')).toBe(false))
  })

  it('pages with the cursor the server issued', async () => {
    const calls = installFetch((request) => {
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
    // The first page is still there: a cursor appends, it does not replace.
    expect(screen.getByText('First page item')).toBeInTheDocument()

    const paged = calls.filter((call) => call.searchParams.get('cursor') === 'cursor-1')
    expect(paged).toHaveLength(1)
  })

  it('offers no "load more" when the server says there is nothing left', async () => {
    installFetch(() => jsonResponse(page([summary()], { has_more: false, next_cursor: null })))

    render()
    await screen.findByText('Refund the duplicate charge')

    expect(screen.queryByRole('button', { name: /load more/i })).not.toBeInTheDocument()
    expect(screen.getByText(/end of the list/i)).toBeInTheDocument()
  })

  it('shows the assignee, or says the item is unassigned', async () => {
    installFetch(() =>
      jsonResponse(
        page([
          summary({ id: ITEM_ID, title: 'Claimed one', assignee: { id: 'u-1', email: 'a@b.c', display_name: 'Nina Member' } }),
          summary({ id: OTHER_ITEM_ID, title: 'Loose one', assignee: null }),
        ]),
      ),
    )

    render()

    const claimed = await screen.findByText('Claimed one')
    expect(within(claimed.closest('li') as HTMLElement).getByText('Nina Member')).toBeInTheDocument()

    const loose = screen.getByText('Loose one')
    expect(within(loose.closest('li') as HTMLElement).getByText('Unassigned')).toBeInTheDocument()
  })
})

describe('ItemListPage — needs-attention presets', () => {
  it('turns "Assigned to me" into one server query', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('button', { name: 'Assigned to me' }))

    await waitFor(() => expect(calls.at(-1)?.searchParams.get('assignee')).toBe('me'))
    // The preset is a view, and the interface says which view is showing.
    expect(screen.getByRole('button', { name: 'Assigned to me' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('turns "Unassigned urgent" into status, priority and assignee filters', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('button', { name: 'Unassigned urgent' }))

    await waitFor(() => {
      const last = calls.at(-1)
      expect(last?.searchParams.getAll('status')).toEqual(['open'])
      expect(last?.searchParams.getAll('priority').sort()).toEqual(['high', 'urgent'])
      expect(last?.searchParams.get('assignee')).toBe('none')
    })
  })

  it('returns to everything in one click, and the server sees no filter', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('button', { name: 'Awaiting approval' }))
    await waitFor(() =>
      expect(calls.at(-1)?.searchParams.getAll('status')).toEqual(['awaiting_approval']),
    )

    await user.click(screen.getByRole('button', { name: 'All work' }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.has('status')).toBe(false))
    expect(screen.getByRole('button', { name: 'All work' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps the preset selected while a search term is typed', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))
    const user = userEvent.setup()

    render()
    await screen.findByText('Refund the duplicate charge')

    await user.click(screen.getByRole('button', { name: 'Assigned to me' }))
    await waitFor(() => expect(calls.at(-1)?.searchParams.get('assignee')).toBe('me'))

    await user.type(screen.getByRole('searchbox'), 'refund')

    // The search term is not part of a preset, so the preset does not flicker off.
    await waitFor(() => expect(calls.at(-1)?.searchParams.get('q')).toBe('refund'))
    expect(screen.getByRole('button', { name: 'Assigned to me' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // ...and the two filters travel together.
    expect(calls.at(-1)?.searchParams.get('assignee')).toBe('me')
  })
})
