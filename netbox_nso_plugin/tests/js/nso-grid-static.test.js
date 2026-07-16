/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-static.js";

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
  root.className = "nso-grid nso-static";
  root.dataset.jsonUrl = "/static.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-static-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({ rows: [row], counts: { all: 1, drift: 0, pending: 0 } });
  document.body.append(root, payload);
  window.NSOGridStatic.mount(root);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_static;
});

afterEach(() => vi.unstubAllGlobals());

describe("static route grid", () => {
  it("combines destination identity and exposes non-identity policy fields inline", () => {
    const row = {
      vrf: "CUSTOMER-A",
      prefix: "198.51.100.0/24",
      next_hop: "192.0.2.1",
      metric: 25,
      permanent: true,
      tag: 120,
      route: { url: "/plugins/routing/static-routes/7/", label: "198.51.100.0/24" },
      edit_url: "/overlay/static_route/7/edit-field/",
      state: "in_sync",
    };
    const table = mount(row);
    const destination = table.config.columns.find((column) => column.field === "_destination").formatter(cell(row));
    const policy = table.config.columns.find((column) => column.field === "_policy").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_destination",
      "next_hop",
      "_policy",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(destination.querySelector('a[href="/plugins/routing/static-routes/7/"]').textContent).toBe(
      "198.51.100.0/24",
    );
    expect(destination.textContent).toContain("VRF CUSTOMER-A");
    expect(policy.textContent).toContain("metric 25");
    expect(policy.textContent).toContain("permanent");
    expect(policy.textContent).toContain("tag 120");
    const edit = policy.querySelector(".nso-popedit");
    expect(edit.dataset.peFields).toBe("metric:text:Metric,permanent:select:Permanent,tag:text:Tag");
    expect(edit.getAttribute("data-pe-v-permanent")).toBe("True");
  });

  it("keeps unresolved routes read-only while retaining their observed identity", () => {
    const row = {
      vrf: "global",
      prefix: "203.0.113.0/24",
      next_hop: "192.0.2.2",
      metric: null,
      permanent: null,
      tag: null,
      route: null,
      edit_url: null,
      state: "imported",
    };
    const table = mount(row);
    const destination = table.config.columns.find((column) => column.field === "_destination").formatter(cell(row));
    const policy = table.config.columns.find((column) => column.field === "_policy").formatter(cell(row));

    expect(destination.querySelector("a")).toBeNull();
    expect(destination.textContent).toContain("203.0.113.0/24");
    expect(policy.querySelector(".nso-popedit")).toBeNull();
  });
});
