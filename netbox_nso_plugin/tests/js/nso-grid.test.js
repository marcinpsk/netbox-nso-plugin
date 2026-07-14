/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* nso-grid.js is a plain browser script — an IIFE assigning window.NSOGrid — so it
 * is imported for its side effect under jsdom and tested through the window global,
 * the same way a page uses it. */
import { describe, expect, it } from "vitest";
import "../../static/netbox_nso_plugin/nso-grid.js";

const G = window.NSOGrid;

/* Parse a formatter's HTML output the way the browser will, so assertions run
 * against the resulting DOM rather than against substrings of the markup. */
function parse(html) {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host;
}

describe("esc", () => {
  /* The bug this harness exists to catch: esc() used to be the textContent →
   * innerHTML trick, which escapes & < > but NOT quotes, while the formatters
   * interpolate device-supplied values INSIDE double-quoted attributes
   * (data-accept="…", href="…", title="…"). A value carrying a `"` closed the
   * attribute and injected its own. */
  const HOSTILE = 'ge-0/0/1 "up"><x onmouseover="alert(1)';

  it("escapes double quotes — a device-supplied value cannot close a double-quoted attribute", () => {
    expect(G.esc(HOSTILE)).not.toContain('"');
  });

  it("escapes single quotes too, for single-quoted attribute contexts", () => {
    expect(G.esc("it's")).not.toContain("'");
    expect(G.esc("it's")).toBe("it&#39;s");
  });

  it("escapes all five specials", () => {
    expect(G.esc('&<>"\'')).toBe("&amp;&lt;&gt;&quot;&#39;");
  });

  it("round-trips any value through a double-quoted attribute intact, growing no extra attributes", () => {
    const corpus = [
      HOSTILE,
      'a"b',
      "a'b",
      "a<b>c&d",
      '"><script>alert(1)</script>',
      'desc with "quotes" & <tags>',
    ];
    for (const value of corpus) {
      const span = parse('<span title="' + G.esc(value) + '">x</span>').querySelector("span");
      expect(span.getAttribute("title")).toBe(value);
      expect(span.attributes.length).toBe(1);
    }
  });

  it("renders null/undefined as the empty string and stringifies non-strings", () => {
    expect(G.esc(null)).toBe("");
    expect(G.esc(undefined)).toBe("");
    expect(G.esc(0)).toBe("0");
    expect(G.esc(false)).toBe("false");
  });

  it("leaves already-escaped input double-escaped rather than guessing", () => {
    expect(G.esc("&amp;")).toBe("&amp;amp;");
  });

  it("acceptBtn: a hostile accept_url stays inside data-accept and grows no event handler", () => {
    const btn = parse(G.acceptBtn({ accept_url: HOSTILE })).querySelector("button");
    expect(btn.getAttribute("data-accept")).toBe(HOSTILE);
    expect(btn.hasAttribute("onmouseover")).toBe(false);
    expect(parse(G.acceptBtn({ accept_url: HOSTILE })).querySelectorAll("*").length).toBe(2); // button + its icon span
  });

  it("linkCell: a hostile url/label stays inside href/text and grows no event handler", () => {
    const a = parse(G.linkCell({ url: HOSTILE, label: HOSTILE })).querySelector("a");
    expect(a.getAttribute("href")).toBe(HOSTILE);
    expect(a.hasAttribute("onmouseover")).toBe(false);
    expect(a.textContent).toBe(HOSTILE);
    expect(a.querySelector("x")).toBeNull();
  });

  it("stateColumn: a hostile residue_job stays inside the residue badge's title", () => {
    const cell = {
      getRow: () => ({ getData: () => ({ state: "in_sync", residue: true, residue_job: HOSTILE }) }),
    };
    const host = parse(G.stateColumn().formatter(cell));
    const residue = host.querySelectorAll(".badge")[1];
    expect(residue.textContent).toBe("removal residue");
    expect(residue.getAttribute("title")).toContain(HOSTILE);
    expect(residue.hasAttribute("onmouseover")).toBe(false);
  });
});

/* Real-shape stand-in for the Tabulator cell object the formatters receive —
 * only the members the harness actually calls (getRow().getData(), getValue()). */
