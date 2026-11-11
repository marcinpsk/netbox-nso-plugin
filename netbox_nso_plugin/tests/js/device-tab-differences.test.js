/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

const script = readFileSync(resolve(process.cwd(), 'netbox_nso_plugin/static/netbox_nso_plugin/nso-device-differences.js'), 'utf8');
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
  it('preserves a large selection without putting identities in filter URLs', async () => {
    const tokens = Array.from({ length: 50 }, (_, index) => `native-${index}-${'x'.repeat(250)}`);
    const catalog = checked => `<form data-intend-form>${tokens.map(token =>
      `<input type="checkbox" name="intend_row" value="${token}" ${checked ? 'checked' : ''} data-intend-row>`
    ).join('')}</form>`;
    document.body.innerHTML = panel('old row').replace('</div>', catalog(true) + '</div>');
    const next = panel('new row').replace('</div>', catalog(false) + '</div>');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => next })));
    document.querySelector('select').dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() => expect(document.body.textContent).toContain('new row'));
    expect(fetch.mock.calls[0][0].length).toBeLessThan(100);
    expect(new FormData(document.querySelector('[data-intend-form]')).getAll('intend_row')).toEqual(tokens);
  });

  it('carries selection on pagination and excludes the form CSRF token from the URL', async () => {
    document.body.innerHTML = panel('');
    document.querySelector('[data-differences-panel]').insertAdjacentHTML('beforeend',
      '<form data-intend-form><input type="checkbox" checked name="intend_row" value="native-row">' +
      '<input name="csrfmiddlewaretoken" value="form-only-token"></form>' +
      '<a data-differences-page href="?page=2">Next</a>');
    const next = panel('').replace('</div>', '<form data-intend-form><input name="csrfmiddlewaretoken" value="fresh-form-token"></form></div>');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => next })));
    document.querySelector('[data-differences-page]').click();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const parameters = new URL(fetch.mock.calls[0][0]).searchParams;
    expect(parameters.get('page')).toBe('2');
    expect(parameters.getAll('intend_row')).toEqual([]);
    expect(parameters.has('csrfmiddlewaretoken')).toBe(false);
    await vi.waitFor(() => expect(new FormData(document.querySelector('[data-intend-form]')).get('csrfmiddlewaretoken')).toBe('fresh-form-token'));
    expect(new FormData(document.querySelector('[data-intend-form]')).getAll('intend_row')).toEqual(['native-row']);
  });

  it('keeps the catalog and difference row checkboxes in agreement', () => {
    document.body.innerHTML = panel('<input type="checkbox" value="native-row" data-intend-row>');
    document.querySelector('[data-differences-panel]').insertAdjacentHTML('beforeend',
      '<input type="checkbox" value="native-row" data-intend-row>');
    const inputs = document.querySelectorAll('[data-intend-row]');
    inputs[0].checked = true;
    inputs[0].dispatchEvent(new Event('change', { bubbles: true }));
    expect(inputs[1].checked).toBe(true);
    inputs[1].checked = false;
    inputs[1].dispatchEvent(new Event('change', { bubbles: true }));
    expect(inputs[0].checked).toBe(false);
  });

  it('keeps unavailable selections and their explicit options across filters', async () => {
    document.body.innerHTML = panel('<tr><td>row</td></tr>');
    document.querySelector('[data-differences-panel]').insertAdjacentHTML('beforeend',
      '<form data-intend-form><input type="checkbox" checked name="intend_row" value="stable-native-identity">' +
      '<input name="lag_id-stable-native-identity" value="7" data-intend-option>' +
      '<input type="checkbox" checked name="restart-stable-native-identity" value="true" data-intend-option></form>');
    const next = panel('').replace('</div>', '<form data-intend-form></form></div>');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => next })));
    document.querySelector('select').dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const parameters = new URL(fetch.mock.calls[0][0]).searchParams;
    expect(parameters.getAll('intend_row')).toEqual([]);
    await vi.waitFor(() => expect(document.body.textContent).toContain('no longer available'));
    const values = new FormData(document.querySelector('[data-intend-form]'));
    expect(values.getAll('intend_row')).toEqual(['stable-native-identity']);
    expect(values.get('lag_id-stable-native-identity')).toBe('7');
    expect(values.get('restart-stable-native-identity')).toBe('true');
  });

  it('keeps a row selected while a filter request is in flight', async () => {
    const catalog = '<form data-intend-form><input type="checkbox" name="intend_row" value="native-row" data-intend-row></form>';
    document.body.innerHTML = panel('old row').replace('</div>', catalog + '</div>');
    let complete;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { complete = resolve; })));
    document.querySelector('select').dispatchEvent(new Event('change', { bubbles: true }));
    const checkbox = document.querySelector('[data-intend-row]');
    checkbox.checked = true; checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    complete({ ok: true, text: async () => panel('new row').replace('</div>', catalog + '</div>') });
    await vi.waitFor(() => expect(document.body.textContent).toContain('new row'));
    expect(document.querySelector('[data-intend-row]').checked).toBe(true);
  });

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

describe('intend catalog filter', () => {
  it('hides native rows that do not match and never submits on Enter', () => {
    document.body.innerHTML = panel('').replace('</div>', '<form data-intend-form><details data-intend-catalog>' +
      '<input type="search" data-intend-filter>' +
      '<div data-intend-choice><label><input type="checkbox" name="intend_row" value="a" data-intend-row> interface_mtu: Ethernet1</label></div>' +
      '<div data-intend-choice><label><input type="checkbox" name="intend_row" value="b" data-intend-row> bgp: 64500</label></div>' +
      '</details></form></div>');
    const filter = document.querySelector('[data-intend-filter]');
    const [ethernet, bgp] = document.querySelectorAll('[data-intend-choice]');
    filter.value = 'ETHERNET';
    filter.dispatchEvent(new Event('input', { bubbles: true }));
    expect([ethernet.hidden, bgp.hidden]).toEqual([false, true]);
    filter.value = '';
    filter.dispatchEvent(new Event('input', { bubbles: true }));
    expect([ethernet.hidden, bgp.hidden]).toEqual([false, false]);
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    filter.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
  });
});
