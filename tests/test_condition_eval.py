"""Condition evaluation against scan/register data."""
from aioebus.conditions import ConditionContext, evaluate, evaluate_all

CTX = ConditionContext(id_sw=808, id_hw=8004,
                       values={"Ehp_heatpumptype": 3, "Scan_id_product": "0010002315"})


def test_numeric_comparisons():
    assert evaluate("id_sw>=200", CTX) is True
    assert evaluate("id_sw<=140", CTX) is False
    assert evaluate("id_sw>=407", CTX) is True
    assert evaluate("id_sw<202", CTX) is False
    assert evaluate("id_sw>807", CTX) is True


def test_range_and_equality():
    assert evaluate("id_sw=350-400", CTX) is False     # 808 outside 350..400
    assert evaluate("id_sw=800-810", CTX) is True       # inclusive
    assert evaluate("id_hw=8004", CTX) is True
    assert evaluate("id_hw=6701", CTX) is False


def test_register_and_product_list():
    assert evaluate("Ehp_heatpumptype=3", CTX) is True
    assert evaluate("Ehp_heatpumptype=2", CTX) is False
    assert evaluate("Scan_id_product='0010002315';'0010002316'", CTX) is True
    assert evaluate("Scan_id_product='9999999999'", CTX) is False


def test_unknown_operand_is_none():
    assert evaluate("Unknownthing=5", CTX) is None
    assert evaluate("id_sw>=200", ConditionContext()) is None  # id_sw not known


def test_evaluate_all_combination():
    assert evaluate_all(["id_sw>=200", "id_hw=8004"], CTX) is True
    assert evaluate_all(["id_sw>=200", "id_sw<=140"], CTX) is False   # any False
    assert evaluate_all(["id_sw>=200", "Unknownthing=5"], CTX) is None  # else unknown
    assert evaluate_all([], CTX) is True
    assert evaluate_all(None, CTX) is True
