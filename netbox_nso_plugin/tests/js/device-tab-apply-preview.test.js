/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, it, onTestFinished, vi } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html'), 'utf8');
const parsed = new DOMParser().parseFromString(source, 'text/html');
const script = [...parsed.querySelectorAll('script')].find(node => node.textContent.includes('function renderIntentPanel(')).textContent;

async function open(payload, csrfToken = '') {
  document.body.innerHTML = `<div id="nso-job-status">
    <div class="alert"></div><span id="nso-job-spinner"></span><span id="nso-job-message"></span></div>
    <form class="nso-action-form nso-apply-form" data-preview-url="/preview/" action="/apply/" data-label="Apply Intent">${csrfToken ? `<input type="hidden" name="csrfmiddlewaretoken" value="${csrfToken}">` : ''}</form>
    <div id="nso-apply-modal" class="d-none"><div id="nso-apply-modal-body"></div><button id="nso-apply-confirm">Confirm Apply</button></div>`;
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => payload })));
  new Function(script)();
  document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
  await vi.waitFor(() => expect(document.getElementById('nso-apply-modal').classList.contains('d-none')).toBe(false));
  return document.getElementById('nso-apply-modal-body');
}
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

it('does not start a preview or Apply from the page URL', async () => {
  window.history.replaceState(null, '', '/?apply_preview=1');
  try {
    document.body.innerHTML = `<div id="nso-job-status">
      <div class="alert"></div><span id="nso-job-spinner"></span><span id="nso-job-message"></span></div>
      <form class="nso-action-form nso-apply-form" data-preview-url="/preview/" action="/apply/" data-label="Apply Intent"></form>
      <div id="nso-apply-modal" class="d-none"><div id="nso-apply-modal-body"></div><button id="nso-apply-confirm">Confirm Apply</button></div>`;
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ can_apply: true, nothing_pending: true, total: 0 }) }));
    vi.stubGlobal('fetch', fetchMock);
    new Function(script)();
    await new Promise(resolveTick => setTimeout(resolveTick, 0));
    expect(fetchMock).not.toHaveBeenCalled();
  } finally {
    window.history.replaceState(null, '', '/');
  }
});

it('does not infer per-row satisfaction from a nonempty whole-device diff', async () => {
  const body = await open({ total: 2, diff_available: true, device_diff: { device_intent: '+ device configuration' },
    routing_changes: [
      { category: 'IS-IS', item: 'example', scope: 'isis', staged_days: null },
      { category: 'Route policy', item: 'example-policy', scope: 'route_policy', staged_days: null },
    ] });
  expect(body.textContent).not.toContain('no device change');
});

it('shows unavailable rather than in-sync when the diff and itemized list are unavailable or empty', async () => {
  const body = await open({ total: 0, diff_available: false, device_diff: {}, nothing_pending: false });
  expect(body.textContent).toContain('unknown');
  expect(body.textContent).not.toContain('in sync');
});

it('does not infer that a pending row belongs to an empty current generation', async () => {
  const body = await open({ total: 1, diff_available: true, device_diff: {},
    routing_changes: [{ category: 'IS-IS', item: 'example', scope: 'isis', staged_days: null }] });
  expect(body.querySelector('.text-bg-info')).toBeNull();
});

it('does not mark staged switching rows unchanged before their Apply preparation', async () => {
  const body = await open({ total: 2, diff_available: true, device_diff: {},
    routing_changes: [
      { category: 'Switchport', item: 'example0', scope: null, staged_days: null },
      { category: 'LACP', item: 'example1', scope: null, staged_days: null },
    ] });
  expect(body.querySelectorAll('.text-bg-info')).toHaveLength(0);
});

it('shows a refused preview and its diff without HTML from catalog text', async () => {
  const text = 'inexpressible-on-ned: <placeholder> & catalog text';
  const body = await open({ total: 0, diff_available: false, preview_status: 'refused',
    device_diff: { device_intent: 'placeholder diff' }, refusal_text: text });
  expect(body.textContent).toContain(text);
  expect(body.textContent).toContain('placeholder diff');
  expect(body.querySelector('placeholder')).toBeNull();
});

it('shows the current execution advisory beside pending intent', async () => {
  const advisory = 'Execution unavailable; showing previous state.';
  const body = await open({ total: 1, diff_available: true, device_diff: {},
    changes: [{ interface: 'Ethernet1', attribute: 'description', device: 'placeholder',
      netbox: 'placeholder', advisory }] });
  expect(body.textContent).toContain(advisory);
});

