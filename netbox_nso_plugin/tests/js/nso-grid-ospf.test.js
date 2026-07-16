/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-ospf.js";

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

function mount(section, row) {
  const root = document.createElement("div");
  root.className = "nso-grid nso-ospf-" + section;
  root.dataset.jsonUrl = "/ospf.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-ospf-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({
    instances: { rows: section === "instances" ? [row] : [], counts: { all: 1 } },
    interfaces: { rows: section === "interfaces" ? [row] : [], counts: { all: 1 } },
  });
  document.body.append(root, payload);
  window.NSOGridOspf.mount(root, section);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_ospf_instances;
  delete window.__nsoGrid_ospf_interfaces;
});

afterEach(() => vi.unstubAllGlobals());

describe("OSPF grid", () => {
  it("compacts an instance into process and editable router-ID columns", () => {
    const row = {
      process_id: "7",
      vrf: "global",
      router_id: "192.0.2.7",
      instance: { label: "7 (192.0.2.7)", url: "/plugins/routing/ospf/7/" },
      edit_url: "/overlay/ospf_instance/7/edit-field/",
      state: "in_sync",
    };
    const table = mount("instances", row);
    const router = table.config.columns.find((column) => column.field === "router_id").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_process",
      "router_id",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(router.textContent).toContain("192.0.2.7");
    expect(router.querySelector(".nso-popedit").getAttribute("data-pe-fields")).toBe(
      "router_id:text:Router ID",
    );
  });

  it("compacts interface details into binding and one multi-field editor", () => {
    const row = {
      iface: { name: "Ethernet1", url: "/dcim/interfaces/1/" },
      process_id: "7",
      area_id: "0.0.0.0",
      network_type: "point-to-point",
      cost: 25,
      passive: true,
      edit_url: "/overlay/ospf_interface/8/edit-field/",
      state: "in_sync",
    };
    const table = mount("interfaces", row);
    const config = table.config.columns.find((column) => column.field === "_config").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_iface",
      "_binding",
      "_config",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(table.config.columns.reduce((sum, column) => sum + column.minWidth, 0)).toBeLessThanOrEqual(710);
    expect(config.textContent).toContain("point-to-point");
    expect(config.textContent).toContain("cost 25");
    expect(config.textContent).toContain("passive");
    const edit = config.querySelector(".nso-popedit");
    expect(edit.getAttribute("data-pe-fields")).toContain("area_id:text:Area");
    expect(edit.getAttribute("data-pe-fields")).toContain("network_type:select:Network type");
    expect(JSON.parse(edit.getAttribute("data-pe-o-network_type"))).toHaveLength(5);
  });
});
