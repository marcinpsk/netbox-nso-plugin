/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* mount() wiring tests. Tabulator is vendored and not under test, so it is replaced
 * by a real-shape stand-in that records the config and exposes the same members the
 * harness calls (on/destroy/replaceData/hideColumn/toggleColumn/setFilter/clearFilter).
 * Everything else — the DOM, the delegated click handlers, fetch bodies — is real. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";

const G = window.NSOGrid;

class FakeTabulator {
  constructor(el, config) {
    this.el = el;
    this.config = config;
    this.data = config.data;
    this.handlers = {};
    this.hiddenCols = new Set();
    this.filter = null;
    this.destroyed = false;
    FakeTabulator.instances.push(this);
  }
  on(event, fn) {
    (this.handlers[event] = this.handlers[event] || []).push(fn);
  }
  emit(event, arg) {
    (this.handlers[event] || []).forEach((fn) => fn(arg));
  }
  destroy() {
    this.destroyed = true;
  }
  replaceData(rows) {
    this.data = rows;
  }
  hideColumn(field) {
    this.hiddenCols.add(field);
  }
  toggleColumn(field) {
    if (this.hiddenCols.has(field)) this.hiddenCols.delete(field);
    else this.hiddenCols.add(field);
  }
  setFilter(a, b, c) {
    this.filter = { a, b, c };
  }
  clearFilter() {
    this.filter = null;
  }
}
FakeTabulator.instances = [];

function gridRoot() {
  const root = document.createElement("div");
  root.className = "nso-grid";
  root.innerHTML =
    '<div class="nso-grid-msg"></div>' +
    '<div class="nso-grid-state">' +
    '<button data-state="all" class="active"></button>' +
    '<button data-state="drift"></button>' +
    '<button data-state="pending"></button>' +
    '<button data-state="ok"></button>' +
    "</div>" +
    '<div class="nso-grid-cols"><button data-col="mtu" class="active"></button></div>' +
    '<span class="nso-grid-n-drift"></span>' +
    '<div class="nso-grid-table"></div>';
  document.body.appendChild(root);
  return root;
}

const ROWS = [
  { state: "drift", name: "ge-0/0/1" },
  { state: "in_sync", name: "ge-0/0/2" },
];

function mountWith(root, extra) {
  return G.mount(
    root,
    Object.assign({ payload: { rows: ROWS, counts: {} }, jsonUrl: "/json", columns: [], key: "t" }, extra || {}),
  );
}

/* Everything the harness posts/fetches goes through global fetch. */
function stubFetch(response) {
  const fn = vi.fn().mockResolvedValue(
    Object.assign({ ok: true, status: 200, json: () => Promise.resolve({}) }, response || {}),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  vi.stubGlobal("Tabulator", FakeTabulator);
  FakeTabulator.instances = [];
  document.body.innerHTML = "";
  document.cookie = "csrftoken=t0k";
  // The window slots deliberately persist across re-renders in a page; between
  // tests they are cross-talk, so clear them.
  Object.keys(window)
    .filter((k) => k.startsWith("__nsoGrid"))
    .forEach((k) => delete window[k]);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mount preconditions", () => {
  it("returns null without a root, without a table element, or without Tabulator", () => {
    expect(mountWith(null)).toBeNull();
    const bare = document.createElement("div");
    document.body.appendChild(bare);
    expect(mountWith(bare)).toBeNull();
    vi.unstubAllGlobals();
    expect(mountWith(gridRoot())).toBeNull();
  });
});

describe("mount data plumbing", () => {
  it("hands the payload rows to Tabulator as a copy, not the payload array itself", () => {
    mountWith(gridRoot());
    const table = FakeTabulator.instances[0];
    expect(table.data).toEqual(ROWS);
    expect(table.data).not.toBe(ROWS);
  });

  it("opts.flatten transforms the rows before they reach the table", () => {
    mountWith(gridRoot(), { flatten: (rows) => rows.map((r) => Object.assign({ flat: true }, r)) });
    expect(FakeTabulator.instances[0].data[0].flat).toBe(true);
  });

  it("opts.extract picks this grid's section out of a multi-table payload", () => {
    const payload = { instances: { rows: [{ state: "drift" }], counts: {} } };
    mountWith(gridRoot(), { payload, extract: (json) => json.instances });
    expect(FakeTabulator.instances[0].data).toEqual([{ state: "drift" }]);
  });

  it("re-mounting the same key destroys the previous table (fragment re-render)", () => {
    mountWith(gridRoot());
    mountWith(gridRoot());
    expect(FakeTabulator.instances[0].destroyed).toBe(true);
    expect(FakeTabulator.instances[1].destroyed).toBe(false);
  });

  it("a payload adapter_error lands in the message strip as a warning", () => {
    const root = gridRoot();
    mountWith(root, { payload: { rows: [], counts: {}, adapter_error: "boom" } });
    expect(root.querySelector(".nso-grid-msg .alert-warning").textContent).toContain("Adapter: boom");
  });
});

