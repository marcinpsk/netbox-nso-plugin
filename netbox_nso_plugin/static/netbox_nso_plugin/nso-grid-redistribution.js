/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact redistribution grid: protocol identity stays fixed; policy knobs edit inline. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function protocolLabel(value) {
    var labels = { bgp: "BGP", connected: "Connected", isis: "IS-IS", ospf: "OSPF", static: "Static" };
    return labels[value] || (value ? value.toUpperCase() : "—");
  }

  function badge(text, classes, title) {
    var item = document.createElement("span");
    item.className = "badge " + classes;
    item.textContent = text;
    if (title) item.title = title;
    return item;
  }

  function protocolFormatter(protocolField, refField) {
    return function (cell) {
      var row = cell.getRow().getData();
      var wrap = document.createElement("div");
      var protocol = document.createElement("span");
      protocol.textContent = protocolLabel(row[protocolField]);
      wrap.appendChild(protocol);
      if (row[refField]) {
        var ref = document.createElement("div");
        ref.className = "small text-muted text-truncate";
        ref.title = row[refField];
        ref.textContent = row[refField];
        wrap.appendChild(ref);
      }
      return wrap;
    };
  }

  function fmtPolicy(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    if (row.route_map) wrap.appendChild(badge(row.route_map, "text-bg-info", "Route map " + row.route_map));
    if (row.metric != null) wrap.appendChild(badge("metric " + row.metric, "text-bg-light border"));
    if (row.metric_type) wrap.appendChild(badge("type " + row.metric_type, "text-bg-secondary"));
    if (!row.route_map && row.metric == null && !row.metric_type) {
      var empty = document.createElement("span");
      empty.className = "text-muted small";
      empty.textContent = "default policy";
      wrap.appendChild(empty);
    }

    if (row.edit_url) {
      var fields = ["route_map:text:Route map", "metric:text:Metric"];
      if ((row.metric_type_options || []).length > 1) fields.push("metric_type:select:Metric type");
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Edit redistribution policy (editing takes ownership)";
      edit.setAttribute("aria-label", "Edit redistribution policy");
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = protocolLabel(row.source_protocol) + " into " + protocolLabel(row.dest_protocol);
      edit.dataset.peFields = fields.join(",");
      edit.setAttribute("data-pe-v-route_map", row.route_map || "");
      edit.setAttribute("data-pe-v-metric", row.metric == null ? "" : row.metric);
      if (fields.length === 3) {
        edit.setAttribute("data-pe-v-metric_type", row.metric_type || "");
        edit.setAttribute("data-pe-o-metric_type", JSON.stringify(row.metric_type_options));
      }
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function fmtActions(cell) {
    var row = cell.getRow().getData();
    var diff =
      '<a href="' +
      G.esc(row.diff_url) +
      '" class="btn btn-xs btn-outline-warning" title="See exactly what differs between this device and NetBox"><span class="mdi mdi-not-equal-variant"></span></a>';
    return '<span class="d-flex gap-1">' + diff + (G.acceptBtn(row) || "") + "</span>";
  }

  function mount(root) {
    var payloadEl = document.getElementById("nso-redist-data");
    if (!root || !payloadEl || !G) return;
    return G.mount(root, {
      key: "redistribution",
      payload: JSON.parse(payloadEl.textContent),
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: "No redistribution.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._destination = [row.dest_protocol || "", row.dest_ref || ""].join(" ");
          row._source = [row.source_protocol || "", row.source_ref || ""].join(" ");
          row._policy = [row.route_map || "", row.metric == null ? "" : row.metric, row.metric_type || ""].join(" ");
          return row;
        });
      },
      columns: [
        {
          title: "Destination",
          field: "_destination",
          formatter: protocolFormatter("dest_protocol", "dest_ref"),
          sorter: "alphanum",
          widthGrow: 1,
          minWidth: 125,
          headerFilter: "input",
          headerFilterPlaceholder: "protocol / ref…",
        },
        {
          title: "Source",
          field: "_source",
          formatter: protocolFormatter("source_protocol", "source_ref"),
          sorter: "alphanum",
          widthGrow: 0.9,
          minWidth: 110,
          headerFilter: "input",
          headerFilterPlaceholder: "protocol / ref…",
        },
        { title: "Policy", field: "_policy", formatter: fmtPolicy, widthGrow: 1.8, minWidth: 210 },
        G.stateColumn({ widthGrow: 0.6, minWidth: 85 }),
        G.lastSyncColumn({ widthGrow: 0.7, minWidth: 95 }),
        { title: "", field: "diff_url", headerSort: false, widthGrow: 0.5, minWidth: 78, formatter: fmtActions },
      ],
    });
  }

  window.NSOGridRedistribution = { mount: mount };
})();
