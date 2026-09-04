"""Contratto statico della GUI WP10.

Questi test non richiedono un browser: proteggono gli endpoint e gli anchor
accessibili che costituiscono il contratto fra la pagina e l'API live.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "scripts" / "static" / "index.html"


def source() -> str:
    return HTML.read_text(encoding="utf-8")


def script() -> str:
    matches = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", source())
    assert len(matches) == 1
    return matches[0]


def test_frontend_has_three_value_sections_and_maxbid_scarcity_calibration():
    text = source()
    for anchor in (
        'id="pc-market-section"',
        'id="pc-fantasy-section"',
        'id="pc-team-section"',
        'id="pc-market"',
        'id="pc-fantasy"',
        'id="pc-team-value"',
        'id="pc-caps"',
        'id="pc-scarcity"',
        'id="pc-calibration"',
        "expected_if_applied",
        "binding_cap",
        "market_cap",
        "reserve_cap",
        "role_cap",
        "opportunity_cap",
    ):
        assert anchor in text, anchor


def test_frontend_exposes_wp10_endpoints_and_controls():
    text = source()
    for endpoint in (
        "/api/state",
        "/api/eval?",
        "/api/sold",
        "/api/unsold",
        "/api/undo",
        "/api/trend",
        "/api/calibration",
        "/api/forward/latest",
        "/api/forward/snapshot",
        "/api/forward/simulate",
        "/api/backup",
        "/api/restore",
        "/api/correct",
        "/api/config",
        "/api/export/svincolati",
        "/api/export/rose",
    ):
        assert endpoint in text, endpoint
    for control in (
        'id="simulate-btn"',
        'id="sim-runs"',
        'id="sim-seed"',
        'id="sim-team"',
        'id="sim-order"',
        'id="sim-players"',
        'id="sim-probs"',
        'id="sim-combos"',
        'id="sim-feasibility"',
        'id="persistence-status"',
        'id="backup-btn"',
        'id="restore-file"',
        'id="restore-btn"',
        'id="correct-btn"',
        'id="slot-P"',
        'id="slot-D"',
        'id="slot-C"',
        'id="slot-A"',
        'id="form-P"',
        'id="form-D"',
        'id="form-C"',
        'id="form-A"',
        'id="cfg-tit"',
        'id="config-minimum-roster-cost"',
        'id="config-feasibility-errors"',
        'id="config-feasibility-warnings"',
        'id="pc-maxbid"',
        'id="calibration-panel"',
        'id="persistence-bar"',
    ):
        assert control in text, control


def test_frontend_has_five_accessible_workspaces_and_data_views():
    text = source()
    js = script()
    for view in ("asta", "mercato", "rose", "analisi", "simulatore"):
        assert f'id="tab-{view}"' in text
        assert f'id="view-{view}"' in text
        assert f'aria-controls="view-{view}"' in text
        assert f'aria-labelledby="tab-{view}"' in text
    for anchor in (
        'role="tablist"',
        'role="tabpanel"',
        'id="market-search"',
        'id="market-sort"',
        'id="market-direction"',
        'id="market-table"',
        'id="market-status"',
        'id="roster-team"',
        'id="roster-role"',
        'id="roster-table"',
        'id="roster-summary"',
        "/api/svincolati?",
        "/api/rose?",
    ):
        assert anchor in text, anchor
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in js
    assert "location.hash.slice(1)" in js
    assert "history.pushState" in js
    assert 'await activateView("asta")' in js
    assert "await pick(pid, { focusPrice: false })" in js
    assert '$("pc-name").focus();' in js
    assert "data.count === 1" in js
    assert '"font-size": 24' in js
    assert "loadMarket" in js and "loadRosters" in js
    assert "marketPageSize = 30" in js
    assert "new URLSearchParams" in js
    for parameter in ("sort_by", "direction", "offset", "limit"):
        assert parameter in js
    # State-changing actions must not leave hidden workspace data stale.
    assert 'if (activeView === "simulatore") await loadLatest();' in js
    assert '$("roster-team").value = "";' in js
    assert '$("roster-summary").replaceChildren();' in js
    assert '$("roster-body").replaceChildren();' in js


def test_rose_summary_renders_dynamic_slots_and_mantra_roles():
    js = script()
    assert 'const rosterRoleSelect = $("roster-role")' in js
    assert "Object.entries(item.filled_slots)" in js
    assert "row.role_display || row.role" in js


def test_recent_operations_list_has_readable_spacing():
    text = source()
    assert "#recent li {" in text
    assert "gap: 8px" in text


def test_frontend_allows_roster_purchase_deletion():
    text = source()
    js = script()
    for anchor in (
        '<th scope="col">Azioni</th>',
        '"delete-player-btn"',
        "deleteRosterPlayer(row, deleteButton)",
        "Rimuovere ${row.player} dalla rosa ${row.team}?",
        "/api/rose/${encodeURIComponent(row.pid)}",
        'method: "DELETE"',
        "td.colSpan = 10",
    ):
        assert anchor in text, anchor
    assert "await refreshAll();" in js
    assert "resetSelection();" in js


def test_frontend_exposes_random_auction_modality():
    text = source()
    js = script()
    for anchor in (
        'id="cfg-auction-mode"',
        '<option value="random">',
        'id="nomination-panel"',
        'id="nomination-status"',
        "/api/nomination?team=",
        "override manuale",
    ):
        assert anchor in text, anchor
    assert "renderNomination(nextNomination)" in js
    assert "if (nextNomination.current) await pick(nextNomination.current.key)" in js
    assert js.count("await refreshAll();") >= 6


def test_frontend_uses_safe_dom_apis_and_live_statuses():
    text = source()
    js = script()
    assert not re.search(r"\.innerHTML\s*=|\.insertAdjacentHTML\s*=", text)
    assert not re.search(r"\beval\s*\(", js)
    assert "textContent" in js
    assert "createElementNS" in js
    assert 'aria-live="polite"' in text
    assert 'role="status"' in text
    assert "window.confirm" in js
    assert "new Option" in js


def test_frontend_no_global_horizontal_overflow_guards():
    """P1-1: CSS guard contro l'overflow orizzontale globale su mobile."""
    text = source()
    assert "overflow-x: clip" in text
    assert "min-width: 0" in text
    assert "minmax(0, 1fr)" in text
    assert (
        "main {\n        display: grid;\n        grid-template-columns: minmax(0, 460px) minmax(0, 1fr);"
        in text
    )
    assert ".table-wrap {" in text and "overflow-x: auto" in text
    # niente più label che avvolgono l'input file nascosto
    assert ".file-label input" not in text


