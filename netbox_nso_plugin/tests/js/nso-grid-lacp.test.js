/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-lacp.js";

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
  root.className = "nso-grid nso-lacp";
  root.dataset.jsonUrl = "/lacp.json";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  const payload = document.createElement("script");
  payload.id = "nso-lacp-data";
  payload.type = "application/json";
  payload.textContent = JSON.stringify({ rows: [row], counts: { all: 1, drift: 0, pending: 0 } });
  document.body.append(root, payload);
  window.NSOGridLACP.mount(root);
  return FakeTabulator.instances.at(-1);
}

function cell(row) {
  return { getRow: () => ({ getData: () => row }) };
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  delete window.__nsoGrid_lacp;
});

afterEach(() => vi.unstubAllGlobals());

describe("LACP grid", () => {
  it("compacts bundle parameters and keeps system identity read-only", () => {
    const row = {
      bundle: { name: "Port-channel10", url: "/dcim/interfaces/10/" },
      lag_id: 10,
      min_links: 2,
      system_priority: 100,
      system_id: "02:00:00:00:00:10",
      timer: "fast",
      admin_key: 10,
      edit_url: "/overlay/lacp_bundle/10/edit-field/",
      members: [],
      state: "in_sync",
    };
    const table = mount(row);
    const bundle = table.config.columns.find((column) => column.field === "_bundle").formatter(cell(row));
    const parameters = table.config.columns.find((column) => column.field === "_parameters").formatter(cell(row));

    expect(table.config.maxHeight).toBe(false);
    expect(table.config.columns.map((column) => column.field)).toEqual([
      "_bundle",
      "_parameters",
      "_members",
      "state",
      "last_sync",
      "accept_url",
    ]);
    expect(bundle.querySelector('a[href="/dcim/interfaces/10/"]').textContent).toBe("Port-channel10");
    expect(bundle.textContent).toContain("LAG 10");
    expect(parameters.textContent).toContain("min 2");
    expect(parameters.textContent).toContain("priority 100");
    expect(parameters.textContent).toContain("fast");
    expect(parameters.textContent).toContain("key 10");
    expect(parameters.textContent).toContain("02:00:00:00:00:10");
    const edit = parameters.querySelector(".nso-popedit");
    expect(edit.dataset.peFields).toBe(
      "min_links:text:Min links,system_priority:text:System priority,timer:select:Timer,admin_key:text:Admin key",
    );
    expect(edit.dataset.peFields).not.toContain("system_id");
  });

  it("renders member mode and priority editors without changing membership", () => {
    const row = {
      bundle: { name: "ae0", url: "/dcim/interfaces/20/" },
      lag_id: 0,
      members: [
        {
          interface: { name: "ge-0/0/0", url: "/dcim/interfaces/21/" },
          mode: "active",
          port_priority: 32,
          edit_url: "/overlay/lacp_member/21/edit-field/",
        },
      ],
      state: "imported",
    };
    const table = mount(row);
    const members = table.config.columns.find((column) => column.field === "_members").formatter(cell(row));

    expect(members.querySelector('a[href="/dcim/interfaces/21/"]').textContent).toBe("ge-0/0/0");
    expect(members.textContent).toContain("active");
    expect(members.textContent).toContain("pri 32");
    const edit = members.querySelector(".nso-popedit");
    expect(edit.dataset.peFields).toBe("mode:select:Mode,port_priority:text:Port priority");
    expect(edit.dataset.peUrl).toBe(row.members[0].edit_url);
    expect(edit.dataset).not.toHaveProperty("peVInterface");
  });
});
