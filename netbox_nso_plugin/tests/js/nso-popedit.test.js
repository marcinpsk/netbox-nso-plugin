/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* nso-popedit.js is an IIFE that installs document-level listeners on import, so the
 * whole editor is driven the way a page does it: real anchors, real click events,
 * real DOM. Only fetch is stubbed (the one true external boundary here). */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-popedit.js";

function makeAnchor(attrs, container) {
  const a = document.createElement("a");
  a.className = "nso-popedit";
  a.textContent = "1500";
  Object.entries(
    Object.assign(
      { "data-pe-url": "/edit/1/", "data-pe-title": "MTU", "data-pe-fields": "mtu:number:MTU", "data-pe-v-mtu": "1500" },
      attrs || {},
    ),
  ).forEach(([k, v]) => a.setAttribute(k, v));
  (container || document.body).appendChild(a);
  return a;
}

const click = (el) => el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
const card = () => document.querySelector(".nso-popedit-card");
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function stubFetch(response) {
  const fn = vi.fn().mockResolvedValue(
    Object.assign({ ok: true, status: 200, json: () => Promise.resolve({}) }, response || {}),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

beforeEach(() => {
  document.body.innerHTML = "";
  document.cookie = "csrftoken=t0k";
});

afterEach(() => {
  // Close any popover a test left open so `open` state cannot leak across tests.
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  vi.unstubAllGlobals();
});

describe("open / close", () => {
  it("clicking the anchor opens a card seeded from the data attributes", () => {
    const anchor = makeAnchor();
    click(anchor);
    expect(card()).not.toBeNull();
    expect(card().querySelector(".card-header").textContent).toBe("MTU");
    const input = card().querySelector("input");
    expect(input.value).toBe("1500");
    expect(input.type).toBe("number");
    expect(anchor.classList.contains("nso-popedit-open")).toBe(true);
  });

  it("clicking the same anchor again toggles the card closed", () => {
    const anchor = makeAnchor();
    click(anchor);
    click(anchor);
    expect(card()).toBeNull();
    expect(anchor.classList.contains("nso-popedit-open")).toBe(false);
  });

  it("Cancel, Escape and clicking outside all close the card", () => {
    const anchor = makeAnchor();
    click(anchor);
    card().querySelector(".btn-outline-secondary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(card()).toBeNull();

    click(anchor);
    card().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(card()).toBeNull();

    click(anchor);
    click(document.body);
    expect(card()).toBeNull();
  });

  it("a multi-field anchor renders one labelled input per field", () => {
    makeAnchor({
      "data-pe-fields": "l2_mtu:number:L2 MTU,ip_mtu:number:IP MTU",
      "data-pe-v-l2_mtu": "9100",
      "data-pe-v-ip_mtu": "9000",
    });
    click(document.querySelector(".nso-popedit"));
    const labels = [...card().querySelectorAll("label")].map((l) => l.textContent);
    expect(labels).toEqual(["L2 MTU", "IP MTU"]);
    expect([...card().querySelectorAll("input")].map((i) => i.value)).toEqual(["9100", "9000"]);
  });

  it("an anchor with no fields opens nothing", () => {
    const anchor = makeAnchor({ "data-pe-fields": "" });
    click(anchor);
    expect(card()).toBeNull();
  });
});

describe("save", () => {
  it("POSTs the trimmed field values with CSRF to data-pe-url", async () => {
    const fetchFn = stubFetch();
    // A text field: a number input's own sanitization would blank " 9100 " before
    // save() ever sees it (jsdom mirrors the spec there), hiding the trim under test.
    click(makeAnchor({ "data-pe-fields": "mtu:text:MTU" }));
    const input = card().querySelector("input");
    input.value = " 9100 ";
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe("/edit/1/");
    expect(init.method).toBe("POST");
    expect(init.body.get("mtu")).toBe("9100");
    expect(init.body.get("csrfmiddlewaretoken")).toBe("t0k");
    expect(init.headers["X-CSRFToken"]).toBe("t0k");
  });

  it("Enter submits a single-field editor; a multi-field editor needs the button", async () => {
    const fetchFn = stubFetch();
    click(makeAnchor());
    card()
      .querySelector("input")
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await settle();
    expect(fetchFn).toHaveBeenCalledTimes(1);

    makeAnchor({ "data-pe-fields": "a:text:A,b:text:B", "data-pe-v-a": "", "data-pe-v-b": "" });
    click(document.querySelectorAll(".nso-popedit")[1]);
    card()
      .querySelector("input")
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await settle();
    expect(fetchFn).toHaveBeenCalledTimes(1); // unchanged — Enter did not submit
  });

  it("refuses to submit a number field the browser could not parse (badInput)", async () => {
    // A badInput number field reports value === "" — on the wire that is an explicit
    // clear-to-NULL and the next push would RETRACT the value from the device. jsdom
    // cannot produce real badInput, so it is pinned onto the live input's validity.
    const fetchFn = stubFetch();
    click(makeAnchor());
    const input = card().querySelector("input");
    Object.defineProperty(input, "validity", { value: { badInput: true } });
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(fetchFn).not.toHaveBeenCalled();
    const err = card().querySelector(".nso-popedit-err");
    expect(err.classList.contains("d-none")).toBe(false);
    expect(err.textContent).toContain("valid number");
    expect(input.classList.contains("is-invalid")).toBe(true);
  });

  it("a success closes the card and fires a bubbling nso:popedit-saved from the anchor", async () => {
    stubFetch();
    const anchor = makeAnchor();
    const saved = vi.fn();
    document.addEventListener("nso:popedit-saved", saved, { once: true });
    click(anchor);
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(card()).toBeNull();
    expect(saved).toHaveBeenCalled();
  });

  it("field errors from the server land under their inputs and re-enable Save", async () => {
    stubFetch({ ok: false, json: () => Promise.resolve({ errors: { mtu: ["Too small.", "Really."] } }) });
    click(makeAnchor());
    const save = card().querySelector(".btn-primary");
    save.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(card()).not.toBeNull(); // stays open for correction
    expect(card().querySelector(".nso-popedit-err").textContent).toBe("Too small. Really.");
    expect(card().querySelector("input").classList.contains("is-invalid")).toBe(true);
    expect(save.disabled).toBe(false);
  });

  it("a non-field server message replaces the heading", async () => {
    stubFetch({ ok: false, json: () => Promise.resolve({ message: "device is deploying" }) });
    click(makeAnchor());
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    const head = card().querySelector(".card-header");
    expect(head.textContent).toBe("device is deploying");
    expect(head.classList.contains("text-danger")).toBe(true);
  });

  it("a network error reports in the heading and re-enables Save", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    click(makeAnchor());
    const save = card().querySelector(".btn-primary");
    save.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(card().querySelector(".card-header").textContent).toContain("network error");
    expect(save.disabled).toBe(false);
  });
});

describe("refresh routing after save", () => {
  /* A grid reloads ITSELF off the bubbling nso:popedit-saved (mount() listens on the
   * grid root), so a save inside ANY grid must not also fire the tab-wide
   * nso:refresh-categories — that would re-fetch every open category for one edit.
   * Server-rendered fragments have no self-refresh, so for them the tab-wide hook
   * is the only path and MUST fire. */
  it("a save inside a .nso-grid grid does NOT fire the tab-wide refresh", async () => {
    stubFetch();
    const grid = document.createElement("div");
    grid.className = "nso-grid";
    document.body.appendChild(grid);
    const anchor = makeAnchor({}, grid);
    const global = vi.fn();
    document.addEventListener("nso:refresh-categories", global, { once: true });
    click(anchor);
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(global).not.toHaveBeenCalled();
  });

  it("a save in a server-rendered fragment (no grid wrapper) fires the tab-wide refresh", async () => {
    stubFetch();
    const anchor = makeAnchor();
    const global = vi.fn();
    document.addEventListener("nso:refresh-categories", global, { once: true });
    click(anchor);
    card().querySelector(".btn-primary").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(global).toHaveBeenCalled();
  });
});
