/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-bgp.js";

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
  root.className = "nso-grid nso-bgp-" + section;
  root.dataset.jsonUrl = "/bgp.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-bgp-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({
    peers: { rows: section === "peers" ? [row] : [], counts: { all: 1 } },
    templates: { rows: section === "templates" ? [row] : [], counts: { all: 1 } },
  });
  document.body.append(root, payload);
  window.NSOGridBgp.mount(root, section);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_bgp_peers;
  delete window.__nsoGrid_bgp_templates;
});

afterEach(() => vi.unstubAllGlobals());

describe("BGP grid", () => {
  it("compacts peer identity and session details into a laptop-width grid", () => {
    const row = {
      asn: "64512",
      vrf: "global",
      peer_address: "192.0.2.20",
      remote_as: "64513",
      enabled: true,
      disabled: false,
      peer: { label: "192.0.2.20/32 (AS64513)", url: "/plugins/routing/bgp/peer/1/" },
      peer_group: "EDGE",
      local_as: "64514",
      source: "Loopback0",
      ttl: 2,
      bfd_enabled: true,
      address_families: [{ af: "ipv4-unicast", enabled: true, inbound: "RM-IN", outbound: "RM-OUT" }],
      edit_url: "/overlay/bgp_peer/7/edit-field/",
      state: "in_sync",
    };
    const table = mount("peers", row);
    const session = table.config.columns.find((column) => column.field === "_session").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_local",
      "_peer",
      "_session",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(table.config.columns.reduce((sum, column) => sum + column.minWidth, 0)).toBeLessThanOrEqual(710);
    expect(session.textContent).toContain("remote AS 64513");
    expect(session.textContent).toContain("EDGE");
    expect(session.textContent).toContain("Loopback0");
    expect(session.textContent).toContain("ipv4-unicast");
    expect(session.querySelector('[title*="in RM-IN"]')).not.toBeNull();
    const edit = session.querySelector(".nso-popedit");
    expect(edit.getAttribute("data-pe-fields")).toBe("remote_as_str:text:Remote AS,enabled:select:State");
  });

  it("shows peer-group templates without assuming they have detail URLs", () => {
    const row = {
      template_name: "EDGE-PEERS",
      remote_as: "64530",
      template: { label: "EDGE-PEERS" },
      address_families: [{ af: "ipv6-unicast", enabled: true, inbound: "PL6-IN", outbound: null }],
      state: "in_sync",
    };
    const table = mount("templates", row);
    const config = table.config.columns.find((column) => column.field === "_config").formatter(cell(row));

    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_template",
      "_config",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(config.textContent).toContain("remote AS 64530");
    expect(config.textContent).toContain("ipv6-unicast");
    expect(config.querySelector('[title*="in PL6-IN"]')).not.toBeNull();
    expect(config.querySelector(".nso-popedit")).toBeNull();
  });

  it("summarizes large address-family sets instead of making a tall peer row", () => {
    const row = {
      asn: "64512",
      vrf: "global",
      peer_address: "192.0.2.40",
      remote_as: "64513",
      enabled: true,
      address_families: [
        { af: "ipv4-unicast", enabled: true, inbound: "RM4-IN" },
        { af: "ipv6-unicast", enabled: true },
        { af: "vpnv4-unicast", enabled: true },
        { af: "vpnv6-unicast", enabled: true, outbound: "RM6-OUT" },
      ],
      state: "in_sync",
    };
    const table = mount("peers", row);
    const session = table.config.columns.find((column) => column.field === "_session").formatter(cell(row));

    expect(session.textContent).toContain("4 AFs");
    expect(session.textContent).not.toContain("vpnv6-unicast");
    expect(session.querySelector('[title*="vpnv6-unicast"]')).not.toBeNull();
    expect(session.querySelector('[title*="out RM6-OUT"]')).not.toBeNull();
  });
});