function fakeCell(rowData, value) {
  return {
    getRow: () => ({ getData: () => rowData }),
    getValue: () => value,
  };
}

describe("badge", () => {
  it("renders the shared vocabulary: kind picks the class and the default label", () => {
    const span = parse(G.badge("drift")).querySelector("span.badge");
    expect(span.className).toBe("badge text-bg-warning text-dark");
    expect(span.textContent).toBe("drift");
    expect(parse(G.badge("apply_failed")).querySelector(".text-bg-danger").textContent).toBe("apply failed");
    expect(parse(G.badge("pending")).querySelector("span").textContent).toBe("pending apply");
  });

  it("an unknown kind falls back to the unknown badge instead of throwing", () => {
    const span = parse(G.badge("no-such-kind")).querySelector("span.badge");
    expect(span.className).toBe("badge text-bg-info");
    expect(span.textContent).toBe("unknown");
  });

  it("a custom label overrides the default and is escaped", () => {
    const span = parse(G.badge("drift", '<b>"x"</b>')).querySelector("span.badge");
    expect(span.textContent).toBe('<b>"x"</b>');
    expect(span.querySelector("b")).toBeNull();
  });
});

describe("cellBadge", () => {
  it("returns the empty string for a missing cell", () => {
    expect(G.cellBadge(null)).toBe("");
    expect(G.cellBadge(undefined)).toBe("");
  });

  it("in-sync-and-owned renders the quiet check icon, not a badge", () => {
    const host = parse(G.cellBadge({ kind: "in_sync", status: "in_sync" }));
    expect(host.querySelector(".mdi-check-circle")).not.toBeNull();
    expect(host.querySelector(".badge")).toBeNull();
  });

  it("in-sync but not-owned renders nothing at all", () => {
    expect(G.cellBadge({ kind: "in_sync", status: "pending" })).toBe("");
  });

  it("any other kind renders its badge", () => {
    const host = parse(G.cellBadge({ kind: "drift" }));
    expect(host.querySelector(".badge").textContent).toBe("drift");
  });
});

describe("acceptBtn", () => {
  it("renders nothing without a cell or an accept_url", () => {
    expect(G.acceptBtn(null)).toBe("");
    expect(G.acceptBtn({})).toBe("");
    expect(G.acceptBtn({ accept_url: "" })).toBe("");
  });

  it("renders the accept button with the url in data-accept", () => {
    const btn = parse(G.acceptBtn({ accept_url: "/plugins/nso/accept/7/" })).querySelector("button.nso-cell-accept");
    expect(btn.getAttribute("data-accept")).toBe("/plugins/nso/accept/7/");
    expect(btn.getAttribute("type")).toBe("button");
  });
});

describe("valueFormatter", () => {
  it("renders the em-dash for a missing cell and for an empty value", () => {
    expect(G.valueFormatter("mtu")(fakeCell({}))).toBe(G.MUTED);
    expect(G.valueFormatter("mtu")(fakeCell({ mtu: { value: null } }))).toContain(G.MUTED);
    expect(G.valueFormatter("mtu")(fakeCell({ mtu: { value: "" } }))).toContain(G.MUTED);
  });

  it("renders value + badge + accept button for an unowned drifting cell", () => {
    const row = { mtu: { value: 9100, kind: "drift", status: "pending", accept_url: "/a/1/" } };
    const host = parse(G.valueFormatter("mtu")(fakeCell(row)));
    expect(host.textContent).toContain("9100");
    expect(host.querySelector(".badge").textContent).toBe("drift");
    expect(host.querySelector("button.nso-cell-accept").getAttribute("data-accept")).toBe("/a/1/");
  });

  it("escapes the device-supplied value", () => {
    const row = { desc: { value: 'up"link <1>', kind: "in_sync", status: "in_sync" } };
    const host = parse(G.valueFormatter("desc")(fakeCell(row)));
    expect(host.textContent).toContain('up"link <1>');
    // The only element is the in-sync check icon — `<1>` stayed text, not markup.
    expect(host.querySelectorAll("*").length).toBe(1);
    expect(host.querySelector("*").className).toContain("mdi-check-circle");
  });

  it("a custom render gets the cell and the whole row, and its output is used as the body", () => {
    const row = { peer: { value: "x", kind: "in_sync", status: "in_sync" }, extra: 7 };
    const seen = [];
    const out = G.valueFormatter("peer", (c, r) => {
      seen.push([c, r]);
      return "<em>custom</em>";
    })(fakeCell(row));
    expect(seen).toEqual([[row.peer, row]]);
    expect(parse(out).querySelector("em").textContent).toBe("custom");
  });
});

