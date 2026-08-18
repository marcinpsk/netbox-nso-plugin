/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The Apply chain poller still lives inline in device_nso_tab.html (its extraction into a
 * first-party asset is card #1571). The block is plain JavaScript with no Django template
 * syntax, so this harness reads it out of the template and runs it under jsdom against the
 * same window.NSOApplyChain helper the page loads. */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-apply-chain.js";

// jsdom rewrites import.meta.url to an http URL, so the template is read from the
// vitest root (this repo) instead.
const TEMPLATE = resolve(process.cwd(), "netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html");

function applyChainScript() {
  const blocks = readFileSync(TEMPLATE, "utf8").match(/<script>([\s\S]*?)<\/script>/g) || [];
  const matching = blocks.filter((block) => block.includes("function pollApplyChain("));
  if (matching.length !== 1) {
    throw new Error(`expected one inline Apply-chain block in the tab template, found ${matching.length}`);
  }
  const body = matching[0].replace(/^<script>/, "").replace(/<\/script>$/, "");
  if (/\{[{%#]/.test(body)) {
    throw new Error("the inline Apply-chain block gained Django template syntax; this loader cannot run it");
  }
  return body;
}

const SCRIPT = applyChainScript();

function mountTab() {
  document.body.innerHTML = `
    <form method="post" class="nso-action-form" action="/plugins/nso/device-management/1/actions/apply/"
          data-label="Apply Intent"><button type="submit" class="nso-action-btn"></button></form>
    <div id="nso-job-activity" data-jobs-url="/plugins/nso/devices/1/jobs/"></div>
    <div id="nso-job-status" class="d-none" data-job-status-url="/plugins/nso/jobs/0/status/">
      <div class="alert"><span id="nso-job-spinner"></span><span id="nso-job-message"></span></div>
    </div>`;
  // First-party template code, read from the repo at test time.
  new Function(SCRIPT)();
  return document.querySelector(".nso-action-form");
}

function message() {
  return document.getElementById("nso-job-message").textContent;
}

/* Copied from ../nso-adapter/docs/api-contract.md, actions/apply 202 response. */
const generations = [
  { generation_id: 81, seq: 4, job_id: 501, mode: "networked" },
  { generation_id: 82, seq: 5, job_id: null, mode: "detach" },
];

/* views.NSODeviceJobsView reads list_jobs() before list_device_generations(), so a
 * successor that attaches and settles between the two calls is reported terminal by a jobs
 * page that predates its job. Rows copied from DeviceGenerationOut and JobOut in
 * ../nso-adapter/tests/api/openapi_snapshot.json. */
const RACED = {
  onboarded: true,
  running: null,
  last: { id: 501, type: "apply", status: "succeeded" },
  jobs: [{ id: 501, type: "apply", status: "succeeded", result: {} }],
  generations: [
    { generation_id: 81, seq: 4, status: "settled", job_id: 501, mode: "networked" },
    { generation_id: 82, seq: 5, status: "settled", job_id: 502, mode: "detach" },
  ],
  blocked_removals: [],
  residue_removals: [],
};

function withChain(chain) {
  return [
    { generation_id: 81, seq: 4, status: "settled", job_id: 501, mode: "networked" },
    chain,
  ];
}

describe("the device tab's Apply generation-chain poller", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    // A real 10s abort timer per poll outlives the test; the signal is never asserted here.
    vi.spyOn(AbortSignal, "timeout").mockReturnValue(undefined);
    document.body.innerHTML = "";
  });

  async function runApply(chain) {
    const refreshed = vi.fn();
    document.addEventListener("nso:refresh-categories", refreshed);
    const fetched = vi.fn(async (url) => ({
      ok: true,
      json: async () =>
        String(url).includes("/actions/apply/")
          ? { status: "ok", message: "Apply triggered.", job_id: 501, skipped: {}, generations }
          : chain,
    }));
    vi.stubGlobal("fetch", fetched);
    const form = mountTab();

    form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(message()).toMatch(/completed|failed|stopped/));
    document.removeEventListener("nso:refresh-categories", refreshed);
    return refreshed;
  }

  it("refreshes the categories when the chain settles without its successor's job details", async () => {
    const refreshed = await runApply(RACED);

    expect(message()).toContain("completed across 2 generation(s)");
    expect(refreshed).toHaveBeenCalledOnce();
  });

  it("refreshes the categories when a failed link's job is missing from the jobs page", async () => {
    const refreshed = await runApply({
      ...RACED,
      generations: withChain({ generation_id: 82, seq: 5, status: "failed", job_id: 502, mode: "detach" }),
    });

    expect(message()).toContain("failed");
    expect(refreshed).toHaveBeenCalledOnce();
  });

  it("refreshes the categories when an abandoned link carries no job at all", async () => {
    const refreshed = await runApply({
      ...RACED,
      generations: withChain({ generation_id: 82, seq: 5, status: "abandoned", job_id: null, mode: "detach" }),
    });

    expect(message()).toContain("was abandoned");
    expect(refreshed).toHaveBeenCalledOnce();
  });

  it("does not refresh when the poll failed and the chain reached no outcome", async () => {
    const refreshed = vi.fn();
    document.addEventListener("nso:refresh-categories", refreshed);
    const fetched = vi.fn(async (url) =>
      String(url).includes("/actions/apply/")
        ? { ok: true, json: async () => ({ status: "ok", job_id: 501, skipped: {}, generations }) }
        : { ok: false, json: async () => ({}) },
    );
    vi.stubGlobal("fetch", fetched);
    const form = mountTab();

    form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(message()).toContain("Could not retrieve"));
    document.removeEventListener("nso:refresh-categories", refreshed);

    expect(refreshed).not.toHaveBeenCalled();
  });
});
