/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html'), 'utf8');
const parsed = new DOMParser().parseFromString(source, 'text/html');
const block = [...parsed.querySelectorAll('script')].find(node => node.textContent.includes('function loadDifferences(')).textContent;
// The tab script needs other page globals; run only the self-contained Differences IIFE.
const start = block.lastIndexOf('(function () {', block.indexOf('function loadDifferences('));
const script = block.slice(start, block.indexOf('})();', start) + 5);
const panel = rows => `<div data-differences-panel>
  <form method="get" action="/differences/" data-differences-filter>
    <select name="kind"><option value="">All kinds</option><option value="missing">missing</option></select>
  </form>
  <table><tbody>${rows}</tbody></table>
</div>`;

// Its listeners are delegated on document, so register them once for the whole file.
new Function(script)();

async function filterBy(body) {
  document.body.innerHTML = panel('<tr><td>old row</td></tr>');
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, text: async () => body })));
  const select = document.querySelector('select[name="kind"]');
  select.value = 'missing';
  select.dispatchEvent(new Event('change', { bubbles: true }));
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(new URL(fetch.mock.calls[0][0]).search).toBe('?kind=missing');
}

afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('differences panel filter', () => {
  it('replaces the panel with the fetched fragment', async () => {
    await filterBy(panel('<tr><td>new row</td></tr>'));
    await vi.waitFor(() => expect(document.body.textContent).toContain('new row'));
    expect(document.querySelectorAll('[data-differences-panel]')).toHaveLength(1);
    expect(document.body.textContent).not.toContain('old row');
  });

  it('keeps the panel and reports an error when the response has no panel', async () => {
    await filterBy('<html><body><form action="/login/"><input name="username"></form></body></html>');
    await vi.waitFor(() => expect(document.body.textContent).toContain('Failed to load differences.'));
    expect(document.querySelectorAll('[data-differences-panel]')).toHaveLength(1);
    expect(document.body.textContent).toContain('old row');
    expect(document.body.textContent).not.toContain('null');
  });
});
