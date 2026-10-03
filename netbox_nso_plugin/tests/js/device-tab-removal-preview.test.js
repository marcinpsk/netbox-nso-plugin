/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html'), 'utf8');
const parsed = new DOMParser().parseFromString(source, 'text/html');
const script = [...parsed.querySelectorAll('script')].find(node => node.textContent.includes('function renderApplyState(')).textContent;
const blockedTemplate = parsed.getElementById('nso-blocked-removal-tpl').outerHTML;
const block = (job = 41) => ({ scope: 'static_route', job_id: job, orphans: { route: [['198.18.0.0/24']] }, blocked_at: null });
const preview = (diff = '<edit-config>literal</edit-config>') => ({ diff_available: true, device_diff: diff ? { device_intent: diff } : {}, generation_id: 73, document_digest: 'a'.repeat(64) });
let blocks, response, poll;

async function mount() {
  document.body.innerHTML = `<div id="nso-job-activity" data-jobs-url="/jobs/"></div>
    <div id="nso-blocked-removals" data-preview-url="/preview/" class="d-none"></div>${blockedTemplate}`;
  vi.stubGlobal('setInterval', vi.fn(callback => { poll = callback; return 1; }));
  vi.stubGlobal('clearInterval', vi.fn());
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === '/preview/') return { ok: true, json: async () => response };
    return { ok: true, json: async () => ({ onboarded: true, running: { type: 'removal' }, blocked_removals: blocks }) };
  }));
  new Function(script)();
  await vi.waitFor(() => expect(document.querySelector('[data-slot="scope"]')?.textContent).toBe('static_route'));
}
const button = () => document.querySelector('[data-action="preview"]');
const result = () => document.querySelector('[data-slot="preview"]');
const previewCalls = () => fetch.mock.calls.filter(([url]) => url === '/preview/');

beforeEach(() => { blocks = [{ ...block(), preview: 'obsolete persisted native delta' }]; response = preview(); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = ''; });

describe('blocked removal current deployment preview', () => {
  it('fetches only on click and renders the current device diff as text', async () => {
    await mount();
    expect(previewCalls()).toHaveLength(0);
    expect(button()).not.toBeNull();
    expect(document.body.textContent).not.toContain('obsolete persisted native delta');
    expect(document.body.textContent).toContain('Force removal can differ');
    expect(document.querySelector('input[name="scope"]').value).toBe('static_route');
    button().click();
    await vi.waitFor(() => expect(result().textContent).toContain('<edit-config>literal</edit-config>'));
    expect(previewCalls()).toHaveLength(1);
    expect(previewCalls()[0][1].cache).toBe('no-store');
    expect(result().children).toHaveLength(0);
    expect(document.body.textContent).toContain('73');
    poll();
    await vi.waitFor(() => expect(fetch.mock.calls.length).toBe(3));
    expect(result().textContent).toContain('<edit-config>literal</edit-config>');
    expect(previewCalls()).toHaveLength(1);
  });

  it('keeps one loading request across polls and discards a response for a replaced job', async () => {
    await mount();
    let finish;
    fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    button().click();
    expect(button().disabled).toBe(true);
    const oldResult = result();
    poll();
    await vi.waitFor(() => expect(fetch.mock.calls.length).toBe(3));
    expect(result()).toBe(oldResult);
    expect(button().disabled).toBe(true);
    button().click();
    expect(previewCalls()).toHaveLength(1);
    blocks = [block(42)];
    poll();
    await vi.waitFor(() => expect(result()).not.toBe(oldResult));
    finish({ ok: true, json: async () => preview('old sensitive delta') });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(document.body.textContent).not.toContain('old sensitive delta');
    expect(button().disabled).toBe(false);
  });

  it.each([
    [preview(''), 'No device change'],
    [{ diff_available: false, device_diff: {} }, 'Preview unavailable'],
    [{ diff_available: true, device_diff: {}, generation_id: null, document_digest: null }, 'Preview unavailable'],
    [{ ...preview(), diff_available: false, diff_error: 'invalid_response' }, 'invalid adapter response'],
    [{ ...preview(), device_diff: { device_intent: 42 } }, 'Preview unavailable'],
  ])('distinguishes empty success from unavailable or malformed responses', async (value, message) => {
    response = value;
    await mount();
    button().click();
    await vi.waitFor(() => expect(result().textContent).toContain(message));
    expect(button().disabled).toBe(false);
  });

  it('discards a late response when the block disappears', async () => {
    await mount();
    let finish;
    fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    button().click();
    blocks = [];
    poll();
    await vi.waitFor(() => expect(document.getElementById('nso-blocked-removals').children).toHaveLength(0));
    finish({ ok: true, json: async () => preview('removed sensitive delta') });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(document.body.textContent).not.toContain('removed sensitive delta');
  });

  it('shows a transport failure without displaying exception content', async () => {
    await mount();
    fetch.mockRejectedValueOnce(new Error('sensitive raw error'));
    button().click();
    await vi.waitFor(() => expect(result().textContent).toContain('Preview unavailable'));
    expect(document.body.textContent).not.toContain('sensitive raw error');
    expect(button().disabled).toBe(false);
  });
});
