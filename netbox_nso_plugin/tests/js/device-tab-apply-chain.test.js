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
  const blocks = readFileSync(TEMPLATE, "utf8").match(/<script>([\s\S]*?)<\/script>/gi) || [];
  const matching = blocks.filter((block) => block.includes("function pollApplyChain("));
  if (matching.length !== 1) {
    throw new Error(`expected one inline Apply-chain block in the tab template, found ${matching.length}`);
  }
  const body = matching[0].replace(/^<script>/i, "").replace(/<\/script>$/i, "");
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

/* The adapter emits EVERY field of these rows on EVERY row, null when unset (the
 * receipts-surface emit-null discipline), and views.NSODeviceActionView rejects an Apply
 * chain that drops one. A fixture that omits a field therefore proves nothing about the
 * poller, so each builder below is pinned against its response's field list. Copied from
 * ../nso-adapter/docs/api-contract.md (actions/apply 202, GET devices/{id}/generations)
 * and ../nso-adapter/tests/api/openapi_snapshot.json (ActionApplyGenerationOut,
 * DeviceGenerationOut, JobOut). */
const APPLY_GENERATION_FIELDS = [
  "generation_id", "seq", "job_id", "mode", "source_push_seq", "stream_revisions", "digest",
];
const DEVICE_GENERATION_FIELDS = [
  ...APPLY_GENERATION_FIELDS, "status", "settlement_cohort", "created_at", "updated_at",
];
const JOB_FIELDS = [
  "id", "type", "device_id", "status", "result", "error", "context",
  "created_at", "updated_at", "started_at", "heartbeat_at", "settle_seq",
];

function pinned(row, fields, what) {
  const missing = fields.filter((field) => !(field in row));
  if (missing.length) {
    throw new Error(`the ${what} fixture dropped required field(s): ${missing.join(", ")}`);
  }
  return row;
}

/* One link of the actions/apply 202 chain. It carries no status and no timestamps; the
 * poller reads those from the listing instead. */
function applyGeneration(overrides) {
  return pinned(
    {
      generation_id: 81,
      seq: 4,
      job_id: 501,
      mode: "networked",
      source_push_seq: { logging: 8801 },
      stream_revisions: { logging: 12 },
      digest: "a".repeat(64),
      ...overrides,
    },
    APPLY_GENERATION_FIELDS,
    "actions/apply generation",
  );
}

function deviceGeneration(overrides) {
  return pinned(
    {
      ...applyGeneration({}),
      status: "settled",
      settlement_cohort: 73,
      created_at: "2026-08-12T09:15:00Z",
      updated_at: "2026-08-12T09:30:00Z",
      ...overrides,
    },
    DEVICE_GENERATION_FIELDS,
    "device generation",
  );
}

function job(overrides) {
  return pinned(
    {
      id: 501,
      type: "apply",
      device_id: 1, // the device mountTab() renders; a job row names its own device
      status: "succeeded",
      result: {},
      error: null,
      context: null,
      created_at: "2026-08-12T09:15:00Z",
      updated_at: "2026-08-12T09:30:00Z",
      started_at: "2026-08-12T09:15:02Z",
      heartbeat_at: null,
      settle_seq: 4,
      ...overrides,
    },
    JOB_FIELDS,
    "job",
  );
}

const generations = [
  applyGeneration({}),
  applyGeneration({ generation_id: 82, seq: 5, job_id: null, mode: "detach", digest: "b".repeat(64) }),
];

const HEAD = deviceGeneration({});

/* views.NSODeviceJobsView reads list_jobs() before list_device_generations(), so a
 * successor that attaches and settles between the two calls is reported terminal by a jobs
 * page that predates its job. */
const RACED = {
  onboarded: true,
  running: null,
  last: job({}),
  jobs: [job({})],
  generations: [
    HEAD,
    deviceGeneration({ generation_id: 82, seq: 5, job_id: 502, mode: "detach", digest: "b".repeat(64) }),
  ],
  blocked_removals: [],
  residue_removals: [],
};

function withChain(chain) {
  return [HEAD, chain];
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

  it("refuses a fixture row that drops a contract field", () => {
    const { digest, ...withoutDigest } = deviceGeneration({});

    expect(digest).toBeTruthy();
    expect(() => pinned(withoutDigest, DEVICE_GENERATION_FIELDS, "device generation")).toThrow(/digest/);
  });

  it("shows a stable message when a no-op response omits one", async () => {
    const fetched = vi.fn(async () => ({
      ok: true,
      json: async () => ({ status: "no_op" }),
    }));
    vi.stubGlobal("fetch", fetched);
    const form = mountTab();

    form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(message()).toBe("Nothing to do."));
    expect(fetched).toHaveBeenCalledOnce();
  });

  it("refreshes the categories when the chain settles without its successor's job details", async () => {
    const refreshed = await runApply(RACED);

    expect(message()).toContain("completed across 2 generation(s)");
    expect(refreshed).toHaveBeenCalledOnce();
  });

  it("refreshes the categories when a failed link's job is missing from the jobs page", async () => {
    const refreshed = await runApply({
      ...RACED,
      generations: withChain(
        deviceGeneration({
          generation_id: 82,
          seq: 5,
          status: "failed",
          job_id: 502,
          mode: "detach",
          digest: "b".repeat(64),
        }),
      ),
    });

    expect(message()).toContain("failed");
    expect(refreshed).toHaveBeenCalledOnce();
  });

  it("refreshes the categories when an abandoned link carries no job at all", async () => {
    const refreshed = await runApply({
      ...RACED,
      generations: withChain(
        deviceGeneration({
          generation_id: 82,
          seq: 5,
          status: "abandoned",
          job_id: null,
          mode: "detach",
          digest: "b".repeat(64),
        }),
      ),
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
