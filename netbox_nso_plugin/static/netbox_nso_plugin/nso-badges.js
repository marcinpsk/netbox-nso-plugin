/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Category-header badge + read-chip rendering for the device NSO tab (READSEM S4
 * D10). The server renders the initial badges; this module rebuilds them from the
 * category-counts JSON (NSOCategoryCountsView) after state-changing actions, so
 * chips survive the nso:refresh-categories rebuild. The read chip's css/label/tip
 * come from the SERVER (summary._CHIP_RENDER — one source of truth); this module
 * only escapes and assembles. */
(function () {
  "use strict";

  /* Escape for BOTH text and attribute contexts — textContent/innerHTML escaping
   * leaves quotes intact, which lets a hostile value break out of an attribute. */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* One D10 read chip from a counts-JSON `read` object ({state, css, label, tip}
   * or null). Healthy = null = NO chip. */
  function chipHtml(read) {
    if (!read || !read.state) {
      return "";
    }
    return '<span class="badge ' + esc(read.css) + ' ms-2" title="' + esc(read.tip) + '">' + esc(read.label) + "</span>";
  }

  /* Rebuild a category header's badge block (mirrors the server-side render). */
  function renderBadges(total, drift, pending, read) {
    var h = '<span class="text-muted ms-1">(' + esc(total) + ")</span>";
    var driftTip =
      "Objects whose on-box content differs from what NetBox holds and that NetBox does not own — open one to see the diff, then Accept or update NetBox.";
    var pendingTip =
      "Objects NetBox owns (Accepted) that have not yet been applied to / confirmed on the device. Run Apply to push them.";
    var syncTip = "Every object in this category agrees with NetBox — nothing drifted and nothing awaiting apply.";
    if (drift) {
      h += '<span class="badge text-bg-warning text-dark ms-2" title="' + driftTip + '">' + esc(drift) + " drift</span>";
    }
    if (pending) {
      h += '<span class="badge text-bg-info ms-1" title="' + pendingTip + '">' + esc(pending) + " pending apply</span>";
    }
    if (total && !drift && !pending) {
      h += '<span class="badge text-bg-success ms-2" title="' + syncTip + '">in sync</span>';
    }
    h += chipHtml(read);
    return h;
  }

  /* ── settle poll (D10 "self-heals via the counts poll") ──────────────────────
   * The one refresh fired at job completion is reliably too early: the RQ
   * reconcile that applies the observations lands seconds AFTER the adapter job
   * the UI polls, so chips freeze on refresh_pending (or a pre-recovery state)
   * until the next user action. The tab keeps re-fetching counts on a short
   * bounded interval until the chips stop moving and nothing is transient. */

  /* Collapse a counts-JSON `categories` object to {key: chipState|null}. */
  function chipStates(categories) {
    var out = {};
    Object.keys(categories || {}).forEach(function (k) {
      var read = categories[k] && categories[k].read;
      out[k] = read && read.state ? read.state : null;
    });
    return out;
  }

  /* Poll again? First tick and fetch failures always retry (the caller bounds
   * the loop); otherwise keep going while states still move or any chip is in a
   * transient state — refresh_pending and reset_pending both mean a reconcile is
   * known to be in flight for it (codex B5-F6). */
  function needsAnotherTick(prev, curr) {
    if (!prev || !curr) {
      return true;
    }
    if (JSON.stringify(prev) !== JSON.stringify(curr)) {
      return true;
    }
    return Object.keys(curr).some(function (k) {
      return curr[k] === "refresh_pending" || curr[k] === "reset_pending";
    });
  }

  /* Generation gate (codex B5-F7): counts responses may resolve out of order —
   * only the newest-started fetch may write the DOM. */
  function makeGenGate() {
    var gen = 0;
    return {
      next: function () {
        gen += 1;
        return gen;
      },
      isCurrent: function (g) {
        return g === gen;
      },
    };
  }

  window.NSOBadges = {
    esc: esc,
    chipHtml: chipHtml,
    renderBadges: renderBadges,
    chipStates: chipStates,
    needsAnotherTick: needsAnotherTick,
    makeGenGate: makeGenGate,
  };
})();
