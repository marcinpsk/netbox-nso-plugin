/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Route-policy grid: compact object link, safe route-map rename, and row actions. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function plain(field) {
    return function (cell) {
      var value = cell.getRow().getData()[field];
      return value == null || value === "" ? G.MUTED : G.esc(value);
    };
  }

  function badge(text, classes, title) {
    var item = document.createElement("span");
    item.className = "badge " + classes;
    item.title = title;
    item.textContent = text;
    return item;
  }

  function fmtName(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("span");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";

    if (row.obj) {
      var link = document.createElement("a");
      link.href = row.obj.url;
      link.title = "Open the policy object and its entries in NetBox";
      link.textContent = row.name;
      wrap.appendChild(link);
    } else {
      var name = document.createElement("span");
      name.textContent = row.name;
      wrap.appendChild(name);
    }

    if (row.per_device) {
      wrap.appendChild(
        badge(
          "per-device",
          "text-bg-info",
          "Per-device — this object intentionally differs by device and is not deduplicated",
        ),
      );
    }
    if (row.unsupported && row.unsupported.length) {
      wrap.appendChild(
        badge(
          row.unsupported.length + " unsupported on NED",
          "text-bg-warning text-dark",
          "The device NED cannot hold these members: " + row.unsupported.join(", "),
        ),
      );
    }

    if (row.edit_url) {
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Rename this route map and update dependent intent";
      edit.setAttribute("aria-label", "Rename route map " + row.name);
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = "Rename " + row.name;
      edit.dataset.peFields = "object_name:text:Name";
      edit.setAttribute("data-pe-v-object_name", row.name);
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function fmtActions(cell) {
    var row = cell.getRow().getData();
    var out = "";
    if (row.diff_url) {
      out +=
        '<a href="' +
        G.esc(row.diff_url) +
        '" class="btn btn-xs btn-outline-warning" title="See exactly what differs between this device and NetBox"><span class="mdi mdi-not-equal-variant"></span></a>';
    }
    if (row.versions_url) {
      out +=
        '<a href="' +
        G.esc(row.versions_url) +
        '" class="btn btn-xs btn-outline-info" title="See every device version and choose the NetBox source"><span class="mdi mdi-source-branch"></span></a>';
    }
    return '<span class="d-flex gap-1">' + out + (G.acceptBtn(row) || "") + "</span>";
  }

  function mount(root) {
    var payloadEl = document.getElementById("nso-rp-data");
    if (!root || !payloadEl || !G) return;
    G.mount(root, {
      key: "route_policy",
      payload: JSON.parse(payloadEl.textContent),
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: "No route policy.",
      columns: [
        {
          title: "Family",
          field: "family",
          formatter: plain("family"),
          sorter: "alphanum",
          widthGrow: 0.8,
          minWidth: 105,
          headerFilter: "input",
          headerFilterPlaceholder: "filter…",
        },
        {
          title: "Policy",
          field: "name",
          formatter: fmtName,
          widthGrow: 2.4,
          minWidth: 210,
          headerFilter: "input",
        },
        G.stateColumn(),
        G.lastSyncColumn(),
        { title: "", field: "diff_url", headerSort: false, widthGrow: 0.8, minWidth: 112, formatter: fmtActions },
      ],
    });
  }

  window.NSOGridRoutePolicy = { mount: mount };
})();
