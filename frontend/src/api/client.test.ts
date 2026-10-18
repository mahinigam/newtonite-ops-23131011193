/**
 * The HTTP client.
 *
 * These tests cover the parts of the contract the interface depends on: the error envelope
 * becoming a typed `ApiError`, the `ETag` being captured from the header, the bearer token
 * being attached, and the query parameters a filtered list produces. If any of these drift,
 * the failure would otherwise show up as a confusing screen rather than a failing test.
 */

import { afterEach, describe, expect, it } from 'vitest'

import { ApiError, api, getAccessToken, setAccessToken } from './client'
import { errorResponse, installFetch, jsonResponse } from '../test/fetch'
import { detail, page, summary } from '../test/fixtures'
afterEach(() => {
  setAccessToken(null)
})

describe('error handling', () => {
  it('turns the error envelope into a typed ApiError', async () => {
    installFetch(() => errorResponse(409, 'item_already_claimed', 'Somebody claimed this first.'))

    const failure = await api.claimItem('item-1').catch((error: unknown) => error)

    expect(failure).toBeInstanceOf(ApiError)
    const error = failure as ApiError
    expect(error.status).toBe(409)
    expect(error.code).toBe('item_already_claimed')
    expect(error.message).toBe('Somebody claimed this first.')
  })

  it('exposes validation failures as field errors', async () => {
    installFetch(() =>
      errorResponse(422, 'validation_failed', 'The request could not be validated.', [
        { field: 'body', message: 'String should have at least 1 character' },
      ]),
    )

    const failure = (await api.addComment('item-1', ' ', 'key').catch((error: unknown) => error)) as ApiError

    expect(failure.fieldErrors).toEqual([
      { field: 'body', message: 'String should have at least 1 character' },
    ])
  })

  it('ignores malformed details rather than throwing', async () => {
    installFetch(() => errorResponse(422, 'validation_failed', 'Nope.', 'not-a-list'))

    const failure = (await api.me().catch((error: unknown) => error)) as ApiError

    expect(failure.fieldErrors).toEqual([])
  })

  it('keeps the status when the body is not JSON', async () => {
    installFetch(() => new Response('<html>502 Bad Gateway</html>', { status: 502 }))

    const failure = (await api.me().catch((error: unknown) => error)) as ApiError

    expect(failure.status).toBe(502)
    expect(failure.code).toBe('http_error')
    expect(failure.message).toContain('502')
  })

  it('reports an unreachable server as a network error, not a crash', async () => {
    installFetch(() => {
      throw new TypeError('Failed to fetch')
    })

    const failure = (await api.me().catch((error: unknown) => error)) as ApiError

    expect(failure.code).toBe('network_error')
    expect(failure.status).toBe(0)
    expect(failure.isAuthenticationFailure).toBe(false)
  })

  it('flags a 401 as an authentication failure', async () => {
    installFetch(() => errorResponse(401, 'session_expired', 'Your session has expired.'))

    const failure = (await api.me().catch((error: unknown) => error)) as ApiError

    expect(failure.isAuthenticationFailure).toBe(true)
  })
})

describe('requests', () => {
  it('attaches the bearer token once a session exists', async () => {
    const calls = installFetch(() => jsonResponse({ user: {}, memberships: [] }))

    await api.me()
    expect(calls[0]?.headers['authorization']).toBeUndefined()

    setAccessToken('tok_abc')
    await api.me()
    expect(calls[1]?.headers['authorization']).toBe('Bearer tok_abc')
    expect(getAccessToken()).toBe('tok_abc')
  })

  it('captures the ETag from the response header', async () => {
    installFetch(() =>
      jsonResponse(summary(), { headers: { ETag: '"7"' } }),
    )

    const response = await api.item('item-1')

    expect(response.etag).toBe('"7"')
    expect(response.data.version).toBe(1)
  })

  it('sends repeated filter parameters and drops empty ones', async () => {
    const calls = installFetch(() => jsonResponse(page([summary()])))

    await api.items(
      'team-1',
      {
        statuses: ['open', 'in_progress'],
        priorities: ['urgent'],
        types: [],
        assignee: 'none',
        search: '',
      },
      null,
      25,
    )

    const call = calls[0]
    expect(call?.searchParams.getAll('status')).toEqual(['open', 'in_progress'])
    expect(call?.searchParams.getAll('priority')).toEqual(['urgent'])
    expect(call?.searchParams.has('type')).toBe(false)
    expect(call?.searchParams.get('assignee')).toBe('none')
    // An empty search and a null cursor are absent rather than sent as empty strings.
    expect(call?.searchParams.has('q')).toBe(false)
    expect(call?.searchParams.has('cursor')).toBe(false)
    expect(call?.searchParams.get('limit')).toBe('25')
  })

  it('sends the cursor verbatim when paging', async () => {
    const calls = installFetch(() => jsonResponse(page([], { has_more: false })))

    await api.comments('item-1', 'eyJjIjoiMjAyNi0xMC0xNSJ9', 10)

    expect(calls[0]?.searchParams.get('cursor')).toBe('eyJjIjoiMjAyNi0xMC0xNSJ9')
    expect(calls[0]?.searchParams.get('limit')).toBe('10')
  })

  it('sends the idempotency key and the If-Match version on conditional writes', async () => {
    const calls = installFetch(() => jsonResponse(detail(), { headers: { ETag: '"8"' } }))

    await api.addComment('item-1', 'A note', 'key-123')
    await api.updateItem('item-1', { priority: 'high' }, '"7"')

    expect(calls[0]?.headers['idempotency-key']).toBe('key-123')
    expect(calls[0]?.body).toEqual({ body: 'A note' })
    expect(calls[1]?.headers['if-match']).toBe('"7"')
    expect(calls[1]?.method).toBe('PATCH')
    expect(calls[1]?.body).toEqual({ priority: 'high' })
  })
})
