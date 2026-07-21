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

  window.NSOBadges = { esc: esc, chipHtml: chipHtml, renderBadges: renderBadges };
})();
