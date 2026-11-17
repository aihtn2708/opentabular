import pytest

from opentabular.dax import DaxSyntaxError, parse_dax
from opentabular.dax import ast as A


def test_precedence_follows_dax():
    # ^ binds tighter than the sign, so -2^2 is -(2^2)
    assert parse_dax("-2^2") == A.Unary("-", A.Binary("^", A.Num("2"), A.Num("2")))
    # * before +
    assert parse_dax("1 + 2 * 3") == A.Binary("+", A.Num("1"), A.Binary("*", A.Num("2"), A.Num("3")))
    # left associative
    assert parse_dax("8 - 3 - 2") == A.Binary("-", A.Binary("-", A.Num("8"), A.Num("3")), A.Num("2"))
    # NOT binds looser than comparison and IN, tighter than &&
    node = parse_dax('NOT T[c] IN {"a", "b"} && TRUE')
    assert isinstance(node, A.Binary) and node.op == "&&"
    assert isinstance(node.left, A.Unary) and node.left.op == "NOT"
    assert node.left.operand.op == "IN"
    # && before ||
    assert parse_dax("a || b && c").op == "||"


def test_references_and_calls():
    assert parse_dax("'My Table'[My Col]") == A.ColumnRef("My Table", "My Col")
    assert parse_dax("Sales[Amount]") == A.ColumnRef("Sales", "Amount")
    assert parse_dax("[Total Sales]") == A.BareRef("Total Sales")
    assert parse_dax("'It''s'[a]]b]") == A.ColumnRef("It's", "a]b")
    assert parse_dax("sum ( Sales[Amount] )") == A.Call("SUM", (A.ColumnRef("Sales", "Amount"),))
    assert parse_dax("BLANK()") == A.Call("BLANK", ())
    assert parse_dax('"say ""hi"""') == A.Str('say "hi"')
    assert parse_dax("= 1.5e3") == A.Num("1.5e3")


def test_variables_and_comments():
    node = parse_dax(
        """
        -- running total
        VAR x = SUM(Sales[Amount])  // first
        VAR y = /* inline */ 2
        RETURN DIVIDE(x, y)
        """
    )
    assert isinstance(node, A.VarBlock)
    assert [n for n, _ in node.vars] == ["x", "y"]
    assert node.body == A.Call("DIVIDE", (A.NameRef("x"), A.NameRef("y")))
    nested = parse_dax("IF(TRUE, VAR a = 1 RETURN a, 0)")
    assert isinstance(nested.args[1], A.VarBlock)


@pytest.mark.parametrize("bad", ["SUM(", "1 +", "VAR x = 1", "[a] [b]", "'open", "1 ? 2", "{1, 2"])
def test_syntax_errors(bad):
    with pytest.raises(DaxSyntaxError):
        parse_dax(bad)


def test_error_reports_position():
    with pytest.raises(DaxSyntaxError) as e:
        parse_dax("SUM(Sales[Amount])\n  +")
    assert (e.value.line, e.value.col) == (2, 4)
