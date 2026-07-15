/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* nso-grid-interface — the Interfaces panel's column definitions for nso-grid.js.
 *
 * The consolidated per-interface grid: one row per interface, a column per
 * attribute; description/enabled edit in place (NetBox takes ownership, same as
 * Accept). Lives in a static file rather than inline in interface.html so the
 * flatten/formatter logic is under the JS test suite; the lazy fragment only
 * calls NSOGridInterface.mount(root).
 */
(function () {
  "use strict";

  function mount(root) {
    if (!root || !window.NSOGrid) return null;
    var payloadEl = document.getElementById("nso-ifg-data");
    if (!payloadEl) return null;
    var payload = JSON.parse(payloadEl.textContent);
    var G = window.NSOGrid;
    var esc = G.esc,
      badge = G.badge,
      cellBadge = G.cellBadge,
      acceptBtn = G.acceptBtn,
      MUTED = G.MUTED;

    // ── column formatters ──────────────────────────────────────────────────────
    function fmtName(cell) {
      var d = cell.getRow().getData();
      return '<a href="' + esc(d.iface.url) + '" title="Open interface in NetBox">' + esc(d.iface.name) + "</a>";
    }
    function fmtEnabled(cell) {
      var c = cell.getRow().getData().enabled;
      if (!c) return MUTED;
      var v = (c.value == null ? "" : String(c.value)).toLowerCase();
      var icon =
        v === "true"
          ? '<span class="mdi mdi-check-circle text-success"></span> Enabled'
          : v === "false"
            ? '<span class="mdi mdi-minus-circle text-muted"></span> Disabled'
            : esc(c.value == null ? "—" : c.value);
      return '<span class="text-nowrap">' + icon + cellBadge(c) + acceptBtn(c) + "</span>";
    }
    var fmtDescription = G.valueFormatter("description");
    function fmtMtu(cell) {
      var d = cell.getRow().getData();
      var c = d.mtu;
      if (!c) return MUTED;
      var t = [c.l2, c.ip, c.mpls]
        .map(function (v) {
          return v == null ? "—" : esc(v);
        })
        .join(" / ");
      var port = c.bound_port ? '<div class="text-muted small">port ' + esc(c.bound_port) + "</div>" : "";
      // The value itself is a popedit anchor (three-field popover) — DOM-built so the
      // data attributes are safely escaped whatever the values contain.
      var wrap = document.createElement("span");
      var a = document.createElement("a");
      a.href = "#";
      a.className = "nso-popedit font-monospace text-nowrap";
      a.title = "Click to edit MTU — an unowned row becomes 'changed' (needs Accept)";
      a.setAttribute("data-pe-url", root.dataset.mtuEditUrl.replace("/0/", "/" + c.pk + "/"));
      a.setAttribute("data-pe-title", d.iface.name + " MTU");
      a.setAttribute("data-pe-fields", "l2_mtu:number:L2 MTU,ip_mtu:number:IP MTU,mpls_mtu:number:MPLS MTU");
      a.setAttribute("data-pe-v-l2_mtu", c.l2 == null ? "" : c.l2);
      a.setAttribute("data-pe-v-ip_mtu", c.ip == null ? "" : c.ip);
      a.setAttribute("data-pe-v-mpls_mtu", c.mpls == null ? "" : c.mpls);
      a.innerHTML = t;
      wrap.appendChild(a);
      var rest = document.createElement("span");
      rest.innerHTML = cellBadge(c) + acceptBtn(c) + port;
      wrap.appendChild(rest);
      return wrap;
    }
    function fmtIps(cell) {
      var ips = cell.getRow().getData().ips || [];
      if (!ips.length) return MUTED;
      return ips
        .map(function (ip) {
          var sec = ip.secondary ? ' <span class="badge text-bg-light">sec</span>' : "";
          var b = ip.kind !== "in_sync" ? " " + badge(ip.kind) : "";
          return "<code>" + esc(ip.address) + "</code>" + sec + b;
        })
        .join("<br>");
    }
    function fmtSwitchport(cell) {
      var c = cell.getRow().getData().switchport;
      if (!c) return MUTED;
      var mode = c.mode ? '<span class="badge text-bg-light">' + esc(c.mode) + "</span> " : "";
      var vlans = [];
      if (c.untagged != null) vlans.push("u:" + esc(c.untagged));
      if (c.tagged && c.tagged.length) vlans.push("t:" + c.tagged.map(esc).join(","));
      return mode + esc(vlans.join(" ")) + cellBadge(c) + acceptBtn(c);
    }

    return G.mount(root, {
      key: "interface",
      payload: payload,
      jsonUrl: root.dataset.jsonUrl,
      placeholder: "No interface state yet — click Refresh from NSO or wait for the next sync.",
      // Flat helper fields so sorting / header-filtering works on plain strings.
      flatten: function (rows) {
        return rows.map(function (r) {
          r._name = r.iface.name;
          r._desc = r.description && r.description.value ? r.description.value : "";
          r._enabled = r.enabled && r.enabled.value != null ? String(r.enabled.value).toLowerCase() : "";
          return r;
        });
      },
      colFields: { enabled: "_enabled", description: "_desc", mtu: "mtu", ips: "ips", switchport: "switchport" },
      cellKeys: { _enabled: "enabled", _desc: "description" },
      columns: [
        {
          title: "Interface",
          field: "_name",
          formatter: fmtName,
          sorter: "alphanum",
          widthGrow: 1.5,
          minWidth: 140,
          headerFilter: "input",
          headerFilterPlaceholder: "filter name…",
        },
        {
          title: "Enabled",
          field: "_enabled",
          formatter: fmtEnabled,
          widthGrow: 1.1,
          minWidth: 120,
          editable: function (cell) {
            return !!cell.getRow().getData().enabled;
          },
          editor: "list",
          editorParams: { values: { true: "Enabled", false: "Disabled" } },
          headerFilter: "list",
          headerFilterParams: { values: { "": "All", true: "Enabled", false: "Disabled" } },
          cssClass: "nso-editable",
        },
        {
          title: "Description",
          field: "_desc",
          formatter: fmtDescription,
          widthGrow: 2.4,
          minWidth: 180,
          editable: function (cell) {
            return !!cell.getRow().getData().description;
          },
          editor: "input",
          headerFilter: "input",
          headerFilterPlaceholder: "filter…",
          cssClass: "nso-editable",
        },
        { title: "MTU L2 / IP / MPLS", field: "mtu", formatter: fmtMtu, widthGrow: 1.3, minWidth: 150, headerSort: false },
        { title: "IPs", field: "ips", formatter: fmtIps, widthGrow: 1.4, minWidth: 130, headerSort: false },
        { title: "Switchport", field: "switchport", formatter: fmtSwitchport, widthGrow: 1.1, minWidth: 110, headerSort: false },
        G.stateColumn(),
      ],
    });
  }

  window.NSOGridInterface = { mount: mount };
})();