def test_frontend_search_is_aria_combobox():
    """P2-1: pattern ARIA combobox/listbox/option per la ricerca."""
    text = source()
    for anchor in (
        'role="combobox"',
        'aria-expanded="false"',
        'aria-controls="results"',
        'aria-autocomplete="list"',
        'aria-activedescendant=""',
        'role="listbox"',
    ):
        assert anchor in text, anchor
    js = script()
    assert "aria-activedescendant" in js
    assert '"aria-selected", "true"' in js
    assert '"role", "option"' in js
    assert "syncCombobox" in js
    assert "result-" in js
    assert re.search(
        r'selected = data;\s*\$\("results"\)\.replaceChildren\(\);\s*'
        r"setActive\(-1\);\s*syncCombobox\(0\);",
        js,
    )


def test_frontend_sim_tables_accessible_headers_and_units():
    """P1-3: thead/th scope="col", caption e unità nelle tabelle del simulatore."""
    text = source()
    for table_id in ("sim-players", "sim-probs", "sim-combos"):
        assert f'<table id="{table_id}"' in text, table_id
        assert f'id="{table_id}-cap"' in text, table_id + " caption"
        assert f'id="{table_id}-body"' in text, table_id + " tbody id"
        # ogni tabella deve contenere th con scope e ilad > 3 colonne
        m = re.search(rf'<table id="{table_id}".*?</table>', text, flags=re.DOTALL)
        assert m, table_id
        block = m.group(0)
        assert "<thead>" in block and "</thead>" in block, table_id
        assert '<th scope="col">' in block, table_id + " scope"
        assert block.count("<th") >= 3, table_id + " colonne"
        assert "<tbody" in block and "</tbody>" in block, table_id
    for unit in (
        "Media (cr)",
        "P10 (cr)",
        "P50 (cr)",
        "P90 (cr)",
        "Sale prob. (%)",
        "Δ model (cr)",
        "Prob. (%)",
        "Budget medio (cr)",
        "Incompleta (%)",
    ):
        assert unit in text, unit


