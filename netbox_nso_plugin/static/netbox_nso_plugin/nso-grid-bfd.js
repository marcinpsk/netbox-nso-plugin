/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact BFD grid: the mode and three timer values form one editable intent row. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function fmtIface(cell) {
    var row = cell.getRow().getData();
    var link = document.createElement("a");
    link.href = row.iface.url;
    link.title = "Open interface in NetBox";
    link.textContent = row.iface.name;
    return link;
  }

  function value(value) {
    return value == null ? "" : String(value);
  }

  function fmtConfig(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-2 flex-wrap";

    var mode = document.createElement("span");
    mode.innerHTML = G.boolBadge(
      row.micro_bfd,
      "micro-BFD",
      "text-bg-info",
      "normal",
      "text-bg-light border",
    );
    wrap.appendChild(mode);

    var timers = document.createElement("span");
    timers.className = "small text-nowrap";
    if (row.min_tx == null && row.min_rx == null && row.multiplier == null) {
      timers.classList.add("text-muted");
      timers.textContent = "device defaults";
    } else {
      timers.textContent =
        "TX " + (row.min_tx == null ? "—" : row.min_tx) +
        " · RX " + (row.min_rx == null ? "—" : row.min_rx) +
        " · ×" + (row.multiplier == null ? "—" : row.multiplier);
    }
    wrap.appendChild(timers);

    if (row.edit_url) {
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Edit BFD configuration (editing takes ownership)";
      edit.setAttribute("aria-label", "Edit BFD configuration for " + row.iface.name);
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = row.iface.name + " BFD";
      edit.dataset.peFields =
        "min_tx:number:Min TX (ms),min_rx:number:Min RX (ms),multiplier:number:Multiplier,micro_bfd:select:Mode";
      edit.setAttribute("data-pe-v-min_tx", value(row.min_tx));
      edit.setAttribute("data-pe-v-min_rx", value(row.min_rx));
      edit.setAttribute("data-pe-v-multiplier", value(row.multiplier));
      edit.setAttribute("data-pe-v-micro_bfd", row.micro_bfd ? "True" : "False");
      edit.setAttribute(
        "data-pe-o-micro_bfd",
        JSON.stringify([
          { value: "False", label: "Normal BFD" },
          { value: "True", label: "micro-BFD" },
        ]),
      );
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function mount(root) {
    var payloadEl = document.getElementById("nso-bfd-data");
    if (!root || !payloadEl || !G) return;
    G.mount(root, {
      key: "bfd",
      payload: JSON.parse(payloadEl.textContent),
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: "No BFD-configured interfaces found on this device.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._iface = row.iface.name;
          row._mode = row.micro_bfd ? "micro-bfd" : "normal";
          return row;
        });
      },
      columns: [
        {
          title: "Interface",
          field: "_iface",
          formatter: fmtIface,
          sorter: "alphanum",
          widthGrow: 1.2,
          minWidth: 140,
          headerFilter: "input",
          headerFilterPlaceholder: "filter interface…",
        },
        {
          title: "BFD config",
          field: "_mode",
          formatter: fmtConfig,
          widthGrow: 2,
          minWidth: 230,
          headerFilter: "list",
          headerFilterParams: {
            values: { "": "All", "micro-bfd": "micro-BFD", normal: "normal" },
          },
        },
        G.stateColumn(),
        G.lastSyncColumn(),
        G.acceptColumn(),
      ],
    });
  }

  window.NSOGridBfd = { mount: mount };
})();
