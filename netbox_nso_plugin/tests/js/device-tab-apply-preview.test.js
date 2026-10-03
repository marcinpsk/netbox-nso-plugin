/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html'), 'utf8');
const parsed = new DOMParser().parseFromString(source, 'text/html');
const script = [...parsed.querySelectorAll('script')].find(node => node.textContent.includes('function renderIntentPanel(')).textContent;

async function open(payload) {
  document.body.innerHTML = `<div id="nso-job-status" data-job-status-url="/jobs/0/">
    <div class="alert"></div><span id="nso-job-spinner"></span><span id="nso-job-message"></span></div>
    <form class="nso-action-form nso-apply-form" data-preview-url="/preview/"></form>
    <div id="nso-apply-modal" class="d-none"><div id="nso-apply-modal-body"></div></div>`;
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => payload })));
  new Function(script)();
  document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
  await vi.waitFor(() => expect(document.getElementById('nso-apply-modal').classList.contains('d-none')).toBe(false));
  return document.getElementById('nso-apply-modal-body');
}
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

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
