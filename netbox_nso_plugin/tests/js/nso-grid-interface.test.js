/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Interfaces-panel column definitions (nso-grid-interface.js).
 *
 * The regression that forced these cells out of the template and under test:
 * an attr cell's INTENT lives on dcim.Interface (netbox_value) while the overlay
 * row mirrors the device (value). The grid displayed, sorted and — because
 * Tabulator's inline editor prefills from the flattened field — EDITED the stale
 * device mirror: after saving a new description the cell kept the old text and
 * re-opening the editor offered the old text back (live on device 55, 1/1/c1/1).
 * The cell must show and prefill what NetBox holds; the device's differing value
 * is an annotation, not the value.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";
import "../../static/netbox_nso_plugin/nso-grid-interface.js";

const G = window.NSOGrid;

/* Real-shape Tabulator stand-in — records the config mount() hands it. */
class FakeTabulator {
  constructor(el, config) {
    this.el = el;
    this.config = config;
    this.data = config.data;
    this.handlers = {};
    FakeTabulator.instances.push(this);
  }
  on(event, fn) {
    (this.handlers[event] = this.handlers[event] || []).push(fn);
  }
  destroy() {}
  replaceData(rows) {
    this.data = rows;
  }
  hideColumn() {}
  toggleColumn() {}
  setFilter() {}
  clearFilter() {}
}
FakeTabulator.instances = [];

function ifaceRoot() {
  const root = document.createElement("div");
  root.className = "nso-grid nso-ifg";
  root.dataset.jsonUrl = "/json";
  root.dataset.mtuEditUrl = "/overlay/interface_mtu/0/";
  root.innerHTML = '<div class="nso-grid-msg"></div><div class="nso-grid-table"></div>';
  document.body.appendChild(root);
  return root;
}

function embedPayload(rows) {
  const s = document.createElement("script");
  s.id = "nso-ifg-data";
  s.type = "application/json";
  s.textContent = JSON.stringify({ rows, counts: { all: rows.length, drift: 0, pending: 0 } });
  document.body.appendChild(s);
}

function row(over) {
  return Object.assign(
    {
      iface: { id: 1, name: "1/1/c1/1", url: "/dcim/interfaces/1/" },
      enabled: null,
      description: null,
      mtu: null,
      ips: [],
      switchport: null,
      state: "pending",
    },
    over,
  );
}

/* The user's exact case: description edited in NetBox (intent pushed, pending
 * apply) while the overlay still mirrors what the device had at last sync. */
const PENDING_DESC = {
  pk: 11,
  value: "old device text",
  netbox_value: "uplink to sw01 (new)",
  status: "accepted",
  kind: "pending",
  label: "pending apply",
  owned: true,
  accept_url: null,
  edit_url: "/if-edit/11/",
};

function mountRows(rows) {
  embedPayload(rows);
  const root = ifaceRoot();
  window.NSOGridInterface.mount(root);
  const table = FakeTabulator.instances[FakeTabulator.instances.length - 1];
  return {
    root,
    table,
    col(field) {
      return table.config.columns.find((c) => c.field === field);
    },
  };
}

function fakeCell(rowData) {
  return { getRow: () => ({ getData: () => rowData }) };
}

function parse(html) {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host;
}

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  Object.keys(window)
    .filter((k) => k.startsWith("__nsoGrid"))
    .forEach((k) => delete window[k]);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mount wiring", () => {
  it("returns null without a root or without the embedded payload", () => {
    expect(window.NSOGridInterface.mount(null)).toBeNull();
    expect(window.NSOGridInterface.mount(ifaceRoot())).toBeNull(); // no #nso-ifg-data
  });

  it("mounts the grid with the embedded rows and the interface column set", () => {
    const { table } = mountRows([row({ description: Object.assign({}, PENDING_DESC) })]);
    expect(table.data).toHaveLength(1);
    const fields = table.config.columns.map((c) => c.field);
    expect(fields).toEqual(["_name", "_enabled", "_desc", "mtu", "ips", "switchport", "state"]);
  });

  it("builds the MTU popedit anchor against this row's overlay pk", () => {
    const mtu = { pk: 77, status: "imported", kind: "drift", label: "drift", l2: 9216, ip: 9000, mpls: null };
    const { col } = mountRows([row({ mtu })]);
    const out = col("mtu").formatter(fakeCell(row({ mtu })));
    const a = out.querySelector("a.nso-popedit");
    expect(a.getAttribute("data-pe-url")).toBe("/overlay/interface_mtu/77/");
    expect(a.getAttribute("data-pe-v-l2_mtu")).toBe("9216");
  });

  it("links a materialized IP and builds the local/peer popedit", () => {
    const ip = {
      pk: 31,
      address: "198.18.30.0/31",
      status: "imported",
      kind: "in_sync",
      url: "/ipam/ip-addresses/91/",
      edit_url: "/interface-ip-state/31/edit/",
      peer: { pk: 32, address: "198.18.30.1/31", interface: "peer-01 / Gi0/2" },
    };
    const { col } = mountRows([row({ ips: [ip] })]);
    const out = col("ips").formatter(fakeCell(row({ ips: [ip] })));
    const detail = out.querySelector('a[href="/ipam/ip-addresses/91/"]');
    const edit = out.querySelector("a.nso-popedit");

    expect(detail.textContent).toBe("198.18.30.0/31");
    expect(edit.getAttribute("data-pe-url")).toBe("/interface-ip-state/31/edit/");
    expect(edit.getAttribute("data-pe-fields")).toContain("peer_address:text:Peer IP (optional)");
    expect(edit.getAttribute("data-pe-v-peer_address")).toBe("198.18.30.1/31");
    expect(edit.getAttribute("data-pe-title")).toContain("peer-01 / Gi0/2");
  });

  it("keeps an overlay-only IP editable without inventing a broken detail link", () => {
    const ip = {
      pk: 33,
      address: "198.18.31.1/32",
      status: "imported",
      kind: "in_sync",
      url: null,
      edit_url: "/interface-ip-state/33/edit/",
      peer: null,
    };
    const { col } = mountRows([row({ ips: [ip] })]);
    const out = col("ips").formatter(fakeCell(row({ ips: [ip] })));

    expect(out.querySelector('a[href^="/ipam/ip-addresses/"]')).toBeNull();
    expect(out.querySelector("code").textContent).toBe("198.18.31.1/32");
    expect(out.querySelector("a.nso-popedit")).not.toBeNull();
  });

  it("shows cable and far-end interface beneath the local interface", () => {
    const linked = row({
      link: {
        cable: { label: "#44", url: "/dcim/cables/44/" },
        peer: { name: "Gi0/2", url: "/dcim/interfaces/2/", device: "peer-01" },
      },
    });
    const { col } = mountRows([linked]);
    const out = parse(col("_name").formatter(fakeCell(linked)));

    expect(out.querySelector('a[href="/dcim/cables/44/"]')).not.toBeNull();
    expect(out.querySelector('a[href="/dcim/interfaces/2/"]').textContent).toContain("peer-01 / Gi0/2");
  });
});

