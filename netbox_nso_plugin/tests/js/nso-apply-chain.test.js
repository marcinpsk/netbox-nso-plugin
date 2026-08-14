/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { describe, expect, it, vi } from "vitest";
import "../../static/netbox_nso_plugin/nso-apply-chain.js";

const C = window.NSOApplyChain;

describe("correlate", () => {
  const expected = [
    { generation_id: 81, job_id: 501 },
    { generation_id: 82, job_id: null },
  ];

  it("does not attribute a concurrent later Apply job to an unattached generation", () => {
    const generations = [
      { generation_id: 81, status: "settled", job_id: 501 },
      { generation_id: 82, status: "pending", job_id: null },
    ];
    const jobs = [
      { id: 502, type: "apply", status: "running" },
      { id: 501, type: "apply", status: "succeeded" },
    ];

    const result = C.correlate(expected, generations, jobs);
    const links = result.links;

    expect(result.outcome).toBe("correlated");
    expect(links[1].jobId).toBe(null);
    expect(links[1].job).toBe(null);
  });

  it("uses the job attached to the matching generation", () => {
    const generations = [
      { generation_id: 81, status: "settled", job_id: 501 },
      { generation_id: 82, status: "running", job_id: 503 },
    ];
    const jobs = [
      { id: 503, type: "removal", status: "running" },
      { id: 502, type: "apply", status: "running" },
      { id: 501, type: "apply", status: "succeeded" },
    ];

    const result = C.correlate(expected, generations, jobs);
    const links = result.links;

    expect(result.outcome).toBe("correlated");
    expect(links[1].jobId).toBe(503);
    expect(links[1].job.id).toBe(503);
  });

  it("reports a surface gap when the listing omits a requested generation", () => {
    const generations = [{ generation_id: 81, status: "settled", job_id: 501 }];
    const jobs = [{ id: 501, type: "apply", status: "succeeded" }];

    const result = C.correlate(expected, generations, jobs);

    expect(result).toEqual({ outcome: "surface_gap", missingGenerationIds: [82] });
  });
});

describe("pollUrl", () => {
  it("requests the generation ids from the Apply response on the first poll", () => {
    const url = C.pollUrl("https://netbox.example/plugins/nso/devices/10/jobs/", [
      { generation_id: 81 },
      { generation_id: 82 },
    ]);

    expect(new URL(url).searchParams.getAll("generation_id")).toEqual(["81", "82"]);
  });
});

describe("createPollGuard", () => {
  it("prevents overlapping ticks and blocks all work after stop", () => {
    const guard = C.createPollGuard();

    expect(guard.enter()).toBe(true);
    expect(guard.enter()).toBe(false);
    guard.leave();
    expect(guard.enter()).toBe(true);
    expect(guard.stop()).toBe(true);
    expect(guard.canWrite()).toBe(false);
    expect(guard.stop()).toBe(false);
    guard.leave();
    expect(guard.enter()).toBe(false);
  });
});

describe("createPollTimer", () => {
  it("cancels the assigned interval when the first tick stops synchronously", () => {
    const schedule = vi.fn(() => 37);
    const cancel = vi.fn();
    let timer;
    const tick = vi.fn(() => timer.stop());
    timer = C.createPollTimer(tick, 2000, schedule, cancel);

    timer.start();

    expect(schedule).toHaveBeenCalledWith(tick, 2000);
    expect(tick).toHaveBeenCalledOnce();
    expect(cancel).toHaveBeenCalledWith(37);
  });
});
