/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-bfd.js";

class FakeTabulator {
  constructor(el, config) {
    this.el = el;
    this.config = config;
    this.handlers = {};
    FakeTabulator.instances.push(this);
  }
  on(event, fn) {
    (this.handlers[event] = this.handlers[event] || []).push(fn);
  }
  destroy() {}
  hideColumn() {}
  toggleColumn() {}
  setFilter() {}
  clearFilter() {}
}
FakeTabulator.instances = [];

function mount(row) {
  const root = document.createElement("div");
  root.className = "nso-grid nso-bfd";
  root.dataset.jsonUrl = "/bfd.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-bfd-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({ rows: [row], counts: { all: 1, drift: 0, pending: 0 } });
  document.body.append(root, payload);
  window.NSOGridBfd.mount(root);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_bfd;
});

afterEach(() => vi.unstubAllGlobals());

describe("BFD grid", () => {
  it("uses one compact editable config column and lets the page own scrolling", () => {
    const row = {
      iface: { name: "ae1", url: "/dcim/interfaces/1/" },
      min_tx: 300,
      min_rx: 400,
      multiplier: 3,
      micro_bfd: true,
      edit_url: "/overlay/bfd/7/edit-field/",
      state: "in_sync",
    };
    const table = mount(row);
    const config = table.config.columns.find((column) => column.field === "_mode");
    const rendered = config.formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_iface",
      "_mode",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(rendered.textContent).toContain("micro-BFD");
    expect(rendered.textContent).toContain("TX 300");
    expect(rendered.textContent).toContain("RX 400");
    expect(rendered.textContent).toContain("×3");
    const edit = rendered.querySelector(".nso-popedit");
    expect(edit.getAttribute("data-pe-url")).toBe(row.edit_url);
    expect(edit.getAttribute("data-pe-fields")).toContain("micro_bfd:select:Mode");
    expect(JSON.parse(edit.getAttribute("data-pe-o-micro_bfd"))).toHaveLength(2);
  });

  it("shows device defaults when all three timers are absent", () => {
    const row = {
      iface: { name: "ae2", url: "/dcim/interfaces/2/" },
      min_tx: null,
      min_rx: null,
      multiplier: null,
      micro_bfd: false,
      edit_url: "/overlay/bfd/8/edit-field/",
      state: "in_sync",
    };
    const table = mount(row);
    const rendered = table.config.columns.find((column) => column.field === "_mode").formatter(cell(row));

    expect(rendered.textContent).toContain("device defaults");
    expect(rendered.querySelector('[data-pe-v-min_tx]').getAttribute("data-pe-v-min_tx")).toBe("");
  });
});