it.each([
  { error: 'placeholder prior <ordinary-error> & failure' },
  { error: 'inexpressible-on-ned: placeholder prior catalog cause' },
  { refusal_text: 'inexpressible-on-ned: placeholder catalog refusal' },
])('shows the projected cause and advisory in separate preview elements: %j', async (projection) => {
  const advisory = 'Execution unavailable; showing previous state.';
  const cause = projection.refusal_text || projection.error;
  const body = await open({ total: 1, diff_available: true, device_diff: {},
    changes: [{ interface: 'Ethernet1', attribute: 'description', device: 'placeholder',
      netbox: 'placeholder', ...projection, advisory }] });
  expect(body.textContent).toContain(cause);
  expect(body.textContent).toContain(advisory);
  const notices = [...body.querySelectorAll('td p')];
  expect(notices.map(node => node.textContent)).toEqual([cause, advisory]);
  expect(body.querySelector('ordinary-error')).toBeNull();
});

it('does not call an empty refused preview nothing pending', async () => {
  const body = await open({ total: 0, diff_available: false, preview_status: 'refused',
    device_diff: {}, refusal_text: 'inexpressible-on-ned: catalog message' });
  expect(body.textContent).toContain('inexpressible-on-ned');
  expect(body.textContent).not.toContain('Nothing pending.');
});

it('wraps a long unbroken refusal token inside the preview table', async () => {
  const text = 'inexpressible-on-ned: ' + 'x'.repeat(200);
  const body = await open({ total: 1, diff_available: true, device_diff: {},
    routing_changes: [{ category: 'BGP', item: 'example', scope: 'bgp', staged_days: null, refusal_text: text }] });
  const notice = [...body.querySelectorAll('td p')].find(node => node.textContent === text);
  expect(notice.classList.contains('text-break')).toBe(true);
});

it('lets the staged badge wrap and uses a readable advisory color in the preview table', async () => {
  const body = await open({ total: 1, diff_available: true, device_diff: {},
    routing_changes: [{ category: 'BGP', item: 'example', scope: 'bgp', staged_days: 19, never_applied: true,
      advisory: 'Execution unavailable; showing previous state.' }] });
  const staged = [...body.querySelectorAll('td .badge')].find(node => node.textContent.startsWith('staged 19d'));
  expect(staged.classList.contains('text-wrap')).toBe(true);
  const advisory = [...body.querySelectorAll('td p')].find(node => node.textContent.startsWith('Execution unavailable'));
  expect(advisory.classList.contains('text-warning-emphasis')).toBe(true);
});

it('prepares one preview with POST and carries its reference through confirm', async () => {
  await open({ total: 1, preview_status: 'ready', can_apply: true, apply_preview: 'signed-placeholder',
    streams: { vlan: { status: 'admitted', label: 'VLAN' } },
    device_diffs: { native: '+ placeholder native', cli: '+ placeholder tree' },
    device_diff: { device_intent: '+ placeholder' }, diff_available: true });
  expect(fetch.mock.calls).toHaveLength(1);
  expect(fetch.mock.calls[0][1].method).toBe('POST');
  expect(document.querySelector('[name="apply_preview"]').value).toBe('signed-placeholder');
  fetch.mockImplementationOnce(async () => ({ ok: true, json: async () => ({ status: 'no_op' }) }));
  document.getElementById('nso-apply-confirm').click();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(new URL(fetch.mock.calls[1][0]).pathname).toBe('/apply/');
  expect(String(fetch.mock.calls[1][1].body)).toBe('apply_preview=signed-placeholder');
});

it('uses the rendered form token for preview and confirm when the cookie is HttpOnly', async () => {
  expect(document.cookie).not.toContain('csrftoken=');
  await open({ total: 1, preview_status: 'ready', can_apply: true, apply_preview: 'signed-placeholder',
    streams: { vlan: { status: 'admitted', label: 'VLAN' } }, diff_available: true }, 'rendered-placeholder-token');
  expect(fetch.mock.calls[0][1].headers['X-CSRFToken']).toBe('rendered-placeholder-token');
  fetch.mockImplementationOnce(async () => ({ ok: true, json: async () => ({ status: 'no_op' }) }));
  document.getElementById('nso-apply-confirm').click();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(fetch.mock.calls[1][1].headers['X-CSRFToken']).toBe('rendered-placeholder-token');
});

