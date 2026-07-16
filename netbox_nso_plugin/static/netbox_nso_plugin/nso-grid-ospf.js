/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact OSPF grids: group relationship fields and edit only safe intent knobs. */
(function () {
  "use strict";

  var G = window.NSOGrid;
  var NETWORK_TYPES = [
    { value: "", label: "Device default" },
    { value: "broadcast", label: "Broadcast" },
    { value: "non-broadcast", label: "Non-broadcast" },
    { value: "point-to-point", label: "Point-to-point" },
    { value: "point-to-multipoint", label: "Point-to-multipoint" },
  ];

  function text(value) {
    return value == null || value === "" ? "—" : String(value);
  }

  function editAnchor(row, title, fields) {
    if (!row.edit_url) return null;
    var edit = document.createElement("a");
    edit.href = "#";
    edit.className = "nso-popedit text-secondary";
    edit.title = "Edit OSPF configuration (editing takes ownership)";
    edit.setAttribute("aria-label", "Edit " + title);
    edit.dataset.peUrl = row.edit_url;
    edit.dataset.peTitle = title;
    edit.dataset.peFields = fields;
    edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
    return edit;
  }

  function fmtProcess(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    var primary = row.instance ? document.createElement("a") : document.createElement("span");
    if (row.instance) primary.href = row.instance.url;
    primary.textContent = "process " + text(row.process_id);
    wrap.appendChild(primary);
    var vrf = document.createElement("div");
    vrf.className = "small text-muted";
    vrf.textContent = "VRF " + text(row.vrf || "global");
    wrap.appendChild(vrf);
    return wrap;
  }

  function fmtRouterId(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-2";
    var value = document.createElement("span");
    value.className = row.router_id ? "font-monospace" : "text-muted";
    value.textContent = text(row.router_id);
    wrap.appendChild(value);
    var edit = editAnchor(row, "OSPF process " + row.process_id, "router_id:text:Router ID");
    if (edit) {
      edit.setAttribute("data-pe-v-router_id", row.router_id || "");
      wrap.appendChild(edit);
    }
    return wrap;
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
    var process = document.createElement("span");
    process.textContent = "process " + text(row.process_id);
    wrap.appendChild(process);
    var area = document.createElement("span");
    area.className = "text-muted small ms-2";
    area.textContent = "· area " + text(row.area_id);
    wrap.appendChild(area);
    return wrap;
  }

  function fmtConfig(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-2 flex-wrap";
    var network = document.createElement("span");
    network.className = row.network_type ? "badge text-bg-light border" : "text-muted small";
    network.textContent = row.network_type || "default network type";
    wrap.appendChild(network);
    var cost = document.createElement("span");
    cost.className = "small text-nowrap";
    cost.textContent = row.cost == null ? "default cost" : "cost " + row.cost;
    wrap.appendChild(cost);
    var mode = document.createElement("span");
    mode.innerHTML = G.boolBadge(
      row.passive,
      "passive",
      "text-bg-warning text-dark",
      "active",
      "text-bg-success",
    );
    wrap.appendChild(mode);

    var edit = editAnchor(
      row,
      row.iface.name + " OSPF",
      "area_id:text:Area,network_type:select:Network type,cost:number:Cost,passive:select:Mode",
    );
    if (edit) {
      edit.setAttribute("data-pe-v-area_id", row.area_id || "");
      edit.setAttribute("data-pe-v-network_type", row.network_type || "");
      edit.setAttribute("data-pe-v-cost", row.cost == null ? "" : row.cost);
      edit.setAttribute("data-pe-v-passive", row.passive ? "True" : "False");
      edit.setAttribute("data-pe-o-network_type", JSON.stringify(NETWORK_TYPES));
      edit.setAttribute(
        "data-pe-o-passive",
        JSON.stringify([
          { value: "False", label: "Active" },
          { value: "True", label: "Passive" },
        ]),
      );
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function instanceColumns() {
    return [
      {
        title: "Process",
        field: "_process",
        formatter: fmtProcess,
        sorter: "alphanum",
        widthGrow: 1.3,
        minWidth: 150,
        headerFilter: "input",
        headerFilterPlaceholder: "filter process / VRF…",
      },
      { title: "Router ID", field: "router_id", formatter: fmtRouterId, widthGrow: 1.2, minWidth: 145 },
      G.stateColumn(),
      G.lastSyncColumn(),
      G.acceptColumn(),
    ];
  }

  function interfaceColumns() {
    return [
      {
        title: "Interface",
        field: "_iface",
        formatter: fmtIface,
        sorter: "alphanum",
        widthGrow: 1.2,
        minWidth: 125,
        headerFilter: "input",
        headerFilterPlaceholder: "filter interface…",
      },
      { title: "OSPF binding", field: "_binding", formatter: fmtBinding, widthGrow: 1.2, minWidth: 135 },
      { title: "Interface config", field: "_config", formatter: fmtConfig, widthGrow: 1.8, minWidth: 205 },
      G.stateColumn({ widthGrow: 0.8, minWidth: 95 }),
      G.lastSyncColumn({ widthGrow: 0.8, minWidth: 105 }),
      G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
    ];
  }

  function mount(root, section) {
    var payloadEl = document.getElementById("nso-ospf-data");
    if (!root || !payloadEl || !G || ["instances", "interfaces"].includes(section) === false) return;
    return G.mount(root, {
      key: "ospf_" + section,
      payload: JSON.parse(payloadEl.textContent),
      extract: function (json) {
        return json[section];
      },
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: section === "instances" ? "No OSPF instances." : "No OSPF interfaces.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._process = [row.process_id || "", row.vrf || "global"].join(" ");
          if (row.iface) row._iface = row.iface.name;
          row._binding = [row.process_id || "", row.area_id || ""].join(" ");
          row._config = [row.network_type || "", row.cost == null ? "" : row.cost, row.passive ? "passive" : "active"].join(" ");
          return row;
        });
      },
      columns: section === "instances" ? instanceColumns() : interfaceColumns(),
    });
  }

  window.NSOGridOspf = { mount: mount };
})();
