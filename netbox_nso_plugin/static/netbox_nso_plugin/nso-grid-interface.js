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
    var rowsById = {};
    var activeDiffRow = null;
    var diffMode = "side-by-side";

    function comparableValue(value) {
      if (value == null) return "";
      var text = String(value);
      var lower = text.toLowerCase();
      return lower === "true" || lower === "false" ? lower : text;
    }

    function same(a, b) {
      return comparableValue(a) === comparableValue(b);
    }

    function displayValue(value) {
      if (value == null || value === "") return "—";
      if (value === true || value === "true" || value === "True") return "true";
      if (value === false || value === "false" || value === "False") return "false";
      return String(value);
    }

    function changedValue(lines, label, device, netbox) {
      if (same(device, netbox)) {
        lines.push("   " + label + ": " + displayValue(device));
      } else {
        lines.push("-  " + label + ": " + displayValue(device));
        lines.push("+  " + label + ": " + displayValue(netbox));
      }
    }

    function switchportLines(lines, cell) {
      var netbox = cell.netbox || {};
      lines.push(" switchport:");
      changedValue(lines, "mode", cell.mode, netbox.mode);
      changedValue(lines, "untagged_vlan", cell.untagged, netbox.untagged);
      lines.push("   tagged_vlans (differences only):");
      var device = (cell.tagged || []).map(Number).sort(function (a, b) { return a - b; });
      var desired = (netbox.tagged || []).map(Number).sort(function (a, b) { return a - b; });
      var deviceSet = new Set(device);
      var desiredSet = new Set(desired);
      Array.from(new Set(device.concat(desired))).sort(function (a, b) { return a - b; }).forEach(function (vid) {
        if (deviceSet.has(vid) && desiredSet.has(vid)) return;
        if (deviceSet.has(vid)) lines.push("-    - " + vid);
        else lines.push("+    - " + vid);
      });
    }

    function switchportDiffers(cell) {
      var netbox = cell.netbox || {};
      return (
        !same(cell.mode, netbox.mode) ||
        !same(cell.untagged, netbox.untagged) ||
        !same((cell.tagged || []).map(Number).sort().join(","), (netbox.tagged || []).map(Number).sort().join(","))
      );
    }

    function ipLines(lines, ip, ifaceName) {
      if (!ip.netbox || (ip.kind !== "drift" && ip.kind !== "conflict")) return;
      var section = [];
      changedValue(section, "presence", ip.device_present ? "present" : "not reported", ip.netbox.present ? "present" : "absent");
      changedValue(section, "address", ip.device_present ? ip.address : null, ip.netbox.address);
      changedValue(section, "vrf", ip.device_present ? (ip.vrf || "global") : null, ip.netbox.present ? (ip.netbox.vrf || "global") : null);
      changedValue(section, "assignment", ip.device_present ? ifaceName : null, ip.netbox.assignment);
      if (section.some(function (line) { return line.charAt(0) === "-" || line.charAt(0) === "+"; })) {
        lines.push(" ip " + ip.address + ":");
        Array.prototype.push.apply(lines, section);
      }
    }

    function interfaceUnifiedDiff(row) {
      var lines = [];
      ["enabled", "description"].forEach(function (key) {
        var c = row[key];
        if (!c || same(c.netbox_value, c.value)) return;
        lines.push(" " + key + ":");
        changedValue(lines, "value", c.value, c.netbox_value);
      });
      if (row.switchport && row.switchport.netbox && switchportDiffers(row.switchport)) {
        switchportLines(lines, row.switchport);
      }
      (row.ips || []).forEach(function (ip) {
        ipLines(lines, ip, row.iface.name);
      });
      if (!lines.length) return "";
      var before = lines.filter(function (line) { return line.charAt(0) !== "+"; }).length;
      var after = lines.filter(function (line) { return line.charAt(0) !== "-"; }).length;
      return (
        "--- interface " + row.iface.name + "\n" +
        "+++ interface " + row.iface.name + "\n" +
        "@@ -1," + before + " +1," + after + " @@\n" +
        lines.join("\n") + "\n"
      );
    }

    function fmtState(cell) {
      var d = cell.getRow().getData();
      var out = badge(d.state, d.label);
      if (interfaceUnifiedDiff(d)) {
        out +=
          ' <button type="button" class="btn btn-xs btn-outline-warning nso-if-diff" data-iface-id="' +
          esc(d.iface.id) +
          '" title="See exactly what differs between this device and NetBox" aria-label="Compare interface ' +
          esc(d.iface.name) +
          '"><span class="mdi mdi-not-equal-variant" aria-hidden="true"></span></button>';
      }
      return out;
    }

    function renderDiff() {
      var body = root.querySelector(".nso-if-diff-body");
      var unified = activeDiffRow && interfaceUnifiedDiff(activeDiffRow);
      if (!body || !unified) return;
      if (window.Diff2Html) {
        body.innerHTML = window.Diff2Html.html(unified, {
          drawFileList: false,
          matching: "lines",
          outputFormat: diffMode,
          colorScheme: document.documentElement.dataset.bsTheme === "dark" ? "dark" : "light",
        });
      } else {
        var pre = document.createElement("pre");
        pre.className = "border rounded bg-body-tertiary p-2 mb-0";
        pre.textContent = unified;
        body.replaceChildren(pre);
      }
    }

    function showDiff(row) {
      var modal = root.querySelector(".nso-if-diff-modal");
      var title = root.querySelector(".nso-if-diff-title");
      var unified = interfaceUnifiedDiff(row);
      if (!modal || !title || !unified) return;
      activeDiffRow = row;
      title.textContent = row.iface.name + " — Device vs NetBox";
      renderDiff();
      modal.classList.remove("d-none");
      modal.style.display = "flex";
      var close = modal.querySelector(".nso-if-diff-close");
      if (close) close.focus();
    }

    function hideDiff() {
      var modal = root.querySelector(".nso-if-diff-modal");
      if (!modal) return;
      activeDiffRow = null;
      modal.classList.add("d-none");
      modal.style.display = "none";
    }

    // ── column formatters ──────────────────────────────────────────────────────
    function fmtName(cell) {
      var d = cell.getRow().getData();
      var out = '<a href="' + esc(d.iface.url) + '" title="Open interface in NetBox">' + esc(d.iface.name) + "</a>";
      if (!d.link) return out;
      var cable = d.link.cable || {};
      var cableIcon = '<span class="mdi mdi-link-variant" aria-hidden="true"></span>';
      var cablePart = cable.url
        ? '<a href="' + esc(cable.url) + '" title="Open ' + esc(cable.label || "cable") + '">' + cableIcon + "</a>"
        : '<span title="Cabled">' + cableIcon + "</span>";
      var peerPart = d.link.peer
        ? ' <a class="nso-if-peer" href="' + esc(d.link.peer.url) + '" title="Cable peer: ' +
          esc(d.link.peer.device) + " / " +
          esc(d.link.peer.name) + '">' +
          esc(d.link.peer.device) + " / " + esc(d.link.peer.name) + "</a>"
        : ' <span title="The far end is not a single NetBox interface">cabled</span>';
      return out + '<div class="small text-muted nso-if-link">' + cablePart + peerPart + "</div>";
    }
    // Both attr cells show the NETBOX value — the intent an inline edit writes and
    // Apply enforces. The overlay's own value is the device MIRROR; rendering it
    // here is the bug where a freshly saved description kept showing (and, via the
    // flattened field the editor prefills from, re-offering) the device's old text.
    // The mirror survives as G.deviceNote when the server says the values differ.
    function fmtEnabled(cell) {
      var c = cell.getRow().getData().enabled;
      if (!c) return MUTED;
      var v = (c.netbox_value == null ? "" : String(c.netbox_value)).toLowerCase();
      var icon =
        v === "true"
          ? '<span class="mdi mdi-check-circle text-success"></span> Enabled'
          : v === "false"
            ? '<span class="mdi mdi-minus-circle text-muted"></span> Disabled'
            : esc(c.netbox_value == null ? "—" : c.netbox_value);
      return '<span class="text-nowrap">' + icon + cellBadge(c) + acceptBtn(c) + "</span>" + G.deviceNote(c);
    }
    var fmtDescription = G.valueFormatter("description", function (c) {
      var main = c.netbox_value == null || c.netbox_value === "" ? MUTED : esc(c.netbox_value);
      return main + G.deviceNote(c);
    });
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
      var wrap = document.createElement("span");
      ips.forEach(function (ip, index) {
        if (index) wrap.appendChild(document.createElement("br"));
        var line = document.createElement("span");
        line.className = "text-nowrap nso-if-ip";
        var code = document.createElement("code");
        code.textContent = ip.address;
        if (ip.url) {
          var detail = document.createElement("a");
          detail.href = ip.url;
          detail.title = "Open IP address in NetBox";
          detail.appendChild(code);
          line.appendChild(detail);
        } else {
          code.title = "Observed in NSO; no native NetBox IPAddress exists yet";
          line.appendChild(code);
        }
        var status = document.createElement("span");
        status.innerHTML =
          (ip.secondary ? ' <span class="badge text-bg-light">sec</span>' : "") +
          (ip.kind !== "in_sync" ? " " + badge(ip.kind, ip.label) : "") +
          acceptBtn(ip);
        line.appendChild(status);

        if (ip.edit_url) {
          var edit = document.createElement("a");
          edit.href = "#";
          edit.className = "nso-popedit ms-1";
          edit.title = ip.peer
            ? "Edit this IP; optionally change the cable peer in the same transaction"
            : "Edit this IP in NetBox";
          edit.setAttribute("aria-label", "Edit " + ip.address);
          edit.setAttribute("data-pe-url", ip.edit_url);
          edit.setAttribute(
            "data-pe-title",
            cell.getRow().getData().iface.name + (ip.peer ? " ↔ " + ip.peer.interface : " IP"),
          );
          edit.setAttribute(
            "data-pe-fields",
            "address:text:IP address" + (ip.peer ? ",peer_address:text:Peer IP (optional)" : ""),
          );
          edit.setAttribute("data-pe-v-address", ip.address);
          if (ip.peer) {
            edit.setAttribute("data-pe-v-peer_address", ip.peer.address);
          }
          edit.innerHTML = '<span class="mdi mdi-pencil" aria-hidden="true"></span>';
          line.appendChild(edit);
        }
        wrap.appendChild(line);
      });
      return wrap;
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
    function fmtNetwork(cell) {
      var d = cell.getRow().getData();
      var hasIps = !!(d.ips && d.ips.length);
      var hasSwitchport = !!d.switchport;
      if (!hasIps && !hasSwitchport) return MUTED;

      var wrap = document.createElement("span");
      if (hasIps) wrap.appendChild(fmtIps(cell));
      if (hasIps && hasSwitchport) wrap.appendChild(document.createElement("br"));
      if (hasSwitchport) {
        var switching = document.createElement("span");
        switching.innerHTML = fmtSwitchport(cell);
        wrap.appendChild(switching);
      }
      return wrap;
    }

    var mounted = G.mount(root, {
      key: "interface",
      payload: payload,
      jsonUrl: root.dataset.jsonUrl,
      // Let the device page own vertical scrolling. A nested 540px Tabulator
      // viewport makes a long interface list awkward to scan and navigate.
      maxHeight: false,
      placeholder: "No interface state yet — click Refresh from NSO or wait for the next sync.",
      // Flat helper fields so sorting / header-filtering works on plain strings.
      // NETBOX values, not the device mirror: Tabulator's inline editor prefills
      // from these fields, and an edit writes NetBox's value — prefilling the
      // mirror hands the operator back the text their own save just replaced.
      flatten: function (rows) {
        rowsById = {};
        return rows.map(function (r) {
          rowsById[String(r.iface.id)] = r;
          r._name = r.iface.name;
          r._desc = r.description && r.description.netbox_value ? r.description.netbox_value : "";
          r._enabled = r.enabled && r.enabled.netbox_value != null ? String(r.enabled.netbox_value).toLowerCase() : "";
          r._network = r.ips && r.ips.length ? "ip" : r.switchport ? "switchport" : "";
          return r;
        });
      },
      colFields: { enabled: "_enabled", description: "_desc", mtu: "mtu", network: "_network" },
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
        {
          title: "IP / Switchport",
          field: "_network",
          formatter: fmtNetwork,
          widthGrow: 1.7,
          minWidth: 140,
          headerSort: false,
        },
        G.stateColumn({ formatter: fmtState, minWidth: 120 }),
      ],
    });

    root.addEventListener("click", function (event) {
      var open = event.target.closest(".nso-if-diff");
      if (open) {
        event.preventDefault();
        showDiff(rowsById[open.dataset.ifaceId]);
        return;
      }
      var mode = event.target.closest("[data-nso-diff-mode]");
      if (mode && activeDiffRow) {
        diffMode = mode.dataset.nsoDiffMode;
        root.querySelectorAll("[data-nso-diff-mode]").forEach(function (candidate) {
          candidate.classList.toggle("active", candidate === mode);
        });
        renderDiff();
        return;
      }
      var modal = root.querySelector(".nso-if-diff-modal");
      if (event.target.closest(".nso-if-diff-close") || event.target === modal) hideDiff();
    });

    return mounted;
  }

  window.NSOGridInterface = { mount: mount };
})();
