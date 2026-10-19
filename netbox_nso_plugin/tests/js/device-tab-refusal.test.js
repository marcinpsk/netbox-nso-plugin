/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, expect, it, vi } from "vitest";

const source = readFileSync(resolve(process.cwd(), "netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html"), "utf8");
const parsed = new DOMParser().parseFromString(source, "text/html");
const script = [...parsed.querySelectorAll("script")].find(node => node.textContent.includes("var box = document.getElementById('nso-job-activity')")).textContent;

afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ""; });

it.each(["Execution unavailable; showing previous state.", "Refused; nothing committed. catalog cause"])(
  "shows structured evidence without a successful carrier badge: %s", async summary => {
    document.body.innerHTML = '<div id="nso-job-activity" data-jobs-url="/jobs/"></div>';
    vi.stubGlobal("fetch", vi.fn(async () => ({ json: async () => ({ onboarded: true,
      running: null, last: { type: "apply", status: "succeeded", refusal_summary: summary,
        error: { message: "placeholder-forged-secret-canary" } } }) })));
    new Function(script)();
    const box = document.getElementById("nso-job-activity");
    await vi.waitFor(() => expect(box.textContent).toContain(summary));
    expect(box.querySelector(".alert-success")).toBeNull();
    expect(box.textContent).not.toContain("placeholder-forged-secret-canary");
  },
);

it("keeps an unavailable prior in-sync row in the common pending filter", () => {
  const template = readFileSync(resolve(process.cwd(),
    "netbox_nso_plugin/templates/netbox_nso_plugin/categories/_table_filter.html"), "utf8");
  const parsed = new DOMParser().parseFromString(template, "text/html");
  const filter = parsed.querySelector("script").textContent.replaceAll("{{ fid }}", "category");
  document.body.innerHTML = '<div id="category"><table><tbody><tr data-status="in_sync" data-owned="true">'
    + '<td><small class="nso-execution-unavailable">Execution unavailable</small></td></tr></tbody></table></div>'
    + '<button data-kind-filter="category" data-kind="pending"><span data-kind-count></span></button>';
  new Function(filter)();
  const pending = document.querySelector("button");
  pending.click();
  expect(pending.textContent).toBe("1");
  expect(document.querySelector("tr").style.display).not.toBe("none");
});

it("classifies accepted and changed rows from their lifecycle state", () => {
  const template = readFileSync(resolve(process.cwd(),
    "netbox_nso_plugin/templates/netbox_nso_plugin/categories/_table_filter.html"), "utf8");
  const parsed = new DOMParser().parseFromString(template, "text/html");
  const filter = parsed.querySelector("script").textContent.replaceAll("{{ fid }}", "category");
  document.body.innerHTML = '<div id="category"><table><tbody>'
    + '<tr data-status="accepted" data-owned="false"><td>Accepted</td></tr>'
    + '<tr data-status="changed" data-owned="true"><td>Changed</td></tr></tbody></table></div>'
    + '<button data-kind-filter="category" data-kind="pending"><span data-kind-count></span></button>';
  new Function(filter)();
  document.querySelector("button").click();
  expect(document.querySelector('tr[data-status="accepted"]').style.display).not.toBe("none");
  expect(document.querySelector('tr[data-status="changed"]').style.display).toBe("none");
});
