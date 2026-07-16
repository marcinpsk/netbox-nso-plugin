/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-isis.js";

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
  root.className = "nso-grid nso-isis-" + section;
  root.dataset.jsonUrl = "/isis.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-isis-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({
    instances: { rows: section === "instances" ? [row] : [], counts: { all: 1 } },
    interfaces: { rows: section === "interfaces" ? [row] : [], counts: { all: 1 } },
  });
  document.body.append(root, payload);
  window.NSOGridIsis.mount(root, section);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_isis_instances;
  delete window.__nsoGrid_isis_interfaces;
});

afterEach(() => vi.unstubAllGlobals());

describe("IS-IS grid", () => {
  it("compacts an interface into binding and one safe config editor", () => {
    const row = {
      iface: { name: "Ethernet1", url: "/dcim/interfaces/1/" },
      af: "ipv4",
      process_tag: "CORE",
      circuit_type: "level-1-2",
      network_type: "point-to-point",
      metric: 25,
      passive: false,
      bfd_enabled: true,
      frr_enabled: true,
      frr_protection: "node",
      hello_auth: "md5",
      edit_url: "/overlay/isis_interface/7/edit-field/",
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
    expect(table.config.columns.reduce((sum, column) => sum + column.minWidth, 0)).toBeLessThanOrEqual(730);
    expect(config.textContent).toContain("point-to-point");
    expect(config.textContent).toContain("metric 25");
    expect(config.textContent).toContain("BFD");
    expect(config.textContent).toContain("FRR node");
    expect(config.textContent).toContain("auth md5");
    const editors = config.querySelectorAll(".nso-popedit");
    expect(editors).toHaveLength(2);
    expect(editors[0].getAttribute("data-pe-fields")).toContain("circuit_type:select:Circuit type");
    expect(editors[1].getAttribute("data-pe-fields")).toContain("frr_protection:select:FRR protection");
  });

  it("compacts an instance into process and editable core config", () => {
    const row = {
      process_tag: "CORE",
      instance: { label: "router-1 (CORE)", url: "/plugins/routing/isis/1/" },
      net: "49.0001.0000.0000.0001.00",
      is_type: "level-1-2",
      metric_style: "wide",
      overload_bit: false,
      fast_reroute: "ti-lfa",
      microloop_avoidance: true,
      area_auth: "md5 ✓",
      domain_auth: "—",
      edit_url: "/overlay/isis_instance/8/edit-field/",
      state: "in_sync",
    };
    const table = mount("instances", row);
    const config = table.config.columns.find((column) => column.field === "_config").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_process",
      "_config",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(config.textContent).toContain(row.net);
    expect(config.textContent).toContain("level-1-2");
    expect(config.textContent).toContain("wide");
    expect(config.textContent).toContain("TI-LFA");
    expect(config.textContent).toContain("area auth md5");
    const editors = config.querySelectorAll(".nso-popedit");
    expect(editors).toHaveLength(2);
    expect(editors[0].getAttribute("data-pe-fields")).toContain("net:text:NET");
    expect([...editors].every((edit) => !edit.getAttribute("data-pe-fields").includes("auth"))).toBe(true);
  });
});