it('renders each stream verdict as a labelled badge and lists only the safe causes', async () => {
  const body = await open({ total: 0, preview_status: 'ready', can_apply: true, diff_available: true,
    streams: { ip: { status: 'blocked', label: 'Interface IP', skip: '', message: 'inexpressible-on-ned: placeholder <cause>' },
      interface_config: { status: 'admitted', label: 'Interface', skip: '', message: '' },
      lag: { status: 'not_evaluated', label: 'LACP', skip: 'already applied', message: '' },
      vlan: { status: 'not_evaluated', label: 'VLAN', skip: '', message: '' } } });
  const badges = [...body.querySelectorAll('.badge')].map(node => [node.textContent, node.className]);
  expect(badges).toEqual(expect.arrayContaining([
    ['Interface', 'badge text-bg-success'],
    ['Interface IP: blocked', 'badge text-bg-danger'],
    ['LACP: already applied', 'badge text-bg-info'],
    ['VLAN: not evaluated', 'badge text-bg-warning text-dark'],
  ]));
  const causes = [...body.querySelectorAll('li')].map(node => node.textContent);
  expect(causes).toEqual(['Interface IP: inexpressible-on-ned: placeholder <cause>']);
  expect(body.querySelector('cause')).toBeNull();
});


it('changes diff formats without preparing another selection', async () => {
  const body = await open({ total: 1, preview_status: 'ready', can_apply: true, apply_preview: 'signed-placeholder',
    streams: { vlan: { status: 'admitted', label: 'VLAN' } }, device_diffs: { native: '+ native placeholder', cli: '+ tree placeholder' },
    diff_available: true });
  body.querySelector('[data-diffmode="native"]').click();
  await vi.waitFor(() => expect(body.textContent).toContain('+ native placeholder'));
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each(['refused', 'unavailable', 'error'])('disables confirm for %s admission', async (status) => {
  await open({ total: 1, preview_status: status, can_apply: false, apply_preview: null,
    streams: { vlan: { status: 'not_evaluated', label: 'VLAN' } }, diff_available: false });
  expect(document.getElementById('nso-apply-confirm').disabled).toBe(true);
});

it('opens a fresh preview after stale admission and submits its new reference', async () => {
  const ready = { total: 1, preview_status: 'ready', can_apply: true, apply_preview: 'original-reference',
    streams: { vlan: { status: 'admitted', label: 'VLAN' } }, diff_available: true };
  await open(ready);
  fetch.mockImplementationOnce(async () => ({ok: false,
    json: async () => ({status: 'preflight_stale', message: 'Apply needs a fresh preview.'})}));
  fetch.mockImplementationOnce(async () => ({ok: true,
    json: async () => ({...ready, apply_preview: 'fresh-reference'})}));
  document.getElementById('nso-apply-confirm').click();
  await vi.waitFor(() => expect(document.querySelector('[name="apply_preview"]').value).toBe('fresh-reference'));
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(String(fetch.mock.calls[1][1].body)).toBe('apply_preview=original-reference');
  expect(fetch.mock.calls[2][1].method).toBe('POST');
  expect(document.getElementById('nso-job-message').textContent).toContain('fresh preview');
  fetch.mockImplementationOnce(async () => ({ok: true, json: async () => ({status: 'no_op'})}));
  document.getElementById('nso-apply-confirm').click();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
  expect(String(fetch.mock.calls[3][1].body)).toBe('apply_preview=fresh-reference');
});

it('uses the same form token for action and category POSTs without reading cookies', async () => {
  const block = [...parsed.querySelectorAll('script')].find(node => node.textContent.includes('function curState(pager)')).textContent;
  const start = block.lastIndexOf('(function () {', block.indexOf('function curState(pager)'));
  const pagerScript = block.slice(start, block.indexOf('})();', start) + 5);
  document.body.innerHTML = `<div id="nso-job-status"><div class="alert"></div>
    <span id="nso-job-spinner"></span><span id="nso-job-message"></span></div>
    <form class="nso-action-form" action="/sync/" data-label="Sync">
      <input type="hidden" name="csrfmiddlewaretoken" value="rendered-placeholder-token"></form>
    <div class="nso-cat-pager" data-url="/rows/"><div class="nso-cat-results"></div>
      <form class="nso-row-form" action="/accept/"><button>Accept</button></form></div>`;
  document.cookie = 'csrftoken=cookie-placeholder';
  onTestFinished(() => { document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT'; });
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ status: 'no_op' }),
    text: async () => '<div class="nso-cat-results">Updated row</div>' })));
  new Function(script + '\n' + pagerScript)();
  document.querySelector('.nso-action-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(fetch.mock.calls[0][1].headers['X-CSRFToken']).toBe('rendered-placeholder-token');
  document.querySelector('.nso-row-form').dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  await vi.waitFor(() => expect(document.body.textContent).toContain('Updated row'));
  expect(fetch.mock.calls[1][1].method).toBe('POST');
  expect(fetch.mock.calls[1][1].headers['X-CSRFToken']).toBe('rendered-placeholder-token');
});