def test_frontend_sim_output_collapsible_and_paginated():
    """P1-4: sezioni collassabili, pager e filtri prima delle tabelle estese."""
    text = source()
    for anchor in (
        '<details class="subpanel" id="panel-players" open>',
        '<details class="subpanel" id="panel-probs">',
        '<details class="subpanel" id="panel-combos" open>',
        '<details class="subpanel" id="panel-feas" open>',
        'id="sim-page-prev-players"',
        'id="sim-page-next-players"',
        'id="sim-page-info-players"',
        'id="sim-page-prev-probs"',
        'id="sim-page-next-probs"',
        'id="sim-page-info-probs"',
        'id="sim-page-prev-combos"',
        'id="sim-page-next-combos"',
        'id="sim-page-info-combos"',
        'class="sim-filter"',
    ):
        assert anchor in text, anchor
    js = script()
    assert "simPage.players = 0" in js  # reset pagina al cambio filtro
    assert "Math.max(0, simPage[key] - 1)" in js  # pager prev clamp
    assert "paginate(" in js
    assert 'aria-label="Pagina precedente probabilità"' in text


def test_frontend_restore_file_keyboard_accessible():
    """P1-5: ripristino JSON come vero button che attiva il file input."""
    text = source()
    assert '<button id="restore-btn" class="file-label" type="button">' in text
    assert 'id="restore-file"' in text
    assert 'class="sr-only"' in text
    assert 'aria-label="Scegli un file di backup JSON da ripristinare"' in text
    js = script()
    assert re.search(
        r'\$\("restore-btn"\)\.addEventListener\(\s*"click",\s*\(\)\s*=>\s*'
        r'\$\("restore-file"\)\.click\(\),?\s*\)',
        js,
    )
    assert ".sr-only" in text


def test_frontend_sim_params_hydration_and_summary():
    """P1-6: idratazione Runs/Seed/Team/Ordine e riepilogo params del report."""
    js = script()
    text = source()
    assert "simulate_params" in js
    assert "renderSimSummary" in js
    assert '$("sim-runs").value = sp.runs' in js
    assert '$("sim-seed").value = sp.seed' in js
    assert "sel.value = sp.team" in js
    assert '$("sim-order").value = sp.player_order' in js
    assert 'id="sim-summary"' in text
    assert 'class="sim-summary-note"' in text
    assert 'class="summary-chips"' in text


def test_frontend_persistence_chips_and_local_timestamp():
    """P2-2: stato persistenza in chip separati e data in formato locale."""
    text = source()
    js = script()
    assert "persist-on" in text or "persist-on" in js
    assert "persist-off" in js
    assert "fmtWhen" in js
    assert "toLocaleString" in js
    assert "event_seq" in js
    assert "last_saved" in js


def test_team_change_preserves_manually_edited_price():
    js = script()
    assert "preservePrice = null" in js
    assert 'preservePrice: $("price").value' in js
    assert '$("price").value = preservePrice' in js
    assert "focusPrice: false" in js


def test_frontend_script_passes_node_check():
    result = subprocess.run(
        ["node", "--check", "-"],
        input=script(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
