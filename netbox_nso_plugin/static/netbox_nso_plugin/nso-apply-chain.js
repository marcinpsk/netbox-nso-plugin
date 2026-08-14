/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  "use strict";

  function correlate(expectedGenerations, reportedGenerations, jobs) {
    var expected = expectedGenerations || [];
    var reportedById = new Map(
      (reportedGenerations || []).map(function (generation) {
        return [String(generation.generation_id), generation];
      }),
    );
    var jobsById = new Map(
      (jobs || []).map(function (job) {
        return [String(job.id), job];
      }),
    );
    var missingGenerationIds = expected
      .filter(function (generation) {
        return !reportedById.has(String(generation.generation_id));
      })
      .map(function (generation) {
        return generation.generation_id;
      });
    if (missingGenerationIds.length) {
      return { outcome: "surface_gap", missingGenerationIds: missingGenerationIds };
    }

    var links = expected.map(function (expectedGeneration) {
      var generation = reportedById.get(String(expectedGeneration.generation_id));
      var jobId = generation.job_id == null ? null : generation.job_id;
      return {
        generationId: expectedGeneration.generation_id,
        generationStatus: generation.status || null,
        jobId: jobId,
        job: jobId == null ? null : jobsById.get(String(jobId)) || null,
      };
    });
    return { outcome: "correlated", links: links };
  }

  function pollUrl(baseUrl, expectedGenerations) {
    var url = new URL(baseUrl, window.location.href);
    (expectedGenerations || []).forEach(function (generation) {
      if (generation.generation_id != null) {
        url.searchParams.append("generation_id", String(generation.generation_id));
      }
    });
    return url.toString();
  }

  function createPollGuard() {
    var stopped = false;
    var inFlight = false;

    return {
      enter: function () {
        if (stopped || inFlight) return false;
        inFlight = true;
        return true;
      },
      leave: function () {
        inFlight = false;
      },
      stop: function () {
        if (stopped) return false;
        stopped = true;
        return true;
      },
      canWrite: function () {
        return !stopped;
      },
    };
  }

  function createPollTimer(tick, delay, schedule, cancel) {
    var interval = null;
    var setTimer = schedule || window.setInterval.bind(window);
    var clearTimer = cancel || window.clearInterval.bind(window);

    return {
      start: function () {
        interval = setTimer(tick, delay);
        tick();
      },
      stop: function () {
        if (interval == null) return;
        clearTimer(interval);
        interval = null;
      },
    };
  }

  window.NSOApplyChain = {
    correlate: correlate,
    pollUrl: pollUrl,
    createPollGuard: createPollGuard,
    createPollTimer: createPollTimer,
  };
})();
