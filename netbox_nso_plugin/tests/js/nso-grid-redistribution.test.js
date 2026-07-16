/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-redistribution.js";

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
  root.className = "nso-grid nso-redist";
  root.dataset.jsonUrl = "/redistribution.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-redist-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({ rows: [row], counts: { all: 1, drift: 0, pending: 0 } });
  document.body.append(root, payload);
  window.NSOGridRedistribution.mount(root);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_redistribution;
});

afterEach(() => vi.unstubAllGlobals());

describe("redistribution grid", () => {
  it("compacts protocol identity and exposes only policy knobs inline", () => {
    const row = {
      dest_protocol: "ospf",
      dest_ref: "10",
      source_protocol: "static",
      source_ref: "",
      route_map: "RM-STATIC",
      metric: 25,
      metric_type: "1",
      metric_type_options: [
        { value: "", label: "Default" },
        { value: "1", label: "Type 1" },
        { value: "2", label: "Type 2" },
      ],
      edit_url: "/overlay/redistribution/7/edit-field/",
      diff_url: "/redistribution/7/diff/",
      state: "in_sync",
    };
    const table = mount(row);
    const destination = table.config.columns.find((column) => column.field === "_destination").formatter(cell(row));
    const source = table.config.columns.find((column) => column.field === "_source").formatter(cell(row));
    const policy = table.config.columns.find((column) => column.field === "_policy").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_destination",
      "_source",
      "_policy",
      "state",
      "last_sync",
      "diff_url",
    ]);
    expect(destination.textContent).toContain("OSPF");
    expect(destination.textContent).toContain("10");
    expect(source.textContent).toContain("Static");
    expect(policy.textContent).toContain("RM-STATIC");
    expect(policy.textContent).toContain("metric 25");
    expect(policy.textContent).toContain("type 1");
    const edit = policy.querySelector(".nso-popedit");
    expect(edit.dataset.peUrl).toBe(row.edit_url);
    expect(edit.dataset.peFields).toBe("route_map:text:Route map,metric:text:Metric,metric_type:select:Metric type");
    expect(JSON.parse(edit.getAttribute("data-pe-o-metric_type"))).toEqual(row.metric_type_options);
  });

  it("keeps unlinked redistribution rows read-only", () => {
    const row = {
      dest_protocol: "bgp",
      dest_ref: "64512/global/ipv4-unicast",
      source_protocol: "connected",
      route_map: null,
      metric: null,
      metric_type: null,
      edit_url: null,
      state: "imported",
    };
    const table = mount(row);
    const policy = table.config.columns.find((column) => column.field === "_policy").formatter(cell(row));

    expect(policy.querySelector(".nso-popedit")).toBeNull();
    expect(policy.textContent).toContain("default policy");
  });
});