describe("column show/hide", () => {
  it("remembered hidden columns are applied on tableBuilt and reflected on the buttons", () => {
    window.__nsoGridHidden_t = { mtu: true };
    const root = gridRoot();
    mountWith(root, { colFields: { mtu: "mtu_field" } });
    const table = FakeTabulator.instances[0];
    table.emit("tableBuilt");
    expect(table.hiddenCols.has("mtu_field")).toBe(true);
    expect(root.querySelector('[data-col="mtu"]').classList.contains("active")).toBe(false);
  });

  it("clicking a column button toggles the column and remembers it", () => {
    const root = gridRoot();
    mountWith(root, { colFields: { mtu: "mtu_field" } });
    const btn = root.querySelector('[data-col="mtu"]');
    btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(FakeTabulator.instances[0].hiddenCols.has("mtu_field")).toBe(true);
    expect(btn.classList.contains("active")).toBe(false);
    expect(window.__nsoGridHidden_t.mtu).toBe(true);
  });
});

describe("quick-filter pills", () => {
  it("drift / pending / ok / all drive the table filter to the server's buckets", () => {
    const root = gridRoot();
    mountWith(root);
    const table = FakeTabulator.instances[0];
    const click = (state) =>
      root.querySelector('[data-state="' + state + '"]').dispatchEvent(new MouseEvent("click", { bubbles: true }));

    click("drift");
    expect(table.filter).toEqual({ a: "state", b: "=", c: "drift" });
    expect(root.querySelector('[data-state="drift"]').classList.contains("active")).toBe(true);
    expect(root.querySelector('[data-state="all"]').classList.contains("active")).toBe(false);

    click("pending");
    expect(table.filter.a({ state: "pending" })).toBe(true);
    expect(table.filter.a({ state: "apply_failed" })).toBe(true);
    expect(table.filter.a({ state: "drift" })).toBe(false);

    click("ok");
    expect(table.filter.a({ state: "in_sync" })).toBe(true);
    expect(table.filter.a({ state: "drift" })).toBe(false);
    expect(table.filter.a({ state: "apply_failed" })).toBe(false);

    click("all");
    expect(table.filter).toBeNull();
  });
});

describe("reload", () => {
  it("re-fetches the category JSON, swaps the data and updates this grid's counts", async () => {
    const fetchFn = stubFetch({
      json: () => Promise.resolve({ rows: [{ state: "in_sync" }], counts: { drift: 3 } }),
    });
    const root = gridRoot();
    const api = mountWith(root);
    await api.reload();
    expect(fetchFn).toHaveBeenCalledWith("/json", { headers: { "X-Requested-With": "XMLHttpRequest" } });
    expect(FakeTabulator.instances[0].data).toEqual([{ state: "in_sync" }]);
    expect(root.querySelector(".nso-grid-n-drift").textContent).toBe("3");
  });

  it("a failed refresh lands in the message strip, not in the console", async () => {
    stubFetch({ ok: false, status: 502 });
    const root = gridRoot();
    const api = mountWith(root);
    await api.reload();
    expect(root.querySelector(".nso-grid-msg .alert-danger").textContent).toContain("HTTP 502");
  });

  it("a bubbling nso:popedit-saved from inside the grid triggers a reload", async () => {
    const fetchFn = stubFetch();
    const root = gridRoot();
    mountWith(root);
    root.querySelector(".nso-grid-table").dispatchEvent(new CustomEvent("nso:popedit-saved", { bubbles: true }));
    await settle();
    expect(fetchFn).toHaveBeenCalledWith("/json", expect.anything());
  });

  /* An inline edit suppresses the tab-wide refresh, so this fetch is the only thing that
   * runs after it. It re-renders rows, never the server-rendered banner include — so
   * without this the rejection the edit just caused stays invisible. */
  function banner() {
    const el = document.createElement("div");
    el.className = "alert nso-push-banner d-none";
    el.innerHTML =
      '<span class="nso-push-headline"></span><div class="nso-push-detail"></div><div class="nso-push-meta"></div>';
    document.body.append(el);
    return el;
  }

  it("shows an intent-push rejection that appeared since the page was rendered", async () => {
    const el = banner();
    stubFetch({
      json: () =>
        Promise.resolve({
          rows: [],
          counts: {},
          push_error: {
            headline: "The adapter rejected the last intent push for this category.",
            message: "Two routes in the payload carry the same triple",
            code: "validation_error",
            detail: { reason: "duplicate_triple" },
            attempt: 2,
            at: "2026-08-04T10:00:00Z",
          },
        }),
    });
    const api = mountWith(gridRoot());
    await api.reload();

    expect(el.classList.contains("d-none")).toBe(false);
    expect(el.querySelector(".nso-push-headline").textContent).toContain("rejected");
    expect(el.querySelector(".nso-push-detail").textContent).toContain("same triple");
    expect(el.querySelector(".nso-push-meta").textContent).toContain("duplicate_triple");
    expect(el.querySelector(".nso-push-meta").textContent).toContain("attempt 2");
  });

  it("hides a rejection the reload shows has cleared", async () => {
    const el = banner();
    el.classList.remove("d-none");
    stubFetch({ json: () => Promise.resolve({ rows: [], counts: {}, push_error: null }) });
    const api = mountWith(gridRoot());
    await api.reload();
    expect(el.classList.contains("d-none")).toBe(true);
  });

  it("leaves the banner alone for a category whose payload has no push_error key", async () => {
    const el = banner();
    el.classList.remove("d-none");
    stubFetch({ json: () => Promise.resolve({ rows: [], counts: {} }) });
    const api = mountWith(gridRoot());
    await api.reload();
    expect(el.classList.contains("d-none")).toBe(false);
  });
});