describe("stateColumn", () => {
  it("formats the row state as its badge, without a residue badge by default", () => {
    const host = parse(G.stateColumn().formatter(fakeCell({ state: "drift" })));
    expect(host.querySelectorAll(".badge").length).toBe(1);
    expect(host.querySelector(".badge").textContent).toBe("drift");
  });

  it("sorts by severity, worst first", () => {
    const sorted = ["in_sync", "pending", "apply_failed", "drift"].sort(G.stateColumn().sorter);
    expect(sorted).toEqual(["apply_failed", "drift", "pending", "in_sync"]);
  });

  it("merges extra column options over the defaults", () => {
    const col = G.stateColumn({ minWidth: 200, field: "other" });
    expect(col.minWidth).toBe(200);
    expect(col.field).toBe("other");
    expect(col.title).toBe("State");
  });
});

describe("acceptColumn", () => {
  it("renders the row-level accept button from the row's accept_url", () => {
    const btn = parse(G.acceptColumn().formatter(fakeCell({ accept_url: "/row/2/" }))).querySelector("button");
    expect(btn.getAttribute("data-accept")).toBe("/row/2/");
  });

  it("renders nothing for an owned row (no accept_url)", () => {
    expect(G.acceptColumn().formatter(fakeCell({}))).toBe("");
  });
});

describe("lastSyncColumn", () => {
  it("renders the timestamp escaped, or the em-dash when never synced", () => {
    expect(G.lastSyncColumn().formatter(fakeCell({}, "2026-07-14 12:00"))).toBe("2026-07-14 12:00");
    expect(G.lastSyncColumn().formatter(fakeCell({}, '<"t">'))).toBe("&lt;&quot;t&quot;&gt;");
    expect(G.lastSyncColumn().formatter(fakeCell({}, null))).toBe(G.MUTED);
  });
});

describe("linkCell", () => {
  it("renders the em-dash when the overlay never matched an object", () => {
    expect(G.linkCell(null)).toBe(G.MUTED);
    expect(G.linkCell({ url: null, label: "x" })).toBe(G.MUTED);
  });

  it("renders a plain link from {label, url}", () => {
    const a = parse(G.linkCell({ url: "/dcim/devices/54/", label: "ra1" })).querySelector("a");
    expect(a.getAttribute("href")).toBe("/dcim/devices/54/");
    expect(a.textContent).toBe("ra1");
  });
});

describe("boolBadge", () => {
  it("on renders the on-label badge with the on-class", () => {
    const span = parse(G.boolBadge(true, "active", "text-bg-success", "passive", "text-bg-warning")).querySelector("span");
    expect(span.className).toBe("badge text-bg-success");
    expect(span.textContent).toBe("active");
  });

  it("off renders the off-label badge, or the em-dash when there is no off label", () => {
    const span = parse(G.boolBadge(false, "active", "text-bg-success", "passive", "text-bg-warning")).querySelector("span");
    expect(span.className).toBe("badge text-bg-warning");
    expect(span.textContent).toBe("passive");
    expect(G.boolBadge(false, "active", "text-bg-success")).toBe(G.MUTED);
  });
});

describe("exports", () => {
  it("SEVERITY ranks apply_failed worst and in_sync best", () => {
    expect(G.SEVERITY[0]).toBe("apply_failed");
    expect(G.SEVERITY[G.SEVERITY.length - 1]).toBe("in_sync");
  });

  it("MUTED is the shared em-dash placeholder", () => {
    expect(parse(G.MUTED).querySelector(".text-muted").textContent).toBe("—");
  });
});
