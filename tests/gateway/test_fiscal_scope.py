from unittest.mock import Mock

import pytest

from plugins.platforms.telegram.fiscal_scope import (
    AccountNotLinked,
    FiscalScope,
    InvalidCuit,
    assert_marked,
    mark_verified_sql,
    valid_cuit,
)


def item(**overrides):
    value = {
        "id": 12, "nombre": "Cliente sintético", "cuit": "20987654326",
        "slug": "cliente-sintetico", "study_id": 1, "relation_id": 101,
        "relation_revision": 3, "verified": False, "representative_id": 11,
        "holder_cuit": "20123456786",
    }
    value.update(overrides)
    return value


def test_cuit_checksum_and_invalid_input_fail_before_database_query():
    assert valid_cuit("20-12345678-6")
    assert not valid_cuit("20-12345678-9")
    query = Mock()
    with pytest.raises(InvalidCuit, match="CUIT_INVALID_CHECKSUM"):
        FiscalScope(query, "ARCA").search("7", "20-12345678-9")
    query.assert_not_called()


def test_unlinked_actor_is_rejected_without_exposing_contributors():
    query = Mock(return_value=[{"linked": False}])
    with pytest.raises(AccountNotLinked):
        FiscalScope(query, "ARCA").require_actor("7")
    assert "fn_actor_telegram_vinculado(7)" in query.call_args.args[0]


def test_scoped_search_and_by_id_use_actor_inside_database_function():
    query = Mock(return_value=[item()])
    scope = FiscalScope(query, "ARCA")
    assert scope.search("7", "cliente-sintetico") == [item()]
    assert "fn_buscar_contribuyente_fiscal" in query.call_args.args[0]
    assert "(\n            7," in query.call_args.args[0]
    assert scope.by_id("7", 12) == [item()]
    assert query.call_args.args[0].rstrip().endswith("s;")
    assert ",12\n" in query.call_args.args[0]


def test_invalid_stored_cuit_is_not_returned():
    query = Mock(return_value=[item(cuit="20987654321")])
    assert FiscalScope(query, "ARCA").by_id("7", 12) == []


def test_verification_requires_exact_cuit_and_expected_database_ack():
    sql = mark_verified_sql("7", item(), "ARCA", "20987654326")
    assert "fn_verificar_representacion_fiscal(7,101,3" in sql
    with pytest.raises(InvalidCuit, match="VERIFIED_SUBJECT_MISMATCH"):
        mark_verified_sql("7", item(), "ARCA", "20123456786")
    assert_marked([{"verified": True}])
    with pytest.raises(RuntimeError, match="REPRESENTATION_VERIFICATION_FAILED"):
        assert_marked([{"verified": False}])