describe("attr cells carry the NetBox intent (device-55 regression)", () => {
  it("flattens _desc from netbox_value — the editor prefill and sort key", () => {
    const { table } = mountRows([row({ description: Object.assign({}, PENDING_DESC) })]);
    // Tabulator's input editor opens on the flattened field: it MUST offer the
    // value the operator saved, not the device's pre-apply text.
    expect(table.data[0]._desc).toBe("uplink to sw01 (new)");
  });

  it("flattens _enabled from netbox_value", () => {
    const enabled = Object.assign({}, PENDING_DESC, { value: "True", netbox_value: false });
    const { table } = mountRows([row({ enabled })]);
    expect(table.data[0]._enabled).toBe("false");
  });

  it("renders the NetBox description as the cell value, the device text as a note", () => {
    const { col } = mountRows([row({ description: Object.assign({}, PENDING_DESC) })]);
    const host = parse(col("_desc").formatter(fakeCell(row({ description: Object.assign({}, PENDING_DESC) }))));
    expect(host.textContent).toContain("uplink to sw01 (new)");
    const note = host.querySelector(".nso-dev-note");
    expect(note.textContent).toBe("device: old device text");
    // The badge still tells the pending-apply story.
    expect(host.textContent).toContain("pending apply");
  });

  it("renders the Enabled icon from the NetBox intent, not the device mirror", () => {
    const enabled = Object.assign({}, PENDING_DESC, { value: "True", netbox_value: false });
    const { col } = mountRows([row({ enabled })]);
    const host = parse(col("_enabled").formatter(fakeCell(row({ enabled }))));
    expect(host.textContent).toContain("Disabled");
    expect(host.querySelector(".nso-dev-note").textContent).toBe("device: True");
  });

  it("keeps an in-sync cell quiet: the value once, no device note", () => {
    const desc = Object.assign({}, PENDING_DESC, {
      value: "same text",
      netbox_value: "same text",
      status: "in_sync",
      kind: "in_sync",
      label: "in sync",
    });
    const { col } = mountRows([row({ description: desc })]);
    const host = parse(col("_desc").formatter(fakeCell(row({ description: desc }))));
    expect(host.textContent).toContain("same text");
    expect(host.querySelector(".nso-dev-note")).toBeNull();
  });

  it("shows an em-dash intent with the device note on a drifted, NetBox-empty description", () => {
    const desc = Object.assign({}, PENDING_DESC, {
      value: "set out-of-band",
      netbox_value: "",
      status: "changed",
      kind: "drift",
      label: "drift",
      owned: false,
      accept_url: "/accept/11/",
    });
    const { col } = mountRows([row({ description: desc })]);
    const host = parse(col("_desc").formatter(fakeCell(row({ description: desc }))));
    expect(host.querySelector(".nso-dev-note").textContent).toBe("device: set out-of-band");
    expect(host.querySelector(".nso-cell-accept")).not.toBeNull();
  });

  it("escapes a hostile device value inside the note", () => {
    const hostile = 'x"><img src=x onerror="alert(1)';
    const desc = Object.assign({}, PENDING_DESC, { value: hostile });
    const { col } = mountRows([row({ description: desc })]);
    const host = parse(col("_desc").formatter(fakeCell(row({ description: desc }))));
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector(".nso-dev-note").textContent).toBe("device: " + hostile);
  });
});

describe("NSOGrid.deviceNote", () => {
  it("is empty for null cells and for in_sync / unknown kinds", () => {
    expect(G.deviceNote(null)).toBe("");
    expect(G.deviceNote({ kind: "in_sync", value: "v" })).toBe("");
    expect(G.deviceNote({ kind: "unknown", value: "v" })).toBe("");
  });

  it("notes the device value for every differing kind", () => {
    for (const kind of ["drift", "pending", "apply_failed", "deploying"]) {
      const host = parse(G.deviceNote({ kind, value: "dev-v" }));
      expect(host.querySelector(".nso-dev-note").textContent).toBe("device: dev-v");
    }
  });

  it("renders an em-dash when the device reported nothing", () => {
    const host = parse(G.deviceNote({ kind: "pending", value: "" }));
    expect(host.querySelector(".nso-dev-note").textContent).toBe("device: —");
  });
});
