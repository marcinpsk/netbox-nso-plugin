/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (C) 2026 Marcin Zieba */
(function () {
  "use strict";
  var requests = new WeakMap();
  function selectionState(panel) {
    var form = panel.querySelector('[data-intend-form]');
    if (!form) return null;
    var options = new Map();
    form.querySelectorAll('[data-intend-option]').forEach(function (input) {
      options.set(input.name, { value: input.value, checked: input.checked, type: input.dataset.intendType || input.type });
    });
    return { rows: new Set(new FormData(form).getAll('intend_row')), options: options };
  }
  function restoreSelection(panel, state) {
    var form = panel.querySelector('[data-intend-form]');
    if (!form || !state) return;
    var missing = new Set(state.rows);
    panel.querySelectorAll('[data-intend-row]').forEach(function (input) {
      input.checked = state.rows.has(input.value);
      missing.delete(input.value);
    });
    form.querySelectorAll('[data-intend-option]').forEach(function (input) {
      var option = state.options.get(input.name);
      if (option) { input.value = option.value; input.checked = option.checked; }
    });
    if (!missing.size) return;
    var unavailable = document.createElement('div');
    unavailable.className = 'alert alert-warning small';
    var message = document.createElement('p');
    message.textContent = 'A selected native row is no longer available. Preview to check its current binding and permissions.';
    unavailable.append(message);
    missing.forEach(function (token) {
      var label = document.createElement('label'); label.className = 'form-check-label d-block mb-2';
      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.className = 'form-check-input';
      checkbox.name = 'intend_row'; checkbox.value = token; checkbox.checked = true; checkbox.dataset.intendRow = '';
      label.append(checkbox, ' Retain unavailable selection'); unavailable.append(label);
      state.options.forEach(function (option, name) {
        if (!name.endsWith('-' + token) || !option.value || (option.type === 'checkbox' && !option.checked)) return;
        var input = document.createElement('input'); input.type = 'hidden'; input.name = name;
        input.value = option.value; input.checked = option.checked;
        input.dataset.intendOption = ''; input.dataset.intendType = option.type; unavailable.append(input);
      });
    });
    form.prepend(unavailable);
  }
  function loadDifferences(panel, parameters) {
    var form = panel.querySelector('[data-differences-filter]');
    var generation = (requests.get(panel) || 0) + 1; requests.set(panel, generation);
    fetch(form.action + '?' + parameters.toString(), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.text(); })
      .then(function (html) {
        if (requests.get(panel) !== generation || !panel.isConnected) return;
        var state = selectionState(panel);
        var container = document.createElement('div'); container.innerHTML = html;
        var next = container.querySelector('[data-differences-panel]');
        if (!next) throw new Error('response has no differences panel');  // e.g. the login page after a session expiry
        restoreSelection(next, state); panel.replaceWith(next);
      })
      .catch(function () {
        if (requests.get(panel) !== generation || !panel.isConnected) return;
        var message = document.createElement('div'); message.className = 'text-danger small';
        message.textContent = 'Failed to load differences.'; panel.prepend(message);
      });
  }
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-intend-row]')) {
      var panel = event.target.closest('[data-differences-panel]');
      panel.querySelectorAll('[data-intend-row]').forEach(function (input) {
        if (input.value === event.target.value) input.checked = event.target.checked;
      });
    }
    var form = event.target.closest('[data-differences-filter]'); if (!form) return;
    loadDifferences(form.closest('[data-differences-panel]'), new URLSearchParams(new FormData(form)));
  });
  document.addEventListener('submit', function (event) {
    var form = event.target.closest('[data-differences-filter]'); if (!form) return;
    event.preventDefault();
    loadDifferences(form.closest('[data-differences-panel]'), new URLSearchParams(new FormData(form)));
  });
  document.addEventListener('click', function (event) {
    var link = event.target.closest('[data-differences-page]'); if (!link) return;
    event.preventDefault();
    loadDifferences(link.closest('[data-differences-panel]'), new URLSearchParams(link.search));
  });
})();
