/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* READSEM S4 (D10): the category-header badge/read-chip renderer the dynamic
 * counts path uses. The chip's css/label/tip arrive from the server's counts JSON
 * (summary._CHIP_RENDER is the single source of truth) — this module must render
 * them escaped, render NOTHING for healthy (null), and never emit bg-secondary. */
import { describe, expect, it } from "vitest";
import "../../static/netbox_nso_plugin/nso-badges.js";

const B = window.NSOBadges;

const STALE = {
  state: "stale",
  css: "text-bg-warning text-dark",
  label: "showing last-known data",
  tip: "The newest read succeeded but served an older cached snapshot (stale).",
};

describe("chipHtml", () => {
  it("renders nothing for a healthy (null) read", () => {
    expect(B.chipHtml(null)).toBe("");
    expect(B.chipHtml(undefined)).toBe("");
  });

  it("renders the server-provided css, label and tooltip", () => {
    const html = B.chipHtml(STALE);
    expect(html).toContain("text-bg-warning");
    expect(html).toContain("showing last-known data");
    expect(html).toContain("older cached snapshot");
  });

  it("escapes hostile content from the payload", () => {
    const html = B.chipHtml({
      state: "unknown",
      css: 'x" onmouseover="alert(1)',
      label: "<script>alert(1)</script>",
      tip: '"><img src=x>',
    });
    expect(html).not.toContain("<script>");
    expect(html).not.toContain('onmouseover="alert');
    expect(html).toContain("&lt;script&gt;");
  });

  it("never emits bg-secondary (operator directive)", () => {
    for (const state of ["stale", "unavailable", "unknown", "refresh_pending", "unsupported"]) {
      const html = B.chipHtml({ state, css: "text-bg-danger", label: state, tip: "" });
      expect(html).not.toContain("bg-secondary");
    }
  });
});

describe("renderBadges", () => {
  it("keeps the counts badges and appends the read chip", () => {
    const html = B.renderBadges(3, 1, 0, STALE);
    expect(html).toContain("(3)");
    expect(html).toContain("1 drift");
    expect(html).toContain("showing last-known data");
  });

  it("healthy read renders counts only — no chip markup", () => {
    const html = B.renderBadges(2, 0, 0, null);
    expect(html).toContain("in sync");
    expect((html.match(/badge/g) || []).length).toBe(1); // just the in-sync badge
  });

  it("a skipped-busy style info chip coexists with pending apply", () => {
    const busy = { state: "refresh_pending", css: "text-bg-info", label: "refresh pending", tip: "" };
    const html = B.renderBadges(5, 0, 2, busy);
    expect(html).toContain("2 pending apply");
    expect(html).toContain("refresh pending");
  });
});
