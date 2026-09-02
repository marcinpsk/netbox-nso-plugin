/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

const TEMPLATE = resolve(process.cwd(), "netbox_nso_plugin/templates/netbox_nso_plugin/device_nso_tab.html");

function jobActivityScript() {
  const blocks = readFileSync(TEMPLATE, "utf8").match(/<script>([\s\S]*?)<\/script>/g) || [];
  const matching = blocks.filter((block) => block.includes("function renderApplyState("));
  if (matching.length !== 1) {
    throw new Error(`expected one blocked-head renderer in the tab template, found ${matching.length}`);
  }
  const body = matching[0].replace(/^<script>/, "").replace(/<\/script>$/, "");
  if (/\{[{%#]/.test(body)) {
    throw new Error("the inline job-activity block gained Django template syntax; this loader cannot run it");
  }
  return body;
}

function mountTab(applyState, applyStateError = null) {
  document.body.innerHTML = `
    <div id="nso-apply-state-error" class="d-none"><span data-slot="message"></span></div>
    <div id="nso-blocked-generation" class="d-none">
      <span data-slot="generation"></span>
      <span data-slot="status"></span>
      <span data-slot="sections"></span>
      <span data-slot="pending"></span>
      <span data-slot="held"></span>
      <form data-action="retry"><input name="generation_id"></form>
      <form data-action="abandon"><input name="generation_id"></form>
    </div>
    <div id="nso-job-activity" data-jobs-url="/plugins/nso/devices/1/jobs/"></div>
    <div id="nso-blocked-removals" class="d-none"></div>
    <template id="nso-blocked-removal-tpl"></template>
    <div id="nso-residue-removals" class="d-none"></div>
    <template id="nso-residue-removal-tpl"></template>`;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      json: async () => ({
        onboarded: true,
        running: null,
        last: null,
        jobs: [],
        generations: [],
        blocked_removals: [],
        residue_removals: [],
        apply_state: applyState,
        apply_state_error: applyStateError,
      }),
    })),
  );
  new Function(jobActivityScript())();
  return document.getElementById("nso-blocked-generation");
}

describe("the device tab's blocked Apply head panel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  it("targets both actions at the exact blocked generation returned by the poll", async () => {
    const panel = mountTab({
      device_id: 10,
      head: {
        generation_id: 73,
        seq: 11,
        status: "failed",
        job_id: 901,
        mode: "networked",
        settlement_cohort: 17,
        sections: ["vlan", "switchport"],
        source_push_seq: { vlan: 44, switchport: 45 },
        created_at: "2026-08-25T12:00:00Z",
        updated_at: "2026-08-25T12:01:00Z",
      },
      blocked: true,
      write_work_pending: false,
      held_jobs: [902, 903],
      pending_generations: 2,
      last_apply_job: null,
    });

    await vi.waitFor(() => expect(panel.classList.contains("d-none")).toBe(false));
    expect(panel.querySelector('[data-slot="generation"]').textContent).toBe("73");
    expect(panel.querySelector('[data-slot="status"]').textContent).toBe("failed");
    expect(panel.querySelector('[data-slot="sections"]').textContent).toBe("vlan, switchport");
    expect(panel.querySelector('[data-slot="pending"]').textContent).toBe("2");
    expect(panel.querySelector('[data-slot="held"]').textContent).toBe("902, 903");
    expect([...panel.querySelectorAll('input[name="generation_id"]')].map((input) => input.value)).toEqual([
      "73",
      "73",
    ]);
  });

  it("stays hidden when the device has no blocked executable head", async () => {
    const panel = mountTab({
      device_id: 10,
      head: null,
      blocked: false,
      write_work_pending: false,
      held_jobs: [],
      pending_generations: 0,
      last_apply_job: null,
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(panel.classList.contains("d-none")).toBe(true);
  });

  it("shows an Apply-state polling failure without fabricating an unblocked state", async () => {
    const panel = mountTab(null, "Adapter returned HTTP 503.");
    const error = document.getElementById("nso-apply-state-error");

    await vi.waitFor(() => expect(error.classList.contains("d-none")).toBe(false));
    expect(error.querySelector('[data-slot="message"]').textContent).toBe("Adapter returned HTTP 503.");
    expect(panel.classList.contains("d-none")).toBe(true);
  });
});
