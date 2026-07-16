/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-route-policy.js";

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
  root.className = "nso-grid nso-rp";
  root.dataset.jsonUrl = "/route-policy.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-rp-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({ rows: [row], counts: { all: 1, drift: 0, pending: 0 } });
  document.body.append(root, payload);
  window.NSOGridRoutePolicy.mount(root);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_route_policy;
});

afterEach(() => vi.unstubAllGlobals());

describe("route-policy grid", () => {
  it("merges the native object into an editable route-map name column", () => {
    const row = {
      family: "route_map",
      name: "RM-EDGE",
      obj: { label: "RM-EDGE", url: "/plugins/routing/route-maps/7/" },
      edit_url: "/overlay/route_map_name/9/edit-field/",
      per_device: true,
      unsupported: [],
      state: "in_sync",
      diff_url: "/diff/9/",
      versions_url: "/versions/9/",
    };
    const table = mount(row);
    const policy = table.config.columns.find((column) => column.field === "name");
    const rendered = policy.formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "family",
      "name",
      "state",
      "last_sync",
      "diff_url",
    ]);
    expect(rendered.querySelector('a[href="/plugins/routing/route-maps/7/"]').textContent).toBe("RM-EDGE");
    expect(rendered.textContent).toContain("per-device");
    const edit = rendered.querySelector(".nso-popedit");
    expect(edit.dataset.peUrl).toBe(row.edit_url);
    expect(edit.dataset.peFields).toBe("object_name:text:Name");
    expect(edit.getAttribute("data-pe-v-object_name")).toBe("RM-EDGE");
  });

  it("does not offer rename for other route-policy families", () => {
    const row = {
      family: "prefix_list",
      name: "PL-EDGE",
      obj: { label: "PL-EDGE", url: "/plugins/routing/prefix-lists/8/" },
      edit_url: null,
      per_device: false,
      unsupported: [],
      state: "in_sync",
    };
    const table = mount(row);
    const rendered = table.config.columns.find((column) => column.field === "name").formatter(cell(row));

    expect(rendered.querySelector(".nso-popedit")).toBeNull();
    expect(rendered.querySelector("a").textContent).toBe("PL-EDGE");
  });
});
