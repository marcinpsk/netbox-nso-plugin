/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Compact BGP peer/template grids. Only non-identity peer state is editable inline. */
(function () {
  "use strict";

  var G = window.NSOGrid;

  function badge(label, css, title) {
    var span = document.createElement("span");
    span.className = "badge " + css;
    span.textContent = label;
    if (title) span.title = title;
    return span;
  }

  function fmtLocal(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    var asn = document.createElement("span");
    asn.textContent = "AS" + (row.asn || "—");
    wrap.appendChild(asn);
    var vrf = document.createElement("div");
    vrf.className = "small text-muted text-truncate";
    vrf.textContent = "VRF " + (row.vrf || "global");
    wrap.appendChild(vrf);
    return wrap;
  }

  function fmtPeer(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    var peer = row.peer && row.peer.url ? document.createElement("a") : document.createElement("span");
    if (row.peer && row.peer.url) peer.href = row.peer.url;
    peer.className = "font-monospace";
    peer.textContent = row.peer_address || "—";
    wrap.appendChild(peer);
    if (row.disabled) {
      wrap.appendChild(
        badge("disabled", "text-bg-warning text-dark", "Deactivated on the device (Junos deactivate / admin down)"),
      );
    }
    return wrap;
  }

  function appendSessionDetails(wrap, row) {
    if (row.peer_group) {
      var group = badge("group " + row.peer_group, "text-bg-light border", "Peer-group " + row.peer_group);
      group.classList.add("d-inline-block", "text-truncate");
      group.style.maxWidth = "110px";
      wrap.appendChild(group);
    }
    var details = [];
    if (row.local_as) details.push("local AS " + row.local_as);
    if (row.source) details.push("source " + row.source);
    if (row.ttl != null) details.push("TTL " + row.ttl);
    if (row.bfd_enabled != null) details.push(row.bfd_enabled ? "BFD enabled" : "BFD disabled");
    if (details.length) {
      var label = row.source ? "source " + row.source : details[0];
      var session = badge(label, row.bfd_enabled ? "text-bg-info" : "text-bg-light border", details.join(" · "));
      session.classList.add("d-inline-block", "text-truncate");
      session.style.maxWidth = "115px";
      wrap.appendChild(session);
    }
  }

  function appendAddressFamilies(wrap, rows) {
    rows = rows || [];
    if (rows.length > 3) {
      var summary = rows.map(function (af) {
        var policies = [];
        if (af.inbound) policies.push("in " + af.inbound);
        if (af.outbound) policies.push("out " + af.outbound);
        return af.af + (af.enabled === false ? " off" : "") + (policies.length ? " (" + policies.join(" · ") + ")" : "");
      });
      wrap.appendChild(badge(rows.length + " AFs", "text-bg-secondary", summary.join(" · ")));
      return;
    }
    rows.forEach(function (af) {
      var policies = [];
      if (af.inbound) policies.push("in " + af.inbound);
      if (af.outbound) policies.push("out " + af.outbound);
      wrap.appendChild(
        badge(
          af.af + (af.enabled === false ? " off" : ""),
          af.enabled === false ? "text-bg-light border" : "text-bg-secondary",
          policies.length ? policies.join(" · ") : "No inbound or outbound policy",
        ),
      );
    });
  }

  function fmtSession(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    var remote = document.createElement("span");
    remote.className = "small text-nowrap";
    remote.textContent = row.remote_as ? "remote AS " + row.remote_as : "remote AS inherited";
    wrap.appendChild(remote);
    wrap.appendChild(
      badge(
        row.enabled === false ? "disabled" : "enabled",
        row.enabled === false ? "text-bg-warning text-dark" : "text-bg-success",
      ),
    );
    appendSessionDetails(wrap, row);
    appendAddressFamilies(wrap, row.address_families);

    if (row.edit_url) {
      var edit = document.createElement("a");
      edit.href = "#";
      edit.className = "nso-popedit text-secondary";
      edit.title = "Edit BGP session (editing takes ownership)";
      edit.setAttribute("aria-label", "Edit BGP peer " + row.peer_address);
      edit.dataset.peUrl = row.edit_url;
      edit.dataset.peTitle = row.peer_address + " BGP session";
      edit.dataset.peFields = "remote_as_str:text:Remote AS,enabled:select:State";
      edit.setAttribute("data-pe-v-remote_as_str", row.remote_as || "");
      edit.setAttribute("data-pe-v-enabled", row.enabled === false ? "False" : "True");
      edit.setAttribute(
        "data-pe-o-enabled",
        JSON.stringify([
          { value: "True", label: "Enabled" },
          { value: "False", label: "Disabled" },
        ]),
      );
      edit.innerHTML = '<span class="mdi mdi-pencil"></span>';
      wrap.appendChild(edit);
    }
    return wrap;
  }

  function fmtTemplate(cell) {
    var row = cell.getRow().getData();
    var span = document.createElement("span");
    span.textContent = (row.template && row.template.label) || row.template_name || "—";
    return span;
  }

  function fmtTemplateConfig(cell) {
    var row = cell.getRow().getData();
    var wrap = document.createElement("div");
    wrap.className = "d-flex align-items-center gap-1 flex-wrap";
    var remote = document.createElement("span");
    remote.className = "small";
    remote.textContent = row.remote_as ? "remote AS " + row.remote_as : "remote AS inherited";
    wrap.appendChild(remote);
    appendAddressFamilies(wrap, row.address_families);
    return wrap;
  }

  function peerColumns() {
    return [
      {
        title: "Local",
        field: "_local",
        formatter: fmtLocal,
        sorter: "alphanum",
        widthGrow: 0.8,
        minWidth: 100,
        headerFilter: "input",
        headerFilterPlaceholder: "ASN / VRF…",
      },
      {
        title: "Peer",
        field: "_peer",
        formatter: fmtPeer,
        sorter: "alphanum",
        widthGrow: 1,
        minWidth: 120,
        headerFilter: "input",
        headerFilterPlaceholder: "filter peer…",
      },
      { title: "Session & policy", field: "_session", formatter: fmtSession, widthGrow: 2.5, minWidth: 265 },
      G.stateColumn({ widthGrow: 0.6, minWidth: 85 }),
      G.lastSyncColumn({ widthGrow: 0.7, minWidth: 95 }),
      G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
    ];
  }

  function templateColumns() {
    return [
      {
        title: "Peer-group",
        field: "_template",
        formatter: fmtTemplate,
        sorter: "alphanum",
        widthGrow: 1,
        minWidth: 145,
        headerFilter: "input",
        headerFilterPlaceholder: "filter peer-group…",
      },
      { title: "Template config", field: "_config", formatter: fmtTemplateConfig, widthGrow: 2.4, minWidth: 300 },
      G.stateColumn({ widthGrow: 0.7, minWidth: 95 }),
      G.lastSyncColumn({ widthGrow: 0.8, minWidth: 105 }),
      G.acceptColumn({ widthGrow: 0.3, minWidth: 45 }),
    ];
  }

  function mount(root, section) {
    var payloadEl = document.getElementById("nso-bgp-data");
    if (!root || !payloadEl || !G || ["peers", "templates"].includes(section) === false) return;
    return G.mount(root, {
      key: "bgp_" + section,
      payload: JSON.parse(payloadEl.textContent),
      extract: function (json) {
        return json[section];
      },
      jsonUrl: root.dataset.jsonUrl,
      maxHeight: false,
      placeholder: section === "peers" ? "No BGP peers." : "No peer-group templates.",
      flatten: function (rows) {
        return rows.map(function (row) {
          row._local = [row.asn || "", row.vrf || "global"].join(" ");
          row._peer = row.peer_address || "";
          row._session = [row.remote_as || "", row.peer_group || "", row.source || ""].join(" ");
          row._template = row.template_name || "";
          row._config = [row.remote_as || "", ...(row.address_families || []).map(function (af) { return af.af; })].join(" ");
          return row;
        });
      },
      columns: section === "peers" ? peerColumns() : templateColumns(),
    });
  }

  window.NSOGridBgp = { mount: mount };
})();
