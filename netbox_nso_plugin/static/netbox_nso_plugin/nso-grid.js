/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* nso-grid — the shared Tabulator harness behind every NSO-tab category grid.
 *
 * Extracted from the Interfaces grid, which was the only panel with one. Everything
 * that is the SAME for every category lives here; a panel supplies only its column
 * definitions. Without this the machinery below would be copy-pasted per panel, and
 * a per-panel copy is how the badge vocabulary and the kind derivation drift apart.
 *
 * There is deliberately NO client-side kind derivation here. A cell's kind decides its
 * badge, whether it offers Accept, and which quick-filter pill counts it, so a second
 * implementation that drifts from the server's would show the operator a row that its
 * own filter then hides. The server already owns this (summary.display_state /
 * interface_row_state — and note those two disagree about apply_failed on purpose), so
 * the payload ships `kind` per cell and `state` per row, and we only render them.
 * _table_filter.html still carries its own copy for the server-rendered tables that
 * have not become grids yet; a panel drops it when it does.
 *
 * Row contract (what a panel's payload rows look like):
 *   { state: "<kind>", ...cells }
 * Cell contract (any value cell the grid can badge / accept / edit):
 *   { value, kind, label, status, accept_url, edit_url, pk }
 * A cell may be null — the column renders an em-dash.
 *
 * Inline edit is wired by opts.cellKeys, NOT by a custom column key: Tabulator validates
 * column definitions and warns on anything it does not recognise.
 *
 * mount(root, opts) -> { table, reload }
 *   opts.payload    { rows, counts, adapter_error }
 *   opts.jsonUrl    re-fetched after every action (post-action reload)
 *   opts.columns    Tabulator column defs
 *   opts.flatten    optional (rows) -> rows, to add sort/filter helper fields
 *   opts.colFields  { toggleKey: tabulatorField } for the show/hide button group
 *   opts.cellKeys   { tabulatorField: rowCellKey } — which cell an editable column edits
 *   opts.key        window-global slot so a re-render destroys the old table
 *   opts.extract    optional (categoryJson) -> { rows, counts }. A panel with several
 *                   sub-tables (OSPF = instances + interfaces) mounts one grid per
 *                   table against the SAME category JSON; each pulls its own section.
 */
(function () {
  "use strict";

  // ── shared vocabulary ───────────────────────────────────────────────────────
  // Identical to _state_badge.html. One definition, so a panel cannot invent a
  // fifth spelling of "drift".
  var BADGE = {
    drift: ["text-bg-warning text-dark", "drift"],
    pending: ["text-bg-info", "pending apply"],
    apply_failed: ["text-bg-danger", "apply failed"],
    deploying: ["text-bg-primary", "deploying"],
    in_sync: ["text-bg-success", "in sync"],
    unknown: ["text-bg-info", "unknown"],
  };
  // Severity order for the State column's sorter — worst first, so the rows an
  // operator must act on sort to the top.
  var SEVERITY = ["apply_failed", "drift", "pending", "deploying", "unknown", "in_sync"];
  // Statuses that mean "NetBox does not own this yet" — mirrors _accept_cell.html.
  var PENDING_KINDS = ["pending", "apply_failed"];
  var MUTED = '<span class="text-muted">—</span>';

  /* Escape for BOTH text and quoted-attribute contexts.
   *
   * The obvious textContent->innerHTML trick escapes & < > but NOT quotes, and half
   * the formatters below interpolate into double-quoted attributes
   * (data-accept="...", href="...", title="..."). A value carrying a `"` would close
   * the attribute early and let the rest inject its own. The values are device-supplied
   * (interface descriptions, route-map names, VRF names), so quotes must go too. */
  var ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) {
    return s == null
      ? ""
      : String(s).replace(/[&<>"']/g, function (c) {
          return ESCAPES[c];
        });
  }

  function badge(kind, label) {
    var b = BADGE[kind] || BADGE.unknown;
    return '<span class="badge ' + b[0] + '">' + esc(label || b[1]) + "</span>";
  }

  /* Value cells stay quiet when in sync (compact) — only trouble gets a badge. */
  function cellBadge(cell) {
    if (!cell) return "";
    if (cell.kind === "in_sync") {
      return cell.status === "in_sync"
        ? ' <span class="mdi mdi-check-circle text-success" title="In sync — NetBox owns this and the device matches."></span>'
        : "";
    }
    return " " + badge(cell.kind, cell.label);
  }

  function acceptBtn(cell) {
    if (!cell || !cell.accept_url) return "";
    return (
      ' <button type="button" class="btn btn-xs btn-outline-success nso-cell-accept" data-accept="' +
      esc(cell.accept_url) +
      '" title="Make NetBox the source of truth for this (re-applied to the device on Apply)">' +
      '<span class="mdi mdi-check"></span></button>'
    );
  }

  function csrfToken() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (el && el.value) return el.value;
    return (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  }

  function post(url, params) {
    params.set("csrfmiddlewaretoken", csrfToken());
    return fetch(url, {
      method: "POST",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrfToken(),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: params,
    }).then(function (r) {
      return r
        .json()
        .catch(function () {
          return {};
        })
        .then(function (d) {
          return { ok: r.ok, data: d };
        });
    });
  }

  /* A plain value cell: the value, then a badge only if it is not in sync, then
   * Accept if the row is still unowned. The common case for most columns. */
  function valueFormatter(key, render) {
    return function (cell) {
      var c = cell.getRow().getData()[key];
      if (!c) return MUTED;
      var body = render ? render(c, cell.getRow().getData()) : c.value == null || c.value === "" ? MUTED : esc(c.value);
      return body + cellBadge(c) + acceptBtn(c);
    };
  }

  /* Trailing Accept column for the row-level-ownership panels.
   *
   * Interfaces accepts per CELL (each attribute is its own overlay row with its own
   * status). Every routing overlay instead owns the whole row — one status, one
   * accept_url — which is what _accept_cell.html renders server-side today. Such a
   * panel gets this column and leaves the cells plain. The button only appears for
   * not-yet-owned statuses, exactly like _accept_cell.html. */
  function acceptColumn(extra) {
    var col = {
      title: "",
      field: "accept_url",
      headerSort: false,
      widthGrow: 0.4,
      minWidth: 52,
      formatter: function (cell) {
        return acceptBtn(cell.getRow().getData()) || "";
      },
    };
    return Object.assign(col, extra || {});
  }

  /* The State column every row-owned panel uses: the status badge, plus the residue
   * badge when this row is a retraction's on-device leftover (what _state_badge.html
   * rendered server-side). Built in once here so seven panels cannot each spell it
   * differently — or quietly forget the residue badge, which is the whole point of
   * being able to attribute a re-imported husk instead of reading it as new config. */
  function stateColumn(extra) {
    var col = {
      title: "State",
      field: "state",
      formatter: function (cell) {
        var d = cell.getRow().getData();
        var out = badge(d.state, d.label);
        if (d.residue) {
          // Same wording and explanation as _state_badge.html — an operator must not have
          // to learn two names for the same thing depending on which panel they are on.
          out +=
            ' <span class="badge text-bg-warning text-dark ms-1" title="A retraction (adapter job #' +
            esc(d.residue_job) +
            ') reported success but this key survived on the device — it re-imported here as an ' +
            'unowned mirror, not new device config. Clean up on-device via a gated commit, or ' +
            'Accept to adopt it as intent.">removal residue</span>';
        }
        return out;
      },
      widthGrow: 0.9,
      minWidth: 110,
      sorter: function (a, b) {
        return SEVERITY.indexOf(a) - SEVERITY.indexOf(b);
      },
      headerFilter: "list",
      headerFilterParams: {
        values: {
          "": "All",
          drift: "Drift",
          pending: "Pending apply",
          apply_failed: "Apply failed",
          in_sync: "In sync",
        },
      },
    };
    return Object.assign(col, extra || {});
  }

  /* A resolved netbox-routing object ({label, url}), or an em-dash when the overlay
   * never matched one. Server sends null rather than a half-built link. */
  function linkCell(obj) {
    if (!obj || !obj.url) return MUTED;
    return '<a href="' + esc(obj.url) + '">' + esc(obj.label) + "</a>";
  }

  /* Compact "Last Synced" column — every routing panel carries one. */
  function lastSyncColumn(extra) {
    var col = {
      title: "Last Synced",
      field: "last_sync",
      widthGrow: 1,
      minWidth: 118,
      formatter: function (cell) {
        return cell.getValue() ? esc(cell.getValue()) : MUTED;
      },
    };
    return Object.assign(col, extra || {});
  }

  /* Yes/no style badge pair (passive/active, disabled, …). */
  function boolBadge(on, onLabel, onClass, offLabel, offClass) {
    return on
      ? '<span class="badge ' + onClass + '">' + esc(onLabel) + "</span>"
      : offLabel
        ? '<span class="badge ' + offClass + '">' + esc(offLabel) + "</span>"
        : MUTED;
  }

  function mount(root, opts) {
    if (!root || typeof Tabulator === "undefined") return null;
    var tableEl = root.querySelector(".nso-grid-table");
    if (!tableEl) return null;

    var flatten =
      opts.flatten ||
      function (rows) {
        return rows;
      };

    /* Which slice of the category JSON this grid renders. A single-table panel is the
     * whole document; a multi-table one (OSPF instances + interfaces) mounts one grid
     * per section against the SAME JSON, each picking its own. */
    var pick =
      opts.extract ||
      function (json) {
        return json;
      };

    function flash(text, type) {
      var m = root.querySelector(".nso-grid-msg");
      if (!m) return;
      m.innerHTML = text
        ? '<div class="alert alert-' + type + ' py-1 px-2 small mb-2">' + esc(text) + "</div>"
        : "";
    }

    var slot = "__nsoGrid_" + (opts.key || "default");
    if (window[slot]) {
      try {
        window[slot].destroy();
      } catch (e) {
        /* stale node from a previous fragment render — replaced below */
      }
    }

    var table = new Tabulator(tableEl, {
      data: flatten(((pick(opts.payload) || {}).rows || []).slice()),
      layout: "fitColumns",
      maxHeight: opts.maxHeight || "540px",
      placeholder: opts.placeholder || "Nothing here yet — click Refresh from NSO or wait for the next sync.",
      columns: opts.columns,
    });
    window[slot] = table;

    // Column show/hide, remembered across re-renders within the page.
    var colFields = opts.colFields || {};
    var hiddenSlot = "__nsoGridHidden_" + (opts.key || "default");
    var hidden = (window[hiddenSlot] = window[hiddenSlot] || {});
    table.on("tableBuilt", function () {
      Object.keys(hidden).forEach(function (k) {
        if (hidden[k] && colFields[k]) table.hideColumn(colFields[k]);
      });
      root.querySelectorAll(".nso-grid-cols [data-col]").forEach(function (b) {
        b.classList.toggle("active", !hidden[b.dataset.col]);
      });
    });

    function reload() {
      return fetch(opts.jsonUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (fresh) {
          var section = pick(fresh) || {};
          table.replaceData(flatten((section.rows || []).slice()));
          // Counts are scoped to THIS grid's root, so sibling sections keep their own.
          Object.keys(section.counts || {}).forEach(function (k) {
            var el = root.querySelector(".nso-grid-n-" + k);
            if (el) el.textContent = section.counts[k];
          });
        })
        .catch(function (e) {
          flash("Failed to refresh: " + e.message, "danger");
        });
    }

    function afterAction(res) {
      if (!res.ok || (res.data && res.data.status === "error")) {
        flash("Action failed: " + ((res.data && res.data.message) || "server error"), "danger");
      } else {
        flash("", "");
        // Other panels show counts for the same overlays — let them re-read.
        document.dispatchEvent(new CustomEvent("nso:refresh-categories"));
      }
      return reload();
    }

    /* Inline edit == take ownership (status -> accepted), exactly like Accept.
     * opts.cellKeys says which cell of the row an editable column writes through to. */
    var cellKeyByField = opts.cellKeys || {};
    table.on("cellEdited", function (cell) {
      var key = cellKeyByField[cell.getField()];
      var target = key && cell.getRow().getData()[key];
      if (!target || !target.edit_url) {
        cell.restoreOldValue();
        return;
      }
      post(target.edit_url, new URLSearchParams({ value: cell.getValue() })).then(afterAction);
    });

    root.addEventListener("click", function (e) {
      // Per-cell Accept (delegated — cells re-render on every data swap).
      var acc = e.target.closest(".nso-cell-accept");
      if (acc) {
        e.preventDefault();
        acc.disabled = true;
        post(acc.dataset.accept, new URLSearchParams()).then(afterAction);
        return;
      }
      // Quick-filter pills. The buckets must match the server's counts exactly.
      var pill = e.target.closest(".nso-grid-state [data-state]");
      if (pill) {
        e.preventDefault();
        root.querySelectorAll(".nso-grid-state [data-state]").forEach(function (b) {
          b.classList.toggle("active", b === pill);
        });
        var s = pill.dataset.state;
        if (s === "all") table.clearFilter(true);
        else if (s === "drift") table.setFilter("state", "=", "drift");
        else if (s === "pending")
          table.setFilter(function (d) {
            return PENDING_KINDS.indexOf(d.state) !== -1;
          });
        else
          table.setFilter(function (d) {
            return d.state !== "drift" && PENDING_KINDS.indexOf(d.state) === -1;
          });
        return;
      }
      var colBtn = e.target.closest(".nso-grid-cols [data-col]");
      if (colBtn) {
        e.preventDefault();
        var key = colBtn.dataset.col;
        hidden[key] = !hidden[key];
        colBtn.classList.toggle("active", !hidden[key]);
        if (colFields[key]) table.toggleColumn(colFields[key]);
      }
    });

    // popedit popovers live inside grid cells; their save bubbles up to us.
    root.addEventListener("nso:popedit-saved", function () {
      reload();
    });

    if (opts.payload.adapter_error) flash("Adapter: " + opts.payload.adapter_error, "warning");

    return { table: table, reload: reload, flash: flash };
  }

  window.NSOGrid = {
    mount: mount,
    badge: badge,
    cellBadge: cellBadge,
    acceptBtn: acceptBtn,
    valueFormatter: valueFormatter,
    stateColumn: stateColumn,
    acceptColumn: acceptColumn,
    lastSyncColumn: lastSyncColumn,
    boolBadge: boolBadge,
    linkCell: linkCell,
    esc: esc,
    post: post,
    MUTED: MUTED,
    SEVERITY: SEVERITY,
  };
})();