describe("per-cell Accept (delegated click)", () => {
  function acceptButton(root) {
    const btn = document.createElement("button");
    btn.className = "nso-cell-accept";
    btn.dataset.accept = "/accept/7/";
    root.querySelector(".nso-grid-table").appendChild(btn);
    return btn;
  }

  it("POSTs to data-accept with CSRF, then reloads and pings the other panels", async () => {
    const fetchFn = stubFetch();
    const root = gridRoot();
    mountWith(root);
    const pinged = vi.fn();
    document.addEventListener("nso:refresh-categories", pinged, { once: true });

    acceptButton(root).dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();

    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe("/accept/7/");
    expect(init.method).toBe("POST");
    expect(init.headers["X-CSRFToken"]).toBe("t0k");
    expect(init.body.get("csrfmiddlewaretoken")).toBe("t0k");
    expect(pinged).toHaveBeenCalled();
    expect(fetchFn).toHaveBeenCalledWith("/json", expect.anything()); // the post-action reload
  });

  it("a failed accept reports in the message strip and does not ping other panels", async () => {
    // The failed POST is followed by a reload of the grid JSON, which succeeds —
    // the failure flash must survive that reload.
    const fetchFn = stubFetch();
    fetchFn.mockResolvedValueOnce({ ok: false, status: 400, json: () => Promise.resolve({ message: "nope" }) });
    const root = gridRoot();
    mountWith(root);
    const pinged = vi.fn();
    document.addEventListener("nso:refresh-categories", pinged, { once: true });

    acceptButton(root).dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();

    expect(root.querySelector(".nso-grid-msg .alert-danger").textContent).toContain("nope");
    expect(pinged).not.toHaveBeenCalled();
  });
});

describe("inline edit takes ownership", () => {
  function editedCell(rowData, value) {
    return {
      getField: () => "mtu",
      getValue: () => value,
      getRow: () => ({ getData: () => rowData }),
      restoreOldValue: vi.fn(),
    };
  }

  it("writes through to the mapped cell's edit_url with the new value", async () => {
    const fetchFn = stubFetch();
    mountWith(gridRoot(), { cellKeys: { mtu: "mtu_cell" } });
    const cell = editedCell({ mtu_cell: { edit_url: "/edit/9/" } }, "9100");
    FakeTabulator.instances[0].emit("cellEdited", cell);
    await settle();
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe("/edit/9/");
    expect(init.body.get("value")).toBe("9100");
    expect(cell.restoreOldValue).not.toHaveBeenCalled();
  });

  it("a column with no writable cell restores the old value instead of posting", () => {
    const fetchFn = stubFetch();
    mountWith(gridRoot(), { cellKeys: { mtu: "mtu_cell" } });
    const cell = editedCell({ mtu_cell: null }, "9100");
    FakeTabulator.instances[0].emit("cellEdited", cell);
    expect(cell.restoreOldValue).toHaveBeenCalled();
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
