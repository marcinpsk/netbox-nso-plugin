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
      "_result",
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

  /* P6.6 — an owned row the apply failed must not read as an ordinary pending row with no
   * words. The server already gives it the apply_failed state; the grid owes the reason. */
  it("renders a failed route's own error message, not a bare state chip", () => {
    const row = {
      vrf: "global",
      prefix: "203.0.113.0/24",
      next_hop: "192.0.2.2",
      metric: 1,
      route: null,
      edit_url: null,
      state: "apply_failed",
      error: "static_route_send_failed: NED rejected the route",
      advisory: null,
    };
    const table = mount(row);
    const result = table.config.columns.find((column) => column.field === "_result").formatter(cell(row));

    expect(result.textContent).toContain("apply failed");
    expect(result.textContent).toContain("NED rejected the route");
    expect(result.querySelector(".badge").className).toContain("text-bg-danger");
  });

  /* P6.7 — `unproven` is a statement about EVIDENCE, not about ownership: it may render
   * neither green nor failed, and it must carry its reason rather than pass silently. */
  it("qualifies an unproven verdict with its advisory instead of a green or failed badge", () => {
    const row = {
      vrf: "global",
      prefix: "203.0.113.0/24",
      next_hop: "192.0.2.2",
      metric: 1,
      route: null,
      edit_url: null,
      state: "pending",
      error: null,
      advisory: "verification disabled — nothing proves this route landed",
    };
    const table = mount(row);
    const result = table.config.columns.find((column) => column.field === "_result").formatter(cell(row));
    const badgeClass = result.querySelector(".badge").className;

    expect(result.textContent).toContain("unproven");
    expect(result.textContent).toContain("nothing proves this route landed");
    expect(badgeClass).not.toContain("text-bg-success");
    expect(badgeClass).not.toContain("text-bg-danger");
  });

  it("leaves the result column empty for a route with no verdict to report", () => {
    const row = {
      vrf: "global",
      prefix: "203.0.113.0/24",
      next_hop: "192.0.2.2",
      metric: 1,
      route: null,
      edit_url: null,
      state: "in_sync",
      error: null,
      advisory: null,
    };
    const table = mount(row);
    const result = table.config.columns.find((column) => column.field === "_result").formatter(cell(row));

    expect(result.querySelector(".badge")).toBeNull();
    expect(result.textContent.trim()).toBe("—");
  });
});
