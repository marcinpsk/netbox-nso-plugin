/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact IS-IS grids. Authentication stays visible but never enters an inline form. */
(function () {
  "use strict";

  var G = window.NSOGrid;
  var TRI_STATE = [
    { value: "", label: "Device default" },
    { value: "True", label: "Enabled" },
    { value: "False", label: "Disabled" },
  ];
  var CIRCUIT_TYPES = [
    { value: "", label: "Device default" },
    { value: "level-1", label: "Level-1" },
    { value: "level-2-only", label: "Level-2 only" },
    { value: "level-1-2", label: "Level-1-2" },
  ];
  var NETWORK_TYPES = [
    { value: "", label: "Device default" },
    { value: "point-to-point", label: "Point-to-point" },
    { value: "broadcast", label: "Broadcast" },
  ];
  var METRIC_STYLES = [
    { value: "", label: "Device default" },
    { value: "wide", label: "Wide" },
    { value: "narrow", label: "Narrow" },
    { value: "transition", label: "Transition" },
  ];
  var FAST_REROUTE = [
    { value: "", label: "Device default" },
    { value: "lfa", label: "LFA" },
    { value: "remote-lfa", label: "Remote LFA" },
    { value: "ti-lfa", label: "TI-LFA" },
  ];
  var FRR_PROTECTION = [
    { value: "", label: "Device default" },
    { value: "link", label: "Link protection" },
    { value: "node", label: "Node protection" },
  ];

  function value(v) {
    return v == null ? "" : String(v);
  }

  function editAnchor(row, title, fields, values, options) {
    if (!row.edit_url) return null;
    var edit = document.createElement("a");
    edit.href = "#";
    edit.className = "nso-popedit text-secondary";
    edit.title = "Edit " + title + " (editing takes ownership)";
    edit.setAttribute("aria-label", "Edit " + title);
    edit.dataset.peUrl = row.edit_url;
    edit.dataset.peTitle = title;
    edit.dataset.peFields = fields;
    Object.keys(values).forEach(function (name) {
      edit.setAttribute("data-pe-v-" + name, value(values[name]));
    });
    Object.keys(options || {}).forEach(function (name) {
      edit.setAttribute("data-pe-o-" + name, JSON.stringify(options[name]));
    });
    edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
    return edit;
  }

  function badge(label, css, title) {
    var span = document.createElement("span");
    span.className = "badge " + css;
    span.textContent = label;
    if (title) span.title = title;
    return span;
  }

  function fmtIface(cell) {
    var row = cell.getRow().getData();
    var link = document.createElement("a");
    link.href = row.iface.url;
    link.title = "Open interface in NetBox";
    link.textContent = row.iface.name;
    return link;
  }

  function fmtBinding(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "text-nowrap";
    var tag = document.createElement("span");
    tag.textContent = row.process_tag || "default";
    wrap.appendChild(tag);
    var af = document.createElement("span");
    af.className = "badge text-bg-light border ms-2";
    af.textContent = row.af || "—";
    wrap.appendChild(af);
    return wrap;
  }

  function fmtInterfaceConfig(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-2 flex-wrap";

    var topology = document.createElement("span");
    topology.className = "small text-nowrap";
    topology.textContent =
      (row.circuit_type || "default circuit") +
      " · " +
      (row.network_type || "default network") +
      " · " +
      (row.metric == null ? "default metric" : "metric " + row.metric);
    wrap.appendChild(topology);
    wrap.appendChild(
      badge(
        row.passive ? "passive" : "active",
        row.passive ? "text-bg-warning text-dark" : "text-bg-success",
      ),
    );
    if (row.bfd_enabled != null) {
      wrap.appendChild(badge(row.bfd_enabled ? "BFD" : "BFD off", row.bfd_enabled ? "text-bg-info" : "text-bg-light border"));
    }
    if (row.frr_enabled != null || row.frr_protection) {
      var frr = row.frr_enabled === false ? "FRR off" : "FRR" + (row.frr_protection ? " " + row.frr_protection : "");
      wrap.appendChild(badge(frr, row.frr_enabled ? "text-bg-info" : "text-bg-light border"));
    }
    if (row.hello_auth) {
      wrap.appendChild(badge("auth " + row.hello_auth, "text-bg-secondary", "Hello authentication (read-only here)"));
    }

    var core = editAnchor(
      row,
      row.iface.name + " IS-IS topology",
      "circuit_type:select:Circuit type,network_type:select:Network type,metric:number:Metric,passive:select:Mode",
      {
        circuit_type: row.circuit_type,
        network_type: row.network_type,
        metric: row.metric,
        passive: row.passive ? "True" : "False",
      },
      {
        circuit_type: CIRCUIT_TYPES,
        network_type: NETWORK_TYPES,
        passive: [
          { value: "False", label: "Active" },
          { value: "True", label: "Passive" },
        ],
      },
    );
    var resilience = editAnchor(
      row,
      row.iface.name + " IS-IS resilience",
      "bfd_enabled:select:BFD,frr_enabled:select:FRR,frr_protection:select:FRR protection",
      {
        bfd_enabled: row.bfd_enabled == null ? "" : row.bfd_enabled ? "True" : "False",
        frr_enabled: row.frr_enabled == null ? "" : row.frr_enabled ? "True" : "False",
        frr_protection: row.frr_protection,
      },
      { bfd_enabled: TRI_STATE, frr_enabled: TRI_STATE, frr_protection: FRR_PROTECTION },
    );
    if (core) wrap.appendChild(core);
    if (resilience) wrap.appendChild(resilience);
    return wrap;
  }

  function fmtProcess(cell) {
    var row = cell.getRow().getData();
    var link = row.instance ? document.createElement("a") : document.createElement("span");
    if (row.instance) link.href = row.instance.url;
    link.textContent = row.process_tag || "default";
    return link;
  }

  function frrLabel(value) {
    return { lfa: "LFA", "remote-lfa": "Remote LFA", "ti-lfa": "TI-LFA" }[value] || value;
  }

  function fmtInstanceConfig(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-2 flex-wrap";
    var net = document.createElement("span");
    net.className = row.net ? "font-monospace small" : "text-muted small";
    net.textContent = row.net || "NET not set";
    wrap.appendChild(net);
    if (row.is_type) wrap.appendChild(badge(row.is_type, "text-bg-light border"));
    if (row.metric_style) wrap.appendChild(badge(row.metric_style, "text-bg-light border"));
    if (row.overload_bit != null) {
      wrap.appendChild(badge(row.overload_bit ? "overload" : "overload off", row.overload_bit ? "text-bg-warning text-dark" : "text-bg-light border"));
    }
    if (row.fast_reroute) wrap.appendChild(badge(frrLabel(row.fast_reroute), "text-bg-info"));
    if (row.microloop_avoidance != null) {
      wrap.appendChild(badge(row.microloop_avoidance ? "microloop" : "microloop off", "text-bg-light border"));
    }
    if (row.area_auth && row.area_auth !== "—") {
      wrap.appendChild(badge("area auth " + row.area_auth, "text-bg-secondary", "Authentication is read-only here"));
    }
    if (row.domain_auth && row.domain_auth !== "—") {
      wrap.appendChild(badge("domain auth " + row.domain_auth, "text-bg-secondary", "Authentication is read-only here"));
    }

    var core = editAnchor(
      row,
      (row.process_tag || "default") + " IS-IS process",
      "net:text:NET,is_type:select:IS type,metric_style:select:Metric style,overload_bit:select:Overload bit",
      {
        net: row.net,
        is_type: row.is_type,
        metric_style: row.metric_style,
        overload_bit: row.overload_bit == null ? "" : row.overload_bit ? "True" : "False",
      },
      { is_type: CIRCUIT_TYPES, metric_style: METRIC_STYLES, overload_bit: TRI_STATE },
    );
    var resilience = editAnchor(
      row,
      (row.process_tag || "default") + " IS-IS resilience",
      "fast_reroute:select:Fast reroute,microloop_avoidance:select:Microloop avoidance",
      {
        fast_reroute: row.fast_reroute,
        microloop_avoidance:
          row.microloop_avoidance == null ? "" : row.microloop_avoidance ? "True" : "False",
      },
      { fast_reroute: FAST_REROUTE, microloop_avoidance: TRI_STATE },
    );
    if (core) wrap.appendChild(core);
    if (resilience) wrap.appendChild(resilience);
    return wrap;
  }

  function interfaceColumns() {
    return [
      {
        title: "Interface",
        field: "_iface",
        formatter: fmtIface,
        sorter: "alphanum",
        widthGrow: 1,
        minWidth: 125,
        headerFilter: "input",
        headerFilterPlaceholder: "filter interface…",
      },
      { title: "Binding", field: "_binding", formatter: fmtBinding, widthGrow: 1, minWidth: 125 },
      { title: "IS-IS config", field: "_config", formatter: fmtInterfaceConfig, widthGrow: 2, minWidth: 235 },
      G.stateColumn({ widthGrow: 0.7, minWidth: 95 }),
      G.lastSyncColumn({ widthGrow: 0.7, minWidth: 105 }),
      G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
    ];
  }

  function instanceColumns() {
    return [
      {
        title: "Process",
        field: "_process",
        formatter: fmtProcess,
        sorter: "alphanum",
        widthGrow: 1,
        minWidth: 125,
        headerFilter: "input",
        headerFilterPlaceholder: "filter process…",
      },
      { title: "Process config", field: "_config", formatter: fmtInstanceConfig, widthGrow: 2.5, minWidth: 300 },
      G.stateColumn({ widthGrow: 0.8, minWidth: 100 }),
      G.lastSyncColumn({ widthGrow: 0.8, minWidth: 110 }),
      G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
    ];
  }

  function mount(root, section) {
    var payloadEl = document.getElementById("nso-isis-data");
    if (!root || !payloadEl || !G || ["instances", "interfaces"].includes(section) === false) return;
    return G.mount(root, {
      key: "isis_" + section,
      payload: JSON.parse(payloadEl.textContent),
      extract: function (json) {
        return json[section];
      },
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: section === "instances" ? "No IS-IS instances." : "No IS-IS interfaces.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._process = row.process_tag || "default";
          if (row.iface) row._iface = row.iface.name;
          row._binding = [row.process_tag || "default", row.af || ""].join(" ");
          row._config = [row.net || "", row.is_type || "", row.network_type || "", row.metric || ""].join(" ");
          return row;
        });
      },
      columns: section === "instances" ? instanceColumns() : interfaceColumns(),
    });
  }

  window.NSOGridIsis = { mount: mount };
})();
