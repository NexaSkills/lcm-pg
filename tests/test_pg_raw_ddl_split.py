"""Raw-DDL statement splitting for the PG adapter.

The sqlite->PG trigger translator emits a translated trigger as ONE string
(`DROP TRIGGER ... CASCADE; DROP FUNCTION ... CASCADE; CREATE FUNCTION ...$trg$
...$trg$; CREATE TRIGGER ...`). Under psycopg, when the connection is inside a
transaction the multi-command string goes down the extended-protocol path, which
executes only its FIRST statement — so the trailing CREATE TRIGGER is silently
dropped (assertion V4: source_insert_guard present as a _trg_ function but with
no trigger, 5/6 not 6/6). _raw_exec now splits top-level DDL strings and runs
each statement separately. These tests lock the splitter's behaviour without a
live database.
"""

from hermes_lcm.backends import postgres


def _translated_source_insert_guard():
    ddl = (
        "CREATE TRIGGER IF NOT EXISTS lcm_assertion_source_insert_guard\n"
        "BEFORE INSERT ON lcm_assertion_sources\n"
        "WHEN NOT EXISTS (SELECT 1 FROM messages AS m\n"
        "    WHERE m.store_id = NEW.source_store_id)\n"
        "BEGIN SELECT RAISE(ABORT, 'assertion source row is missing or "
        "metadata changed'); END;"
    )
    return postgres._translate_sqlite_trigger(ddl)


def test_splitter_isolates_create_trigger_after_function():
    translated = _translated_source_insert_guard()
    assert translated is not None
    parts = postgres._split_toplevel_statements(translated)
    heads = [p.lstrip()[:15].upper() for p in parts]
    assert any(h.startswith("CREATE FUNCTION") for h in heads)
    assert any(h.startswith("CREATE TRIGGER") for h in heads)
    # the whole point: the trailing CREATE TRIGGER is its OWN statement (not
    # glued onto CREATE FUNCTION), so it cannot be swallowed by the
    # extended-protocol first-statement-only rule.
    create_trigger_parts = [
        p for p in parts if p.lstrip().upper().startswith("CREATE TRIGGER")]
    assert len(create_trigger_parts) == 1
    assert "source_insert_guard" in create_trigger_parts[0]
    assert "$trg$" not in create_trigger_parts[0]


def test_splitter_does_not_split_inside_dollar_quoted_body():
    # The plpgsql body carries an interior `RAISE EXCEPTION 'x'; END IF;` —
    # none of those interior semicolons may split the CREATE FUNCTION apart.
    translated = _translated_source_insert_guard()
    parts = postgres._split_toplevel_statements(translated)
    func = next(p for p in parts if p.lstrip().upper().startswith("CREATE FUNCTION"))
    assert "$trg$" in func
    assert "RAISE EXCEPTION" in func
    assert "RETURN NEW" in func


def test_splitter_handles_escaped_single_quotes():
    # A doubled '' inside a string literal must not open a new quote span.
    sql = "SELECT 'it''s fine'; SELECT 2"
    assert postgres._split_toplevel_statements(sql) == [
        "SELECT 'it''s fine'",
        "SELECT 2",
    ]


def test_ddl_head_gate_rejects_data_statements():
    assert postgres._SQL_DDL_HEAD_RE.match("DROP TRIGGER foo ON t")
    assert postgres._SQL_DDL_HEAD_RE.match("CREATE FUNCTION f() ...")
    assert not postgres._SQL_DDL_HEAD_RE.match("SELECT 1; SELECT 2")
    assert not postgres._SQL_DDL_HEAD_RE.match("INSERT INTO t VALUES (1); COMMIT")


def test_single_statement_strings_pass_through_unsplit():
    assert postgres._split_toplevel_statements('SELECT 1') == ["SELECT 1"]
    assert postgres._split_toplevel_statements("  ") == []
