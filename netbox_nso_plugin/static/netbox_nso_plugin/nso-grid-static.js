/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact static-route grid. Route identity stays fixed; native policy edits inline. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function badge(text, classes, title) {
    var item = document.createElement("span");
    item.className = "badge " + classes;
    item.textContent = text;
    if (title) item.title = title;
    return item;
  }

  function fmtDestination(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    var prefix = row.route ? document.createElement("a") : document.createElement("span");
    if (row.route) prefix.href = row.route.url;
    prefix.className = "font-monospace";
    prefix.textContent = row.prefix || "—";
    wrap.appendChild(prefix);
    var vrf = document.createElement("div");
    vrf.className = "small text-muted text-truncate";
    vrf.textContent = "VRF " + (row.vrf || "global");
    wrap.appendChild(vrf);
    return wrap;
  }

  function fmtNextHop(cell) {
    var value = cell.getRow().getData().next_hop;
    var span = document.createElement("span");
    span.className = "font-monospace";
    span.textContent = value || "—";
    return span;
  }

  function fmtPolicy(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    wrap.appendChild(badge("metric " + (row.metric == null ? "default" : row.metric), "text-bg-light border"));
    if (row.permanent) wrap.appendChild(badge("permanent", "text-bg-info"));
    if (row.tag != null) wrap.appendChild(badge("tag " + row.tag, "text-bg-secondary"));

    if (row.edit_url) {
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Edit route policy (editing takes ownership)";
      edit.setAttribute("aria-label", "Edit static route " + row.prefix);
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = row.prefix + " policy";
      edit.dataset.peFields = "metric:text:Metric,permanent:select:Permanent,tag:text:Tag";
      edit.setAttribute("data-pe-v-metric", row.metric == null ? "" : row.metric);
      edit.setAttribute("data-pe-v-permanent", row.permanent ? "True" : "False");
      edit.setAttribute("data-pe-v-tag", row.tag == null ? "" : row.tag);
      edit.setAttribute(
        "data-pe-o-permanent",
        JSON.stringify([
          { value: "False", label: "No" },
          { value: "True", label: "Yes" },
        ]),
      );
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function mount(root) {
    var payloadEl = document.getElementById("nso-static-data");
    if (!root || !payloadEl || !G) return;
    return G.mount(root, {
      key: "static",
      payload: JSON.parse(payloadEl.textContent),
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: "No static routes.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._destination = [row.prefix || "", row.vrf || "global"].join(" ");
          row._policy = [row.metric == null ? "" : row.metric, row.permanent ? "permanent" : "", row.tag || ""].join(
            " ",
          );
          return row;
        });
      },
      columns: [
        {
          title: "Destination",
          field: "_destination",
          formatter: fmtDestination,
          sorter: "alphanum",
          widthGrow: 1.4,
          minWidth: 160,
          headerFilter: "input",
          headerFilterPlaceholder: "prefix / VRF…",
        },
        {
          title: "Next hop",
          field: "next_hop",
          formatter: fmtNextHop,
          widthGrow: 1,
          minWidth: 125,
          headerFilter: "input",
        },
        { title: "Policy", field: "_policy", formatter: fmtPolicy, widthGrow: 1.5, minWidth: 190 },
        G.stateColumn({ widthGrow: 0.6, minWidth: 85 }),
        G.lastSyncColumn({ widthGrow: 0.7, minWidth: 95 }),
        G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
      ],
    });
  }

  window.NSOGridStatic = { mount: mount };
})();
