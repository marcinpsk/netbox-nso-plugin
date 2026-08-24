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
      { generation_id: 81, seq: 81 },
      { generation_id: 82, seq: 82 },
    ]);

    expect(new URL(url).searchParams.getAll("generation_id")).toEqual(["81", "82"]);
    expect(new URL(url).searchParams.get("since_seq")).toBe("80");
  });
});

describe("pollRequestOptions", () => {
  const platformTimeout = AbortSignal.timeout;

  it("bounds each generation-chain request to ten seconds", () => {
    const signal = {};
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(signal);

    try {
      expect(C.pollRequestOptions()).toEqual({
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal,
      });
      expect(timeout).toHaveBeenCalledWith(10_000);
    } finally {
      timeout.mockRestore();
    }
  });

  it("leaves the platform timeout implementation intact", () => {
    expect(AbortSignal.timeout).toBe(platformTimeout);
  });
});

describe("firstJobId", () => {
  it("uses the first job attached anywhere in the generation chain", () => {
    expect(C.firstJobId([{ generation_id: 81, job_id: null }, { generation_id: 82, job_id: 503 }])).toBe(503);
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

  it("does not schedule or tick again when start is called twice", () => {
    const schedule = vi.fn(() => 37);
    const tick = vi.fn();
    const timer = C.createPollTimer(tick, 2000, schedule, vi.fn());

    timer.start();
    timer.start();

    expect(schedule).toHaveBeenCalledOnce();
    expect(tick).toHaveBeenCalledOnce();
  });
});
